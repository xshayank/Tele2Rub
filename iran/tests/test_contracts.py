"""Golden-fixture tests for the Iran ↔ Kharej contract encoder/decoder.

Step 10 of Track B: verifies that every message type defined in
``kharej/contracts.py`` can be round-tripped through ``encode(decode(wire))``
without data loss.

For every fixture in ``iran/tests/fixtures/``:
1. Load the JSON and reconstruct the wire string
   ``RTUNES::<json.dumps(data)>``.
2. Pass through ``decode(wire)`` → typed ``AnyMessage``.
3. Assert the result is the expected Pydantic type.
4. Assert ``encode(decode(wire)) == wire`` (round-trip idempotency).
5. Assert every required field is present and has the correct Python type.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    """Load a JSON fixture file and return the parsed dict."""
    path = _FIXTURES_DIR / f"{name}.json"
    assert path.exists(), f"Fixture not found: {path}"
    return json.loads(path.read_text())


def _wire(data: dict) -> str:
    """Re-encode *data* to ``RTUNES::<compact-json>`` as pydantic would produce."""
    from iran.contracts import RTUNES_PREFIX

    return RTUNES_PREFIX + json.dumps(data, separators=(",", ":"))


def _roundtrip(name: str):
    """Load fixture, decode, re-encode, assert wire == original encode(decode)."""
    from iran.contracts import decode, encode

    data = _load_fixture(name)
    # Reconstruct the wire string from the fixture JSON
    fixture_wire = _wire(data)

    msg = decode(fixture_wire)
    assert msg is not None

    # encode(decode(wire)) must equal the wire produced by pydantic's
    # model_dump_json() — i.e. the canonical serialization.
    canonical_wire = encode(msg)
    canonical_data = json.loads(canonical_wire.split("::", 1)[1])
    fixture_data = json.loads(fixture_wire.split("::", 1)[1])
    assert canonical_data == fixture_data, (
        f"Round-trip mismatch for '{name}':\n"
        f"  fixture:   {fixture_data}\n"
        f"  canonical: {canonical_data}"
    )
    return msg


# ===========================================================================
# 1. Job messages
# ===========================================================================


class TestJobCreateSingle:
    def test_decode_type(self):
        from iran.contracts import JobCreate

        msg = _roundtrip("job.create.single")
        assert isinstance(msg, JobCreate)

    def test_required_fields(self):
        from iran.contracts import JobCreate

        msg = _roundtrip("job.create.single")
        assert isinstance(msg, JobCreate)
        assert isinstance(msg.v, int) and msg.v == 1
        assert msg.type == "job.create"
        assert isinstance(msg.ts, datetime)
        assert isinstance(msg.job_id, str)
        assert isinstance(msg.user_id, str)
        assert msg.user_status == "active"
        assert msg.platform.value == "spotify"
        assert msg.url.startswith("https://open.spotify.com/")
        assert msg.quality == "mp3"
        assert msg.job_type == "single"

    def test_optional_batch_fields_are_none(self):
        msg = _roundtrip("job.create.single")
        assert msg.collection_name is None
        assert msg.track_ids is None
        assert msg.total_tracks is None
        assert msg.batch_seq is None
        assert msg.batch_total is None


class TestJobCreateBatch:
    def test_decode_type(self):
        from iran.contracts import JobCreate

        msg = _roundtrip("job.create.batch")
        assert isinstance(msg, JobCreate)

    def test_batch_fields(self):
        msg = _roundtrip("job.create.batch")
        assert msg.job_type == "batch"
        assert msg.collection_name == "Top 50 Global"
        assert isinstance(msg.track_ids, list)
        assert len(msg.track_ids) == 3
        assert isinstance(msg.total_tracks, int) and msg.total_tracks == 3
        assert msg.batch_seq == 1
        assert msg.batch_total == 1


class TestJobAccepted:
    def test_decode_type(self):
        from iran.contracts import JobAccepted

        msg = _roundtrip("job.accepted")
        assert isinstance(msg, JobAccepted)

    def test_fields(self):
        from iran.contracts import JobAccepted

        msg = _roundtrip("job.accepted")
        assert isinstance(msg, JobAccepted)
        assert isinstance(msg.worker_version, str)
        assert isinstance(msg.queue_position, int)
        assert msg.queue_position >= 1


class TestJobProgressSingle:
    def test_decode_type(self):
        from iran.contracts import JobProgress

        msg = _roundtrip("job.progress.single")
        assert isinstance(msg, JobProgress)

    def test_single_fields(self):
        from iran.contracts import JobProgress

        msg = _roundtrip("job.progress.single")
        assert isinstance(msg, JobProgress)
        assert msg.phase == "downloading"
        assert isinstance(msg.percent, int)
        assert 0 <= msg.percent <= 100
        assert isinstance(msg.speed, str)
        assert isinstance(msg.eta_sec, int)


class TestJobProgressBatch:
    def test_decode_type(self):
        from iran.contracts import JobProgress

        msg = _roundtrip("job.progress.batch")
        assert isinstance(msg, JobProgress)

    def test_batch_fields(self):
        msg = _roundtrip("job.progress.batch")
        assert msg.phase == "downloading"
        assert isinstance(msg.done_tracks, int)
        assert isinstance(msg.total_tracks, int)
        assert isinstance(msg.failed_tracks, int)
        assert isinstance(msg.current_track, str)


class TestJobProgressZipping:
    def test_decode_type(self):
        from iran.contracts import JobProgress

        msg = _roundtrip("job.progress.zipping")
        assert isinstance(msg, JobProgress)

    def test_zipping_fields(self):
        msg = _roundtrip("job.progress.zipping")
        assert msg.phase == "zipping"
        assert isinstance(msg.part, int) and msg.part >= 1
        assert isinstance(msg.total_parts, int) and msg.total_parts >= 1


class TestJobCompletedSingle:
    def test_decode_type(self):
        from iran.contracts import JobCompleted

        msg = _roundtrip("job.completed.single")
        assert isinstance(msg, JobCompleted)

    def test_parts(self):
        from iran.contracts import JobCompleted, S2ObjectRef

        msg = _roundtrip("job.completed.single")
        assert isinstance(msg, JobCompleted)
        assert len(msg.parts) == 1
        part = msg.parts[0]
        assert isinstance(part, S2ObjectRef)
        assert isinstance(part.key, str)
        assert isinstance(part.size, int)
        assert isinstance(part.mime, str)
        assert isinstance(part.sha256, str)

    def test_metadata(self):
        msg = _roundtrip("job.completed.single")
        assert isinstance(msg.metadata, dict)
        assert "title" in msg.metadata
        assert "artist" in msg.metadata


class TestJobCompletedMultipart:
    def test_decode_type(self):
        from iran.contracts import JobCompleted

        msg = _roundtrip("job.completed.multipart")
        assert isinstance(msg, JobCompleted)

    def test_multiple_parts(self):
        msg = _roundtrip("job.completed.multipart")
        assert len(msg.parts) == 2
        for part in msg.parts:
            assert part.mime == "application/zip"


class TestJobFailed:
    def test_decode_type(self):
        from iran.contracts import JobFailed

        msg = _roundtrip("job.failed")
        assert isinstance(msg, JobFailed)

    def test_fields(self):
        from iran.contracts import JobFailed

        msg = _roundtrip("job.failed")
        assert isinstance(msg, JobFailed)
        assert isinstance(msg.error_code, str)
        assert isinstance(msg.message, str)
        assert isinstance(msg.retryable, bool)


class TestJobCancel:
    def test_decode_type(self):
        from iran.contracts import JobCancel

        msg = _roundtrip("job.cancel")
        assert isinstance(msg, JobCancel)

    def test_fields(self):
        from iran.contracts import JobCancel

        msg = _roundtrip("job.cancel")
        assert isinstance(msg, JobCancel)
        assert msg.type == "job.cancel"
        assert isinstance(msg.job_id, str)


# ===========================================================================
# 2. User whitelist / block messages
# ===========================================================================


class TestUserWhitelistAdd:
    def test_decode_type(self):
        from iran.contracts import UserWhitelistAdd

        msg = _roundtrip("user.whitelist.add")
        assert isinstance(msg, UserWhitelistAdd)

    def test_fields(self):
        from iran.contracts import UserWhitelistAdd

        msg = _roundtrip("user.whitelist.add")
        assert isinstance(msg, UserWhitelistAdd)
        assert isinstance(msg.user_id, str)
        assert isinstance(msg.display_name, str)


class TestUserWhitelistRemove:
    def test_decode_type(self):
        from iran.contracts import UserWhitelistRemove

        msg = _roundtrip("user.whitelist.remove")
        assert isinstance(msg, UserWhitelistRemove)

    def test_fields(self):
        from iran.contracts import UserWhitelistRemove

        msg = _roundtrip("user.whitelist.remove")
        assert isinstance(msg, UserWhitelistRemove)
        assert isinstance(msg.user_id, str)


class TestUserBlockAdd:
    def test_decode_type(self):
        from iran.contracts import UserBlockAdd

        msg = _roundtrip("user.block.add")
        assert isinstance(msg, UserBlockAdd)

    def test_fields(self):
        from iran.contracts import UserBlockAdd

        msg = _roundtrip("user.block.add")
        assert isinstance(msg, UserBlockAdd)
        assert isinstance(msg.user_id, str)
        assert isinstance(msg.reason, str)


class TestUserBlockRemove:
    def test_decode_type(self):
        from iran.contracts import UserBlockRemove

        msg = _roundtrip("user.block.remove")
        assert isinstance(msg, UserBlockRemove)

    def test_fields(self):
        from iran.contracts import UserBlockRemove

        msg = _roundtrip("user.block.remove")
        assert isinstance(msg, UserBlockRemove)
        assert isinstance(msg.user_id, str)


# ===========================================================================
# 3. Admin messages
# ===========================================================================


class TestAdminClearcache:
    def test_decode_type(self):
        from iran.contracts import AdminClearcache

        msg = _roundtrip("admin.clearcache")
        assert isinstance(msg, AdminClearcache)

    def test_target_field(self):
        from iran.contracts import AdminClearcache

        msg = _roundtrip("admin.clearcache")
        assert isinstance(msg, AdminClearcache)
        assert msg.target in ("lru", "isrc", "all")


class TestAdminSettingsUpdate:
    def test_decode_type(self):
        from iran.contracts import AdminSettingsUpdate

        msg = _roundtrip("admin.settings.update")
        assert isinstance(msg, AdminSettingsUpdate)

    def test_settings_dict(self):
        from iran.contracts import AdminSettingsUpdate

        msg = _roundtrip("admin.settings.update")
        assert isinstance(msg, AdminSettingsUpdate)
        assert isinstance(msg.settings, dict)
        assert len(msg.settings) > 0


class TestAdminCookiesUpdate:
    def test_decode_type(self):
        from iran.contracts import AdminCookiesUpdate

        msg = _roundtrip("admin.cookies.update")
        assert isinstance(msg, AdminCookiesUpdate)

    def test_fields(self):
        from iran.contracts import AdminCookiesUpdate

        msg = _roundtrip("admin.cookies.update")
        assert isinstance(msg, AdminCookiesUpdate)
        assert isinstance(msg.s2_key, str)
        assert isinstance(msg.sha256, str)


class TestAdminAckOk:
    def test_decode_type(self):
        from iran.contracts import AdminAck

        msg = _roundtrip("admin.ack.ok")
        assert isinstance(msg, AdminAck)

    def test_ok_fields(self):
        from iran.contracts import AdminAck

        msg = _roundtrip("admin.ack.ok")
        assert isinstance(msg, AdminAck)
        assert msg.status == "ok"
        assert isinstance(msg.acked_type, str)
        assert isinstance(msg.detail, str)
        assert isinstance(msg.effective_config, dict)


class TestAdminAckError:
    def test_decode_type(self):
        from iran.contracts import AdminAck

        msg = _roundtrip("admin.ack.error")
        assert isinstance(msg, AdminAck)

    def test_error_fields(self):
        from iran.contracts import AdminAck

        msg = _roundtrip("admin.ack.error")
        assert isinstance(msg, AdminAck)
        assert msg.status == "error"
        assert isinstance(msg.detail, str)
        assert msg.effective_config is None


# ===========================================================================
# 4. Health messages
# ===========================================================================


class TestHealthPing:
    def test_decode_type(self):
        from iran.contracts import HealthPing

        msg = _roundtrip("health.ping")
        assert isinstance(msg, HealthPing)

    def test_fields(self):
        from iran.contracts import HealthPing

        msg = _roundtrip("health.ping")
        assert isinstance(msg, HealthPing)
        assert isinstance(msg.request_id, str)


class TestHealthPong:
    def test_decode_type(self):
        from iran.contracts import HealthPong

        msg = _roundtrip("health.pong")
        assert isinstance(msg, HealthPong)

    def test_fields(self):
        from iran.contracts import CircuitBreakerState, HealthPong, ProviderStatus

        msg = _roundtrip("health.pong")
        assert isinstance(msg, HealthPong)
        assert isinstance(msg.request_id, str)
        assert isinstance(msg.worker_version, str)
        assert isinstance(msg.queue_depth, int)
        assert isinstance(msg.disk_free_gb, float)
        assert isinstance(msg.uptime_sec, int)
        assert isinstance(msg.circuit_breakers, list)
        assert all(isinstance(cb, CircuitBreakerState) for cb in msg.circuit_breakers)
        assert isinstance(msg.providers, list)
        assert all(isinstance(p, ProviderStatus) for p in msg.providers)


# ===========================================================================
# 5. All fixtures are present
# ===========================================================================


class TestAllFixturesPresent:
    """Sanity-check that every expected fixture file exists."""

    EXPECTED = [
        "job.create.single",
        "job.create.batch",
        "job.accepted",
        "job.progress.single",
        "job.progress.batch",
        "job.progress.zipping",
        "job.completed.single",
        "job.completed.multipart",
        "job.failed",
        "job.cancel",
        "user.whitelist.add",
        "user.whitelist.remove",
        "user.block.add",
        "user.block.remove",
        "admin.clearcache",
        "admin.settings.update",
        "admin.cookies.update",
        "admin.ack.ok",
        "admin.ack.error",
        "health.ping",
        "health.pong",
    ]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_fixture_exists(self, name):
        path = _FIXTURES_DIR / f"{name}.json"
        assert path.exists(), f"Missing fixture: {name}.json"

    @pytest.mark.parametrize("name", EXPECTED)
    def test_fixture_roundtrips(self, name):
        """Each fixture must round-trip through decode → encode without change."""
        _roundtrip(name)


# ===========================================================================
# 6. decode() error cases
# ===========================================================================


class TestDecodeErrors:
    def test_missing_prefix_raises(self):
        from iran.contracts import decode

        with pytest.raises(ValueError, match="prefix"):
            decode('{"v":1,"type":"job.cancel","ts":"2026-04-26T17:05:56Z","job_id":"abc"}')

    def test_wrong_version_raises(self):
        import json as _json

        from iran.contracts import RTUNES_PREFIX, decode

        data = {"v": 2, "type": "job.cancel", "ts": "2026-04-26T17:05:56Z", "job_id": "abc"}
        with pytest.raises(ValueError, match="version"):
            decode(RTUNES_PREFIX + _json.dumps(data))

    def test_unknown_type_raises(self):
        import json as _json

        from pydantic import ValidationError

        from iran.contracts import RTUNES_PREFIX, decode

        data = {"v": 1, "type": "unknown.type", "ts": "2026-04-26T17:05:56Z", "job_id": None}
        with pytest.raises((ValueError, ValidationError)):
            decode(RTUNES_PREFIX + _json.dumps(data))
