"""S2 read-only client for the Iran VPS (Track B, Step 6).

Provides a boto3-backed async wrapper over Arvan S2 (S3-compatible) for:

* Presigned GET URL generation
* HEAD-based existence / size checks
* Streaming object downloads (async generator)
* Listing all S2 objects for a given job

Only **read credentials** are present here; write credentials live
exclusively on the Kharej VPS (see ``architecture.md`` §1.3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import boto3
import botocore.client
import botocore.config
import botocore.exceptions

if TYPE_CHECKING:
    from iran.config import IranSettings

logger = logging.getLogger("iran.s2")

# Default read chunk size used by get_object_stream
_CHUNK_SIZE = 64 * 1024  # 64 KB


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class IranS2Config:
    """Read-only S2 connection parameters for the Iran VPS.

    These are typically populated from :class:`iran.config.IranSettings`
    (env vars prefixed with ``IRAN_``):

    * ``IRAN_S2_ENDPOINT_URL``
    * ``IRAN_S2_ACCESS_KEY``
    * ``IRAN_S2_SECRET_KEY``
    * ``IRAN_S2_BUCKET``
    * ``IRAN_S2_PRESIGN_EXPIRE_SECONDS``
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    presign_expire_seconds: int = 3600


# ---------------------------------------------------------------------------
# Protocol (public interface)
# ---------------------------------------------------------------------------


@runtime_checkable
class S2ClientProtocol(Protocol):
    """Read-only S2 client interface used by the DI container."""

    def generate_presigned_url(self, key: str, expires: int | None = None) -> str:
        """Return a presigned GET URL valid for *expires* seconds."""
        ...

    async def head_object(self, key: str) -> dict | None:
        """Return metadata for *key*, or ``None`` if the object does not exist."""
        ...

    async def get_object_stream(self, key: str) -> AsyncIterator[bytes]:
        """Async-iterate over raw bytes of the S2 object at *key*."""
        ...

    async def list_job_objects(self, job_id: str) -> list[dict]:
        """Return info dicts for every S2 object under ``media/{job_id}/``."""
        ...

    async def list_objects_by_prefix(self, prefix: str) -> list[dict]:
        """Return info dicts for every S2 object whose key starts with *prefix*."""
        ...

    async def delete_job_objects(self, job_id: str) -> int:
        """Delete all S2 objects under ``media/{job_id}/`` and return the number deleted."""
        ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------


