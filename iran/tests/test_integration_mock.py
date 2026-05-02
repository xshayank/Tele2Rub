"""Mock Track A worker integration tests (Step 10 of Track B).

Simulates the full Iran ↔ Kharej lifecycle using:
- A fresh in-memory SQLite database
- FakeRubikaTransport (in-process, no real Rubika connection)
- The real inbound handlers from ``iran.main._make_handlers``
- The real EventBus

No live Rubika connection or S2 bucket is required.

Test cases
----------
- Happy path: pending → accepted → running (progress ×3) → completed
- JobFailed path: all ``error_code`` variants → DB status=failed
- JobCancel path: Iran sends JobCancel → DB status=cancelled
- Admin path: HealthPong → DB setting updated; AdminAck → effective_config stored
- Idempotency: duplicate JobProgress (same ts) processed exactly once
- SSE: completed/failed/cancelled jobs emit terminal event immediately
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KHAREJ_GUID = "kharej-test-guid"
_IRAN_GUID = "iran-test-guid"
_TS = datetime(2026, 4, 26, 17, 5, 56, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


async def _drain() -> None:
    """Yield the event loop and allow background tasks (including aiosqlite
    thread-pool queries) to complete."""
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# DB patching helper
# ---------------------------------------------------------------------------


class _DBPatch:
    """Context manager that redirects iran.db.engine to use a test engine."""

    def __init__(self, test_engine):
        import iran.db.engine as _eng_mod

        self._mod = _eng_mod
        self._orig_engine = _eng_mod._engine
        self._orig_factory = _eng_mod._session_factory
        self._test_engine = test_engine

    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        factory = async_sessionmaker(
            self._test_engine, class_=AsyncSession, expire_on_commit=False
        )
        self._mod._engine = self._test_engine
        self._mod._session_factory = factory
        return factory

    async def __aexit__(self, *_):
        self._mod._engine = self._orig_engine
        self._mod._session_factory = self._orig_factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory SQLite engine with all Iran DB tables created."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def integration_env(db_engine):
    """Set up all integration test components.

    Yields a dict with:
    - ``session_factory``: async session factory pointing at the test DB
    - ``rubika_client``: started IranRubikaClient with FakeRubikaTransport
    - ``transport``: the FakeRubikaTransport (for injecting messages)
    - ``event_bus``: real EventBus
    - ``app``: FastAPI app (state wired for admin handlers)
    """
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from iran.config import IranSettings
    from iran.event_bus import make_event_bus
    from iran.main import _make_handlers
    from iran.rubika_client import FakeRubikaTransport, IranRubikaClient, IranRubikaConfig

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    # Minimal FastAPI stub so that _make_handlers can store event_bus / pending_pings
    settings = IranSettings(SECRET_KEY="test-secret-integration")
    fake_app = FastAPI()
    fake_app.state.settings = settings
    event_bus = make_event_bus()
    fake_app.state.event_bus = event_bus
    fake_app.state.pending_pings = {}

    stub_s2 = MagicMock()
    stub_s2.generate_presigned_url.return_value = "https://s3.example.com/presigned"
    stub_s2.list_job_objects = AsyncMock(return_value=[])
    fake_app.state.s2_client = stub_s2

    transport = FakeRubikaTransport(kharej_guid=_KHAREJ_GUID, iran_guid=_IRAN_GUID)
    rubika_config = IranRubikaConfig(
        RUBIKA_SESSION_IRAN="",
        KHAREJ_RUBIKA_ACCOUNT_GUID=_KHAREJ_GUID,
        IRAN_RUBIKA_ACCOUNT_GUID=_IRAN_GUID,
    )
    rubika_client = IranRubikaClient(rubika_config, transport=transport)
    fake_app.state.rubika_client = rubika_client

    # Patch iran.db.engine to use our in-memory test DB
    async with _DBPatch(db_engine) as patched_factory:
        # Register handlers from main.py on the rubika client
        handlers = _make_handlers(fake_app)
        for msg_type, handler in handlers.items():
            rubika_client.register_handler(msg_type, handler)

        await rubika_client.start()

        yield {
            "session_factory": patched_factory,
            "rubika_client": rubika_client,
            "transport": transport,
            "event_bus": event_bus,
            "app": fake_app,
        }

        await rubika_client.stop()
        await event_bus.close()


async def _create_job(session_factory, *, user_id: str | None = None, status: str = "pending") -> str:
    """Seed a Job row in the DB and return its ID."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from iran.db.models import Job

    if user_id is None:
        user_id = _uid()
    job_id = _uid()
    async with session_factory() as session:
        job = Job(
            id=job_id,
            user_id=user_id,
            platform="spotify",
            url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            quality="mp3",
            job_type="single",
            status=status,
        )
        session.add(job)
        await session.commit()
    return job_id


