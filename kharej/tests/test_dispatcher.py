"""Tests for kharej/dispatcher.py (Step 6 — Worker Loop & Dispatcher).

Uses pytest + pytest-asyncio with real AccessControl, Settings,
ProgressReporter (backed by a recording AsyncMock send), and
MagicMock/AsyncMock for RubikaClient and S2Client.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kharej.access_control import AccessControl
from kharej.contracts import (
    JobAccepted,
    JobCancel,
    JobCompleted,
    JobCreate,
    JobFailed,
    JobProgress,
    Platform,
    S2ObjectRef,
)
from kharej.dispatcher import Dispatcher, Job
from kharej.progress_reporter import ProgressReporter
from kharej.settings import KharejSettings

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_ALLOWED_USER = "user-allowed-aaaa"
_BLOCKED_USER = "user-blocked-bbbb"
_NOT_WHITELISTED_USER = "user-nope-cccc"
_URL = "https://music.example.com/track/abc123"

# ---------------------------------------------------------------------------
# Temp-dir pool: accumulated during the module lifetime, flushed at exit
# ---------------------------------------------------------------------------

_TEMP_DIRS: list[str] = []


def _cleanup_temp_dirs() -> None:
    for td in _TEMP_DIRS:
        shutil.rmtree(td, ignore_errors=True)


atexit.register(_cleanup_temp_dirs)


def _temp_dir() -> Path:
    td = tempfile.mkdtemp()
    _TEMP_DIRS.append(td)
    return Path(td)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_send_mock() -> AsyncMock:
    """Return a recording AsyncMock suitable as a ProgressReporter send."""
    return AsyncMock()


def _make_dispatcher(
    *,
    send: AsyncMock | None = None,
    access: AccessControl | None = None,
    downloaders: dict | None = None,
    job_timeout_seconds: float = 60.0,
    tmp_path: Any = None,
) -> tuple[Dispatcher, AsyncMock]:
    """Build a Dispatcher wired to a recording AsyncMock send function.

    Returns (dispatcher, send_mock).
    """
    if send is None:
        send = _make_send_mock()

    rubika = AsyncMock()
    rubika.send = send

    s2 = MagicMock()

    if access is None:
        td = _temp_dir()
        access = AccessControl(state_path=td / "access_state.json")
        # Default: empty whitelist = everyone allowed.

    settings = KharejSettings()
    progress = ProgressReporter(send, throttle_sec=0.0)

    dispatcher = Dispatcher(
        s2=s2,
        rubika=rubika,
        access=access,
        settings=settings,
        progress=progress,
        downloaders=downloaders,
        job_timeout_seconds=job_timeout_seconds,
    )
    return dispatcher, send


def _job_create(
    *,
    job_id: str = "job-0001",
    user_id: str = _ALLOWED_USER,
    platform: Any = "stub",
    url: str = _URL,
    quality: str = "mp3",
) -> JobCreate:
    """Build a JobCreate using model_construct to bypass platform enum validation."""
    return JobCreate.model_construct(
        v=1,
        ts=_NOW,
        job_id=job_id,
        user_id=user_id,
        platform=platform,
        url=url,
        quality=quality,
        job_type="single",
        user_status="active",
        format_hint=None,
        collection_name=None,
        track_ids=None,
        total_tracks=None,
        batch_seq=None,
        batch_total=None,
    )


def _job_cancel(*, job_id: str = "job-0001") -> JobCancel:
    return JobCancel(ts=_NOW, job_id=job_id)


def _sent_types(send: AsyncMock) -> list[str]:
    """Return the message types of all calls to *send*."""
    result = []
    for call in send.call_args_list:
        msg = call.args[0] if call.args else call.kwargs.get("msg") or call.kwargs.get("message")
        result.append(getattr(msg, "type", type(msg).__name__))
    return result


def _find_sent(send: AsyncMock, msg_type: type) -> list[Any]:
    """Return all sent messages of a given type."""
    result = []
    for call in send.call_args_list:
        msg = call.args[0] if call.args else next(iter(call.kwargs.values()), None)
        if isinstance(msg, msg_type):
            result.append(msg)
    return result


async def _drain() -> None:
    """Yield control to the event loop so spawned tasks can start."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Fake downloaders
