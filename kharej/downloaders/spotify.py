"""Spotify downloader adapter for the Kharej VPS worker.

Wraps the existing ``spotify_dl`` / ``rubetunes.downloader`` waterfall
(Spotify GraphQL → Tidal Alt → Deezer → YouTube Music fallback) and exposes
a clean async interface for the dispatcher.

Flow
----
1. Parse the Spotify track ID from *job.url* via ``parse_spotify_track_id``.
2. Fetch track metadata (title, artists, cover URL) via ``get_track_info``.
3. Download the audio file via ``download_track`` (async, wraps a thread).
4. Upload the audio to ``media/{job_id}/{safe_title}.{ext}``.
5. If a cover URL is available, download the thumbnail and upload it to
   ``thumbs/{job_id}.jpg``.
6. Return ``list[S2ObjectRef]`` with the media ref and, if applicable, the
   thumbnail ref.

Read-only usage
---------------
This module imports ``spotify_dl`` (the top-level shim) **read-only** — it
never monkeypatches or modifies any state in that module.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from kharej.contracts import S2ObjectRef, make_media_key, make_thumb_key
from kharej.downloaders.common import safe_filename

if TYPE_CHECKING:
    from kharej.dispatcher import Job
    from kharej.progress_reporter import ProgressReporter
    from kharej.s2_client import S2Client
    from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.downloaders.spotify")


class SpotifyDownloader:
    """Download a single Spotify track and upload it (+ thumbnail) to Arvan S2."""

    platform: ClassVar[str] = "spotify"

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Resolve, download, upload.  Returns 1–2 :class:`~kharej.contracts.S2ObjectRef` objects."""
        # ------------------------------------------------------------------
        # Import spotify_dl shim (read-only)
        # ------------------------------------------------------------------
        try:
            import spotify_dl as _spodl  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "spotify_dl shim is not importable; ensure the rubetunes package is installed"
            ) from exc

        # ------------------------------------------------------------------
        # Parse track ID
        # ------------------------------------------------------------------
        track_id: str | None = await asyncio.to_thread(_spodl.parse_spotify_track_id, job.url)
        if not track_id:
            raise ValueError(f"Could not parse Spotify track ID from URL: {job.url!r}")

        # ------------------------------------------------------------------
        # Fetch track metadata (blocking network call)
        # ------------------------------------------------------------------
        logger.info({"event": "spotify.fetch_info", "job_id": job.job_id, "track_id": track_id})
        await progress.report_progress(job.job_id, 0, phase="downloading")

        info: dict = await asyncio.to_thread(_spodl.get_track_info, track_id)

        title: str = info.get("title") or "Unknown"
        artists: list[str] = info.get("artists") or []
        cover_url: str | None = info.get("cover_url") or info.get("cover")

        logger.info(
            {
                "event": "spotify.track_info",
                "job_id": job.job_id,
                "title": title,
                "artists": artists,
            }
        )

        # ------------------------------------------------------------------
        # Download (the existing waterfall is already async)
        # ------------------------------------------------------------------
        ytdlp_bin: str = "yt-dlp"  # could be made configurable

        with tempfile.TemporaryDirectory(prefix=f"kharej_sp_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)

            logger.info({"event": "spotify.download_start", "job_id": job.job_id})
            audio_path: Path = await _spodl.download_track(info, tmp_dir, ytdlp_bin)

            await progress.report_progress(job.job_id, 90, phase="uploading")

            # ------------------------------------------------------------------
            # Upload audio
            # ------------------------------------------------------------------
            ext = audio_path.suffix.lstrip(".")
            artist_part = safe_filename(", ".join(artists)) if artists else ""
            title_part = safe_filename(title)
            stem = f"{artist_part}_-_{title_part}" if artist_part else title_part
            s2_filename = f"{stem}.{ext}" if ext else stem
            s2_key = make_media_key(job.job_id, s2_filename)

            logger.info(
                {
                    "event": "spotify.upload_start",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "size": audio_path.stat().st_size,
                }
            )
            audio_ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, audio_path, s2_key)
            logger.info(
                {
                    "event": "spotify.upload_done",
                    "job_id": job.job_id,
                    "key": s2_key,
                    "sha256": audio_ref.sha256,
                }
            )

            refs: list[S2ObjectRef] = [audio_ref]

            # ------------------------------------------------------------------
            # Optional: download and upload thumbnail
            # ------------------------------------------------------------------
            if cover_url:
                try:
                    thumb_ref = await _upload_thumbnail(
                        cover_url=cover_url,
                        job_id=job.job_id,
                        tmp_dir=tmp_dir,
                        s2=s2,
                    )
                    if thumb_ref is not None:
                        refs.append(thumb_ref)
                except Exception as exc:
                    logger.warning(
                        {
                            "event": "spotify.thumb_failed",
                            "job_id": job.job_id,
                            "error": repr(exc),
                        }
                    )

            await progress.report_progress(job.job_id, 100, phase="uploading")
            return refs


# ---------------------------------------------------------------------------
# Thumbnail helper
# ---------------------------------------------------------------------------


async def _upload_thumbnail(
    cover_url: str,
    job_id: str,
    tmp_dir: Path,
    s2: S2Client,
) -> S2ObjectRef | None:
    """Download *cover_url* and upload it to ``thumbs/{job_id}.jpg``.

    Returns ``None`` on any error (caller logs and continues).
    """
    try:
        import urllib.request  # noqa: PLC0415

        thumb_path = tmp_dir / f"{job_id}_thumb.jpg"
        await asyncio.to_thread(urllib.request.urlretrieve, cover_url, str(thumb_path))

        if not thumb_path.exists() or thumb_path.stat().st_size == 0:
            return None

        s2_key = make_thumb_key(job_id)
        ref: S2ObjectRef = await asyncio.to_thread(
            s2.upload_file,
            thumb_path,
            s2_key,
            content_type="image/jpeg",
        )
        logger.info({"event": "spotify.thumb_uploaded", "job_id": job_id, "key": s2_key})
        return ref
    except Exception as exc:
        logger.debug("Thumbnail upload failed: %s", exc)
        return None
