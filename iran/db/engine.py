"""Async SQLAlchemy engine + session factory for the Iran VPS service.

Usage::

    from iran.db.engine import get_async_session

    async with get_async_session() as session:
        session.add(some_model)
        await session.commit()

The engine and session factory are constructed lazily on the first call to
:func:`_get_engine` / :func:`_get_session_factory` so that importing this
module does not immediately require a live database URL or ``pydantic-settings``
to be installed (useful in tests that supply their own engine).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Lazy engine + session factory
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(url: str | None = None) -> AsyncEngine:
    """Build (or rebuild) the async engine from *url* or settings."""
    if url is None:
        # Import settings lazily to avoid hard dependency at import time.
        try:
            from iran.config import get_settings

            url = get_settings().DATABASE_URL
        except Exception:
            url = ""
    return create_async_engine(
        url or "sqlite+aiosqlite:///:memory:",
        pool_pre_ping=True,
        echo=False,
    )


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


# ---------------------------------------------------------------------------
# Convenience module-level aliases (may be reassigned in tests)
# ---------------------------------------------------------------------------

# These are properties rather than cached values so tests can patch them.
# For convenience, we expose them as module-level names via __getattr__.

def __getattr__(name: str) -> object:
    if name == "engine":
        return _get_engine()
    if name == "AsyncSessionLocal":
        return _get_session_factory()
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# Alembic migration helper
# ---------------------------------------------------------------------------


async def run_migrations() -> None:
    """Run ``alembic upgrade head`` programmatically against the live database.

    Safe to call on every startup - Alembic is idempotent and skips
    revisions that have already been applied.  When ``DATABASE_URL`` is
    empty (e.g. during unit tests that supply their own engine) this
    function is a no-op.

    Set the environment variable ``IRAN_RUN_MIGRATIONS=0`` to skip
    migrations entirely (useful in development / CI when no live database
    is available).  The default is to run migrations so that production
    deployments stay up-to-date automatically.
    """
    # Check the opt-out flag first so we fail fast without importing Alembic.
    try:
        from iran.config import get_settings

        settings = get_settings()
        url = settings.DATABASE_URL
        run_flag = settings.RUN_MIGRATIONS
    except (ImportError, AttributeError, ValueError):
        url = os.environ.get("IRAN_DATABASE_URL", "")
        # Pydantic-settings handles common boolean strings ("false", "no", "0"
        # -> False) automatically for the RUN_MIGRATIONS bool field.  Replicate
        # the same set here for the rare fallback path (no settings available).
        run_flag = os.environ.get("IRAN_RUN_MIGRATIONS", "1").strip() not in ("0", "false", "no")

    if not run_flag:
        return  # explicitly disabled via IRAN_RUN_MIGRATIONS=0

    if not url:
        return  # no-op in tests / when DB is not configured

    import pathlib

    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_ini = pathlib.Path(__file__).parent / "alembic.ini"
    alembic_cfg = AlembicConfig(str(alembic_ini))
    alembic_cfg.set_main_option("sqlalchemy.url", url)

    # Run in a thread pool to avoid blocking the event loop
    import asyncio
    import functools

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(command.upgrade, alembic_cfg, "head")
        )
    except Exception as exc:
        raise RuntimeError(
            f"Database migration failed - Alembic could not run 'upgrade head'.\n"
            f"  alembic.ini  : {alembic_ini}\n"
            f"  migrations   : {alembic_ini.parent / 'migrations'}\n"
            f"  database url : {url!r}\n"
            f"To skip migrations set IRAN_RUN_MIGRATIONS=0.\n"
            f"Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Dependency-injection helper (FastAPI / general use)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager that yields a transactional ``AsyncSession``.

    Rolls back on unexpected exceptions; commits on success or on
    ``HTTPException`` (so audit-log entries survive HTTP error responses).
    """
    from fastapi import HTTPException

    async with _get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except HTTPException:
            # Commit so that audit-log / rate-limit entries written before
            # the HTTP error are persisted (e.g. login failure records).
            await session.commit()
            raise
        except Exception:
            await session.rollback()
            raise
