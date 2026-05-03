"""Spotify downloader adapter for the Kharej VPS worker.

Supports single tracks, playlists, and albums.

Flow (single track)
-------------------
1. Parse the Spotify track ID from *job.url* via ``parse_spotify_track_id``.
2. Fetch track metadata (title, artists, cover URL) via ``get_track_info``.
3. Download the audio file via yt-dlp YouTube search (with cookies).
4. Upload the audio to ``media/{job_id}/{safe_title}.{ext}``.
5. If a cover URL is available, download the thumbnail and upload it to
   ``thumbs/{job_id}.jpg``.
6. Return ``list[S2ObjectRef]`` with the media ref and, if applicable, the
   thumbnail ref.

Flow (playlist / album)
-----------------------
1. Detect the collection URL with ``_is_spotify_collection``.
2. Resolve track list via the ``spotify_dl`` shim.
3. For each track, call ``_download_spotify_track_locally`` and upload to S2.
4. Report per-track progress.
5. Return all refs.

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
from kharej.downloaders.common import resolve_cookies_path, safe_filename

if TYPE_CHECKING:
    from kharej.dispatcher import Job
    from kharej.progress_reporter import ProgressReporter
    from kharej.s2_client import S2Client
    from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.downloaders.spotify")


async def _download_spotify_track_locally(
    title: str,
    artist: str,
    quality: str,
    tmp_dir: "Path",
    info: dict,
    ytdlp_bin: str = "yt-dlp",
    cookies_path: str | None = None,
) -> "Path":
    """Download a Spotify track locally via YouTube search with cookies.

    Always uses yt-dlp with a YouTube search query so that cookies can be
    passed and bot-detection is avoided.  Returns the local Path of the
    downloaded audio file.
    """
    import yt_dlp as _yt_dlp  # noqa: PLC0415

    query = f"ytsearch1:{artist} - {title}" if artist else f"ytsearch1:{title}"
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
        "outtmpl": str(tmp_dir / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
    }
    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    def _run_ytdlp() -> None:
        with _yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])

    try:
        await asyncio.to_thread(_run_ytdlp)
        audio_path = next(tmp_dir.glob("*.mp3"), None)
        if audio_path is not None:
            return audio_path
        logger.warning({"event": "spotify.ytdlp_no_mp3", "query": query})
    except Exception as exc:
        logger.warning({"event": "spotify.ytdlp_failed", "error": repr(exc)})

    raise RuntimeError(
        f"All download sources failed for track: {artist!r} - {title!r}. "
        "Ensure a valid cookies.txt is present and yt-dlp is up to date."
    )


def _is_spotify_collection(url: str) -> bool:
    """Return True if the URL points to a Spotify playlist or album.

    Uses ``urllib.parse`` to check the path component so that the check is
    not confused by query parameters or fragments that happen to contain
    the substrings ``/playlist/`` or ``/album/``.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    path = urlparse(url).path
    return "/playlist/" in path or "/album/" in path


