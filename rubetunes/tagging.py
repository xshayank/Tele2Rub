from __future__ import annotations

"""Metadata tagging — embed_metadata function.

Supports MP3 (ID3), FLAC (Vorbis comments), and M4A (iTunes atoms).
"""

import logging
import re
import subprocess
import urllib.request
from pathlib import Path

log = logging.getLogger("spotify_dl")

__all__ = [
    "embed_metadata",
    "_safe_filename",
]


def _safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(". ")[:200]


def embed_metadata(filepath: Path, info: dict) -> None:
    """Embed ID3 (MP3) or Vorbis/FLAC tags and cover art using mutagen.

    Lyrics are fetched automatically from lrclib.net when ``info`` has a
    ``title`` and at least one ``artist`` but no ``lyrics`` key.
    """
    # Fetch lyrics from lrclib.net if not already present in info
    if not info.get("lyrics") and info.get("title") and info.get("artists"):
        try:
            from rubetunes.spotify_meta import get_lyrics  # noqa: PLC0415

            artists = info["artists"]
            artist_str = artists[0] if isinstance(artists, list) else str(artists)
            lyrics = get_lyrics(
                info["title"],
                artist_str,
                album_name=info.get("album", ""),
                duration=int(info.get("duration", 0) or 0),
            )
            if lyrics:
                info["lyrics"] = lyrics
                log.debug("Lyrics fetched from lrclib for %r", info.get("title"))
        except Exception as exc:
            log.debug("lrclib lyrics fetch failed: %s", exc)

    try:
        from mutagen.id3 import (
            ID3, ID3NoHeaderError,
            TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, APIC, TSRC, TCON, COMM,
            USLT, TXXX,
        )
        from mutagen.flac import FLAC, Picture
    except ImportError:
        log.warning("mutagen not installed — skipping tag embedding")
        return

    cover_data: bytes | None = None
    if info.get("cover_url"):
        try:
            req = urllib.request.Request(info["cover_url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                cover_data = r.read()
        except Exception as exc:
            log.warning("cover art download failed: %s", exc)

    ext = filepath.suffix.lower()

    if ext == ".mp3":
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()

        tags.add(TIT2(encoding=3, text=info.get("title", "")))
        tags.add(TPE1(encoding=3, text=", ".join(info.get("artists", []))))
        tags.add(TALB(encoding=3, text=info.get("album", "")))
        tags.add(TDRC(encoding=3, text=str(info.get("release_date", ""))))
        tags.add(TRCK(encoding=3, text=str(info.get("track_number", 1))))
        tags.add(TPOS(encoding=3, text=str(info.get("disc_number", 1))))
        if info.get("isrc"):
            tags.add(TSRC(encoding=3, text=info["isrc"]))
        if info.get("albumartist") or info.get("album_artist"):
            tags.add(TPE2(encoding=3, text=info.get("albumartist") or info.get("album_artist") or ""))
        if info.get("genre"):
            tags.add(TCON(encoding=3, text=info["genre"]))
        if info.get("isrc"):
            tags.add(COMM(encoding=3, lang="eng", desc="", text=info.get("isrc", "")))
        if info.get("upc"):
            tags.add(TXXX(encoding=3, desc="UPC", text=info["upc"]))
        if info.get("lyrics"):
            tags.add(USLT(encoding=3, lang="eng", desc="", text=info["lyrics"]))
        if cover_data:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))
        tags.save(str(filepath))
        log.debug("ID3 tags written to %s", filepath.name)

    elif ext == ".flac":
        audio = FLAC(str(filepath))
        audio["title"]       = info.get("title", "")
        audio["artist"]      = ", ".join(info.get("artists", []))
        audio["album"]       = info.get("album", "")
        audio["date"]        = str(info.get("release_date", ""))
        audio["tracknumber"] = str(info.get("track_number", 1))
        audio["discnumber"]  = str(info.get("disc_number", 1))
        if info.get("isrc"):
            audio["isrc"] = info["isrc"]
        if info.get("albumartist") or info.get("album_artist"):
            audio["albumartist"] = info.get("albumartist") or info.get("album_artist") or ""
        if info.get("genre"):
            audio["genre"] = info["genre"]
        if info.get("comment"):
            audio["comment"] = info["comment"]
        if info.get("upc"):
            audio["upc"] = [info["upc"]]
        if info.get("lyrics"):
            audio["lyrics"] = [info["lyrics"]]
        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        log.debug("FLAC tags written to %s", filepath.name)

    elif ext == ".m4a":
        try:
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(filepath))
            audio["\xa9nam"] = [info.get("title", "")]
            audio["\xa9ART"] = [", ".join(info.get("artists", []))]
            audio["\xa9alb"] = [info.get("album", "")]
            audio["\xa9day"] = [str(info.get("release_date", ""))]
            trkn = info.get("track_number", 1)
            trkn_total = info.get("track_total", 0)
            audio["trkn"] = [(int(trkn), int(trkn_total))]
            disk = info.get("disc_number", 1)
            audio["disk"] = [(int(disk), 0)]
            if info.get("isrc"):
                audio["----:com.apple.iTunes:ISRC"] = [info["isrc"].encode()]
            if info.get("albumartist") or info.get("album_artist"):
                audio["aART"] = [info.get("albumartist") or info.get("album_artist") or ""]
            if info.get("genre"):
                audio["\xa9gen"] = [info["genre"]]
            if info.get("lyrics"):
                audio["\xa9lyr"] = [info["lyrics"]]
            if cover_data:
                audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            log.debug("MP4 tags written to %s", filepath.name)
        except Exception as exc:
            log.warning(
                "mutagen MP4 tagging failed for %s: %s — trying ffmpeg remux",
                filepath.name, exc,
            )
            import shutil
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                meta_args: list[str] = []
                for k, v in [
                    ("title",  info.get("title", "")),
                    ("artist", ", ".join(info.get("artists", []))),
                    ("album",  info.get("album", "")),
                    ("date",   str(info.get("release_date", ""))),
                    ("track",  str(info.get("track_number", 1))),
                ]:
                    if v:
                        meta_args += ["-metadata", f"{k}={v}"]
                if info.get("isrc"):
                    meta_args += ["-metadata", f"ISRC={info['isrc']}"]
                tmp_path = filepath.with_suffix(".tagged.m4a")
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-i", str(filepath)] + meta_args
                        + ["-c", "copy", str(tmp_path)],
                        capture_output=True, timeout=60,
                    )
                    if tmp_path.exists() and tmp_path.stat().st_size > 0:
                        filepath.unlink(missing_ok=True)
                        tmp_path.rename(filepath)
                        log.debug("ffmpeg M4A metadata remux OK: %s", filepath.name)
                    else:
                        tmp_path.unlink(missing_ok=True)
                except Exception as exc2:
                    log.warning("ffmpeg M4A remux also failed: %s", exc2)
