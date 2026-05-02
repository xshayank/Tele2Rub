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
# Dependency-injection helper (FastAPI / general use)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager that yields a transactional ``AsyncSession``.

    Rolls back on exception; commits + closes on success.
    """
    async with _get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