class SpotifyDownloader:
    """Download a Spotify track (or collection) and upload it (+ thumbnail) to Arvan S2."""

    platform: ClassVar[str] = "spotify"

    async def run(
        self,
        job: Job,
        *,
        s2: S2Client,
        progress: ProgressReporter,
        settings: KharejSettings,
    ) -> list[S2ObjectRef]:
        """Resolve, download, upload.  Returns :class:`~kharej.contracts.S2ObjectRef` objects."""
        # ------------------------------------------------------------------
        # Import spotify_dl shim (read-only)
        # ------------------------------------------------------------------
        try:
            import spotify_dl as _spodl  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "spotify_dl shim is not importable; ensure the rubetunes package is installed"
            ) from exc

        ytdlp_bin: str = "yt-dlp"  # could be made configurable
        cookies_path = resolve_cookies_path(settings)

        # ------------------------------------------------------------------
        # Playlist / Album  — expand to individual tracks and download each
        # ------------------------------------------------------------------
        if _is_spotify_collection(job.url):
            tracks: list[dict] | None = None
            for getter in ("get_playlist_tracks", "get_album_tracks", "list_collection_tracks"):
                fn = getattr(_spodl, getter, None)
                if fn is None:
                    continue
                try:
                    tracks = await asyncio.to_thread(fn, job.url)
                    if tracks:
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        {
                            "event": "spotify.collection_getter_failed",
                            "getter": getter,
                            "job_id": job.job_id,
                            "error": repr(exc),
                        }
                    )

            if not tracks:
                logger.warning(
                    {
                        "event": "spotify.collection_fallback",
                        "job_id": job.job_id,
                        "url": job.url,
                        "note": "Could not resolve collection tracks; treating URL as single item",
                    }
                )
                # Fall through to single-track logic below
            else:
                return await self._run_collection(
                    job=job,
                    tracks=tracks,
                    s2=s2,
                    progress=progress,
                    ytdlp_bin=ytdlp_bin,
                    cookies_path=cookies_path,
                )

        # ------------------------------------------------------------------
        # Single track
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

        with tempfile.TemporaryDirectory(prefix=f"kharej_sp_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)

            logger.info({"event": "spotify.download_start", "job_id": job.job_id})
            artist: str = artists[0] if artists else ""
            audio_path: Path = await _download_spotify_track_locally(
                title, artist, job.quality or "mp3", tmp_dir, info, ytdlp_bin,
                cookies_path=cookies_path,
            )

            await progress.report_progress(job.job_id, 90, phase="uploading")

            refs: list[S2ObjectRef] = []
            audio_ref = await self._upload_audio(
                audio_path=audio_path,
                job_id=job.job_id,
                artists=artists,
                title=title,
                s2=s2,
            )
            refs.append(audio_ref)

            # Optional thumbnail
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

    async def _run_collection(
        self,
        job: "Job",
        tracks: list[dict],
        s2: "S2Client",
        progress: "ProgressReporter",
        ytdlp_bin: str,
        cookies_path: str | None,
    ) -> list[S2ObjectRef]:
        """Download each track in a playlist/album and return all S2 refs."""
        total = len(tracks)
        all_refs: list[S2ObjectRef] = []

        with tempfile.TemporaryDirectory(prefix=f"kharej_spcol_{job.job_id}_") as tmp_str:
            tmp_dir = Path(tmp_str)

            for idx, track_info in enumerate(tracks):
                title: str = track_info.get("title") or "Unknown"
                raw_artists = track_info.get("artists") or []
                artist: str = raw_artists[0] if raw_artists else ""

                logger.info(
                    {
                        "event": "spotify.collection_track_start",
                        "job_id": job.job_id,
                        "track": f"{idx + 1}/{total}",
                        "title": title,
                        "artist": artist,
                    }
                )

                # Each track gets its own sub-directory to avoid filename collisions
                track_dir = tmp_dir / f"track_{idx:04d}"
                track_dir.mkdir()

                try:
                    audio_path = await _download_spotify_track_locally(
                        title,
                        artist,
                        job.quality or "mp3",
                        track_dir,
                        track_info,
                        ytdlp_bin,
                        cookies_path=cookies_path,
                    )
                    audio_ref = await self._upload_audio(
                        audio_path=audio_path,
                        job_id=job.job_id,
                        artists=list(raw_artists) if raw_artists else [],
                        title=title,
                        s2=s2,
                        track_index=idx,
                    )
                    all_refs.append(audio_ref)
                except Exception as exc:
                    logger.warning(
                        {
                            "event": "spotify.collection_track_failed",
                            "job_id": job.job_id,
                            "title": title,
                            "error": repr(exc),
                        }
                    )

                done = idx + 1
                percent = int(done / total * 100)
                await progress.report_progress(
                    job.job_id,
                    percent,
                    phase="downloading",
                    done_tracks=done,
                    total_tracks=total,
                )

        if not all_refs:
            raise RuntimeError(
                f"All tracks failed for collection job {job.job_id!r} ({total} tracks)"
            )
        return all_refs

    @staticmethod
    async def _upload_audio(
        audio_path: "Path",
        job_id: str,
        artists: list[str],
        title: str,
        s2: "S2Client",
        track_index: int | None = None,
    ) -> "S2ObjectRef":
        """Upload *audio_path* to S2 and return the ref."""
        ext = audio_path.suffix.lstrip(".")
        artist_part = safe_filename(", ".join(artists)) if artists else ""
        title_part = safe_filename(title)
        stem = f"{artist_part}_-_{title_part}" if artist_part else title_part
        if track_index is not None:
            stem = f"{track_index:04d}_{stem}"
        s2_filename = f"{stem}.{ext}" if ext else stem
        s2_key = make_media_key(job_id, s2_filename)

        logger.info(
            {
                "event": "spotify.upload_start",
                "job_id": job_id,
                "key": s2_key,
                "size": audio_path.stat().st_size,
            }
        )
        ref: S2ObjectRef = await asyncio.to_thread(s2.upload_file, audio_path, s2_key)
        logger.info(
            {
                "event": "spotify.upload_done",
                "job_id": job_id,
                "key": s2_key,
                "sha256": ref.sha256,
            }
        )
        return ref


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
