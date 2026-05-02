"""Shared contracts between the Iran VPS and the Kharej VPS worker.

This module is the **canonical Track A representation** of every control message
defined in ``docs/research/arvan-webui-migration/message-schema.md``.

Contract version: **v=1** (frozen).

See also
--------
- Spec: ``docs/research/arvan-webui-migration/message-schema.md``
- Human overview: ``docs/research/arvan-webui-migration/CONTRACTS.md``
- Task split §3: ``docs/research/arvan-webui-migration/task-split.md``

Breaking-change rule
--------------------
Any renaming or removal of a required field requires bumping
``CONTRACT_VERSION`` from ``1`` to ``2`` and following the migration rules
described in ``message-schema.md`` §12.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

CONTRACT_VERSION: int = 1
"""Current contract version.  Increment only on breaking changes."""

RTUNES_PREFIX: str = "RTUNES::"
"""Routing prefix prepended to every message sent over Rubika."""

MAX_MESSAGE_BYTES: int = 4096
"""Hard upper limit on the UTF-8-encoded size of a single Rubika message."""


# ---------------------------------------------------------------------------
# Supporting enums
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Lifecycle states of a download job (canonical: task-split.md §3.3)."""

    pending = "pending"
    accepted = "accepted"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Platform(str, Enum):
    """Media platforms supported by the Kharej Worker."""

    youtube = "youtube"
    spotify = "spotify"
    tidal = "tidal"
    qobuz = "qobuz"
    amazon = "amazon"
    soundcloud = "soundcloud"
    bandcamp = "bandcamp"
    musicdl = "musicdl"


class AccessDecision(str, Enum):
    """Result of ``access_control.check_access()``."""

    allow = "allow"
    block = "block"
    not_whitelisted = "not_whitelisted"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class S2ObjectRef(BaseModel):
    """Reference to a single object stored in Arvan S2.

    Used inside ``JobCompleted.parts``.
    Key convention: ``media/{job_id}/{safe_filename}[.ext]``
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., description="S2 object key (bucket-relative path).")
    size: int = Field(..., ge=0, description="Exact file size in bytes.")
    mime: str = Field(..., description="MIME type of the uploaded file.")
    sha256: str = Field(..., description="Hex-encoded SHA-256 of the uploaded file.")


class CircuitBreakerState(BaseModel):
    """State of a single circuit breaker, embedded in ``HealthPong``."""

    model_config = ConfigDict(extra="forbid")

    key: str
    state: Literal["closed", "open", "half-open"]
    consecutive_failures: int = 0
    seconds_until_close: int | None = None


class ProviderStatus(BaseModel):
    """Health status of a single provider, embedded in ``HealthPong``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["up", "degraded", "down"]
    response_ms: int | None = None


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """Base class shared by every control message.

    Every message is transmitted as ``RTUNES::<json>`` over Rubika.
    """

    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = 1
    """Schema version.  Always ``1`` for this contract."""

    type: str
    """Message type discriminator (e.g. ``"job.create"``)."""

    ts: datetime
    """UTC timestamp of when the message was created (ISO-8601)."""

    job_id: str | None = None
    """UUID v4 of the related job, or ``null`` for admin/health messages."""


# ---------------------------------------------------------------------------
# Job messages  (Iran → Kharej unless noted)
# ---------------------------------------------------------------------------


class JobCreate(Envelope):
    """Request a new download job.  Direction: Iran → Kharej."""

    type: Literal["job.create"] = "job.create"

    user_id: str = Field(..., description="UUID of the requesting user.")
    user_status: Literal["active", "admin"] = Field(
        ..., description="Access level of the requesting user."
    )
    platform: Platform = Field(..., description="Target media platform.")
    url: str = Field(..., description="Platform URL (validated by Iran VPS).")
    quality: str = Field(
        ...,
        description='Quality/format string, e.g. "mp3", "flac", "hires", "1080p".',
    )
    job_type: Literal["single", "batch"] = Field(
        ..., description='"single" for a track; "batch" for playlist/album.'
    )
    format_hint: str | None = Field(
        None, description='Optional user-specified format override ("mp3", "flac", "m4a").'
    )

    # Batch-only fields (present when job_type == "batch")
    collection_name: str | None = Field(
        None, description="Human-readable playlist/album name (batch jobs only)."
    )
    track_ids: list[str] | None = Field(
        None, description="Platform track IDs (omit if >200 tracks)."
    )
    total_tracks: int | None = Field(None, ge=1, description="Total track count (batch jobs only).")
    batch_seq: int | None = Field(None, ge=1, description="Sequence number for split batches.")
    batch_total: int | None = Field(None, ge=1, description="Total split-batch count.")


class JobAccepted(Envelope):
    """Acknowledge that the worker will process the job.  Direction: Kharej → Iran."""

    type: Literal["job.accepted"] = "job.accepted"

    worker_version: str = Field(..., description="Semver of the Kharej Worker.")
    queue_position: int = Field(
        ..., ge=1, description="Position in the Kharej internal queue (1 = processing now)."
    )


