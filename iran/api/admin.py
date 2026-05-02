"""Admin / Control-Plane API endpoints for the Iran VPS service (Track B, Step 9).

Endpoints
---------
GET    /admin/users                  Paginated user list
PATCH  /admin/users/{id}             Approve / block / delete a user
GET    /admin/registrations          Pending registration queue
PATCH  /admin/registrations/{id}     Approve or reject a registration
GET    /admin/jobs                   All jobs (paginated, filtered)
DELETE /admin/jobs/{id}              Force-cancel any job
GET    /admin/storage                S2 usage summary
DELETE /admin/storage/{job_id}       Delete all S2 objects for a job
GET    /admin/settings               Read all settings
PATCH  /admin/settings               Update settings (sends AdminSettingsUpdate)
POST   /admin/settings/clearcache    Send AdminClearcache to Kharej
POST   /admin/settings/cookies       Upload cookies.txt → S2 + send AdminCookiesUpdate
GET    /admin/health                 Cached HealthPong data from DB
POST   /admin/health/ping            Send HealthPing; wait ≤ 10 s for HealthPong
GET    /admin/audit                  Paginated audit log
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from iran.api.deps import get_db
from iran.contracts import (
    AdminClearcache,
    AdminCookiesUpdate,
    AdminSettingsUpdate,
    HealthPing,
    JobCancel,
    UserBlockAdd,
    UserWhitelistAdd,
    UserWhitelistRemove,
)
from iran.db.models import AuditLog, Job, Registration, Setting, User

logger = logging.getLogger("iran.api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Auth dependency — always returns 403 for any auth failure (security practice)
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


async def _require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Admin auth dependency — raises 403 for unauthenticated OR non-admin users."""
    _403 = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin privileges required",
    )
    if credentials is None:
        raise _403

    from iran.api.auth import decode_access_token

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise _403

    user_id: str = payload["sub"]
    result = await session.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()
    if user is None or user.status not in ("active",) or user.role != "admin":
        raise _403
    return user


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PatchUserRequest(BaseModel):
    action: str  # "approve" | "block" | "delete" | "unblock"
    reason: str | None = None


class PatchRegistrationRequest(BaseModel):
    action: str  # "approve" | "reject"
    notes: str | None = None


class PatchSettingsRequest(BaseModel):
    settings: dict[str, Any]


class ClearcacheRequest(BaseModel):
    target: str = "all"  # "lru" | "isrc" | "all"


# ---------------------------------------------------------------------------
# Helper: send message via rubika_client on app.state
# ---------------------------------------------------------------------------


def _rubika(request: Request) -> Any:
    """Return the rubika_client from app.state (raises 503 if missing)."""
    client = getattr(request.app.state, "rubika_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Rubika client not available")
    return client


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    role: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return a paginated list of registered users."""
    q = select(User)
    if status_filter:
        q = q.where(User.status == status_filter)
    if role:
        q = q.where(User.role == role)
    total_result = await session.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    q = q.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    rows = (await session.execute(q)).scalars().all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "users": [_user_dict(u) for u in rows],
    }


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
    }


# ---------------------------------------------------------------------------
# PATCH /admin/users/{id}
# ---------------------------------------------------------------------------


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    body: PatchUserRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Approve, block, unblock, or delete a user."""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    rubika = _rubika(request)
    now = datetime.now(tz=timezone.utc)
    action = body.action

    if action == "approve":
        user.status = "active"
        msg = UserWhitelistAdd(
            ts=now,
            job_id=None,
            user_id=user_id,
            display_name=user.display_name,
        )
        await rubika.send(msg)
        _audit(session, admin.id, "admin.user.approve", user_id)

    elif action == "block":
        user.status = "blocked"
        msg = UserBlockAdd(
            ts=now,
            job_id=None,
            user_id=user_id,
            reason=body.reason,
        )
        await rubika.send(msg)
        _audit(session, admin.id, "admin.user.block", user_id)

    elif action == "unblock":
        user.status = "active"
        msg = UserWhitelistAdd(
            ts=now,
            job_id=None,
            user_id=user_id,
            display_name=user.display_name,
        )
        await rubika.send(msg)
        _audit(session, admin.id, "admin.user.unblock", user_id)

    elif action == "delete":
        was_active = user.status == "active"
        user.status = "deleted"
        if was_active:
            msg = UserWhitelistRemove(ts=now, job_id=None, user_id=user_id)
            await rubika.send(msg)
        _audit(session, admin.id, "admin.user.delete", user_id)

    else:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action!r}")

    return {"status": "ok", "user_id": user_id, "new_status": user.status}


