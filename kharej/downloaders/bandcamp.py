"""Bandcamp downloader adapter for the Kharej VPS worker.

Uses yt-dlp's Bandcamp extractor (via
``rubetunes.providers.bandcamp.download_bandcamp``) to download a track or
album URL and upload the result to Arvan S2.

Flow
----
1. Validate the Bandcamp URL from *job.url* via ``parse_bandcamp_url``.
2. Download the audio file via ``download_bandcamp`` (runs yt-dlp as a
   subprocess; the call is already async-native).
3. Upload the audio to ``media/{job_id}/{safe_filename}.{ext}``.
4. Return a single :class:`~kharej.contracts.S2ObjectRef`.
"""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger("kharej.downloaders.bandcamp")


class BandcampDownloader:
    """Download a Bandcamp track or album and upload it to Arvan S2."""

    platform: ClassVar[str] = "bandcamp"

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Download, upload, return one :class:`~kharej.contracts.S2ObjectRef`."""
        try:
            from rubetunes.providers.bandcamp import (  # noqa: PLC0415
                download_bandcamp,
                parse_bandcamp_url,
            )
        except ImportError as exc:
            raise RuntimeError(
                "rubetunes.providers.bandcamp is not importable; "
                "ensure the rubetunes package is installed"
            ) from exc

        bc_url: str | None = parse_bandcamp_url(job.url)
        if not bc_url:
            raise ValueError(f"Could not parse Bandcamp URL from: {job.url!r}")

        logger.info({"event": "bandcamp.download_start", "job_id": job.job_id, "url": bc_url})
        await progress.report_progress(job.job_id, 0, phase="downloading")

        ytdlp_bin: str = settings.get("ytdlp_bin") or "yt-dlp"

        with tempfile.TemporaryDirectory(prefix=f"kharej_bc_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            safe_name = safe_filename(bc_url.rstrip("/").rsplit("/", 1)[-1] or "bandcamp_track")

            audio_path: Path = await download_bandcamp(
                url=bc_url,
                download_dir=tmp_dir,
                ytdlp_bin=ytdlp_bin,
                safe_name=safe_name,
            )

            await progress.report_progress(job.job_id, 90, phase="uploading")

            ext = audio_path.suffix.lstrip(".")
            s2_filename = f"{safe_filename(audio_path.stem)}.{ext}" if ext else safe_filename(audio_path.stem)
            s2_key = make_media_key(job.job_id, s2_filename)

            logger.info(
                {
                    "event": "bandcamp.upload_start",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "size": audio_path.stat().st_size,
                }
            )
            ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, audio_path, s2_key)
            logger.info(
                {
                    "event": "bandcamp.upload_done",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "sha256": ref.sha256,
                }
            )

            await progress.report_progress(job.job_id, 100, phase="uploading")
            return [ref]
