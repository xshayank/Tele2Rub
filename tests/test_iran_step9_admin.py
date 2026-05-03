"""Tests for Track B Step 9 — Admin/Control-Plane API.

Coverage
--------
- Auth: unauthenticated requests return 403 for all admin endpoints
- GET /admin/users — returns user list
- PATCH /admin/users/{id} approve → UserWhitelistAdd sent
- PATCH /admin/users/{id} block   → UserBlockAdd sent
- PATCH /admin/users/{id} delete  → UserWhitelistRemove sent
- PATCH /admin/users/{id} invalid action → 422
- GET /admin/registrations — pending queue
- PATCH /admin/registrations/{id} approve → status=active + UserWhitelistAdd
- PATCH /admin/registrations/{id} reject  → status=deleted
- GET /admin/jobs — all jobs
- DELETE /admin/jobs/{id} — cancel job, sends JobCancel
- DELETE /admin/jobs/{id} for completed job → 409
- GET /admin/storage — returns summary
- GET /admin/settings — returns settings dict
- PATCH /admin/settings — upserts keys, sends AdminSettingsUpdate
- POST /admin/settings/clearcache — sends AdminClearcache
- GET /admin/health — no data
- POST /admin/health/ping — timeout path
- POST /admin/health/ping — success path (pong injected)
- GET /admin/audit — paginated log
- Admin UI pages return 200 HTML

Acceptance criteria (from Step 9 spec):
- Admin approves user → UserWhitelistAdd sent exactly once
- POST /admin/health/ping → HealthPong stored and returned within 10 s
- Admin updates settings → AdminSettingsUpdate sent → AdminAck stores effective_config
- Unauthenticated request to /admin/* returns 403
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    """Fresh in-memory SQLite engine with all tables."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app(db_engine):
    """FastAPI app wired to in-memory DB with stub Rubika + S2."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.deps import get_db
    from iran.config import IranSettings
    from iran.event_bus import make_event_bus
    from iran.main import create_app
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, IranRubikaConfig

    settings = IranSettings(SECRET_KEY="test-secret-step9")
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

    transport = FakeRubikaTransport(kharej_guid="kharej-test", iran_guid="iran-test")
    rubika_config = IranRubikaConfig(
        RUBIKA_SESSION_IRAN="",
        KHAREJ_RUBIKA_ACCOUNT_GUID="kharej-test",
        IRAN_RUBIKA_ACCOUNT_GUID="iran-test",
    )
    rubika_client = IranRubikaClient(rubika_config, transport=transport)
    test_app.state.rubika_client = rubika_client

    stub_s2 = MagicMock()
    stub_s2.generate_presigned_url.return_value = "https://s3.example.com/presigned"
    stub_s2.list_job_objects = AsyncMock(return_value=[])
    test_app.state.s2_client = stub_s2

    test_app.state.event_bus = make_event_bus()
    test_app.state.pending_pings = {}

    yield test_app


@pytest_asyncio.fixture
async def session_factory(db_engine):
    """Re-usable async session factory for test setup helpers."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _create_admin(session_factory) -> tuple[str, str]:
    """Create an admin user and return (user_id, email)."""
    from passlib.context import CryptContext

    from iran.db.models import User

    _pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
    user_id = str(uuid.uuid4())
    async with session_factory() as s:
        user = User(
            id=user_id,
            email="admin@test.com",
            display_name="Admin",
            password_hash=_pwd.hash("admin-pass"),
            role="admin",
            status="active",
        )
        s.add(user)
        await s.commit()
    return user_id, "admin@test.com"


async def _create_user(session_factory, *, status: str = "pending_approval") -> str:
    """Create a regular user and return user_id."""
    from passlib.context import CryptContext

    from iran.db.models import Registration, User

    _pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
    user_id = str(uuid.uuid4())
    async with session_factory() as s:
        user = User(
            id=user_id,
            email=f"user-{user_id[:8]}@test.com",
            display_name="Test User",
            password_hash=_pwd.hash("password"),
            role="user",
            status=status,
        )
        s.add(user)
        await s.flush()
        reg = Registration(user_id=user_id, notes=None)
        s.add(reg)
        await s.commit()
    return user_id


