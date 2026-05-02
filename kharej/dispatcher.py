"""Job dispatcher for the Kharej VPS worker.

Receives ``AnyMessage`` objects from :class:`~kharej.rubika_client.RubikaClient`,
applies the access-control gate, looks up a platform-specific
:class:`Downloader`, spawns an ``asyncio`` task per job, and publishes
lifecycle events (``job.accepted`` → ``job.completed`` | ``job.failed``) back to
the Iran VPS via :class:`~kharej.progress_reporter.ProgressReporter`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable

from kharej import __version__
from kharej.access_control import AccessControl
from kharej.contracts import (
    AccessDecision,
    AdminAck,
    AdminClearcache,
    AdminCookiesUpdate,
    AdminSettingsUpdate,
    AnyMessage,
    HealthPing,
    HealthPong,
    JobCancel,
    JobCreate,
    S2ObjectRef,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
)
from kharej.progress_reporter import ProgressReporter
from kharej.rubika_client import RubikaClient
from kharej.s2_client import S2Client
from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.dispatcher")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_COOKIES_PATH = Path(__file__).parent / "state" / "cookies.txt"
"""Default path to the ``cookies.txt`` file replaced by ``admin.cookies.update``."""


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """Normalised, immutable representation of a single download job.

    Built by the dispatcher from a validated :class:`~kharej.contracts.JobCreate`
    message and passed to the :class:`Downloader` implementation.
    """

    job_id: str
    user_id: str
    platform: str
    url: str
    quality: str | None
    job_type: str  # 'single' | 'batch'
    payload: JobCreate  # original message, kept for downloader extras


# ---------------------------------------------------------------------------
# Downloader protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Downloader(Protocol):
    """Protocol that every platform-specific download adapter must satisfy."""

    platform: ClassVar[str]

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Perform the download, upload to S2, return the list of S2ObjectRef.

        - Should call ``progress.report_progress()`` periodically.
        - Must **not** call ``progress.report_completed`` / ``report_failed`` —
          the dispatcher does that based on the return value or exception.
        """
        ...


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Routes inbound Rubika messages to the correct download handler.

    Parameters
    ----------
    s2:
        S2 storage client passed through to each downloader.
    rubika:
        Rubika client (used for its ``send`` indirectly via *progress*).
    access:
        Access-control gate.
    settings:
        Runtime key-value settings store.
    progress:
        Progress reporter (wraps the outbound Rubika ``send`` callable).
    downloaders:
        Optional explicit mapping of ``platform_str → Downloader``.  When
        ``None`` (default), a single built-in
        :class:`~kharej.downloaders.stub.StubDownloader` is registered so the
        Step 6 smoke flow works out of the box.
    job_timeout_seconds:
        Hard per-job time cap (default 1 h).  Jobs that exceed this are
        cancelled and reported as ``error_code="timeout"``.
    """

    def __init__(
        self,
        *,
        s2: S2Client,
        rubika: RubikaClient,
        access: AccessControl,
        settings: KharejSettings,
        progress: ProgressReporter,
        downloaders: Mapping[str, Any] | None = None,
        job_timeout_seconds: float = 60 * 60,
        cookies_path: Path | None = None,
    ) -> None:
        self._s2 = s2
        self._rubika = rubika
        self._access = access
        self._settings = settings
        self._progress = progress
        self._job_timeout = job_timeout_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._started_at: float = time.monotonic()
        self._cookies_path: Path = cookies_path or _COOKIES_PATH

        if downloaders is not None:
            self._downloaders: dict[str, Any] = dict(downloaders)
        else:
            from kharej.downloaders.amazon import AmazonDownloader
            from kharej.downloaders.bandcamp import BandcampDownloader
            from kharej.downloaders.batch import BatchDownloader
            from kharej.downloaders.musicdl import MusicdlDownloader
            from kharej.downloaders.qobuz import QobuzDownloader
            from kharej.downloaders.soundcloud import SoundcloudDownloader
            from kharej.downloaders.spotify import SpotifyDownloader
            from kharej.downloaders.stub import StubDownloader
            from kharej.downloaders.tidal import TidalDownloader
            from kharej.downloaders.youtube import YoutubeDownloader

            stub = StubDownloader()
            yt = YoutubeDownloader()
            sp = SpotifyDownloader()
            tidal = TidalDownloader()
            qobuz = QobuzDownloader()
            amazon = AmazonDownloader()
            sc = SoundcloudDownloader()
            bc = BandcampDownloader()
            mdl = MusicdlDownloader()
            batch = BatchDownloader(
                per_track_downloaders={
                    yt.platform: yt,
                    sp.platform: sp,
                    tidal.platform: tidal,
                    qobuz.platform: qobuz,
                    amazon.platform: amazon,
                    sc.platform: sc,
                    bc.platform: bc,
                }
            )
            self._downloaders = {
                stub.platform: stub,
                yt.platform: yt,
                sp.platform: sp,
                tidal.platform: tidal,
                qobuz.platform: qobuz,
                amazon.platform: amazon,
                sc.platform: sc,
                bc.platform: bc,
                mdl.platform: mdl,
                batch.platform: batch,
            }

        # Separate storage for the batch downloader instance so that the
        # dispatcher can route by job_type == "batch" regardless of which
        # platform string is on the message.
        self._batch_downloader: Any = self._downloaders.get("batch")

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register(self, downloader: Any) -> None:
        """Register (or replace) a downloader for its declared platform."""
        self._downloaders[downloader.platform] = downloader
        if downloader.platform == "batch":
            self._batch_downloader = downloader

    def has(self, platform: str) -> bool:
        """Return ``True`` if a downloader is registered for *platform*."""
        return platform in self._downloaders

    # ------------------------------------------------------------------
    # In-flight counter
    # ------------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        """Number of currently running download tasks."""
        return len(self._tasks)

    # ------------------------------------------------------------------
    # Message routing entry point
    # ------------------------------------------------------------------

    async def handle_message(self, msg: AnyMessage) -> None:
        """Single entry point wired to :meth:`~kharej.rubika_client.RubikaClient.on_message`.

        Routes by message type.  Unknown / unhandled types are logged and
        ignored — they never raise.
        """
        try:
            if isinstance(msg, JobCreate):
                await self.handle_job_create(msg)
            elif isinstance(msg, JobCancel):
                await self.handle_job_cancel(msg)
            elif isinstance(msg, HealthPing):
                await self.handle_health_ping(msg)
            elif isinstance(msg, UserWhitelistAdd):
                await self._access.handle_whitelist_add(msg, self._rubika.send)
            elif isinstance(msg, UserWhitelistRemove):
                await self._access.handle_whitelist_remove(msg, self._rubika.send)
            elif isinstance(msg, UserBlockAdd):
                await self._access.handle_block_add(msg, self._rubika.send)
            elif isinstance(msg, UserBlockRemove):
                await self._access.handle_block_remove(msg, self._rubika.send)
            elif isinstance(msg, AdminSettingsUpdate):
                await self._settings.handle_settings_update(msg, self._rubika.send)
            elif isinstance(msg, AdminClearcache):
                await self.handle_admin_clearcache(msg)
            elif isinstance(msg, AdminCookiesUpdate):
                await self.handle_admin_cookies_update(msg)
            else:
                logger.info(
                    {"event": "dispatcher.ignored", "type": getattr(msg, "type", "unknown")}
                )
        except Exception:
            logger.exception(
                {"event": "dispatcher.handler_error", "type": getattr(msg, "type", "unknown")}
            )

    # ------------------------------------------------------------------
    # Job create
    # ------------------------------------------------------------------

    async def handle_job_create(self, msg: JobCreate) -> None:
        """Process a ``job.create`` message end-to-end."""
        job_id = msg.job_id or ""

        # Derive host for logging (never log the full URL at INFO+).
        host = urllib.parse.urlsplit(msg.url).netloc
        platform_str = (
            str(msg.platform.value) if hasattr(msg.platform, "value") else str(msg.platform)
        )

        # 1. Access check.
        decision = self._access.check_access(msg.user_id)

        if decision == AccessDecision.block:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": platform_str,
                    "host": host,
                    "error_code": "blocked",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="blocked",
                error_msg="user is blocked",
            )
            return

        if decision == AccessDecision.not_whitelisted:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": platform_str,
                    "host": host,
                    "error_code": "not_whitelisted",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="not_whitelisted",
                error_msg="user not approved",
            )
            return

        # 2. Accept the job.
        logger.info(
            {
                "event": "dispatcher.job_accepted",
                "job_id": job_id,
                "platform": platform_str,
                "host": host,
            }
        )
        await self._progress.report_accepted(
            job_id,
            worker_version=__version__,
            queue_position=self.in_flight + 1,
        )

        # 3. Look up downloader.
        # Batch jobs are routed to the BatchDownloader regardless of which
        # platform the tracks belong to — the BatchDownloader holds a reference
        # to the per-platform single-track downloaders internally.
        if msg.job_type == "batch" and self._batch_downloader is not None:
            downloader = self._batch_downloader
        else:
            downloader = self._downloaders.get(platform_str)
            if downloader is None:
                # Also try via direct key match (handles Platform enum lookup).
                downloader = self._downloaders.get(msg.platform)

        if downloader is None:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": platform_str,
                    "host": host,
                    "error_code": "unsupported_platform",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="unsupported_platform",
                error_msg=f"no handler for {platform_str}",
            )
            return

        # 4. Build Job.
        job = Job(
            job_id=job_id,
            user_id=msg.user_id,
            platform=platform_str,
            url=msg.url,
            quality=msg.quality if msg.quality else None,
            job_type=msg.job_type,
            payload=msg,
        )

        # 5. Reject duplicate job_id.
        if job_id in self._tasks:
            logger.warning(
                {
                    "event": "dispatcher.duplicate_job",
                    "job_id": job_id,
                    "platform": platform_str,
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="duplicate_job",
                error_msg="job already running",
            )
            return

        # 6. Spawn background task (dispatcher does NOT await it).
        task = asyncio.create_task(self._run_job(job, downloader))
        self._tasks[job_id] = task

    # ------------------------------------------------------------------
    # Job cancel
    # ------------------------------------------------------------------

    async def handle_job_cancel(self, msg: JobCancel) -> None:
        """Cancel a running job by ``job_id`` (idempotent if not running)."""
        job_id = msg.job_id or ""
        task = self._tasks.get(job_id)
        if task is None:
            logger.info({"event": "dispatcher.cancel_noop", "job_id": job_id})
            return
        logger.debug({"event": "dispatcher.cancelling", "job_id": job_id})
        task.cancel()

    # ------------------------------------------------------------------
    # Health ping/pong
    # ------------------------------------------------------------------

    async def handle_health_ping(self, msg: HealthPing) -> None:
        """Handle ``health.ping``: respond with ``health.pong``."""
        logger.info({"event": "dispatcher.health_ping", "request_id": msg.request_id})
        try:
            disk = shutil.disk_usage("/")
            disk_free_gb = disk.free / (1024 ** 3)
        except Exception:
            disk_free_gb = 0.0

        uptime_sec = int(time.monotonic() - self._started_at)

        pong = HealthPong(
            ts=datetime.now(tz=timezone.utc),
            request_id=msg.request_id,
            worker_version=__version__,
            queue_depth=self.in_flight,
            circuit_breakers=[],
            providers=[],
            disk_free_gb=round(disk_free_gb, 2),
            uptime_sec=uptime_sec,
        )
        await self._rubika.send(pong)
        logger.info({"event": "dispatcher.health_pong_sent", "request_id": msg.request_id})

    # ------------------------------------------------------------------
    # Admin: clearcache
    # ------------------------------------------------------------------

    async def handle_admin_clearcache(self, msg: AdminClearcache) -> None:
        """Handle ``admin.clearcache``: flush caches and send ``admin.ack``."""
        target = msg.target
        logger.info({"event": "dispatcher.admin_clearcache", "target": target})
        # The kharej worker does not maintain LRU or ISRC caches locally;
        # the ack is sent immediately with a note.
        ack = AdminAck(
            ts=datetime.now(tz=timezone.utc),
            acked_type="admin.clearcache",
            status="ok",
            detail=f"clearcache({target}): no local caches on kharej worker",
        )
        await self._rubika.send(ack)

    # ------------------------------------------------------------------
    # Admin: cookies update
    # ------------------------------------------------------------------

    async def handle_admin_cookies_update(self, msg: AdminCookiesUpdate) -> None:
        """Handle ``admin.cookies.update``: fetch from S2, replace local cookies.txt."""
        logger.info({"event": "dispatcher.admin_cookies_update", "s2_key": msg.s2_key})
        try:
            data: bytes = self._s2.get_object_bytes(msg.s2_key)

            # Verify SHA-256.
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if actual_sha256 != msg.sha256:
                raise ValueError(
                    f"SHA-256 mismatch: expected {msg.sha256!r}, got {actual_sha256!r}"
                )

            # Atomically replace cookies.txt.
            self._cookies_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._cookies_path.with_suffix(".tmp")
            tmp_path.write_bytes(data)
            tmp_path.replace(self._cookies_path)

            logger.info(
                {
                    "event": "dispatcher.admin_cookies_replaced",
                    "path": str(self._cookies_path),
                    "size": len(data),
                }
            )
            ack = AdminAck(
                ts=datetime.now(tz=timezone.utc),
                acked_type="admin.cookies.update",
                status="ok",
                detail=f"cookies.txt replaced ({len(data)} bytes)",
            )
        except Exception as exc:
            logger.exception(
                {"event": "dispatcher.admin_cookies_error", "exc": repr(exc)}
            )
            ack = AdminAck(
                ts=datetime.now(tz=timezone.utc),
                acked_type="admin.cookies.update",
                status="error",
                detail=repr(exc),
            )
        await self._rubika.send(ack)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, *, drain_timeout: float = 60.0) -> None:
        """Cancel-or-drain in-flight jobs.

        Waits up to *drain_timeout* seconds for jobs to finish naturally.
        After that, force-cancels any remaining tasks.  Cancelled tasks
        report ``error_code="cancelled"`` via :meth:`_run_job`'s exception
        handler.
        """
        if not self._tasks:
            return

        tasks = list(self._tasks.values())
        done, pending = await asyncio.wait(tasks, timeout=drain_timeout)

        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal: run a single job
    # ------------------------------------------------------------------

    async def _run_job(self, job: Job, downloader: Any) -> None:
        """Wrap one downloader invocation in timeout + error handling."""
        job_id = job.job_id
        try:
            s2_keys: list[S2ObjectRef] = await asyncio.wait_for(
                downloader.run(
                    job,
                    s2=self._s2,
                    progress=self._progress,
                    settings=self._settings,
                ),
                timeout=self._job_timeout,
            )
            logger.info(
                {
                    "event": "dispatcher.job_completed",
                    "job_id": job_id,
                    "platform": job.platform,
                }
            )
            await self._progress.report_completed(job_id, s2_keys=s2_keys)

        except asyncio.CancelledError:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": job.platform,
                    "error_code": "cancelled",
                }
            )
            try:
                await self._progress.report_failed(
                    job_id,
                    error_code="cancelled",
                    error_msg="cancelled by request or shutdown",
                )
            except Exception:
                pass  # Don't let reporting failure mask the cancellation
            raise

        except asyncio.TimeoutError:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": job.platform,
                    "error_code": "timeout",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="timeout",
                error_msg=f"exceeded {self._job_timeout}s",
            )

        except NotImplementedError as exc:
            logger.info(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": job.platform,
                    "error_code": "not_implemented",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="not_implemented",
                error_msg=str(exc),
            )

        except Exception as exc:
            logger.exception(
                {
                    "event": "dispatcher.job_failed",
                    "job_id": job_id,
                    "platform": job.platform,
                    "error_code": "error",
                }
            )
            await self._progress.report_failed(
                job_id,
                error_code="error",
                error_msg=repr(exc),
            )

        finally:
            self._tasks.pop(job_id, None)
