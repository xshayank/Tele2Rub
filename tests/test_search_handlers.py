"""Tests for the Iran search API endpoints.

Coverage:
- POST /search (youtube, spotify, musicdl) — happy path, timeout, worker error
- GET /search/thumb — presigned URL redirect, invalid key prefix rejected

Uses the same fixture pattern as test_iran_step9_admin.py:
  - In-memory SQLite DB
  - FakeRubikaTransport + IranRubikaClient
  - Stub S2 client
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures (mirror test_iran_step9_admin.py pattern)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def app(db_engine, session_factory):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.api.deps import get_db
    from iran.config import IranSettings
    from iran.event_bus import make_event_bus
    from iran.main import create_app
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, IranRubikaConfig

    settings = IranSettings(SECRET_KEY="test-secret-search-tests")
    test_app = create_app(settings)

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
    stub_s2.generate_presigned_url.return_value = "https://s3.example.com/presigned/thumb"
    test_app.state.s2_client = stub_s2

    test_app.state.event_bus = make_event_bus()
    test_app.state.pending_pings = {}
    test_app.state.pending_searches = {}
    test_app.state.search_results = {}

    yield test_app

    await test_app.state.event_bus.close()


@pytest_asyncio.fixture
async def client(app):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        follow_redirects=False,
    ) as c:
        yield c


async def _create_active_user_token(session_factory) -> str:
    """Create an active user in the DB and return a Bearer token."""
    from passlib.context import CryptContext

    from iran.api.auth import create_access_token
    from iran.db.models import User

    _pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
    user_id = str(uuid.uuid4())
    async with session_factory() as s:
        user = User(
            id=user_id,
            email=f"user-{user_id[:8]}@test.com",
            display_name="Test User",
            password_hash=_pwd.hash("password"),
            role="user",
            status="active",
        )
        s.add(user)
        await s.commit()

    return create_access_token({"sub": user_id, "role": "user"})


# ---------------------------------------------------------------------------
# POST /search tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_youtube_happy_path(app, client, session_factory) -> None:
    """POST /search youtube returns results when Kharej replies immediately."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_send(msg) -> None:
        req_id = getattr(msg, "request_id", "")
        app.state.search_results[req_id] = {
            "results": [
                {
                    "title": "Never Gonna Give You Up",
                    "channel": "Rick Astley",
                    "duration": "3:33",
                    "video_id": "dQw4w9WgXcQ",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "thumbnail_key": "thumbs/search/yt/dQw4w9WgXcQ.jpg",
                }
            ],
            "error": None,
        }
        event = app.state.pending_searches.get(req_id)
        if event:
            event.set()

    with patch.object(app.state.rubika_client, "send", side_effect=fake_send):
        resp = await client.post(
            "/search",
            json={"platform": "youtube", "query": "rick astley", "limit": 5},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["platform"] == "youtube"
    assert len(data["results"]) == 1
    r = data["results"][0]
    assert r["title"] == "Never Gonna Give You Up"
    assert r["thumbnail_key"] == "thumbs/search/yt/dQw4w9WgXcQ.jpg"


@pytest.mark.asyncio
async def test_search_spotify_happy_path(app, client, session_factory) -> None:
    """POST /search spotify returns categorised results."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_send(msg) -> None:
        req_id = getattr(msg, "request_id", "")
        app.state.search_results[req_id] = {
            "results": [
                {
                    "tracks": [
                        {
                            "title": "Shape of You",
                            "artists": "Ed Sheeran",
                            "url": "https://open.spotify.com/track/7qiZfU4dY1lWllzX7mPBI3",
                            "cover_key": "thumbs/search/sp/track_7qiZfU4dY1lWllzX7mPBI3.jpg",
                            "type": "track",
                        }
                    ],
                    "albums": [],
                    "playlists": [],
                }
            ],
            "error": None,
        }
        event = app.state.pending_searches.get(req_id)
        if event:
            event.set()

    with patch.object(app.state.rubika_client, "send", side_effect=fake_send):
        resp = await client.post(
            "/search",
            json={"platform": "spotify", "query": "ed sheeran", "limit": 5},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["platform"] == "spotify"
    sp = data["results"][0]
    assert sp["tracks"][0]["title"] == "Shape of You"
    assert sp["tracks"][0]["cover_key"].startswith("thumbs/search/sp/")


@pytest.mark.asyncio
async def test_search_musicdl_happy_path(app, client, session_factory) -> None:
    """POST /search musicdl: text-only results, no cover/thumbnail keys."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_send(msg) -> None:
        req_id = getattr(msg, "request_id", "")
        app.state.search_results[req_id] = {
            "results": [
                {
                    "title": "Bohemian Rhapsody",
                    "artist": "Queen",
                    "source": "NeteaseMusicClient",
                    "duration": "5:55",
                }
            ],
            "error": None,
        }
        event = app.state.pending_searches.get(req_id)
        if event:
            event.set()

    with patch.object(app.state.rubika_client, "send", side_effect=fake_send):
        resp = await client.post(
            "/search",
            json={"platform": "musicdl", "query": "bohemian rhapsody", "limit": 5},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["platform"] == "musicdl"
    r = data["results"][0]
    assert r["title"] == "Bohemian Rhapsody"
    assert "thumbnail_key" not in r
    assert "cover_key" not in r


@pytest.mark.asyncio
async def test_search_worker_error_returns_502(app, client, session_factory) -> None:
    """POST /search: SearchFailed reply → 502 with error detail."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_send(msg) -> None:
        req_id = getattr(msg, "request_id", "")
        app.state.search_results[req_id] = {
            "results": [],
            "error": "Spotify rate limited",
        }
        event = app.state.pending_searches.get(req_id)
        if event:
            event.set()

    with patch.object(app.state.rubika_client, "send", side_effect=fake_send):
        resp = await client.post(
            "/search",
            json={"platform": "spotify", "query": "test", "limit": 3},
            headers=headers,
        )

    assert resp.status_code == 502
    assert "Spotify rate limited" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_search_timeout_returns_504(app, client, session_factory) -> None:
    """POST /search: timeout (no Kharej reply) → 504."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_send(msg) -> None:
        pass  # Kharej never replies

    async def immediate_timeout(coro, timeout):
        raise asyncio.TimeoutError()

    with patch.object(app.state.rubika_client, "send", side_effect=fake_send):
        with patch("asyncio.wait_for", side_effect=immediate_timeout):
            resp = await client.post(
                "/search",
                json={"platform": "youtube", "query": "test", "limit": 3},
                headers=headers,
            )

    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_search_empty_query_returns_422(app, client, session_factory) -> None:
    """POST /search with blank query → 422."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/search",
        json={"platform": "youtube", "query": "   ", "limit": 5},
        headers=headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /search/thumb tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thumb_valid_key_redirects(app, client, session_factory) -> None:
    """GET /search/thumb with valid thumbs/search/ key → 302 redirect."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        "/search/thumb",
        params={"key": "thumbs/search/yt/dQw4w9WgXcQ.jpg"},
        headers=headers,
    )
    assert resp.status_code == 302
    assert "presigned" in resp.headers["location"]


@pytest.mark.asyncio
async def test_thumb_invalid_prefix_returns_400(app, client, session_factory) -> None:
    """GET /search/thumb with key outside thumbs/search/ prefix → 400."""
    token = await _create_active_user_token(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        "/search/thumb",
        params={"key": "media/some-job-id/file.mp3"},
        headers=headers,
    )
    assert resp.status_code == 400
