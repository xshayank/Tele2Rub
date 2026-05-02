"""Tests for kharej/contracts.py (Step 2 — Shared Contracts)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from kharej.contracts import (
    RTUNES_PREFIX,
    AccessDecision,
    AdminAck,
    AdminClearcache,
    AdminCookiesUpdate,
    AdminSettingsUpdate,
    CircuitBreakerState,
    Envelope,
    HealthPing,
    HealthPong,
    JobAccepted,
    JobCancel,
    JobCompleted,
    JobCreate,
    JobFailed,
    JobProgress,
    JobStatus,
    Platform,
    ProviderStatus,
    S2ObjectRef,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
    decode,
    encode,
    make_media_key,
    make_part_key,
    make_thumb_key,
    make_tmp_prefix,
)

_NOW = datetime(2026, 4, 26, 17, 5, 56, tzinfo=timezone.utc)
_JOB_ID = "550e8400-e29b-41d4-a716-446655440000"
_USER_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


# ---------------------------------------------------------------------------
# Enum sanity
# ---------------------------------------------------------------------------


def test_job_status_values() -> None:
    assert set(JobStatus) == {
        JobStatus.pending,
        JobStatus.accepted,
        JobStatus.running,
        JobStatus.completed,
        JobStatus.failed,
        JobStatus.cancelled,
    }


def test_platform_values() -> None:
    assert set(Platform) == {
        Platform.youtube,
        Platform.spotify,
        Platform.tidal,
        Platform.qobuz,
        Platform.amazon,
        Platform.soundcloud,
        Platform.bandcamp,
        Platform.musicdl,
    }


def test_access_decision_values() -> None:
    assert set(AccessDecision) == {
        AccessDecision.allow,
        AccessDecision.block,
        AccessDecision.not_whitelisted,
    }


# ---------------------------------------------------------------------------
# Envelope rejects unknown fields
# ---------------------------------------------------------------------------


def test_envelope_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Envelope(v=1, type="job.create", ts=_NOW, extra_field="boom")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# JobCreate
# ---------------------------------------------------------------------------


def test_job_create_single() -> None:
    msg = JobCreate(
        ts=_NOW,
        job_id=_JOB_ID,
        user_id=_USER_ID,
        user_status="active",
        platform=Platform.spotify,
        url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        quality="flac",
        job_type="single",
        format_hint=None,
    )
    assert msg.type == "job.create"
    assert msg.v == 1
    assert msg.platform is Platform.spotify
    assert msg.job_type == "single"
    assert msg.track_ids is None


def test_job_create_batch() -> None:
    msg = JobCreate(
        ts=_NOW,
        job_id=_JOB_ID,
        user_id=_USER_ID,
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
    assert msg.job_type == "batch"
    assert msg.total_tracks == 50
    assert len(msg.track_ids) == 2  # type: ignore[arg-type]


def test_job_create_invalid_platform() -> None:
    with pytest.raises(ValidationError):
        JobCreate(
            ts=_NOW,
            job_id=_JOB_ID,
            user_id=_USER_ID,
            user_status="active",
            platform="napster",  # type: ignore[arg-type]
            url="https://example.com",
            quality="mp3",
            job_type="single",
        )


# ---------------------------------------------------------------------------
# JobAccepted
# ---------------------------------------------------------------------------


def test_job_accepted() -> None:
    msg = JobAccepted(
        ts=_NOW,
        job_id=_JOB_ID,
        worker_version="2.0.0",
        queue_position=1,
    )
    assert msg.type == "job.accepted"
    assert msg.queue_position == 1


def test_job_accepted_queue_position_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        JobAccepted(ts=_NOW, job_id=_JOB_ID, worker_version="2.0.0", queue_position=0)


# ---------------------------------------------------------------------------
# JobProgress
# ---------------------------------------------------------------------------


def test_job_progress_single() -> None:
    msg = JobProgress(
        ts=_NOW,
        job_id=_JOB_ID,
        phase="downloading",
        percent=42,
        speed="3.2 MB/s",
        eta_sec=18,
    )
    assert msg.type == "job.progress"
    assert msg.percent == 42


def test_job_progress_batch() -> None:
    msg = JobProgress(
        ts=_NOW,
        job_id=_JOB_ID,
        phase="downloading",
        done_tracks=12,
        total_tracks=50,
        failed_tracks=1,
        current_track="Shape of You — Ed Sheeran",
    )
    assert msg.done_tracks == 12
    assert msg.failed_tracks == 1


def test_job_progress_invalid_phase() -> None:
    with pytest.raises(ValidationError):
        JobProgress(ts=_NOW, job_id=_JOB_ID, phase="thinking")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JobCompleted
# ---------------------------------------------------------------------------


def test_job_completed() -> None:
    ref = S2ObjectRef(
        key=f"media/{_JOB_ID}/Shape_of_You.flac",
        size=34205696,
        mime="audio/flac",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    msg = JobCompleted(
        ts=_NOW,
        job_id=_JOB_ID,
        parts=[ref],
        metadata={"title": "Shape of You", "quality": "flac"},
    )
    assert msg.type == "job.completed"
    assert len(msg.parts) == 1
    assert msg.parts[0].mime == "audio/flac"


def test_job_completed_requires_at_least_one_part() -> None:
    with pytest.raises(ValidationError):
        JobCompleted(ts=_NOW, job_id=_JOB_ID, parts=[])


# ---------------------------------------------------------------------------
# JobFailed
# ---------------------------------------------------------------------------


def test_job_failed() -> None:
    msg = JobFailed(
        ts=_NOW,
        job_id=_JOB_ID,
        error_code="no_source_available",
        message="All download sources exhausted.",
        retryable=False,
    )
    assert msg.type == "job.failed"
    assert msg.retryable is False


def test_job_failed_invalid_error_code() -> None:
    with pytest.raises(ValidationError):
        JobFailed(
            ts=_NOW,
            job_id=_JOB_ID,
            error_code="oops",  # type: ignore[arg-type]
            message="x",
            retryable=True,
        )


# ---------------------------------------------------------------------------
# JobCancel
# ---------------------------------------------------------------------------


def test_job_cancel() -> None:
    msg = JobCancel(ts=_NOW, job_id=_JOB_ID)
    assert msg.type == "job.cancel"


# ---------------------------------------------------------------------------
# User whitelist / block messages
# ---------------------------------------------------------------------------


def test_user_whitelist_add() -> None:
    msg = UserWhitelistAdd(ts=_NOW, user_id=_USER_ID, display_name="Ali Rezaei")
    assert msg.type == "user.whitelist.add"
    assert msg.display_name == "Ali Rezaei"


def test_user_whitelist_remove() -> None:
    msg = UserWhitelistRemove(ts=_NOW, user_id=_USER_ID)
    assert msg.type == "user.whitelist.remove"


def test_user_block_add() -> None:
    msg = UserBlockAdd(ts=_NOW, user_id=_USER_ID, reason="Spam detected")
    assert msg.type == "user.block.add"
    assert msg.reason == "Spam detected"


def test_user_block_remove() -> None:
    msg = UserBlockRemove(ts=_NOW, user_id=_USER_ID)
    assert msg.type == "user.block.remove"


# ---------------------------------------------------------------------------
# Admin messages
# ---------------------------------------------------------------------------


def test_admin_clearcache() -> None:
    msg = AdminClearcache(ts=_NOW, target="all")
    assert msg.type == "admin.clearcache"
    assert msg.target == "all"


def test_admin_clearcache_invalid_target() -> None:
    with pytest.raises(ValidationError):
        AdminClearcache(ts=_NOW, target="banana")  # type: ignore[arg-type]


def test_admin_settings_update() -> None:
    msg = AdminSettingsUpdate(
        ts=_NOW,
        settings={"BATCH_CONCURRENCY": 4, "USER_TRACKS_PER_HOUR": 50},
    )
    assert msg.type == "admin.settings.update"
    assert msg.settings["BATCH_CONCURRENCY"] == 4


def test_admin_cookies_update() -> None:
    msg = AdminCookiesUpdate(
        ts=_NOW,
        s2_key="tmp/cookies-update-2026-04-26.txt",
        sha256="abc123def456",
    )
    assert msg.type == "admin.cookies.update"
    assert msg.s2_key.startswith("tmp/")


def test_admin_ack_ok() -> None:
    msg = AdminAck(ts=_NOW, acked_type="admin.clearcache", status="ok")
    assert msg.type == "admin.ack"
    assert msg.status == "ok"


def test_admin_ack_error() -> None:
    msg = AdminAck(
        ts=_NOW,
        acked_type="admin.settings.update",
        status="error",
        detail="Unknown setting key: FOO",
    )
    assert msg.status == "error"
    assert "FOO" in msg.detail  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Health messages
# ---------------------------------------------------------------------------


def test_health_ping() -> None:
    msg = HealthPing(ts=_NOW, request_id="ping-abc123")
    assert msg.type == "health.ping"
    assert msg.request_id == "ping-abc123"


def test_health_pong() -> None:
    msg = HealthPong(
        ts=_NOW,
        request_id="ping-abc123",
        worker_version="2.0.0",
        queue_depth=3,
        circuit_breakers=[
            CircuitBreakerState(key="qobuz", state="closed", consecutive_failures=0),
            CircuitBreakerState(
                key="deezer",
                state="open",
                consecutive_failures=4,
                seconds_until_close=312,
            ),
        ],
        providers=[
            ProviderStatus(name="Qobuz", status="up", response_ms=145),
            ProviderStatus(name="Deezer", status="down", response_ms=None),
        ],
        disk_free_gb=42.3,
        uptime_sec=86400,
    )
    assert msg.type == "health.pong"
    assert msg.queue_depth == 3
    assert msg.disk_free_gb == pytest.approx(42.3)
    assert len(msg.circuit_breakers) == 2
    assert len(msg.providers) == 2


# ---------------------------------------------------------------------------
# S2ObjectRef validation
# ---------------------------------------------------------------------------


def test_s2_object_ref_negative_size_rejected() -> None:
    with pytest.raises(ValidationError):
        S2ObjectRef(key="media/x/y.flac", size=-1, mime="audio/flac", sha256="abc")


# ---------------------------------------------------------------------------
# S2 key helpers
# ---------------------------------------------------------------------------


def test_make_media_key() -> None:
    key = make_media_key(_JOB_ID, "Shape_of_You.flac")
    assert key == f"media/{_JOB_ID}/Shape_of_You.flac"


def test_make_part_key() -> None:
    key = make_part_key(_JOB_ID, "TodaysTopHits", 1)
    assert key == f"media/{_JOB_ID}/TodaysTopHits-part1.zip"


def test_make_thumb_key() -> None:
    assert make_thumb_key("GBAHS1600463") == "thumbs/GBAHS1600463.jpg"


def test_make_tmp_prefix() -> None:
    assert make_tmp_prefix(_JOB_ID) == f"tmp/{_JOB_ID}/"


# ---------------------------------------------------------------------------
# encode / decode round-trip
# ---------------------------------------------------------------------------


def _make_job_create() -> JobCreate:
    return JobCreate(
        ts=_NOW,
        job_id=_JOB_ID,
        user_id=_USER_ID,
        user_status="active",
        platform=Platform.spotify,
        url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        quality="flac",
        job_type="single",
    )


def test_encode_starts_with_prefix() -> None:
    wire = encode(_make_job_create())
    assert wire.startswith(RTUNES_PREFIX)


def test_encode_body_is_valid_json() -> None:
    wire = encode(_make_job_create())
    body = wire[len(RTUNES_PREFIX):]
    data = json.loads(body)
    assert data["type"] == "job.create"
    assert data["v"] == 1


def test_decode_round_trip() -> None:
    original = _make_job_create()
    wire = encode(original)
    decoded = decode(wire)
    assert isinstance(decoded, JobCreate)
    assert decoded.job_id == _JOB_ID
    assert decoded.platform is Platform.spotify


def test_decode_all_message_types() -> None:
    """Ensure every concrete message type can round-trip through encode/decode."""
    messages: list[Envelope] = [
        _make_job_create(),
        JobAccepted(ts=_NOW, job_id=_JOB_ID, worker_version="2.0.0", queue_position=1),
        JobProgress(ts=_NOW, job_id=_JOB_ID, phase="downloading", percent=50),
        JobCompleted(
            ts=_NOW,
            job_id=_JOB_ID,
            parts=[S2ObjectRef(key="media/x/y.flac", size=100, mime="audio/flac", sha256="abc")],
        ),
        JobFailed(
            ts=_NOW, job_id=_JOB_ID, error_code="internal_error", message="oops", retryable=True
        ),
        JobCancel(ts=_NOW, job_id=_JOB_ID),
        UserWhitelistAdd(ts=_NOW, user_id=_USER_ID),
        UserWhitelistRemove(ts=_NOW, user_id=_USER_ID),
        UserBlockAdd(ts=_NOW, user_id=_USER_ID),
        UserBlockRemove(ts=_NOW, user_id=_USER_ID),
        AdminClearcache(ts=_NOW, target="all"),
        AdminSettingsUpdate(ts=_NOW, settings={"k": "v"}),
        AdminCookiesUpdate(ts=_NOW, s2_key="tmp/c.txt", sha256="abc"),
        AdminAck(ts=_NOW, acked_type="admin.clearcache", status="ok"),
        HealthPing(ts=_NOW, request_id="r1"),
        HealthPong(
            ts=_NOW,
            request_id="r1",
            worker_version="2.0.0",
            queue_depth=0,
            disk_free_gb=10.0,
            uptime_sec=100,
        ),
    ]
    for msg in messages:
        wire = encode(msg)
        decoded = decode(wire)
        assert decoded.type == msg.type, f"Round-trip failed for {msg.type}"


def test_decode_rejects_missing_prefix() -> None:
    with pytest.raises(ValueError, match="prefix"):
        decode('{"v": 1, "type": "job.create", "ts": "2026-04-26T17:05:56Z"}')


def test_decode_rejects_wrong_version() -> None:
    wire = f'{RTUNES_PREFIX}{{"v": 2, "type": "job.create", "ts": "2026-04-26T17:05:56Z"}}'
    with pytest.raises(ValueError, match="version"):
        decode(wire)


def test_decode_rejects_unknown_type() -> None:
    wire = (
        f'{RTUNES_PREFIX}{{"v": 1, "type": "not.a.real.type", '
        f'"ts": "2026-04-26T17:05:56Z", "job_id": null}}'
    )
    with pytest.raises((ValueError, Exception)):
        decode(wire)


def test_encode_raises_on_oversized_message() -> None:
    """A message that exceeds MAX_MESSAGE_BYTES must raise ValueError."""
    big_settings = {"k" * i: "v" * 100 for i in range(1, 60)}
    msg = AdminSettingsUpdate(ts=_NOW, settings=big_settings)
    with pytest.raises(ValueError, match="bytes"):
        encode(msg)
