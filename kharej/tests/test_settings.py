"""Tests for kharej/settings.py (Step 5b — Runtime Settings).

All tests use a temporary directory to avoid touching the production state
file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kharej.contracts import AdminAck, AdminSettingsUpdate
from kharej.settings import KharejSettings, _load_disk, _load_env_defaults, _save_disk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, *, env: dict[str, str] | None = None) -> KharejSettings:
    """Return a KharejSettings backed by *tmp_path*.

    If *env* is provided it is merged into ``os.environ`` for the duration of
    construction (the real ``monkeypatch`` fixture is not available here, so
    we set/restore manually).
    """
    return KharejSettings(state_path=tmp_path / "kharej_settings.json")


# ---------------------------------------------------------------------------
# Test 1: get with default returns default when key absent
# ---------------------------------------------------------------------------


def test_get_default_when_absent(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert s.get("nonexistent_key") is None
    assert s.get("nonexistent_key", 42) == 42


# ---------------------------------------------------------------------------
# Test 2: set persists to disk
# ---------------------------------------------------------------------------


def test_set_persists_to_disk(tmp_path: Path) -> None:
    state_path = tmp_path / "kharej_settings.json"
    s = KharejSettings(state_path=state_path)
    s.set("max_parallel", 4)

    data = json.loads(state_path.read_text())
    assert data["max_parallel"] == 4


# ---------------------------------------------------------------------------
# Test 3: get after set returns value
# ---------------------------------------------------------------------------


def test_get_after_set(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.set("queue_depth", 10)
    assert s.get("queue_depth") == 10


# ---------------------------------------------------------------------------
# Test 4: state survives a restart
# ---------------------------------------------------------------------------


def test_state_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "kharej_settings.json"
    s1 = KharejSettings(state_path=state_path)
    s1.set("feature_tidal", True)
    s1.set("metrics_port", 9091)

    s2 = KharejSettings(state_path=state_path)
    assert s2.get("feature_tidal") is True
    assert s2.get("metrics_port") == 9091


# ---------------------------------------------------------------------------
# Test 5: disk values override env defaults
# ---------------------------------------------------------------------------


def test_disk_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHAREJ_MAX_PARALLEL", "2")
    state_path = tmp_path / "kharej_settings.json"
    # Pre-populate disk with a different value
    _save_disk({"max_parallel": "8"}, state_path)

    s = KharejSettings(state_path=state_path)
    # Disk should win over env
    assert s.get("max_parallel") == "8"


# ---------------------------------------------------------------------------
# Test 6: env defaults are readable
# ---------------------------------------------------------------------------


def test_env_defaults_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHAREJ_METRICS_PORT", "9090")
    s = KharejSettings(state_path=tmp_path / "kharej_settings.json")
    assert s.get("metrics_port") == "9090"


# ---------------------------------------------------------------------------
# Test 7: effective_config returns merged snapshot
# ---------------------------------------------------------------------------


def test_effective_config_is_snapshot(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.set("k1", "v1")
    s.set("k2", "v2")
    cfg = s.effective_config()
    assert cfg["k1"] == "v1"
    assert cfg["k2"] == "v2"
    # Mutating the snapshot must not affect internal state
    cfg["k1"] = "mutated"
    assert s.get("k1") == "v1"


# ---------------------------------------------------------------------------
# Test 8: handle_settings_update applies all keys and sends ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_settings_update_applies_and_acks(tmp_path: Path) -> None:
    import datetime

    s = _settings(tmp_path)
    sent: list[Any] = []

    async def fake_send(msg: Any) -> None:
        sent.append(msg)

    msg = AdminSettingsUpdate(
        ts=datetime.datetime.now(datetime.timezone.utc),
        settings={"max_parallel": 5, "feature_tidal": False},
    )
    await s.handle_settings_update(msg, fake_send)

    assert s.get("max_parallel") == 5
    assert s.get("feature_tidal") is False
    assert len(sent) == 1
    ack = sent[0]
    assert isinstance(ack, AdminAck)
    assert ack.acked_type == "admin.settings.update"
    assert ack.status == "ok"
    assert ack.effective_config is not None
    assert ack.effective_config["max_parallel"] == 5


# ---------------------------------------------------------------------------
# Test 9: handle_settings_update persists to disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_settings_update_persists(tmp_path: Path) -> None:
    import datetime

    state_path = tmp_path / "kharej_settings.json"
    s = KharejSettings(state_path=state_path)
    sent: list[Any] = []

    async def fake_send(msg: Any) -> None:
        sent.append(msg)

    msg = AdminSettingsUpdate(
        ts=datetime.datetime.now(datetime.timezone.utc),
        settings={"persist_me": "yes"},
    )
    await s.handle_settings_update(msg, fake_send)

    data = json.loads(state_path.read_text())
    assert data["persist_me"] == "yes"


# ---------------------------------------------------------------------------
# Test 10: load_disk returns empty dict on missing file
# ---------------------------------------------------------------------------


def test_load_disk_missing_returns_empty(tmp_path: Path) -> None:
    result = _load_disk(tmp_path / "no_file.json")
    assert result == {}


# ---------------------------------------------------------------------------
# Test 11: load_disk returns empty dict on invalid JSON
# ---------------------------------------------------------------------------


def test_load_disk_invalid_json_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    result = _load_disk(bad)
    assert result == {}


# ---------------------------------------------------------------------------
# Test 12: load_env_defaults uses KHAREJ_ prefix and lowercases
# ---------------------------------------------------------------------------


def test_load_env_defaults_prefix_and_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHAREJ_FOO_BAR", "hello")
    monkeypatch.setenv("OTHER_VAR", "ignored")
    result = _load_env_defaults()
    assert "foo_bar" in result
    assert result["foo_bar"] == "hello"
    assert "other_var" not in result


# ---------------------------------------------------------------------------
# Test 13: set overwrites existing value
# ---------------------------------------------------------------------------


def test_set_overwrites_existing(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.set("count", 1)
    s.set("count", 99)
    assert s.get("count") == 99
    data = json.loads((tmp_path / "kharej_settings.json").read_text())
    assert data["count"] == 99
