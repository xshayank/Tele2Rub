"""Unit tests for Track B Step 6 — Iran S2 Read-Only Client.

Coverage:
- IranS2Config / IranS2Client importability
- generate_presigned_url: URL contains endpoint and key
- generate_presigned_url: custom ``expires`` parameter honoured
- head_object: returns dict for an existing object
- head_object: returns None on HTTP 404 (NoSuchKey) without raising
- get_object_stream: yields all chunks from a multi-chunk response
- list_job_objects: returns one entry per S2 object under the job prefix
- list_job_objects: empty prefix → empty list
- make_s2_client: returns stub when credentials are not configured
- make_s2_client: returns IranS2Client when credentials are present
- _StubS2Client: no-op methods work without error
"""

from __future__ import annotations

import sys
import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import botocore.exceptions
import pytest
from botocore.stub import Stubber

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENDPOINT = "https://s3.ir-thr-at1.arvanstorage.ir"
_BUCKET = "test-bucket"
_ACCESS_KEY = "read-access-key"
_SECRET_KEY = "read-secret-key"
_JOB_ID = "550e8400-e29b-41d4-a716-446655440000"
_KEY = f"media/{_JOB_ID}/Shape_of_You.flac"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    from iran.s2_client import IranS2Config

    defaults = dict(
        endpoint_url=_ENDPOINT,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        bucket=_BUCKET,
        presign_expire_seconds=3600,
    )
    defaults.update(overrides)
    return IranS2Config(**defaults)


def _make_raw_s3():
    """Return a real boto3 S3 client pointing at the fake endpoint (no real calls)."""
    return boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name="us-east-1",
    )


def _make_client(raw_s3=None, **config_overrides):
    from iran.s2_client import IranS2Client

    if raw_s3 is None:
        raw_s3 = _make_raw_s3()
    return IranS2Client(_make_config(**config_overrides), client_factory=raw_s3)


# ---------------------------------------------------------------------------
# 1. Importability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_iran_s2_config_importable(self):
        from iran.s2_client import IranS2Config  # noqa: F401

    def test_iran_s2_client_importable(self):
        from iran.s2_client import IranS2Client  # noqa: F401

    def test_s2_client_protocol_importable(self):
        from iran.s2_client import S2ClientProtocol  # noqa: F401

    def test_make_s2_client_importable(self):
        from iran.s2_client import make_s2_client  # noqa: F401

    def test_stub_importable(self):
        from iran.s2_client import _StubS2Client  # noqa: F401


# ---------------------------------------------------------------------------
# 2. IranS2Config
# ---------------------------------------------------------------------------


class TestIranS2Config:
    def test_fields_set_correctly(self):
        cfg = _make_config()
        assert cfg.endpoint_url == _ENDPOINT
        assert cfg.access_key == _ACCESS_KEY
        assert cfg.secret_key == _SECRET_KEY
        assert cfg.bucket == _BUCKET
        assert cfg.presign_expire_seconds == 3600

    def test_default_expire_is_3600(self):
        from iran.s2_client import IranS2Config

        cfg = IranS2Config(
            endpoint_url=_ENDPOINT,
            access_key=_ACCESS_KEY,
            secret_key=_SECRET_KEY,
            bucket=_BUCKET,
        )
        assert cfg.presign_expire_seconds == 3600

    def test_custom_expire(self):
        cfg = _make_config(presign_expire_seconds=7200)
        assert cfg.presign_expire_seconds == 7200


# ---------------------------------------------------------------------------
# 3. generate_presigned_url
# ---------------------------------------------------------------------------


class TestGeneratePresignedUrl:
    """generate_presigned_url is purely client-side signing — no network call."""

    def test_url_contains_endpoint(self):
        client = _make_client()
        url = client.generate_presigned_url(_KEY)
        assert _ENDPOINT in url

    def test_url_contains_key(self):
        client = _make_client()
        url = client.generate_presigned_url(_KEY)
        assert "Shape_of_You.flac" in url

    def test_url_contains_bucket(self):
        client = _make_client()
        url = client.generate_presigned_url(_KEY)
        assert _BUCKET in url

    def test_url_is_string(self):
        client = _make_client()
        url = client.generate_presigned_url(_KEY)
        assert isinstance(url, str)
        assert url.startswith("https://")

    def test_custom_expires_accepted(self):
        """Custom expires parameter does not raise and still returns a URL."""
        client = _make_client()
        url = client.generate_presigned_url(_KEY, expires=300)
        assert isinstance(url, str)
        assert len(url) > 0


