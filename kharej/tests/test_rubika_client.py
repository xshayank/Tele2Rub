"""Tests for kharej/rubika_client.py (Step 4 — Rubika Control Client).

Uses a ``FakeTransport`` in-process; never touches the real rubpy library.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import pytest

from kharej.contracts import (
    JobAccepted,
    JobCompleted,
    JobCreate,
    S2ObjectRef,
    encode,
)
from kharej.rubika_client import (
    InboundMessage,
    RubikaClient,
    RubikaConfig,
    RubikaNotConnectedError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IRAN_GUID = "iran-account-guid-abc123"
_OTHER_GUID = "some-other-guid-xyz"
_SESSION = "test-session"


# ---------------------------------------------------------------------------
# FakeTransport
# ---------------------------------------------------------------------------


class FakeTransport:
    """In-process RubikaTransport implementation for unit testing."""

    def __init__(self, *, start_connected: bool = False) -> None:
        self._connected: bool = start_connected
        self._outbound: list[tuple[str, str]] = []
        self._callbacks: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self.connect_call_count: int = 0
        self.connect_fail_remaining: int = 0  # fail this many times before succeeding
        self.connect_sleeps: list[float] = []  # record asyncio.sleep durations

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_call_count += 1
        if self.connect_fail_remaining > 0:
            self.connect_fail_remaining -= 1
            raise OSError("Simulated connect failure")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_text(self, peer_guid: str, text: str) -> None:
        if not self._connected:
            raise OSError("Not connected")
        self._outbound.append((peer_guid, text))

    def subscribe(self, callback: Callable[[InboundMessage], Awaitable[None]]) -> None:
        self._callbacks.append(callback)

    async def inject(self, msg: InboundMessage) -> None:
        """Push an inbound message into all registered callbacks."""
        for cb in self._callbacks:
            await cb(msg)

    def flip_disconnected(self) -> None:
        """Simulate a network drop."""
        self._connected = False


def _make_config(**overrides: Any) -> RubikaConfig:
    defaults: dict[str, Any] = {
        "session_name": _SESSION,
        "iran_account_guid": _IRAN_GUID,
    }
    defaults.update(overrides)
    return RubikaConfig(**defaults)


def _make_client(
    transport: FakeTransport | None = None,
    **config_overrides: Any,
) -> tuple[RubikaClient, FakeTransport]:
    if transport is None:
        transport = FakeTransport()
    config = _make_config(**config_overrides)
    client = RubikaClient(config, transport_factory=lambda _cfg: transport)
    return client, transport


def _ts() -> datetime:
    return datetime.now(tz=timezone.utc)


def _job_create() -> JobCreate:
    return JobCreate(
        ts=_ts(),
        job_id="job-1",
        user_id="user-1",
        user_status="active",
        platform="youtube",
        url="https://youtube.com/watch?v=abc",
        quality="mp3",
        job_type="single",
    )


def _job_accepted() -> JobAccepted:
    return JobAccepted(
        ts=_ts(),
        job_id="job-1",
        worker_version="0.1.0",
        queue_position=1,
    )


# ---------------------------------------------------------------------------
# Test 1: config from_env missing raises
# ---------------------------------------------------------------------------


def test_config_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUBIKA_SESSION_KHAREJ", raising=False)
    monkeypatch.delenv("IRAN_RUBIKA_ACCOUNT_GUID", raising=False)

    with pytest.raises(ValueError) as exc_info:
        RubikaConfig.from_env()

    text = str(exc_info.value)
    assert "RUBIKA_SESSION_KHAREJ" in text
    assert "IRAN_RUBIKA_ACCOUNT_GUID" in text


# ---------------------------------------------------------------------------
# Test 2: start connects and subscribes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_connects_and_subscribes() -> None:
    client, transport = _make_client()
    await client.start()
    try:
        assert client.connected is True
        assert len(transport._callbacks) == 1
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 3: send encodes and publishes to iran guid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_encodes_and_publishes_to_iran_guid() -> None:
    client, transport = _make_client()
    await client.start()
    try:
        msg = _job_accepted()
        await client.send(msg)
        assert len(transport._outbound) == 1
        peer, text = transport._outbound[0]
        assert peer == _IRAN_GUID
        assert text.startswith("RTUNES::")
        from kharej.contracts import decode
        decoded = decode(text)
        assert isinstance(decoded, JobAccepted)
        assert decoded.job_id == msg.job_id
        assert decoded.worker_version == msg.worker_version
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 4: send raises when not connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_raises_when_not_connected() -> None:
    client, transport = _make_client()
    # Do NOT call start() — transport is not connected.
    with pytest.raises(RubikaNotConnectedError):
        await client.send(_job_accepted())
    assert len(transport._outbound) == 0


# ---------------------------------------------------------------------------
# Test 5: inbound valid dispatched to handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_valid_dispatched_to_handler() -> None:
    client, transport = _make_client()
    received: list[Any] = []

    async def handler(msg: Any) -> None:
        received.append(msg)

    client.on_message(handler)
    await client.start()
    try:
        job = _job_create()
        wire = encode(job)
        await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text=wire))
        await asyncio.sleep(0.05)  # let the task complete
        assert len(received) == 1
        assert isinstance(received[0], JobCreate)
        assert received[0].job_id == job.job_id
        assert received[0].url == job.url
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 6: wrong sender dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_wrong_sender_dropped(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client()
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        import logging
        with caplog.at_level(logging.INFO, logger="kharej.rubika"):
            await transport.inject(
                InboundMessage(sender_guid=_OTHER_GUID, text=encode(_job_create()))
            )
            await asyncio.sleep(0.05)

        assert called is False
        assert any(
            getattr(r, "event", None) == "rubika.reject_sender"
            for r in caplog.records
        )
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 7: unprefixed text dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_unprefixed_dropped() -> None:
    client, transport = _make_client()
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text="hello world"))
        await asyncio.sleep(0.05)
        assert called is False
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 8: oversize message dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_oversize_dropped(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client(inbound_max_bytes=64)
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        import logging
        big_text = "RTUNES::" + "x" * 200
        with caplog.at_level(logging.WARNING, logger="kharej.rubika"):
            await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text=big_text))
            await asyncio.sleep(0.05)

        assert called is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.reject_oversize" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 9: invalid json dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invalid_json_dropped(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client()
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        import logging
        with caplog.at_level(logging.WARNING, logger="kharej.rubika"):
            await transport.inject(
                InboundMessage(sender_guid=_IRAN_GUID, text="RTUNES::not-json")
            )
            await asyncio.sleep(0.05)

        assert called is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.reject_invalid" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 10: wrong version dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_wrong_version_dropped(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client()
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        import json
        import logging

        bad_msg = json.dumps({"v": 2, "type": "job.create", "ts": _ts().isoformat()})
        with caplog.at_level(logging.WARNING, logger="kharej.rubika"):
            await transport.inject(
                InboundMessage(sender_guid=_IRAN_GUID, text=f"RTUNES::{bad_msg}")
            )
            await asyncio.sleep(0.05)

        assert called is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.reject_invalid" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 11: unknown type dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_unknown_type_dropped(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client()
    called = False

    async def handler(msg: Any) -> None:
        nonlocal called
        called = True

    client.on_message(handler)
    await client.start()
    try:
        import json
        import logging

        bad_msg = json.dumps(
            {"v": 1, "type": "job.totally_made_up", "ts": _ts().isoformat()}
        )
        with caplog.at_level(logging.WARNING, logger="kharej.rubika"):
            await transport.inject(
                InboundMessage(sender_guid=_IRAN_GUID, text=f"RTUNES::{bad_msg}")
            )
            await asyncio.sleep(0.05)

        assert called is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.reject_invalid" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 12: handler exception does not kill the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_loop(caplog: pytest.LogCaptureFixture) -> None:
    client, transport = _make_client()
    call_count = 0

    async def bad_handler(msg: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("handler boom")

    client.on_message(bad_handler)
    await client.start()
    try:
        import logging

        wire = encode(_job_create())
        with caplog.at_level(logging.ERROR, logger="kharej.rubika"):
            # First message — handler raises
            await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text=wire))
            await asyncio.sleep(0.05)

            # Second message — should still be dispatched
            await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text=wire))
            await asyncio.sleep(0.05)

        assert call_count == 2
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.handler_failed" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 13: handler replacement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_replacement() -> None:
    client, transport = _make_client()
    h1_called = False
    h2_received: list[Any] = []

    async def h1(msg: Any) -> None:
        nonlocal h1_called
        h1_called = True

    async def h2(msg: Any) -> None:
        h2_received.append(msg)

    client.on_message(h1)
    client.on_message(h2)  # replace h1 with h2
    await client.start()
    try:
        wire = encode(_job_create())
        await transport.inject(InboundMessage(sender_guid=_IRAN_GUID, text=wire))
        await asyncio.sleep(0.05)
        assert h1_called is False
        assert len(h2_received) == 1
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 14: reconnect after disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_after_disconnect(caplog: pytest.LogCaptureFixture) -> None:
    # Use a small initial backoff so the reconnect happens quickly in tests.
    client, transport = _make_client(reconnect_initial_seconds=0.1)
    await client.start()
    try:
        import logging

        transport.flip_disconnected()

        with caplog.at_level(logging.INFO, logger="kharej.rubika"):
            # Give supervisor time to detect disconnect and reconnect
            # (polling 0.1s + backoff ~0.1s + connect = ~0.3s; wait 1s for safety)
            await asyncio.sleep(1.0)

        assert transport.connected is True
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "rubika.reconnected" in events
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 15: reconnect backoff grows then caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_backoff_grows_then_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail connect() 4× then succeed; assert sleep durations follow backoff."""
    transport = FakeTransport(start_connected=True)
    config = _make_config(
        reconnect_initial_seconds=1.0,
        reconnect_max_seconds=30.0,
    )
    client = RubikaClient(config, transport_factory=lambda _: transport)

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        # Capture only backoff sleeps (>= 0.5); polling sleep is 0.1 so is excluded.
        if delay >= 0.5:
            sleep_calls.append(delay)
        await real_sleep(0)  # don't actually wait in tests

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.start()
    try:
        transport.flip_disconnected()
        transport.connect_fail_remaining = 4

        # Wait for the supervisor to cycle through 4 failures + 1 success
        for _ in range(60):
            await real_sleep(0.02)
            if transport.connected:
                break

        # We should have at least 4 backoff sleeps recorded
        assert len(sleep_calls) >= 4

        expected = [1.0, 2.0, 4.0, 8.0]
        for i, exp in enumerate(expected):
            if i < len(sleep_calls):
                assert sleep_calls[i] >= exp * 0.8, (
                    f"sleep_calls[{i}]={sleep_calls[i]:.2f} < {exp * 0.8:.2f} (expected ~{exp})"
                )
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 16: stop is idempotent and cancels supervisor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_cancels_supervisor() -> None:
    client, transport = _make_client()
    await client.start()
    task = client._supervisor_task
    assert task is not None

    await client.stop()
    assert task.done()

    # Second stop should be a no-op without errors
    await client.stop()
    assert task.done()


# ---------------------------------------------------------------------------
# Test 17: send raises on oversize payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_raises_on_oversize_payload() -> None:
    client, transport = _make_client()
    await client.start()
    try:
        # Build a JobCompleted whose encoded size exceeds MAX_MESSAGE_BYTES
        big_parts = [
            S2ObjectRef(
                key=f"media/job-1/{'a' * 60}{i}.flac",
                size=1024 * 1024,
                mime="audio/flac",
                sha256="a" * 64,
            )
            for i in range(30)
        ]
        msg = JobCompleted(ts=_ts(), job_id="job-1", parts=big_parts)
        with pytest.raises(ValueError, match="bytes"):
            await client.send(msg)
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Test 18: no secret in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    mock_secret_session = "SECRET-TOKEN"
    config = RubikaConfig(
        session_name=mock_secret_session,
        iran_account_guid=_IRAN_GUID,
    )
    transport = FakeTransport()
    client = RubikaClient(config, transport_factory=lambda _: transport)

    import logging

    with caplog.at_level(logging.DEBUG, logger="kharej.rubika"):
        await client.start()
        await client.stop()

    assert mock_secret_session not in caplog.text
