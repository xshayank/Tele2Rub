"""FastAPI application factory for the Iran VPS service (Track B).

The ``create_app`` function is the single entry point for constructing the
ASGI application.  It wires dependency-injection stubs (Rubika client, S2
client, event bus) onto ``app.state`` so that later steps can swap in real
implementations without changing routing code.

Lifespan events (startup / shutdown) are registered via the ``@asynccontextmanager``
pattern introduced in FastAPI ≥ 0.93.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from iran.api.auth import router as auth_router
from iran.api.health import router as health_router
from iran.config import IranSettings, get_settings
from iran.event_bus import make_event_bus
from iran.logging_setup import configure_logging
from iran.rubika_client import make_rubika_client
from iran.s2_client import make_s2_client

logger = logging.getLogger("iran.main")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """ASGI lifespan: initialise DI stubs on startup, clean up on shutdown."""
    settings: IranSettings = app.state.settings  # type: ignore[attr-defined]

    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    logger.info(
        "Iran service starting",
        extra={
            "event": "startup",
            "host": settings.HOST,
            "port": settings.PORT,
        },
    )

    # ------------------------------------------------------------------
    # Initialise DI stubs (replaced by real implementations in later steps)
    # ------------------------------------------------------------------
    app.state.rubika_client = make_rubika_client()
    app.state.s2_client = make_s2_client()
    app.state.event_bus = make_event_bus()

    yield  # ← application runs here

    # ------------------------------------------------------------------
    # Shutdown: close long-lived resources
    # ------------------------------------------------------------------
    logger.info("Iran service shutting down", extra={"event": "shutdown"})
    await app.state.event_bus.close()
    await app.state.rubika_client.close()


def create_app(settings: IranSettings | None = None) -> FastAPI:
    """Construct and return the FastAPI ASGI application.

    Parameters
    ----------
    settings:
        Optional pre-built settings object (useful in tests).  When
        omitted, the cached singleton from :func:`iran.config.get_settings`
        is used.
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="RubeTunes Iran Service",
        description=(
            "Iran-side web service and admin panel for the RubeTunes "
            "download platform (Track B)."
        ),
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Store settings on app state so endpoints and lifespan can access them.
    app.state.settings = settings

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    app.include_router(health_router)
    app.include_router(auth_router)

    return app


def _get_app_metadata() -> dict[str, Any]:
    """Return version metadata without constructing the full app (used by CLI)."""
    import iran

    return {
        "service": "iran",
        "version": iran.__version__,
    }