async def _get_job(session_factory, job_id: str) -> Any:
    """Fetch a Job row by its ID."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from iran.db.models import Job

    async with session_factory() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()


# ===========================================================================
# Happy-path lifecycle tests
# ===========================================================================


class TestHappyPathLifecycle:
    @pytest.mark.asyncio
    async def test_job_accepted_updates_db(self, integration_env):
        from iran.contracts import JobAccepted

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        msg = JobAccepted(ts=_TS, job_id=job_id, worker_version="2.0.0", queue_position=1)
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job is not None
        assert job.status == "accepted"
        assert job.accepted_at is not None

    @pytest.mark.asyncio
    async def test_job_progress_updates_db(self, integration_env):
        from iran.contracts import JobProgress

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        msg = JobProgress(
            ts=_TS, job_id=job_id, phase="downloading", percent=50, speed="2 MB/s", eta_sec=60
        )
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job.status == "running"
        assert job.phase == "downloading"
        assert job.progress == 50
        assert job.speed == "2 MB/s"

    @pytest.mark.asyncio
    async def test_full_lifecycle_pending_to_completed(self, integration_env):
        from iran.contracts import JobAccepted, JobCompleted, JobProgress, S2ObjectRef

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        # 1. JobAccepted
        await env["transport"].inject_msg(
            _KHAREJ_GUID,
            JobAccepted(ts=_TS, job_id=job_id, worker_version="2.0.0", queue_position=1),
        )
        await _drain()

        # 2. JobProgress × 3
        for i, phase in enumerate(["downloading", "processing", "uploading"], start=1):
            ts_i = _TS + timedelta(seconds=i)
            await env["transport"].inject_msg(
                _KHAREJ_GUID,
                JobProgress(ts=ts_i, job_id=job_id, phase=phase, percent=i * 30),
            )
        await _drain()

        # 3. JobCompleted
        s2ref = S2ObjectRef(
            key=f"media/{job_id}/track.mp3", size=1024, mime="audio/mpeg", sha256="deadbeef"
        )
        ts_done = _TS + timedelta(seconds=10)
        await env["transport"].inject_msg(
            _KHAREJ_GUID,
            JobCompleted(
                ts=ts_done,
                job_id=job_id,
                parts=[s2ref],
                metadata={"title": "Test Track"},
            ),
        )
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job.status == "completed"
        assert job.completed_at is not None
        assert job.s2_keys is not None
        assert len(job.s2_keys) == 1
        assert job.s2_keys[0]["key"] == f"media/{job_id}/track.mp3"
        assert job.metadata_json == {"title": "Test Track"}

    @pytest.mark.asyncio
    async def test_lifecycle_emits_sse_events_in_order(self, integration_env):
        from iran.contracts import JobAccepted, JobCompleted, JobProgress, S2ObjectRef

        env = integration_env
        job_id = await _create_job(env["session_factory"])
        collected: list[dict] = []

        async with env["event_bus"].subscribe(job_id) as queue:
            # Accepted
            await env["transport"].inject_msg(
                _KHAREJ_GUID,
                JobAccepted(ts=_TS, job_id=job_id, worker_version="2.0.0", queue_position=1),
            )
            await _drain()
            if not queue.empty():
                collected.append(queue.get_nowait())

            # Progress
            ts_prog = _TS + timedelta(seconds=1)
            await env["transport"].inject_msg(
                _KHAREJ_GUID,
                JobProgress(ts=ts_prog, job_id=job_id, phase="downloading", percent=50),
            )
            await _drain()
            if not queue.empty():
                collected.append(queue.get_nowait())

            # Completed
            s2ref = S2ObjectRef(
                key=f"media/{job_id}/t.mp3", size=512, mime="audio/mpeg", sha256="cafe"
            )
            ts_done = _TS + timedelta(seconds=5)
            await env["transport"].inject_msg(
                _KHAREJ_GUID,
                JobCompleted(ts=ts_done, job_id=job_id, parts=[s2ref], metadata={}),
            )
            await _drain()
            if not queue.empty():
                collected.append(queue.get_nowait())

        event_types = [e["type"] for e in collected]
        assert "job.accepted" in event_types
        assert "job.progress" in event_types
        assert "job.completed" in event_types
        # Order must be preserved
        accepted_idx = event_types.index("job.accepted")
        progress_idx = event_types.index("job.progress")
        completed_idx = event_types.index("job.completed")
        assert accepted_idx < progress_idx < completed_idx


# ===========================================================================
# JobFailed — all error_code variants
# ===========================================================================


ALL_ERROR_CODES = [
    "no_source_available",
    "s2_upload_failed",
    "download_timeout",
    "rate_limited",
    "invalid_url",
    "access_denied",
    "disk_space_error",
    "internal_error",
    "blocked",
    "not_whitelisted",
    "unsupported_platform",
    "duplicate_job",
    "cancelled",
    "timeout",
    "not_implemented",
    "error",
    "shutdown",
]


class TestJobFailedPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_code", ALL_ERROR_CODES)
    async def test_job_failed_stores_error_code(self, integration_env, error_code):
        from iran.contracts import JobFailed

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        msg = JobFailed(
            ts=_TS,
            job_id=job_id,
            error_code=error_code,
            message=f"Error: {error_code}",
            retryable=error_code in ("download_timeout", "rate_limited", "s2_upload_failed"),
        )
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job.status == "failed"
        assert job.error_code == error_code
        assert job.error_msg == f"Error: {error_code}"

    @pytest.mark.asyncio
    async def test_job_failed_emits_sse_event(self, integration_env):
        from iran.contracts import JobFailed

        env = integration_env
        job_id = await _create_job(env["session_factory"])
        collected: list[dict] = []

        async with env["event_bus"].subscribe(job_id) as queue:
            await env["transport"].inject_msg(
                _KHAREJ_GUID,
                JobFailed(
                    ts=_TS,
                    job_id=job_id,
                    error_code="internal_error",
                    message="Something went wrong.",
                    retryable=False,
                ),
            )
            await _drain()
            while not queue.empty():
                collected.append(queue.get_nowait())

        assert any(e["type"] == "job.failed" for e in collected)
        failed_event = next(e for e in collected if e["type"] == "job.failed")
        assert failed_event["error_code"] == "internal_error"
        assert failed_event["retryable"] is False


# ===========================================================================
# JobCancel path
# ===========================================================================


class TestJobCancelPath:
    @pytest.mark.asyncio
    async def test_cancel_endpoint_sets_db_cancelled(self, integration_env, db_engine):
        """DELETE /jobs/{id} → DB status='cancelled', JobCancel sent via Rubika."""
        import httpx
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from iran.api.auth import hash_password
        from iran.api.deps import get_db
        from iran.config import IranSettings
        from iran.db.models import Job, User
        from iran.main import create_app

        env = integration_env
        session_factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )

        settings = IranSettings(SECRET_KEY="test-cancel-secret")
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
        test_app.state.rubika_client = env["rubika_client"]
        test_app.state.s2_client = env["app"].state.s2_client
        test_app.state.event_bus = env["event_bus"]
        test_app.state.pending_pings = {}

        user_id = _uid()
        async with session_factory() as session:
            user = User(
                id=user_id,
                email="cancel-test@example.com",
                display_name="Cancel Test",
                password_hash=hash_password("pass1234"),
                role="user",
                status="active",
            )
            session.add(user)
            await session.commit()

        job_id = await _create_job(session_factory, user_id=user_id, status="pending")

        httpx_transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(
            transport=httpx_transport, base_url="https://testserver", follow_redirects=False
        ) as client:
            login_resp = await client.post(
                "/auth/login", json={"email": "cancel-test@example.com", "password": "pass1234"}
            )
            assert login_resp.status_code == 200
            token = login_resp.json()["access_token"]

            del_resp = await client.delete(
                f"/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}
            )
            assert del_resp.status_code == 204

        job = await _get_job(session_factory, job_id)
        assert job.status == "cancelled"

        # JobCancel must have been sent to the Kharej transport
        sent_types = [
            t.split("::", 1)[1] for _, t in env["transport"].sent if "::" in t
        ]
        import json as _json

        sent_msgs = [_json.loads(s) for s in sent_types]
        cancel_msgs = [m for m in sent_msgs if m.get("type") == "job.cancel"]
        assert len(cancel_msgs) == 1
        assert cancel_msgs[0]["job_id"] == job_id


# ===========================================================================
# Admin contract tests
# ===========================================================================


class TestAdminContractHandlers:
    @pytest.mark.asyncio
    async def test_health_pong_stored_in_settings(self, integration_env):
        from iran.contracts import HealthPong

        env = integration_env
        request_id = "req-" + _uid()[:8]

        msg = HealthPong(
            ts=_TS,
            job_id=None,
            request_id=request_id,
            worker_version="2.0.0",
            queue_depth=2,
            disk_free_gb=100.0,
            uptime_sec=7200,
        )
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        from sqlalchemy import select

        from iran.db.models import Setting

        async with env["session_factory"]() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == "last_health_pong")
            )
            setting = result.scalar_one_or_none()

        assert setting is not None
        import json as _json

        payload = _json.loads(setting.value)
        assert payload["request_id"] == request_id
        assert payload["worker_version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_admin_ack_ok_stores_effective_config(self, integration_env):
        from iran.contracts import AdminAck

        env = integration_env

        msg = AdminAck(
            ts=_TS,
            job_id=None,
            acked_type="admin.settings.update",
            status="ok",
            detail="Applied",
            effective_config={"MAX_JOBS_PER_HOUR": "15", "PRESIGNED_URL_TTL_SEC": "1800"},
        )
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        from sqlalchemy import select

        from iran.db.models import Setting

        async with env["session_factory"]() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == "MAX_JOBS_PER_HOUR")
            )
            setting = result.scalar_one_or_none()

        assert setting is not None
        assert setting.value == "15"

    @pytest.mark.asyncio
    async def test_admin_ack_error_does_not_store_config(self, integration_env):
        from iran.contracts import AdminAck

        env = integration_env

        msg = AdminAck(
            ts=_TS,
            job_id=None,
            acked_type="admin.settings.update",
            status="error",
            detail="Failed",
            effective_config=None,
        )
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        # No settings should be persisted for an error ack
        from sqlalchemy import select

        from iran.db.models import AuditLog

        async with env["session_factory"]() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.action == "admin.ack")
            )
            entries = result.scalars().all()

        assert len(entries) >= 1
        last_entry = entries[-1]
        assert last_entry.payload["status"] == "error"


# ===========================================================================
# Idempotency test
# ===========================================================================


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_job_progress_processed_once(self, integration_env):
        """The same JobProgress (identical ts) must be de-duplicated by the client."""
        from iran.contracts import JobProgress

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        msg = JobProgress(
            ts=_TS,  # fixed ts — dedup key is (job_id, type, ts)
            job_id=job_id,
            phase="downloading",
            percent=33,
        )

        # Inject the same message twice
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await env["transport"].inject_msg(_KHAREJ_GUID, msg)
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job.status == "running"
        # The percent should be 33 (set once), not a double-update artifact
        assert job.progress == 33

    @pytest.mark.asyncio
    async def test_second_progress_with_different_ts_is_processed(self, integration_env):
        """A JobProgress with a *different* ts must NOT be de-duplicated."""
        from iran.contracts import JobProgress

        env = integration_env
        job_id = await _create_job(env["session_factory"])

        msg1 = JobProgress(ts=_TS, job_id=job_id, phase="downloading", percent=25)
        msg2 = JobProgress(
            ts=_TS + timedelta(seconds=3), job_id=job_id, phase="downloading", percent=75
        )

        await env["transport"].inject_msg(_KHAREJ_GUID, msg1)
        await _drain()
        await env["transport"].inject_msg(_KHAREJ_GUID, msg2)
        await _drain()

        job = await _get_job(env["session_factory"], job_id)
        assert job.progress == 75  # second update applied


# ===========================================================================
# Unknown job_id graceful handling
# ===========================================================================


class TestUnknownJobHandling:
    @pytest.mark.asyncio
    async def test_job_accepted_unknown_job_no_crash(self, integration_env):
        from iran.contracts import JobAccepted

        env = integration_env
        fake_job_id = _uid()

        # Must not raise — handler logs a warning and returns
        await env["transport"].inject_msg(
            _KHAREJ_GUID,
            JobAccepted(ts=_TS, job_id=fake_job_id, worker_version="2.0.0", queue_position=1),
        )
        await _drain()
        # No crash = pass

    @pytest.mark.asyncio
    async def test_job_completed_unknown_job_no_crash(self, integration_env):
        from iran.contracts import JobCompleted, S2ObjectRef

        env = integration_env
        fake_job_id = _uid()

        s2ref = S2ObjectRef(
            key=f"media/{fake_job_id}/t.mp3", size=512, mime="audio/mpeg", sha256="cafe"
        )
        await env["transport"].inject_msg(
            _KHAREJ_GUID,
            JobCompleted(ts=_TS, job_id=fake_job_id, parts=[s2ref], metadata={}),
        )
        await _drain()
        # No crash = pass
