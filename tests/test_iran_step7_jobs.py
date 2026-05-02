"""Unit tests for Track B Step 7 — Core Job API.

Uses ``httpx.AsyncClient`` + ``ASGITransport`` against a fresh in-memory
SQLite database so no PostgreSQL or Rubika instance is required.

Coverage:
- POST /jobs:
  - Happy-path creates job (202, job_id returned)
  - Invalid domain → 422 without DB touch
  - Private IP in URL → 422
  - Invalid scheme → 422
  - Invalid platform → 422
  - Rate limit: 11th job in an hour returns 429
  - JobCreate message sent to Rubika transport exactly once
- GET /jobs/{id}:
  - Owner can retrieve their job (200)
  - Admin can retrieve any job (200)
  - Non-owner non-admin gets 403
  - Unknown job_id → 404
- DELETE /jobs/{id}:
  - Owner can cancel pending job (204)
  - JobCancel sent to Rubika exactly once
  - status set to 'cancelled' in DB
  - Already-completed job returns 409
  - Non-owner gets 403
  - Unknown job_id → 404
- GET /jobs/{id}/events (SSE):
  - Completed job emits terminal event immediately and closes
  - Failed job emits terminal event immediately and closes
  - Cancelled job emits terminal event immediately and closes
  - Unknown job → 404
  - Non-owner → 403
- GET /jobs/{id}/download:
  - Returns parts list for completed job
  - Returns 409 for non-completed job
  - Non-owner → 403
  - Unknown job → 404
- GET /jobs/{id}/download?part=N:
  - Returns 302 redirect to presigned URL for part 0
  - Out-of-range part → 404
- GET /jobs:
  - Returns paginated list of user's own jobs
  - Pagination (page, per_page)
- validate_job_url:
  - Allows valid spotify/youtube/etc. domains
  - Rejects localhost
  - Rejects 127.0.0.1
  - Rejects 192.168.x.x
  - Rejects unknown domains
  - Rejects non-https/http scheme
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
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
    """FastAPI app wired to the in-memory test database, stub Rubika + S2."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.deps import get_db
    from iran.config import IranSettings
    from iran.event_bus import make_event_bus
    from iran.main import create_app
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, IranRubikaConfig

    settings = IranSettings(SECRET_KEY="test-secret-for-step7")

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

    # Wire stub Rubika client (no actual connection)
    transport = FakeRubikaTransport(kharej_guid="kharej-test", iran_guid="iran-test")
    rubika_config = IranRubikaConfig(
        RUBIKA_SESSION_IRAN="",
        KHAREJ_RUBIKA_ACCOUNT_GUID="kharej-test",
        IRAN_RUBIKA_ACCOUNT_GUID="iran-test",
    )
    rubika_client = IranRubikaClient(rubika_config, transport=transport)
    test_app.state.rubika_client = rubika_client

    # Wire stub S2 client
    stub_s2 = MagicMock()
    stub_s2.generate_presigned_url.return_value = "https://s3.example.com/presigned"
    test_app.state.s2_client = stub_s2

    # Wire real EventBus
    test_app.state.event_bus = make_event_bus()

    yield test_app, transport, session_factory

    await test_app.state.event_bus.close()


@pytest_asyncio.fixture
async def client(app):
    """httpx AsyncClient pointing at the test app."""
    import httpx

    test_app, transport, session_factory = app
    transport_asgi = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport_asgi, base_url="https://testserver", follow_redirects=False
    ) as c:
        yield c, transport, session_factory


# ---------------------------------------------------------------------------
# Helpers: seed DB rows
# ---------------------------------------------------------------------------


async def _seed_user(
    session_factory,
    *,
    email: str = "alice@example.com",
    password: str = "securepassword1",
    role: str = "user",
    status: str = "active",
) -> dict:
    from sqlalchemy.ext.asyncio import AsyncSession

    from iran.api.auth import hash_password
    from iran.db.models import User

    user_id = str(uuid.uuid4())
    async with session_factory() as session:
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


async def _get_token(http_client, *, email: str, password: str) -> str:
    """Log in and return the access token."""
    resp = await http_client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _seed_job(
    session_factory,
    *,
    user_id: str,
    status: str = "pending",
    platform: str = "spotify",
    url: str = "https://open.spotify.com/track/abc",
    s2_keys: list | None = None,
    error_code: str | None = None,
    error_msg: str | None = None,
) -> str:
    from sqlalchemy.ext.asyncio import AsyncSession

    from iran.db.models import Job

    job_id = str(uuid.uuid4())
    async with session_factory() as session:
        job = Job(
            id=job_id,
            user_id=user_id,
            platform=platform,
            url=url,
            quality="mp3",
            job_type="single",
            status=status,
            s2_keys=s2_keys,
            error_code=error_code,
            error_msg=error_msg,
        )
        session.add(job)
        await session.commit()
    return job_id


