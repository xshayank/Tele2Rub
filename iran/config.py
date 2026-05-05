"""Settings for the Iran VPS service (Track B).

All configuration is loaded from environment variables with the ``IRAN_``
prefix (e.g. ``IRAN_PORT=8000``).  An optional ``.env`` file is also
supported for local development.

Usage::

    from iran.config import get_settings

    settings = get_settings()        # cached singleton
    print(settings.PORT)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Guard against accidental contract upgrades — bump iran/ code if v changes.
from kharej.contracts import CONTRACT_VERSION as _CONTRACT_VERSION

assert _CONTRACT_VERSION == 1, (  # noqa: S101
    f"kharej.contracts.CONTRACT_VERSION is {_CONTRACT_VERSION!r}; "
    "update iran/ code to handle the new version before proceeding."
)


class IranSettings(BaseSettings):
    """Runtime configuration for the Iran VPS service.

    All fields can be overridden via environment variables prefixed with
    ``IRAN_`` (e.g. ``IRAN_PORT=9000``).
    """

    # ------------------------------------------------------------------
    # HTTP server
    # ------------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # "json" | "text"

    # ------------------------------------------------------------------
    # Security (filled in Step 4)
    # ------------------------------------------------------------------
    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 180
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ------------------------------------------------------------------
    # Database (filled in Step 3)
    # ------------------------------------------------------------------
    DATABASE_URL: str = ""

    # ------------------------------------------------------------------
    # Rubika transport (filled in Step 5)
    # ------------------------------------------------------------------
    RUBIKA_SESSION_IRAN: str = ""
    KHAREJ_RUBIKA_ACCOUNT_GUID: str = ""
    IRAN_RUBIKA_ACCOUNT_GUID: str = ""

    # ------------------------------------------------------------------
    # Database migrations
    # ------------------------------------------------------------------
    # Set IRAN_RUN_MIGRATIONS=0 to skip running Alembic on startup.
    # Useful in development / CI when no live database is available.
    # Defaults to True so production deployments are safe out of the box.
    RUN_MIGRATIONS: bool = True

    # ------------------------------------------------------------------
    # Job rate limiting (Step 7)
    # ------------------------------------------------------------------
    MAX_JOBS_PER_HOUR: int = 10

    # ------------------------------------------------------------------
    # S2 read-only client (filled in Step 6)
    # ------------------------------------------------------------------
    S2_ENDPOINT_URL: str = ""
    S2_ACCESS_KEY: str = ""
    S2_SECRET_KEY: str = ""
    S2_BUCKET: str = ""
    S2_PRESIGN_EXPIRE_SECONDS: int = 3600

    model_config = SettingsConfigDict(
        env_prefix="IRAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> IranSettings:
    """Return the cached application settings singleton."""
    return IranSettings()
