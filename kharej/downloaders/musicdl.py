"""musicdl downloader adapter for the Kharej VPS worker.

Wraps the ``rubetunes.providers.musicdl.MusicdlClient`` to search for and
download tracks from NetEase, QQ Music, and other supported sources.

The ``job.url`` field is treated as a search query string.  An optional
musicdl source filter can be passed via ``job.payload.format_hint``
(e.g. ``"NeteaseMusicClient"``).  The downloader picks the first result that
successfully downloads.

Flow
----
1. Read the search query from *job.url*.
2. (Optionally) restrict to a specific source via ``job.payload.format_hint``.
3. Search with :class:`~rubetunes.providers.musicdl.MusicdlClient`.
4. Try to download the first result; on failure, try the next result (up to
   ``_MAX_CANDIDATES`` attempts).
5. Upload the audio to ``media/{job_id}/{safe_filename}.{ext}``.
6. Return a single :class:`~kharej.contracts.S2ObjectRef`.
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

logger = logging.getLogger("kharej.downloaders.musicdl")

# Maximum number of search results to try before giving up.
_MAX_CANDIDATES = 3


class MusicdlDownloader:
    """Download a track via musicdl (NetEase, QQ Music, …) and upload to Arvan S2."""

    platform: ClassVar[str] = "musicdl"

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Search, download, upload, return one :class:`~kharej.contracts.S2ObjectRef`."""
        try:
            from rubetunes.providers.musicdl import MusicdlClient  # noqa: PLC0415
            from rubetunes.providers.musicdl.errors import MusicdlNotInstalledError  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "rubetunes.providers.musicdl is not importable; "
                "ensure the rubetunes package is installed"
            ) from exc

        query: str = job.url.strip()
        if not query:
            raise ValueError("musicdl job.url (search query) must not be empty")

        # Optional source filter from format_hint (e.g. "NeteaseMusicClient")
        source_hint: str | None = getattr(job.payload, "format_hint", None)
        sources: list[str] | None = [source_hint] if source_hint else None

        logger.info(
            {
                "event": "musicdl.search_start",
                "job_id": job.job_id,
                "query": query,
                "sources": sources,
            }
        )
        await progress.report_progress(job.job_id, 0, phase="downloading")

        try:
            client = MusicdlClient(sources=sources)
        except MusicdlNotInstalledError as exc:
            raise RuntimeError(
                "musicdl Python package is not installed; install musicdl to use this platform"
            ) from exc

        result = await client.search(query, sources=sources, limit=_MAX_CANDIDATES)

        if not result.tracks:
            raise RuntimeError(
                f"musicdl returned no results for query: {query!r}"
            )

        logger.info(
            {
                "event": "musicdl.search_done",
                "job_id": job.job_id,
                "total": result.total,
            }
        )

        with tempfile.TemporaryDirectory(prefix=f"kharej_mdl_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)
            last_error: Exception | None = None

            for idx, track in enumerate(result.tracks[:_MAX_CANDIDATES]):
                logger.info(
                    {
                        "event": "musicdl.download_attempt",
                        "job_id": job.job_id,
                        "attempt": idx + 1,
                        "track": track.display_title,
                        "source": track.source,
                    }
                )
                try:
                    dl_result = await client.download(track, dest_dir=tmp_dir)
                    if not dl_result.success or not dl_result.file_path:
                        raise RuntimeError(
                            dl_result.error or "musicdl download returned no file"
                        )

                    audio_path = Path(dl_result.file_path)
                    if not audio_path.exists() or audio_path.stat().st_size == 0:
                        raise RuntimeError(
                            f"musicdl file not found or empty: {audio_path}"
                        )

                    await progress.report_progress(job.job_id, 90, phase="uploading")

                    ext = audio_path.suffix.lstrip(".")
                    stem = safe_filename(track.song_name or audio_path.stem)
                    s2_filename = f"{stem}.{ext}" if ext else stem
                    s2_key = make_media_key(job.job_id, s2_filename)

                    logger.info(
                        {
                            "event": "musicdl.upload_start",
                            "job_id": job.job_id,
                            "key": s2_key,
                            "size": audio_path.stat().st_size,
                        }
                    )
                    ref: S2ObjectRef = await asyncio.to_thread(
                        s2.upload_file, audio_path, s2_key
                    )
                    logger.info(
                        {
                            "event": "musicdl.upload_done",
                            "job_id": job.job_id,
                            "key": s2_key,
                            "sha256": ref.sha256,
                        }
                    )
                    await progress.report_progress(job.job_id, 100, phase="uploading")
                    return [ref]

                except Exception as exc:
                    logger.warning(
                        {
                            "event": "musicdl.download_failed",
                            "job_id": job.job_id,
                            "attempt": idx + 1,
                            "error": repr(exc),
                        }
                    )
                    last_error = exc

            raise RuntimeError(
                f"musicdl: all {min(len(result.tracks), _MAX_CANDIDATES)} download "
                f"attempts failed for query {query!r}. Last error: {last_error!r}"
            )
