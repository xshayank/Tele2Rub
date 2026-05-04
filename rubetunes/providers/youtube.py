from __future__ import annotations

"""YouTube Music provider — MP3 fallback via yt-dlp.

Implements:
  _get_youtube_music_url_by_isrc  — resolve a YouTube Music URL by ISRC (R7)
  _download_youtube_music         — download + convert to MP3 via yt-dlp (R7)
"""

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("spotify_dl")

__all__ = [
    "_find_cookies_file",
    "_get_youtube_music_url_by_isrc",
    "_download_youtube_music",
]


def _find_cookies_file() -> Path | None:
    """Return the cookies.txt at the repo root if present, else None.

    This matches the file _base_cmd() in rub.py uses for the YouTube video
    downloader, so YouTube Music benefits from the same login state.
    """
    # rubetunes/providers/youtube.py → repo root is parents[2]
    root = Path(__file__).resolve().parents[2]
    cookies = root / "cookies.txt"
    return cookies if cookies.exists() else None


def _get_youtube_music_url_by_isrc(
    isrc: str,
    title: str = "",
    artist: str = "",
    ytdlp_bin: str = "yt-dlp",
    cookies_path: str | None = None,
) -> str | None:
    """Return a YouTube Music URL for the track identified by *isrc*.

    Strategy:
    1. Search YouTube Music by ISRC (exact metadata match).
    2. Fall back to "{title} {artist}" text search if ISRC yields nothing.

    Returns the best-match URL string or None.
    """
    queries: list[str] = []
    if isrc:
        queries.append(isrc)
    if title:
        q = f"{title} {artist}".strip() if artist else title
        queries.append(q)

    for query in queries:
        url = _ytdlp_search(query, ytdlp_bin, cookies_path=cookies_path)
        if url:
            return url
    return None


def _ytdlp_search(query: str, ytdlp_bin: str, cookies_path: str | None = None) -> str | None:
    """Run a yt-dlp --default-search query and return the first result URL."""
    try:
        cmd = [
            ytdlp_bin,
            "--default-search", "ytsearch1:",
            "--dump-json",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
        ]
        cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]
        cmd.append(query)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout.strip().splitlines()[0])
        return data.get("webpage_url") or data.get("url") or None
    except Exception as exc:
        log.debug("ytdlp search %r: %s", query, exc)
        return None


def _download_youtube_music(
    query_or_url: str,
    output_dir: Path,
    ytdlp_bin: str,
    *,
    info: dict | None = None,
    cookies_path: str | None = None,
) -> Path:
    """Download the best audio from YouTube Music and convert to MP3 V0.

    *query_or_url* may be a full URL or a search query string.
    Returns the path to the downloaded .mp3 file.
    """
    if info:
        from rubetunes.tagging import _safe_filename
        title  = info.get("title") or "track"
        artist = (info.get("artists") or [""])[0]
        base   = _safe_filename(f"{artist} - {title}" if artist else title)
    else:
        base = "youtube_track"

    out_tmpl = str(output_dir / f"{base}.%(ext)s")

    if query_or_url.startswith("http"):
        target = query_or_url
    else:
        target = f"ytsearch1:{query_or_url}"

    cmd = [
        ytdlp_bin,
        target,
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",          # V0 VBR
        "-o", out_tmpl,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--print", "after_move:filepath",
    ]
    cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp YouTube Music exit {result.returncode}: "
            f"{(result.stderr or result.stdout)[:400]}"
        )

    printed = (result.stdout or "").strip().splitlines()
    if printed:
        p = Path(printed[-1])
        if p.exists():
            return p

    # Fallback: newest mp3 in directory
    candidates = sorted(
        (p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]

    raise RuntimeError("yt-dlp YouTube Music succeeded but no .mp3 file found")
