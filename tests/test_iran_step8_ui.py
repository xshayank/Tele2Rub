"""Tests for Track B Step 8 — UI Layer (server-rendered Jinja2 pages).

All routes return ``text/html`` and must respond with ``200 OK``.

Routes under test
-----------------
GET  /              Home / job-submit form
GET  /login         Login form
GET  /register      Registration form
GET  /pending       Pending-approval notice
GET  /ui/jobs/{id}  Job progress page (job_id embedded in HTML)
GET  /library       Library page
GET  /settings      Settings page
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures (mirror Step 7 setup to reuse DB/app infrastructure)
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
    """FastAPI app wired to in-memory DB, stub Rubika + S2."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.deps import get_db
    from iran.config import IranSettings
    from iran.event_bus import make_event_bus
    from iran.main import create_app
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, IranRubikaConfig

    settings = IranSettings(SECRET_KEY="test-secret-step8")
    test_app = create_app(settings)

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

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
    test_app.state.s2_client = stub_s2

    test_app.state.event_bus = make_event_bus()

    yield test_app

    await test_app.state.event_bus.close()


@pytest_asyncio.fixture
async def client(app):
    """httpx AsyncClient for the test app."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: basic page rendering (200 + text/html)
# ---------------------------------------------------------------------------


class TestUIPages:
    @pytest.mark.asyncio
    async def test_home_page_ok(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_login_page_ok(self, client):
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_register_page_ok(self, client):
        resp = await client.get("/register")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_pending_page_ok(self, client):
        resp = await client.get("/pending")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_library_page_ok(self, client):
        resp = await client.get("/library")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_settings_page_ok(self, client):
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_job_page_ok(self, client):
        job_id = str(uuid.uuid4())
        resp = await client.get(f"/ui/jobs/{job_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_job_page_contains_job_id(self, client):
        """The job_id must appear in the rendered HTML for JavaScript use."""
        job_id = str(uuid.uuid4())
        resp = await client.get(f"/ui/jobs/{job_id}")
        assert resp.status_code == 200
        assert job_id in resp.text


# ---------------------------------------------------------------------------
# Tests: page content smoke checks
# ---------------------------------------------------------------------------


class TestUIPageContent:
    @pytest.mark.asyncio
    async def test_home_has_submit_form(self, client):
        resp = await client.get("/")
        assert "submit-form" in resp.text or "ارسال درخواست" in resp.text

    @pytest.mark.asyncio
    async def test_login_has_form_fields(self, client):
        resp = await client.get("/login")
        assert "email" in resp.text
        assert "password" in resp.text

    @pytest.mark.asyncio
    async def test_register_has_form_fields(self, client):
        resp = await client.get("/register")
        assert "display_name" in resp.text
        assert "email" in resp.text
        assert "password" in resp.text

    @pytest.mark.asyncio
    async def test_pending_shows_approval_notice(self, client):
        resp = await client.get("/pending")
        assert "انتظار" in resp.text

    @pytest.mark.asyncio
    async def test_library_has_filter(self, client):
        resp = await client.get("/library")
        assert "filter-status" in resp.text or "فیلتر" in resp.text

    @pytest.mark.asyncio
    async def test_settings_has_danger_zone(self, client):
        resp = await client.get("/settings")
        assert "حذف حساب" in resp.text or "delete" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_job_page_has_progress_bar(self, client):
        job_id = str(uuid.uuid4())
        resp = await client.get(f"/ui/jobs/{job_id}")
        assert "progress" in resp.text

    @pytest.mark.asyncio
    async def test_job_page_has_cancel_button(self, client):
        job_id = str(uuid.uuid4())
        resp = await client.get(f"/ui/jobs/{job_id}")
        assert "cancel" in resp.text.lower() or "لغو" in resp.text

    @pytest.mark.asyncio
    async def test_home_has_platform_grid(self, client):
        resp = await client.get("/")
        assert "Spotify" in resp.text and "YouTube" in resp.text

    @pytest.mark.asyncio
    async def test_pages_use_rtl_layout(self, client):
        """All pages must include dir=rtl for Persian RTL layout."""
        job_id = str(uuid.uuid4())
        paths = [
            "/",
            "/login",
            "/register",
            "/pending",
            "/library",
            "/settings",
            f"/ui/jobs/{job_id}",
        ]
        for path in paths:
            resp = await client.get(path)
            assert 'dir="rtl"' in resp.text, f"RTL not found in {path}"

    @pytest.mark.asyncio
    async def test_pages_include_vazirmatn_font(self, client):
        """All pages should reference the Vazirmatn font."""
        resp = await client.get("/")
        assert "Vazirmatn" in resp.text

    @pytest.mark.asyncio
    async def test_nav_links_present(self, client):
        """Navigation links to main sections should appear on all pages."""
        resp = await client.get("/")
        assert "/library" in resp.text
        assert "/settings" in resp.text
        assert "/login" in resp.text

    @pytest.mark.asyncio
    async def test_home_contains_logo_svg(self, client):
        """Home page HTML must reference at least one of the logo SVG files."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "rube_desktop.svg" in resp.text or "rube_mobile.svg" in resp.text

    @pytest.mark.asyncio
    async def test_pages_include_error_normalizer_helper(self, client):
        """Base template should expose shared API error formatting helper."""
        home = await client.get("/")
        job = await client.get(f"/ui/jobs/{uuid.uuid4()}")
        search = await client.get("/search")
        assert "window.getApiErrorMessage" in home.text
        assert "window.getApiErrorMessage" in job.text
        assert "window.getApiErrorMessage" in search.text

    @pytest.mark.asyncio
    async def test_static_mobile_svg_ok(self, client):
        """GET /static/rube_mobile.svg must return 200 with image/svg+xml."""
        resp = await client.get("/static/rube_mobile.svg")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_static_desktop_svg_ok(self, client):
        """GET /static/rube_desktop.svg must return 200 with image/svg+xml."""
        resp = await client.get("/static/rube_desktop.svg")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]

    # ---------------------------------------------------------------------------
    # Tests: SSE endpoint integration (existing API unchanged)
    # ---------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_job_page_invalid_uuid_returns_404(self, client):
        """Non-UUID job_id should return 404 (defense-in-depth)."""
        resp = await client.get("/ui/jobs/not-a-uuid")
        assert resp.status_code == 404

    """Ensure that Step 8 changes do not break the existing JSON API routes."""

    @pytest.mark.asyncio
    async def test_health_check_still_works(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_jobs_api_still_requires_auth(self, client):
        resp = await client.post(
            "/jobs",
            json={
                "url": "https://open.spotify.com/track/abc",
                "platform": "spotify",
                "quality": "mp3",
            },
        )
        assert resp.status_code == 401
