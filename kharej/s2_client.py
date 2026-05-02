"""Arvan S2 Object Storage client for the Kharej VPS.

Provides a simple synchronous wrapper over ``boto3`` (S3-compatible API) for
Arvan S2 storage, with single-pass SHA-256 streaming, multipart upload support,
per-operation tenacity retries, and structured logging under ``kharej.s2``.

No Rubika or job knowledge lives here — pure S2 I/O.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import boto3
import botocore.client
import botocore.config
import botocore.exceptions
from pydantic import BaseModel
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from kharej.contracts import S2ObjectRef

logger = logging.getLogger("kharej.s2")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class S2Error(Exception):
    """Base class for all S2 client errors."""


class S2NotFound(S2Error):
    """Raised when an S2 object key is not found (outside of head_object)."""


class S2AccessDenied(S2Error):
    """Raised on access-denied / credential errors from S2."""


class S2UploadFailed(S2Error):
    """Raised when an upload fails after all retries are exhausted."""


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

_NO_RETRY_CODES: frozenset[str] = frozenset(
    {
        "NoSuchKey",
        "NoSuchBucket",
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    }
)

_RETRY_CODES: frozenset[str] = frozenset(
    {
        "InternalError",
        "SlowDown",
        "RequestTimeout",
        "ServiceUnavailable",
    }
)


def _should_retry(exc: Exception) -> bool:
    """Return ``True`` if *exc* is a transient error that should be retried.

    Never-retry codes (surfaced immediately): NoSuchKey, NoSuchBucket,
    AccessDenied, InvalidAccessKeyId, SignatureDoesNotMatch.
    """
    if isinstance(
        exc,
        (
            botocore.exceptions.EndpointConnectionError,
            botocore.exceptions.ConnectionClosedError,
            botocore.exceptions.ReadTimeoutError,
            botocore.exceptions.ResponseStreamingError,
        ),
    ):
        return True

    if isinstance(exc, botocore.exceptions.ClientError):
        code = exc.response["Error"]["Code"]
        if code in _NO_RETRY_CODES:
            return False
        if code in _RETRY_CODES:
            return True
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if http_status >= 500:
            return True
        return False

    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class S2Config(BaseModel):
    """Pydantic v2 configuration model for the Arvan S2 client.

    Load from the environment via :meth:`from_env`.
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "ir-thr-at1"
    multipart_threshold_bytes: int = 100 * 1024 * 1024  # 100 MB
    multipart_chunk_bytes: int = 16 * 1024 * 1024  # 16 MB
    max_attempts: int = 5

    @classmethod
    def from_env(cls) -> S2Config:
        """Construct an :class:`S2Config` from environment variables.

        Required variables
        ------------------
        ``ARVAN_S2_ENDPOINT``, ``ARVAN_S2_ACCESS_KEY_WRITE``,
        ``ARVAN_S2_SECRET_WRITE``, ``ARVAN_S2_BUCKET``.

        Optional
        --------
        ``ARVAN_S2_REGION`` (default: ``"ir-thr-at1"``).

        Raises
        ------
        ValueError
            If any required variable is absent or empty.
        """
        required: dict[str, str] = {
            "endpoint_url": "ARVAN_S2_ENDPOINT",
            "access_key": "ARVAN_S2_ACCESS_KEY_WRITE",
            "secret_key": "ARVAN_S2_SECRET_WRITE",
            "bucket": "ARVAN_S2_BUCKET",
        }
        missing = [env for env in required.values() if not os.environ.get(env)]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        return cls(
            endpoint_url=os.environ["ARVAN_S2_ENDPOINT"],
            access_key=os.environ["ARVAN_S2_ACCESS_KEY_WRITE"],
            secret_key=os.environ["ARVAN_S2_SECRET_WRITE"],
            bucket=os.environ["ARVAN_S2_BUCKET"],
            region=os.environ.get("ARVAN_S2_REGION", "ir-thr-at1"),
        )


