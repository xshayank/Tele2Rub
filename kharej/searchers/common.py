"""Shared helpers for the Kharej search adapters.

Provides :func:`upload_thumb_to_s3` — a utility that downloads an image from
a URL and uploads it to the S3 bucket, returning the S3 object key.

Design notes
------------
The Iran VPS **cannot** reach external platforms (YouTube CDN, Spotify CDN,
etc.) directly.  All thumbnails and cover images must therefore be uploaded by
the Kharej worker to the shared S3 bucket so that Iran can serve them via
presigned URLs.

A lightweight existence check (HEAD request) is performed before re-uploading:
if a key already exists the upload is skipped, providing free caching across
repeated searches for the same content.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kharej.s2_client import S2Client

logger = logging.getLogger("kharej.searchers.common")

# Hard limit on thumbnail file size accepted for S3 upload (5 MB).
_MAX_THUMB_BYTES: int = 5 * 1024 * 1024

# Allowlist of CDN hostnames that Kharej is permitted to download thumbnails from.
# This prevents the thumbnail helper from being used as an arbitrary SSRF vector.
_ALLOWED_THUMB_HOSTS: frozenset[str] = frozenset(
    {
        "i.ytimg.com",  # YouTube thumbnails
        "i.scdn.co",  # Spotify cover art
        "mosaic.scdn.co",  # Spotify mosaic covers
        "lineup-images.scdn.co",  # Spotify playlist headers
    }
)


async def upload_thumb_to_s3(
    image_url: str,
    s2: "S2Client",
    s3_key: str,
    *,
    content_type: str = "image/jpeg",
) -> str | None:
    """Download *image_url* and upload it to S3 at *s3_key*.

    Parameters
    ----------
    image_url:
        Source URL of the image.  Must be reachable from the Kharej VPS.
    s2:
        Kharej S2 client (writable).
    s3_key:
        Destination key in the S3 bucket.
    content_type:
        MIME type to store with the object.

    Returns
    -------
    str | None
        The S3 key on success, or ``None`` on any failure.  Individual
        thumbnail failures never propagate — callers should treat ``None`` as
        "no thumbnail available".
    """
    try:
        # Existence check — skip re-upload if already cached.
        existing = await asyncio.to_thread(s2.head_object, s3_key)
        if existing is not None:
            logger.debug("thumb cache hit: %s", s3_key)
            return s3_key
    except Exception as exc:  # noqa: BLE001
        logger.debug("thumb head_object check failed (%s): %s", s3_key, exc)
        # Continue — attempt upload anyway

    # Validate that the URL hostname is in the allowlist to prevent SSRF.
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        parsed_url = urlparse(image_url)
        hostname = (parsed_url.hostname or "").lower()
        if hostname not in _ALLOWED_THUMB_HOSTS:
            logger.warning(
                "Thumbnail URL hostname %r is not in allowlist; skipping", hostname
            )
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thumbnail URL parse error for %r: %s", image_url, exc)
        return None

    def _blocking_fetch_and_upload() -> str:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "RubeTunes/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = resp.read(_MAX_THUMB_BYTES + 1)
        if len(data) > _MAX_THUMB_BYTES:
            raise ValueError(
                f"Thumbnail at {image_url!r} exceeds {_MAX_THUMB_BYTES} bytes"
            )
        with tempfile.NamedTemporaryFile(suffix=Path(s3_key).suffix or ".jpg", delete=False) as tf:
            tf.write(data)
            tmp_path = Path(tf.name)
        try:
            s2.upload_file(tmp_path, s3_key, content_type=content_type)
        finally:
            tmp_path.unlink(missing_ok=True)
        return s3_key

    try:
        return await asyncio.to_thread(_blocking_fetch_and_upload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to upload thumbnail %s → %s: %s", image_url, s3_key, exc)
        return None
