"""Tests for kharej/worker.py (Step 6 — Worker entrypoint).

Tests cover CLI modes: --help, --version, --check-config, --healthcheck,
and the main run loop (SIGTERM shutdown).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kharej


# ---------------------------------------------------------------------------
# 1. --help exits 0
# ---------------------------------------------------------------------------


def test_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# 2. --version prints kharej.__version__
# ---------------------------------------------------------------------------


def test_version_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert kharej.__version__ in result.stdout


# ---------------------------------------------------------------------------
# 3. --check-config exits non-zero when env is missing
# ---------------------------------------------------------------------------


def test_check_config_missing_env_nonzero() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RUBIKA_", "IRAN_", "ARVAN_"))}
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--check-config"],
        capture_output=True,
        env=env,
    )
    assert result.returncode != 0
    # Must not dump a Python traceback.
    assert b"Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# 4. --check-config exits 0 with redacted summary when config is present
# ---------------------------------------------------------------------------


def test_check_config_ok() -> None:
    env = dict(os.environ)
    env["RUBIKA_SESSION_KHAREJ"] = "test-session"
    env["IRAN_RUBIKA_ACCOUNT_GUID"] = "dummy-guid-1234"
    env["ARVAN_S2_ENDPOINT"] = "https://s3.ir-thr-at1.arvanstorage.ir"
    env["ARVAN_S2_ACCESS_KEY_WRITE"] = "dummy-access-key"
    env["ARVAN_S2_SECRET_WRITE"] = "dummy-secret"
    env["ARVAN_S2_BUCKET"] = "test-bucket"

    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--check-config"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    # Output must be valid JSON-ish and must not contain raw secrets.
    out = result.stdout
    assert "dummy-access-key" not in out
    assert "dummy-secret" not in out
    assert "session_name_set" in out or "session" in out


# ---------------------------------------------------------------------------
# 5. --healthcheck with mocked clients → exit 0
# ---------------------------------------------------------------------------


def test_healthcheck_uses_injected_clients(monkeypatch) -> None:
    from kharej.worker import main

    # Fake transport that immediately reports "connected".
    class _FakeTransport:
        connected = True

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send_text(self, peer, text):
            pass

        def subscribe(self, cb):
            pass

    # Patch RubikaClient to use the fake transport.
    from kharej import rubika_client as rc

    monkeypatch.setattr(rc, "_DefaultRubikaTransport", lambda session: _FakeTransport())

    # Patch S2Client.head_object to return None (object not found = healthy).
    from kharej import s2_client as sc

    monkeypatch.setattr(sc.S2Client, "head_object", lambda self, key: None)

    env_patch = {
        "RUBIKA_SESSION_KHAREJ": "test-session",
        "IRAN_RUBIKA_ACCOUNT_GUID": "dummy-guid",
        "ARVAN_S2_ENDPOINT": "https://s3.example.com",
        "ARVAN_S2_ACCESS_KEY_WRITE": "ak",
        "ARVAN_S2_SECRET_WRITE": "sk",
        "ARVAN_S2_BUCKET": "bucket",
    }
    with patch.dict(os.environ, env_patch):
        result = main(["--healthcheck", "--healthcheck-timeout", "5"])

    assert result == 0


# ---------------------------------------------------------------------------
# 6. --healthcheck with S2AccessDenied → non-zero
# ---------------------------------------------------------------------------


def test_healthcheck_failure_returns_nonzero(monkeypatch) -> None:
    from kharej import s2_client as sc
    from kharej.s2_client import S2AccessDenied
    from kharej.worker import main

    # Fake transport that connects immediately.
    class _FakeTransport:
        connected = True

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send_text(self, peer, text):
            pass

        def subscribe(self, cb):
            pass

    def _raise_denied(self, key):
        raise S2AccessDenied("denied")

    monkeypatch.setattr(sc.S2Client, "head_object", _raise_denied)

    from kharej import rubika_client as rc

    monkeypatch.setattr(rc, "_DefaultRubikaTransport", lambda session: _FakeTransport())

    env_patch = {
        "RUBIKA_SESSION_KHAREJ": "test-session",
        "IRAN_RUBIKA_ACCOUNT_GUID": "dummy-guid",
        "ARVAN_S2_ENDPOINT": "https://s3.example.com",
        "ARVAN_S2_ACCESS_KEY_WRITE": "ak",
        "ARVAN_S2_SECRET_WRITE": "sk",
        "ARVAN_S2_BUCKET": "bucket",
    }
    with patch.dict(os.environ, env_patch):
        result = main(["--healthcheck", "--healthcheck-timeout", "5"])

    assert result != 0


# ---------------------------------------------------------------------------
# 7. run() starts and stops cleanly on SIGTERM (simulated via stop_event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_starts_and_stops_on_signal(monkeypatch) -> None:
    """Simulate the run loop by patching rubika.start/stop and firing the stop event."""
    from kharej import rubika_client as rc
    from kharej import s2_client as sc
    from kharej import worker as wmod

    class _FakeTransport:
        connected = True

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send_text(self, peer, text):
            pass

        def subscribe(self, cb):
            pass

    monkeypatch.setattr(rc, "_DefaultRubikaTransport", lambda session: _FakeTransport())
    monkeypatch.setattr(sc.S2Client, "head_object", lambda self, key: None)

    env_patch = {
        "RUBIKA_SESSION_KHAREJ": "test-session",
        "IRAN_RUBIKA_ACCOUNT_GUID": "dummy-guid",
        "ARVAN_S2_ENDPOINT": "https://s3.example.com",
        "ARVAN_S2_ACCESS_KEY_WRITE": "ak",
        "ARVAN_S2_SECRET_WRITE": "sk",
        "ARVAN_S2_BUCKET": "bucket",
    }

    # Patch asyncio.Event to auto-set after a short delay so run() exits.
    original_event = asyncio.Event

    class _AutoEvent:
        def __init__(self):
            self._event = original_event()

        def set(self):
            self._event.set()

        async def wait(self):
            # Auto-set after a tiny delay so we don't block forever.
            asyncio.get_running_loop().call_later(0.1, self._event.set)
            await self._event.wait()

    monkeypatch.setattr(asyncio, "Event", _AutoEvent)

    with patch.dict(os.environ, env_patch):
        result = await asyncio.wait_for(wmod.run(), timeout=5.0)

    assert result == 0
