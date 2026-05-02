"""Unit tests for Track B Step 5 — Iran-Side Rubika Transport Client.

Tests:
- IranRubikaClient importability and interface
- FakeRubikaTransport test double
- Message dispatch to registered handlers (happy-path)
- Malformed / invalid messages are rejected without crashing
- Forced disconnect → reconnect supervisor logic
- De-duplication: same (job_id, type, ts) processed only once
- EventBus subscribe/publish/unsubscribe
- Contract fixture tests — exact JSON wire format for messages Iran sends
- Outbound send encodes and transmits via transport
- RubikaSendError raised on send failure
- IranRubikaConfig loads from env prefix IRAN_
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants used across tests
# ---------------------------------------------------------------------------

_KHAREJ_GUID = "kharej-account-guid"
_IRAN_GUID = "iran-account-guid"
_TS = datetime(2026, 4, 26, 17, 5, 56, tzinfo=timezone.utc)
_JOB_ID = "550e8400-e29b-41d4-a716-446655440000"
_USER_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


def _make_config(
    *,
    kharej_guid: str = _KHAREJ_GUID,
    iran_guid: str = _IRAN_GUID,
    session: str = "test-session",
):
    from iran.rubika_client import IranRubikaConfig

    return IranRubikaConfig(
        RUBIKA_SESSION_IRAN=session,
        KHAREJ_RUBIKA_ACCOUNT_GUID=kharej_guid,
        IRAN_RUBIKA_ACCOUNT_GUID=iran_guid,
    )


# ---------------------------------------------------------------------------
# 1. Importability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_rubika_client_importable(self):
        from iran.rubika_client import IranRubikaClient  # noqa: F401

    def test_rubika_config_importable(self):
        from iran.rubika_client import IranRubikaConfig  # noqa: F401

    def test_fake_transport_importable(self):
        from iran.rubika_client import FakeRubikaTransport  # noqa: F401

    def test_rubika_send_error_importable(self):
        from iran.rubika_client import RubikaSendError  # noqa: F401

    def test_make_rubika_client_importable(self):
        from iran.rubika_client import make_rubika_client  # noqa: F401

    def test_event_bus_importable(self):
        from iran.event_bus import EventBus  # noqa: F401

    def test_make_event_bus_importable(self):
        from iran.event_bus import make_event_bus  # noqa: F401


# ---------------------------------------------------------------------------
# 2. IranRubikaConfig
# ---------------------------------------------------------------------------


class TestIranRubikaConfig:
    def test_defaults_are_empty_strings(self):
        from iran.rubika_client import IranRubikaConfig

        cfg = IranRubikaConfig()
        assert cfg.RUBIKA_SESSION_IRAN == ""
        assert cfg.KHAREJ_RUBIKA_ACCOUNT_GUID == ""
        assert cfg.IRAN_RUBIKA_ACCOUNT_GUID == ""

    def test_loaded_from_kwargs(self):
        cfg = _make_config()
        assert cfg.RUBIKA_SESSION_IRAN == "test-session"
        assert cfg.KHAREJ_RUBIKA_ACCOUNT_GUID == _KHAREJ_GUID
        assert cfg.IRAN_RUBIKA_ACCOUNT_GUID == _IRAN_GUID

    def test_env_prefix_is_iran(self, monkeypatch):
        monkeypatch.setenv("IRAN_RUBIKA_SESSION_IRAN", "env-session")
        monkeypatch.setenv("IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID", "env-kharej")
        from iran.rubika_client import IranRubikaConfig

        cfg = IranRubikaConfig()
        assert cfg.RUBIKA_SESSION_IRAN == "env-session"
        assert cfg.KHAREJ_RUBIKA_ACCOUNT_GUID == "env-kharej"


# ---------------------------------------------------------------------------
# 3. FakeRubikaTransport basics
# ---------------------------------------------------------------------------


class TestFakeRubikaTransport:
    @pytest.mark.asyncio
    async def test_connect_sets_connected(self):
        from iran.rubika_client import FakeRubikaTransport

        t = FakeRubikaTransport()
        await t.connect()
        assert t.connected is True

    @pytest.mark.asyncio
    async def test_disconnect_clears_connected(self):
        from iran.rubika_client import FakeRubikaTransport

        t = FakeRubikaTransport()
        await t.connect()
        await t.disconnect()
        assert t.connected is False

    @pytest.mark.asyncio
    async def test_send_text_recorded(self):
        from iran.rubika_client import FakeRubikaTransport

        t = FakeRubikaTransport()
        await t.send_text("some-guid", "hello")
        assert t.sent == [("some-guid", "hello")]

    @pytest.mark.asyncio
    async def test_inject_raw_dispatched(self):
        from iran.rubika_client import FakeRubikaTransport

        t = FakeRubikaTransport()
        received: list[tuple[str, str]] = []

        async def _run():
            await t.connect()
            await t.inject_raw(_KHAREJ_GUID, "RTUNES::test")
            t.simulate_disconnect()
            try:
                await t.receive_loop(lambda s, r: received.append((s, r)))
            except ConnectionError:
                pass

        await _run()
        assert received == [(_KHAREJ_GUID, "RTUNES::test")]

    @pytest.mark.asyncio
    async def test_simulate_disconnect_raises(self):
        from iran.rubika_client import FakeRubikaTransport

        t = FakeRubikaTransport()
        t.simulate_disconnect()
        with pytest.raises(ConnectionError):
            await t.receive_loop(lambda s, r: None)


# ---------------------------------------------------------------------------
# 4. IranRubikaClient dispatch
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_transport():
    """Return (IranRubikaClient, FakeRubikaTransport) already started."""
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient

    transport = FakeRubikaTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
    config = _make_config()
    client = IranRubikaClient(config, transport=transport)
    await client.start()
    yield client, transport
    await client.stop()


async def _drain() -> None:
    """Yield control so that create_task callbacks have a chance to run."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestDispatch:
    @pytest.mark.asyncio
    async def test_handler_called_for_registered_type(self, client_and_transport):
        from iran.contracts import JobAccepted

        client, transport = client_and_transport
        received: list = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        msg = JobAccepted(
            ts=_TS, job_id=_JOB_ID, worker_version="1.0.0", queue_position=1
        )
        await transport.inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        assert len(received) == 1
        assert isinstance(received[0], JobAccepted)

    @pytest.mark.asyncio
    async def test_handler_called_exactly_once(self, client_and_transport):
        from iran.contracts import JobAccepted

        client, transport = client_and_transport
        call_count = 0

        async def _handler(msg):
            nonlocal call_count
            call_count += 1

        client.register_handler("job.accepted", _handler)

        msg = JobAccepted(ts=_TS, job_id=_JOB_ID, worker_version="1.0.0", queue_position=1)
        await transport.inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_unregistered_type_ignored(self, client_and_transport):
        from iran.contracts import AdminAck

        client, transport = client_and_transport
        called = []

        async def _handler(msg):
            called.append(msg)

        # Register handler for a different type
        client.register_handler("health.pong", _handler)

        msg = AdminAck(ts=_TS, job_id=None, acked_type="admin.clearcache", status="ok")
        await transport.inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        assert called == []

    @pytest.mark.asyncio
    async def test_message_from_wrong_sender_ignored(self, client_and_transport):
        from iran.contracts import JobAccepted

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        msg = JobAccepted(ts=_TS, job_id=_JOB_ID, worker_version="1.0.0", queue_position=1)
        wire = msg.model_dump_json()
        await transport.inject_raw("unknown-sender", f"RTUNES::{wire}")
        await _drain()

        assert received == []

    @pytest.mark.asyncio
    async def test_echo_from_own_account_ignored(self, client_and_transport):
        from iran.contracts import JobAccepted

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        msg = JobAccepted(ts=_TS, job_id=_JOB_ID, worker_version="1.0.0", queue_position=1)
        wire = msg.model_dump_json()
        await transport.inject_raw(_IRAN_GUID, f"RTUNES::{wire}")
        await _drain()

        assert received == []