# ===========================================================================
# validate_job_url  (unit tests, no HTTP)
# ===========================================================================


class TestValidateJobUrl:
    def test_allows_spotify(self):
        from iran.api.jobs import validate_job_url

        url = validate_job_url("https://open.spotify.com/track/abc")
        assert url == "https://open.spotify.com/track/abc"

    def test_allows_youtube(self):
        from iran.api.jobs import validate_job_url

        validate_job_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_allows_youtu_be(self):
        from iran.api.jobs import validate_job_url

        validate_job_url("https://youtu.be/dQw4w9WgXcQ")

    def test_allows_tidal(self):
        from iran.api.jobs import validate_job_url

        validate_job_url("https://tidal.com/browse/track/12345678")

    def test_allows_bandcamp(self):
        from iran.api.jobs import validate_job_url

        validate_job_url("https://bandcamp.com/track/xyz")

    def test_rejects_unknown_domain(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("https://evil.com/track/abc")
        assert exc_info.value.status_code == 422

    def test_rejects_localhost(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("http://localhost/admin")
        assert exc_info.value.status_code == 422

    def test_rejects_loopback_ip(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("http://127.0.0.1/admin")
        assert exc_info.value.status_code == 422

    def test_rejects_private_ip(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("http://192.168.1.1/admin")
        assert exc_info.value.status_code == 422

    def test_rejects_rfc1918_10(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("http://10.0.0.1/secret")
        assert exc_info.value.status_code == 422

    def test_rejects_non_http_scheme(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("ftp://open.spotify.com/track/abc")
        assert exc_info.value.status_code == 422

    def test_rejects_javascript_scheme(self):
        from fastapi import HTTPException

        from iran.api.jobs import validate_job_url

        with pytest.raises(HTTPException) as exc_info:
            validate_job_url("javascript://open.spotify.com/alert(1)")
        assert exc_info.value.status_code == 422


# ===========================================================================
# POST /jobs
# ===========================================================================


class TestCreateJob:
    @pytest.mark.asyncio
    async def test_create_job_happy_path(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory)
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc123",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert "job_id" in data
        assert len(data["job_id"]) == 36  # UUID4

    @pytest.mark.asyncio
    async def test_create_job_sends_rubika_message(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="bob@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc123",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert len(transport.sent) == 1
        wire = transport.sent[0][1]
        assert wire.startswith("RTUNES::")
        assert '"job.create"' in wire

    @pytest.mark.asyncio
    async def test_create_job_invalid_domain_returns_422(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="carol@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        # Check no DB jobs exist before
        initial_sends = len(transport.sent)

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://evil.com/track/abc",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        # No Rubika message sent for invalid URL
        assert len(transport.sent) == initial_sends

    @pytest.mark.asyncio
    async def test_create_job_private_ip_returns_422(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dave@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "http://192.168.1.100/track",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_job_invalid_platform_returns_422(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="eve@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc",
                "platform": "napster",  # not a valid Platform
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_job_unauthenticated_returns_401(self, client):
        http_client, transport, session_factory = client

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc",
                "platform": "spotify",
                "quality": "mp3",
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rate_limit_11th_job_returns_429(self, client):
        """11th job in one hour must return 429."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="franky@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        # Seed 10 audit_log entries for job.created in the last hour
        from sqlalchemy.ext.asyncio import AsyncSession

        from iran.db.models import AuditLog

        async with session_factory() as session:
            for _ in range(10):
                session.add(
                    AuditLog(
                        actor_id=user["id"],
                        action="job.created",
                        created_at=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
                    )
                )
            await session.commit()

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_rate_limit_old_entries_do_not_count(self, client):
        """Jobs older than 1 hour don't count towards rate limit."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="grace@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        from sqlalchemy.ext.asyncio import AsyncSession

        from iran.db.models import AuditLog

        async with session_factory() as session:
            for _ in range(10):
                session.add(
                    AuditLog(
                        actor_id=user["id"],
                        action="job.created",
                        # more than 1 hour ago
                        created_at=datetime.now(tz=timezone.utc) - timedelta(hours=2),
                    )
                )
            await session.commit()

        resp = await http_client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc",
                "platform": "spotify",
                "quality": "mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        # Should succeed (old entries don't count)
        assert resp.status_code == 202


# ===========================================================================
# GET /jobs/{id}
# ===========================================================================


class TestGetJob:
    @pytest.mark.asyncio
    async def test_owner_can_get_job(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="h@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(session_factory, user_id=user["id"])

        resp = await http_client.get(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["user_id"] == user["id"]
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_admin_can_get_any_job(self, client):
        http_client, transport, session_factory = client
        owner = await _seed_user(session_factory, email="owner@example.com")
        admin = await _seed_user(
            session_factory, email="admin@example.com", role="admin"
        )
        token = await _get_token(
            http_client, email=admin["email"], password=admin["password"]
        )
        job_id = await _seed_job(session_factory, user_id=owner["id"])

        resp = await http_client.get(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_owner_gets_403(self, client):
        http_client, transport, session_factory = client
        owner = await _seed_user(session_factory, email="owner2@example.com")
        other = await _seed_user(session_factory, email="other@example.com")
        token = await _get_token(
            http_client, email=other["email"], password=other["password"]
        )
        job_id = await _seed_job(session_factory, user_id=owner["id"])

        resp = await http_client.get(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_job_returns_404(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="i@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.get(
            f"/jobs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ===========================================================================
# DELETE /jobs/{id}
# ===========================================================================


class TestCancelJob:
    @pytest.mark.asyncio
    async def test_cancel_pending_job(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="j@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(session_factory, user_id=user["id"], status="pending")

        before_sends = len(transport.sent)
        resp = await http_client.delete(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

        # JobCancel sent exactly once
        assert len(transport.sent) == before_sends + 1
        wire = transport.sent[-1][1]
        assert '"job.cancel"' in wire

    @pytest.mark.asyncio
    async def test_cancel_updates_db_status(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="k@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(session_factory, user_id=user["id"], status="accepted")

        await http_client.delete(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        # Verify status in DB
        from sqlalchemy.ext.asyncio import AsyncSession

        from iran.db.models import Job

        async with session_factory() as session:
            job = await session.get(Job, job_id)
        assert job.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_completed_job_returns_409(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="l@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(
            session_factory, user_id=user["id"], status="completed",
            s2_keys=[{"key": "media/x/file.mp3", "size": 1000, "mime": "audio/mpeg", "sha256": "abc"}],
        )

        resp = await http_client.delete(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_non_owner_gets_403(self, client):
        http_client, transport, session_factory = client
        owner = await _seed_user(session_factory, email="owner3@example.com")
        other = await _seed_user(session_factory, email="other2@example.com")
        token = await _get_token(
            http_client, email=other["email"], password=other["password"]
        )
        job_id = await _seed_job(session_factory, user_id=owner["id"])

        resp = await http_client.delete(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_cancel_unknown_job_returns_404(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="m@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.delete(
            f"/jobs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ===========================================================================
# GET /jobs/{id}/events  (SSE)
# ===========================================================================


class TestJobEvents:
    @pytest.mark.asyncio
    async def test_sse_completed_job_emits_terminal_and_closes(self, client):
        """SSE for a completed job should emit job.completed immediately."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="sse_complete@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        parts = [
            {"key": "media/x/file.flac", "size": 42000000, "mime": "audio/flac", "sha256": "abc123"}
        ]
        job_id = await _seed_job(
            session_factory, user_id=user["id"], status="completed", s2_keys=parts
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "event: job.completed" in body
        assert '"type": "job.completed"' in body

    @pytest.mark.asyncio
    async def test_sse_failed_job_emits_terminal_event(self, client):
        """SSE for a failed job should emit job.failed immediately."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="sse_fail@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(
            session_factory,
            user_id=user["id"],
            status="failed",
            error_code="download_timeout",
            error_msg="timed out",
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: job.failed" in body
        assert "download_timeout" in body

    @pytest.mark.asyncio
    async def test_sse_cancelled_job_emits_terminal_event(self, client):
        """SSE for a cancelled job should emit a job.failed (cancelled) event."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="sse_cancel@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(session_factory, user_id=user["id"], status="cancelled")

        resp = await http_client.get(
            f"/jobs/{job_id}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: job.failed" in body
        assert "cancelled" in body

    @pytest.mark.asyncio
    async def test_sse_unknown_job_returns_404(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="sse_404@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.get(
            f"/jobs/{uuid.uuid4()}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_sse_non_owner_gets_403(self, client):
        http_client, transport, session_factory = client
        owner = await _seed_user(session_factory, email="sse_owner@example.com")
        other = await _seed_user(session_factory, email="sse_other@example.com")
        token = await _get_token(
            http_client, email=other["email"], password=other["password"]
        )
        job_id = await _seed_job(session_factory, user_id=owner["id"])

        resp = await http_client.get(
            f"/jobs/{job_id}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


# ===========================================================================
# GET /jobs/{id}/download
# ===========================================================================


class TestDownloadJob:
    @pytest.mark.asyncio
    async def test_download_returns_parts_list(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dl@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        parts = [
            {"key": "media/x/track.mp3", "size": 5000000, "mime": "audio/mpeg", "sha256": "abc"}
        ]
        job_id = await _seed_job(
            session_factory, user_id=user["id"], status="completed", s2_keys=parts
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "parts" in data
        assert len(data["parts"]) == 1
        assert data["parts"][0]["key"] == "media/x/track.mp3"

    @pytest.mark.asyncio
    async def test_download_part0_returns_302(self, client):
        """GET /jobs/{id}/download?part=0 should 302-redirect to presigned URL."""
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dl2@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        parts = [
            {"key": "media/x/track.mp3", "size": 5000000, "mime": "audio/mpeg", "sha256": "abc"}
        ]
        job_id = await _seed_job(
            session_factory, user_id=user["id"], status="completed", s2_keys=parts
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/download?part=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        # follow_redirects=False, so we get the 302
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location == "https://s3.example.com/presigned"

    @pytest.mark.asyncio
    async def test_download_out_of_range_part_returns_404(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dl3@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        parts = [
            {"key": "media/x/track.mp3", "size": 5000000, "mime": "audio/mpeg", "sha256": "abc"}
        ]
        job_id = await _seed_job(
            session_factory, user_id=user["id"], status="completed", s2_keys=parts
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/download?part=5",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_non_completed_job_returns_409(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dl4@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])
        job_id = await _seed_job(session_factory, user_id=user["id"], status="pending")

        resp = await http_client.get(
            f"/jobs/{job_id}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_download_non_owner_gets_403(self, client):
        http_client, transport, session_factory = client
        owner = await _seed_user(session_factory, email="dlowner@example.com")
        other = await _seed_user(session_factory, email="dlother@example.com")
        token = await _get_token(
            http_client, email=other["email"], password=other["password"]
        )
        parts = [
            {"key": "media/x/t.mp3", "size": 1000, "mime": "audio/mpeg", "sha256": "a"}
        ]
        job_id = await _seed_job(
            session_factory, user_id=owner["id"], status="completed", s2_keys=parts
        )

        resp = await http_client.get(
            f"/jobs/{job_id}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_download_unknown_job_returns_404(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="dl5@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        resp = await http_client.get(
            f"/jobs/{uuid.uuid4()}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ===========================================================================
# GET /jobs  (list)
# ===========================================================================


class TestListJobs:
    @pytest.mark.asyncio
    async def test_list_returns_user_jobs(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="list@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        # Seed 3 jobs
        for _ in range(3):
            await _seed_job(session_factory, user_id=user["id"])

        resp = await http_client.get(
            "/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data
        assert data["total"] == 3
        assert len(data["jobs"]) == 3

    @pytest.mark.asyncio
    async def test_list_does_not_return_other_users_jobs(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="list2@example.com")
        other = await _seed_user(session_factory, email="listother@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        # Seed job for 'other'
        await _seed_job(session_factory, user_id=other["id"])
        # Seed job for 'user'
        await _seed_job(session_factory, user_id=user["id"])

        resp = await http_client.get(
            "/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1  # only user's own job

    @pytest.mark.asyncio
    async def test_list_pagination(self, client):
        http_client, transport, session_factory = client
        user = await _seed_user(session_factory, email="listpag@example.com")
        token = await _get_token(http_client, email=user["email"], password=user["password"])

        for _ in range(5):
            await _seed_job(session_factory, user_id=user["id"])

        resp = await http_client.get(
            "/jobs?page=1&per_page=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["jobs"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["per_page"] == 2

    @pytest.mark.asyncio
    async def test_list_unauthenticated_returns_401(self, client):
        http_client, transport, session_factory = client

        resp = await http_client.get("/jobs")
        assert resp.status_code == 401


# ===========================================================================
# Error code message mapping
# ===========================================================================


class TestErrorCodeMessages:
    def test_all_documented_codes_have_messages(self):
        from iran.api.jobs import ERROR_CODE_MESSAGES

        expected_codes = [
            "no_source_available",
            "s2_upload_failed",
            "download_timeout",
            "rate_limited",
            "invalid_url",
            "access_denied",
            "disk_space_error",
            "blocked",
            "not_whitelisted",
            "unsupported_platform",
            "duplicate_job",
            "cancelled",
            "internal_error",
            "error",
        ]
        for code in expected_codes:
            assert code in ERROR_CODE_MESSAGES, f"Missing message for error_code={code!r}"
            assert ERROR_CODE_MESSAGES[code]  # non-empty string

    def test_job_response_includes_human_error_message(self):
        from iran.api.jobs import ERROR_CODE_MESSAGES, _job_to_response
        from iran.db.models import Job

        job = Job(
            id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            platform="spotify",
            url="https://open.spotify.com/track/x",
            quality="mp3",
            job_type="single",
            status="failed",
            error_code="download_timeout",
            error_msg="timed out after 30s",
        )
        data = _job_to_response(job)
        assert data["error_message"] == ERROR_CODE_MESSAGES["download_timeout"]
