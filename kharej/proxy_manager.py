"""HTTP proxy manager for the Kharej VPS worker.

Fetches HTTP proxy lists from multiple sources via :pypi:`pyfreeproxy`
(CharlesPikachu/freeproxy), validates them by measuring actual download
throughput through each proxy, then maintains a rotating pool of working
proxies.  A background asyncio task performs a full refresh every 15 minutes
automatically.  Validated proxies are persisted to disk so the pool is
available immediately after a process restart without waiting for the first
full refresh cycle.

The module exposes a process-global singleton :data:`proxy_manager` that all
downloaders should use::

    from kharej.proxy_manager import proxy_manager

    proxy_url = proxy_manager.get_proxy()           # "http://ip:port" or None
    if proxy_url:
        proxy_manager.mark_proxy_succeeded(proxy_url)   # record a successful download
    proxy_manager.mark_proxy_failed(proxy_url)      # safe to call with None (no-op)
    await proxy_manager.start()                     # begin background refresh
    await proxy_manager.stop()                      # cancel background refresh

Proxy sourcing
--------------
Candidates are scraped from several well-maintained free-proxy sources using
the :func:`freeproxy.modules.BuildProxiedSession` API from ``pyfreeproxy``.
Only HTTP/HTTPS proxies are collected so that yt-dlp can use them directly
(SOCKS proxies require extra yt-dlp flags not currently applied).

If ``pyfreeproxy`` is not installed the manager falls back gracefully to an
empty candidate list and logs a warning.

Proxy validation
----------------
Each candidate proxy is tested with a real download speed check:

1. HTTP GET request for a 5 MB file through the proxy using the ``requests``
   library (timeout :data:`_VALIDATE_TIMEOUT`).
2. Verify the response status is 200 OK.
3. Download :data:`_SPEEDTEST_SAMPLE_BYTES` bytes and measure throughput.
4. Proxy is accepted only when throughput ≥ :data:`_MIN_SPEED_BPS`.

Additionally each candidate must pass a YouTube HTTPS reachability check so
that yt-dlp proxy failures are minimised.

Validation runs concurrently in a
:class:`~concurrent.futures.ThreadPoolExecutor` so it does not block the
asyncio event loop.

Proxy pool refresh
------------------
The working proxy pool is replaced wholesale on every refresh cycle (every
:data:`_REFRESH_INTERVAL` seconds, default 15 minutes).  Proxies do **not**
expire on a fixed timer; they remain available until the next refresh replaces
them.  When the pool becomes empty due to failures :meth:`scan_and_get_proxy`
runs an immediate on-demand refresh so that the waiting download always gets a
fresh proxy without a second concurrent scan competing for resources.

Proxy scoring
-------------
Each proxy accumulates a composite *score* that drives selection probability:

* **Speed** — raw download throughput measured during validation (bytes/sec).
* **Success bonus** — every confirmed successful download adds 5 % to the
  weight (capped at +100 %, i.e. a 2× multiplier at 20+ successes).
* **Scan-pass bonus** — every additional validation cycle the proxy survives
  adds 10 % (capped at +100 %, i.e. a 2× multiplier after 10+ cycles).

:meth:`~ProxyManager.get_proxy` uses *weighted random* selection so that
high-scoring proxies are chosen more frequently while still giving every
working proxy some chance of being selected.

Callers should invoke :meth:`~ProxyManager.mark_proxy_succeeded` after each
successful download so that the scoring system improves over time.

Disk cache
----------
After every successful refresh the validated proxy list (including per-proxy
score records) is written atomically to :data:`_PROXY_CACHE_FILE`
(``kharej/state/proxies.json``).  On startup the cache is loaded immediately
so requests do not fail while the first background refresh is still in
progress.  The legacy plain-list cache format is accepted transparently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("kharej.proxy_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: pyfreeproxy source names (HTTP/HTTPS only) used to scrape proxy candidates.
#: All lists are merged before validation so the pool is as large as possible.
#: Sources that only provide SOCKS proxies are excluded because yt-dlp is
#: invoked with a plain ``--proxy http://...`` argument.
_FREEPROXY_SOURCES: list[str] = [
    "ProxiflyProxiedSession",
    "GeonodeProxiedSession",
    "OpenProxyListProxiedSession",
    "FreeproxylistProxiedSession",
    "ProxylistProxiedSession",
    "TheSpeedXProxiedSession",
]

#: Public HTTP speed-test server used for proxy validation.  Using a numeric
#: IPv4 address avoids any DNS lookup through the proxy under test.
_SPEEDTEST_URL: str = "http://212.183.159.230/5MB.zip"

#: Number of bytes to download when measuring proxy speed.  30 KB is enough
#: to gauge throughput without wasting bandwidth on slow proxies.
_SPEEDTEST_SAMPLE_BYTES: int = 30 * 1024  # 30 KB

#: Minimum acceptable download speed through a proxy (bytes/sec).
#: Proxies slower than this are rejected as unsuitable for media downloads.
_MIN_SPEED_BPS: float = 30 * 1024  # 30 KB/s

#: Seconds before a validation attempt is considered failed.
_VALIDATE_TIMEOUT: float = 12.0

#: How many proxies to validate concurrently.
_VALIDATE_WORKERS: int = 60

#: URL used to verify that the proxy can reach YouTube's HTTPS endpoints.
#: A 204 response proves that the proxy supports HTTPS CONNECT tunnelling to
#: Google/YouTube servers.
_YOUTUBE_CHECK_URL: str = "https://www.youtube.com/generate_204"

#: YouTube oEmbed endpoint for a well-known video.  A successful 200 JSON
#: response proves that the proxy can access YouTube video content (not just
#: the CDN edge), which is a closer proxy for whether yt-dlp will succeed.
_YOUTUBE_OEMBED_URL: str = (
    "https://www.youtube.com/oembed"
    "?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ&format=json"
)

#: Timeout (seconds) for the YouTube HTTPS connectivity check.
_YOUTUBE_CHECK_TIMEOUT: float = 10.0

#: Maximum number of proxies to pick from when calling get_proxy().
#: Keeping only the fastest N proxies in the candidate pool ensures that
#: downloads preferentially use high-throughput proxies.
_TOP_PROXY_COUNT: int = 50

#: Maximum thread-pool workers for parallel source fetching.
#: Caps the number of concurrent HTTP scrapers to avoid excessive thread
#: creation when the sources list grows.
_FETCH_WORKERS: int = 10

#: Speed multiplier applied when a proxy responds almost instantly (< 10 ms).
#: In this case, elapsed time is too small to compute a reliable bps value so
#: we award the proxy a high synthetic speed rather than treating it as broken.
_INSTANT_RESPONSE_SPEED_MULTIPLIER: float = 10.0

#: Seconds between automatic proxy list refreshes (15 minutes).
_REFRESH_INTERVAL: float = 900.0

#: Path to the disk cache that persists validated proxies across restarts.
_PROXY_CACHE_FILE: Path = Path(__file__).parent / "state" / "proxies.json"


# ---------------------------------------------------------------------------
# Proxy scoring
# ---------------------------------------------------------------------------


@dataclass
class _ProxyRecord:
    """Runtime statistics for a single validated proxy.

    Attributes:
        speed_bps:   Download throughput measured during the most recent
                     validation (bytes/sec).
        successes:   Number of downloads that completed successfully through
                     this proxy.
        scan_passes: Number of validation cycles this proxy has survived
                     (starts at 1 when first validated).
    """

    speed_bps: float
    successes: int = field(default=0)
    scan_passes: int = field(default=1)


def _compute_proxy_weight(record: _ProxyRecord) -> float:
    """Return the composite selection weight for a proxy.

    Higher weight → higher probability of being chosen by
    :meth:`~ProxyManager.get_proxy`.

    Components:

    * **speed_bps** — raw throughput from the most recent validation.
    * **success bonus** — each confirmed download success adds 5 % (capped
      at +100 %, giving a maximum 2× multiplier at 20+ successes).
    * **scan bonus** — each additional validation cycle survived adds 10 %
      (capped at +100 %, giving a maximum 2× multiplier after 10+ cycles).
    """
    success_bonus = 1.0 + min(record.successes * 0.05, 1.0)
    scan_bonus = 1.0 + min((record.scan_passes - 1) * 0.1, 1.0)
    return record.speed_bps * success_bonus * scan_bonus


# ---------------------------------------------------------------------------
# Disk cache helpers
# ---------------------------------------------------------------------------


def _load_proxy_cache(path: Path) -> dict[str, _ProxyRecord]:
    """Load saved proxy records from *path*; return an empty dict on any error.

    Accepts both the legacy format (a plain JSON array of URL strings) and the
    current format (a JSON array of ``{"url", "speed_bps", "successes",
    "scan_passes"}`` objects).  Legacy entries are converted to fresh records
    with the minimum acceptable speed so that they are usable immediately
    while the first background refresh fills in accurate measurements.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {}
        records: dict[str, _ProxyRecord] = {}
        for item in data:
            if isinstance(item, str):
                # Legacy format: plain URL string.
                records[item] = _ProxyRecord(speed_bps=_MIN_SPEED_BPS)
            elif isinstance(item, dict):
                url = item.get("url")
                if not isinstance(url, str) or not url:
                    continue
                try:
                    raw_speed = float(item.get("speed_bps") or _MIN_SPEED_BPS)
                    # Reject NaN/Inf and speeds below the minimum threshold.
                    if not math.isfinite(raw_speed) or raw_speed < _MIN_SPEED_BPS:
                        raw_speed = _MIN_SPEED_BPS
                    raw_successes = max(0, int(item.get("successes") or 0))
                    # max(1, ...) guards against 0 or negative values that could
                    # appear in manually edited or corrupted cache files.
                    raw_scan_passes = max(1, int(item.get("scan_passes") or 0))
                except (ValueError, TypeError):
                    # Malformed numeric field — skip this entry rather than
                    # dropping the entire cache.
                    logger.debug(
                        {"event": "proxy_manager.cache_bad_entry", "url": url, "item": item}
                    )
                    continue
                records[url] = _ProxyRecord(
                    speed_bps=raw_speed,
                    successes=raw_successes,
                    scan_passes=raw_scan_passes,
                )
        return records
    except Exception:
        return {}