# ---------------------------------------------------------------------------
# 5. Malformed message handling
# ---------------------------------------------------------------------------


class TestMalformedMessages:
    @pytest.mark.asyncio
    async def test_no_rtunes_prefix_ignored(self, client_and_transport, caplog):
        import logging

        client, transport = client_and_transport
        with caplog.at_level(logging.DEBUG, logger="iran.rubika"):
            await transport.inject_raw(_KHAREJ_GUID, '{"v":1,"type":"job.accepted"}')
            await _drain()
        # No crash, handler never reached (no registered handler needed)

    @pytest.mark.asyncio
    async def test_invalid_json_logged_as_warning(self, client_and_transport, caplog):
        import logging

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        with caplog.at_level(logging.WARNING, logger="iran.rubika"):
            await transport.inject_raw(_KHAREJ_GUID, "RTUNES::not-valid-json{{{")
            await _drain()

        assert received == []
        assert any("decode_error" in r.message or "Failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_wrong_contract_version_logged(self, client_and_transport, caplog):
        import logging

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        # v=99 is unsupported
        bad_payload = json.dumps(
            {"v": 99, "type": "job.accepted", "ts": _TS.isoformat(), "job_id": _JOB_ID,
             "worker_version": "1.0.0", "queue_position": 1}
        )
        with caplog.at_level(logging.WARNING, logger="iran.rubika"):
            await transport.inject_raw(_KHAREJ_GUID, f"RTUNES::{bad_payload}")
            await _drain()

        assert received == []

    @pytest.mark.asyncio
    async def test_oversized_message_rejected(self, client_and_transport, caplog):
        import logging

        from iran.contracts import MAX_MESSAGE_BYTES

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        oversized = "RTUNES::" + "x" * MAX_MESSAGE_BYTES
        with caplog.at_level(logging.WARNING, logger="iran.rubika"):
            await transport.inject_raw(_KHAREJ_GUID, oversized)
            await _drain()

        assert received == []
        assert any("oversized" in r.message.lower() or "Oversized" in r.message
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_required_field_logged(self, client_and_transport, caplog):
        import logging

        client, transport = client_and_transport
        received = []

        async def _handler(msg):
            received.append(msg)

        client.register_handler("job.accepted", _handler)

        # Missing worker_version and queue_position
        bad = json.dumps(
            {"v": 1, "type": "job.accepted", "ts": _TS.isoformat(), "job_id": _JOB_ID}
        )
        with caplog.at_level(logging.WARNING, logger="iran.rubika"):
            await transport.inject_raw(_KHAREJ_GUID, f"RTUNES::{bad}")
            await _drain()

        assert received == []


# ---------------------------------------------------------------------------
# 6. De-duplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_same_message_dispatched_once(self, client_and_transport):
        from iran.contracts import JobProgress

        client, transport = client_and_transport
        call_count = 0

        async def _handler(msg):
            nonlocal call_count
            call_count += 1

        client.register_handler("job.progress", _handler)

        msg = JobProgress(
            ts=_TS,
            job_id=_JOB_ID,
            phase="downloading",
            percent=42,
        )
        # Inject the same message twice
        await transport.inject_msg(_KHAREJ_GUID, msg)
        await transport.inject_msg(_KHAREJ_GUID, msg)
        await _drain()
        await asyncio.sleep(0.05)  # extra time for second task

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_different_ts_not_deduplicated(self, client_and_transport):
        from iran.contracts import JobProgress

        client, transport = client_and_transport
        call_count = 0

        async def _handler(msg):
            nonlocal call_count
            call_count += 1

        client.register_handler("job.progress", _handler)

        ts1 = datetime(2026, 4, 26, 17, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 4, 26, 17, 0, 1, tzinfo=timezone.utc)

        msg1 = JobProgress(ts=ts1, job_id=_JOB_ID, phase="downloading", percent=10)
        msg2 = JobProgress(ts=ts2, job_id=_JOB_ID, phase="downloading", percent=20)

        await transport.inject_msg(_KHAREJ_GUID, msg1)
        await transport.inject_msg(_KHAREJ_GUID, msg2)
        await _drain()
        await asyncio.sleep(0.05)

        assert call_count == 2


# ---------------------------------------------------------------------------
# 7. Reconnect supervisor
# ---------------------------------------------------------------------------


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_after_disconnect(self):
        """Supervisor reconnects after a simulated disconnect."""
        from iran.rubika_client import FakeRubikaTransport, IranRubikaClient

        connect_count = 0

        class CountingTransport(FakeRubikaTransport):
            async def connect(self) -> None:
                nonlocal connect_count
                connect_count += 1
                await super().connect()

        transport = CountingTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
        config = _make_config()
        # Use very short backoff for tests
        client = IranRubikaClient(config, transport=transport)
        client._BACKOFF_BASE = 0.01

        await client.start()
        await asyncio.sleep(0.05)  # let supervisor connect

        # Trigger a disconnect
        transport.simulate_disconnect()
        # Give the supervisor time to reconnect
        await asyncio.sleep(0.15)

        await client.stop()

        assert connect_count >= 2, f"Expected at least 2 connects, got {connect_count}"

    @pytest.mark.asyncio
    async def test_stop_prevents_further_reconnects(self):
        """Once stop() is called the supervisor loop exits cleanly."""
        from iran.rubika_client import FakeRubikaTransport, IranRubikaClient

        transport = FakeRubikaTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
        config = _make_config()
        client = IranRubikaClient(config, transport=transport)
        client._BACKOFF_BASE = 0.01

        await client.start()
        await asyncio.sleep(0.05)
        await client.stop()

        assert client._running is False
        assert client._loop_task is None or client._loop_task.done()


# ---------------------------------------------------------------------------
# 8. Outbound send
# ---------------------------------------------------------------------------


class TestOutboundSend:
    @pytest.mark.asyncio
    async def test_send_encodes_and_records(self):
        from iran.contracts import HealthPing
        from iran.rubika_client import FakeRubikaTransport, IranRubikaClient

        transport = FakeRubikaTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
        config = _make_config()
        client = IranRubikaClient(config, transport=transport)
        await client.start()

        msg = HealthPing(ts=_TS, job_id=None, request_id="req-abc123")
        await client.send(msg)
        await client.stop()

        assert len(transport.sent) == 1
        guid, wire = transport.sent[0]
        assert guid == _KHAREJ_GUID
        assert wire.startswith("RTUNES::")
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "health.ping"
        assert payload["request_id"] == "req-abc123"

    @pytest.mark.asyncio
    async def test_send_raises_rubika_send_error_on_failure(self):
        from iran.contracts import HealthPing
        from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, RubikaSendError

        class FailingTransport(FakeRubikaTransport):
            async def send_text(self, account_guid, text):
                raise OSError("network down")

        transport = FailingTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
        config = _make_config()
        client = IranRubikaClient(config, transport=transport)
        await client.start()

        msg = HealthPing(ts=_TS, job_id=None, request_id="req-fail")
        with pytest.raises(RubikaSendError):
            await client.send(msg)

        await client.stop()


# ---------------------------------------------------------------------------
# 9. EventBus
# ---------------------------------------------------------------------------


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_reaches_subscriber(self):
        from iran.event_bus import EventBus

        bus = EventBus()
        async with bus.subscribe("job-1") as queue:
            bus.publish("job-1", {"type": "test", "value": 42})
            event = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert event == {"type": "test", "value": 42}

    @pytest.mark.asyncio
    async def test_publish_fan_out(self):
        from iran.event_bus import EventBus

        bus = EventBus()
        received_a: list = []
        received_b: list = []

        async def _sub_a():
            async with bus.subscribe("job-2") as q:
                received_a.append(await asyncio.wait_for(q.get(), timeout=1.0))

        async def _sub_b():
            async with bus.subscribe("job-2") as q:
                received_b.append(await asyncio.wait_for(q.get(), timeout=1.0))

        task_a = asyncio.create_task(_sub_a())
        task_b = asyncio.create_task(_sub_b())
        await asyncio.sleep(0.01)  # allow both to subscribe

        bus.publish("job-2", {"type": "fanout"})
        await asyncio.gather(task_a, task_b)

        assert received_a == [{"type": "fanout"}]
        assert received_b == [{"type": "fanout"}]

    @pytest.mark.asyncio
    async def test_publish_to_different_job_not_received(self):
        from iran.event_bus import EventBus

        bus = EventBus()
        received = []

        async with bus.subscribe("job-3") as queue:
            bus.publish("job-4", {"type": "wrong-job"})  # different job_id
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.05)
                received.append(event)
            except asyncio.TimeoutError:
                pass

        assert received == []

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_queue(self):
        from iran.event_bus import EventBus

        bus = EventBus()
        q: asyncio.Queue = asyncio.Queue()
        bus._subscribers["job-5"].add(q)
        bus.unsubscribe("job-5", q)
        assert "job-5" not in bus._subscribers

    @pytest.mark.asyncio
    async def test_close_signals_all_subscribers(self):
        from iran.event_bus import EventBus

        bus = EventBus()
        received_sentinel: list = []

        async with bus.subscribe("job-6") as q:
            await bus.close()
            item = await asyncio.wait_for(q.get(), timeout=0.5)
            received_sentinel.append(item)

        assert received_sentinel == [None]  # None is the close sentinel

    @pytest.mark.asyncio
    async def test_make_event_bus_returns_event_bus(self):
        from iran.event_bus import EventBus, make_event_bus

        bus = make_event_bus()
        assert isinstance(bus, EventBus)


# ---------------------------------------------------------------------------
# 10. Contract fixture tests — exact JSON wire format for Iran-sent messages
# ---------------------------------------------------------------------------


class TestContractFixtures:
    """Verify encode() produces valid JSON matching the spec examples."""

    def _ts(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def test_job_create_single_wire_fields(self):
        from iran.contracts import JobCreate, Platform, encode

        msg = JobCreate(
            ts=self._ts("2026-04-26T17:05:56Z"),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            user_status="active",
            platform=Platform.spotify,
            url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            quality="flac",
            job_type="single",
            format_hint=None,
        )
        wire = encode(msg)
        assert wire.startswith("RTUNES::")
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["v"] == 1
        assert payload["type"] == "job.create"
        assert payload["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert payload["user_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        assert payload["user_status"] == "active"
        assert payload["platform"] == "spotify"
        assert payload["url"] == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        assert payload["quality"] == "flac"
        assert payload["job_type"] == "single"

    def test_job_create_batch_wire_fields(self):
        from iran.contracts import JobCreate, Platform, encode

        msg = JobCreate(
            ts=self._ts("2026-04-26T17:10:00Z"),
            job_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            user_status="active",
            platform=Platform.spotify,
            url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            quality="mp3",
            job_type="batch",
            format_hint="mp3",
            collection_name="Today's Top Hits",
            track_ids=["4uLU6hMCjMI75M1A2tKUQC", "7qiZfU4dY1lWllzX7mPBI3"],
            total_tracks=50,
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "job.create"
        assert payload["job_type"] == "batch"
        assert payload["collection_name"] == "Today's Top Hits"
        assert payload["total_tracks"] == 50
        assert "4uLU6hMCjMI75M1A2tKUQC" in payload["track_ids"]

    def test_job_cancel_wire_fields(self):
        from iran.contracts import JobCancel, encode

        msg = JobCancel(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id="550e8400-e29b-41d4-a716-446655440000",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "job.cancel"
        assert payload["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert payload["v"] == 1

    def test_health_ping_wire_fields(self):
        from iran.contracts import HealthPing, encode

        msg = HealthPing(
            ts=self._ts("2026-04-26T18:01:00Z"),
            job_id=None,
            request_id="req-a1b2c3d4",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "health.ping"
        assert payload["job_id"] is None
        assert payload["request_id"] == "req-a1b2c3d4"
        assert payload["v"] == 1

    def test_user_whitelist_add_wire_fields(self):
        from iran.contracts import UserWhitelistAdd, encode

        msg = UserWhitelistAdd(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            user_id="user-uuid-123",
            display_name="Alice",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "user.whitelist.add"
        assert payload["user_id"] == "user-uuid-123"
        assert payload["display_name"] == "Alice"

    def test_user_whitelist_remove_wire_fields(self):
        from iran.contracts import UserWhitelistRemove, encode

        msg = UserWhitelistRemove(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            user_id="user-uuid-123",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "user.whitelist.remove"
        assert payload["user_id"] == "user-uuid-123"

    def test_user_block_add_wire_fields(self):
        from iran.contracts import UserBlockAdd, encode

        msg = UserBlockAdd(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            user_id="bad-actor-uuid",
            reason="spam",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "user.block.add"
        assert payload["user_id"] == "bad-actor-uuid"
        assert payload["reason"] == "spam"

    def test_user_block_remove_wire_fields(self):
        from iran.contracts import UserBlockRemove, encode

        msg = UserBlockRemove(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            user_id="user-uuid-123",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "user.block.remove"

    def test_admin_settings_update_wire_fields(self):
        from iran.contracts import AdminSettingsUpdate, encode

        msg = AdminSettingsUpdate(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            settings={"max_jobs": 10, "rate_limit": 5},
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "admin.settings.update"
        assert payload["settings"]["max_jobs"] == 10

    def test_admin_clearcache_wire_fields(self):
        from iran.contracts import AdminClearcache, encode

        msg = AdminClearcache(
            ts=self._ts("2026-04-26T18:00:00Z"),
            job_id=None,
            target="all",
        )
        wire = encode(msg)
        payload = json.loads(wire[len("RTUNES::"):])
        assert payload["type"] == "admin.clearcache"
        assert payload["target"] == "all"

    def test_health_pong_decode_round_trip(self):
        from iran.contracts import (
            CircuitBreakerState,
            HealthPong,
            ProviderStatus,
            decode,
            encode,
        )

        msg = HealthPong(
            ts=self._ts("2026-04-26T18:01:01Z"),
            job_id=None,
            request_id="req-a1b2c3d4",
            worker_version="1.0.0",
            queue_depth=3,
            circuit_breakers=[
                CircuitBreakerState(key="spotify", state="closed", consecutive_failures=0)
            ],
            providers=[
                ProviderStatus(name="spotify", status="up", response_ms=45),
                ProviderStatus(name="tidal", status="up", response_ms=120),
            ],
            disk_free_gb=28.4,
            uptime_sec=86400,
        )
        wire = encode(msg)
        decoded = decode(wire)
        assert isinstance(decoded, HealthPong)
        assert decoded.request_id == "req-a1b2c3d4"
        assert decoded.worker_version == "1.0.0"
        assert decoded.queue_depth == 3
        assert decoded.disk_free_gb == pytest.approx(28.4)
        assert decoded.circuit_breakers[0].key == "spotify"
        assert decoded.providers[0].name == "spotify"

    def test_job_accepted_decode_round_trip(self):
        from iran.contracts import JobAccepted, decode, encode

        msg = JobAccepted(
            ts=self._ts("2026-04-26T17:05:57Z"),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            worker_version="1.0.0",
            queue_position=1,
        )
        wire = encode(msg)
        decoded = decode(wire)
        assert isinstance(decoded, JobAccepted)
        assert decoded.worker_version == "1.0.0"
        assert decoded.queue_position == 1

    def test_job_failed_decode_round_trip(self):
        from iran.contracts import JobFailed, decode, encode

        msg = JobFailed(
            ts=self._ts("2026-04-26T17:10:05Z"),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            error_code="no_source_available",
            message="All providers exhausted for this track.",
            retryable=True,
        )
        wire = encode(msg)
        decoded = decode(wire)
        assert isinstance(decoded, JobFailed)
        assert decoded.error_code == "no_source_available"
        assert decoded.retryable is True

    def test_job_completed_decode_round_trip(self):
        from iran.contracts import JobCompleted, S2ObjectRef, decode, encode

        msg = JobCompleted(
            ts=self._ts("2026-04-26T17:10:05Z"),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            parts=[
                S2ObjectRef(
                    key="media/550e8400-e29b-41d4-a716-446655440000/Shape_of_You.flac",
                    size=42000000,
                    mime="audio/flac",
                    sha256="abcdef12" * 8,
                )
            ],
            metadata={"title": "Shape of You", "artist": "Ed Sheeran"},
        )
        wire = encode(msg)
        decoded = decode(wire)
        assert isinstance(decoded, JobCompleted)
        assert decoded.parts[0].mime == "audio/flac"
        assert decoded.metadata["title"] == "Shape of You"


# ---------------------------------------------------------------------------
# 11. _LRUSet unit tests
# ---------------------------------------------------------------------------


class TestLRUSet:
    def test_contains_after_add(self):
        from iran.rubika_client import _LRUSet

        s = _LRUSet(maxsize=10)
        s.add("key1")
        assert s.contains("key1")

    def test_not_contains_before_add(self):
        from iran.rubika_client import _LRUSet

        s = _LRUSet(maxsize=10)
        assert not s.contains("key1")

    def test_evicts_oldest_at_max(self):
        from iran.rubika_client import _LRUSet

        s = _LRUSet(maxsize=3)
        s.add("a")
        s.add("b")
        s.add("c")
        s.add("d")  # should evict "a"
        assert not s.contains("a")
        assert s.contains("d")

    def test_re_add_moves_to_end(self):
        from iran.rubika_client import _LRUSet

        s = _LRUSet(maxsize=3)
        s.add("a")
        s.add("b")
        s.add("a")  # re-add a → moves to end
        s.add("c")  # fills up
        s.add("d")  # evicts b (oldest), not a
        assert not s.contains("b")
        assert s.contains("a")
        assert s.contains("d")


# ---------------------------------------------------------------------------
# 12. Integration: handlers update DB via DB session
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session_factory():
    """Fresh in-memory SQLite engine + session factory for integration tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_job(db_session_factory):
    """Insert a pending Job row and return its id + the factory."""
    from iran.db.models import Job, User

    factory = db_session_factory
    user_id = _uid()
    job_id = _uid()
    async with factory() as session:
        user = User(
            id=user_id,
            email="handler-test@example.com",
            display_name="Handler Test",
            password_hash="x",
            role="user",
            status="active",
        )
        session.add(user)
        job = Job(
            id=job_id,
            user_id=user_id,
            platform="spotify",
            url="https://open.spotify.com/track/abc",
            quality="flac",
            job_type="single",
            status="pending",
        )
        session.add(job)
        await session.commit()
    return job_id, factory


@pytest.mark.asyncio
async def test_on_job_accepted_updates_db(seeded_job, monkeypatch):
    """on_job_accepted handler updates job.status and job.accepted_at."""
    import iran.db.engine as _engine_mod

    job_id, factory = seeded_job

    # Patch the module-level engine to use the test factory
    original_factory = _engine_mod._session_factory

    async def _override_get_session():
        from fastapi import HTTPException

        async with factory() as session:
            try:
                yield session
                await session.commit()
            except HTTPException:
                await session.commit()
                raise
            except Exception:
                await session.rollback()
                raise

    import contextlib
    monkeypatch.setattr(
        _engine_mod, "get_async_session",
        contextlib.asynccontextmanager(_override_get_session),
    )

    from iran.contracts import JobAccepted

    # Build handler directly (as main.py does)
    app_state_event_bus = __import__("iran.event_bus", fromlist=["make_event_bus"]).make_event_bus()

    class _FakeApp:
        class state:
            pass

    _FakeApp.state.event_bus = app_state_event_bus

    from iran.main import _make_handlers
    handlers = _make_handlers(_FakeApp)

    msg = JobAccepted(
        ts=_TS,
        job_id=job_id,
        worker_version="1.0.0",
        queue_position=1,
    )
    await handlers["job.accepted"](msg)

    # Verify the DB row was updated
    from iran.db.models import Job
    async with factory() as session:
        job = await session.get(Job, job_id)
    assert job.status == "accepted"
    assert job.accepted_at is not None


@pytest.mark.asyncio
async def test_on_job_failed_updates_db(seeded_job, monkeypatch):
    """on_job_failed handler updates job.status, error_code, error_msg."""
    import contextlib

    import iran.db.engine as _engine_mod

    job_id, factory = seeded_job

    async def _override_get_session():
        from fastapi import HTTPException

        async with factory() as session:
            try:
                yield session
                await session.commit()
            except HTTPException:
                await session.commit()
                raise
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(
        _engine_mod, "get_async_session",
        contextlib.asynccontextmanager(_override_get_session),
    )

    from iran.contracts import JobFailed

    app_state_event_bus = __import__("iran.event_bus", fromlist=["make_event_bus"]).make_event_bus()

    class _FakeApp:
        class state:
            pass

    _FakeApp.state.event_bus = app_state_event_bus

    from iran.main import _make_handlers
    handlers = _make_handlers(_FakeApp)

    msg = JobFailed(
        ts=_TS,
        job_id=job_id,
        error_code="no_source_available",
        message="All providers failed.",
        retryable=False,
    )
    await handlers["job.failed"](msg)

    from iran.db.models import Job
    async with factory() as session:
        job = await session.get(Job, job_id)
    assert job.status == "failed"
    assert job.error_code == "no_source_available"
    assert job.error_msg == "All providers failed."


@pytest.mark.asyncio
async def test_on_health_pong_writes_setting(db_session_factory, monkeypatch):
    """on_health_pong handler writes last_health_pong to the settings table."""
    import contextlib

    import iran.db.engine as _engine_mod

    factory = db_session_factory

    async def _override_get_session():
        from fastapi import HTTPException

        async with factory() as session:
            try:
                yield session
                await session.commit()
            except HTTPException:
                await session.commit()
                raise
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(
        _engine_mod, "get_async_session",
        contextlib.asynccontextmanager(_override_get_session),
    )

    from iran.contracts import HealthPong

    class _FakeApp:
        class state:
            pass

    from iran.event_bus import make_event_bus
    _FakeApp.state.event_bus = make_event_bus()

    from iran.main import _make_handlers
    handlers = _make_handlers(_FakeApp)

    msg = HealthPong(
        ts=_TS,
        job_id=None,
        request_id="req-health-1",
        worker_version="1.0.0",
        queue_depth=2,
        circuit_breakers=[],
        providers=[],
        disk_free_gb=50.0,
        uptime_sec=3600,
    )
    await handlers["health.pong"](msg)

    from iran.db.models import Setting
    async with factory() as session:
        setting = await session.get(Setting, "last_health_pong")

    assert setting is not None
    stored = json.loads(setting.value)
    assert stored["worker_version"] == "1.0.0"
    assert stored["queue_depth"] == 2
    assert stored["request_id"] == "req-health-1"


@pytest.mark.asyncio
async def test_dedup_same_progress_updates_db_once(seeded_job, monkeypatch):
    """De-duplication: same job.progress message dispatched only once → one DB update."""
    import contextlib

    import iran.db.engine as _engine_mod
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient

    job_id, factory = seeded_job

    update_count = 0

    async def _override_get_session():
        nonlocal update_count

        from fastapi import HTTPException

        async with factory() as session:
            try:
                yield session
                update_count += 1
                await session.commit()
            except HTTPException:
                await session.commit()
                raise
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(
        _engine_mod, "get_async_session",
        contextlib.asynccontextmanager(_override_get_session),
    )

    from iran.contracts import JobProgress

    class _FakeApp:
        class state:
            pass

    from iran.event_bus import make_event_bus
    _FakeApp.state.event_bus = make_event_bus()

    from iran.main import _make_handlers
    handlers = _make_handlers(_FakeApp)

    transport = FakeRubikaTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
    config = _make_config()
    client = IranRubikaClient(config, transport=transport)
    client.register_handler("job.progress", handlers["job.progress"])
    await client.start()

    msg = JobProgress(ts=_TS, job_id=job_id, phase="downloading", percent=50)
    # Inject the same message twice
    await transport.inject_msg(_KHAREJ_GUID, msg)
    await transport.inject_msg(_KHAREJ_GUID, msg)  # duplicate
    await _drain()
    await asyncio.sleep(0.1)

    await client.stop()

    # De-duplication means only one DB update
    assert update_count == 1, f"Expected 1 DB update, got {update_count}"