# ---------------------------------------------------------------------------
# GET /admin/registrations
# ---------------------------------------------------------------------------


@router.get("/registrations")
async def list_registrations(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return the pending registration queue (users awaiting admin review)."""
    q = (
        select(Registration, User)
        .join(User, User.id == Registration.user_id)
        .where(User.status == "pending_approval")
        .where(Registration.reviewed_at.is_(None))
    )
    total_result = await session.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    q = q.order_by(Registration.id).offset((page - 1) * per_page).limit(per_page)
    rows = (await session.execute(q)).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "registrations": [
            {
                "id": reg.id,
                "user_id": reg.user_id,
                "email": user.email,
                "display_name": user.display_name,
                "notes": reg.notes,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            }
            for reg, user in rows
        ],
    }


# ---------------------------------------------------------------------------
# PATCH /admin/registrations/{id}
# ---------------------------------------------------------------------------


@router.patch("/registrations/{reg_id}")
async def patch_registration(
    reg_id: str,
    body: PatchRegistrationRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Approve or reject a registration."""
    reg = await session.get(Registration, reg_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Registration not found")

    user = await session.get(User, reg.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Associated user not found")

    now = datetime.now(tz=timezone.utc)
    reg.reviewed_by = admin.id
    reg.reviewed_at = now
    if body.notes:
        reg.notes = body.notes

    action = body.action
    if action == "approve":
        user.status = "active"
        rubika = _rubika(request)
        msg = UserWhitelistAdd(
            ts=now,
            job_id=None,
            user_id=user.id,
            display_name=user.display_name,
        )
        await rubika.send(msg)
        _audit(session, admin.id, "admin.registration.approve", reg_id)

    elif action == "reject":
        user.status = "deleted"
        _audit(session, admin.id, "admin.registration.reject", reg_id)

    else:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action!r}")

    return {
        "status": "ok",
        "registration_id": reg_id,
        "user_id": user.id,
        "action": action,
    }


# ---------------------------------------------------------------------------
# GET /admin/jobs
# ---------------------------------------------------------------------------


@router.get("/jobs")
async def list_all_jobs(
    status_filter: str | None = Query(None, alias="status"),
    platform: str | None = Query(None),
    user_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return all jobs with pagination and optional filters."""
    q = select(Job)
    if status_filter:
        q = q.where(Job.status == status_filter)
    if platform:
        q = q.where(Job.platform == platform)
    if user_id:
        q = q.where(Job.user_id == user_id)
    total_result = await session.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    q = q.order_by(Job.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    rows = (await session.execute(q)).scalars().all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "jobs": [_job_dict(j) for j in rows],
    }


def _job_dict(j: Job) -> dict:
    return {
        "job_id": j.id,
        "user_id": j.user_id,
        "platform": j.platform,
        "status": j.status,
        "progress": j.progress,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


# ---------------------------------------------------------------------------
# DELETE /admin/jobs/{id}
# ---------------------------------------------------------------------------


@router.delete("/jobs/{job_id}", status_code=204, response_class=Response, response_model=None)
async def force_cancel_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> None:
    """Force-cancel any job regardless of owner."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="Job is already in a terminal state")

    rubika = _rubika(request)
    msg = JobCancel(ts=datetime.now(tz=timezone.utc), job_id=job_id)
    await rubika.send(msg)
    job.status = "cancelled"
    _audit(session, admin.id, "admin.job.cancel", job_id)


# ---------------------------------------------------------------------------
# GET /admin/storage
# ---------------------------------------------------------------------------


@router.get("/storage")
async def get_storage(
    request: Request,
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return S2 storage usage summary under ``media/`` and ``thumbs/``."""
    s2 = getattr(request.app.state, "s2_client", None)
    if s2 is None:
        raise HTTPException(status_code=503, detail="S2 client not available")

    try:
        media_objects = await s2.list_job_objects("")
    except Exception as exc:
        logger.warning("S2 list_job_objects failed: %s", exc)
        media_objects = []

    total_objects = len(media_objects)
    total_bytes = sum(o.get("size", 0) for o in media_objects)

    return {
        "total_objects": total_objects,
        "total_bytes": total_bytes,
        "objects": media_objects,
    }


# ---------------------------------------------------------------------------
# DELETE /admin/storage/{job_id}
# ---------------------------------------------------------------------------


@router.delete("/storage/{job_id}", status_code=204, response_class=Response, response_model=None)
async def delete_storage(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> None:
    """Delete all S2 objects for a job."""
    s2 = getattr(request.app.state, "s2_client", None)
    if s2 is None:
        raise HTTPException(status_code=503, detail="S2 client not available")

    try:
        objects = await s2.list_job_objects(job_id)
    except Exception as exc:
        logger.warning("S2 list_job_objects failed for job %s: %s", job_id, exc)
        objects = []

    delete_count = 0
    if objects:
        delete_fn = getattr(s2, "delete_job_objects", None)
        if delete_fn is not None:
            try:
                await delete_fn(job_id)
                delete_count = len(objects)
            except Exception as exc:
                logger.warning("S2 delete_job_objects failed for job %s: %s", job_id, exc)
        else:
            logger.info("S2 client does not support delete; skipping for job %s", job_id)

    _audit(session, admin.id, "admin.storage.delete", job_id, payload={"deleted": delete_count})


# ---------------------------------------------------------------------------
# GET /admin/settings
# ---------------------------------------------------------------------------


@router.get("/settings")
async def get_settings(
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return all key/value settings from the DB."""
    rows = (await session.execute(select(Setting))).scalars().all()
    return {
        "settings": {
            r.key: {
                "value": r.value,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        }
    }


# ---------------------------------------------------------------------------
# PATCH /admin/settings
# ---------------------------------------------------------------------------


@router.patch("/settings")
async def update_settings(
    body: PatchSettingsRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Persist settings to DB and send ``AdminSettingsUpdate`` to the Kharej Worker."""
    now = datetime.now(tz=timezone.utc)
    for key, value in body.settings.items():
        str_value = str(value)
        existing = await session.get(Setting, key)
        if existing is None:
            session.add(Setting(key=key, value=str_value, updated_at=now))
        else:
            existing.value = str_value
            existing.updated_at = now

    rubika = _rubika(request)
    msg = AdminSettingsUpdate(ts=now, job_id=None, settings=body.settings)
    await rubika.send(msg)
    _audit(session, admin.id, "admin.settings.update", None, payload={"settings": body.settings})

    return {"status": "ok", "sent": True}


# ---------------------------------------------------------------------------
# POST /admin/settings/clearcache
# ---------------------------------------------------------------------------


@router.post("/settings/clearcache")
async def clearcache(
    request: Request,
    body: ClearcacheRequest = ClearcacheRequest(),
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Send ``AdminClearcache`` to the Kharej Worker."""
    target = body.target or "all"
    if target not in ("lru", "isrc", "all"):
        raise HTTPException(status_code=422, detail=f"Invalid target: {target!r}")

    rubika = _rubika(request)
    msg = AdminClearcache(ts=datetime.now(tz=timezone.utc), job_id=None, target=target)
    await rubika.send(msg)
    _audit(session, admin.id, "admin.clearcache", None, payload={"target": target})
    return {"status": "ok", "target": target}


# ---------------------------------------------------------------------------
# POST /admin/settings/cookies
# ---------------------------------------------------------------------------


@router.post("/settings/cookies")
async def upload_cookies(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Upload a new ``cookies.txt`` to S2 ``tmp/`` and send ``AdminCookiesUpdate``."""
    s2 = getattr(request.app.state, "s2_client", None)
    if s2 is None:
        raise HTTPException(status_code=503, detail="S2 client not available")

    content = await file.read()
    sha256 = hashlib.sha256(content).hexdigest()
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    s2_key = f"tmp/cookies-{date_str}/cookies.txt"

    upload_fn = getattr(s2, "put_object", None)
    if upload_fn is not None:
        try:
            await upload_fn(s2_key, content)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"S2 upload failed: {exc}") from exc

    rubika = _rubika(request)
    msg = AdminCookiesUpdate(
        ts=datetime.now(tz=timezone.utc),
        job_id=None,
        s2_key=s2_key,
        sha256=sha256,
    )
    await rubika.send(msg)
    _audit(
        session,
        admin.id,
        "admin.cookies.update",
        None,
        payload={"s2_key": s2_key, "sha256": sha256},
    )
    return {"status": "ok", "s2_key": s2_key, "sha256": sha256}


# ---------------------------------------------------------------------------
# GET /admin/health
# ---------------------------------------------------------------------------


@router.get("/health")
async def get_health(
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return the most recently cached ``HealthPong`` data from the settings table."""
    row = await session.get(Setting, "last_health_pong")
    if row is None:
        return {"status": "no_data", "pong": None}
    try:
        pong = json.loads(row.value)
    except Exception:
        pong = row.value
    return {"status": "ok", "pong": pong}


# ---------------------------------------------------------------------------
# POST /admin/health/ping
# ---------------------------------------------------------------------------


@router.post("/health/ping")
async def health_ping(
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict:
    """Send ``HealthPing``; wait up to 10 s for the ``HealthPong`` response."""
    request_id = f"ping-{uuid.uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Register a pending-ping event so on_health_pong can signal us.
    pending_pings: dict[str, asyncio.Event] = getattr(
        request.app.state, "pending_pings", {}
    )
    event = asyncio.Event()
    pending_pings[request_id] = event

    rubika = _rubika(request)
    msg = HealthPing(ts=now, job_id=None, request_id=request_id)
    try:
        await rubika.send(msg)
    except Exception as exc:
        pending_pings.pop(request_id, None)
        raise HTTPException(status_code=502, detail=f"Failed to send ping: {exc}") from exc

    _audit(session, admin.id, "admin.health.ping", None, payload={"request_id": request_id})

    try:
        await asyncio.wait_for(event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        pending_pings.pop(request_id, None)
        return {"status": "timeout", "request_id": request_id, "pong": None}

    pending_pings.pop(request_id, None)

    # Read the stored pong from settings
    pong_row = await session.get(Setting, "last_health_pong")
    pong_data = None
    if pong_row:
        try:
            pong_data = json.loads(pong_row.value)
        except Exception:
            pong_data = pong_row.value

    return {"status": "ok", "request_id": request_id, "pong": pong_data}


# ---------------------------------------------------------------------------
# GET /admin/audit
# ---------------------------------------------------------------------------


@router.get("/audit")
async def get_audit(
    actor_id: str | None = Query(None),
    action: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict:
    """Return a paginated audit log with optional filters."""
    q = select(AuditLog)
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if action:
        q = q.where(AuditLog.action == action)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'since' date format")
        q = q.where(AuditLog.created_at >= since_dt)
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'until' date format")
        q = q.where(AuditLog.created_at <= until_dt)

    total_result = await session.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    q = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    rows = (await session.execute(q)).scalars().all()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "entries": [
            {
                "id": e.id,
                "actor_id": e.actor_id,
                "action": e.action,
                "target_id": e.target_id,
                "payload": e.payload,
                "ip_addr": e.ip_addr,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _audit(
    session: AsyncSession,
    actor_id: str | None,
    action: str,
    target_id: str | None,
    payload: dict | None = None,
) -> None:
    """Append an audit log entry (does not flush; relies on session commit)."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_id=target_id,
        payload=payload,
    )
    session.add(entry)
