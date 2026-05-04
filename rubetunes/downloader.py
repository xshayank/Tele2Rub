from __future__ import annotations

"""Multi-provider music downloader.

Implements:
  - build_platform_choices   — rank available sources for a track
  - best_source_label        — human label for the highest-ranked source
  - download_track           — async auto-waterfall entry point
  - download_track_from_choice — async single-source downloader (R2)
  - DownloadError            — clean exception with source name
"""

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "QUALITY_MP3",
    "QUALITY_FLAC_CD",
    "QUALITY_FLAC_HI",
    "QUALITY_MENU",
    "_QUALITY_LABELS",
    "DownloadError",
    "build_platform_choices",
    "best_source_label",
    "download_track",
    "download_track_from_choice",
]

QUALITY_MP3     = "mp3"
QUALITY_FLAC_CD = "flac_cd"
QUALITY_FLAC_HI = "flac_hi"

_QUALITY_LABELS = {
    QUALITY_MP3:     "MP3 320k",
    QUALITY_FLAC_CD: "FLAC CD (16-bit / 44.1 kHz)",
    QUALITY_FLAC_HI: "FLAC Hi-Res (24-bit)",
}

QUALITY_MENU = [
    {"label": "\U0001f3b5 MP3 320k",                    "quality": QUALITY_MP3},
    {"label": "\U0001f4bf FLAC CD (16-bit / 44.1 kHz)", "quality": QUALITY_FLAC_CD},
    {"label": "\u2b50 FLAC Hi-Res (24-bit)",            "quality": QUALITY_FLAC_HI},
]


class DownloadError(RuntimeError):
    """A download from a named source failed."""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        super().__init__(f"[{source}] {message}")


# ---------------------------------------------------------------------------
# Platform choices builder (R1)
# ---------------------------------------------------------------------------

def build_platform_choices(info: dict, quality: str) -> list:
    """Return a ranked list of download platform choices filtered by *quality*.

    quality: "mp3" | "flac_cd" | "flac_hi" | "any"

    Each entry is a dict with keys: source, quality, label, url, rank.
    An extra "auto" entry (rank 0) is prepended when ≥2 sources are available.
    """
    choices: list[dict] = []

    want_flac = quality in ("flac_cd", "flac_hi", "any")
    want_mp3  = quality in ("mp3", "any")

    # 1. Qobuz — best FLAC
    qobuz_id = info.get("qobuz_id")
    if qobuz_id and want_flac:
        bit_depth   = int(info.get("qobuz_bit_depth") or 16)
        sample_rate = int(info.get("qobuz_sample_rate") or 44100)
        if quality in ("flac_hi", "any") and bit_depth >= 24:
            label = "Qobuz Hi-Res {}-bit / {} kHz".format(
                bit_depth, sample_rate // 1000
            )
            choices.append({
                "source": "qobuz", "quality": "flac_hi",
                "label": label, "url": info.get("qobuz_url"), "rank": 1,
            })
        else:
            choices.append({
                "source": "qobuz", "quality": "flac_cd",
                "label": "Qobuz FLAC 16-bit / 44.1 kHz",
                "url": info.get("qobuz_url"), "rank": 2,
            })

    # 2. Tidal Alt
    if want_flac:
        tidal_alt_url       = info.get("tidal_alt_url")
        tidal_alt_available = info.get("tidal_alt_available", False)
        if tidal_alt_url or tidal_alt_available:
            choices.append({
                "source": "tidal_alt", "quality": "flac_cd",
                "label": "Tidal FLAC",
                "url": tidal_alt_url if isinstance(tidal_alt_url, str) else None,
                "rank": 3,
            })

    # 3. Deezer (requires DEEZER_ARL env var)
    deezer_id = info.get("deezer_id")
    if deezer_id and want_flac and os.getenv("DEEZER_ARL", "").strip():
        choices.append({
            "source": "deezer", "quality": "flac_cd",
            "label": "Deezer FLAC",
            "url": info.get("deezer_url"), "rank": 4,
        })

    # 4. Amazon Music
    amazon_id = info.get("amazon_id")
    if amazon_id:
        choices.append({
            "source": "amazon",
            "quality": "flac_cd" if want_flac else "mp3",
            "label": "Amazon Music",
            "url": info.get("amazon_url"), "rank": 5,
        })

    # 5. YouTube Music (MP3 — last FLAC fallback or explicit MP3 request)
    if want_mp3 or (want_flac and not choices):
        if info.get("isrc") or info.get("title"):
            choices.append({
                "source": "youtube", "quality": "mp3",
                "label": "YouTube Music MP3", "url": None, "rank": 6,
            })

    # 7. monochrome (Tidal via community proxy) — fallback after YouTube
    if want_flac or want_mp3:
        if info.get("track_id") or info.get("isrc") or info.get("title"):
            choices.append({
                "source": "monochrome",
                "quality": "flac_cd" if want_flac else "mp3",
                "label": "Monochrome (Tidal proxy)",
                "url": None, "rank": 7,
            })

    # 8. musicdl (multi-source CN/global) — last-resort fallback
    if want_flac or want_mp3:
        if info.get("title"):
            choices.append({
                "source": "musicdl",
                "quality": "mp3",
                "label": "musicdl (multi-source)",
                "url": None, "rank": 8,
            })

    # Sort by rank
    choices.sort(key=lambda c: c["rank"])

    # Filter out providers with an open circuit breaker (best-effort)
    try:
        from rubetunes.circuit_breaker import _is_circuit_open
        filtered = [c for c in choices if not _is_circuit_open("download", c["source"])]
        if filtered:
            choices = filtered
    except Exception:
        pass

    # Prepend "auto" waterfall entry when ≥2 sources are available
    if len(choices) >= 2:
        choices = [{
            "source": "auto", "quality": quality,
            "label": "\u26a1 Auto (best available)",
            "url": None, "rank": 0,
            "_sub_choices": list(choices),
        }] + choices

    return choices


