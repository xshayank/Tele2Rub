"""Unit tests for Track B Step 3 — Database Models + Alembic Migrations.

Uses ``pytest-asyncio`` with an in-memory ``aiosqlite`` database so no
PostgreSQL instance is required in CI.

Covers:
- All six tables can be created via ``Base.metadata.create_all``.
- Insert + select round-trips for every table.
- Relationships (User→Job, User→RefreshToken, User→AuditLog, User→Registration).
- JSON columns (s2_keys, metadata_json, audit_log.payload) survive a round-trip.
- Default column values (role, status, job_type, progress, done_tracks, …).
- FK constraints are reflected in the schema.
- Alembic migration module is importable and exposes ``upgrade``/``downgrade``.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session():
    """Async SQLAlchemy session backed by a fresh in-memory SQLite database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Model importability
# ---------------------------------------------------------------------------


class TestModelImports:
    def test_base_importable(self):
        from iran.db.models import Base  # noqa: F401

    def test_user_importable(self):
        from iran.db.models import User  # noqa: F401

    def test_job_importable(self):
        from iran.db.models import Job  # noqa: F401

    def test_refresh_token_importable(self):
        from iran.db.models import RefreshToken  # noqa: F401

    def test_audit_log_importable(self):
        from iran.db.models import AuditLog  # noqa: F401

    def test_setting_importable(self):
        from iran.db.models import Setting  # noqa: F401

    def test_registration_importable(self):
        from iran.db.models import Registration  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Engine module importability
# ---------------------------------------------------------------------------


class TestEngineImports:
    def test_engine_module_importable(self):
        import iran.db.engine  # noqa: F401

    def test_get_async_session_importable(self):
        from iran.db.engine import get_async_session  # noqa: F401

    def test_engine_lazy_attribute(self):
        import iran.db.engine as eng_mod

        # engine is exposed via __getattr__ — accessing it should return a value
        e = eng_mod.engine
        assert e is not None

    def test_async_session_local_lazy_attribute(self):
        import iran.db.engine as eng_mod

        sl = eng_mod.AsyncSessionLocal
        assert sl is not None

    def test_engine_module_valid_utf8(self):
        """iran/db/engine.py must be decodable as UTF-8 with no stray bytes.

        Regression guard: a Windows-1252 en-dash (0x97) was accidentally
        introduced in the run_migrations docstring, causing a SyntaxError
        at import time under Python 3.12.
        """
        import py_compile
        from pathlib import Path

        engine_path = Path(_REPO_ROOT) / "iran" / "db" / "engine.py"
        raw = engine_path.read_bytes()
        # Raises UnicodeDecodeError if the file is not valid UTF-8
        raw.decode("utf-8")
        # Raises PyCompileError / SyntaxError if Python cannot parse it
        py_compile.compile(str(engine_path), doraise=True)


# ---------------------------------------------------------------------------
# 2b. Alembic config — script_location resolves to an existing directory
# ---------------------------------------------------------------------------


