from __future__ import annotations

"""SoundCloud provider (C1).

Downloads a SoundCloud track URL via yt-dlp and embeds metadata.

Usage (from rub.py)::

    !soundcloud <url>

The handler calls get_soundcloud_info() to fetch metadata, then the
download is delegated to yt-dlp.
"""

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["parse_soundcloud_url", "download_soundcloud"]

_SOUNDCLOUD_RE = re.compile(
    r"https?://(?:www\.)?soundcloud\.com/[\w\-]+/[\w\-]+"
)


def parse_soundcloud_url(text: str) -> str | None:
    """Return the SoundCloud track URL if *text* contains one, else None."""
    m = _SOUNDCLOUD_RE.search(text)
    return m.group(0) if m else None


async def download_soundcloud(
    url: str,
    download_dir: Path,
    ytdlp_bin: str,
    safe_name: str = "soundcloud_track",
    cookies_path: str | None = None,
    proxy: str | None = None,
) -> Path:
    """Download a SoundCloud track via yt-dlp and return the output path."""
    output_tmpl = str(download_dir / f"{safe_name}.%(ext)s")
    cmd = [
        ytdlp_bin,
        url,
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "-o",
        output_tmpl,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
    ]
    cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--embed-thumbnail"]
    log.info("SoundCloud download: %s", url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        err = stdout.decode(errors="replace")
        raise RuntimeError(f"yt-dlp SoundCloud exit {proc.returncode}: {err[:400]}")

    exts = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}
    candidates = sorted(
        (p for p in download_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("yt-dlp reported success but no audio file found for SoundCloud")
    return candidates[0]