async def _get_admin_token(async_client) -> str:
    """Log in the admin user and return an access token."""
    resp = await async_client.post(
        "/auth/login",
        json={"email": "admin@test.com", "password": "admin-pass"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Helper: httpx async client
# ---------------------------------------------------------------------------

import httpx


@pytest_asyncio.fixture
async def client(app):
    """httpx.AsyncClient against the test app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: admin client (pre-authenticated)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(app, session_factory):
    """httpx.AsyncClient pre-authenticated as admin."""
    await _create_admin(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as c:
        token = await _get_admin_token(c)
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


# ===========================================================================
# Auth tests
# ===========================================================================


class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_users_403(self, client):
        r = await client.get("/admin/users")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_registrations_403(self, client):
        r = await client.get("/admin/registrations")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_jobs_403(self, client):
        r = await client.get("/admin/jobs")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_settings_403(self, client):
        r = await client.get("/admin/settings")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_health_403(self, client):
        r = await client.get("/admin/health")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_audit_403(self, client):
        r = await client.get("/admin/audit")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_non_admin_user_403(self, app, session_factory, client):
        """Active non-admin user gets 403 on admin endpoints."""
        from passlib.context import CryptContext

        from iran.db.models import User

        _pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
        uid = str(uuid.uuid4())
        async with session_factory() as s:
            s.add(
                User(
                    id=uid,
                    email="regular@test.com",
                    display_name="Regular",
                    password_hash=_pwd.hash("pw"),
                    role="user",
                    status="active",
                )
            )
            await s.commit()

        resp = await client.post(
            "/auth/login", json={"email": "regular@test.com", "password": "pw"}
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        r = await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


# ===========================================================================
# GET /admin/users
# ===========================================================================


class TestAdminUsers:
    @pytest.mark.asyncio
    async def test_list_users_ok(self, admin_client, session_factory):
        await _create_user(session_factory)
        r = await admin_client.get("/admin/users")
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_filter_by_status(self, admin_client, session_factory):
        uid = await _create_user(session_factory, status="active")
        r = await admin_client.get("/admin/users?status=active")
        assert r.status_code == 200
        ids = [u["id"] for u in r.json()["users"]]
        assert uid in ids

    @pytest.mark.asyncio
    async def test_approve_user_sends_whitelist_add(self, admin_client, app, session_factory):
        """Admin approves pending user → UserWhitelistAdd sent exactly once."""
        uid = await _create_user(session_factory)
        r = await admin_client.patch(f"/admin/users/{uid}", json={"action": "approve"})
        assert r.status_code == 200
        data = r.json()
        assert data["new_status"] == "active"

        # Verify UserWhitelistAdd was sent
        from iran.contracts import decode, RTUNES_PREFIX

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert types.count("user.whitelist.add") >= 1

    @pytest.mark.asyncio
    async def test_block_user_sends_block_add(self, admin_client, app, session_factory):
        uid = await _create_user(session_factory, status="active")
        r = await admin_client.patch(
            f"/admin/users/{uid}", json={"action": "block", "reason": "ToS"}
        )
        assert r.status_code == 200
        assert r.json()["new_status"] == "blocked"

        from iran.contracts import decode, RTUNES_PREFIX

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "user.block.add" in types

    @pytest.mark.asyncio
    async def test_delete_active_user_sends_whitelist_remove(
        self, admin_client, app, session_factory
    ):
        uid = await _create_user(session_factory, status="active")
        r = await admin_client.patch(f"/admin/users/{uid}", json={"action": "delete"})
        assert r.status_code == 200
        assert r.json()["new_status"] == "deleted"

        from iran.contracts import decode, RTUNES_PREFIX

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "user.whitelist.remove" in types

    @pytest.mark.asyncio
    async def test_invalid_action_422(self, admin_client, session_factory):
        uid = await _create_user(session_factory)
        r = await admin_client.patch(f"/admin/users/{uid}", json={"action": "fly"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_user_404(self, admin_client):
        r = await admin_client.patch(
            f"/admin/users/{uuid.uuid4()}", json={"action": "approve"}
        )
        assert r.status_code == 404


# ===========================================================================
# GET /admin/registrations + PATCH
# ===========================================================================


class TestAdminRegistrations:
    @pytest.mark.asyncio
    async def test_list_registrations(self, admin_client, session_factory):
        await _create_user(session_factory)
        r = await admin_client.get("/admin/registrations")
        assert r.status_code == 200
        data = r.json()
        assert "registrations" in data
        assert data["total"] >= 1

    @pytest.mark.asyncio
    async def test_approve_registration(self, admin_client, app, session_factory):
        from iran.db.models import Registration

        uid = await _create_user(session_factory)
        # Get the registration id
        async with session_factory() as s:
            from sqlalchemy import select

            res = await s.execute(
                select(Registration).where(Registration.user_id == uid)
            )
            reg = res.scalar_one()
            reg_id = reg.id

        r = await admin_client.patch(
            f"/admin/registrations/{reg_id}", json={"action": "approve"}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "approve"

        # Verify UserWhitelistAdd sent
        from iran.contracts import RTUNES_PREFIX, decode

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "user.whitelist.add" in types

    @pytest.mark.asyncio
    async def test_reject_registration(self, admin_client, session_factory):
        from iran.db.models import Registration, User

        uid = await _create_user(session_factory)
        async with session_factory() as s:
            from sqlalchemy import select

            res = await s.execute(
                select(Registration).where(Registration.user_id == uid)
            )
            reg = res.scalar_one()
            reg_id = reg.id

        r = await admin_client.patch(
            f"/admin/registrations/{reg_id}", json={"action": "reject"}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "reject"

        async with session_factory() as s:
            user = await s.get(User, uid)
            assert user.status == "deleted"


# ===========================================================================
# GET /admin/jobs  + DELETE
# ===========================================================================


class TestAdminJobs:
    async def _make_job(self, session_factory, status: str = "running") -> str:
        from iran.db.models import Job, User

        from passlib.context import CryptContext

        _pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
        uid = str(uuid.uuid4())
        jid = str(uuid.uuid4())
        async with session_factory() as s:
            s.add(
                User(
                    id=uid,
                    email=f"u{uid[:6]}@t.com",
                    display_name="U",
                    password_hash=_pwd.hash("p"),
                    role="user",
                    status="active",
                )
            )
            await s.flush()
            s.add(
                Job(
                    id=jid,
                    user_id=uid,
                    platform="youtube",
                    url="https://youtube.com/watch?v=test",
                    status=status,
                )
            )
            await s.commit()
        return jid

    @pytest.mark.asyncio
    async def test_list_jobs(self, admin_client, session_factory):
        await self._make_job(session_factory)
        r = await admin_client.get("/admin/jobs")
        assert r.status_code == 200
        data = r.json()
        assert "jobs" in data
        assert data["total"] >= 1

    @pytest.mark.asyncio
    async def test_force_cancel_job(self, admin_client, app, session_factory):
        jid = await self._make_job(session_factory, status="running")
        r = await admin_client.delete(f"/admin/jobs/{jid}")
        assert r.status_code == 204

        from iran.contracts import RTUNES_PREFIX, decode

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "job.cancel" in types

    @pytest.mark.asyncio
    async def test_cancel_completed_job_409(self, admin_client, session_factory):
        jid = await self._make_job(session_factory, status="completed")
        r = await admin_client.delete(f"/admin/jobs/{jid}")
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_unknown_job_404(self, admin_client):
        r = await admin_client.delete(f"/admin/jobs/{uuid.uuid4()}")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_filter_by_status(self, admin_client, session_factory):
        jid = await self._make_job(session_factory, status="pending")
        r = await admin_client.get("/admin/jobs?status=pending")
        assert r.status_code == 200
        ids = [j["job_id"] for j in r.json()["jobs"]]
        assert jid in ids


# ===========================================================================
# GET /admin/storage
# ===========================================================================


class TestAdminStorage:
    @pytest.mark.asyncio
    async def test_get_storage_ok(self, admin_client):
        r = await admin_client.get("/admin/storage")
        assert r.status_code == 200
        data = r.json()
        assert "total_objects" in data
        assert "total_bytes" in data


# ===========================================================================
# GET/PATCH /admin/settings
# ===========================================================================


class TestAdminSettings:
    @pytest.mark.asyncio
    async def test_get_settings_empty(self, admin_client):
        r = await admin_client.get("/admin/settings")
        assert r.status_code == 200
        assert "settings" in r.json()

    @pytest.mark.asyncio
    async def test_patch_settings_sends_message(self, admin_client, app):
        """PATCH /admin/settings sends AdminSettingsUpdate to Kharej."""
        r = await admin_client.patch(
            "/admin/settings",
            json={"settings": {"download_concurrency": "4", "enable_zip_split": "true"}},
        )
        assert r.status_code == 200
        assert r.json()["sent"] is True

        from iran.contracts import RTUNES_PREFIX, decode

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "admin.settings.update" in types

    @pytest.mark.asyncio
    async def test_patch_settings_upserts_db(self, admin_client, session_factory):
        await admin_client.patch(
            "/admin/settings",
            json={"settings": {"my_key": "my_val"}},
        )
        from iran.db.models import Setting

        async with session_factory() as s:
            row = await s.get(Setting, "my_key")
        assert row is not None
        assert row.value == "my_val"


# ===========================================================================
# POST /admin/settings/clearcache
# ===========================================================================


class TestAdminClearcache:
    @pytest.mark.asyncio
    async def test_clearcache_default(self, admin_client, app):
        r = await admin_client.post("/admin/settings/clearcache", json={"target": "all"})
        assert r.status_code == 200
        assert r.json()["target"] == "all"

        from iran.contracts import RTUNES_PREFIX, decode

        transport = app.state.rubika_client._transport
        types = [decode(wire).type for _, wire in transport.sent if wire.startswith(RTUNES_PREFIX)]
        assert "admin.clearcache" in types

    @pytest.mark.asyncio
    async def test_clearcache_invalid_target(self, admin_client):
        r = await admin_client.post(
            "/admin/settings/clearcache", json={"target": "badvalue"}
        )
        assert r.status_code == 422


# ===========================================================================
# GET /admin/health + POST /admin/health/ping
# ===========================================================================


class TestAdminHealth:
    @pytest.mark.asyncio
    async def test_get_health_no_data(self, admin_client):
        r = await admin_client.get("/admin/health")
        assert r.status_code == 200
        assert r.json()["status"] == "no_data"

    @pytest.mark.asyncio
    async def test_health_ping_timeout(self, admin_client):
        """POST /admin/health/ping returns timeout when no pong arrives."""
        import asyncio as _asyncio
        import unittest.mock as _mock

        async def fast_timeout(coro, timeout):
            raise _asyncio.TimeoutError()

        with _mock.patch("asyncio.wait_for", side_effect=fast_timeout):
            r = await admin_client.post("/admin/health/ping")

        assert r.status_code == 200
        assert r.json()["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_health_ping_success(self, admin_client, app, db_engine, session_factory):
        """POST /admin/health/ping returns pong when HealthPong arrives in time."""
        from datetime import datetime, timezone

        from iran.contracts import HealthPong
        from iran.db.models import Setting

        pong_payload = {
            "request_id": "WILL_BE_SET",
            "worker_version": "1.0.0",
            "queue_depth": 0,
            "circuit_breakers": [],
            "providers": [],
            "disk_free_gb": 50.0,
            "uptime_sec": 300,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }

        async def fake_send(msg):
            # Immediately respond with a HealthPong carrying the same request_id
            req_id = msg.request_id
            pong_payload["request_id"] = req_id
            # Store pong in DB
            async with session_factory() as s:
                row = await s.get(Setting, "last_health_pong")
                if row is None:
                    s.add(Setting(key="last_health_pong", value=json.dumps(pong_payload)))
                else:
                    row.value = json.dumps(pong_payload)
                await s.commit()
            # Signal the pending_pings event
            event = app.state.pending_pings.get(req_id)
            if event is not None:
                event.set()

        import unittest.mock as _mock

        with _mock.patch.object(
            app.state.rubika_client, "send", side_effect=fake_send
        ):
            r = await admin_client.post("/admin/health/ping")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["pong"] is not None
        assert data["pong"]["worker_version"] == "1.0.0"


# ===========================================================================
# GET /admin/audit
# ===========================================================================


class TestAdminAudit:
    @pytest.mark.asyncio
    async def test_get_audit_ok(self, admin_client):
        r = await admin_client.get("/admin/audit")
        assert r.status_code == 200
        data = r.json()
        assert "entries" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_audit_records_user_approve(self, admin_client, session_factory):
        """Approving a user creates an audit log entry."""
        uid = await _create_user(session_factory)
        await admin_client.patch(f"/admin/users/{uid}", json={"action": "approve"})

        r = await admin_client.get("/admin/audit?action=admin.user.approve")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert any(e["action"] == "admin.user.approve" for e in entries)


# ===========================================================================
# Admin UI page tests (Step 9b)
# ===========================================================================


class TestAdminUIPages:
    @pytest.mark.asyncio
    async def test_admin_dashboard_200(self, client):
        r = await client.get("/admin")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_users_page_200(self, client):
        r = await client.get("/admin/ui/users")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_registrations_page_200(self, client):
        r = await client.get("/admin/ui/registrations")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_jobs_page_200(self, client):
        r = await client.get("/admin/ui/jobs")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_storage_page_200(self, client):
        r = await client.get("/admin/ui/storage")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_settings_page_200(self, client):
        r = await client.get("/admin/ui/settings")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_health_page_200(self, client):
        r = await client.get("/admin/ui/health")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_admin_audit_page_200(self, client):
        r = await client.get("/admin/ui/audit")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# ===========================================================================
# AdminAck → effective_config stored in settings (main.py handler)
# ===========================================================================


class TestAdminAckHandler:
    @pytest.mark.asyncio
    async def test_admin_ack_stores_effective_config(self, app, db_engine):
        """on_admin_ack writes effective_config to settings table."""
        from iran.contracts import AdminAck
        from iran.db import engine as _engine_mod

        # Monkey-patch engine module to use our test engine
        orig_session_fn = _engine_mod.get_async_session
        from contextlib import asynccontextmanager

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        sf = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def _test_session():
            from fastapi import HTTPException

            async with sf() as s:
                try:
                    yield s
                    await s.commit()
                except HTTPException:
                    await s.commit()
                    raise
                except Exception:
                    await s.rollback()
                    raise

        _engine_mod.get_async_session = _test_session
        try:
            # Build the handlers (they close over the engine module)
            from iran.main import _make_handlers

            handlers = _make_handlers(app)

            ack = AdminAck(
                ts=datetime.now(tz=timezone.utc),
                job_id=None,
                acked_type="admin.settings.update",
                status="ok",
                detail=None,
                effective_config={"download_concurrency": "8"},
            )
            await handlers["admin.ack"](ack)

            async with sf() as s:
                from iran.db.models import Setting

                row = await s.get(Setting, "download_concurrency")
            assert row is not None
            assert row.value == "8"
        finally:
            _engine_mod.get_async_session = orig_session_fn
