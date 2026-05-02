"""Tests for Step 10 — Admin/Control message handlers in kharej/dispatcher.py.

Covers:
- health.ping → health.pong (echoed request_id, queue_depth, worker_version)
- user.whitelist.add / user.whitelist.remove
- user.block.add / user.block.remove
- admin.settings.update (merges keys, ignores unknown, ack contains effective_config)
- admin.clearcache (lru / isrc / all) → admin.ack status=ok
- admin.cookies.update → downloads from S2, verifies SHA-256, replaces cookies.txt

All tests use:
- AsyncMock for rubika.send (captures outbound messages)
- Real AccessControl / KharejSettings on temp directories
- MagicMock for S2Client (cookies.update tests)
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kharej.access_control import AccessControl
from kharej.contracts import (
    AdminAck,
    AdminClearcache,
    AdminCookiesUpdate,
    AdminSettingsUpdate,
    HealthPing,
    HealthPong,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
)
from kharej.dispatcher import Dispatcher
from kharej.progress_reporter import ProgressReporter
from kharej.settings import KharejSettings

# ---------------------------------------------------------------------------
# Temp-dir pool
# ---------------------------------------------------------------------------

_TEMP_DIRS: list[str] = []


def _cleanup() -> None:
    for td in _TEMP_DIRS:
        shutil.rmtree(td, ignore_errors=True)


atexit.register(_cleanup)


def _temp_dir() -> Path:
    td = tempfile.mkdtemp()
    _TEMP_DIRS.append(td)
    return Path(td)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)


def _make_rubika_mock() -> AsyncMock:
    """Return an AsyncMock whose .send is a recording coroutine."""
    rubika = AsyncMock()
    rubika.send = AsyncMock()
    return rubika


def _make_dispatcher(
    *,
    rubika: AsyncMock | None = None,
    access: AccessControl | None = None,
    settings: KharejSettings | None = None,
    s2: Any = None,
    cookies_path: Path | None = None,
) -> tuple[Dispatcher, AsyncMock]:
    """Build a Dispatcher configured for admin-handler tests.

    Returns (dispatcher, rubika_send_mock).
    """
    if rubika is None:
        rubika = _make_rubika_mock()

    if s2 is None:
        s2 = MagicMock()

    td = _temp_dir()
    if access is None:
        access = AccessControl(state_path=td / "access_state.json")
    if settings is None:
        settings = KharejSettings(state_path=td / "kharej_settings.json")

    send_mock: AsyncMock = rubika.send
    progress = ProgressReporter(send_mock, throttle_sec=0.0)

    dispatcher = Dispatcher(
        s2=s2,
        rubika=rubika,
        access=access,
        settings=settings,
        progress=progress,
        downloaders={},
        cookies_path=cookies_path or (td / "cookies.txt"),
    )
    return dispatcher, send_mock


def _sent_msgs(send_mock: AsyncMock) -> list[Any]:
    """Collect all positional args from every call to *send_mock*."""
    return [call.args[0] for call in send_mock.call_args_list if call.args]


# ---------------------------------------------------------------------------
# health.ping → health.pong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ping_returns_pong() -> None:
    dispatcher, send_mock = _make_dispatcher()
    ping = HealthPing(ts=_NOW, request_id="req-abc-123")
    await dispatcher.handle_message(ping)

    msgs = _sent_msgs(send_mock)
    assert len(msgs) == 1
    pong = msgs[0]
    assert isinstance(pong, HealthPong)
    assert pong.request_id == "req-abc-123"
    assert pong.queue_depth == 0
    assert pong.worker_version  # non-empty
    assert pong.uptime_sec >= 0
    assert pong.disk_free_gb >= 0.0


@pytest.mark.asyncio
async def test_health_ping_echoes_request_id() -> None:
    dispatcher, send_mock = _make_dispatcher()
    ping = HealthPing(ts=_NOW, request_id="unique-id-42")
    await dispatcher.handle_message(ping)

    pong = _sent_msgs(send_mock)[0]
    assert isinstance(pong, HealthPong)
    assert pong.request_id == "unique-id-42"


# ---------------------------------------------------------------------------
# user.whitelist.add / user.whitelist.remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whitelist_add_persists_and_acks() -> None:
    td = _temp_dir()
    ac = AccessControl(state_path=td / "access.json")
    dispatcher, send_mock = _make_dispatcher(access=ac)

    msg = UserWhitelistAdd(ts=_NOW, user_id="user-aaa")
    await dispatcher.handle_message(msg)

    # User should now be in the whitelist.
    assert "user-aaa" in ac.whitelist

    # An admin.ack should have been sent.
    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "user.whitelist.add"
    assert acks[0].status == "ok"


@pytest.mark.asyncio
async def test_whitelist_remove_persists_and_acks() -> None:
    td = _temp_dir()
    ac = AccessControl(state_path=td / "access.json")
    # Pre-populate.
    await ac.handle_whitelist_add(
        UserWhitelistAdd(ts=_NOW, user_id="user-bbb"), send=AsyncMock()
    )
    dispatcher, send_mock = _make_dispatcher(access=ac)

    msg = UserWhitelistRemove(ts=_NOW, user_id="user-bbb")
    await dispatcher.handle_message(msg)

    assert "user-bbb" not in ac.whitelist

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "user.whitelist.remove"
    assert acks[0].status == "ok"


# ---------------------------------------------------------------------------
# user.block.add / user.block.remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_add_persists_and_acks() -> None:
    td = _temp_dir()
    ac = AccessControl(state_path=td / "access.json")
    dispatcher, send_mock = _make_dispatcher(access=ac)

    msg = UserBlockAdd(ts=_NOW, user_id="user-ccc", reason="spam")
    await dispatcher.handle_message(msg)

    assert "user-ccc" in ac.blocklist

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "user.block.add"
    assert acks[0].status == "ok"


@pytest.mark.asyncio
async def test_block_remove_persists_and_acks() -> None:
    td = _temp_dir()
    ac = AccessControl(state_path=td / "access.json")
    # Pre-populate.
    await ac.handle_block_add(
        UserBlockAdd(ts=_NOW, user_id="user-ddd"), send=AsyncMock()
    )
    dispatcher, send_mock = _make_dispatcher(access=ac)

    msg = UserBlockRemove(ts=_NOW, user_id="user-ddd")
    await dispatcher.handle_message(msg)

    assert "user-ddd" not in ac.blocklist

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "user.block.remove"
    assert acks[0].status == "ok"


# ---------------------------------------------------------------------------
# admin.settings.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_update_merges_keys() -> None:
    td = _temp_dir()
    settings = KharejSettings(state_path=td / "settings.json")
    dispatcher, send_mock = _make_dispatcher(settings=settings)

    msg = AdminSettingsUpdate(ts=_NOW, settings={"max_parallel": "4", "quality": "flac"})
    await dispatcher.handle_message(msg)

    assert settings.get("max_parallel") == "4"
    assert settings.get("quality") == "flac"

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "admin.settings.update"
    assert acks[0].status == "ok"


@pytest.mark.asyncio
async def test_settings_update_ack_contains_effective_config() -> None:
    td = _temp_dir()
    settings = KharejSettings(state_path=td / "settings.json")
    dispatcher, send_mock = _make_dispatcher(settings=settings)

    msg = AdminSettingsUpdate(ts=_NOW, settings={"foo": "bar"})
    await dispatcher.handle_message(msg)

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    ack = acks[0]
    assert ack.effective_config is not None
    assert ack.effective_config.get("foo") == "bar"


@pytest.mark.asyncio
async def test_settings_update_accepts_unknown_keys() -> None:
    """Unknown keys should be stored without crashing (no allow-list in contract)."""
    td = _temp_dir()
    settings = KharejSettings(state_path=td / "settings.json")
    dispatcher, send_mock = _make_dispatcher(settings=settings)

    msg = AdminSettingsUpdate(ts=_NOW, settings={"totally_unknown_key_xyz": "value"})
    await dispatcher.handle_message(msg)

    # No exception → ack sent.
    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].status == "ok"
    assert settings.get("totally_unknown_key_xyz") == "value"


# ---------------------------------------------------------------------------
# admin.clearcache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["lru", "isrc", "all"])
async def test_clearcache_sends_ok_ack(target: str) -> None:
    from typing import Literal, cast

    dispatcher, send_mock = _make_dispatcher()
    msg = AdminClearcache(
        ts=_NOW,
        target=cast(Literal["lru", "isrc", "all"], target),
    )
    await dispatcher.handle_message(msg)

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "admin.clearcache"
    assert acks[0].status == "ok"


# ---------------------------------------------------------------------------
# admin.cookies.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_update_replaces_file() -> None:
    td = _temp_dir()
    cookies_path = td / "cookies.txt"
    cookie_data = b"# Netscape HTTP Cookie File\nexample.com\tFALSE\t/\tFALSE\t0\tsession\tabc123\n"
    sha256 = hashlib.sha256(cookie_data).hexdigest()

    # Set up a fake S2 client that returns our cookie bytes.
    s2 = MagicMock()
    s2.get_object_bytes.return_value = cookie_data

    dispatcher, send_mock = _make_dispatcher(s2=s2, cookies_path=cookies_path)

    msg = AdminCookiesUpdate(ts=_NOW, s2_key="tmp/job-1/cookies.txt", sha256=sha256)
    await dispatcher.handle_message(msg)

    # File should now exist with the correct content.
    assert cookies_path.exists()
    assert cookies_path.read_bytes() == cookie_data

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "admin.cookies.update"
    assert acks[0].status == "ok"


@pytest.mark.asyncio
async def test_cookies_update_sha256_mismatch_sends_error_ack() -> None:
    td = _temp_dir()
    cookies_path = td / "cookies.txt"

    s2 = MagicMock()
    s2.get_object_bytes.return_value = b"real content"

    dispatcher, send_mock = _make_dispatcher(s2=s2, cookies_path=cookies_path)

    msg = AdminCookiesUpdate(
        ts=_NOW,
        s2_key="tmp/job-1/cookies.txt",
        sha256="0" * 64,  # wrong hash
    )
    await dispatcher.handle_message(msg)

    # File should NOT have been replaced.
    assert not cookies_path.exists()

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].acked_type == "admin.cookies.update"
    assert acks[0].status == "error"


@pytest.mark.asyncio
async def test_cookies_update_s2_error_sends_error_ack() -> None:
    td = _temp_dir()
    cookies_path = td / "cookies.txt"

    s2 = MagicMock()
    s2.get_object_bytes.side_effect = Exception("S2 unavailable")

    dispatcher, send_mock = _make_dispatcher(s2=s2, cookies_path=cookies_path)

    msg = AdminCookiesUpdate(
        ts=_NOW,
        s2_key="tmp/job-1/cookies.txt",
        sha256="a" * 64,
    )
    await dispatcher.handle_message(msg)

    acks = [m for m in _sent_msgs(send_mock) if isinstance(m, AdminAck)]
    assert len(acks) == 1
    assert acks[0].status == "error"


# ---------------------------------------------------------------------------
# Exception safety: handler errors must not propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_message_swallows_handler_exception() -> None:
    """handle_message must never raise even when the handler raises."""
    dispatcher, send_mock = _make_dispatcher()

    # Patch rubika.send to raise on HealthPing.
    dispatcher._rubika.send.side_effect = RuntimeError("transport down")

    ping = HealthPing(ts=_NOW, request_id="req-err")
    # Must not raise.
    await dispatcher.handle_message(ping)
