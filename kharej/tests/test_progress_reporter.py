"""Tests for kharej/progress_reporter.py (Step 5c — Progress Reporter).

Verifies throttling, percent-delta gating, immediate terminal messages, and
concurrency safety.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from kharej.contracts import (
    JobCompleted,
    JobFailed,
    JobProgress,
    S2ObjectRef,
)
from kharej.progress_reporter import ProgressReporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_ID = "job-test-1234"
_NOW = datetime.now(tz=timezone.utc)


def _progress(
    *,
    job_id: str = _JOB_ID,
    phase: str = "downloading",
    percent: int | None = 50,
    **kwargs: Any,
) -> JobProgress:
    return JobProgress(
        ts=_NOW,
        job_id=job_id,
        phase=phase,
        percent=percent,
        **kwargs,
    )


def _completed(*, job_id: str = _JOB_ID) -> JobCompleted:
    return JobCompleted(
        ts=_NOW,
        job_id=job_id,
        parts=[
            S2ObjectRef(
                key=f"media/{job_id}/track.flac",
                size=1024,
                mime="audio/flac",
                sha256="a" * 64,
            )
        ],
    )


def _failed(*, job_id: str = _JOB_ID) -> JobFailed:
    return JobFailed(
        ts=_NOW,
        job_id=job_id,
        error_code="internal_error",
        message="Something went wrong",
        retryable=True,
    )


def _reporter(*, throttle_sec: float = 3.0, min_percent_delta: int = 1) -> tuple[ProgressReporter, list[Any]]:
    sent: list[Any] = []

    async def fake_send(msg: Any) -> None:
        sent.append(msg)

    return ProgressReporter(fake_send, throttle_sec=throttle_sec, min_percent_delta=min_percent_delta), sent


# ---------------------------------------------------------------------------
# Test 1: first report is sent immediately (elapsed since epoch >> throttle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_report_sent_immediately() -> None:
    reporter, sent = _reporter(throttle_sec=3.0)
    await reporter.report(_progress(percent=10))
    assert len(sent) == 1
    assert isinstance(sent[0], JobProgress)


# ---------------------------------------------------------------------------
# Test 2: second report within throttle window is suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_report_within_window_suppressed() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)  # very long throttle
    await reporter.report(_progress(percent=10))
    await reporter.report(_progress(percent=20))
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Test 3: report after throttle window is sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_after_window_sent() -> None:
    reporter, sent = _reporter(throttle_sec=0.05)
    await reporter.report(_progress(percent=10))
    await asyncio.sleep(0.1)  # wait for throttle to expire
    await reporter.report(_progress(percent=20))
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Test 4: percent change below min_percent_delta is suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_percent_change_suppressed() -> None:
    reporter, sent = _reporter(throttle_sec=0.0, min_percent_delta=5)
    await reporter.report(_progress(percent=10))
    # Only 2% change — below min_percent_delta=5
    await reporter.report(_progress(percent=12))
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Test 5: percent change >= min_percent_delta is sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sufficient_percent_change_sent() -> None:
    reporter, sent = _reporter(throttle_sec=0.0, min_percent_delta=5)
    await reporter.report(_progress(percent=10))
    await reporter.report(_progress(percent=15))  # exactly 5% change
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Test 6: burst of 100 callbacks within 1s produces ≤1 outgoing message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_100_callbacks_produces_at_most_1_message() -> None:
    reporter, sent = _reporter(throttle_sec=3.0)
    # Fire 100 progress reports rapidly
    for i in range(100):
        await reporter.report(_progress(percent=i % 101))
    assert len(sent) <= 1


# ---------------------------------------------------------------------------
# Test 7: complete() sends immediately and bypasses throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sends_immediately() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)  # very long throttle
    # Even without any progress sends, complete must go through
    await reporter.complete(_completed())
    assert len(sent) == 1
    assert isinstance(sent[0], JobCompleted)


# ---------------------------------------------------------------------------
# Test 8: complete() cleans up job state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_cleans_up_state() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)
    await reporter.report(_progress(percent=50))
    assert _JOB_ID in reporter._states

    await reporter.complete(_completed())
    assert _JOB_ID not in reporter._states


# ---------------------------------------------------------------------------
# Test 9: fail() sends immediately and bypasses throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_sends_immediately() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)
    await reporter.fail(_failed())
    assert len(sent) == 1
    assert isinstance(sent[0], JobFailed)


# ---------------------------------------------------------------------------
# Test 10: fail() cleans up job state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_cleans_up_state() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)
    await reporter.report(_progress(percent=30))
    assert _JOB_ID in reporter._states

    await reporter.fail(_failed())
    assert _JOB_ID not in reporter._states


# ---------------------------------------------------------------------------
# Test 11: multiple jobs are throttled independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_jobs_throttled_independently() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)

    await reporter.report(_progress(job_id="job-A", percent=10))
    await reporter.report(_progress(job_id="job-B", percent=10))
    await reporter.report(_progress(job_id="job-A", percent=20))  # throttled
    await reporter.report(_progress(job_id="job-B", percent=20))  # throttled

    assert len(sent) == 2  # one per job (the first)


# ---------------------------------------------------------------------------
# Test 12: None percent bypasses percent-delta check (uses time throttle only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_percent_uses_time_throttle_only() -> None:
    reporter, sent = _reporter(throttle_sec=0.0, min_percent_delta=10)
    # phase-only progress (no percent)
    await reporter.report(_progress(percent=None, phase="uploading"))
    await reporter.report(_progress(percent=None, phase="uploading"))
    # With throttle_sec=0.0, both should send (percent filter skipped for None)
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Test 13: reset_job clears state so next report sends immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_job_clears_state() -> None:
    reporter, sent = _reporter(throttle_sec=100.0)
    await reporter.report(_progress(percent=50))
    assert len(sent) == 1

    reporter.reset_job(_JOB_ID)
    assert _JOB_ID not in reporter._states

    # Next report should send again (state was cleared)
    await reporter.report(_progress(percent=51))
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Test 14: complete after complete does not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_twice_no_error() -> None:
    reporter, sent = _reporter(throttle_sec=3.0)
    await reporter.complete(_completed())
    await reporter.complete(_completed())
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Test 15: concurrent report() calls are safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_reports_are_safe() -> None:
    reporter, sent = _reporter(throttle_sec=0.0, min_percent_delta=1)

    # Fire 20 concurrent coroutines, each with a different percent
    tasks = [
        asyncio.create_task(reporter.report(_progress(percent=p)))
        for p in range(20)
    ]
    await asyncio.gather(*tasks)

    # No assertion on exact count — just ensure no exception was raised
    assert isinstance(sent, list)