class IranS2Client:
    """Async-friendly, read-only Arvan S2 client backed by boto3.

    All blocking boto3 calls run in a thread-pool via :func:`asyncio.to_thread`
    so the FastAPI event loop is never blocked.

    Parameters
    ----------
    config:
        Connection parameters.
    client_factory:
        Optional callable that returns a pre-built ``botocore`` S3 client.
        Inject a :class:`botocore.stub.Stubber`-backed or ``moto``-backed
        client in tests; leave ``None`` in production.
    """

    def __init__(
        self,
        config: IranS2Config,
        *,
        client_factory: "botocore.client.BaseClient | None" = None,
    ) -> None:
        self._config = config
        if client_factory is not None:
            self._s3: botocore.client.BaseClient = client_factory
        else:
            self._s3 = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key,
                config=botocore.config.Config(
                    retries={"max_attempts": 3, "mode": "adaptive"},
                ),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_presigned_url(self, key: str, expires: int | None = None) -> str:
        """Return a presigned GET URL for *key*.

        Parameters
        ----------
        key:
            S2 object key (bucket-relative path).
        expires:
            URL lifetime in seconds.  Defaults to
            :attr:`IranS2Config.presign_expire_seconds`.
        """
        ttl = expires if expires is not None else self._config.presign_expire_seconds
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._config.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    async def head_object(self, key: str) -> dict | None:
        """Return a metadata dict for *key*, or ``None`` on 404.

        Returned dict keys: ``size`` (int), ``content_type`` (str),
        ``etag`` (str).
        """
        try:
            resp = await asyncio.to_thread(
                self._s3.head_object,
                Bucket=self._config.bucket,
                Key=key,
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchKey"):
                return None
            raise
        return {
            "size": resp["ContentLength"],
            "content_type": resp.get("ContentType", "application/octet-stream"),
            "etag": resp.get("ETag", ""),
        }

    async def get_object_stream(self, key: str) -> AsyncIterator[bytes]:
        """Async-iterate over raw bytes of the S2 object at *key*.

        Each iteration yields up to 64 KB.  The underlying boto3
        ``get_object`` call runs in a thread-pool to avoid blocking the
        event loop.
        """
        resp = await asyncio.to_thread(
            self._s3.get_object,
            Bucket=self._config.bucket,
            Key=key,
        )
        body = resp["Body"]
        while True:
            chunk: bytes = await asyncio.to_thread(body.read, _CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    async def list_job_objects(self, job_id: str) -> list[dict]:
        """Return info dicts for every S2 object under ``media/{job_id}/``.

        Each dict contains: ``key`` (str), ``size`` (int),
        ``last_modified`` (ISO-8601 str).
        """
        prefix = f"media/{job_id}/"

        def _list() -> list[dict]:
            objects: list[dict] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._config.bucket, Prefix=prefix
            ):
                for obj in page.get("Contents", []):
                    objects.append(
                        {
                            "key": obj["Key"],
                            "size": obj["Size"],
                            "last_modified": obj["LastModified"].isoformat(),
                        }
                    )
            return objects

        return await asyncio.to_thread(_list)

    async def list_objects_by_prefix(self, prefix: str) -> list[dict]:
        """Return info dicts for every S2 object whose key starts with *prefix*.

        Each dict contains: ``key`` (str), ``size`` (int),
        ``last_modified`` (ISO-8601 str).
        """

        def _list() -> list[dict]:
            objects: list[dict] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._config.bucket, Prefix=prefix
            ):
                for obj in page.get("Contents", []):
                    objects.append(
                        {
                            "key": obj["Key"],
                            "size": obj["Size"],
                            "last_modified": obj["LastModified"].isoformat(),
                        }
                    )
            return objects

        return await asyncio.to_thread(_list)

    async def delete_job_objects(self, job_id: str) -> int:
        """Delete all S2 objects under ``media/{job_id}/`` and return the number deleted.

        Uses the S3 ``delete_objects`` batch API (up to 1 000 keys per call).
        Logs a warning for any per-key errors returned by the API rather than
        raising, so a partial failure does not propagate to callers.
        """
        prefix = f"media/{job_id}/"

        def _delete() -> int:
            keys: list[dict] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append({"Key": obj["Key"]})

            if not keys:
                return 0

            # delete_objects accepts at most 1 000 keys per call.
            deleted = 0
            for i in range(0, len(keys), 1000):
                batch = keys[i : i + 1000]
                resp = self._s3.delete_objects(
                    Bucket=self._config.bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                errors = resp.get("Errors", [])
                if errors:
                    logger.warning(
                        "S2 delete_objects: some keys could not be deleted",
                        extra={"job_id": job_id, "errors": errors},
                    )
                deleted += len(batch) - len(errors)

            return deleted

        return await asyncio.to_thread(_delete)


# ---------------------------------------------------------------------------
# Stub (used when S2 credentials are not configured)
# ---------------------------------------------------------------------------


class _StubS2Client:
    """No-op stub returned by :func:`make_s2_client` when credentials are absent."""

    def generate_presigned_url(self, key: str, expires: int | None = None) -> str:  # noqa: ARG002
        logger.debug("StubS2Client.generate_presigned_url (no-op)", extra={"key": key})
        return ""

    async def head_object(self, key: str) -> dict | None:  # noqa: ARG002
        logger.debug("StubS2Client.head_object (no-op)", extra={"key": key})
        return None

    async def get_object_stream(self, key: str) -> AsyncIterator[bytes]:  # noqa: ARG002
        logger.debug("StubS2Client.get_object_stream (no-op)", extra={"key": key})
        if False:  # pragma: no cover — makes this an async generator without dead code
            yield b""

    async def list_job_objects(self, job_id: str) -> list[dict]:  # noqa: ARG002
        logger.debug("StubS2Client.list_job_objects (no-op)", extra={"job_id": job_id})
        return []

    async def list_objects_by_prefix(self, prefix: str) -> list[dict]:  # noqa: ARG002
        logger.debug("StubS2Client.list_objects_by_prefix (no-op)", extra={"prefix": prefix})
        return []

    async def delete_job_objects(self, job_id: str) -> int:  # noqa: ARG002
        logger.debug("StubS2Client.delete_job_objects (no-op)", extra={"job_id": job_id})
        return 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_s2_client(settings: "IranSettings | None" = None) -> IranS2Client | _StubS2Client:
    """Return a configured :class:`IranS2Client`, or a no-op stub.

    If *settings* is ``None`` the cached singleton from
    :func:`iran.config.get_settings` is used.  When S2 credentials are not
    configured (empty strings) the stub is returned so the application starts
    up without raising.
    """
    if settings is None:
        from iran.config import get_settings

        settings = get_settings()

    if not (settings.S2_ENDPOINT_URL and settings.S2_ACCESS_KEY and settings.S2_SECRET_KEY):
        logger.info("S2 credentials not configured — using stub client")
        return _StubS2Client()

    config = IranS2Config(
        endpoint_url=settings.S2_ENDPOINT_URL,
        access_key=settings.S2_ACCESS_KEY,
        secret_key=settings.S2_SECRET_KEY,
        bucket=settings.S2_BUCKET,
        presign_expire_seconds=settings.S2_PRESIGN_EXPIRE_SECONDS,
    )
    logger.info("S2 client configured", extra={"endpoint": config.endpoint_url})
    return IranS2Client(config)
