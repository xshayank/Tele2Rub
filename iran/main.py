"""FastAPI application factory for the Iran VPS service (Track B).

The ``create_app`` function is the single entry point for constructing the
ASGI application.  It wires dependency-injection stubs (Rubika client, S2
client, event bus) onto ``app.state`` so that later steps can swap in real
implementations without changing routing code.

Lifespan events (startup / shutdown) are registered via the ``@asynccontextmanager``
pattern introduced in FastAPI ≥ 0.93.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from iran.api.auth import router as auth_router
from iran.api.health import router as health_router
from iran.api.jobs import router as jobs_router
from iran.config import IranSettings, get_settings
from iran.event_bus import make_event_bus
from iran.logging_setup import configure_logging
from iran.rubika_client import IranRubikaConfig, make_rubika_client
from iran.s2_client import make_s2_client

logger = logging.getLogger("iran.main")


# ---------------------------------------------------------------------------
# Inbound message handlers (registered during lifespan startup)
# ---------------------------------------------------------------------------


def _make_handlers(app: FastAPI) -> dict[str, Any]:
    """Build a dict of ``msg_type → async handler`` closures.

    Each handler receives a typed ``AnyMessage`` and updates the DB / event bus
    via ``app.state``.
    """
    from iran.contracts import (
        AdminAck,
        HealthPong,
        JobAccepted,
        JobCompleted,
        JobFailed,
        JobProgress,
    )
    from iran.db import engine as _engine_mod
    from iran.db.models import AuditLog, Job, Setting

    async def on_job_accepted(msg: JobAccepted) -> None:
        """Update job status to 'accepted' and publish to EventBus."""
        async with _engine_mod.get_async_session() as session:
            job = await session.get(Job, str(msg.job_id))
            if job is None:
                logger.warning("job.accepted for unknown job", extra={"job_id": msg.job_id})
                return
            job.status = "accepted"
            job.accepted_at = datetime.now(tz=timezone.utc)
        event = {"type": "job.accepted", "job_id": str(msg.job_id)}
        app.state.event_bus.publish(str(msg.job_id), event)
        logger.info("job accepted", extra={"job_id": msg.job_id})

    async def on_job_progress(msg: JobProgress) -> None:
        """Update job progress fields and publish to EventBus."""
        async with _engine_mod.get_async_session() as session:
            job = await session.get(Job, str(msg.job_id))
            if job is None:
                logger.warning("job.progress for unknown job", extra={"job_id": msg.job_id})
                return
            job.status = "running"
            job.phase = msg.phase
            if msg.percent is not None:
                job.progress = msg.percent
            if msg.speed is not None:
                job.speed = msg.speed
            if msg.done_tracks is not None:
                job.done_tracks = msg.done_tracks
            if msg.total_tracks is not None:
                job.total_tracks = msg.total_tracks
            if msg.failed_tracks is not None:
                job.failed_tracks = msg.failed_tracks
            if msg.current_track is not None:
                job.current_track = msg.current_track
        event = {
            "type": "job.progress",
            "job_id": str(msg.job_id),
            "phase": msg.phase,
            "percent": msg.percent,
            "speed": msg.speed,
            "done_tracks": msg.done_tracks,
            "total_tracks": msg.total_tracks,
        }
        app.state.event_bus.publish(str(msg.job_id), event)

    async def on_job_completed(msg: JobCompleted) -> None:
        """Update job to 'completed', store S2 keys + metadata, publish EventBus."""
        async with _engine_mod.get_async_session() as session:
            job = await session.get(Job, str(msg.job_id))
            if job is None:
                logger.warning("job.completed for unknown job", extra={"job_id": msg.job_id})
                return
            job.status = "completed"
            job.completed_at = datetime.now(tz=timezone.utc)
            job.s2_keys = [p.model_dump() for p in msg.parts]
            job.metadata_json = msg.metadata
        event = {
            "type": "job.completed",
            "job_id": str(msg.job_id),
            "parts": [p.model_dump() for p in msg.parts],
            "metadata": msg.metadata,
        }
        app.state.event_bus.publish(str(msg.job_id), event)
        logger.info("job completed", extra={"job_id": msg.job_id})

    async def on_job_failed(msg: JobFailed) -> None:
        """Update job to 'failed', store error details, publish EventBus."""
        async with _engine_mod.get_async_session() as session:
            job = await session.get(Job, str(msg.job_id))
            if job is None:
                logger.warning("job.failed for unknown job", extra={"job_id": msg.job_id})
                return
            job.status = "failed"
            job.error_code = msg.error_code
            job.error_msg = msg.message
        event = {
            "type": "job.failed",
            "job_id": str(msg.job_id),
            "error_code": msg.error_code,
            "message": msg.message,
            "retryable": msg.retryable,
        }
        app.state.event_bus.publish(str(msg.job_id), event)
        logger.warning(
            "job failed",
            extra={"job_id": msg.job_id, "error_code": msg.error_code},
        )

    async def on_admin_ack(msg: AdminAck) -> None:
        """Log admin.ack and append an audit_log entry."""
        logger.info(
            "admin.ack received",
            extra={
                "acked_type": msg.acked_type,
                "status": msg.status,
                "detail": msg.detail,
            },
        )
        async with _engine_mod.get_async_session() as session:
            entry = AuditLog(
                action="admin.ack",
                payload={
                    "acked_type": msg.acked_type,
                    "status": msg.status,
                    "detail": msg.detail,
                    "effective_config": msg.effective_config,
                },
            )
            session.add(entry)

    async def on_health_pong(msg: HealthPong) -> None:
        """Persist the health pong payload to the settings table."""
        payload_json = json.dumps(
            {
                "request_id": msg.request_id,
                "worker_version": msg.worker_version,
                "queue_depth": msg.queue_depth,
                "circuit_breakers": [cb.model_dump() for cb in msg.circuit_breakers],
                "providers": [p.model_dump() for p in msg.providers],
                "disk_free_gb": msg.disk_free_gb,
                "uptime_sec": msg.uptime_sec,
                "ts": msg.ts.isoformat(),
            }
        )
        async with _engine_mod.get_async_session() as session:
            existing = await session.get(Setting, "last_health_pong")
            if existing is None:
                setting = Setting(key="last_health_pong", value=payload_json)
                session.add(setting)
            else:
                existing.value = payload_json
                existing.updated_at = datetime.now(tz=timezone.utc)
        logger.info(
            "health.pong stored",
            extra={
                "worker_version": msg.worker_version,
                "queue_depth": msg.queue_depth,
            },
        )

    return {
        "job.accepted": on_job_accepted,
        "job.progress": on_job_progress,
        "job.completed": on_job_completed,
        "job.failed": on_job_failed,
        "admin.ack": on_admin_ack,
        "health.pong": on_health_pong,
    }


# ---------------------------------------------------------------------------
# ASGI lifespan
# ---------------------------------------------------------------------------


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
    # Initialise DI objects
    # ------------------------------------------------------------------
    app.state.event_bus = make_event_bus()
    app.state.s2_client = make_s2_client(settings)

    rubika_config = IranRubikaConfig(
        RUBIKA_SESSION_IRAN=settings.RUBIKA_SESSION_IRAN,
        KHAREJ_RUBIKA_ACCOUNT_GUID=settings.KHAREJ_RUBIKA_ACCOUNT_GUID,
        IRAN_RUBIKA_ACCOUNT_GUID=settings.IRAN_RUBIKA_ACCOUNT_GUID,
    )
    rubika_client = make_rubika_client(rubika_config)
    app.state.rubika_client = rubika_client

    # Register inbound message handlers
    handlers = _make_handlers(app)
    for msg_type, handler in handlers.items():
        rubika_client.register_handler(msg_type, handler)

    # Start the Rubika client only when session credentials are configured
    if settings.RUBIKA_SESSION_IRAN and settings.KHAREJ_RUBIKA_ACCOUNT_GUID:
        await rubika_client.start()
        logger.info("Rubika client started", extra={"event": "rubika_started"})
    else:
        logger.info(
            "Rubika client not started (credentials not configured)",
            extra={"event": "rubika_skip"},
        )

    yield  # ← application runs here

    # ------------------------------------------------------------------
    # Shutdown: close long-lived resources
    # ------------------------------------------------------------------
    logger.info("Iran service shutting down", extra={"event": "shutdown"})
    await app.state.event_bus.close()
    await app.state.rubika_client.stop()


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
    app.include_router(jobs_router)

    return app


def _get_app_metadata() -> dict[str, Any]:
    """Return version metadata without constructing the full app (used by CLI)."""
    import iran

    return {
        "service": "iran",
        "version": iran.__version__,
    }
