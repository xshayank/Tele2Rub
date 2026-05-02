"""Alembic environment configuration for the Iran VPS service (Track B).

Supports both *offline* (SQL script generation) and *online* (live DB)
migration modes.  The async engine is built from ``IranSettings.DATABASE_URL``
so the same environment variables used in production drive migrations.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Alembic Config object
# ---------------------------------------------------------------------------

config = context.config

# Configure Python logging from the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Target metadata — import all models so Alembic can detect changes
# ---------------------------------------------------------------------------

from iran.db.models import Base  # noqa: E402

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_database_url() -> str:
    """Return the database URL, preferring the settings over alembic.ini."""
    # Allow override via environment / IranSettings
    try:
        from iran.config import get_settings

        url = get_settings().DATABASE_URL
        if url:
            return url
    except Exception:  # noqa: BLE001
        pass
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "No DATABASE_URL configured.  Set IRAN_DATABASE_URL in the environment."
        )
    return url


# ---------------------------------------------------------------------------
# Offline mode — generate SQL without a live connection
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in *offline* mode (generate SQL script)."""
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — apply migrations against a live database
# ---------------------------------------------------------------------------


def do_run_migrations(connection) -> None:  # type: ignore[type-arg]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in *online* mode using an async engine."""
    connectable = create_async_engine(_get_database_url(), echo=False)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
