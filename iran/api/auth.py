"""Authentication endpoints for the Iran VPS service (Track B, Step 4).

Endpoints
---------
POST /auth/register   Public  — create user (status=pending_approval)
POST /auth/login      Public  — validate credentials, issue JWT + refresh cookie
POST /auth/refresh    Cookie  — rotate refresh token, re-issue access token
POST /auth/logout     JWT     — revoke the current refresh token

JWT Strategy
------------
- Access token: HS256, claims ``{sub, role, status, exp}``, 3-hour TTL.
- Refresh token: random 32-byte hex; SHA-256 hex digest stored in DB.
  Delivered as ``httpOnly; Secure; SameSite=Strict`` cookie named
  ``refresh_token``.

Rate Limiting
-------------
Login failures are counted per IP address in the ``audit_log`` table.
More than ``MAX_LOGIN_ATTEMPTS`` failures within ``RATE_LIMIT_WINDOW_MINUTES``
minutes results in a 429 response.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from iran.api.deps import get_current_user, get_db
from iran.db.models import AuditLog, RefreshToken, Registration, User

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
    pbkdf2_sha256__rounds=600000,
)


def hash_password(plain: str) -> str:
    """Return the pbkdf2_sha256 hash of *plain*."""
    try:
        return _pwd_context.hash(plain)
    except ValueError as exc:
        raise ValueError(f"Password hashing failed: {exc}") from exc


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` iff *plain* matches *hashed*."""
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"
_REFRESH_COOKIE = "refresh_token"
_ACCESS_COOKIE = "access_token"


def _secret_key() -> str:
    """Return the signing secret from settings (never empty in production)."""
    from iran.config import get_settings

    key = get_settings().SECRET_KEY
    if not key:
        if get_settings().ENVIRONMENT != "test" and not os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError("IRAN_SECRET_KEY must be set to a strong random value")
        key = os.environ.setdefault("_IRAN_JWT_FALLBACK_KEY", os.urandom(32).hex())
    return key


def set_access_token_cookie(response: Response, token: str) -> None:
    """Set a short-lived HttpOnly access-token cookie for browser clients."""
    from iran.config import get_settings

    response.set_cookie(
        key=_ACCESS_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def clear_access_token_cookie(response: Response) -> None:
    response.delete_cookie(key=_ACCESS_COOKIE, path="/")


def create_access_token(
    user_id: str,
    role: str,
    user_status: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Encode and return a signed HS256 JWT access token."""
    from jose import jwt

    from iran.config import get_settings

    if expires_delta is None:
        expires_delta = timedelta(minutes=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.now(tz=timezone.utc) + expires_delta
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "status": user_status,
        "exp": expire,
    }
    return jwt.encode(payload, _secret_key(), algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode *token* and return its claims, or ``None`` on any error."""
    from jose import JWTError, jwt

    try:
        return jwt.decode(token, _secret_key(), algorithms=[_ALGORITHM])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Refresh token helpers
# ---------------------------------------------------------------------------


def _new_raw_refresh_token() -> str:
    """Generate a cryptographically random 32-byte hex string."""
    return os.urandom(32).hex()


def _hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of *raw*."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def _create_refresh_token_record(
    session: AsyncSession,
    user_id: str,
    expire_days: int,
) -> str:
    """Insert a new ``RefreshToken`` row; return the raw (unhashed) token."""
    from iran.config import get_settings

    raw = _new_raw_refresh_token()
    expires_at = datetime.now(tz=timezone.utc) + timedelta(
        days=expire_days or get_settings().REFRESH_TOKEN_EXPIRE_DAYS
    )
    record = RefreshToken(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token=_hash_token(raw),
        expires_at=expires_at,
        revoked=False,
    )
    session.add(record)
    await session.flush()
    return raw


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------

MAX_LOGIN_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 15


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (for cross-dialect DB storage)."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


async def _check_rate_limit(session: AsyncSession, ip_addr: str) -> None:
    """Raise 429 if *ip_addr* has exceeded the login failure limit."""
    window_start = _utcnow() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    result = await session.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == "auth.login.failed",
            AuditLog.ip_addr == ip_addr,
            AuditLog.created_at >= window_start,
        )
    )
    failure_count: int = result.scalar_one()
    if failure_count >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many failed login attempts. "
                f"Try again in {RATE_LIMIT_WINDOW_MINUTES} minutes."
            ),
        )


async def _record_login_failure(
    session: AsyncSession, ip_addr: str, email: str
) -> None:
    """Append a ``auth.login.failed`` entry to ``audit_log``."""
    entry = AuditLog(
        id=str(uuid.uuid4()),
        actor_id=None,
        action="auth.login.failed",
        target_id=email,
        payload={"email": email, "ip": ip_addr},
        ip_addr=ip_addr,
        created_at=_utcnow(),
    )
    session.add(entry)
    await session.flush()


async def _check_registration_rate_limit(session: AsyncSession, ip_addr: str) -> None:
    window_start = _utcnow() - timedelta(minutes=60)
    result = await session.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == "auth.register",
            AuditLog.ip_addr == ip_addr,
            AuditLog.created_at >= window_start,
        )
    )
    if result.scalar_one() >= 5:
        raise HTTPException(status_code=429, detail="Too many registrations. Try again later.")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str
    password: str
    notes: str | None = None

    @field_validator("password")
    @classmethod
    def _password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("display_name")
    @classmethod
    def _display_name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("display_name must not be blank")
        return v