# ---------------------------------------------------------------------------
# MIME helper
# ---------------------------------------------------------------------------


def _guess_mime(filename: str) -> str:
    """Guess MIME type from *filename*; defaults to ``application/octet-stream``."""
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# S2Client
# ---------------------------------------------------------------------------


class S2Client:
    """Synchronous boto3 wrapper for Arvan S2 (S3-compatible) storage.

    Parameters
    ----------
    config:
        S2 connection configuration.
    client_factory:
        Optional callable that returns a ``botocore`` S3 client.  Pass only in
        tests to inject a ``moto``- or ``Stubber``-backed client; production
        code leaves this as ``None`` and a default path-style s3v4 client is
        built automatically.
    """

    def __init__(
        self,
        config: S2Config,
        *,
        client_factory: Callable[[], botocore.client.BaseClient] | None = None,
    ) -> None:
        self._config = config
        if client_factory is not None:
            self._client: botocore.client.BaseClient = client_factory()
        else:
            self._client = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key,
                region_name=config.region,
                config=botocore.config.Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": config.max_attempts, "mode": "standard"},
                ),
            )

    # ------------------------------------------------------------------
    # Internal: retry decorator factory
    # ------------------------------------------------------------------

    def _retrying(self, fn: Callable) -> Callable:
        """Wrap *fn* with tenacity retries according to the S2 retry policy."""
        return retry(
            retry=retry_if_exception(_should_retry),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            stop=stop_after_attempt(self._config.max_attempts),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )(fn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path,
        key: str,
        *,
        content_type: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> S2ObjectRef:
        """Upload a local file to S2.

        The file is opened exactly **once**; SHA-256 is computed in the same
        streaming pass that feeds the upload — no double-reads.

        Returns
        -------
        S2ObjectRef
            Reference to the uploaded object with correct SHA-256 and size.
        """
        total = local_path.stat().st_size
        mime = content_type or _guess_mime(local_path.name)
        with local_path.open("rb") as fh:
            return self._do_upload(
                fh,
                key,
                length=total,
                mime=mime,
                on_progress=on_progress,
                extra_metadata=extra_metadata,
            )

    def upload_stream(
        self,
        stream: BinaryIO,
        key: str,
        *,
        length: int,
        content_type: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> S2ObjectRef:
        """Upload a binary stream to S2.

        Parameters
        ----------
        length:
            Total byte count of *stream* (required for multipart threshold
            decision and ``Content-Length`` header).
        """
        mime = content_type or "application/octet-stream"
        return self._do_upload(
            stream,
            key,
            length=length,
            mime=mime,
            on_progress=on_progress,
            extra_metadata=extra_metadata,
        )

    def download_to_file(self, key: str, local_path: Path) -> S2ObjectRef:
        """Download an S2 object to *local_path*.

        Raises
        ------
        S2NotFound
            If *key* does not exist.
        S2AccessDenied
            On access-denied errors.
        """
        cfg = self._config

        def _get() -> dict:
            return self._client.get_object(Bucket=cfg.bucket, Key=key)

        try:
            resp = self._retrying(_get)()
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchKey":
                raise S2NotFound(key) from exc
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise S2AccessDenied(str(exc)) from exc
            raise S2Error(str(exc)) from exc

        mime = resp.get("ContentType", "application/octet-stream")
        hasher = hashlib.sha256()
        total_written = 0
        body = resp["Body"]
        with local_path.open("wb") as fh:
            for chunk in iter(lambda: body.read(256 * 1024), b""):
                hasher.update(chunk)
                fh.write(chunk)
                total_written += len(chunk)

        size = resp.get("ContentLength") or total_written
        return S2ObjectRef(key=key, size=size, mime=mime, sha256=hasher.hexdigest())

    def get_object_bytes(self, key: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """Read a small S2 object fully into memory.

        Raises
        ------
        ValueError
            If the object size (from ``Content-Length`` header or actual data)
            exceeds *max_bytes*.
        S2NotFound
            If *key* does not exist.
        """
        cfg = self._config

        def _get() -> dict:
            return self._client.get_object(Bucket=cfg.bucket, Key=key)

        try:
            resp = self._retrying(_get)()
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchKey":
                raise S2NotFound(key) from exc
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise S2AccessDenied(str(exc)) from exc
            raise S2Error(str(exc)) from exc

        content_length: int = resp.get("ContentLength", 0)
        if content_length > max_bytes:
            raise ValueError(
                f"Object '{key}' is {content_length} bytes, exceeds max_bytes={max_bytes}"
            )

        data = resp["Body"].read()
        if len(data) > max_bytes:
            raise ValueError(
                f"Object '{key}' content ({len(data)} bytes) exceeds max_bytes={max_bytes}"
            )
        return data

    def head_object(self, key: str) -> S2ObjectRef | None:
        """Return an :class:`S2ObjectRef` for *key*, or ``None`` on 404.

        Raises on any error other than 404.
        """
        cfg = self._config

        def _head() -> dict:
            return self._client.head_object(Bucket=cfg.bucket, Key=key)

        try:
            resp = self._retrying(_head)()
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if code == "404" or http_status == 404 or code == "NoSuchKey":
                return None
            raise

        return S2ObjectRef(
            key=key,
            size=resp.get("ContentLength", 0),
            mime=resp.get("ContentType", "application/octet-stream"),
            sha256=resp.get("Metadata", {}).get("sha256", ""),
        )

    def delete_object(self, key: str) -> None:
        """Delete a single S2 object (idempotent)."""
        cfg = self._config

        def _delete() -> None:
            self._client.delete_object(Bucket=cfg.bucket, Key=key)

        self._retrying(_delete)()

    def delete_prefix(self, prefix: str) -> int:
        """Delete all objects whose key begins with *prefix*.

        Uses ``ListObjectsV2`` + ``DeleteObjects`` in batches of up to 1 000.

        Returns
        -------
        int
            Number of objects deleted.
        """
        cfg = self._config
        paginator = self._client.get_paginator("list_objects_v2")
        total_deleted = 0
        for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            to_delete = [{"Key": obj["Key"]} for obj in contents]
            self._client.delete_objects(
                Bucket=cfg.bucket,
                Delete={"Objects": to_delete, "Quiet": True},
            )
            total_deleted += len(to_delete)
        return total_deleted

    def list_prefix(self, prefix: str) -> Iterator[S2ObjectRef]:
        """Yield an :class:`S2ObjectRef` for every object under *prefix*."""
        cfg = self._config
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield S2ObjectRef(
                    key=obj["Key"],
                    size=obj.get("Size", 0),
                    mime=_guess_mime(obj["Key"]),
                    sha256="",
                )

    def generate_presigned_get_url(self, key: str, *, expires: int = 3600) -> str:
        """Return a presigned GET URL for *key* valid for *expires* seconds."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._config.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        expires: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """Return a presigned PUT URL for *key* valid for *expires* seconds."""
        params: dict = {"Bucket": self._config.bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=expires,
        )

    # ------------------------------------------------------------------
    # Internal upload logic
    # ------------------------------------------------------------------

    def _do_upload(
        self,
        stream: BinaryIO,
        key: str,
        *,
        length: int,
        mime: str,
        on_progress: Callable[[int, int], None] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> S2ObjectRef:
        """Dispatch to single-shot or multipart upload based on *length*."""
        cfg = self._config
        t0 = time.monotonic()
        is_multipart = length > cfg.multipart_threshold_bytes

        try:
            if is_multipart:
                ref = self._upload_multipart(
                    stream,
                    key,
                    length=length,
                    mime=mime,
                    on_progress=on_progress,
                    extra_metadata=extra_metadata,
                )
            else:
                ref = self._upload_single(
                    stream,
                    key,
                    length=length,
                    mime=mime,
                    on_progress=on_progress,
                    extra_metadata=extra_metadata,
                )
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "s2.upload failed",
                extra={
                    "event": "s2.upload",
                    "key": key,
                    "size": length,
                    "duration_ms": duration_ms,
                    "error_code": code,
                },
            )
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise S2AccessDenied(str(exc)) from exc
            raise S2UploadFailed(str(exc)) from exc
        except S2Error:
            raise
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "s2.upload failed",
                extra={"event": "s2.upload", "key": key, "size": length, "duration_ms": duration_ms},
            )
            raise S2UploadFailed(str(exc)) from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "s2.upload succeeded",
            extra={
                "event": "s2.upload",
                "key": key,
                "size": ref.size,
                "duration_ms": duration_ms,
                "multipart": is_multipart,
            },
        )
        return ref

    def _upload_single(
        self,
        stream: BinaryIO,
        key: str,
        *,
        length: int,
        mime: str,
        on_progress: Callable[[int, int], None] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> S2ObjectRef:
        """Single-shot PutObject upload (≤ multipart_threshold_bytes)."""
        bucket = self._config.bucket

        # Single streaming pass: read and hash simultaneously.
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        while True:
            chunk = stream.read(256 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            chunks.append(chunk)

        data = b"".join(chunks)
        sha256 = hasher.hexdigest()
        actual_size = len(data)

        put_kwargs: dict = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentType": mime,
            "ContentLength": actual_size,
        }
        if extra_metadata:
            put_kwargs["Metadata"] = extra_metadata

        if on_progress:
            on_progress(0, actual_size)

        def _put() -> None:
            self._client.put_object(**put_kwargs)

        self._retrying(_put)()

        if on_progress:
            on_progress(actual_size, actual_size)

        return S2ObjectRef(key=key, size=actual_size, mime=mime, sha256=sha256)

    def _upload_multipart(
        self,
        stream: BinaryIO,
        key: str,
        *,
        length: int,
        mime: str,
        on_progress: Callable[[int, int], None] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> S2ObjectRef:
        """Multipart upload (> multipart_threshold_bytes).

        Hashes each chunk as it is read; aborts the multipart upload on any
        failure before re-raising.
        """
        cfg = self._config
        bucket = cfg.bucket
        chunk_size = cfg.multipart_chunk_bytes

        create_kwargs: dict = {"Bucket": bucket, "Key": key, "ContentType": mime}
        if extra_metadata:
            create_kwargs["Metadata"] = extra_metadata

        def _create() -> dict:
            return self._client.create_multipart_upload(**create_kwargs)

        resp = self._retrying(_create)()
        upload_id: str = resp["UploadId"]

        hasher = hashlib.sha256()
        parts: list[dict] = []
        bytes_done = 0
        part_number = 1
        last_cb_ts = 0.0

        try:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
                pn = part_number
                chunk_data = chunk

                def _upload_part(pn: int = pn, data: bytes = chunk_data) -> dict:
                    return self._client.upload_part(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=pn,
                        Body=data,
                    )

                part_resp = self._retrying(_upload_part)()
                parts.append({"PartNumber": pn, "ETag": part_resp["ETag"]})
                bytes_done += len(chunk)
                part_number += 1

                if on_progress:
                    now = time.monotonic()
                    if now - last_cb_ts >= 0.1:
                        on_progress(bytes_done, length)
                        last_cb_ts = now

            def _complete() -> dict:
                return self._client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )

            self._retrying(_complete)()

        except Exception:
            try:
                self._client.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            except Exception:
                pass
            raise

        if on_progress:
            on_progress(bytes_done, length)

        return S2ObjectRef(
            key=key,
            size=bytes_done,
            mime=mime,
            sha256=hasher.hexdigest(),
        )
