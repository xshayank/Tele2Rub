"""HTTP proxy manager for the Kharej VPS worker.

Fetches HTTP proxy lists from multiple sources via :pypi:`pyfreeproxy`
(CharlesPikachu/freeproxy), validates them by measuring actual download
throughput through each proxy, then maintains a rotating pool of working
proxies.  A background asyncio task refreshes the pool every 15 minutes
automatically.  Validated proxies are persisted to disk so the pool is
available immediately after a process restart without waiting for the first
full refresh cycle.

The module exposes a process-global singleton :data:`proxy_manager` that all
downloaders should use::

    from kharej.proxy_manager import proxy_manager

    proxy_url = proxy_manager.get_proxy()       # "http://ip:port" or None
    proxy_manager.mark_proxy_failed(proxy_url)  # evict a bad proxy immediately
    await proxy_manager.start()                 # begin background refresh
    await proxy_manager.stop()                  # cancel background refresh

Primary proxy
-------------
Set the ``KHAREJ_PRIMARY_PROXY`` environment variable to an ``http://host:port``
URL to make the manager always prefer that proxy over the scraped pool::

    KHAREJ_PRIMARY_PROXY=http://1.2.3.4:8080

When a primary proxy is configured :meth:`ProxyManager.get_proxy` always
returns it — the scanned pool is only used as a fallback when the primary
proxy has been marked as failed via :meth:`ProxyManager.mark_proxy_failed`.
The failed flag is automatically cleared at the start of every refresh cycle
(every 15 minutes) so the primary proxy is retried after a temporary outage.

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

Disk cache
----------
After every successful refresh the validated proxy list is written atomically
to :data:`_PROXY_CACHE_FILE` (``kharej/state/proxies.json``).  On startup the
cache is loaded immediately so requests do not fail while the first background
refresh is still in progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
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
# Disk cache helpers
# ---------------------------------------------------------------------------


def _load_proxy_cache(path: Path) -> list[str]:
    """Load the saved proxy list from *path*; return an empty list on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str)]
    except Exception:
        pass
    return []


def _save_proxy_cache(proxies: list[str], path: Path) -> None:
    """Atomically write *proxies* to *path* as a JSON array."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".proxies_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(proxies, f)
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


def _validate_proxies(proxy_urls: Sequence[str]) -> list[str]:
    """Return valid *proxy_urls* sorted by descending download speed.

    Proxies are validated concurrently and the resulting list is ordered
    fastest-first so that :meth:`ProxyManager.get_proxy` can preferentially
    select high-throughput proxies.
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

    # Sort fastest-first so that get_proxy() picks from the best candidates.
    results.sort(key=lambda t: t[1], reverse=True)
    working = [url for url, _ in results]

    logger.info(
        {
            "event": "proxy_manager.validation_done",
            "total": len(proxy_urls),
            "working": len(working),
        }
    )
    return working


# ---------------------------------------------------------------------------
# ProxyManager
# ---------------------------------------------------------------------------


class ProxyManager:
    """Manage a pool of validated HTTP proxies with periodic auto-refresh.

    The manager is safe to use from multiple threads (downloaders call
    :meth:`get_proxy` from ``asyncio.to_thread`` workers).

    On instantiation the disk cache is loaded immediately so callers always
    have a non-empty pool even before the first background refresh completes.
    """

    def __init__(
        self,
        sources: list[str] | None = None,
        cache_file: Path | None = None,
        primary_proxy: str | None = None,
    ) -> None:
        self._sources: list[str] = sources if sources is not None else list(_FREEPROXY_SOURCES)
        self._cache_file: Path = cache_file or _PROXY_CACHE_FILE
        self._lock = threading.Lock()
        # Primary proxy: always preferred over the scanned pool when set.
        self._primary_proxy: str | None = primary_proxy or os.environ.get("KHAREJ_PRIMARY_PROXY")
        # Tracks whether the primary proxy has been marked as failed for the current cycle.
        self._primary_proxy_failed: bool = False
        if self._primary_proxy:
            logger.info(
                {
                    "event": "proxy_manager.primary_proxy_set",
                    "proxy": self._primary_proxy,
                }
            )
        # Pre-populate from disk so requests succeed immediately after restart.
        cached = _load_proxy_cache(self._cache_file)
        self._working: list[str] = cached
        if cached:
            logger.info(
                {
                    "event": "proxy_manager.cache_loaded",
                    "count": len(cached),
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
        """Return a working ``http://host:port`` proxy URL, or ``None``.

        When a primary proxy is configured via ``KHAREJ_PRIMARY_PROXY`` it is
        always returned first — the scanned pool is only used as a fallback
        when the primary proxy has been marked failed via
        :meth:`mark_proxy_failed`.

        Otherwise proxies are stored sorted fastest-first (by the speed
        measured during validation).  To strike a balance between throughput
        and distribution, a random proxy is chosen from the fastest
        :data:`_TOP_PROXY_COUNT` candidates (or all available proxies when the
        pool is smaller).
        """
        with self._lock:
            if self._primary_proxy and not self._primary_proxy_failed:
                return self._primary_proxy
            if not self._working:
                return None
            pool = self._working[:_TOP_PROXY_COUNT]
            return random.choice(pool)

    def mark_proxy_failed(self, proxy_url: str) -> None:
        """Evict *proxy_url* from the working pool immediately.

        Call this when a download fails with a proxy-related error so that
        subsequent requests from other jobs do not reuse the broken proxy.
        If the pool becomes empty after the eviction, a background refresh is
        triggered automatically.

        When *proxy_url* is the configured primary proxy it is not evicted from
        the pool (it is not part of the scanned pool to begin with); instead a
        flag is set so that :meth:`get_proxy` falls back to the scanned pool
        until the next refresh cycle resets the flag.
        """
        with self._lock:
            if self._primary_proxy and proxy_url == self._primary_proxy:
                self._primary_proxy_failed = True
                logger.warning(
                    {
                        "event": "proxy_manager.primary_proxy_failed",
                        "proxy": proxy_url,
                        "msg": "Primary proxy marked as failed; falling back to scanned pool until next refresh",
                    }
                )
                return

            try:
                self._working.remove(proxy_url)
            except ValueError:
                return  # already removed, nothing to do
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
                    "msg": "No working proxies left; triggering background refresh",
                }
            )
            # Fire-and-forget refresh from a thread so we don't block the caller.
            threading.Thread(target=self._refresh, daemon=True, name="proxy-refresh").start()

    def working_count(self) -> int:
        """Return the number of currently validated working proxies."""
        with self._lock:
            return len(self._working)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Synchronously fetch and validate the proxy list, then update the pool.

        Also resets the primary-proxy failed flag so that a previously failing
        primary proxy is retried after each refresh cycle.
        """
        # Reset the primary proxy failed flag so it gets retried after the refresh.
        with self._lock:
            if self._primary_proxy_failed:
                logger.info(
                    {
                        "event": "proxy_manager.primary_proxy_reset",
                        "proxy": self._primary_proxy,
                        "msg": "Primary proxy failure flag cleared; will retry primary proxy",
                    }
                )
                self._primary_proxy_failed = False

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
        working = _validate_proxies(candidates)
        with self._lock:
            self._working = working
        # Persist to disk so the pool survives a process restart.
        try:
            _save_proxy_cache(working, self._cache_file)
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
        """Background task: refresh the proxy pool every :data:`_REFRESH_INTERVAL` seconds."""
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