# ---------------------------------------------------------------------------
# 4. head_object
# ---------------------------------------------------------------------------


class TestHeadObject:
    @pytest.mark.asyncio
    async def test_returns_dict_for_existing_key(self):
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "head_object",
                {
                    "ContentLength": 42_000_000,
                    "ContentType": "audio/flac",
                    "ETag": '"abc123"',
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={"Bucket": _BUCKET, "Key": _KEY},
            )
            client = _make_client(raw_s3)
            result = await client.head_object(_KEY)

        assert result is not None
        assert result["size"] == 42_000_000
        assert result["content_type"] == "audio/flac"
        assert result["etag"] == '"abc123"'

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_client_error(
                "head_object",
                service_error_code="NoSuchKey",
                service_message="The specified key does not exist.",
                http_status_code=404,
            )
            client = _make_client(raw_s3)
            result = await client.head_object("missing/key.flac")

        assert result is None

    @pytest.mark.asyncio
    async def test_re_raises_non_404_errors(self):
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_client_error(
                "head_object",
                service_error_code="AccessDenied",
                service_message="Access Denied",
                http_status_code=403,
            )
            client = _make_client(raw_s3)
            with pytest.raises(botocore.exceptions.ClientError):
                await client.head_object(_KEY)


# ---------------------------------------------------------------------------
# 5. get_object_stream
# ---------------------------------------------------------------------------


class TestGetObjectStream:
    @pytest.mark.asyncio
    async def test_yields_all_chunks(self):
        """Stubber returns a 3-chunk body; the async generator must yield all three."""
        chunk1 = b"A" * 1024
        chunk2 = b"B" * 1024
        chunk3 = b"C" * 512
        body = BytesIO(chunk1 + chunk2 + chunk3)

        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "get_object",
                {
                    "Body": body,
                    "ContentLength": len(chunk1) + len(chunk2) + len(chunk3),
                    "ContentType": "audio/flac",
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={"Bucket": _BUCKET, "Key": _KEY},
            )
            client = _make_client(raw_s3)
            collected = b""
            async for chunk in client.get_object_stream(_KEY):
                collected += chunk

        assert len(collected) == len(chunk1) + len(chunk2) + len(chunk3)
        assert collected == chunk1 + chunk2 + chunk3

    @pytest.mark.asyncio
    async def test_yields_bytes_type(self):
        body = BytesIO(b"hello world")
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "get_object",
                {
                    "Body": body,
                    "ContentLength": 11,
                    "ContentType": "application/octet-stream",
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={"Bucket": _BUCKET, "Key": _KEY},
            )
            client = _make_client(raw_s3)
            chunks = []
            async for chunk in client.get_object_stream(_KEY):
                assert isinstance(chunk, bytes)
                chunks.append(chunk)
        assert b"".join(chunks) == b"hello world"


# ---------------------------------------------------------------------------
# 6. list_job_objects
# ---------------------------------------------------------------------------