class JobProgress(Envelope):
    """Report download/upload progress.  Direction: Kharej → Iran.

    Published at most once every 3 seconds per job.
    """

    type: Literal["job.progress"] = "job.progress"

    phase: Literal["downloading", "processing", "uploading", "zipping"] = Field(
        ..., description="Current processing phase."
    )

    # Single-file fields
    percent: int | None = Field(None, ge=0, le=100, description="Progress 0–100 (single files).")
    speed: str | None = Field(None, description='Human-readable speed, e.g. "3.2 MB/s".')
    eta_sec: int | None = Field(None, ge=0, description="Estimated seconds remaining.")

    # Batch fields
    done_tracks: int | None = Field(None, ge=0, description="Tracks completed so far (batch).")
    total_tracks: int | None = Field(None, ge=1, description="Total tracks in batch.")
    failed_tracks: int | None = Field(None, ge=0, description="Tracks that failed (batch).")
    current_track: str | None = Field(
        None, description="Title of the track being processed (batch)."
    )

    # Multipart ZIP fields
    part: int | None = Field(None, ge=1, description="Current ZIP part being uploaded.")
    total_parts: int | None = Field(None, ge=1, description="Total ZIP parts.")


class JobCompleted(Envelope):
    """All files uploaded to S2.  Direction: Kharej → Iran."""

    type: Literal["job.completed"] = "job.completed"

    parts: list[S2ObjectRef] = Field(
        ..., min_length=1, description="Uploaded S2 objects (always an array)."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Human-readable metadata for Web UI display.",
    )


class JobFailed(Envelope):
    """The job could not be completed.  Direction: Kharej → Iran."""

    type: Literal["job.failed"] = "job.failed"

    error_code: Literal[
        "no_source_available",
        "s2_upload_failed",
        "download_timeout",
        "rate_limited",
        "invalid_url",
        "access_denied",
        "disk_space_error",
        "internal_error",
        # Worker/dispatcher codes (Step 6+)
        "blocked",
        "not_whitelisted",
        "unsupported_platform",
        "duplicate_job",
        "cancelled",
        "timeout",
        "not_implemented",
        "error",
        "shutdown",
    ] = Field(..., description="Machine-readable error code.")
    message: str = Field(..., description="Human-readable error description.")
    retryable: bool = Field(..., description="Whether the caller may retry the job.")


class JobCancel(Envelope):
    """Cancel an in-progress job.  Direction: Iran → Kharej."""

    type: Literal["job.cancel"] = "job.cancel"


# ---------------------------------------------------------------------------
# User whitelist/block messages  (Iran → Kharej)
# ---------------------------------------------------------------------------


class UserWhitelistAdd(Envelope):
    """Add a user to the Kharej whitelist.  Direction: Iran → Kharej."""

    type: Literal["user.whitelist.add"] = "user.whitelist.add"

    user_id: str = Field(..., description="UUID of the user to whitelist.")
    display_name: str | None = Field(None, description="Human-readable display name.")


class UserWhitelistRemove(Envelope):
    """Remove a user from the Kharej whitelist.  Direction: Iran → Kharej."""

    type: Literal["user.whitelist.remove"] = "user.whitelist.remove"

    user_id: str = Field(..., description="UUID of the user to remove.")


class UserBlockAdd(Envelope):
    """Block a user on the Kharej side.  Direction: Iran → Kharej."""

    type: Literal["user.block.add"] = "user.block.add"

    user_id: str = Field(..., description="UUID of the user to block.")
    reason: str | None = Field(None, description="Optional human-readable reason.")


class UserBlockRemove(Envelope):
    """Unblock a user on the Kharej side.  Direction: Iran → Kharej."""

    type: Literal["user.block.remove"] = "user.block.remove"

    user_id: str = Field(..., description="UUID of the user to unblock.")


# ---------------------------------------------------------------------------
# Admin messages  (Iran → Kharej unless noted)
# ---------------------------------------------------------------------------


class AdminClearcache(Envelope):
    """Flush metadata caches on the Kharej Worker.  Direction: Iran → Kharej."""

    type: Literal["admin.clearcache"] = "admin.clearcache"

    target: Literal["lru", "isrc", "all"] = Field(
        ..., description="Which cache(s) to flush."
    )


class AdminSettingsUpdate(Envelope):
    """Push updated runtime settings to the Kharej Worker.  Direction: Iran → Kharej."""

    type: Literal["admin.settings.update"] = "admin.settings.update"

    settings: dict[str, Any] = Field(
        ..., description="Key-value map of settings to apply."
    )


class AdminCookiesUpdate(Envelope):
    """Replace ``cookies.txt`` on the Kharej Worker.  Direction: Iran → Kharej.

    The cookies file is uploaded to S2 first (it may exceed 4 KB); this
    message tells the worker where to fetch it.
    """

    type: Literal["admin.cookies.update"] = "admin.cookies.update"

    s2_key: str = Field(
        ..., description='S2 key under ``tmp/`` where the new cookies file lives.'
    )
    sha256: str = Field(..., description="Expected hex-encoded SHA-256 of the cookies file.")


