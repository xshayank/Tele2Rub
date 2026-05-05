"""SpotiFLAC backend integration — Qobuz and Amazon Music FLAC downloads.

Implements the SpotiFLAC download flow described in
https://github.com/xshayank/SpotiFLAC/blob/94a53b47f00c8393cdfc6f8e38b124fb51201286/docs/backend-api.md

Flow
----
1. Receive a track ``info`` dict (from ``get_track_info``) that already contains
   ``qobuz_id`` and/or ``amazon_url`` resolved from the track ISRC.
2. For Qobuz:
   a. Try MusicDL primary provider (POST musicdl.me/api/qobuz/download).
   b. Fallback to stream proxies (dab.yeet.su / dabmusic.xyz / spotbye).
   c. Automatic quality fallback: 27 → 7 → 6.
3. For Amazon:
   a. Extract ASIN from the Amazon Music URL.
   b. Fetch stream URL + decryption key via spotbye proxy.
   c. Download the M4A, decrypt if key provided, detect codec, rename to .flac.
4. Return the local ``Path`` of the downloaded audio file, or ``None`` on total failure.

Provider priority
-----------------
A module-level ``_ProviderTracker`` records per-provider success / failure counts
for the lifetime of the process.  Providers with recent successes are tried first.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

# ---------------------------------------------------------------------------
# Shared HTTP headers (browser User-Agent required by all endpoints)
# ---------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_COMMON_HEADERS: dict[str, str] = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
}

# ---------------------------------------------------------------------------
# Qobuz quality codes (highest → lowest)
# ---------------------------------------------------------------------------

#: Default quality chain — tries highest quality first, falls back gracefully.
_DEFAULT_QUALITY_CHAIN: list[int] = [27, 7, 6]

#: Map from kharej quality string to Qobuz quality-code chains.
_QUALITY_TO_CHAIN: dict[str, list[int]] = {
    "27": [27, 7, 6],
    "7": [7, 6],
    "6": [6],
    "flac_hi": [27, 7, 6],
    "flac_24bit": [27, 7, 6],
    "24bit": [27, 7, 6],
    "flac_cd": [6, 7],
    "cd": [6, 7],
    "flac": [27, 7, 6],
    "hires": [27, 7, 6],
}

# ---------------------------------------------------------------------------
# MusicDL Qobuz endpoint
# ---------------------------------------------------------------------------

_MUSICDL_QOBUZ_URL = "https://www.musicdl.me/api/qobuz/download"
_MUSICDL_TIMEOUT = 45  # seconds — downloads can be slow

# ---------------------------------------------------------------------------
# Amazon ASIN regex
# ---------------------------------------------------------------------------

_ASIN_RE = re.compile(r"B[0-9A-Z]{9}")

# ---------------------------------------------------------------------------
# Provider priority tracker
# ---------------------------------------------------------------------------


class _ProviderTracker:
    """In-memory per-process success/failure tracker for download providers.

    Providers are ranked by success_count DESC, then failure_count ASC.
    """

    def __init__(self) -> None:
        # key → {"success": int, "failure": int, "last_success": float, "last_failure": float}
        self._stats: dict[str, dict] = {}

    def record_success(self, key: str) -> None:
        s = self._stats.setdefault(
            key, {"success": 0, "failure": 0, "last_success": 0.0, "last_failure": 0.0}
        )
        s["success"] += 1
        s["last_success"] = time.monotonic()

    def record_failure(self, key: str) -> None:
        s = self._stats.setdefault(
            key, {"success": 0, "failure": 0, "last_success": 0.0, "last_failure": 0.0}
        )
        s["failure"] += 1
        s["last_failure"] = time.monotonic()

    def sort_providers(self, providers: list[str]) -> list[str]:
        """Return *providers* ordered by descending preference."""

        def _score(key: str) -> tuple:
            s = self._stats.get(key, {})
            # Higher success → better; higher failure → worse; recent success → better
            return (s.get("success", 0), -s.get("failure", 0), s.get("last_success", 0.0))

        return sorted(providers, key=_score, reverse=True)


_tracker = _ProviderTracker()

# ---------------------------------------------------------------------------
# MusicDL Qobuz primary provider
# ---------------------------------------------------------------------------


def _get_qobuz_url_via_musicdl(qobuz_track_id: str, quality: int) -> str | None:
    """POST to MusicDL and return a direct download URL, or ``None`` on failure.

    No authentication headers are required (the ``X-Debug-Key`` is an
    internal runtime token not available to us; the endpoint has been observed
    to work without it for tracks that are in the MusicDL cache).
    """
    provider_key = f"musicdl_qobuz:{quality}"
    try:
        resp = requests.post(
            _MUSICDL_QOBUZ_URL,
            json={
                "url": f"https://open.qobuz.com/track/{qobuz_track_id}",
                "quality": str(quality),
            },
            headers={**_COMMON_HEADERS, "Content-Type": "application/json"},
            timeout=_MUSICDL_TIMEOUT,
        )
        if not resp.ok:
            log.debug(
                "musicdl qobuz HTTP %d for track %s q=%s", resp.status_code, qobuz_track_id, quality
            )
            _tracker.record_failure(provider_key)
            return None
        data = resp.json()
        if data.get("success") and data.get("download_url"):
            url = data["download_url"]
            if url.startswith("http"):
                _tracker.record_success(provider_key)
                return url
        log.debug(
            "musicdl qobuz: success=False or no URL for track %s q=%s", qobuz_track_id, quality
        )
        _tracker.record_failure(provider_key)
    except Exception as exc:
        log.debug("musicdl qobuz provider failed (track=%s q=%s): %s", qobuz_track_id, quality, exc)
        _tracker.record_failure(provider_key)
    return None


# ---------------------------------------------------------------------------
# Stream-proxy Qobuz fallback
# ---------------------------------------------------------------------------


def _get_qobuz_url_via_stream_proxies(qobuz_track_id: str, quality: int) -> str | None:
    """Try the stream proxy APIs and return a download URL, or ``None`` on failure."""
    from rubetunes.providers.qobuz import _get_qobuz_stream_url  # noqa: PLC0415

    provider_key = f"stream_proxy_qobuz:{quality}"
    try:
        url = _get_qobuz_stream_url(qobuz_track_id, quality)
        if url:
            _tracker.record_success(provider_key)
            return url
        _tracker.record_failure(provider_key)
    except Exception as exc:
        log.debug("qobuz stream proxy failed (track=%s q=%s): %s", qobuz_track_id, quality, exc)
        _tracker.record_failure(provider_key)
    return None


# ---------------------------------------------------------------------------
# Qobuz download orchestrator
# ---------------------------------------------------------------------------


def _get_qobuz_download_url(qobuz_track_id: str, quality: int) -> str | None:
    """Try MusicDL then stream proxies for a single quality level.

    Returns a direct audio URL or ``None`` if all providers fail.
    Providers are tried in priority order based on past success/failure rates.
    """
    provider_fns: dict[str, Callable[[str, int], str | None]] = {
        "musicdl_qobuz": _get_qobuz_url_via_musicdl,
        "stream_proxy_qobuz": _get_qobuz_url_via_stream_proxies,
    }
    ordered_keys = _tracker.sort_providers(list(provider_fns))
    for key in ordered_keys:
        url = provider_fns[key](qobuz_track_id, quality)
        if url:
            return url
    return None


def _download_file(url: str, dest_path: Path) -> None:
    """Download *url* to *dest_path* using a streaming GET request."""
    resp = requests.get(
        url,
        headers={**_COMMON_HEADERS},
        timeout=120,
        stream=True,
    )
    resp.raise_for_status()
    with dest_path.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            if chunk:
                fh.write(chunk)


def _try_qobuz_download(
    qobuz_id: str,
    quality_chain: list[int],
    tmp_dir: Path,
) -> Path | None:
    """Try downloading a Qobuz FLAC with automatic quality fallback.

    Tries each quality in *quality_chain* in order, using MusicDL first then
    stream proxies for each quality.  Returns the local ``Path`` of the
    downloaded file on success, or ``None`` if all attempts fail.
    """
    for quality in quality_chain:
        log.info("spotiflac: trying qobuz track_id=%s quality=%s", qobuz_id, quality)
        url = _get_qobuz_download_url(qobuz_id, quality)
        if not url:
            log.debug("spotiflac: no URL for qobuz track_id=%s quality=%s", qobuz_id, quality)
            continue
        dest = tmp_dir / f"qobuz_{qobuz_id}_q{quality}.flac"
        try:
            _download_file(url, dest)
            if dest.exists() and dest.stat().st_size > 0:
                log.info(
                    "spotiflac: qobuz download OK track_id=%s quality=%s size=%d",
                    qobuz_id,
                    quality,
                    dest.stat().st_size,
                )
                return dest
        except Exception as exc:
            log.debug(
                "spotiflac: qobuz download failed track_id=%s q=%s: %s", qobuz_id, quality, exc
            )
            if dest.exists():
                dest.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------------------
# Amazon download helpers
# ---------------------------------------------------------------------------


def _extract_asin(amazon_url: str) -> str | None:
    """Extract the Amazon track ASIN from an Amazon Music URL."""
    # Prefer trackAsin query parameter
    from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

    parsed = urlparse(amazon_url)
    qs = parse_qs(parsed.query)
    if "trackAsin" in qs:
        val = qs["trackAsin"][0]
        if _ASIN_RE.fullmatch(val):
            return val

    # Try path patterns: /tracks/<ASIN> or /albums/<ASIN>/<ASIN>
    for m in _ASIN_RE.finditer(amazon_url):
        return m.group(0)

    return None


def _try_amazon_download(amazon_url: str, tmp_dir: Path, info: dict) -> Path | None:
    """Download an Amazon Music track via the spotbye proxy.

    Fetches stream URL + decryption key, downloads the M4A, optionally
    decrypts it with ffmpeg, and returns the local ``Path``.
    """
    from rubetunes.providers.amazon import (
        _convert_or_rename_amazon,
        _get_amazon_stream_url,
    )  # noqa: PLC0415

    asin = _extract_asin(amazon_url)
    if not asin:
        log.debug("spotiflac: could not extract ASIN from amazon_url=%s", amazon_url)
        return None

    log.info("spotiflac: trying amazon ASIN=%s", asin)
    stream_url, decryption_key = _get_amazon_stream_url(asin)
    if not stream_url:
        log.debug("spotiflac: amazon proxy returned no stream URL for ASIN=%s", asin)
        return None

    raw_path = tmp_dir / f"amazon_{asin}.m4a"
    try:
        _download_file(stream_url, raw_path)
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            log.debug("spotiflac: amazon raw download empty for ASIN=%s", asin)
            return None
    except Exception as exc:
        log.debug("spotiflac: amazon raw download failed ASIN=%s: %s", asin, exc)
        if raw_path.exists():
            raw_path.unlink(missing_ok=True)
        return None

    try:
        out_path = _convert_or_rename_amazon(raw_path, decryption_key or "", tmp_dir, info)
        if out_path.exists() and out_path.stat().st_size > 0:
            log.info(
                "spotiflac: amazon download OK ASIN=%s size=%d path=%s",
                asin,
                out_path.stat().st_size,
                out_path,
            )
            return out_path
    except Exception as exc:
        log.debug("spotiflac: amazon post-processing failed ASIN=%s: %s", asin, exc)

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def download_spotiflac(info: dict, quality: str, tmp_dir: Path) -> Path | None:
    """Try to download FLAC audio using the SpotiFLAC backend.

    Parameters
    ----------
    info:
        Track info dict as returned by ``get_track_info``.  Must contain at
        least one of ``qobuz_id`` or ``amazon_url`` for this function to do
        anything useful.
    quality:
        Quality preference string (e.g. ``"flac"``, ``"flac_hi"``, ``"27"``).
        Used to select the Qobuz quality-code chain.
    tmp_dir:
        Temporary directory where audio files will be written.

    Returns
    -------
    Path | None
        Local path of the downloaded audio file, or ``None`` if all providers
        fail.
    """
    quality_chain = _QUALITY_TO_CHAIN.get(quality.lower(), _DEFAULT_QUALITY_CHAIN)

    # ------------------------------------------------------------------
    # 1. Try Qobuz
    # ------------------------------------------------------------------
    qobuz_id: str | None = info.get("qobuz_id") or _extract_qobuz_id_from_url(
        info.get("qobuz_url") or ""
    )
    if qobuz_id:
        result = _try_qobuz_download(str(qobuz_id), quality_chain, tmp_dir)
        if result is not None:
            return result
        log.info("spotiflac: qobuz failed for qobuz_id=%s — trying amazon", qobuz_id)

    # ------------------------------------------------------------------
    # 2. Try Amazon
    # ------------------------------------------------------------------
    amazon_url: str | None = info.get("amazon_url")
    if amazon_url:
        result = _try_amazon_download(amazon_url, tmp_dir, info)
        if result is not None:
            return result
        log.info("spotiflac: amazon failed for amazon_url=%s", amazon_url)

    if not qobuz_id and not amazon_url:
        log.debug(
            "spotiflac: no qobuz_id or amazon_url in info — cannot attempt SpotiFLAC download"
        )

    return None


def _extract_qobuz_id_from_url(url: str) -> str | None:
    """Extract a Qobuz numeric track ID from a Qobuz URL, if present."""
    if not url:
        return None
    from rubetunes.spotify_meta import parse_qobuz_track_id  # noqa: PLC0415

    return parse_qobuz_track_id(url)


__all__ = [
    "download_spotiflac",
    "_ProviderTracker",
    "_tracker",
    "_get_qobuz_url_via_musicdl",
    "_get_qobuz_url_via_stream_proxies",
    "_get_qobuz_download_url",
    "_try_qobuz_download",
    "_try_amazon_download",
    "_extract_asin",
    "_QUALITY_TO_CHAIN",
    "_DEFAULT_QUALITY_CHAIN",
    "_MUSICDL_QOBUZ_URL",
    "_COMMON_HEADERS",
]
