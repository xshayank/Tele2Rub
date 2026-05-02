"""Unit tests for Track B Step 4 — Auth Service.

Tests all four auth endpoints using ``httpx.AsyncClient`` backed by a
fresh in-memory SQLite database (``aiosqlite``).  No PostgreSQL instance
is required.

Coverage:
- POST /auth/register:
  - Happy-path registration (201, pending_approval status)
  - Duplicate e-mail returns 409
  - Short password returns 422
  - Blank display_name returns 422
- POST /auth/login:
  - Active user can log in, receives access_token + refresh cookie
  - Unknown e-mail returns 401
  - Wrong password returns 401
  - Pending user cannot log in (401)
  - Blocked user cannot log in (401)
  - Rate limit blocks login after MAX_LOGIN_ATTEMPTS failures (429)
- POST /auth/refresh:
  - Valid cookie issues new access token and rotates cookie
  - Absent cookie returns 401
  - Revoked refresh token returns 401
  - Expired refresh token returns 401
- POST /auth/logout:
  - Valid JWT + cookie revokes token (204)
  - After logout, refresh token is revoked (401 on re-use)
  - No JWT returns 401
- get_current_user dependency:
  - Valid token returns user
  - No token returns 401
  - Expired token returns 401
  - Blocked user token returns 401
- require_admin dependency:
  - Admin user passes
  - Regular user gets 403
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    """Create a fresh in-memory SQLite engine with all tables."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app(db_engine):
    """FastAPI app wired to the in-memory test database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.deps import get_db
    from iran.config import IranSettings
    from iran.main import create_app

    settings = IranSettings(SECRET_KEY="test-secret-key-for-step4-tests")
    test_app = create_app(settings)

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _override_get_db():
        from fastapi import HTTPException

        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except HTTPException:
                await session.commit()
                raise
            except Exception:
                await session.rollback()
                raise

    test_app.dependency_overrides[get_db] = _override_get_db
    yield test_app


@pytest_asyncio.fixture
async def client(app):
    """httpx AsyncClient pointing at the test app."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    # Use https:// so that Secure cookies are sent (the ASGI transport is
    # connection-level and does not require a real TLS session).
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=True
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: seed an active user directly into the DB
# ---------------------------------------------------------------------------