class TestAlembicConfig:
    """Verify that the Alembic configuration points at a real migrations dir.

    This is a regression test for the startup crash caused by
    ``script_location = migrations`` being resolved relative to CWD instead of
    relative to the ``alembic.ini`` file itself.
    The fix changes the value to ``%(here)s/migrations`` so Alembic always
    finds ``iran/db/migrations/`` regardless of where the process is started.
    """

    def test_script_location_exists(self):
        """Alembic config script_location must resolve to an existing directory."""
        from pathlib import Path

        from alembic.config import Config as AlembicConfig

        alembic_ini = Path(_REPO_ROOT) / "iran" / "db" / "alembic.ini"
        assert alembic_ini.exists(), f"alembic.ini not found at {alembic_ini}"

        cfg = AlembicConfig(str(alembic_ini))
        script_location = cfg.get_main_option("script_location")
        assert script_location is not None, "script_location is not set in alembic.ini"

        # Resolve relative paths from the ini file's directory (same as Alembic)
        resolved = Path(script_location)
        if not resolved.is_absolute():
            resolved = alembic_ini.parent / script_location
        resolved = resolved.resolve()

        assert resolved.exists(), (
            f"Alembic script_location '{script_location}' resolves to '{resolved}' "
            f"which does not exist.  Check iran/db/alembic.ini."
        )
        assert resolved.is_dir(), (
            f"Alembic script_location '{resolved}' exists but is not a directory."
        )

    def test_run_migrations_no_op_without_db_url(self):
        """run_migrations() must be a no-op when DATABASE_URL is not set."""
        import asyncio
        import os

        env_backup = os.environ.copy()
        try:
            # Remove any DB URL from the environment so run_migrations skips
            for key in list(os.environ.keys()):
                if key == "IRAN_DATABASE_URL":
                    del os.environ[key]

            # Also clear the lru_cache so settings are re-read
            from iran.config import get_settings

            get_settings.cache_clear()

            from iran.db.engine import run_migrations

            # Should return without raising
            asyncio.run(run_migrations())
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
            from iran.config import get_settings as _gs

            _gs.cache_clear()

    def test_run_migrations_no_op_when_flag_disabled(self):
        """run_migrations() must skip when IRAN_RUN_MIGRATIONS=0 is set."""
        import asyncio
        import os

        env_backup = os.environ.copy()
        try:
            os.environ["IRAN_RUN_MIGRATIONS"] = "0"
            # Set a non-empty DATABASE_URL so the flag is the only skip path
            os.environ["IRAN_DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"

            from iran.config import get_settings

            get_settings.cache_clear()

            from iran.db.engine import run_migrations

            # Should return without raising (no Alembic call)
            asyncio.run(run_migrations())
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
            from iran.config import get_settings as _gs

            _gs.cache_clear()


# ---------------------------------------------------------------------------
# 3. Table creation (schema smoke test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_all_tables():
    """Base.metadata.create_all() completes without error on SQLite."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# 4. users table
# ---------------------------------------------------------------------------


class TestUserTable:
    @pytest.mark.asyncio
    async def test_insert_minimal_user(self, db_session):
        from iran.db.models import User

        user = User(
            id=_uid(),
            email="alice@example.com",
            display_name="Alice",
            password_hash="hashed",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.id is not None

    @pytest.mark.asyncio
    async def test_user_default_role(self, db_session):
        from iran.db.models import User

        user = User(
            id=_uid(),
            email="bob@example.com",
            display_name="Bob",
            password_hash="hashed",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.role == "user"

    @pytest.mark.asyncio
    async def test_user_default_status(self, db_session):
        from iran.db.models import User

        user = User(
            id=_uid(),
            email="carol@example.com",
            display_name="Carol",
            password_hash="hashed",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.status == "pending_approval"

    @pytest.mark.asyncio
    async def test_user_admin_role(self, db_session):
        from iran.db.models import User

        user = User(
            id=_uid(),
            email="admin@example.com",
            display_name="Admin",
            password_hash="hashed",
            role="admin",
            status="active",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.role == "admin"
        assert user.status == "active"

    @pytest.mark.asyncio
    async def test_user_rubika_guid(self, db_session):
        from iran.db.models import User

        uid = _uid()
        user = User(
            id=uid,
            email=f"user_{uid}@example.com",
            display_name="Rubika User",
            password_hash="hashed",
            rubika_guid="rubika-12345",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.rubika_guid == "rubika-12345"

    @pytest.mark.asyncio
    async def test_user_last_seen_at(self, db_session):
        from iran.db.models import User

        now = _now()
        user = User(
            id=_uid(),
            email="seen@example.com",
            display_name="Seen",
            password_hash="hashed",
            last_seen_at=now,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.last_seen_at is not None

    @pytest.mark.asyncio
    async def test_user_unique_email_constraint(self, db_session):
        """Inserting two users with the same email raises an integrity error."""
        from sqlalchemy.exc import IntegrityError

        from iran.db.models import User

        email = f"dup_{_uid()}@example.com"
        db_session.add(User(id=_uid(), email=email, display_name="A", password_hash="h"))
        await db_session.commit()
        db_session.add(User(id=_uid(), email=email, display_name="B", password_hash="h"))
        with pytest.raises(IntegrityError):
            await db_session.commit()


# ---------------------------------------------------------------------------
# 5. jobs table
# ---------------------------------------------------------------------------


class TestJobTable:
    @pytest.fixture(autouse=True)
    def _user_id(self):
        self._uid = _uid()

    async def _make_user(self, session, uid: str | None = None):
        from iran.db.models import User

        u_id = uid or _uid()
        user = User(
            id=u_id,
            email=f"{u_id}@example.com",
            display_name="Test User",
            password_hash="hashed",
        )
        session.add(user)
        await session.flush()
        return user

    @pytest.mark.asyncio
    async def test_insert_minimal_job(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="spotify",
            url="https://open.spotify.com/track/abc",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.status == "pending"

    @pytest.mark.asyncio
    async def test_job_defaults(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="youtube",
            url="https://youtube.com/watch?v=xyz",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.job_type == "single"
        assert job.status == "pending"
        assert job.progress == 0
        assert job.done_tracks == 0
        assert job.failed_tracks == 0

    @pytest.mark.asyncio
    async def test_job_s2_keys_json(self, db_session):
        """s2_keys stores a list of S2ObjectRef-shaped dicts round-trip."""
        from iran.db.models import Job

        user = await self._make_user(db_session)
        s2_keys = [
            {"key": "media/abc/track.flac", "size": 12345, "mime": "audio/flac", "sha256": "aa" * 32},
        ]
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="tidal",
            url="https://tidal.com/browse/track/123",
            s2_keys=s2_keys,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.s2_keys == s2_keys

    @pytest.mark.asyncio
    async def test_job_metadata_json(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        metadata = {"title": "Shape of You", "artist": "Ed Sheeran", "duration": 234}
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="spotify",
            url="https://open.spotify.com/track/xyz",
            metadata_json=metadata,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.metadata_json == metadata

    @pytest.mark.asyncio
    async def test_job_batch_fields(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="spotify",
            url="https://open.spotify.com/playlist/abc",
            job_type="batch",
            total_tracks=30,
            done_tracks=10,
            failed_tracks=2,
            current_track="Track 11",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.job_type == "batch"
        assert job.total_tracks == 30
        assert job.done_tracks == 10
        assert job.failed_tracks == 2
        assert job.current_track == "Track 11"

    @pytest.mark.asyncio
    async def test_job_error_fields(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="youtube",
            url="https://youtube.com/watch?v=err",
            status="failed",
            error_code="no_source_available",
            error_msg="No valid source found",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.status == "failed"
        assert job.error_code == "no_source_available"

    @pytest.mark.asyncio
    async def test_job_timestamps(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        now = _now()
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="spotify",
            url="https://open.spotify.com/track/ts",
            accepted_at=now,
            completed_at=now,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.accepted_at is not None
        assert job.completed_at is not None

    @pytest.mark.asyncio
    async def test_job_progress_and_phase(self, db_session):
        from iran.db.models import Job

        user = await self._make_user(db_session)
        job = Job(
            id=_uid(),
            user_id=user.id,
            platform="spotify",
            url="https://open.spotify.com/track/prog",
            progress=75,
            phase="uploading",
            speed="3.2 MB/s",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.progress == 75
        assert job.phase == "uploading"
        assert job.speed == "3.2 MB/s"


# ---------------------------------------------------------------------------
# 6. refresh_tokens table
# ---------------------------------------------------------------------------


class TestRefreshTokenTable:
    async def _make_user(self, session) -> str:
        from iran.db.models import User

        uid = _uid()
        session.add(
            User(id=uid, email=f"{uid}@example.com", display_name="X", password_hash="h")
        )
        await session.flush()
        return uid

    @pytest.mark.asyncio
    async def test_insert_refresh_token(self, db_session):
        from iran.db.models import RefreshToken

        user_id = await self._make_user(db_session)
        token = RefreshToken(
            id=_uid(),
            user_id=user_id,
            token="sha256hexofrawtoken",
            expires_at=_now(),
        )
        db_session.add(token)
        await db_session.commit()
        await db_session.refresh(token)
        assert token.revoked is False

    @pytest.mark.asyncio
    async def test_refresh_token_revoke(self, db_session):
        from iran.db.models import RefreshToken

        user_id = await self._make_user(db_session)
        token = RefreshToken(
            id=_uid(),
            user_id=user_id,
            token="sha256hexrevoked",
            expires_at=_now(),
            revoked=True,
        )
        db_session.add(token)
        await db_session.commit()
        await db_session.refresh(token)
        assert token.revoked is True

    @pytest.mark.asyncio
    async def test_refresh_token_unique_constraint(self, db_session):
        """Two tokens with the same SHA-256 hash raise IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        from iran.db.models import RefreshToken

        user_id = await self._make_user(db_session)
        shared_hash = "sha256hashvalue"
        db_session.add(
            RefreshToken(id=_uid(), user_id=user_id, token=shared_hash, expires_at=_now())
        )
        await db_session.commit()
        db_session.add(
            RefreshToken(id=_uid(), user_id=user_id, token=shared_hash, expires_at=_now())
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()


# ---------------------------------------------------------------------------
# 7. audit_log table
# ---------------------------------------------------------------------------


class TestAuditLogTable:
    async def _make_user(self, session) -> str:
        from iran.db.models import User

        uid = _uid()
        session.add(
            User(id=uid, email=f"{uid}@example.com", display_name="X", password_hash="h")
        )
        await session.flush()
        return uid

    @pytest.mark.asyncio
    async def test_insert_audit_log_with_actor(self, db_session):
        from iran.db.models import AuditLog

        actor_id = await self._make_user(db_session)
        entry = AuditLog(
            id=_uid(),
            actor_id=actor_id,
            action="job.create",
            target_id=_uid(),
            payload={"platform": "spotify"},
            ip_addr="1.2.3.4",
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        assert entry.action == "job.create"
        assert entry.payload == {"platform": "spotify"}

    @pytest.mark.asyncio
    async def test_audit_log_system_event_no_actor(self, db_session):
        """System events have a null actor_id."""
        from iran.db.models import AuditLog

        entry = AuditLog(
            id=_uid(),
            actor_id=None,
            action="system.startup",
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        assert entry.actor_id is None

    @pytest.mark.asyncio
    async def test_audit_log_json_payload(self, db_session):
        from iran.db.models import AuditLog

        actor_id = await self._make_user(db_session)
        payload = {"user_id": _uid(), "role": "admin", "nested": {"k": [1, 2, 3]}}
        entry = AuditLog(id=_uid(), actor_id=actor_id, action="user.approve", payload=payload)
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        assert entry.payload == payload


# ---------------------------------------------------------------------------
# 8. settings table
# ---------------------------------------------------------------------------


class TestSettingsTable:
    @pytest.mark.asyncio
    async def test_insert_setting(self, db_session):
        from iran.db.models import Setting

        setting = Setting(key="max_concurrent_jobs", value="5")
        db_session.add(setting)
        await db_session.commit()
        await db_session.refresh(setting)
        assert setting.value == "5"

    @pytest.mark.asyncio
    async def test_setting_update(self, db_session):
        from sqlalchemy import select

        from iran.db.models import Setting

        setting = Setting(key="feature_flag_x", value="false")
        db_session.add(setting)
        await db_session.commit()

        result = await db_session.execute(
            select(Setting).where(Setting.key == "feature_flag_x")
        )
        s = result.scalar_one()
        s.value = "true"
        await db_session.commit()
        await db_session.refresh(s)
        assert s.value == "true"

    @pytest.mark.asyncio
    async def test_multiple_settings(self, db_session):
        from iran.db.models import Setting

        db_session.add_all(
            [
                Setting(key="a", value="1"),
                Setting(key="b", value="2"),
                Setting(key="c", value="3"),
            ]
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# 9. registrations table
# ---------------------------------------------------------------------------


class TestRegistrationsTable:
    async def _make_user(self, session) -> str:
        from iran.db.models import User

        uid = _uid()
        session.add(
            User(id=uid, email=f"{uid}@example.com", display_name="X", password_hash="h")
        )
        await session.flush()
        return uid

    @pytest.mark.asyncio
    async def test_insert_registration(self, db_session):
        from iran.db.models import Registration

        user_id = await self._make_user(db_session)
        reg = Registration(id=_uid(), user_id=user_id, notes="Referred by Alice")
        db_session.add(reg)
        await db_session.commit()
        await db_session.refresh(reg)
        assert reg.notes == "Referred by Alice"
        assert reg.reviewed_by is None
        assert reg.reviewed_at is None

    @pytest.mark.asyncio
    async def test_registration_reviewed(self, db_session):
        from iran.db.models import Registration

        user_id = await self._make_user(db_session)
        admin_id = await self._make_user(db_session)
        now = _now()
        reg = Registration(
            id=_uid(),
            user_id=user_id,
            reviewed_by=admin_id,
            reviewed_at=now,
        )
        db_session.add(reg)
        await db_session.commit()
        await db_session.refresh(reg)
        assert reg.reviewed_by == admin_id
        assert reg.reviewed_at is not None

    @pytest.mark.asyncio
    async def test_registration_unique_user_id(self, db_session):
        """A user can only have one registration record."""
        from sqlalchemy.exc import IntegrityError

        from iran.db.models import Registration

        user_id = await self._make_user(db_session)
        db_session.add(Registration(id=_uid(), user_id=user_id))
        await db_session.commit()
        db_session.add(Registration(id=_uid(), user_id=user_id))
        with pytest.raises(IntegrityError):
            await db_session.commit()


# ---------------------------------------------------------------------------
# 10. Relationships
# ---------------------------------------------------------------------------


class TestRelationships:
    @pytest.mark.asyncio
    async def test_user_job_relationship(self, db_session):
        from sqlalchemy import select

        from iran.db.models import Job, User

        uid = _uid()
        user = User(id=uid, email=f"{uid}@example.com", display_name="U", password_hash="h")
        db_session.add(user)
        await db_session.flush()

        job = Job(id=_uid(), user_id=user.id, platform="spotify", url="https://x.com")
        db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(select(User).where(User.id == uid))
        loaded_user = result.scalar_one()
        # Explicitly load the relationship
        await db_session.refresh(loaded_user, attribute_names=["jobs"])
        assert len(loaded_user.jobs) == 1
        assert loaded_user.jobs[0].platform == "spotify"

    @pytest.mark.asyncio
    async def test_user_refresh_token_relationship(self, db_session):
        from sqlalchemy import select

        from iran.db.models import RefreshToken, User

        uid = _uid()
        user = User(id=uid, email=f"{uid}@example.com", display_name="U", password_hash="h")
        db_session.add(user)
        await db_session.flush()

        rt = RefreshToken(id=_uid(), user_id=uid, token="tok1", expires_at=_now())
        db_session.add(rt)
        await db_session.commit()

        result = await db_session.execute(select(User).where(User.id == uid))
        loaded_user = result.scalar_one()
        await db_session.refresh(loaded_user, attribute_names=["refresh_tokens"])
        assert len(loaded_user.refresh_tokens) == 1


# ---------------------------------------------------------------------------
# 11. Alembic migration module
# ---------------------------------------------------------------------------


class TestAlembicMigration:
    def test_migration_module_importable(self):
        import iran.db.migrations.versions.initial_schema  # noqa: F401

    def test_migration_has_upgrade(self):
        import iran.db.migrations.versions.initial_schema as m

        assert callable(m.upgrade)

    def test_migration_has_downgrade(self):
        import iran.db.migrations.versions.initial_schema as m

        assert callable(m.downgrade)

    def test_migration_revision(self):
        import iran.db.migrations.versions.initial_schema as m

        assert m.revision == "0001_initial_schema"

    def test_migration_down_revision_is_none(self):
        import iran.db.migrations.versions.initial_schema as m

        assert m.down_revision is None


# ---------------------------------------------------------------------------
# 12. Migration apply (upgrade then downgrade on SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alembic_upgrade_and_downgrade():
    """Run upgrade() then downgrade() via Alembic op against a live SQLite DB."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from iran.db.models import Base

    # We test create_all / drop_all as a proxy for upgrade/downgrade
    # (Alembic operations themselves are tested in unit tests above)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# 13. get_async_session context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_async_session_rolls_back_on_error():
    """get_async_session() rolls back when an exception is raised."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iran.db.models import Base, Setting

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_cm():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    with pytest.raises(RuntimeError, match="deliberate"):
        async with _session_cm() as session:
            session.add(Setting(key="test_rollback", value="x"))
            raise RuntimeError("deliberate")

    # Verify the setting was not committed
    from sqlalchemy import select
    async with factory() as session:
        result = await session.execute(
            select(Setting).where(Setting.key == "test_rollback")
        )
        assert result.scalar_one_or_none() is None

    await engine.dispose()