# ---------------------------------------------------------------------------


class _OkDownloader:
    """Returns a single S2ObjectRef immediately."""

    platform = "ok_platform"
    _ref = S2ObjectRef(key="media/job-0001/track.flac", size=1024, mime="audio/flac", sha256="a" * 64)

    async def run(self, job, *, s2, progress, settings):
        return [self._ref]


class _ErrorDownloader:
    """Raises RuntimeError immediately."""

    platform = "error_platform"

    async def run(self, job, *, s2, progress, settings):
        raise RuntimeError("boom")


class _SlowDownloader:
    """Sleeps for a long time (simulates in-flight job)."""

    platform = "slow_platform"

    def __init__(self, sleep_seconds: float = 100.0) -> None:
        self._sleep = sleep_seconds

    async def run(self, job, *, s2, progress, settings):
        await asyncio.sleep(self._sleep)
        return []


class _ProgressDownloader:
    """Calls progress.report_progress once then returns."""

    platform = "progress_platform"

    async def run(self, job, *, s2, progress, settings):
        await progress.report_progress(job.job_id, 50)
        return []


# ---------------------------------------------------------------------------
# 1. Blocked user → JobFailed(blocked), no JobAccepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_user_gets_job_failed_blocked(tmp_path) -> None:
    from kharej.access_control import AccessControl

    access = AccessControl(state_path=tmp_path / "a.json")
    access._state["blocklist"].append(_BLOCKED_USER)
    dispatcher, send = _make_dispatcher(access=access)

    await dispatcher.handle_job_create(_job_create(user_id=_BLOCKED_USER))

    failed = _find_sent(send, JobFailed)
    assert len(failed) == 1
    assert failed[0].error_code == "blocked"
    assert not _find_sent(send, JobAccepted)


# ---------------------------------------------------------------------------
# 2. Not whitelisted → JobFailed(not_whitelisted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_whitelisted_user_gets_not_whitelisted(tmp_path) -> None:
    from kharej.access_control import AccessControl

    access = AccessControl(state_path=tmp_path / "a.json")
    # Enable whitelist with one other user.
    access._state["whitelist"].append("some-other-user")
    dispatcher, send = _make_dispatcher(access=access)

    await dispatcher.handle_job_create(_job_create(user_id=_NOT_WHITELISTED_USER))

    failed = _find_sent(send, JobFailed)
    assert len(failed) == 1
    assert failed[0].error_code == "not_whitelisted"
    assert not _find_sent(send, JobAccepted)


# ---------------------------------------------------------------------------
# 3. Unknown platform → JobAccepted then JobFailed(unsupported_platform)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_platform_gets_unsupported_platform() -> None:
    # Use an empty downloaders dict so no platform is supported.
    dispatcher, send = _make_dispatcher(downloaders={})

    await dispatcher.handle_job_create(_job_create(platform="zzz"))
    await _drain()

    accepted = _find_sent(send, JobAccepted)
    assert len(accepted) == 1

    failed = _find_sent(send, JobFailed)
    assert len(failed) == 1
    assert failed[0].error_code == "unsupported_platform"


# ---------------------------------------------------------------------------
# 4. Stub platform → JobAccepted then JobFailed(not_implemented)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_platform_returns_not_implemented() -> None:
    from kharej.downloaders.stub import StubDownloader

    stub = StubDownloader()
    dispatcher, send = _make_dispatcher(downloaders={"stub": stub})

    await dispatcher.handle_job_create(_job_create(platform="stub"))
    await _drain()
    await asyncio.sleep(0.05)  # let the task finish

    accepted = _find_sent(send, JobAccepted)
    assert len(accepted) == 1

    failed = _find_sent(send, JobFailed)
    assert len(failed) == 1
    assert failed[0].error_code == "not_implemented"


# ---------------------------------------------------------------------------
# 5. Successful run → JobAccepted then JobCompleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_run_publishes_completed() -> None:
    ok = _OkDownloader()
    dispatcher, send = _make_dispatcher(downloaders={ok.platform: ok})

    await dispatcher.handle_job_create(_job_create(platform=ok.platform))
    await _drain()
    await asyncio.sleep(0.05)

    accepted = _find_sent(send, JobAccepted)
    assert len(accepted) == 1

    completed = _find_sent(send, JobCompleted)
    assert len(completed) == 1
    assert len(completed[0].parts) == 1
    assert completed[0].parts[0].key == "media/job-0001/track.flac"


