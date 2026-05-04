from __future__ import annotations

"""Bandcamp provider (C2).

Downloads a Bandcamp track or album via yt-dlp.

Usage (from rub.py)::

    !bandcamp <url>
"""

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["parse_bandcamp_url", "download_bandcamp"]

_BANDCAMP_RE = re.compile(
    r"https?://[\w\-]+\.bandcamp\.com/(?:track|album)/[\w\-]+"
)


def parse_bandcamp_url(text: str) -> str | None:
    """Return the Bandcamp URL if *text* contains one, else None."""
    m = _BANDCAMP_RE.search(text)
    return m.group(0) if m else None


async def download_bandcamp(
    url: str,
    download_dir: Path,
    ytdlp_bin: str,
    safe_name: str = "bandcamp_track",
    cookies_path: str | None = None,
) -> Path:
    """Download a Bandcamp track/album via yt-dlp and return the output path."""
    output_tmpl = str(download_dir / f"{safe_name}.%(ext)s")
    cmd = [
        ytdlp_bin,
        url,
        "--extract-audio",
        "--audio-format",
        "flac",
        "--audio-quality",
        "0",
        "-o",
        output_tmpl,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
    ]
    cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]
    log.info("Bandcamp download: %s", url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        err = stdout.decode(errors="replace")
        raise RuntimeError(f"yt-dlp Bandcamp exit {proc.returncode}: {err[:400]}")

    exts = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}
    candidates = sorted(
        (p for p in download_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("yt-dlp reported success but no audio file found for Bandcamp")
    return candidates[0]
