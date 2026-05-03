"""YouTube downloader adapter for the Kharej VPS worker.

Downloads a single video/audio track via the ``yt-dlp`` executable,
uploads the result to Arvan S2, and emits progress through
:class:`~kharej.progress_reporter.ProgressReporter`.

Progress parsing
----------------
``yt-dlp`` is invoked with ``--progress --newline`` so each progress update
appears on its own stdout line.  Lines matching ``[download]  XX.X% of ...``
are parsed via :data:`_PERCENT_RE` and scheduled back to the async event loop
via ``asyncio.run_coroutine_threadsafe``.

S2 key
------
``media/{job_id}/{safe_title}.{ext}``
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from kharej.contracts import S2ObjectRef, make_media_key
from kharej.downloaders.common import safe_filename

if TYPE_CHECKING:
    from kharej.dispatcher import Job
    from kharej.progress_reporter import ProgressReporter
    from kharej.s2_client import S2Client
    from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.downloaders.youtube")

_PERCENT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)\s*%")
_SPEED_RE = re.compile(r"at\s+([\d.]+\s*\S+/s)")

# Hardcoded cookies path — primary location
_HARDCODED_COOKIES_PATH = Path("/root/newrube/RubeTunes/kharej/cookies.txt")


def _find_ytdlp(settings: "KharejSettings") -> str:
    """Return the path to the yt-dlp executable."""
    # 1. Explicit override from settings
    explicit = settings.get("ytdlp_path")
    if explicit and Path(explicit).is_file():
        return str(explicit)

    # 2. On PATH
    found = shutil.which("yt-dlp")
    if found:
        return found

    # 3. Common fallback locations
    for candidate in [
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        str(Path(__file__).parent.parent.parent / "yt-dlp"),  # repo root
        str(Path(__file__).parent.parent / "yt-dlp"),         # kharej/
    ]:
        if Path(candidate).is_file():
            return candidate

    raise RuntimeError(
        "yt-dlp executable not found. Install it with: pip install yt-dlp "
        "or set KHAREJ_YTDLP_PATH to the binary path."
    )


_YOUTUBE_FORMATS: dict[str, str] = {
    "mp3": "bestaudio/best",
    "mp4": "bestvideo+bestaudio/best",
    "mp4-1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio/bestvideo[height<=1080]+bestaudio/best",
    "mp4-720p": "bestvideo[height<=720][ext=mp4]+bestaudio/bestvideo[height<=720]+bestaudio/best",
    "mp4-480p": "bestvideo[height<=480][ext=mp4]+bestaudio/bestvideo[height<=480]+bestaudio/best",
    "mp4-360p": "bestvideo[height<=360][ext=mp4]+bestaudio/bestvideo[height<=360]+bestaudio/best",
}

_VALID_QUALITIES: frozenset[str] = frozenset(_YOUTUBE_FORMATS)

_AUDIO_FORMAT = "mp3"


def _resolve_format(quality: str) -> str:
    """Map a quality hint string to a yt-dlp format selector."""
    return _YOUTUBE_FORMATS.get(quality.lower(), _YOUTUBE_FORMATS[_AUDIO_FORMAT])


def _is_audio_quality(quality: str) -> bool:
    """Return True if *quality* implies audio-only extraction."""
    return quality.lower() == "mp3"


def _build_command(
    ytdlp_bin: str,
    url: str,
    outtmpl: str,
    quality: str,
    cookies_path: str | None,
) -> list[str]:
    """Build the yt-dlp CLI command list."""
    fmt = _resolve_format(quality)
    cmd = [
        ytdlp_bin,
        "--format", fmt,
        "--output", outtmpl,
        "--no-playlist",
        "--progress",
        "--newline",
        "--no-warnings",
        # Enable remote JS challenge solver for YouTube bot detection bypass
        "--remote-components", "ejs:npm",
    ]
    if cookies_path and Path(cookies_path).is_file():
        cmd += ["--cookies", cookies_path]
        logger.info({"event": "youtube.cookies_applied", "path": cookies_path})
    elif cookies_path:
        logger.warning({
            "event": "youtube.cookies_missing",
            "path": cookies_path,
            "msg": "cookies file not found, proceeding without cookies",
        })

    if _is_audio_quality(quality):
        cmd += [
            "--extract-audio",
            "--audio-format", _AUDIO_FORMAT,
            "--audio-quality", "0",
        ]

    cmd.append(url)
    return cmd


def _run_ytdlp_subprocess(
    cmd: list[str],
    job_id: str,
    loop: asyncio.AbstractEventLoop,
    progress_coro_factory,
) -> None:
    """Run yt-dlp as a subprocess, parse progress lines, call progress_coro_factory."""
    logger.debug({"event": "youtube.subprocess_cmd", "cmd": cmd})

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            output_lines.append(line)
            logger.debug({"event": "youtube.ytdlp_output", "line": line})

        m = _PERCENT_RE.search(line)
        if m:
            percent = min(100, int(float(m.group(1))))
            speed_m = _SPEED_RE.search(line)
            speed = speed_m.group(1) if speed_m else None
            asyncio.run_coroutine_threadsafe(
                progress_coro_factory(percent, speed),
                loop,
            )

    process.wait()
    if process.returncode != 0:
        stderr_tail = "\n".join(output_lines[-20:])
        raise RuntimeError(
            f"yt-dlp exited with code {process.returncode}:\n{stderr_tail}"
        )


def _resolve_cookies_path(settings: "KharejSettings") -> str | None:
    """Resolve the cookies.txt path using a priority chain.

    Priority:
    1. Hardcoded path: /root/newrube/RubeTunes/kharej/cookies.txt
    2. settings key ``cookies_path`` (set via KHAREJ_COOKIES_PATH env var or admin API)
    3. Auto-discovery: kharej/cookies.txt, then repo root cookies.txt
    """
    # 1. Hardcoded primary path
    if _HARDCODED_COOKIES_PATH.is_file():
        logger.info({
            "event": "youtube.cookies_hardcoded",
            "path": str(_HARDCODED_COOKIES_PATH),
        })
        return str(_HARDCODED_COOKIES_PATH)

    # 2. Settings / env var
    configured = settings.get("cookies_path") or os.environ.get("KHAREJ_COOKIES_PATH")
    if configured and Path(configured).is_file():
        return str(configured)
    if configured:
        logger.warning({
            "event": "youtube.cookies_configured_missing",
            "path": configured,
        })

    # 3. Auto-discovery
    _kharej_dir = Path(__file__).parent.parent   # kharej/
    _repo_root = _kharej_dir.parent              # repo root
    for _candidate in [
        _kharej_dir / "cookies.txt",
        _repo_root / "cookies.txt",
    ]:
        if _candidate.is_file():
            logger.info({
                "event": "youtube.cookies_autodiscovered",
                "path": str(_candidate),
            })
            return str(_candidate)

    logger.warning({"event": "youtube.cookies_not_found", "msg": "No cookies.txt found anywhere"})
    return None


class YoutubeDownloader:
    """Download a single YouTube video/audio track and upload it to Arvan S2."""

    platform: ClassVar[str] = "youtube"

    async def run(
        self,
        job: "Job",
        *,
        s2: "S2Client",
        progress: "ProgressReporter",
        settings: "KharejSettings",
    ) -> list[S2ObjectRef]:
        loop = asyncio.get_running_loop()

        quality: str = job.quality or settings.get("default_audio_quality") or "mp3"
        if quality.lower() not in _VALID_QUALITIES:
            quality = "mp3"

        cookies_path = _resolve_cookies_path(settings)

        ytdlp_bin = _find_ytdlp(settings)

        with tempfile.TemporaryDirectory(prefix=f"kharej_yt_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            outtmpl = str(tmp_dir / "%(title)s.%(ext)s")

            cmd = _build_command(ytdlp_bin, job.url, outtmpl, quality, cookies_path)

            logger.info({
                "event": "youtube.download_start",
                "job_id": job.job_id,
                "quality": quality,
                "cookies": bool(cookies_path),
            })

            async def _make_progress(percent: int, speed: str | None) -> None:
                await progress.report_progress(
                    job.job_id, percent, phase="downloading", speed=speed
                )

            await asyncio.to_thread(
                _run_ytdlp_subprocess,
                cmd,
                job.job_id,
                loop,
                _make_progress,
            )

            # Find the downloaded file — prefer most-recently-modified media file
            _MEDIA_EXTS = {
                ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".webm",
                ".mp4", ".mkv", ".avi", ".mov",
            }
            files = [p for p in tmp_dir.iterdir() if p.is_file()]
            if not files:
                raise RuntimeError("yt-dlp produced no output file")
            media_files = [p for p in files if p.suffix.lower() in _MEDIA_EXTS]
            candidates = media_files or files
            local_path = max(candidates, key=lambda p: p.stat().st_mtime)
            ext = local_path.suffix.lstrip(".")

            stem = safe_filename(local_path.stem)
            s2_filename = f"{stem}.{ext}" if ext else stem
            s2_key = make_media_key(job.job_id, s2_filename)

            logger.info({
                "event": "youtube.upload_start",
                "job_id": job.job_id,
                "key": s2_key,
                "size": local_path.stat().st_size,
            })
            await progress.report_progress(job.job_id, 100, phase="uploading")

            ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, local_path, s2_key)
            logger.info({
                "event": "youtube.upload_done",
                "job_id": job.job_id,
                "key": s2_key,
            })
            return [ref]
