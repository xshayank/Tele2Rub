"""Tests for the YouTube downloader format-not-available retry logic.

Covers:
- _run_ytdlp_subprocess retries once with a fallback format when yt-dlp outputs
  "Requested format is not available" and exits non-zero.
- _run_ytdlp_subprocess does NOT retry on unrelated errors.
- Retry uses the audio fallback for audio qualities and the video fallback otherwise.
- _replace_format_arg helper correctly replaces the --format value in a command.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kharej.downloaders.youtube import (
    _FALLBACK_AUDIO_FORMAT,
    _FALLBACK_VIDEO_FORMAT,
    _FORMAT_NOT_AVAILABLE_MSG,
    _replace_format_arg,
    _run_ytdlp_subprocess,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_ID = "test-job-retry-0001"


def _make_event_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    return loop


def _noop_progress(_percent: int, _speed: Any) -> Any:
    """A coroutine factory that does nothing (used to satisfy the interface)."""

    async def _inner() -> None:
        pass

    return _inner()


def _make_process(*, output_lines: list[str], returncode: int) -> MagicMock:
    """Build a mock subprocess.Popen process."""
    proc = MagicMock()
    proc.stdout = iter(line + "\n" for line in output_lines)
    proc.returncode = returncode
    proc.wait.return_value = None
    return proc


# ---------------------------------------------------------------------------
# _replace_format_arg
# ---------------------------------------------------------------------------


def test_replace_format_arg_replaces_value() -> None:
    cmd = ["yt-dlp", "--format", "bestvideo+bestaudio", "--output", "%(title)s.%(ext)s"]
    result = _replace_format_arg(cmd, "bv*+ba/b")
    assert result == ["yt-dlp", "--format", "bv*+ba/b", "--output", "%(title)s.%(ext)s"]


def test_replace_format_arg_original_unchanged() -> None:
    cmd = ["yt-dlp", "--format", "old", "url"]
    _replace_format_arg(cmd, "new")
    assert cmd[2] == "old"  # original list is not mutated


def test_replace_format_arg_no_format_flag() -> None:
    cmd = ["yt-dlp", "url"]
    result = _replace_format_arg(cmd, "new")
    assert result == cmd  # returned unchanged


# ---------------------------------------------------------------------------
# _run_ytdlp_subprocess — retry on "Requested format is not available"
# ---------------------------------------------------------------------------


def test_retry_on_format_not_available_video() -> None:
    """First run fails with format error; second run succeeds → no exception raised."""
    first_proc = _make_process(
        output_lines=[f"ERROR: [youtube] abcXYZ: {_FORMAT_NOT_AVAILABLE_MSG}"],
        returncode=1,
    )
    second_proc = _make_process(output_lines=["[download] 100%"], returncode=0)

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bv*[height<=1080]+ba/b[height<=1080]/b", "url"]
        with patch("subprocess.Popen", side_effect=[first_proc, second_proc]):
            # Should succeed without raising
            _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=False)
    finally:
        loop.close()


def test_retry_uses_video_fallback_format() -> None:
    """Retry command must use _FALLBACK_VIDEO_FORMAT when _is_audio=False."""
    first_proc = _make_process(
        output_lines=[f"ERROR: {_FORMAT_NOT_AVAILABLE_MSG}"],
        returncode=1,
    )
    second_proc = _make_process(output_lines=[], returncode=0)

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bv*+ba/b", "url"]
        popen_calls: list[list[str]] = []

        def capture_popen(call_cmd, **kwargs):  # type: ignore[override]
            popen_calls.append(list(call_cmd))
            return [first_proc, second_proc][len(popen_calls) - 1]

        with patch("subprocess.Popen", side_effect=capture_popen):
            _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=False)

        assert len(popen_calls) == 2
        retry_cmd = popen_calls[1]
        fmt_idx = retry_cmd.index("--format")
        assert retry_cmd[fmt_idx + 1] == _FALLBACK_VIDEO_FORMAT
    finally:
        loop.close()


def test_retry_uses_audio_fallback_format() -> None:
    """Retry command must use _FALLBACK_AUDIO_FORMAT when _is_audio=True."""
    first_proc = _make_process(
        output_lines=[f"ERROR: {_FORMAT_NOT_AVAILABLE_MSG}"],
        returncode=1,
    )
    second_proc = _make_process(output_lines=[], returncode=0)

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bestaudio/best", "url"]
        popen_calls: list[list[str]] = []

        def capture_popen(call_cmd, **kwargs):  # type: ignore[override]
            popen_calls.append(list(call_cmd))
            return [first_proc, second_proc][len(popen_calls) - 1]

        with patch("subprocess.Popen", side_effect=capture_popen):
            _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=True)

        assert len(popen_calls) == 2
        retry_cmd = popen_calls[1]
        fmt_idx = retry_cmd.index("--format")
        assert retry_cmd[fmt_idx + 1] == _FALLBACK_AUDIO_FORMAT
    finally:
        loop.close()


def test_no_retry_on_unrelated_error() -> None:
    """Unrelated non-zero exit (e.g., network error) must NOT trigger a retry."""
    only_proc = _make_process(
        output_lines=["ERROR: [youtube] abcXYZ: HTTP Error 403: Forbidden"],
        returncode=1,
    )

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bv*+ba/b", "url"]
        popen_calls: list[Any] = []

        def capture_popen(call_cmd, **kwargs):  # type: ignore[override]
            popen_calls.append(call_cmd)
            return only_proc

        with pytest.raises(RuntimeError, match="non-zero status"):
            with patch("subprocess.Popen", side_effect=capture_popen):
                _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=False)

        # Exactly one Popen call — no retry
        assert len(popen_calls) == 1
    finally:
        loop.close()


def test_retry_both_fail_raises() -> None:
    """If both the first run and the retry fail, RuntimeError is raised."""
    first_proc = _make_process(
        output_lines=[f"ERROR: {_FORMAT_NOT_AVAILABLE_MSG}"],
        returncode=1,
    )
    second_proc = _make_process(
        output_lines=["ERROR: retry also failed"],
        returncode=1,
    )

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bv*+ba/b", "url"]
        with patch("subprocess.Popen", side_effect=[first_proc, second_proc]):
            with pytest.raises(RuntimeError, match="format fallback retry"):
                _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=False)
    finally:
        loop.close()


def test_retry_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    """Retry must emit an INFO log line with event=youtube.retry_format."""
    import logging

    first_proc = _make_process(
        output_lines=[f"ERROR: {_FORMAT_NOT_AVAILABLE_MSG}"],
        returncode=1,
    )
    second_proc = _make_process(output_lines=[], returncode=0)

    loop = _make_event_loop()
    try:
        cmd = ["yt-dlp", "--format", "bv*+ba/b", "url"]
        with caplog.at_level(logging.INFO, logger="kharej.downloaders.youtube"):
            with patch("subprocess.Popen", side_effect=[first_proc, second_proc]):
                _run_ytdlp_subprocess(cmd, _JOB_ID, loop, _noop_progress, _is_audio=False)

        assert any(
            isinstance(r.msg, dict) and r.msg.get("event") == "youtube.retry_format"
            for r in caplog.records
        )
    finally:
        loop.close()