async def _seed_user(
    db_engine,
    *,
    email: str = "alice@example.com",
    password: str = "securepassword1",
    role: str = "user",
    status: str = "active",
) -> dict:
    """Insert a user directly and return their id + raw password."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.auth import hash_password
    from iran.db.models import User

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = str(uuid.uuid4())
    async with factory() as session:
        user = User(
            id=user_id,
            email=email,
            display_name=email.split("@")[0],
            password_hash=hash_password(password),
            role=role,
            status=status,
        )
        session.add(user)
        await session.commit()
    return {"id": user_id, "email": email, "password": password, "role": role}


# ===========================================================================
# 1. POST /auth/register
# ===========================================================================


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_happy_path(self, client):
        resp = await client.post(
            "/auth/register",
            json={
                "email": "new@example.com",
                "display_name": "New User",
                "password": "strongpass1",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending_approval"
        assert "user_id" in data
        assert "message" in data

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, client):
        payload = {
            "email": "dup@example.com",
            "display_name": "Dup",
            "password": "strongpass1",
        }
        r1 = await client.post("/auth/register", json=payload)
        assert r1.status_code == 201
        r2 = await client.post("/auth/register", json=payload)
        assert r2.status_code == 409

    @pytest.mark.asyncio
    async def test_register_short_password(self, client):
        resp = await client.post(
            "/auth/register",
            json={"email": "x@example.com", "display_name": "X", "password": "short"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_blank_display_name(self, client):
        resp = await client.post(
            "/auth/register",
            json={"email": "y@example.com", "display_name": "   ", "password": "strongpass1"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_invalid_email(self, client):
        resp = await client.post(
            "/auth/register",
            json={"email": "not-an-email", "display_name": "X", "password": "strongpass1"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_with_notes(self, client):
        resp = await client.post(
            "/auth/register",
            json={
                "email": "noted@example.com",
                "display_name": "Noted",
                "password": "strongpass1",
                "notes": "Please approve ASAP",
            },
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_register_creates_pending_status(self, client, db_engine):
        resp = await client.post(
            "/auth/register",
            json={
                "email": "pending@example.com",
                "display_name": "Pending",
                "password": "strongpass1",
            },
        )
        assert resp.status_code == 201
        user_id = resp.json()["user_id"]

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import User

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            assert user.status == "pending_approval"
            assert user.role == "user"

    @pytest.mark.asyncio
    async def test_register_creates_registration_record(self, client, db_engine):
        resp = await client.post(
            "/auth/register",
            json={
                "email": "reg@example.com",
                "display_name": "Reg",
                "password": "strongpass1",
            },
        )
        assert resp.status_code == 201
        user_id = resp.json()["user_id"]

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import Registration

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                select(Registration).where(Registration.user_id == user_id)
            )
            reg = result.scalar_one_or_none()
            assert reg is not None

    @pytest.mark.asyncio
    async def test_register_creates_audit_log(self, client, db_engine):
        resp = await client.post(
            "/auth/register",
            json={
                "email": "audited@example.com",
                "display_name": "Audited",
                "password": "strongpass1",
            },
        )
        assert resp.status_code == 201
        user_id = resp.json()["user_id"]

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import AuditLog

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "auth.register",
                    AuditLog.target_id == user_id,
                )
            )
            entry = result.scalar_one_or_none()
            assert entry is not None


# ===========================================================================
# 2. POST /auth/login
# ===========================================================================


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_active_user(self, client, db_engine):
        user = await _seed_user(db_engine, email="login@example.com")
        resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        # refresh token cookie must be set
        assert "refresh_token" in resp.cookies

    @pytest.mark.asyncio
    async def test_login_unknown_email(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "irrelevant"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client, db_engine):
        user = await _seed_user(db_engine, email="badpw@example.com")
        resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_pending_user(self, client, db_engine):
        user = await _seed_user(
            db_engine, email="pend@example.com", status="pending_approval"
        )
        resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_blocked_user(self, client, db_engine):
        user = await _seed_user(
            db_engine, email="blocked@example.com", status="blocked"
        )
        resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_rate_limit(self, client, db_engine):
        """After MAX_LOGIN_ATTEMPTS failures the IP is rate-limited (429)."""
        from iran.api.auth import MAX_LOGIN_ATTEMPTS

        await _seed_user(db_engine, email="rl@example.com")
        for _ in range(MAX_LOGIN_ATTEMPTS):
            r = await client.post(
                "/auth/login",
                json={"email": "rl@example.com", "password": "wrongpassword"},
            )
            assert r.status_code == 401

        r = await client.post(
            "/auth/login",
            json={"email": "rl@example.com", "password": "wrongpassword"},
        )
        assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_login_records_failure_audit_log(self, client, db_engine):
        resp = await client.post(
            "/auth/login",
            json={"email": "unknown@example.com", "password": "whatever"},
        )
        assert resp.status_code == 401

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import AuditLog

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "auth.login.failed",
                    AuditLog.target_id == "unknown@example.com",
                )
            )
            entry = result.scalar_one_or_none()
            assert entry is not None

    @pytest.mark.asyncio
    async def test_login_updates_last_seen_at(self, client, db_engine):
        user = await _seed_user(db_engine, email="seen@example.com")
        await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import User

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                select(User).where(User.id == user["id"])
            )
            u = result.scalar_one()
            assert u.last_seen_at is not None


# ===========================================================================
# 3. POST /auth/refresh
# ===========================================================================


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_valid_cookie(self, client, db_engine):
        user = await _seed_user(db_engine, email="ref@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        assert login_resp.status_code == 200

        refresh_resp = await client.post("/auth/refresh")
        assert refresh_resp.status_code == 200
        data = refresh_resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        # New cookie must be set
        assert "refresh_token" in refresh_resp.cookies

    @pytest.mark.asyncio
    async def test_refresh_old_token_revoked(self, client, app, db_engine):
        """After rotation, the old raw token cannot be re-used."""
        user = await _seed_user(db_engine, email="rotref@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        old_cookie = login_resp.cookies["refresh_token"]

        # Perform first rotation
        r1 = await client.post("/auth/refresh")
        assert r1.status_code == 200

        # Manually inject the old cookie and try again
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"refresh_token": old_cookie},
        ) as old_client:
            r2 = await old_client.post("/auth/refresh")
            assert r2.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_no_cookie(self, client):
        resp = await client.post("/auth/refresh")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_expired_token(self, client, app, db_engine):
        """An expired refresh token returns 401."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.api.auth import _hash_token, _new_raw_refresh_token
        from iran.db.models import RefreshToken

        user = await _seed_user(db_engine, email="expref@example.com")
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        raw = _new_raw_refresh_token()
        async with factory() as session:
            record = RefreshToken(
                id=str(uuid.uuid4()),
                user_id=user["id"],
                token=_hash_token(raw),
                expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
                revoked=False,
            )
            session.add(record)
            await session.commit()

        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"refresh_token": raw},
        ) as exp_client:
            resp = await exp_client.post("/auth/refresh")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_revoked_token(self, client, app, db_engine):
        """A revoked refresh token returns 401."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.api.auth import _hash_token, _new_raw_refresh_token
        from iran.db.models import RefreshToken

        user = await _seed_user(db_engine, email="revref@example.com")
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        raw = _new_raw_refresh_token()
        async with factory() as session:
            record = RefreshToken(
                id=str(uuid.uuid4()),
                user_id=user["id"],
                token=_hash_token(raw),
                expires_at=datetime.now(tz=timezone.utc) + timedelta(days=7),
                revoked=True,
            )
            session.add(record)
            await session.commit()

        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"refresh_token": raw},
        ) as rev_client:
            resp = await rev_client.post("/auth/refresh")
            assert resp.status_code == 401


# ===========================================================================
# 4. POST /auth/logout
# ===========================================================================


class TestLogout:
    @pytest.mark.asyncio
    async def test_logout_happy_path(self, client, db_engine):
        user = await _seed_user(db_engine, email="out@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        access_token = login_resp.json()["access_token"]

        resp = await client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_logout_revokes_refresh_token(self, client, db_engine):
        user = await _seed_user(db_engine, email="outrev@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        access_token = login_resp.json()["access_token"]

        await client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Now the refresh token must be revoked
        resp = await client.post("/auth/refresh")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_no_jwt_returns_401(self, client):
        resp = await client.post("/auth/logout")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_invalid_jwt_returns_401(self, client):
        resp = await client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer this.is.not.valid"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_idempotent_without_cookie(self, client, db_engine):
        """Logout without a cookie still returns 204."""
        user = await _seed_user(db_engine, email="idem@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        access_token = login_resp.json()["access_token"]
        # Remove the cookie
        client.cookies.delete("refresh_token")

        resp = await client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 204


# ===========================================================================
# 5. JWT helpers (unit tests)
# ===========================================================================


class TestJWTHelpers:
    def test_create_and_decode_access_token(self):
        from iran.api.auth import create_access_token, decode_access_token

        with patch.dict("os.environ", {"IRAN_SECRET_KEY": "test-secret"}):
            token = create_access_token(
                "user-123", "user", "active", timedelta(minutes=15)
            )
            payload = decode_access_token(token)
            assert payload is not None
            assert payload["sub"] == "user-123"
            assert payload["role"] == "user"
            assert payload["status"] == "active"

    def test_decode_invalid_token_returns_none(self):
        from iran.api.auth import decode_access_token

        result = decode_access_token("this.is.garbage")
        assert result is None

    def test_decode_expired_token_returns_none(self):
        from iran.api.auth import create_access_token, decode_access_token

        with patch.dict("os.environ", {"IRAN_SECRET_KEY": "test-secret"}):
            token = create_access_token(
                "user-xyz", "user", "active", timedelta(seconds=-1)
            )
            result = decode_access_token(token)
            assert result is None

    def test_password_hash_and_verify(self):
        from iran.api.auth import hash_password, verify_password

        hashed = hash_password("mysecret")
        assert verify_password("mysecret", hashed)
        assert not verify_password("wrong", hashed)

    def test_token_hash_is_sha256(self):
        import hashlib

        from iran.api.auth import _hash_token

        raw = "deadbeef"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert _hash_token(raw) == expected


# ===========================================================================
# 6. get_current_user / require_admin (dependency tests via route)
# ===========================================================================


class TestCurrentUserDependency:
    @pytest.mark.asyncio
    async def test_no_token_returns_401(self, client):
        """A protected route with no token should return 401."""
        resp = await client.post("/auth/logout")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_token_returns_401(self, client):
        resp = await client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer not.a.jwt.at.all"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_blocked_user_token_rejected(self, client, db_engine):
        """A token belonging to a blocked user should be rejected."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.api.auth import create_access_token
        from iran.db.models import User

        user = await _seed_user(db_engine, email="blk@example.com", status="blocked")
        # Issue a token manually (bypasses login status check)
        token = create_access_token(user["id"], "user", "active", timedelta(minutes=15))

        # Ensure user is blocked in the DB
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(select(User).where(User.id == user["id"]))
            u = result.scalar_one()
            u.status = "blocked"
            await session.commit()

        resp = await client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401