# ---------------------------------------------------------------------------
# 6. Downloader exception → JobFailed(error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downloader_exception_publishes_error() -> None:
    err = _ErrorDownloader()
    dispatcher, send = _make_dispatcher(downloaders={err.platform: err})

    await dispatcher.handle_job_create(_job_create(platform=err.platform))
    await _drain()
    await asyncio.sleep(0.05)

    failed = _find_sent(send, JobFailed)
    assert len(failed) == 1
    assert failed[0].error_code == "error"
    assert "boom" in failed[0].message


# ---------------------------------------------------------------------------
# 7. Duplicate job_id → second gets JobFailed(duplicate_job), first completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_job_id_rejected() -> None:
    slow = _SlowDownloader(sleep_seconds=2.0)
    dispatcher, send = _make_dispatcher(downloaders={slow.platform: slow})

    job_create = _job_create(platform=slow.platform, job_id="dup-job")

    # First create: should be accepted and start running.
    await dispatcher.handle_job_create(job_create)
    await _drain()

    # Second create with same job_id: should be rejected.
    await dispatcher.handle_job_create(job_create)
    await _drain()

    failed = _find_sent(send, JobFailed)
    assert any(f.error_code == "duplicate_job" for f in failed)

    # Clean up: cancel the first task.
    task = dispatcher._tasks.get("dup-job")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 8. Cancel running job → JobFailed(cancelled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_running_job() -> None:
    slow = _SlowDownloader(sleep_seconds=5.0)
    dispatcher, send = _make_dispatcher(downloaders={slow.platform: slow})

    await dispatcher.handle_job_create(_job_create(platform=slow.platform, job_id="cancel-job"))
    await _drain()

    assert dispatcher.in_flight == 1

    await dispatcher.handle_job_cancel(_job_cancel(job_id="cancel-job"))
    await asyncio.sleep(0.1)  # let task handle CancelledError

    failed = _find_sent(send, JobFailed)
    assert any(f.error_code == "cancelled" for f in failed)
    assert dispatcher.in_flight == 0


# ---------------------------------------------------------------------------
# 9. Cancel unknown job_id → noop (no exception, no message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_unknown_job_id_is_noop() -> None:
    dispatcher, send = _make_dispatcher(downloaders={})

    # Should not raise.
    await dispatcher.handle_job_cancel(_job_cancel(job_id="does-not-exist"))

    assert send.call_count == 0


# ---------------------------------------------------------------------------
# 10. Job timeout → JobFailed(timeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_timeout() -> None:
    slow = _SlowDownloader(sleep_seconds=1.0)
    dispatcher, send = _make_dispatcher(
        downloaders={slow.platform: slow},
        job_timeout_seconds=0.05,
    )

    await dispatcher.handle_job_create(_job_create(platform=slow.platform, job_id="timeout-job"))
    await asyncio.sleep(0.5)  # longer than timeout

    failed = _find_sent(send, JobFailed)
    assert any(f.error_code == "timeout" for f in failed)
    assert dispatcher.in_flight == 0


# ---------------------------------------------------------------------------
# 11. handle_message routes by type; HealthPing sends a HealthPong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_message_routes_by_type() -> None:
    from kharej.contracts import HealthPing, HealthPong

    ok = _OkDownloader()
    dispatcher, send = _make_dispatcher(downloaders={ok.platform: ok})

    job_create = _job_create(platform=ok.platform, job_id="msg-route-job")
    job_cancel = _job_cancel(job_id="msg-route-job")
    health_ping = HealthPing(ts=_NOW, job_id=None, request_id="req-1")

    # JobCreate triggers accepted + task spawn.
    await dispatcher.handle_message(job_create)
    await _drain()

    assert _find_sent(send, JobAccepted)

    # JobCancel cancels the task.
    await dispatcher.handle_message(job_cancel)
    await asyncio.sleep(0.1)

    # HealthPing must trigger exactly one HealthPong send (Step 10).
    pre_count = send.call_count
    await dispatcher.handle_message(health_ping)
    assert send.call_count == pre_count + 1
    pongs = _find_sent(send, HealthPong)
    assert len(pongs) == 1
    assert pongs[0].request_id == "req-1"


# ---------------------------------------------------------------------------
# 12. Unknown message type does not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_message_type_does_not_raise() -> None:
    from kharej.contracts import JobAccepted as JA

    dispatcher, send = _make_dispatcher(downloaders={})

    # JobAccepted is a valid AnyMessage but not a type the dispatcher handles.
    msg = JA(ts=_NOW, job_id="x", worker_version="0.1.0", queue_position=1)
    # Must not raise.
    await dispatcher.handle_message(msg)


# ---------------------------------------------------------------------------
# 13. in_flight counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_counter() -> None:
    slow = _SlowDownloader(sleep_seconds=100.0)
    dispatcher, send = _make_dispatcher(downloaders={slow.platform: slow})

    for i in range(3):
        await dispatcher.handle_job_create(
            _job_create(platform=slow.platform, job_id=f"inflight-{i}")
        )
    await _drain()

    assert dispatcher.in_flight == 3

    # Cancel all tasks.
    for i in range(3):
        await dispatcher.handle_job_cancel(_job_cancel(job_id=f"inflight-{i}"))

    await asyncio.sleep(0.2)

    assert dispatcher.in_flight == 0


# ---------------------------------------------------------------------------
# 14. shutdown drains or cancels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drains_or_cancels() -> None:
    slow = _SlowDownloader(sleep_seconds=5.0)
    dispatcher, send = _make_dispatcher(downloaders={slow.platform: slow})

    for i in range(2):
        await dispatcher.handle_job_create(
            _job_create(platform=slow.platform, job_id=f"shutdown-{i}")
        )
    await _drain()
    assert dispatcher.in_flight == 2

    # Very short drain_timeout → tasks will be force-cancelled.
    await dispatcher.shutdown(drain_timeout=0.1)

    assert dispatcher.in_flight == 0
    failed = _find_sent(send, JobFailed)
    # Each job should produce a JobFailed with error_code "cancelled" or "shutdown".
    terminal_codes = {f.error_code for f in failed}
    assert terminal_codes <= {"cancelled", "shutdown"}
    assert len(failed) >= 2


# ---------------------------------------------------------------------------
# 15. ProgressReporter is invoked through the dispatcher (smoke)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_reporter_invoked_through_dispatcher_smoke() -> None:
    send = _make_send_mock()
    rubika = AsyncMock()
    rubika.send = send
    s2 = MagicMock()
    settings = KharejSettings()

    access = AccessControl(state_path=_temp_dir() / "access_state.json")
    progress = ProgressReporter(send, throttle_sec=0.0)

    prog = _ProgressDownloader()
    dispatcher = Dispatcher(
        s2=s2,
        rubika=rubika,
        access=access,
        settings=settings,
        progress=progress,
        downloaders={prog.platform: prog},
    )

    await dispatcher.handle_job_create(_job_create(platform=prog.platform))
    await _drain()
    await asyncio.sleep(0.05)

    progress_msgs = _find_sent(send, JobProgress)
    assert len(progress_msgs) >= 1


# ---------------------------------------------------------------------------
# 16. URL is redacted in logs — only host appears, not full URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_url_is_redacted_in_logs(caplog) -> None:
    sensitive_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    expected_host = "www.youtube.com"
    dispatcher, send = _make_dispatcher(downloaders={})

    with caplog.at_level(logging.INFO, logger="kharej.dispatcher"):
        await dispatcher.handle_job_create(
            _job_create(platform="zzz", url=sensitive_url)
        )

    # Full URL must not appear in any log record.
    for record in caplog.records:
        if record.name != "kharej.dispatcher":
            continue
        record_text = str(record.msg)
        assert sensitive_url not in record_text

    # The host field must appear in at least one structured log record.
    host_logged = any(
        isinstance(r.msg, dict) and r.msg.get("host") == expected_host
        for r in caplog.records
        if r.name == "kharej.dispatcher"
    )
    assert host_logged, "Expected 'host' field with value in at least one log record"
