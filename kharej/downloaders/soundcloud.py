"""SoundCloud downloader adapter for the Kharej VPS worker.

Uses yt-dlp's SoundCloud extractor (via
``rubetunes.providers.soundcloud.download_soundcloud``) to download a track
or playlist URL and upload the result to Arvan S2.

Flow
----
1. Validate the SoundCloud URL from *job.url* via ``parse_soundcloud_url``.
2. Download the audio file via ``download_soundcloud`` (runs yt-dlp as a
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

logger = logging.getLogger("kharej.downloaders.soundcloud")


class SoundcloudDownloader:
    """Download a SoundCloud track and upload it to Arvan S2."""

    platform: ClassVar[str] = "soundcloud"

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
            from rubetunes.providers.soundcloud import (  # noqa: PLC0415
                download_soundcloud,
                parse_soundcloud_url,
            )
        except ImportError as exc:
            raise RuntimeError(
                "rubetunes.providers.soundcloud is not importable; "
                "ensure the rubetunes package is installed"
            ) from exc

        sc_url: str | None = parse_soundcloud_url(job.url)
        if not sc_url:
            raise ValueError(f"Could not parse SoundCloud URL from: {job.url!r}")

        logger.info({"event": "soundcloud.download_start", "job_id": job.job_id, "url": sc_url})
        await progress.report_progress(job.job_id, 0, phase="downloading")

        ytdlp_bin: str = settings.get("ytdlp_bin") or "yt-dlp"

        with tempfile.TemporaryDirectory(prefix=f"kharej_sc_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            safe_name = safe_filename(sc_url.rstrip("/").rsplit("/", 1)[-1] or "soundcloud_track")

            audio_path: Path = await download_soundcloud(
                url=sc_url,
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
                    "event": "soundcloud.upload_start",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "size": audio_path.stat().st_size,
                }
            )
            ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, audio_path, s2_key)
            logger.info(
                {
                    "event": "soundcloud.upload_done",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "sha256": ref.sha256,
                }
            )

            await progress.report_progress(job.job_id, 100, phase="uploading")
            return [ref]