class TestListJobObjects:
    @pytest.mark.asyncio
    async def test_returns_entries_for_three_part_job(self):
        """list_job_objects must return one dict per S2 object under the job prefix."""
        _ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "Contents": [
                        {
                            "Key": f"media/{_JOB_ID}/album-part1.zip",
                            "Size": 100_000,
                            "LastModified": _ts,
                        },
                        {
                            "Key": f"media/{_JOB_ID}/album-part2.zip",
                            "Size": 200_000,
                            "LastModified": _ts,
                        },
                        {
                            "Key": f"media/{_JOB_ID}/album-part3.zip",
                            "Size": 50_000,
                            "LastModified": _ts,
                        },
                    ],
                    "IsTruncated": False,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={
                    "Bucket": _BUCKET,
                    "Prefix": f"media/{_JOB_ID}/",
                },
            )
            client = _make_client(raw_s3)
            result = await client.list_job_objects(_JOB_ID)

        assert len(result) == 3
        assert result[0]["key"] == f"media/{_JOB_ID}/album-part1.zip"
        assert result[0]["size"] == 100_000
        assert result[1]["key"] == f"media/{_JOB_ID}/album-part2.zip"
        assert result[2]["key"] == f"media/{_JOB_ID}/album-part3.zip"

    @pytest.mark.asyncio
    async def test_empty_prefix_returns_empty_list(self):
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "Contents": [],
                    "IsTruncated": False,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={
                    "Bucket": _BUCKET,
                    "Prefix": f"media/{_JOB_ID}/",
                },
            )
            client = _make_client(raw_s3)
            result = await client.list_job_objects(_JOB_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_job_objects_order_preserved(self):
        """Objects must be returned in the order the paginator yields them."""
        _ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        keys = [
            f"media/{_JOB_ID}/track-{i}.flac" for i in range(1, 6)
        ]
        raw_s3 = _make_raw_s3()
        with Stubber(raw_s3) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "Contents": [
                        {"Key": k, "Size": 1000 * (i + 1), "LastModified": _ts}
                        for i, k in enumerate(keys)
                    ],
                    "IsTruncated": False,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
                expected_params={
                    "Bucket": _BUCKET,
                    "Prefix": f"media/{_JOB_ID}/",
                },
            )
            client = _make_client(raw_s3)
            result = await client.list_job_objects(_JOB_ID)

        assert [r["key"] for r in result] == keys


# ---------------------------------------------------------------------------
# 7. Stub client
# ---------------------------------------------------------------------------


class TestStubS2Client:
    def test_generate_presigned_url_returns_empty_string(self):
        from iran.s2_client import _StubS2Client

        stub = _StubS2Client()
        assert stub.generate_presigned_url("some/key.flac") == ""

    @pytest.mark.asyncio
    async def test_head_object_returns_none(self):
        from iran.s2_client import _StubS2Client

        stub = _StubS2Client()
        result = await stub.head_object("some/key.flac")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_object_stream_yields_nothing(self):
        from iran.s2_client import _StubS2Client

        stub = _StubS2Client()
        chunks = []
        async for chunk in stub.get_object_stream("some/key.flac"):
            chunks.append(chunk)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_list_job_objects_returns_empty_list(self):
        from iran.s2_client import _StubS2Client

        stub = _StubS2Client()
        result = await stub.list_job_objects("some-job-id")
        assert result == []


# ---------------------------------------------------------------------------
# 8. make_s2_client factory
# ---------------------------------------------------------------------------


class TestMakeS2Client:
    def test_returns_stub_when_no_endpoint(self, monkeypatch):
        from iran.s2_client import make_s2_client, _StubS2Client
        from iran.config import IranSettings

        settings = IranSettings(S2_ENDPOINT_URL="", S2_ACCESS_KEY="", S2_SECRET_KEY="")
        result = make_s2_client(settings)
        assert isinstance(result, _StubS2Client)

    def test_returns_stub_when_endpoint_only(self, monkeypatch):
        from iran.s2_client import make_s2_client, _StubS2Client
        from iran.config import IranSettings

        settings = IranSettings(
            S2_ENDPOINT_URL=_ENDPOINT,
            S2_ACCESS_KEY="",
            S2_SECRET_KEY="",
        )
        result = make_s2_client(settings)
        assert isinstance(result, _StubS2Client)

    def test_returns_real_client_when_credentials_configured(self):
        from iran.s2_client import make_s2_client, IranS2Client
        from iran.config import IranSettings

        settings = IranSettings(
            S2_ENDPOINT_URL=_ENDPOINT,
            S2_ACCESS_KEY=_ACCESS_KEY,
            S2_SECRET_KEY=_SECRET_KEY,
            S2_BUCKET=_BUCKET,
        )
        result = make_s2_client(settings)
        assert isinstance(result, IranS2Client)
