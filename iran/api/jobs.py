"""Core job API endpoints for the Iran VPS service (Track B, Step 7).

Endpoints
---------
POST   /jobs                        JWT (active)             Create a new download job
GET    /jobs                        JWT (active)             Paginated list of user's jobs
GET    /jobs/{id}                   JWT (owner or admin)     Current job state
GET    /jobs/{id}/events            JWT (owner or admin)     SSE stream of job events
DELETE /jobs/{id}                   JWT (owner or admin)     Cancel job
GET    /jobs/{id}/download          JWT (owner or admin)     List download parts
GET    /jobs/{id}/download?part=N   JWT (owner or admin)     302 redirect to presigned URL
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from iran.api.deps import get_current_user, get_db
from iran.contracts import JobCancel, JobCreate, Platform
from iran.db.models import AuditLog, Job

logger = logging.getLogger("iran.api.jobs")

router = APIRouter(prefix="/jobs", tags=["jobs"])

# ---------------------------------------------------------------------------
# SSRF-prevention: allowed URL domains
# ---------------------------------------------------------------------------

ALLOWED_DOMAINS: set[str] = {
    "open.spotify.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "music.youtube.com",
    "tidal.com",
    "www.tidal.com",
    "open.tidal.com",
    "qobuz.com",
    "www.qobuz.com",
    "open.qobuz.com",
    "amazon.com",
    "music.amazon.com",
    "www.amazon.com",
    "soundcloud.com",
    "www.soundcloud.com",
    "bandcamp.com",
}

# ---------------------------------------------------------------------------
# Error code → human-readable UI message
# ---------------------------------------------------------------------------

ERROR_CODE_MESSAGES: dict[str, str] = {
    "no_source_available": "No download source found for this track.",
    "s2_upload_failed": "Storage upload failed. Please retry.",
    "download_timeout": "Download timed out. Please retry.",
    "rate_limited": "Rate limited by source. Please try again later.",
    "invalid_url": "Invalid or unsupported URL.",
    "access_denied": "Access denied on the worker side.",
    "disk_space_error": "Worker disk full. Contact admin.",
    "blocked": "Your account has been blocked.",
    "not_whitelisted": "Your account is not whitelisted for downloads.",
    "unsupported_platform": "Platform not supported.",
    "duplicate_job": "This job is already in progress.",
    "cancelled": "Job was cancelled.",
    "internal_error": "An internal error occurred. Please retry.",
    "error": "An internal error occurred. Please retry.",
    # Additional codes from contracts
    "timeout": "The operation timed out. Please retry.",
    "not_implemented": "This feature is not yet implemented.",
    "shutdown": "The worker was shut down. Please retry.",
}


# ---------------------------------------------------------------------------
# SSRF validation
# ---------------------------------------------------------------------------


def validate_job_url(url: str) -> str:
    """Validate *url* for SSRF safety.

    - Only ``https`` and ``http`` schemes allowed.
    - Hostname must be in :data:`ALLOWED_DOMAINS`.
    - Private / loopback / link-local IP ranges rejected.

    Returns the (unchanged) *url* on success.
    Raises :class:`~fastapi.HTTPException` 422 on failure.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Malformed URL.",
        )

    if parsed.scheme not in ("https", "http"):
        raise HTTPException(
            status_code=422,
            detail="Invalid URL scheme.",
        )

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise HTTPException(
            status_code=422,
            detail="URL has no hostname.",
        )

    # Reject private / loopback / link-local addresses
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(
                status_code=422,
                detail="URL domain not allowed.",
            )
    except ValueError:
        pass  # not an IP address — hostname check below

    if hostname not in ALLOWED_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail="URL domain not allowed.",
        )

    return url


# ---------------------------------------------------------------------------
# Rate limit helper
# ---------------------------------------------------------------------------

_DEFAULT_MAX_JOBS_PER_HOUR = 10


async def _get_max_jobs_per_hour(session: AsyncSession) -> int:
    """Return MAX_JOBS_PER_HOUR from the settings table (default 10)."""
    from iran.db.models import Setting

    row = await session.get(Setting, "MAX_JOBS_PER_HOUR")
    if row is not None:
        try:
            return int(row.value)
        except (ValueError, TypeError):
            pass
    return _DEFAULT_MAX_JOBS_PER_HOUR


