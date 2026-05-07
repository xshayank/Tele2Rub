"""Instagram downloader adapter for the Kharej VPS worker."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from kharej.contracts import S2ObjectRef, make_media_key
from kharej.downloaders.common import safe_filename
from kharej.downloaders.youtube import _find_ytdlp, _resolve_cookies_path, _run_ytdlp_subprocess

if TYPE_CHECKING:
    from kharej.dispatcher import Job
    from kharej.progress_reporter import ProgressReporter
    from kharej.s2_client import S2Client
    from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.downloaders.instagram")


def _build_command(
    ytdlp_bin: str,
    url: str,
    outtmpl: str,
    cookies_path: str | None,
) -> list[str]:
    cmd = [
        ytdlp_bin,
        "-S",
        "proto,ext:mp4,res,br",
        "--output",
        outtmpl,
        "--no-playlist",
        "--progress",
        "--newline",
        "--no-warnings",
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd.append(url)
    return cmd


class InstagramDownloader:
    """Download a single Instagram video and upload it to Arvan S2."""

    platform: ClassVar[str] = "instagram"

    async def run(
        self,
        job: "Job",
        *,
        s2: "S2Client",
        progress: "ProgressReporter",
        settings: "KharejSettings",
    ) -> list[S2ObjectRef]:
        loop = asyncio.get_running_loop()

        cookies_path = _resolve_cookies_path(settings)
        ytdlp_bin = _find_ytdlp(settings)

        with tempfile.TemporaryDirectory(prefix=f"kharej_ig_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            outtmpl = str(tmp_dir / "%(title)s.%(ext)s")
            cmd = _build_command(ytdlp_bin, job.url, outtmpl, cookies_path)

            logger.info(
                {
                    "event": "instagram.download_start",
                    "job_id": job.job_id,
                    "cookies": bool(cookies_path),
                }
            )

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

            files = [p for p in tmp_dir.iterdir() if p.is_file()]
            if not files:
                raise RuntimeError("yt-dlp produced no output file")

            local_path = max(files, key=lambda p: p.stat().st_mtime)
            ext = local_path.suffix.lstrip(".")
            stem = safe_filename(local_path.stem)
            s2_filename = f"{stem}.{ext}" if ext else stem
            s2_key = make_media_key(job.job_id, s2_filename)

            await progress.report_progress(job.job_id, 100, phase="uploading")
            ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, local_path, s2_key)
            return [ref]
