"""Tests for kharej/access_control.py (Step 5a — Access Control).

All mutations use a temporary directory so the production state file is never
touched and tests are fully isolated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kharej.access_control import AccessControl, _default_state, _load_state, _save_state
from kharej.contracts import (
    AccessDecision,
    AdminAck,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_A = "user-aaaa-1111"
_USER_B = "user-bbbb-2222"
_USER_C = "user-cccc-3333"


def _ac(tmp_path: Path) -> AccessControl:
    """Return an AccessControl backed by *tmp_path*."""
    return AccessControl(state_path=tmp_path / "access_state.json")


async def _collect_sends(ac: AccessControl, action: str, **kwargs: Any) -> list[Any]:
    """Call *action* on *ac*, collect all objects passed to the send callback."""
    sent: list[Any] = []

    async def fake_send(msg: Any) -> None:
        sent.append(msg)

    handler = getattr(ac, action)
    await handler(**kwargs, send=fake_send)
    return sent


# ---------------------------------------------------------------------------
# Test 1: empty state — everyone is allowed
# ---------------------------------------------------------------------------


def test_check_access_empty_state_allows_all(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    assert ac.check_access(_USER_A) == AccessDecision.allow
    assert ac.check_access(_USER_B) == AccessDecision.allow


# ---------------------------------------------------------------------------
# Test 2: blocklist takes priority
# ---------------------------------------------------------------------------


def test_check_access_blocked_user(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    state = _default_state()
    state["blocklist"] = [_USER_A]
    _save_state(state, state_path)

    ac = AccessControl(state_path=state_path)
    assert ac.check_access(_USER_A) == AccessDecision.block
    # Non-blocked user in the same scenario — whitelist empty so allowed
    assert ac.check_access(_USER_B) == AccessDecision.allow


# ---------------------------------------------------------------------------
# Test 3: whitelist filtering
# ---------------------------------------------------------------------------


def test_check_access_whitelist_mode(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    state = _default_state()
    state["whitelist"] = [_USER_A]
    _save_state(state, state_path)

    ac = AccessControl(state_path=state_path)
    assert ac.check_access(_USER_A) == AccessDecision.allow
    assert ac.check_access(_USER_B) == AccessDecision.not_whitelisted


# ---------------------------------------------------------------------------
# Test 4: blocklist wins over whitelist
# ---------------------------------------------------------------------------


def test_check_access_block_beats_whitelist(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    state = _default_state()
    state["whitelist"] = [_USER_A]
    state["blocklist"] = [_USER_A]
    _save_state(state, state_path)

    ac = AccessControl(state_path=state_path)
    # Blocked user must be denied even if also whitelisted.
    assert ac.check_access(_USER_A) == AccessDecision.block


# ---------------------------------------------------------------------------
# Test 5: handle_whitelist_add adds user and persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_whitelist_add_persists(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    msg = UserWhitelistAdd(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_A)
    sent = await _collect_sends(ac, "handle_whitelist_add", msg=msg)

    assert _USER_A in ac.whitelist
    # Check disk
    data = json.loads((tmp_path / "access_state.json").read_text())
    assert _USER_A in data["whitelist"]
    # Check ack
    assert len(sent) == 1
    assert isinstance(sent[0], AdminAck)
    assert sent[0].acked_type == "user.whitelist.add"
    assert sent[0].status == "ok"


# ---------------------------------------------------------------------------
# Test 6: handle_whitelist_add is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_whitelist_add_idempotent(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    msg = UserWhitelistAdd(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_A)
    await _collect_sends(ac, "handle_whitelist_add", msg=msg)
    await _collect_sends(ac, "handle_whitelist_add", msg=msg)

    assert ac.whitelist.count(_USER_A) == 1


# ---------------------------------------------------------------------------
# Test 7: handle_whitelist_remove removes user and persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_whitelist_remove_persists(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    state = _default_state()
    state["whitelist"] = [_USER_A, _USER_B]
    _save_state(state, state_path)

    ac = AccessControl(state_path=state_path)
    msg = UserWhitelistRemove(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_A)
    sent = await _collect_sends(ac, "handle_whitelist_remove", msg=msg)

    assert _USER_A not in ac.whitelist
    assert _USER_B in ac.whitelist
    data = json.loads(state_path.read_text())
    assert _USER_A not in data["whitelist"]
    assert len(sent) == 1
    assert isinstance(sent[0], AdminAck)
    assert sent[0].acked_type == "user.whitelist.remove"


# ---------------------------------------------------------------------------
# Test 8: handle_whitelist_remove on absent user does not error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_whitelist_remove_missing_is_noop(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    msg = UserWhitelistRemove(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_C)
    sent = await _collect_sends(ac, "handle_whitelist_remove", msg=msg)
    # Should not raise; should still ack
    assert len(sent) == 1
    assert isinstance(sent[0], AdminAck)


# ---------------------------------------------------------------------------
# Test 9: handle_block_add adds user and persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_block_add_persists(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    msg = UserBlockAdd(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_B, reason="spam")
    sent = await _collect_sends(ac, "handle_block_add", msg=msg)

    assert _USER_B in ac.blocklist
    data = json.loads((tmp_path / "access_state.json").read_text())
    assert _USER_B in data["blocklist"]
    assert len(sent) == 1
    assert isinstance(sent[0], AdminAck)
    assert sent[0].acked_type == "user.block.add"


# ---------------------------------------------------------------------------
# Test 10: handle_block_remove removes user and persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_block_remove_persists(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    state = _default_state()
    state["blocklist"] = [_USER_A, _USER_B]
    _save_state(state, state_path)

    ac = AccessControl(state_path=state_path)
    msg = UserBlockRemove(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), user_id=_USER_A)
    sent = await _collect_sends(ac, "handle_block_remove", msg=msg)

    assert _USER_A not in ac.blocklist
    assert _USER_B in ac.blocklist
    data = json.loads(state_path.read_text())
    assert _USER_A not in data["blocklist"]
    assert len(sent) == 1
    assert isinstance(sent[0], AdminAck)
    assert sent[0].acked_type == "user.block.remove"


# ---------------------------------------------------------------------------
# Test 11: state survives a restart
# ---------------------------------------------------------------------------


def test_state_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "access_state.json"
    ac1 = AccessControl(state_path=state_path)

    # Add whitelist + block entries via internal _state manipulation for speed
    ac1._state["whitelist"] = [_USER_A]
    ac1._state["blocklist"] = [_USER_B]
    ac1._persist()

    # Reload from disk
    ac2 = AccessControl(state_path=state_path)
    assert ac2.check_access(_USER_A) == AccessDecision.allow
    assert ac2.check_access(_USER_B) == AccessDecision.block
    assert ac2.check_access(_USER_C) == AccessDecision.not_whitelisted


# ---------------------------------------------------------------------------
# Test 12: load_state returns defaults on missing file
# ---------------------------------------------------------------------------


def test_load_state_missing_file_returns_defaults(tmp_path: Path) -> None:
    state = _load_state(tmp_path / "nonexistent.json")
    assert state == _default_state()


# ---------------------------------------------------------------------------
# Test 13: load_state returns defaults on corrupt file
# ---------------------------------------------------------------------------


def test_load_state_corrupt_file_returns_defaults(tmp_path: Path) -> None:
    state_path = tmp_path / "bad.json"
    state_path.write_text("NOT JSON", encoding="utf-8")
    state = _load_state(state_path)
    assert state == _default_state()


# ---------------------------------------------------------------------------
# Test 14: load_state returns defaults on version mismatch
# ---------------------------------------------------------------------------


def test_load_state_version_mismatch_returns_defaults(tmp_path: Path) -> None:
    state_path = tmp_path / "future.json"
    state_path.write_text(
        json.dumps({"v": 99, "whitelist": [_USER_A], "blocklist": []}),
        encoding="utf-8",
    )
    state = _load_state(state_path)
    assert state == _default_state()


# ---------------------------------------------------------------------------
# Test 15: whitelist property returns a copy
# ---------------------------------------------------------------------------


def test_whitelist_property_returns_copy(tmp_path: Path) -> None:
    ac = _ac(tmp_path)
    ac._state["whitelist"] = [_USER_A]
    wl = ac.whitelist
    wl.append(_USER_B)
    # Internal state should be unchanged
    assert _USER_B not in ac._state["whitelist"]