def best_source_label(info: dict) -> str:
    """Return a short human label for the highest-ranked available source."""
    choices = build_platform_choices(info, "any")
    for c in choices:
        if c["source"] != "auto":
            return c["label"]
    return "Unknown"


# ---------------------------------------------------------------------------
# Internal provider download helpers (sync — run inside thread/executor)
# ---------------------------------------------------------------------------

def _safe_name(info: dict) -> str:
    from rubetunes.tagging import _safe_filename
    title  = info.get("title") or "track"
    artist = (info.get("artists") or [""])[0]
    base   = f"{artist} - {title}" if artist else title
    return _safe_filename(base)


def _cookies_args(cookies_path: str | None) -> list[str]:
    """Return ``["--cookies", path]`` when *cookies_path* points to an existing file, else ``[]``."""
    if cookies_path and Path(cookies_path).is_file():
        return ["--cookies", cookies_path]
    return []


def _download_url_to_file(url: str, out_path: Path, *, timeout: int = 120) -> None:
    """Stream *url* into *out_path*."""
    with requests.get(
        url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(65536):
                if chunk:
                    fh.write(chunk)


def _ext_from_content_type(ct: str, url: str = "") -> str:
    ct = ct.lower()
    if "flac" in ct:
        return ".flac"
    if "mp3" in ct or "mpeg" in ct:
        return ".mp3"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    # Guess from URL extension
    for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
        if url.lower().endswith(ext) or (ext + "?") in url.lower():
            return ext
    return ".flac"


# --- Qobuz ---

def _download_qobuz(info: dict, quality: str, output_dir: Path) -> Path:
    from rubetunes.providers.qobuz import (
        _get_qobuz_stream_url, _QOBUZ_QUALITY_CHAIN,
    )

    qobuz_id = str(info.get("qobuz_id") or "")
    if not qobuz_id:
        raise DownloadError("qobuz", "No Qobuz ID in track info")

    quality_codes = _QOBUZ_QUALITY_CHAIN.get(quality) or _QOBUZ_QUALITY_CHAIN["flac_cd"]
    if not quality_codes:
        quality_codes = [6]

    stream_url: str | None = None
    for qcode in quality_codes:
        stream_url = _get_qobuz_stream_url(qobuz_id, qcode)
        if stream_url:
            break

    # Authenticated fallback (R5)
    if not stream_url:
        email    = os.getenv("QOBUZ_EMAIL", "").strip()
        password = os.getenv("QOBUZ_PASSWORD", "").strip()
        if email and password:
            try:
                from rubetunes.providers.qobuz import _get_qobuz_stream_url_auth
                for qcode in quality_codes:
                    stream_url = _get_qobuz_stream_url_auth(qobuz_id, qcode)
                    if stream_url:
                        break
            except Exception as exc:
                log.debug("qobuz auth fallback: %s", exc)

    if not stream_url:
        raise DownloadError("qobuz", f"No stream URL found for Qobuz track {qobuz_id}")

    # Probe content-type to choose extension
    try:
        head = requests.head(
            stream_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True
        )
        ct  = head.headers.get("content-type", "")
        ext = _ext_from_content_type(ct, stream_url)
    except Exception:
        ext = ".flac"

    out_path = output_dir / f"{_safe_name(info)}{ext}"
    _download_url_to_file(stream_url, out_path)
    return out_path


# --- Tidal Alt ---

def _download_tidal_alt(info: dict, output_dir: Path) -> Path:
    from rubetunes.providers.tidal_alt import (
        _download_tidal_manifest, _ext_from_manifest,
        _get_tidal_alt_url, _get_tidal_alt_url_by_tidal_id,
    )

    result = info.get("tidal_alt_url")

    if result is None:
        # Fetch on demand
        spotify_id = info.get("track_id") or ""
        tidal_id   = str(info.get("tidal_id") or "")
        if spotify_id:
            result = _get_tidal_alt_url(spotify_id)
        if result is None and tidal_id:
            result = _get_tidal_alt_url_by_tidal_id(tidal_id)
        if result is None:
            raise DownloadError("tidal_alt", "Could not fetch Tidal Alt URL")

    if isinstance(result, dict) and result.get("type") == "manifest":
        ext      = _ext_from_manifest(result)
        out_path = output_dir / f"{_safe_name(info)}{ext}"
        _download_tidal_manifest(result, out_path)
    elif isinstance(result, str) and result.startswith("http"):
        try:
            head = requests.head(
                result, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True
            )
            ext = _ext_from_content_type(head.headers.get("content-type", ""), result)
        except Exception:
            ext = ".flac"
        out_path = output_dir / f"{_safe_name(info)}{ext}"
        _download_url_to_file(result, out_path)
    else:
        raise DownloadError("tidal_alt", "Unexpected Tidal Alt response format")

    return out_path


# --- Deezer ---

def _download_deezer(info: dict, quality: str, output_dir: Path, ytdlp_bin: str, cookies_path: str | None = None) -> Path:
    arl = os.getenv("DEEZER_ARL", "").strip()
    if not arl:
        raise DownloadError("deezer", "DEEZER_ARL env var not set")

    deezer_url = info.get("deezer_url") or ""
    if not deezer_url:
        dz_id = info.get("deezer_id")
        if not dz_id:
            raise DownloadError("deezer", "No Deezer URL or ID in track info")
        deezer_url = f"https://www.deezer.com/track/{dz_id}"

    audio_fmt = "flac" if quality in ("flac_cd", "flac_hi") else "mp3"
    out_tmpl  = str(output_dir / f"{_safe_name(info)}.%(ext)s")

    cmd = [
        ytdlp_bin,
        "--add-header", f"Cookie:arl={arl}",
        "-x", "--audio-format", audio_fmt, "--audio-quality", "0",
        "-o", out_tmpl,
        "--no-playlist", "--quiet", "--no-warnings",
        "--print", "after_move:filepath",
    ]
    cmd += _cookies_args(cookies_path)
    cmd.append(deezer_url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise DownloadError("deezer", f"yt-dlp exit {result.returncode}: {result.stderr[:300]}")

    printed = (result.stdout or "").strip().splitlines()
    if printed and Path(printed[-1]).exists():
        return Path(printed[-1])

    # Fallback: find newest audio file
    exts = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}
    candidates = sorted(
        (p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if candidates:
        return candidates[0]
    raise DownloadError("deezer", "yt-dlp succeeded but no audio file found")


# --- Amazon ---

def _download_amazon(info: dict, quality: str, output_dir: Path, ytdlp_bin: str, cookies_path: str | None = None) -> Path:
    from rubetunes.providers.amazon import (
        _get_amazon_stream_url, _convert_or_rename_amazon,
    )

    amazon_id  = str(info.get("amazon_id") or "")
    amazon_url = info.get("amazon_url") or (
        f"https://music.amazon.com/tracks/{amazon_id}" if amazon_id else ""
    )

    stream_url: str | None = None
    decryption_key: str | None = None

    if amazon_id:
        stream_url, decryption_key = _get_amazon_stream_url(amazon_id)

    if stream_url:
        raw_path = output_dir / f"{_safe_name(info)}.raw"
        _download_url_to_file(stream_url, raw_path)
        return _convert_or_rename_amazon(raw_path, decryption_key or "", output_dir, info)

    # Fall back to yt-dlp
    if not amazon_url:
        raise DownloadError("amazon", "No Amazon URL or ID in track info")

    audio_fmt = "flac" if quality in ("flac_cd", "flac_hi") else "mp3"
    out_tmpl  = str(output_dir / f"{_safe_name(info)}.%(ext)s")
    cmd = [
        ytdlp_bin, "-x",
        "--audio-format", audio_fmt, "--audio-quality", "0",
        "-o", out_tmpl,
        "--no-playlist", "--quiet", "--no-warnings",
        "--print", "after_move:filepath",
    ]
    cmd += _cookies_args(cookies_path)
    cmd.append(amazon_url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise DownloadError("amazon", f"yt-dlp exit {result.returncode}: {result.stderr[:300]}")

    printed = (result.stdout or "").strip().splitlines()
    if printed and Path(printed[-1]).exists():
        return Path(printed[-1])

    exts = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}
    candidates = sorted(
        (p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if candidates:
        return candidates[0]
    raise DownloadError("amazon", "yt-dlp succeeded but no audio file found")


# --- YouTube Music ---

def _download_youtube_music(info: dict, output_dir: Path, ytdlp_bin: str, cookies_path: str | None = None) -> Path:
    from rubetunes.providers.youtube import (
        _get_youtube_music_url_by_isrc, _download_youtube_music as _yt_dl,
    )

    isrc   = info.get("isrc") or ""
    title  = info.get("title") or ""
    artist = (info.get("artists") or [""])[0]

    query_or_url: str | None = None
    if isrc:
        query_or_url = _get_youtube_music_url_by_isrc(isrc, title, artist, ytdlp_bin, cookies_path=cookies_path)
    if not query_or_url:
        if title:
            query_or_url = f"{title} {artist}".strip()
        else:
            raise DownloadError("youtube", "No ISRC or title to search YouTube Music")

    return _yt_dl(query_or_url, output_dir, ytdlp_bin, info=info, cookies_path=cookies_path)


# --- Monochrome (Tidal proxy) ---

def _download_monochrome(info: dict, quality: str, output_dir: Path) -> Path:
    try:
        from rubetunes.providers.monochrome import (
            MonochromeClient, download_track, extension_for_quality,
        )
    except ImportError as exc:
        raise DownloadError("monochrome", f"monochrome provider not available: {exc}") from exc

    isrc   = info.get("isrc") or ""
    title  = info.get("title") or ""
    artist = (info.get("artists") or [""])[0]

    tidal_quality = (
        "HI_RES_LOSSLESS" if quality == "flac_hi"
        else "LOSSLESS" if quality == "flac_cd"
        else "HIGH"
    )

    async def _run() -> Path:
        async with MonochromeClient() as client:
            tracks: list = []
            if isrc:
                tracks = await client.search_tracks(isrc)
            if not tracks and title:
                query = f"{title} {artist}".strip()
                tracks = await client.search_tracks(query)
            if not tracks:
                raise DownloadError("monochrome", "No tracks found on Monochrome/Tidal")
            track = tracks[0]
            stream_info = await client.get_stream_info(track.id, tidal_quality)
            ext = extension_for_quality(tidal_quality)
            out_path = output_dir / f"{_safe_name(info)}.{ext}"
            return await download_track(track, stream_info, out_path)

    try:
        return asyncio.run(_run())
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("monochrome", str(exc)) from exc


# --- musicdl (multi-source) ---

def _download_musicdl(info: dict, output_dir: Path) -> Path:
    try:
        from rubetunes.providers.musicdl import MusicdlClient
    except ImportError as exc:
        raise DownloadError("musicdl", f"musicdl provider not available: {exc}") from exc

    title  = info.get("title") or ""
    artist = (info.get("artists") or [""])[0]
    if not title:
        raise DownloadError("musicdl", "No title to search musicdl")

    query = f"{title} {artist}".strip()

    async def _run() -> Path:
        client = MusicdlClient()
        result = await client.search(query, limit=5)
        if not result.tracks:
            raise DownloadError("musicdl", f"No results for {query!r}")
        track = result.tracks[0]
        dl = await client.download(track, dest_dir=output_dir)
        if not dl.success or not dl.file_path:
            raise DownloadError("musicdl", dl.error or "Download failed")
        return Path(dl.file_path)

    try:
        return asyncio.run(_run())
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("musicdl", str(exc)) from exc


# ---------------------------------------------------------------------------
# Waterfall helper
# ---------------------------------------------------------------------------

def _do_waterfall(info: dict, output_dir: Path, ytdlp_bin: str, cookies_path: str | None = None) -> Path:
    """Try all available sources in priority order."""
    choices = build_platform_choices(info, "any")
    providers = [c for c in choices if c["source"] != "auto"]

    last_exc: Exception | None = None
    for choice in providers:
        src = choice["source"]
        try:
            fp = _download_by_source(info, choice, output_dir, ytdlp_bin, cookies_path=cookies_path)
            # Record success
            try:
                from rubetunes.circuit_breaker import _record_provider_outcome
                _record_provider_outcome("download", src, True)
            except Exception:
                pass
            return fp
        except DownloadError as exc:
            log.warning("waterfall: %s skipped: %s", src, exc)
            try:
                from rubetunes.circuit_breaker import _record_provider_outcome
                _record_provider_outcome("download", src, False, str(exc))
            except Exception:
                pass
            last_exc = exc
        except Exception as exc:
            log.warning("waterfall: %s unexpected: %s", src, exc)
            last_exc = exc

    raise DownloadError("waterfall", f"All sources failed — last: {last_exc}")


def _download_by_source(info: dict, choice: dict, output_dir: Path, ytdlp_bin: str, cookies_path: str | None = None) -> Path:
    """Dispatch to the correct provider download function."""
    src     = choice["source"]
    quality = choice.get("quality", "mp3")
    if src == "qobuz":
        return _download_qobuz(info, quality, output_dir)
    if src == "tidal_alt":
        return _download_tidal_alt(info, output_dir)
    if src == "deezer":
        return _download_deezer(info, quality, output_dir, ytdlp_bin, cookies_path=cookies_path)
    if src == "amazon":
        return _download_amazon(info, quality, output_dir, ytdlp_bin, cookies_path=cookies_path)
    if src == "youtube":
        return _download_youtube_music(info, output_dir, ytdlp_bin, cookies_path=cookies_path)
    if src == "monochrome":
        return _download_monochrome(info, quality, output_dir)
    if src == "musicdl":
        return _download_musicdl(info, output_dir)
    raise DownloadError(src, f"Unknown source: {src!r}")


# ---------------------------------------------------------------------------
# Public async API (R1, R2)
# ---------------------------------------------------------------------------

async def download_track_from_choice(
    info: dict,
    choice: dict,
    output_dir: "str | Path" = ".",
    ytdlp_bin: str = "yt-dlp",
    cookies_path: str | None = None,
) -> Path:
    """Download a track from *choice* (produced by ``build_platform_choices``).

    Signature matches rub.py call-site: (info, choice, output_dir, ytdlp_bin).
    Steps: history check → provider download → embed_metadata → record history
    → report to circuit breaker → update Prometheus counters.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    src     = choice.get("source", "auto")
    quality = choice.get("quality", "mp3")

    # Determine a stable track identifier for history
    track_id = (
        info.get("track_id") or info.get("qobuz_id") or
        info.get("tidal_id") or info.get("amazon_id") or
        info.get("isrc") or ""
    )
    track_id = str(track_id)

    # (1) History deduplication
    if track_id and src != "auto":
        try:
            from rubetunes.history import _check_download_history
            cached = _check_download_history(track_id, src, quality)
            if cached is not None:
                log.info("history hit: %s [%s/%s]", track_id, src, quality)
                return cached
        except Exception:
            pass

    loop = asyncio.get_event_loop()

    try:
        if src == "auto":
            # (3) Auto waterfall
            fp = await loop.run_in_executor(
                None, _do_waterfall, info, out_dir, ytdlp_bin, cookies_path
            )
        else:
            fp = await loop.run_in_executor(
                None, _download_by_source, info, choice, out_dir, ytdlp_bin, cookies_path
            )
    except DownloadError:
        # (6) Report to circuit breaker
        try:
            from rubetunes.circuit_breaker import _record_provider_outcome
            _record_provider_outcome("download", src, False)
        except Exception:
            pass
        # (7) Update Prometheus counters
        try:
            from rubetunes.metrics import inc_downloads, inc_provider_failures
            inc_downloads(src, "failure")
            inc_provider_failures(src, "download_error")
        except Exception:
            pass
        raise
    except Exception as exc:
        wrapped = DownloadError(src, str(exc))
        try:
            from rubetunes.circuit_breaker import _record_provider_outcome
            _record_provider_outcome("download", src, False, str(exc))
        except Exception:
            pass
        try:
            from rubetunes.metrics import inc_downloads, inc_provider_failures
            inc_downloads(src, "failure")
            inc_provider_failures(src, type(exc).__name__)
        except Exception:
            pass
        raise wrapped from exc

    # (4) Embed metadata
    try:
        from rubetunes.tagging import embed_metadata
        await loop.run_in_executor(None, embed_metadata, fp, info)
    except Exception as exc:
        log.warning("embed_metadata failed: %s", exc)

    # (5) Record success to history
    if track_id and src != "auto":
        try:
            from rubetunes.history import _record_download_history
            _record_download_history(
                track_id, src, quality, fp,
                title=info.get("title", ""),
                artists=", ".join(info.get("artists") or []),
            )
        except Exception:
            pass

    # (6) Report success to circuit breaker
    try:
        from rubetunes.circuit_breaker import _record_provider_outcome
        _record_provider_outcome("download", src, True)
    except Exception:
        pass

    # (7) Update Prometheus counters
    try:
        from rubetunes.metrics import inc_downloads
        inc_downloads(src, "success")
    except Exception:
        pass

    return fp


async def download_track(
    info: dict,
    output_dir: "str | Path" = ".",
    ytdlp_bin: str = "yt-dlp",
    cookies_path: str | None = None,
) -> Path:
    """Auto-waterfall entry point: Qobuz → Tidal Alt → Deezer → YouTube Music.

    Used by the old ``_do_music_download`` path in rub.py.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_waterfall, info, out_dir, ytdlp_bin, cookies_path)