async def _check_rate_limit(user_id: str, session: AsyncSession) -> None:
    """Raise 429 if the user has submitted too many jobs in the last hour."""
    max_jobs = await _get_max_jobs_per_hour(session)
    window_start = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    count_result = await session.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor_id == user_id,
            AuditLog.action == "job.created",
            AuditLog.created_at >= window_start,
        )
    )
    count = count_result.scalar_one()
    if count >= max_jobs:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: at most {max_jobs} jobs per hour.",
        )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    """Request body for ``POST /jobs``."""

    url: str
    platform: Platform
    quality: str = "mp3"
    job_type: str = "single"
    format_hint: str | None = None
    collection_name: str | None = None
    total_tracks: int | None = None

    @field_validator("job_type")
    @classmethod
    def _validate_job_type(cls, v: str) -> str:
        if v not in ("single", "batch"):
            raise ValueError("job_type must be 'single' or 'batch'")
        return v

    @field_validator("quality")
    @classmethod
    def _validate_quality(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("quality must not be empty")
        return v.strip()


class JobResponse(BaseModel):
    """Serialised job row returned by ``GET /jobs/{id}``."""

    job_id: str
    user_id: str
    platform: str
    url: str
    quality: str | None
    job_type: str
    status: str
    progress: int
    speed: str | None
    phase: str | None
    error_code: str | None
    error_message: str | None
    s2_keys: Any | None
    total_tracks: int | None
    done_tracks: int
    failed_tracks: int
    current_track: str | None
    metadata: Any | None
    created_at: str | None
    accepted_at: str | None
    completed_at: str | None


def _job_to_response(job: Job) -> dict[str, Any]:
    """Convert a :class:`Job` ORM instance to a plain dict."""
    return {
        "job_id": job.id,
        "user_id": job.user_id,
        "platform": job.platform,
        "url": job.url,
        "quality": job.quality,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "speed": job.speed,
        "phase": job.phase,
        "error_code": job.error_code,
        "error_message": ERROR_CODE_MESSAGES.get(job.error_code or "", job.error_msg),
        "s2_keys": job.s2_keys,
        "total_tracks": job.total_tracks,
        "done_tracks": job.done_tracks,
        "failed_tracks": job.failed_tracks,
        "current_track": job.current_track,
        "metadata": job.metadata_json,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "accepted_at": job.accepted_at.isoformat() if job.accepted_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Helper: ownership check
# ---------------------------------------------------------------------------


def _assert_owner_or_admin(job: Job, current_user: Any) -> None:
    """Raise 403 unless *current_user* owns *job* or is an admin."""
    if job.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    body: CreateJobRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> dict[str, str]:
    """Create a new download job and dispatch it to the Kharej worker."""

    # 1. SSRF validation
    validate_job_url(body.url)

    # 2. Per-user rate limit
    await _check_rate_limit(current_user.id, session)

    # 3. Generate job_id and insert DB row
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        user_id=current_user.id,
        platform=body.platform.value,
        url=body.url,
        quality=body.quality,
        job_type=body.job_type,
        status="pending",
    )
    session.add(job)

    # 4. Build and send JobCreate over Rubika
    msg = JobCreate(
        v=1,
        type="job.create",
        ts=datetime.now(tz=timezone.utc),
        job_id=job_id,
        user_id=current_user.id,
        user_status="admin" if current_user.role == "admin" else "active",
        platform=body.platform,
        url=body.url,
        quality=body.quality,
        job_type=body.job_type,  # type: ignore[arg-type]
        format_hint=body.format_hint,
        collection_name=body.collection_name,
        total_tracks=body.total_tracks,
    )
    rubika_client = request.app.state.rubika_client
    try:
        await rubika_client.send(msg)
    except Exception as exc:
        logger.error(
            "Failed to send JobCreate to Rubika",
            extra={"job_id": job_id, "error": str(exc)},
        )
        # Still persist the job — it can be retried; do not block the response.

    # 5. Audit log
    session.add(
        AuditLog(
            actor_id=current_user.id,
            action="job.created",
            target_id=job_id,
            payload={
                "platform": body.platform.value,
                "url": body.url,
                "quality": body.quality,
                "job_type": body.job_type,
            },
            ip_addr=_get_client_ip(request),
        )
    )

    logger.info(
        "Job created",
        extra={"job_id": job_id, "user_id": current_user.id, "platform": body.platform.value},
    )
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# GET /jobs
# ---------------------------------------------------------------------------


@router.get("")
async def list_jobs(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a paginated list of the current user's jobs."""
    offset = (page - 1) * per_page
    result = await session.execute(
        select(Job)
        .where(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    jobs = result.scalars().all()
    count_result = await session.execute(
        select(func.count(Job.id)).where(Job.user_id == current_user.id)
    )
    total = count_result.scalar_one()
    return {
        "jobs": [_job_to_response(j) for j in jobs],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the current state of a single job."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _assert_owner_or_admin(job, current_user)
    return _job_to_response(job)


# ---------------------------------------------------------------------------
# DELETE /jobs/{id}
# ---------------------------------------------------------------------------


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> None:
    """Cancel a job: send JobCancel to Rubika and mark DB row as cancelled."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _assert_owner_or_admin(job, current_user)

    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already in terminal state: {job.status}.",
        )

    # Send JobCancel over Rubika
    msg = JobCancel(
        v=1,
        type="job.cancel",
        ts=datetime.now(tz=timezone.utc),
        job_id=job_id,
    )
    rubika_client = request.app.state.rubika_client
    try:
        await rubika_client.send(msg)
    except Exception as exc:
        logger.error(
            "Failed to send JobCancel to Rubika",
            extra={"job_id": job_id, "error": str(exc)},
        )

    # Update DB
    job.status = "cancelled"

    session.add(
        AuditLog(
            actor_id=current_user.id,
            action="job.cancelled",
            target_id=job_id,
            payload={"previous_status": job.status},
        )
    )

    logger.info("Job cancelled", extra={"job_id": job_id, "user_id": current_user.id})


# ---------------------------------------------------------------------------
# GET /jobs/{id}/events  (SSE)
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SSE_HEARTBEAT_INTERVAL = 15  # seconds


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events stream for a job.

    Immediately emits a terminal event when the job is already in a final
    state.  Otherwise fans out events from the :class:`~iran.event_bus.EventBus`
    with a keep-alive heartbeat every 15 seconds.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _assert_owner_or_admin(job, current_user)

    # Snapshot terminal state before entering the stream so that we can emit
    # the final event immediately if the job is already done.
    terminal_event: dict[str, Any] | None = None
    if job.status == "completed":
        terminal_event = {
            "type": "job.completed",
            "job_id": job_id,
            "parts": job.s2_keys or [],
            "metadata": job.metadata_json or {},
        }
    elif job.status == "failed":
        terminal_event = {
            "type": "job.failed",
            "job_id": job_id,
            "error_code": job.error_code,
            "message": ERROR_CODE_MESSAGES.get(job.error_code or "", job.error_msg or ""),
            "retryable": job.error_code not in ("blocked", "not_whitelisted", "invalid_url"),
        }
    elif job.status == "cancelled":
        terminal_event = {
            "type": "job.failed",
            "job_id": job_id,
            "error_code": "cancelled",
            "message": ERROR_CODE_MESSAGES["cancelled"],
            "retryable": False,
        }

    event_bus = request.app.state.event_bus

    async def _stream() -> Any:
        # If job already finished, emit terminal event and close immediately.
        if terminal_event is not None:
            event_type = terminal_event["type"]
            yield f"event: {event_type}\ndata: {json.dumps(terminal_event)}\n\n"
            return

        # Live stream via EventBus
        async with event_bus.subscribe(job_id) as queue:
            while True:
                # Wait for an event or heartbeat timeout
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # Keep-alive comment (not an event)
                    yield ": keep-alive\n\n"
                    continue

                # None sentinel means EventBus closed (server shutdown)
                if event is None:
                    break

                event_type = event.get("type", "message")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

                # Close stream after terminal events
                if event_type in ("job.completed", "job.failed"):
                    break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /jobs/{id}/download
# ---------------------------------------------------------------------------


@router.get("/{job_id}/download")
async def download_job(
    job_id: str,
    part: int | None = Query(default=None, ge=0),
    request: Request = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Any:
    """Return the download part list, or redirect to a presigned URL for part N.

    - Without ``?part``: returns ``{"parts": [...S2ObjectRef dicts...]}``
    - With ``?part=N``: 302-redirects to a presigned S2 URL for part N.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _assert_owner_or_admin(job, current_user)

    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job has not completed yet.",
        )

    parts: list[dict[str, Any]] = job.s2_keys or []

    if part is None:
        return {"parts": parts}

    if part >= len(parts):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Part {part} not found (job has {len(parts)} part(s)).",
        )

    s2_client = request.app.state.s2_client  # type: ignore[union-attr]
    key = parts[part]["key"]
    try:
        presigned_url = s2_client.generate_presigned_url(key)
    except Exception as exc:
        logger.error(
            "Failed to generate presigned URL",
            extra={"job_id": job_id, "key": key, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate download URL.",
        ) from exc

    return RedirectResponse(url=presigned_url, status_code=status.HTTP_302_FOUND)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Extract the client IP from request headers or connection info."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