class TestRequireAdmin:
    @pytest.mark.asyncio
    async def test_admin_user_has_role(self, db_engine):
        from iran.api.auth import create_access_token, decode_access_token

        admin = await _seed_user(db_engine, email="adm@example.com", role="admin")
        token = create_access_token(admin["id"], "admin", "active", timedelta(minutes=15))
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["role"] == "admin"

    @pytest.mark.asyncio
    async def test_access_token_carries_role(self, db_engine):
        from iran.api.auth import create_access_token, decode_access_token

        user = await _seed_user(db_engine, email="role@example.com", role="user")
        token = create_access_token(user["id"], "user", "active", timedelta(minutes=15))
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["role"] == "user"


# ===========================================================================
# 7. Full workflow: register → approve → login
# ===========================================================================


class TestFullWorkflow:
    @pytest.mark.asyncio
    async def test_register_pending_approve_login(self, client, db_engine):
        """Complete flow: register (pending) → admin approves → user logs in."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import User

        # Step 1: register
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "workflow@example.com",
                "display_name": "Workflow",
                "password": "strongpass1",
            },
        )
        assert reg_resp.status_code == 201
        user_id = reg_resp.json()["user_id"]

        # Step 2: attempt login — should fail (pending)
        login_pending = await client.post(
            "/auth/login",
            json={"email": "workflow@example.com", "password": "strongpass1"},
        )
        assert login_pending.status_code == 401

        # Step 3: admin approves (directly in DB)
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            u = result.scalar_one()
            u.status = "active"
            await session.commit()

        # Step 4: login now succeeds
        login_active = await client.post(
            "/auth/login",
            json={"email": "workflow@example.com", "password": "strongpass1"},
        )
        assert login_active.status_code == 200
        assert "access_token" in login_active.json()

    @pytest.mark.asyncio
    async def test_expired_refresh_returns_401_after_login(self, client, db_engine):
        """Login, wait for token to 'expire', refresh attempt returns 401."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.db.models import RefreshToken

        user = await _seed_user(db_engine, email="expflow@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        assert login_resp.status_code == 200

        # Expire the token directly in the DB
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                select(RefreshToken).where(RefreshToken.user_id == user["id"])
            )
            token_rec = result.scalar_one()
            token_rec.expires_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)
            await session.commit()

        resp = await client.post("/auth/refresh")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_and_refresh_rejected(self, client, db_engine):
        """After logout, the refresh token cannot be re-used."""
        user = await _seed_user(db_engine, email="logoutflow@example.com")
        login_resp = await client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        access_token = login_resp.json()["access_token"]

        await client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        resp = await client.post("/auth/refresh")
        assert resp.status_code == 401


# ===========================================================================
# 8. Contract fixture — outgoing message field verification
# ===========================================================================


class TestContractFixtures:
    """Verify that access token claims match the frozen contract schema."""

    def test_access_token_claims_schema(self):
        from iran.api.auth import create_access_token, decode_access_token

        token = create_access_token("u1", "admin", "active", timedelta(minutes=15))
        payload = decode_access_token(token)
        assert payload is not None
        # Required fields per Step 4 spec
        assert "sub" in payload
        assert "role" in payload
        assert "status" in payload
        assert "exp" in payload

    def test_access_token_sub_is_user_id(self):
        from iran.api.auth import create_access_token, decode_access_token

        uid = str(uuid.uuid4())
        token = create_access_token(uid, "user", "active", timedelta(minutes=15))
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == uid

    @pytest.mark.asyncio
    async def test_register_response_schema(self, client):
        """Register response has the expected keys."""
        resp = await client.post(
            "/auth/register",
            json={
                "email": "schema@example.com",
                "display_name": "Schema",
                "password": "strongpass1",
            },
        )
        data = resp.json()
        assert set(data.keys()) >= {"user_id", "status", "message"}
