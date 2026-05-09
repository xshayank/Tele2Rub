from __future__ import annotations

"""Async-friendly wrapper around musicdl's MusicClient.

All blocking musicdl calls are dispatched via ``asyncio.to_thread`` so the
Rubika event loop is never blocked.  The module lazy-imports musicdl so a
missing or broken install only breaks the musicdl routes, not the whole app.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from rubetunes.providers.musicdl.config import (
    MUSICDL_DEFAULT_SOURCES,
    MUSICDL_DOWNLOAD_DIR,
    build_init_cfg,
    build_requests_overrides,
)
from rubetunes.providers.musicdl.errors import (
    MusicdlDownloadError,
    MusicdlNotInstalledError,
    MusicdlSearchError,
)
from rubetunes.providers.musicdl.models import (
    MusicdlDownloadResult,
    MusicdlSearchResult,
    MusicdlTrack,
)

__all__ = ["MusicdlClient"]

log = logging.getLogger(__name__)

AUDIO_EXTS: frozenset[str] = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"})

# Clock-skew tolerance: prefer files whose mtime is within this many seconds
# of the download start timestamp (handles NFS/FAT clock drift and overwrites).
_MTIME_TOLERANCE_SECONDS: float = 2.0

# Maximum number of characters to include from a raw SongInfo repr in log messages.
_MAX_RAW_REPR_LENGTH: int = 500


def _build_candidate_dirs(effective_dir: Path, source: str | None) -> list[Path]:
    """Return a deduplicated list of directories to scan for downloaded audio files.

    Covers the common locations where musicdl may write files, regardless of
    whether it honours the configured ``work_dir``.
    """
    seen: set[Path] = set()
    result: list[Path] = []

    def _add(d: Path) -> None:
        key = d.resolve() if d.exists() else d.absolute()
        if key not in seen:
            seen.add(key)
            result.append(d)

    _add(effective_dir)
    if source:
        _add(effective_dir / source)
    _add(MUSICDL_DOWNLOAD_DIR)
    if source:
        _add(MUSICDL_DOWNLOAD_DIR / source)
    _add(Path.cwd())
    return result


def _snapshot_audio_files(dirs: list[Path]) -> frozenset[Path]:
    """Return a frozenset of all audio files currently present in *dirs*."""
    files: set[Path] = set()
    for d in dirs:
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    files.add(p)
    return frozenset(files)


def _find_downloaded_file(
    dirs: list[Path],
    song_name: str,
    existing: frozenset[Path],
    since_ts: float | None = None,
) -> Path | None:
    """Return the most recently modified audio file under any of *dirs*.

    Priority (highest first):
    1. Files with ``mtime >= since_ts - 2`` (written during the download window).
    2. Files not present in *existing* (newly created).
    3. Files whose stem contains *song_name*.
    4. The file with the newest mtime overall.
    """
    candidates: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                candidates.append(p)
    if not candidates:
        return None

    # Strongly prefer files written during (or just before) the download window
    if since_ts is not None:
        recent = [p for p in candidates if p.stat().st_mtime >= since_ts - _MTIME_TOLERANCE_SECONDS]
        if recent:
            candidates = recent

    # Prefer files that weren't there before the download
    new_candidates = [p for p in candidates if p not in existing]
    if new_candidates:
        candidates = new_candidates

    # Prefer files whose stem contains the song_name (best-effort)
    if song_name:
        name_matches = [p for p in candidates if song_name.lower() in p.stem.lower()]
        if name_matches:
            candidates = name_matches

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _import_musicdl() -> Any:
    """Lazy-import musicdl.MusicClient; raises MusicdlNotInstalledError if absent."""
    try:
        from musicdl.musicdl import MusicClient  # type: ignore[import]

        return MusicClient
    except ImportError as exc:
        raise MusicdlNotInstalledError() from exc


def _import_client_builder() -> Any:
    """Lazy-import MusicClientBuilder to access REGISTERED_MODULES."""
    try:
        from musicdl.modules import MusicClientBuilder  # type: ignore[import]

        return MusicClientBuilder
    except ImportError as exc:
        raise MusicdlNotInstalledError() from exc


class MusicdlClient:
    """Async wrapper around musicdl's ``MusicClient``.

    Usage::

        client = MusicdlClient()
        result = await client.search("Bohemian Rhapsody", limit=5)
        for track in result.tracks:
            print(track.display_title)
    """

    def __init__(
        self,
        sources: list[str] | None = None,
        proxy: str | None = None,
    ) -> None:
        self._sources: list[str] = sources or MUSICDL_DEFAULT_SOURCES or []
        self._proxy_overrides: dict = build_requests_overrides(proxy=proxy)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    def list_sources(self) -> list[str]:
        """Return all source client names currently registered in musicdl.

        This reads ``MusicClientBuilder.REGISTERED_MODULES`` at runtime so
        it always reflects the actual installed version of musicdl.
        """
        builder = _import_client_builder()
        return sorted(builder.REGISTERED_MODULES.keys())

    async def search(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ) -> MusicdlSearchResult:
        """Search for tracks across musicdl sources.

        Parameters
        ----------
        query:
            Free-text search string.
        sources:
            Override the default sources for this call only.
        limit:
            Maximum tracks to return *per source* (approximated via
            ``search_size_per_source``).
        """
        if not query:
            raise MusicdlSearchError("Search query must not be empty.")

        effective_sources = sources or self._sources

        def _blocking_search() -> dict:
            MusicClient = _import_musicdl()
            init_cfg: dict = {}
            for src in effective_sources:
                cfg = build_init_cfg(src)
                cfg["search_size_per_source"] = limit
                init_cfg[src] = cfg

            overrides: dict = {}
            if self._proxy_overrides:
                for src in effective_sources:
                    overrides[src] = self._proxy_overrides

            try:
                client = MusicClient(
                    music_sources=effective_sources,
                    init_music_clients_cfg=init_cfg,
                    requests_overrides=overrides,
                )
                return client.search(keyword=query)
            except Exception as exc:
                raise MusicdlSearchError(f"musicdl search failed: {exc}") from exc

        log.info("musicdl search | query=%r | sources=%s", query, effective_sources)
        raw_results: dict = await asyncio.to_thread(_blocking_search)

        # Normalise
        by_source: dict[str, list[MusicdlTrack]] = {}
        all_tracks: list[MusicdlTrack] = []
        for src, infos in raw_results.items():
            tracks = [MusicdlTrack.from_song_info(i) for i in (infos or [])]
            by_source[src] = tracks
            all_tracks.extend(tracks)

        return MusicdlSearchResult(
            query=query,
            tracks=all_tracks,
            by_source=by_source,
            total=len(all_tracks),
        )

    async def download(
        self,
        track: MusicdlTrack,
        dest_dir: Path | None = None,
    ) -> MusicdlDownloadResult:
        """Download a track previously returned by :meth:`search`.

        Parameters
        ----------
        track:
            A :class:`MusicdlTrack` whose ``_raw`` field holds the original
            musicdl ``SongInfo`` object.
        dest_dir:
            Override the download directory for this call.  Defaults to the
            source-specific sub-directory under ``MUSICDL_DOWNLOAD_DIR``.
        """
        if track._raw is None:
            raise MusicdlDownloadError("Cannot download a MusicdlTrack without a raw SongInfo.")

        effective_dir = dest_dir or (MUSICDL_DOWNLOAD_DIR / (track.source or "unknown"))

        def _blocking_download() -> MusicdlTrack:
            MusicClient = _import_musicdl()
            init_cfg = {track.source: build_init_cfg(track.source)}
            if dest_dir:
                init_cfg[track.source]["work_dir"] = str(effective_dir)

            overrides: dict = {}
            if self._proxy_overrides:
                overrides[track.source] = self._proxy_overrides

            try:
                client = MusicClient(
                    music_sources=[track.source],
                    init_music_clients_cfg=init_cfg,
                    requests_overrides=overrides,
                )
                downloaded = client.download(song_infos=[track._raw])
                if not downloaded:
                    raise MusicdlDownloadError(f"musicdl returned no results for track: {track.song_name!r}")
                return MusicdlTrack.from_song_info(downloaded[0])
            except MusicdlDownloadError:
                raise
            except Exception as exc:
                raise MusicdlDownloadError(f"musicdl download failed: {exc}") from exc

        log.info(
            "musicdl download | track=%r | source=%s | dest=%s",
            track.song_name,
            track.source,
            effective_dir,
        )
        effective_dir.mkdir(parents=True, exist_ok=True)

        # Build the full set of candidate directories and snapshot existing
        # audio files BEFORE the download so we can identify newly-written files.
        candidate_dirs = _build_candidate_dirs(effective_dir, track.source)
        existing_files: frozenset[Path] = _snapshot_audio_files(candidate_dirs)
        since_ts = time.time()

        result_track: MusicdlTrack = await asyncio.to_thread(_blocking_download)

        # If musicdl didn't populate file_path (the common case), locate the
        # newly written audio file by scanning all candidate directories.
        if not result_track.file_path:
            dirs_to_scan: list[Path] = list(candidate_dirs)

            # Best-effort: extract a path hint from the raw SongInfo if available.
            # Different musicdl source implementations may store the written path
            # under different attribute names (file_path is the documented field,
            # filepath/savepath are seen in some third-party or older source modules).
            if result_track._raw is not None:
                for attr in ("file_path", "filepath", "savepath"):
                    hint = getattr(result_track._raw, attr, None)
                    if hint and isinstance(hint, str):
                        hint_dir = Path(hint).parent
                        hint_key = hint_dir.resolve() if hint_dir.exists() else hint_dir.absolute()
                        if hint_dir.is_dir() and hint_key not in {
                            (d.resolve() if d.exists() else d.absolute()) for d in dirs_to_scan
                        }:
                            dirs_to_scan.append(hint_dir)

            resolved = _find_downloaded_file(dirs_to_scan, track.song_name, existing_files, since_ts)
            if resolved:
                result_track.file_path = str(resolved)
                log.debug("musicdl: resolved file_path via disk scan → %s", resolved)
            else:
                scanned_counts: dict[str, int] = {}
                for d in dirs_to_scan:
                    if d.exists():
                        scanned_counts[str(d)] = sum(
                            1
                            for p in d.rglob("*")
                            if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                        )
                raw_repr = repr(result_track._raw)[:_MAX_RAW_REPR_LENGTH] if result_track._raw else "None"
                log.warning(
                    "musicdl: no audio file found after download | track=%r | dirs=%s | counts=%s | raw=%s",
                    track.song_name,
                    [str(d) for d in dirs_to_scan],
                    scanned_counts,
                    raw_repr,
                )

        fp = Path(result_track.file_path) if result_track.file_path else effective_dir
        if result_track.file_path:
            error = ""
        else:
            dirs_str = ", ".join(str(d) for d in candidate_dirs)
            error = (
                f"musicdl reported success but no audio file was found under {dirs_str}. "
                f"This usually means the source ignored work_dir; check {MUSICDL_DOWNLOAD_DIR} "
                f"and the bot's CWD."
            )
        return MusicdlDownloadResult(
            track=result_track,
            file_path=fp,
            success=bool(result_track.file_path),
            error=error,
        )
