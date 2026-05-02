"""YouTube downloader adapter for the Kharej VPS worker.

Downloads a single video/audio track via ``yt-dlp``, uploads the result to
Arvan S2, and emits progress through :class:`~kharej.progress_reporter.ProgressReporter`.

Progress hook flow
------------------
``yt_dlp.YoutubeDL`` calls the progress hook on the download thread.  The hook
parses ``_percent_str`` / ``downloaded_bytes`` / ``total_bytes`` into an integer
0–100 and ``_speed_str`` into a human-readable speed string, then schedules a
``progress.report_progress()`` coroutine via ``asyncio.run_coroutine_threadsafe``.

S2 key
------
``media/{job_id}/{safe_title}.{ext}``
"""

from __future__ import annotations

import asyncio
import logging
import re
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

# ---------------------------------------------------------------------------
# Progress-hook helpers (pure functions, easily unit-tested)
# ---------------------------------------------------------------------------

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_percent(hook_info: dict) -> int:
    """Extract an integer 0–100 percent from a yt-dlp progress-hook dict.

    Tries ``_percent_str`` first, then falls back to
    ``downloaded_bytes / total_bytes`` arithmetic.  Returns 0 when neither
    value is available.
    """
    percent_str: str = hook_info.get("_percent_str", "")
    m = _PERCENT_RE.search(percent_str)
    if m:
        return min(100, int(float(m.group(1))))

    downloaded: int | None = hook_info.get("downloaded_bytes")
    total: int | None = hook_info.get("total_bytes") or hook_info.get("total_bytes_estimate")
    if downloaded is not None and total:
        return min(100, int(downloaded * 100 / total))

    return 0


def parse_speed(hook_info: dict) -> str | None:
    """Extract a human-readable speed string from a yt-dlp progress-hook dict.

    Returns ``None`` when the speed is not (yet) available.
    """
    speed_str: str | None = hook_info.get("_speed_str")
    if speed_str:
        return speed_str.strip()
    return None


def parse_eta(hook_info: dict) -> int | None:
    """Extract ETA in seconds from a yt-dlp progress-hook dict."""
    eta = hook_info.get("eta")
    if eta is not None:
        try:
            return int(eta)
        except (TypeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class YoutubeDownloader:
    """Download a single YouTube video/audio track and upload it to Arvan S2."""

    platform: ClassVar[str] = "youtube"

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Download, upload, return one :class:`~kharej.contracts.S2ObjectRef`."""
        loop = asyncio.get_running_loop()

        # ------------------------------------------------------------------
        # Build yt-dlp options
        # ------------------------------------------------------------------
        quality: str = job.quality or settings.get("default_audio_quality") or "bestaudio/best"
        cookies_path: str | None = settings.get("cookies_path")

        with tempfile.TemporaryDirectory(prefix=f"kharej_yt_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            outtmpl = str(tmp_dir / "%(title)s.%(ext)s")

            ydl_opts: dict = {
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "format": _resolve_format(quality),
                "progress_hooks": [],  # filled below
                "postprocessors": [],
            }

            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path

            # Add audio extraction postprocessor for audio-only quality hints.
            if _is_audio_quality(quality):
                ydl_opts["postprocessors"].append(
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": _audio_codec(quality),
                        "preferredquality": "0",
                    }
                )

            # ------------------------------------------------------------------
            # Progress hook (called from the yt-dlp download thread)
            # ------------------------------------------------------------------
            def _progress_hook(info: dict) -> None:
                if info.get("status") != "downloading":
                    return
                percent = parse_percent(info)
                speed = parse_speed(info)
                eta_sec = parse_eta(info)
                asyncio.run_coroutine_threadsafe(
                    progress.report_progress(
                        job.job_id,
                        percent,
                        phase="downloading",
                        speed=speed,
                        eta_sec=eta_sec,
                    ),
                    loop,
                )

            ydl_opts["progress_hooks"].append(_progress_hook)

            # ------------------------------------------------------------------
            # Download (blocking — run in executor thread)
            # ------------------------------------------------------------------
            logger.info(
                {
                    "event": "youtube.download_start",
                    "job_id": job.job_id,
                    "quality": quality,
                }
            )
            await asyncio.to_thread(_do_yt_download, job.url, ydl_opts)

            # ------------------------------------------------------------------
            # Find the downloaded file
            # ------------------------------------------------------------------
            # yt-dlp may produce the final file plus transient fragments/parts.
            # Prefer the most-recently-modified file with a recognised media
            # extension; fall back to the largest file if nothing matches.
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

            # ------------------------------------------------------------------
            # Derive S2 key
            # ------------------------------------------------------------------
            stem = safe_filename(local_path.stem)
            s2_filename = f"{stem}.{ext}" if ext else stem
            s2_key = make_media_key(job.job_id, s2_filename)

            # ------------------------------------------------------------------
            # Upload
            # ------------------------------------------------------------------
            logger.info(
                {
                    "event": "youtube.upload_start",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "size": local_path.stat().st_size,
                }
            )
            await progress.report_progress(job.job_id, 100, phase="uploading")

            ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, local_path, s2_key)
            logger.info(
                {
                    "event": "youtube.upload_done",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "sha256": ref.sha256,
                }
            )
            return [ref]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _do_yt_download(url: str, ydl_opts: dict) -> None:
    """Blocking yt-dlp download — intended to be called via ``asyncio.to_thread``."""
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed; add yt-dlp to requirements") from exc

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def _resolve_format(quality: str) -> str:
    """Map a quality hint string to a yt-dlp format selector."""
    _MAP = {
        "mp3": "bestaudio/best",
        "m4a": "bestaudio[ext=m4a]/bestaudio/best",
        "flac": "bestaudio/best",
        "ogg": "bestaudio/best",
        "opus": "bestaudio[ext=webm]/bestaudio/best",
        "bestaudio": "bestaudio/best",
        "best": "bestvideo+bestaudio/best",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    }
    return _MAP.get(quality.lower(), quality)


def _is_audio_quality(quality: str) -> bool:
    """Return True if *quality* implies audio-only extraction."""
    return quality.lower() in {"mp3", "m4a", "flac", "ogg", "opus", "bestaudio"}


def _audio_codec(quality: str) -> str:
    """Return the FFmpeg audio codec for *quality*."""
    _CODEC = {"mp3": "mp3", "m4a": "m4a", "flac": "flac", "ogg": "vorbis", "opus": "opus"}
    return _CODEC.get(quality.lower(), "mp3")