def _save_proxy_cache(records: dict[str, _ProxyRecord], path: Path) -> None:
    """Atomically write *records* to *path* as a JSON array of score objects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "url": url,
            "speed_bps": rec.speed_bps,
            "successes": rec.successes,
            "scan_passes": rec.scan_passes,
        }
        for url, rec in records.items()
    ]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".proxies_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Proxy validation helpers
# ---------------------------------------------------------------------------


def _http_speed_check(proxy_url: str) -> float:
    """Return the measured download speed (bytes/sec) through *proxy_url*, or 0.0 on failure.

    Uses the ``requests`` library to issue an HTTP GET through *proxy_url* to
    a public speed-test file.  Returns 0.0 when:

    * The response status is not 200 OK.
    * Fewer than :data:`_SPEEDTEST_SAMPLE_BYTES` are received.
    * Measured throughput is below :data:`_MIN_SPEED_BPS`.
    * Any network or library exception occurs.
    """
    try:
        import requests  # noqa: PLC0415  (lazy import keeps startup cost low)
    except ImportError:
        logger.error({"event": "proxy_manager.requests_missing"})
        return 0.0

    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        with requests.get(
            _SPEEDTEST_URL,
            stream=True,
            proxies=proxies,
            timeout=_VALIDATE_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return 0.0

            downloaded = 0
            start = time.perf_counter()
            for chunk in resp.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded >= _SPEEDTEST_SAMPLE_BYTES:
                    break

            elapsed = time.perf_counter() - start
            if downloaded < _SPEEDTEST_SAMPLE_BYTES:
                return 0.0
            if elapsed < 0.01:
                # Nearly instant — treat as max speed but require full sample.
                return float(_MIN_SPEED_BPS * _INSTANT_RESPONSE_SPEED_MULTIPLIER)
            speed = downloaded / elapsed
            return speed if speed >= _MIN_SPEED_BPS else 0.0

    except Exception:
        return 0.0


def _http_youtube_check(proxy_url: str) -> bool:
    """Return True if the proxy can both reach YouTube and serve video content.

    Two checks are performed, both must pass:

    1. **Connectivity** — GET :data:`_YOUTUBE_CHECK_URL` (``generate_204``).
       The response **must** be 204 No Content.  Any other status (including
       5xx from a mis-configured proxy) means the proxy does not correctly
       forward YouTube HTTPS traffic.

    2. **Content** — GET :data:`_YOUTUBE_OEMBED_URL` (the oEmbed endpoint for
       a well-known video).  A 200 JSON response proves that the proxy can
       fetch YouTube video metadata — a much closer signal for yt-dlp success
       than a bare connectivity check.

    Requiring both filters eliminates proxies whose IPs are flagged or
    geo-blocked by YouTube (which would still respond to ``generate_204`` but
    refuse to serve video content, causing the "No video formats found!" error
    in yt-dlp).
    """
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        return True  # If requests is missing, skip the check rather than blocking all proxies

    proxies = {"http": proxy_url, "https": proxy_url}

    # --- Check 1: generate_204 must return exactly 204 ---
    try:
        resp = requests.get(
            _YOUTUBE_CHECK_URL,
            proxies=proxies,
            timeout=_YOUTUBE_CHECK_TIMEOUT,
            allow_redirects=False,
            stream=False,
        )
        if resp.status_code != 204:
            return False
    except Exception:
        return False

    # --- Check 2: oEmbed endpoint must return 200 with valid JSON ---
    try:
        resp = requests.get(
            _YOUTUBE_OEMBED_URL,
            proxies=proxies,
            timeout=_YOUTUBE_CHECK_TIMEOUT,
            allow_redirects=True,
            stream=False,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        # A valid oEmbed response always has a "title" field.
        if not isinstance(data, dict) or "title" not in data:
            return False
    except Exception:
        return False

    return True


def _validate_single_proxy(proxy_url: str) -> float:
    """Return the measured speed (bytes/sec) for *proxy_url*, or 0.0 if it fails.

    Both checks must succeed:

    * :func:`_http_speed_check` — adequate download throughput via plain HTTP.
      Returns the measured speed so callers can rank proxies.
    * :func:`_http_youtube_check` — can reach YouTube and fetch video content.

    Returning 0.0 indicates the proxy should be discarded.
    """
    speed = _http_speed_check(proxy_url)
    if speed <= 0.0:
        return 0.0
    return speed if _http_youtube_check(proxy_url) else 0.0


def _fetch_proxies_from_source(source: str) -> list[str]:
    """Scrape HTTP/HTTPS proxy candidates from a single pyfreeproxy *source*.

    Uses :func:`freeproxy.modules.BuildProxiedSession` to scrape the source
    and returns a deduplicated list of ``http://ip:port`` URLs.  Only HTTP and
    HTTPS proxy entries are included so that yt-dlp can use them without extra
    SOCKS flags.

    Returns an empty list if pyfreeproxy is not installed or the source fails.
    """
    try:
        from freeproxy.modules import BuildProxiedSession  # noqa: PLC0415
    except ImportError:
        logger.warning(
            {
                "event": "proxy_manager.pyfreeproxy_missing",
                "msg": "pyfreeproxy is not installed; install pyfreeproxy to enable proxy scraping",
            }
        )
        return []

    try:
        sess = BuildProxiedSession(
            {
                "type": source,
                "max_pages": 1,
                "disable_print": True,
            }  # disable_print is a standard pyfreeproxy param
        )
        proxy_infos = sess.refreshproxies()
    except Exception as exc:
        logger.warning(
            {
                "event": "proxy_manager.source_fetch_failed",
                "source": source,
                "error": repr(exc),
            }
        )
        return []

    results: list[str] = []
    seen: set[str] = set()
    for info in proxy_infos:
        protocol = (info.protocol or "").lower()
        # Only HTTP/HTTPS proxies; SOCKS requires additional yt-dlp flags.
        if protocol not in ("http", "https"):
            continue
        proxy_url = f"http://{info.ip}:{info.port}"
        if proxy_url not in seen:
            seen.add(proxy_url)
            results.append(proxy_url)

    logger.info(
        {
            "event": "proxy_manager.source_fetch_done",
            "source": source,
            "candidates": len(results),
        }
    )
    return results


def _fetch_all_proxy_lists(sources: list[str]) -> list[str]:
    """Scrape and deduplicate HTTP proxies from all pyfreeproxy *sources* in parallel."""
    seen: set[str] = set()
    combined: list[str] = []
    lock = threading.Lock()

    def _fetch_and_collect(source: str) -> None:
        proxies = _fetch_proxies_from_source(source)
        with lock:
            for proxy in proxies:
                if proxy not in seen:
                    seen.add(proxy)
                    combined.append(proxy)

    with ThreadPoolExecutor(max_workers=min(len(sources), _FETCH_WORKERS)) as executor:
        list(executor.map(_fetch_and_collect, sources))

    logger.info(
        {
            "event": "proxy_manager.fetch_all_done",
            "sources": len(sources),
            "total_candidates": len(combined),
        }
    )
    return combined


def _validate_proxies(proxy_urls: Sequence[str]) -> list[tuple[str, float]]:
    """Return valid ``(url, speed_bps)`` pairs from *proxy_urls*, sorted fastest-first.

    Proxies are validated concurrently and the resulting list is ordered
    fastest-first so that :meth:`ProxyManager._refresh` can rank and score
    them correctly.
    """
    if not proxy_urls:
        return []

    results: list[tuple[str, float]] = []
    lock = threading.Lock()

    def _check(proxy_url: str) -> None:
        speed = _validate_single_proxy(proxy_url)
        if speed > 0.0:
            with lock:
                results.append((proxy_url, speed))

    with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as executor:
        list(executor.map(_check, proxy_urls))

    # Sort fastest-first so that _refresh() can rank proxies correctly.
    results.sort(key=lambda t: t[1], reverse=True)

    logger.info(
        {
            "event": "proxy_manager.validation_done",
            "total": len(proxy_urls),
            "working": len(results),
        }
    )
    return results


# ---------------------------------------------------------------------------
# ProxyManager
# ---------------------------------------------------------------------------


class ProxyManager:
    """Manage a pool of validated HTTP proxies with periodic auto-refresh.

    The manager is safe to use from multiple threads (downloaders call
    :meth:`get_proxy` from ``asyncio.to_thread`` workers).

    On instantiation the disk cache is loaded immediately so callers always
    have a non-empty pool even before the first background refresh completes.

    Each proxy carries a :class:`_ProxyRecord` that tracks its download speed,
    number of confirmed successful downloads, and how many validation cycles it
    has survived.  :meth:`get_proxy` performs *weighted random* selection so
    that higher-scoring proxies are chosen more often.

    Proxies are **not** expired on a fixed timer.  The entire pool is replaced
    wholesale on each periodic refresh (every :data:`_REFRESH_INTERVAL`
    seconds) or on an immediate on-demand refresh triggered by
    :meth:`scan_and_get_proxy` when the pool is found to be empty.
    """

    def __init__(self, sources: list[str] | None = None, cache_file: Path | None = None) -> None:
        self._sources: list[str] = sources if sources is not None else list(_FREEPROXY_SOURCES)
        self._cache_file: Path = cache_file or _PROXY_CACHE_FILE
        self._lock = threading.Lock()
        # Per-proxy score records (speed, successes, scan passes).
        self._proxy_records: dict[str, _ProxyRecord] = {}
        # Pre-populate from disk so requests succeed immediately after restart.
        cached_records = _load_proxy_cache(self._cache_file)
        # Sort cached proxies by their stored composite weight so the best
        # proxies are at the front of the working list right from startup.
        self._working: list[str] = sorted(
            cached_records.keys(),
            key=lambda u: _compute_proxy_weight(cached_records[u]),
            reverse=True,
        )
        self._proxy_records = cached_records
        if self._working:
            logger.info(
                {
                    "event": "proxy_manager.cache_loaded",
                    "count": len(self._working),
                }
            )
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Fetch and validate the proxy list immediately, then schedule periodic refresh."""
        # Initial fetch in a background thread so we don't block the event loop.
        await asyncio.to_thread(self._refresh)
        # Schedule recurring refresh
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info({"event": "proxy_manager.started", "interval_sec": _REFRESH_INTERVAL})

    async def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info({"event": "proxy_manager.stopped"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_proxy(self) -> str | None:
        """Return a working ``http://host:port`` proxy URL chosen by weighted random, or ``None``.

        Proxies are stored sorted by their composite score (speed × success
        bonus × scan-pass bonus) and a weighted-random selection is made from
        the top :data:`_TOP_PROXY_COUNT` candidates (or all available proxies
        when the pool is smaller).  Higher-scoring proxies are therefore chosen
        more often while every working proxy retains some chance of selection.
        """
        with self._lock:
            if not self._working:
                return None
            # Re-rank by current composite weight so that success-counter changes
            # made between refreshes are reflected in the top-N selection, rather
            # than relying on the ordering established at the last refresh.
            self._working.sort(
                key=lambda u: _compute_proxy_weight(
                    self._proxy_records.get(u, _ProxyRecord(speed_bps=_MIN_SPEED_BPS))
                ),
                reverse=True,
            )
            pool = self._working[:_TOP_PROXY_COUNT]
            weights = [
                # Every proxy in _working always has a corresponding record.
                # The .get() fallback is a defensive guard against any transient
                # inconsistency (e.g. loading an edge-case cache).
                _compute_proxy_weight(
                    self._proxy_records.get(p, _ProxyRecord(speed_bps=_MIN_SPEED_BPS))
                )
                for p in pool
            ]
            return random.choices(pool, weights=weights, k=1)[0]

    def mark_proxy_succeeded(self, proxy_url: str | None) -> None:
        """Record a successful download through *proxy_url*.

        Increments the proxy's success counter so that future weighted
        selection favours it more strongly.  If *proxy_url* is ``None`` or
        the proxy is no longer in the working pool (e.g. evicted by a
        concurrent failure) the call is a no-op.
        """
        if proxy_url is None:
            return
        with self._lock:
            rec = self._proxy_records.get(proxy_url)
            if rec is None:
                return
            rec.successes += 1
            # Capture while holding the lock so the logged value is consistent
            # with the state change even if another thread modifies the record.
            successes = rec.successes

        logger.debug(
            {
                "event": "proxy_manager.proxy_succeeded",
                "proxy": proxy_url,
                "successes": successes,
            }
        )

    def mark_proxy_failed(self, proxy_url: str | None) -> None:
        """Evict *proxy_url* from the working pool immediately.

        Call this when a download fails with a proxy-related error so that
        subsequent requests from other jobs do not reuse the broken proxy.
        If *proxy_url* is ``None`` the call is a no-op.
        When the pool becomes empty after the eviction, the next call to
        :meth:`scan_and_get_proxy` will trigger a fresh on-demand refresh.
        """
        if proxy_url is None:
            return
        with self._lock:
            try:
                self._working.remove(proxy_url)
            except ValueError:
                return  # already removed, nothing to do
            self._proxy_records.pop(proxy_url, None)
            remaining = len(self._working)

        logger.warning(
            {
                "event": "proxy_manager.proxy_evicted",
                "proxy": proxy_url,
                "remaining": remaining,
            }
        )

        if remaining == 0:
            logger.warning(
                {
                    "event": "proxy_manager.pool_empty",
                    "msg": "No working proxies left; next scan_and_get_proxy call will refresh",
                }
            )

    def working_count(self) -> int:
        """Return the number of currently validated working proxies."""
        with self._lock:
            return len(self._working)

    async def scan_and_get_proxy(self) -> str | None:
        """Return a working proxy URL, scanning for new ones if the pool is empty.

        If the pool already has valid proxies, one is returned immediately (no
        scan overhead).  If the pool is empty, an immediate proxy refresh scan
        is triggered in a background thread; a proxy from the newly populated
        pool is returned, or ``None`` when the scan also finds no working
        proxies.

        This method is intended for use in downloaders that should trigger a
        fresh proxy scan rather than silently proceeding without a proxy when
        the pool has been exhausted.
        """
        proxy = self.get_proxy()
        if proxy is not None:
            return proxy

        logger.info(
            {
                "event": "proxy_manager.scan_triggered",
                "reason": "pool_empty",
                "msg": "Proxy pool is empty; running immediate scan before download",
            }
        )
        await asyncio.to_thread(self._refresh)
        return self.get_proxy()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Synchronously fetch and validate the proxy list, then replace the pool."""
        logger.info({"event": "proxy_manager.refresh_start", "sources": self._sources})
        candidates = _fetch_all_proxy_lists(self._sources)
        if not candidates:
            logger.warning(
                {
                    "event": "proxy_manager.empty_list",
                    "msg": "All proxy lists are empty; keeping existing pool",
                }
            )
            return
        working_with_speeds = _validate_proxies(candidates)
        if not working_with_speeds:
            logger.warning(
                {
                    "event": "proxy_manager.no_valid_proxies",
                    "msg": "Validation found no working proxies; keeping existing pool",
                }
            )
            return
        with self._lock:
            old_records = self._proxy_records
            new_records: dict[str, _ProxyRecord] = {}
            for url, speed in working_with_speeds:
                if url in old_records:
                    # Proxy survived re-validation: preserve download history,
                    # update the measured speed, and credit an extra scan pass.
                    rec = old_records[url]
                    rec.speed_bps = speed
                    rec.scan_passes += 1
                    new_records[url] = rec
                else:
                    new_records[url] = _ProxyRecord(speed_bps=speed)

            # Sort by descending composite weight so the best proxies sit at
            # the front of _working and get_proxy()'s top-N slice is correct.
            working = sorted(
                new_records.keys(),
                key=lambda u: _compute_proxy_weight(new_records[u]),
                reverse=True,
            )
            self._working = working
            self._proxy_records = new_records
        # Persist to disk so the pool (including score records) survives a
        # process restart.
        try:
            _save_proxy_cache(self._proxy_records, self._cache_file)
        except Exception as exc:
            logger.warning(
                {
                    "event": "proxy_manager.cache_save_error",
                    "error": repr(exc),
                }
            )
        logger.info(
            {
                "event": "proxy_manager.refresh_done",
                "working": len(working),
            }
        )

    async def _refresh_loop(self) -> None:
        """Background task: replace the proxy pool every 15 minutes."""
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            try:
                await asyncio.to_thread(self._refresh)
            except Exception as exc:
                logger.warning(
                    {
                        "event": "proxy_manager.refresh_error",
                        "error": repr(exc),
                    }
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Global proxy manager instance used by all downloaders.
proxy_manager: ProxyManager = ProxyManager()