class AdminAck(Envelope):
    """Generic acknowledgement for admin control messages.  Direction: Kharej → Iran."""

    type: Literal["admin.ack"] = "admin.ack"

    acked_type: str = Field(..., description="The ``type`` of the admin message being acknowledged.")
    status: Literal["ok", "error"] = Field(..., description='"ok" or "error".')
    detail: str | None = Field(None, description="Optional human-readable detail or error message.")
    effective_config: dict[str, Any] | None = Field(
        None, description="New effective settings (populated by admin.settings.update ack)."
    )


# ---------------------------------------------------------------------------
# Health messages
# ---------------------------------------------------------------------------


class HealthPing(Envelope):
    """Request health status from the Kharej Worker.  Direction: Iran → Kharej."""

    type: Literal["health.ping"] = "health.ping"

    request_id: str = Field(..., description="Opaque identifier echoed back in health.pong.")


class HealthPong(Envelope):
    """Health status response.  Direction: Kharej → Iran."""

    type: Literal["health.pong"] = "health.pong"

    request_id: str = Field(..., description="Echoed from the corresponding health.ping.")
    worker_version: str = Field(..., description="Semver of the Kharej Worker.")
    queue_depth: int = Field(..., ge=0, description="Number of jobs in the internal queue.")
    circuit_breakers: list[CircuitBreakerState] = Field(
        default_factory=list, description="State of each circuit breaker."
    )
    providers: list[ProviderStatus] = Field(
        default_factory=list, description="Live health of each provider endpoint."
    )
    disk_free_gb: float = Field(..., ge=0.0, description="Free disk space on the Kharej VPS.")
    uptime_sec: int = Field(..., ge=0, description="Worker process uptime in seconds.")


# ---------------------------------------------------------------------------
# Discriminated union of all message types
# ---------------------------------------------------------------------------

AnyMessage = Annotated[
    JobCreate
    | JobAccepted
    | JobProgress
    | JobCompleted
    | JobFailed
    | JobCancel
    | UserWhitelistAdd
    | UserWhitelistRemove
    | UserBlockAdd
    | UserBlockRemove
    | AdminClearcache
    | AdminSettingsUpdate
    | AdminCookiesUpdate
    | AdminAck
    | HealthPing
    | HealthPong,
    Field(discriminator="type"),
]
"""Union of every concrete message class, discriminated on the ``type`` field."""


# ---------------------------------------------------------------------------
# S2 key helpers (canonical: task-split.md §3.2)
# ---------------------------------------------------------------------------


def make_media_key(job_id: str, safe_filename: str) -> str:
    """Return the S2 key for a single media file.

    Example: ``media/550e8400-…/Shape_of_You.flac``
    """
    return f"media/{job_id}/{safe_filename}"


def make_part_key(job_id: str, safe_filename: str, part: int) -> str:
    """Return the S2 key for a multipart ZIP file.

    Example: ``media/a1b2c3d4-…/TodaysTopHits-part1.zip``
    """
    return f"media/{job_id}/{safe_filename}-part{part}.zip"


def make_thumb_key(isrc_or_job_id: str) -> str:
    """Return the S2 key for a thumbnail image.

    Example: ``thumbs/GBAHS1600463.jpg``
    """
    return f"thumbs/{isrc_or_job_id}.jpg"


def make_tmp_prefix(job_id: str) -> str:
    """Return the S2 prefix used for multipart upload staging.

    Example: ``tmp/550e8400-…/``
    """
    return f"tmp/{job_id}/"


# ---------------------------------------------------------------------------
# Encode / decode helpers
# ---------------------------------------------------------------------------


def encode(msg: BaseModel) -> str:
    """Serialize *msg* to ``"RTUNES::<json>"`` ready to send over Rubika.

    Raises
    ------
    ValueError
        If the serialized message exceeds ``MAX_MESSAGE_BYTES``.
    """
    body = msg.model_dump_json()
    wire = f"{RTUNES_PREFIX}{body}"
    if len(wire.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"Encoded message is {len(wire.encode())} bytes, "
            f"exceeding the {MAX_MESSAGE_BYTES}-byte limit."
        )
    return wire


def decode(raw: str) -> AnyMessage:
    """Parse a raw Rubika text message into a typed ``AnyMessage``.

    Parameters
    ----------
    raw:
        The full text as received from Rubika (must start with ``RTUNES::``).

    Returns
    -------
    AnyMessage
        A validated, typed message object.

    Raises
    ------
    ValueError
        If *raw* does not start with ``RTUNES::`` or ``v`` ≠ 1.
    pydantic.ValidationError
        If the JSON payload does not match any known message type.
    """
    if not raw.startswith(RTUNES_PREFIX):
        raise ValueError(
            f"Message does not start with the expected prefix {RTUNES_PREFIX!r}."
        )
    body = raw[len(RTUNES_PREFIX):]
    data = json.loads(body)
    if data.get("v") != CONTRACT_VERSION:
        raise ValueError(
            f"Unsupported contract version v={data.get('v')!r}. "
            f"This receiver only handles v={CONTRACT_VERSION}."
        )
    from pydantic import TypeAdapter

    adapter: TypeAdapter[AnyMessage] = TypeAdapter(AnyMessage)
    return adapter.validate_python(data)
