"""Unit tests for Track B Step 2 — Contracts Mirror.

Verifies that:
- ``iran.contracts`` re-exports every symbol expected by Iran-side code.
- ``from iran.contracts import <Type>`` works without importing kharej directly.
- ``encode`` → ``decode`` round-trips preserve every field for all message types.
- S2 key helpers produce the expected output.
- The ``CONTRACT_VERSION`` assertion in ``iran.config`` fires on version mismatch.
- Invalid or malformed wire payloads are rejected cleanly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Importability — iran.contracts must expose all expected names
# ---------------------------------------------------------------------------


class TestImportability:
    """iran.contracts is importable and every public name is accessible."""

    def test_contracts_module_importable(self):
        import iran.contracts  # noqa: F401

    def test_job_create_importable(self):
        from iran.contracts import JobCreate  # noqa: F401

    def test_job_accepted_importable(self):
        from iran.contracts import JobAccepted  # noqa: F401

    def test_job_progress_importable(self):
        from iran.contracts import JobProgress  # noqa: F401

    def test_job_completed_importable(self):
        from iran.contracts import JobCompleted  # noqa: F401

    def test_job_failed_importable(self):
        from iran.contracts import JobFailed  # noqa: F401

    def test_job_cancel_importable(self):
        from iran.contracts import JobCancel  # noqa: F401

    def test_user_whitelist_add_importable(self):
        from iran.contracts import UserWhitelistAdd  # noqa: F401

    def test_user_whitelist_remove_importable(self):
        from iran.contracts import UserWhitelistRemove  # noqa: F401

    def test_user_block_add_importable(self):
        from iran.contracts import UserBlockAdd  # noqa: F401

    def test_user_block_remove_importable(self):
        from iran.contracts import UserBlockRemove  # noqa: F401

    def test_admin_clearcache_importable(self):
        from iran.contracts import AdminClearcache  # noqa: F401

    def test_admin_settings_update_importable(self):
        from iran.contracts import AdminSettingsUpdate  # noqa: F401

    def test_admin_cookies_update_importable(self):
        from iran.contracts import AdminCookiesUpdate  # noqa: F401

    def test_admin_ack_importable(self):
        from iran.contracts import AdminAck  # noqa: F401

    def test_health_ping_importable(self):
        from iran.contracts import HealthPing  # noqa: F401

    def test_health_pong_importable(self):
        from iran.contracts import HealthPong  # noqa: F401

    def test_s2_object_ref_importable(self):
        from iran.contracts import S2ObjectRef  # noqa: F401

    def test_job_status_importable(self):
        from iran.contracts import JobStatus  # noqa: F401

    def test_platform_importable(self):
        from iran.contracts import Platform  # noqa: F401

    def test_access_decision_importable(self):
        from iran.contracts import AccessDecision  # noqa: F401

    def test_encode_importable(self):
        from iran.contracts import encode  # noqa: F401

    def test_decode_importable(self):
        from iran.contracts import decode  # noqa: F401

    def test_key_helpers_importable(self):
        from iran.contracts import (  # noqa: F401
            make_media_key,
            make_part_key,
            make_thumb_key,
            make_tmp_prefix,
        )

    def test_contract_version_importable(self):
        from iran.contracts import CONTRACT_VERSION  # noqa: F401

    def test_rtunes_prefix_importable(self):
        from iran.contracts import RTUNES_PREFIX  # noqa: F401

    def test_max_message_bytes_importable(self):
        from iran.contracts import MAX_MESSAGE_BYTES  # noqa: F401

    def test_any_message_importable(self):
        from iran.contracts import AnyMessage  # noqa: F401

    def test_iran_contracts_identical_to_kharej(self):
        """iran.contracts symbols ARE the kharej.contracts symbols (not copies)."""
        import iran.contracts as iran_c
        import kharej.contracts as kharej_c

        assert iran_c.JobCreate is kharej_c.JobCreate
        assert iran_c.encode is kharej_c.encode
        assert iran_c.decode is kharej_c.decode
        assert iran_c.CONTRACT_VERSION == kharej_c.CONTRACT_VERSION


# ---------------------------------------------------------------------------
# 2. CONTRACT_VERSION assertion in iran.config
# ---------------------------------------------------------------------------


class TestContractVersionAssertion:
    def test_config_module_loads_with_correct_version(self):
        """iran.config must import cleanly when CONTRACT_VERSION == 1."""
        import iran.config  # noqa: F401

    def test_contract_version_is_one(self):
        from iran.contracts import CONTRACT_VERSION

        assert CONTRACT_VERSION == 1

    def test_config_asserts_version_is_one(self):
        """Patching CONTRACT_VERSION to 2 in sys.modules triggers assertion."""
        import importlib
        import types
        import unittest.mock as mock

        # Build a fake kharej.contracts module with version 2
        fake_kharej = types.ModuleType("kharej.contracts")
        fake_kharej.CONTRACT_VERSION = 2  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"kharej.contracts": fake_kharej}):
            # Re-importing iran.config (after clearing cache) must raise AssertionError
            if "iran.config" in sys.modules:
                del sys.modules["iran.config"]
            with pytest.raises(AssertionError, match="CONTRACT_VERSION"):
                importlib.import_module("iran.config")

        # Restore iran.config so later tests work
        if "iran.config" in sys.modules:
            del sys.modules["iran.config"]
        import iran.config  # re-import with real module  # noqa: F401


# ---------------------------------------------------------------------------
# 3. encode → decode round-trips for every message type
# ---------------------------------------------------------------------------


class TestEncodeDecodeRoundTrip:
    """Every concrete message type survives encode → decode with field equality."""

    def _roundtrip(self, msg):
        from iran.contracts import decode, encode

        wire = encode(msg)
        assert wire.startswith("RTUNES::")
        recovered = decode(wire)
        return recovered

    def test_roundtrip_job_create_single(self):
        from iran.contracts import JobCreate, Platform

        msg = JobCreate(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            user_status="active",
            platform=Platform.spotify,
            url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            quality="flac",
            job_type="single",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.create"
        assert recovered.job_id == msg.job_id
        assert recovered.user_id == msg.user_id
        assert recovered.platform == Platform.spotify
        assert recovered.url == msg.url
        assert recovered.quality == "flac"

    def test_roundtrip_job_create_batch(self):
        from iran.contracts import JobCreate, Platform

        msg = JobCreate(
            ts=_utcnow(),
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
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.create"
        assert recovered.job_type == "batch"
        assert recovered.collection_name == "Today's Top Hits"
        assert recovered.total_tracks == 50
        assert recovered.track_ids == ["4uLU6hMCjMI75M1A2tKUQC", "7qiZfU4dY1lWllzX7mPBI3"]

    def test_roundtrip_job_accepted(self):
        from iran.contracts import JobAccepted

        msg = JobAccepted(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            worker_version="1.0.0",
            queue_position=1,
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.accepted"
        assert recovered.worker_version == "1.0.0"
        assert recovered.queue_position == 1

    def test_roundtrip_job_progress_single(self):
        from iran.contracts import JobProgress

        msg = JobProgress(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            phase="downloading",
            percent=42,
            speed="3.2 MB/s",
            eta_sec=15,
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.progress"
        assert recovered.phase == "downloading"
        assert recovered.percent == 42
        assert recovered.speed == "3.2 MB/s"
        assert recovered.eta_sec == 15

    def test_roundtrip_job_progress_batch(self):
        from iran.contracts import JobProgress

        msg = JobProgress(
            ts=_utcnow(),
            job_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            phase="downloading",
            done_tracks=7,
            total_tracks=50,
            failed_tracks=1,
            current_track="Blinding Lights",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.progress"
        assert recovered.done_tracks == 7
        assert recovered.total_tracks == 50
        assert recovered.failed_tracks == 1
        assert recovered.current_track == "Blinding Lights"

    def test_roundtrip_job_progress_zipping(self):
        from iran.contracts import JobProgress

        msg = JobProgress(
            ts=_utcnow(),
            job_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            phase="zipping",
            part=1,
            total_parts=2,
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.progress"
        assert recovered.phase == "zipping"
        assert recovered.part == 1
        assert recovered.total_parts == 2

    def test_roundtrip_job_completed(self):
        from iran.contracts import JobCompleted, S2ObjectRef

        msg = JobCompleted(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            parts=[
                S2ObjectRef(
                    key="media/550e8400-e29b-41d4-a716-446655440000/Shape_of_You.flac",
                    size=42000000,
                    mime="audio/flac",
                    sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                )
            ],
            metadata={"title": "Shape of You", "artist": "Ed Sheeran"},
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.completed"
        assert len(recovered.parts) == 1
        assert recovered.parts[0].key == msg.parts[0].key
        assert recovered.parts[0].size == 42000000
        assert recovered.parts[0].mime == "audio/flac"
        assert recovered.metadata["title"] == "Shape of You"

    def test_roundtrip_job_failed(self):
        from iran.contracts import JobFailed

        msg = JobFailed(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            error_code="no_source_available",
            message="All providers exhausted for this track.",
            retryable=True,
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.failed"
        assert recovered.error_code == "no_source_available"
        assert recovered.retryable is True

    def test_roundtrip_job_cancel(self):
        from iran.contracts import JobCancel

        msg = JobCancel(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "job.cancel"
        assert recovered.job_id == msg.job_id

    def test_roundtrip_user_whitelist_add(self):
        from iran.contracts import UserWhitelistAdd

        msg = UserWhitelistAdd(
            ts=_utcnow(),
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            display_name="Alice",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "user.whitelist.add"
        assert recovered.user_id == msg.user_id
        assert recovered.display_name == "Alice"

    def test_roundtrip_user_whitelist_remove(self):
        from iran.contracts import UserWhitelistRemove

        msg = UserWhitelistRemove(
            ts=_utcnow(),
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "user.whitelist.remove"
        assert recovered.user_id == msg.user_id

    def test_roundtrip_user_block_add(self):
        from iran.contracts import UserBlockAdd

        msg = UserBlockAdd(
            ts=_utcnow(),
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            reason="Spam",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "user.block.add"
        assert recovered.user_id == msg.user_id
        assert recovered.reason == "Spam"

    def test_roundtrip_user_block_remove(self):
        from iran.contracts import UserBlockRemove

        msg = UserBlockRemove(
            ts=_utcnow(),
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "user.block.remove"
        assert recovered.user_id == msg.user_id

    def test_roundtrip_admin_clearcache(self):
        from iran.contracts import AdminClearcache

        msg = AdminClearcache(ts=_utcnow(), target="all")
        recovered = self._roundtrip(msg)
        assert recovered.type == "admin.clearcache"
        assert recovered.target == "all"

    def test_roundtrip_admin_settings_update(self):
        from iran.contracts import AdminSettingsUpdate

        msg = AdminSettingsUpdate(
            ts=_utcnow(),
            settings={"max_concurrent_jobs": "4", "log_level": "DEBUG"},
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "admin.settings.update"
        assert recovered.settings["max_concurrent_jobs"] == "4"

    def test_roundtrip_admin_cookies_update(self):
        from iran.contracts import AdminCookiesUpdate

        msg = AdminCookiesUpdate(
            ts=_utcnow(),
            s2_key="tmp/test-job-id/cookies.txt",
            sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "admin.cookies.update"
        assert recovered.s2_key == msg.s2_key
        assert recovered.sha256 == msg.sha256

    def test_roundtrip_admin_ack(self):
        from iran.contracts import AdminAck

        msg = AdminAck(
            ts=_utcnow(),
            acked_type="admin.clearcache",
            status="ok",
            detail="LRU cache flushed (1024 entries)",
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "admin.ack"
        assert recovered.acked_type == "admin.clearcache"
        assert recovered.status == "ok"

    def test_roundtrip_health_ping(self):
        from iran.contracts import HealthPing

        msg = HealthPing(ts=_utcnow(), request_id="req-a1b2c3d4")
        recovered = self._roundtrip(msg)
        assert recovered.type == "health.ping"
        assert recovered.request_id == "req-a1b2c3d4"

    def test_roundtrip_health_pong(self):
        from iran.contracts import CircuitBreakerState, HealthPong, ProviderStatus

        msg = HealthPong(
            ts=_utcnow(),
            request_id="req-a1b2c3d4",
            worker_version="1.0.0",
            queue_depth=3,
            circuit_breakers=[
                CircuitBreakerState(
                    key="spotify", state="closed", consecutive_failures=0
                )
            ],
            providers=[
                ProviderStatus(name="spotify", status="up", response_ms=45),
                ProviderStatus(name="tidal", status="up", response_ms=120),
            ],
            disk_free_gb=28.4,
            uptime_sec=86400,
        )
        recovered = self._roundtrip(msg)
        assert recovered.type == "health.pong"
        assert recovered.request_id == "req-a1b2c3d4"
        assert recovered.worker_version == "1.0.0"
        assert recovered.queue_depth == 3
        assert len(recovered.circuit_breakers) == 1
        assert recovered.circuit_breakers[0].key == "spotify"
        assert len(recovered.providers) == 2
        assert recovered.disk_free_gb == pytest.approx(28.4)
        assert recovered.uptime_sec == 86400


# ---------------------------------------------------------------------------
# 4. Wire-format: outgoing JSON matches contract schema
# ---------------------------------------------------------------------------


class TestWireFormat:
    """Outgoing messages conform to the spec wire format."""

    def test_job_create_wire_contains_required_fields(self):
        from iran.contracts import JobCreate, Platform, encode

        msg = JobCreate(
            ts=_utcnow(),
            job_id="550e8400-e29b-41d4-a716-446655440000",
            user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            user_status="active",
            platform=Platform.spotify,
            url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            quality="flac",
            job_type="single",
        )
        wire = encode(msg)
        body = json.loads(wire[len("RTUNES::"):])
        assert body["v"] == 1
        assert body["type"] == "job.create"
        assert "ts" in body
        assert body["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert body["user_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        assert body["platform"] == "spotify"

    def test_health_ping_wire_has_null_job_id(self):
        from iran.contracts import HealthPing, encode

        msg = HealthPing(ts=_utcnow(), request_id="req-xyz")
        wire = encode(msg)
        body = json.loads(wire[len("RTUNES::"):])
        assert body["job_id"] is None
        assert body["request_id"] == "req-xyz"

    def test_wire_starts_with_rtunes_prefix(self):
        from iran.contracts import RTUNES_PREFIX, JobCancel, encode

        msg = JobCancel(ts=_utcnow(), job_id="aaa")
        wire = encode(msg)
        assert wire.startswith(RTUNES_PREFIX)

    def test_wire_version_is_always_one(self):
        from iran.contracts import JobCancel, encode

        msg = JobCancel(ts=_utcnow(), job_id="bbb")
        wire = encode(msg)
        body = json.loads(wire[len("RTUNES::"):])
        assert body["v"] == 1


# ---------------------------------------------------------------------------
# 5. Decode: incoming JSON dispatches to correct type
# ---------------------------------------------------------------------------


class TestDecodeDispatch:
    """decode() returns the expected concrete type for each message."""

    def _decode(self, payload: dict):
        from iran.contracts import RTUNES_PREFIX, decode

        wire = f"{RTUNES_PREFIX}{json.dumps(payload)}"
        return decode(wire)

    def test_decode_job_create_returns_job_create_type(self):
        from iran.contracts import JobCreate

        payload = {
            "v": 1,
            "type": "job.create",
            "ts": "2026-04-26T17:05:56Z",
            "job_id": "550e8400-e29b-41d4-a716-446655440000",
            "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "user_status": "active",
            "platform": "spotify",
            "url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            "quality": "flac",
            "job_type": "single",
            "format_hint": None,
        }
        msg = self._decode(payload)
        assert isinstance(msg, JobCreate)
        assert msg.platform.value == "spotify"

    def test_decode_job_accepted_returns_job_accepted_type(self):
        from iran.contracts import JobAccepted

        payload = {
            "v": 1,
            "type": "job.accepted",
            "ts": "2026-04-26T17:05:57Z",
            "job_id": "550e8400-e29b-41d4-a716-446655440000",
            "worker_version": "1.0.0",
            "queue_position": 1,
        }
        msg = self._decode(payload)
        assert isinstance(msg, JobAccepted)
        assert msg.queue_position == 1

    def test_decode_job_progress_returns_job_progress_type(self):
        from iran.contracts import JobProgress

        payload = {
            "v": 1,
            "type": "job.progress",
            "ts": "2026-04-26T17:05:59Z",
            "job_id": "550e8400-e29b-41d4-a716-446655440000",
            "phase": "downloading",
            "percent": 42,
            "speed": "3.2 MB/s",
            "eta_sec": 15,
        }
        msg = self._decode(payload)
        assert isinstance(msg, JobProgress)
        assert msg.percent == 42

    def test_decode_job_completed_returns_job_completed_type(self):
        from iran.contracts import JobCompleted

        payload = {
            "v": 1,
            "type": "job.completed",
            "ts": "2026-04-26T17:10:05Z",
            "job_id": "550e8400-e29b-41d4-a716-446655440000",
            "parts": [
                {
                    "key": "media/550e8400/Shape_of_You.flac",
                    "size": 42000000,
                    "mime": "audio/flac",
                    "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                }
            ],
            "metadata": {"title": "Shape of You"},
        }
        msg = self._decode(payload)
        assert isinstance(msg, JobCompleted)
        assert msg.parts[0].mime == "audio/flac"

    def test_decode_job_failed_returns_job_failed_type(self):
        from iran.contracts import JobFailed

        payload = {
            "v": 1,
            "type": "job.failed",
            "ts": "2026-04-26T17:10:05Z",
            "job_id": "550e8400-e29b-41d4-a716-446655440000",
            "error_code": "no_source_available",
            "message": "All providers exhausted.",
            "retryable": True,
        }
        msg = self._decode(payload)
        assert isinstance(msg, JobFailed)
        assert msg.error_code == "no_source_available"
        assert msg.retryable is True

    def test_decode_health_pong_returns_health_pong_type(self):
        from iran.contracts import HealthPong

        payload = {
            "v": 1,
            "type": "health.pong",
            "ts": "2026-04-26T18:01:01Z",
            "job_id": None,
            "request_id": "req-a1b2c3d4",
            "worker_version": "1.0.0",
            "queue_depth": 3,
            "circuit_breakers": [
                {"key": "spotify", "state": "closed", "consecutive_failures": 0}
            ],
            "providers": [
                {"name": "spotify", "status": "up", "response_ms": 45}
            ],
            "disk_free_gb": 28.4,
            "uptime_sec": 86400,
        }
        msg = self._decode(payload)
        assert isinstance(msg, HealthPong)
        assert msg.request_id == "req-a1b2c3d4"

    def test_decode_admin_ack_returns_admin_ack_type(self):
        from iran.contracts import AdminAck

        payload = {
            "v": 1,
            "type": "admin.ack",
            "ts": "2026-04-26T18:00:05Z",
            "job_id": None,
            "acked_type": "admin.clearcache",
            "status": "ok",
            "detail": None,
            "effective_config": None,
        }
        msg = self._decode(payload)
        assert isinstance(msg, AdminAck)
        assert msg.acked_type == "admin.clearcache"
        assert msg.status == "ok"


# ---------------------------------------------------------------------------
# 6. Error handling — invalid payloads are rejected cleanly
# ---------------------------------------------------------------------------


class TestDecodeErrors:
    """Malformed or invalid messages raise ValueError or ValidationError."""

    def test_missing_rtunes_prefix_raises(self):
        from iran.contracts import decode

        with pytest.raises(ValueError, match="RTUNES::"):
            decode('{"v": 1, "type": "job.cancel", "ts": "2026-01-01T00:00:00Z"}')

    def test_wrong_version_raises(self):
        from iran.contracts import RTUNES_PREFIX, decode

        payload = json.dumps(
            {"v": 99, "type": "job.cancel", "ts": "2026-01-01T00:00:00Z", "job_id": None}
        )
        with pytest.raises(ValueError, match="v=99"):
            decode(f"{RTUNES_PREFIX}{payload}")

    def test_invalid_json_raises(self):
        import json as _json

        from iran.contracts import RTUNES_PREFIX, decode

        with pytest.raises(_json.JSONDecodeError):
            decode(f"{RTUNES_PREFIX}not-valid-json")

    def test_unknown_type_raises(self):
        from pydantic import ValidationError

        from iran.contracts import RTUNES_PREFIX, decode

        payload = json.dumps(
            {"v": 1, "type": "unknown.type.xyz", "ts": "2026-01-01T00:00:00Z", "job_id": None}
        )
        with pytest.raises((ValidationError, ValueError)):
            decode(f"{RTUNES_PREFIX}{payload}")

    def test_missing_required_field_raises(self):
        """Decoding a job.create without platform raises ValidationError."""
        from pydantic import ValidationError

        from iran.contracts import RTUNES_PREFIX, decode

        # platform is required
        payload = json.dumps(
            {
                "v": 1,
                "type": "job.create",
                "ts": "2026-01-01T00:00:00Z",
                "job_id": "aaa",
                "user_id": "bbb",
                "user_status": "active",
                # platform is deliberately missing
                "url": "https://example.com",
                "quality": "mp3",
                "job_type": "single",
            }
        )
        with pytest.raises(ValidationError):
            decode(f"{RTUNES_PREFIX}{payload}")


# ---------------------------------------------------------------------------
# 7. S2 key helpers
# ---------------------------------------------------------------------------


class TestS2KeyHelpers:
    def test_make_media_key(self):
        from iran.contracts import make_media_key

        key = make_media_key("job-123", "Shape_of_You.flac")
        assert key == "media/job-123/Shape_of_You.flac"

    def test_make_part_key(self):
        from iran.contracts import make_part_key

        key = make_part_key("job-456", "TodaysTopHits", 1)
        assert key == "media/job-456/TodaysTopHits-part1.zip"

    def test_make_thumb_key(self):
        from iran.contracts import make_thumb_key

        key = make_thumb_key("GBAHS1600463")
        assert key == "thumbs/GBAHS1600463.jpg"

    def test_make_tmp_prefix(self):
        from iran.contracts import make_tmp_prefix

        prefix = make_tmp_prefix("job-789")
        assert prefix == "tmp/job-789/"

    def test_media_key_contains_job_id(self):
        from iran.contracts import make_media_key

        job_id = "550e8400-e29b-41d4-a716-446655440000"
        key = make_media_key(job_id, "track.mp3")
        assert job_id in key

    def test_part_key_contains_part_number(self):
        from iran.contracts import make_part_key

        key = make_part_key("job-abc", "archive", 3)
        assert "part3" in key
        assert key.endswith(".zip")


# ---------------------------------------------------------------------------
# 8. Oversize message encoding is rejected
# ---------------------------------------------------------------------------


class TestOversizeMessage:
    def test_oversize_encode_raises_value_error(self):
        from iran.contracts import AdminSettingsUpdate, encode

        # Craft a settings dict large enough to exceed MAX_MESSAGE_BYTES
        large_settings = {f"key_{i}": "x" * 50 for i in range(200)}
        msg = AdminSettingsUpdate(ts=_utcnow(), settings=large_settings)
        with pytest.raises(ValueError, match="bytes"):
            encode(msg)