class RegisterResponse(BaseModel):
    user_id: str
    status: str
    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """Create a new user with ``status=pending_approval``.

    Inserts a matching ``registrations`` row and writes an ``audit_log`` entry.
    Returns 409 if the e-mail address is already registered.
    """
    # Check for duplicate e-mail
    existing = await session.execute(
        select(User).where(User.email == body.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email address already registered",
        )

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        email=body.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role="user",
        status="pending_approval",
    )
    session.add(user)
    await session.flush()

    # Pending-approval inbox entry
    registration = Registration(
        id=str(uuid.uuid4()),
        user_id=user_id,
        notes=body.notes,
    )
    session.add(registration)

    # Audit trail
    audit = AuditLog(
        id=str(uuid.uuid4()),
        actor_id=user_id,
        action="auth.register",
        target_id=user_id,
        payload={"email": body.email, "display_name": body.display_name},
        ip_addr=ip_addr,
    )
    session.add(audit)
    await session.flush()

    return RegisterResponse(
        user_id=user_id,
        status="pending_approval",
        message="Registration received. Await admin approval before logging in.",
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and receive an access token",
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Validate credentials and issue a JWT access token + refresh cookie.

    Raises:
    - 429 when the IP has exceeded the login failure rate limit.
    - 401 when credentials are invalid.
    - 401 when the account is not ``active``.
    """
    from iran.config import get_settings

    ip_addr = request.client.host if request.client else "unknown"

    # Rate-limit check first
    await _check_rate_limit(session, ip_addr)

    # Look up the user
    result = await session.execute(
        select(User).where(User.email == body.email)
    )
    user: User | None = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        # Record failure for rate-limiting; don't reveal which field is wrong
        await _record_login_failure(session, ip_addr, body.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is not active. Await admin approval.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Update last_seen_at
    user.last_seen_at = datetime.now(tz=timezone.utc)
    await session.flush()

    # Issue tokens
    settings = get_settings()
    access_token = create_access_token(user.id, user.role, user.status)
    set_access_token_cookie(response, access_token)
    raw_refresh = await _create_refresh_token_record(
        session, user.id, settings.REFRESH_TOKEN_EXPIRE_DAYS
    )

    # Set httpOnly refresh token cookie
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_refresh,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/auth",
    )

    # Audit
    session.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor_id=user.id,
            action="auth.login",
            target_id=user.id,
            payload={"ip": ip_addr},
            ip_addr=ip_addr,
        )
    )
    await session.flush()

    return TokenResponse(access_token=access_token)


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Rotate the refresh token and re-issue an access token",
)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    session: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Rotate the refresh token stored in the ``httpOnly`` cookie.

    Returns a new access token and sets a new ``refresh_token`` cookie.
    Raises 401 when the token is absent, expired, or revoked.
    """
    from iran.config import get_settings

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cookie not found",
        )

    token_hash = _hash_token(refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token == token_hash)
    )
    record: RefreshToken | None = result.scalar_one_or_none()

    now = datetime.now(tz=timezone.utc)
    if record is None or record.revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or has been revoked",
        )
    # Compare tz-aware datetimes safely
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired",
        )

    # Look up user
    user_result = await session.execute(
        select(User).where(User.id == record.user_id)
    )
    user: User | None = user_result.scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active",
        )

    # Rotate: revoke old, issue new
    record.revoked = True
    await session.flush()

    settings = get_settings()
    new_raw = await _create_refresh_token_record(
        session, user.id, settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    access_token = create_access_token(user.id, user.role, user.status)
    set_access_token_cookie(response, access_token)

    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=new_raw,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/auth",
    )

    return RefreshResponse(access_token=access_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Revoke the current refresh token",
)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Revoke the refresh token and clear the cookie.

    Requires a valid JWT access token.  Safe to call even if the cookie is
    absent (idempotent).
    """
    if refresh_token:
        token_hash = _hash_token(refresh_token)
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.token == token_hash,
                RefreshToken.user_id == current_user.id,
            )
        )
        record: RefreshToken | None = result.scalar_one_or_none()
        if record is not None:
            record.revoked = True
            await session.flush()

    # Clear the cookie
    response.delete_cookie(key=_REFRESH_COOKIE, path="/auth")
    clear_access_token_cookie(response)

    # Audit
    session.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor_id=current_user.id,
            action="auth.logout",
            target_id=current_user.id,
            payload={},
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


@router.get("/me", summary="Get current user's profile and job quota")
async def get_me(
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the authenticated user's profile and job-quota usage.

    ``quota_used`` and ``jobs_remaining`` are ``null`` when no quota is configured.
    """
    from iran.db.models import Job

    user = await session.get(User, current_user.id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    quota_used: int | None = None
    jobs_remaining: int | None = None

    if user.job_limit is not None:
        now = datetime.now(tz=timezone.utc)
        expires_at = user.job_limit_expires_at
        period_active = expires_at is None or (
            (expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at)
            >= now
        )
        if period_active:
            q = select(func.count(Job.id)).where(
                Job.user_id == user.id,
                Job.status.not_in(["failed", "cancelled"]),
            )
            start_at = user.job_limit_start_at
            if start_at is not None:
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                q = q.where(Job.created_at >= start_at)
            quota_used = (await session.execute(q)).scalar_one()
            jobs_remaining = max(0, user.job_limit - quota_used)

    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "job_limit": user.job_limit,
        "job_limit_expires_at": user.job_limit_expires_at.isoformat() if user.job_limit_expires_at else None,
        "quota_used": quota_used,
        "jobs_remaining": jobs_remaining,
    }
    ip_addr = request.client.host if request.client else "unknown"
    await _check_registration_rate_limit(session, ip_addr)
    body.email = body.email.lower()
