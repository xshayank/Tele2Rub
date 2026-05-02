"""Progress reporter for the Kharej VPS worker.

Coalesces download/upload progress callbacks and emits ``job.progress``
messages back to the Iran VPS via a caller-supplied async send function.

Throttling rules (per job):

- At most **1** ``job.progress`` message every ``throttle_sec`` seconds
  (default 3 s).
- Additionally, if *percent* is provided, only emit when the percent value
  has changed by at least ``min_percent_delta`` points (default 1 %).

Both conditions must be satisfied simultaneously for a progress message to be
emitted.

Terminal messages (``job.completed``, ``job.failed``) are **always** sent
immediately, bypassing the throttle, and their per-job state is cleaned up.

The reporter is safe to call from any coroutine: an ``asyncio.Lock`` guards
all per-job state mutations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from kharej.contracts import JobAccepted, JobCompleted, JobFailed, JobProgress

logger = logging.getLogger("kharej.progress_reporter")

# ---------------------------------------------------------------------------
# Per-job state
# ---------------------------------------------------------------------------


@dataclass
class _JobState:
    last_sent_at: float = field(default=0.0)
    last_percent: int | None = field(default=None)


# ---------------------------------------------------------------------------
# ProgressReporter
# ---------------------------------------------------------------------------


class ProgressReporter:
    """Per-job throttled progress reporter.

    Parameters
    ----------
    send:
        Async callable that accepts a Pydantic ``BaseModel`` and delivers it
        to the Iran VPS (typically ``RubikaClient.send``).
    throttle_sec:
        Minimum seconds between consecutive ``job.progress`` sends for the
        same job (default 3 s).
    min_percent_delta:
        Minimum change in *percent* required to emit a progress message when
        *percent* is provided (default 1 %).
    """

    def __init__(
        self,
        send: Callable[[BaseModel], Awaitable[None]],
        *,
        throttle_sec: float = 3.0,
        min_percent_delta: int = 1,
    ) -> None:
        self._send = send
        self._throttle_sec = throttle_sec
        self._min_percent_delta = min_percent_delta
        self._states: dict[str, _JobState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def report(self, msg: JobProgress) -> None:
        """Conditionally emit *msg* subject to time and percent-delta throttle.

        The message is dropped silently if:
        - fewer than ``throttle_sec`` seconds have elapsed since the last
          send for this job, **or**
        - *percent* is set and has not changed by at least
          ``min_percent_delta`` points since the last send.
        """
        job_id = msg.job_id or ""
        now = time.monotonic()

        should_send = False
        async with self._lock:
            state = self._states.setdefault(job_id, _JobState())
            elapsed = now - state.last_sent_at

            if elapsed >= self._throttle_sec:
                # Time gate passed — check percent-delta gate.
                if (
                    msg.percent is None
                    or state.last_percent is None
                    or abs(msg.percent - state.last_percent) >= self._min_percent_delta
                ):
                    # Update state before releasing the lock so concurrent
                    # callers see the updated timestamp immediately.
                    state.last_sent_at = now
                    if msg.percent is not None:
                        state.last_percent = msg.percent
                    should_send = True

        if should_send:
            logger.debug(
                "Sending job.progress",
                extra={"event": "progress.sent", "job_id": job_id, "percent": msg.percent},
            )
            await self._send(msg)

    async def complete(self, msg: JobCompleted) -> None:
        """Send *msg* immediately (bypasses throttle) and clean up job state."""
        job_id = msg.job_id or ""
        async with self._lock:
            self._states.pop(job_id, None)
        logger.info(
            "Sending job.completed",
            extra={"event": "progress.completed", "job_id": job_id},
        )
        await self._send(msg)

    async def fail(self, msg: JobFailed) -> None:
        """Send *msg* immediately (bypasses throttle) and clean up job state."""
        job_id = msg.job_id or ""
        async with self._lock:
            self._states.pop(job_id, None)
        logger.info(
            "Sending job.failed",
            extra={"event": "progress.failed", "job_id": job_id, "error_code": msg.error_code},
        )
        await self._send(msg)

    def reset_job(self, job_id: str) -> None:
        """Remove per-job throttle state (call on job cancel or cleanup)."""
        self._states.pop(job_id, None)

    # ------------------------------------------------------------------
    # Convenience helpers (Step 6+)
    # ------------------------------------------------------------------

    async def report_progress(
        self,
        job_id: str,
        percent: int,
        *,
        phase: str = "downloading",
        speed: str | None = None,
        eta_sec: int | None = None,
    ) -> None:
        """Convenience wrapper: create a ``JobProgress`` and call :meth:`report`."""
        from datetime import datetime, timezone

        await self.report(
            JobProgress(
                ts=datetime.now(tz=timezone.utc),
                job_id=job_id,
                phase=phase,  # type: ignore[arg-type]
                percent=percent,
                speed=speed,
                eta_sec=eta_sec,
            )
        )

    async def report_accepted(
        self,
        job_id: str,
        *,
        worker_version: str,
        queue_position: int = 1,
    ) -> None:
        """Send a ``JobAccepted`` message immediately (bypasses throttle)."""
        from datetime import datetime, timezone

        logger.info(
            "Sending job.accepted",
            extra={"event": "progress.accepted", "job_id": job_id, "queue_position": queue_position},
        )
        await self._send(
            JobAccepted(
                ts=datetime.now(tz=timezone.utc),
                job_id=job_id,
                worker_version=worker_version,
                queue_position=queue_position,
            )
        )

    async def report_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        error_msg: str,
        retryable: bool = False,
    ) -> None:
        """Convenience wrapper: create a ``JobFailed`` and call :meth:`fail`."""
        from datetime import datetime, timezone

        await self.fail(
            JobFailed(
                ts=datetime.now(tz=timezone.utc),
                job_id=job_id,
                error_code=error_code,  # type: ignore[arg-type]
                message=error_msg,
                retryable=retryable,
            )
        )

    async def report_completed(
        self,
        job_id: str,
        *,
        s2_keys: list,
    ) -> None:
        """Convenience wrapper: create a ``JobCompleted`` and call :meth:`complete`."""
        from datetime import datetime, timezone

        await self.complete(
            JobCompleted(
                ts=datetime.now(tz=timezone.utc),
                job_id=job_id,
                parts=s2_keys,
            )
        )
