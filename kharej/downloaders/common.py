"""Shared helpers for all Kharej downloader adapters.

Provides:
- :func:`safe_filename` — sanitize a name for use as a filesystem / S2 key component.
- :func:`get_downloads_dir` — resolve the configurable base download directory.
- :func:`cleanup_path` — best-effort removal of a file or directory after upload.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kharej.settings import KharejSettings

logger = logging.getLogger("kharej.downloaders.common")

# ---------------------------------------------------------------------------
# Safe filename
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"[ _]+")
_LEADING_TRAILING = re.compile(r"^[\s._]+|[\s._]+$")


def safe_filename(name: str) -> str:
    """Return *name* sanitized for use as a filesystem path component or S2 key segment.

    - Replaces ``:``, ``/``, ``\\``, and other unsafe characters with ``_``.
    - Collapses runs of spaces/underscores into a single ``_``.
    - Strips leading/trailing whitespace, dots, and underscores.
    - Falls back to ``"unknown"`` if the result is empty.
    """
    result = _UNSAFE_CHARS.sub("_", name)
    result = _MULTI_SPACE.sub("_", result)
    result = _LEADING_TRAILING.sub("", result)
    return result or "unknown"


# ---------------------------------------------------------------------------
# Downloads directory
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "download_dir"


def _default_download_dir() -> Path:
    """Return the default download directory using the system temp directory."""
    import tempfile  # noqa: PLC0415

    return Path(tempfile.gettempdir()) / "kharej_downloads"


def get_downloads_dir(settings: KharejSettings) -> Path:
    """Return the base directory under which job downloads are placed.

    Reads ``settings.get("download_dir")``.  Falls back to
    ``/tmp/kharej_downloads`` when the setting is absent.

    The directory is created (with parents) on first access.
    """
    raw = settings.get(_SETTINGS_KEY)
    base: Path = Path(raw) if raw else _default_download_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def make_job_dir(settings: KharejSettings, job_id: str) -> Path:
    """Return (and create) a per-job subdirectory under the downloads dir."""
    job_dir = get_downloads_dir(settings) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def make_temp_job_dir(job_id: str) -> Path:
    """Create an OS-level temp directory for a single job.

    Prefer :func:`make_job_dir` for persistent directories. Use this only
    when you want automatic cleanup via a ``with tempfile.TemporaryDirectory()``
    context manager — this function is a helper to produce a deterministic
    sub-path inside a caller-managed temp root.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"kharej_{job_id}_"))
    return tmp


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def cleanup_path(path: Path, *, missing_ok: bool = True) -> None:
    """Best-effort removal of *path* (file or directory) after a successful upload.

    Logs a warning on failure but never raises — a cleanup error must never
    mask a successfully completed job.
    """
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=missing_ok)
        logger.debug("cleanup_path: removed %s", path)
    except Exception as exc:
        logger.warning("cleanup_path: could not remove %s: %s", path, exc)
