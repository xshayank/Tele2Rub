"""Tests for kharej/s2_client.py (Step 3 — Arvan S2 Client).

Uses ``moto`` for the majority of tests (real boto3 calls intercepted by moto).
Uses ``botocore.stub.Stubber`` for tests that require injecting specific errors
(retry behaviour, abort-on-failure).
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import boto3
import botocore.exceptions
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from kharej.contracts import S2ObjectRef, make_media_key
from kharej.s2_client import (
    S2AccessDenied,
    S2Client,
    S2Config,
    S2NotFound,
    S2UploadFailed,
    _guess_mime,
    _should_retry,
)

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

_BUCKET = "test-bucket"
_REGION = "us-east-1"  # moto default; Arvan region used only in prod
_FAKE_ENDPOINT = "https://s3.ir-thr-at1.arvanstorage.ir"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> S2Config:
    defaults = {
        "endpoint_url": _FAKE_ENDPOINT,
        "access_key": "test-access-key",
        "secret_key": "test-secret-key",
        "bucket": _BUCKET,
        "region": _REGION,
        "max_attempts": 1,  # no retries by default — keeps tests fast
    }
    defaults.update(overrides)
    return S2Config(**defaults)


def _make_moto_s3():
    """Return a moto-backed boto3 S3 client with the test bucket created."""
    import botocore.config as _botocore_config

    client = boto3.client(
        "s3",
        region_name=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=_botocore_config.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )
    client.create_bucket(Bucket=_BUCKET)
    return client


def _make_s2(client, **config_overrides) -> S2Client:
    return S2Client(_make_config(**config_overrides), client_factory=lambda: client)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. test_config_from_env_missing_raises
# ---------------------------------------------------------------------------


def test_config_from_env_missing_raises(monkeypatch) -> None:
    for var in (
        "ARVAN_S2_ENDPOINT",
        "ARVAN_S2_ACCESS_KEY_WRITE",
        "ARVAN_S2_SECRET_WRITE",
        "ARVAN_S2_BUCKET",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ValueError) as exc_info:
        S2Config.from_env()

    msg = str(exc_info.value)
    # All four missing vars should be mentioned
    assert "ARVAN_S2_ENDPOINT" in msg
    assert "ARVAN_S2_ACCESS_KEY_WRITE" in msg
    assert "ARVAN_S2_SECRET_WRITE" in msg
    assert "ARVAN_S2_BUCKET" in msg


# ---------------------------------------------------------------------------
# 2. test_config_from_env_ok
# ---------------------------------------------------------------------------


def test_config_from_env_ok(monkeypatch) -> None:
    monkeypatch.setenv("ARVAN_S2_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("ARVAN_S2_ACCESS_KEY_WRITE", "mykey")
    monkeypatch.setenv("ARVAN_S2_SECRET_WRITE", "mysecret")
    monkeypatch.setenv("ARVAN_S2_BUCKET", "my-bucket")
    monkeypatch.setenv("ARVAN_S2_REGION", "eu-west-1")

    cfg = S2Config.from_env()

    assert cfg.endpoint_url == "https://s3.example.com"
    assert cfg.access_key == "mykey"
    assert cfg.secret_key == "mysecret"
    assert cfg.bucket == "my-bucket"
    assert cfg.region == "eu-west-1"
    # defaults
    assert cfg.multipart_threshold_bytes == 100 * 1024 * 1024
    assert cfg.multipart_chunk_bytes == 16 * 1024 * 1024
    assert cfg.max_attempts == 5


# ---------------------------------------------------------------------------
# 3. test_upload_file_small_roundtrip
# ---------------------------------------------------------------------------


@mock_aws
def test_upload_file_small_roundtrip(tmp_path: Path) -> None:
    data = b"A" * 1024
    local = tmp_path / "track.flac"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    ref = s2.upload_file(local, "media/job1/track.flac")

    assert isinstance(ref, S2ObjectRef)
    assert ref.key == "media/job1/track.flac"
    assert ref.size == 1024
    assert ref.sha256 == _sha256(data)
    assert ref.mime == "audio/flac"

    # head_object should confirm presence
    head = s2.head_object("media/job1/track.flac")
    assert head is not None
    assert head.size == 1024


# ---------------------------------------------------------------------------
# 4. test_upload_file_large_multipart
# ---------------------------------------------------------------------------


@mock_aws
def test_upload_file_large_multipart(tmp_path: Path) -> None:
    # S3 multipart requires each part (except last) to be >= 5 MB.
    chunk = 6 * 1024 * 1024  # 6 MB per chunk — above S3 minimum
    data = b"B" * (3 * chunk)  # 18 MB — forces 3 parts
    local = tmp_path / "album.zip"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    # Patch create_multipart_upload to detect multipart usage
    multipart_used = []
    _original_create = s3.create_multipart_upload

    def _spy_create(**kwargs):
        multipart_used.append(True)
        return _original_create(**kwargs)

    s3.create_multipart_upload = _spy_create

    s2 = _make_s2(
        s3,
        multipart_threshold_bytes=chunk,
        multipart_chunk_bytes=chunk,
        max_attempts=3,
    )

    ref = s2.upload_file(local, "media/job2/album.zip")

    assert multipart_used, "Expected multipart upload to be used"
    assert ref.sha256 == _sha256(data)
    assert ref.size == len(data)


# ---------------------------------------------------------------------------
# 5. test_upload_stream_roundtrip
# ---------------------------------------------------------------------------


@mock_aws
def test_upload_stream_roundtrip() -> None:
    data = b"C" * 2048
    stream = BytesIO(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    ref = s2.upload_stream(
        stream, "media/job3/audio.mp3", length=len(data), content_type="audio/mpeg"
    )

    assert ref.size == len(data)
    assert ref.sha256 == _sha256(data)
    assert ref.mime == "audio/mpeg"


# ---------------------------------------------------------------------------
# 6. test_upload_progress_callback_invoked
# ---------------------------------------------------------------------------


@mock_aws
def test_upload_progress_callback_invoked(tmp_path: Path) -> None:
    data = b"D" * 512
    local = tmp_path / "clip.mp3"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    calls: list[tuple[int, int]] = []
    s2.upload_file(local, "media/job4/clip.mp3", on_progress=lambda d, t: calls.append((d, t)))

    assert len(calls) >= 2, "Progress callback should be called at least twice"
    # First call: start (0, total)
    assert calls[0] == (0, len(data))
    # Last call: finish (total, total)
    assert calls[-1] == (len(data), len(data))


# ---------------------------------------------------------------------------
# 7. test_download_to_file_roundtrip
# ---------------------------------------------------------------------------


@mock_aws
def test_download_to_file_roundtrip(tmp_path: Path) -> None:
    data = b"E" * 4096
    upload_path = tmp_path / "source.mp3"
    upload_path.write_bytes(data)
    download_path = tmp_path / "dest.mp3"

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    s2.upload_file(upload_path, "media/job5/source.mp3")
    ref = s2.download_to_file("media/job5/source.mp3", download_path)

    assert download_path.read_bytes() == data
    assert ref.sha256 == _sha256(data)
    assert ref.key == "media/job5/source.mp3"


# ---------------------------------------------------------------------------
# 8. test_get_object_bytes_max_bytes_enforced
# ---------------------------------------------------------------------------


@mock_aws
def test_get_object_bytes_max_bytes_enforced(tmp_path: Path) -> None:
    data = b"F" * (1 * 1024 * 1024)  # 1 MB
    local = tmp_path / "big.bin"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)
    s2.upload_file(local, "media/job6/big.bin")

    with pytest.raises(ValueError, match="max_bytes"):
        s2.get_object_bytes("media/job6/big.bin", max_bytes=512_000)


# ---------------------------------------------------------------------------
# 9. test_head_object_missing_returns_none
# ---------------------------------------------------------------------------


@mock_aws
def test_head_object_missing_returns_none() -> None:
    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    result = s2.head_object("does/not/exist.flac")
    assert result is None


# ---------------------------------------------------------------------------
# 10. test_delete_object
# ---------------------------------------------------------------------------


@mock_aws
def test_delete_object(tmp_path: Path) -> None:
    data = b"G" * 128
    local = tmp_path / "tmp.ogg"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    key = "media/job7/tmp.ogg"
    s2.upload_file(local, key)
    assert s2.head_object(key) is not None

    s2.delete_object(key)
    assert s2.head_object(key) is None


# ---------------------------------------------------------------------------
# 11. test_delete_prefix_batches
# ---------------------------------------------------------------------------


@mock_aws
def test_delete_prefix_batches(tmp_path: Path) -> None:
    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    prefix = "batch/job8/"
    keys = [f"{prefix}track{i}.mp3" for i in range(5)]
    for key in keys:
        local = tmp_path / f"track{key[-5:]}"
        local.write_bytes(b"H" * 64)
        s2.upload_file(local, key)

    deleted = s2.delete_prefix(prefix)
    assert deleted == 5

    for key in keys:
        assert s2.head_object(key) is None


# ---------------------------------------------------------------------------
# 12. test_list_prefix
# ---------------------------------------------------------------------------


@mock_aws
def test_list_prefix(tmp_path: Path) -> None:
    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    prefix = "listing/job9/"
    uploads: dict[str, int] = {}
    for i in range(3):
        key = f"{prefix}track{i}.mp3"
        data = b"I" * (64 * (i + 1))
        local = tmp_path / f"t{i}.mp3"
        local.write_bytes(data)
        s2.upload_file(local, key)
        uploads[key] = len(data)

    listed = list(s2.list_prefix(prefix))

    assert len(listed) == 3
    listed_keys = {ref.key for ref in listed}
    assert listed_keys == set(uploads.keys())
    for ref in listed:
        assert ref.size == uploads[ref.key]


# ---------------------------------------------------------------------------
# 13. test_presigned_get_and_put_urls_contain_signature
# ---------------------------------------------------------------------------


@mock_aws
def test_presigned_get_and_put_urls_contain_signature(tmp_path: Path) -> None:
    data = b"J" * 32
    local = tmp_path / "song.mp3"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)
    s2.upload_file(local, "media/job10/song.mp3")

    get_url = s2.generate_presigned_get_url("media/job10/song.mp3", expires=300)
    put_url = s2.generate_presigned_put_url("media/job10/song2.mp3", expires=300)

    assert "X-Amz-Signature" in get_url
    assert "X-Amz-Signature" in put_url


# ---------------------------------------------------------------------------
# 14. test_retry_on_5xx
# ---------------------------------------------------------------------------


def test_retry_on_5xx(tmp_path: Path) -> None:
    """Two SlowDown 503s then a 200 — exactly three attempts."""
    data = b"K" * 128
    local = tmp_path / "retry.mp3"
    local.write_bytes(data)

    config = _make_config(max_attempts=3)
    raw_client = boto3.client(
        "s3",
        region_name=_REGION,
        endpoint_url=_FAKE_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    with patch("time.sleep"):  # speed up exponential backoff
        with Stubber(raw_client) as stubber:
            for _ in range(2):
                stubber.add_client_error(
                    "put_object",
                    service_error_code="SlowDown",
                    service_message="Please reduce your request rate.",
                    http_status_code=503,
                )
            stubber.add_response("put_object", {})

            s2 = S2Client(config, client_factory=lambda: raw_client)
            ref = s2.upload_file(local, "media/job11/retry.mp3")

            stubber.assert_no_pending_responses()  # all 3 queued responses consumed

    assert ref.key == "media/job11/retry.mp3"


# ---------------------------------------------------------------------------
# 15. test_no_retry_on_access_denied
# ---------------------------------------------------------------------------


def test_no_retry_on_access_denied(tmp_path: Path) -> None:
    """AccessDenied must surface immediately without retrying."""
    data = b"L" * 128
    local = tmp_path / "denied.mp3"
    local.write_bytes(data)

    config = _make_config(max_attempts=3)  # would retry if not for AccessDenied
    raw_client = boto3.client(
        "s3",
        region_name=_REGION,
        endpoint_url=_FAKE_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    with Stubber(raw_client) as stubber:
        stubber.add_client_error(
            "put_object",
            service_error_code="AccessDenied",
            service_message="Access Denied",
            http_status_code=403,
        )

        s2 = S2Client(config, client_factory=lambda: raw_client)

        with pytest.raises(S2AccessDenied):
            s2.upload_file(local, "media/job12/denied.mp3")

        # If retry had happened, Stubber would have no more queued responses
        # and the second put_object would raise StubResponseError.
        stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# 16. test_multipart_abort_on_failure
# ---------------------------------------------------------------------------


@mock_aws
def test_multipart_abort_on_failure(tmp_path: Path) -> None:
    """Fail second upload_part → abort_multipart_upload called, S2UploadFailed raised."""
    # S3 requires each part (except last) to be >= 5 MB.
    chunk = 6 * 1024 * 1024
    data = b"M" * (3 * chunk)  # 18 MB → 3 parts
    local = tmp_path / "fail_mp.bin"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    config = _make_config(
        multipart_threshold_bytes=chunk,
        multipart_chunk_bytes=chunk,
        max_attempts=1,  # one attempt per operation — fail immediately
    )

    call_count = [0]
    abort_called = [False]
    _original_upload_part = s3.upload_part
    _original_abort = s3.abort_multipart_upload

    def _failing_upload_part(**kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise botocore.exceptions.ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "Injected failure"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "UploadPart",
            )
        return _original_upload_part(**kwargs)

    def _tracking_abort(**kwargs):
        abort_called[0] = True
        return _original_abort(**kwargs)

    s3.upload_part = _failing_upload_part
    s3.abort_multipart_upload = _tracking_abort

    s2 = S2Client(config, client_factory=lambda: s3)

    with pytest.raises(S2UploadFailed):
        s2.upload_file(local, "media/job13/fail_mp.bin")

    assert abort_called[0], "abort_multipart_upload should have been called"


# ---------------------------------------------------------------------------
# 17. test_sha256_is_streamed_not_double_read
# ---------------------------------------------------------------------------


@mock_aws
def test_sha256_is_streamed_not_double_read(tmp_path: Path) -> None:
    """Path.open('rb') is called exactly once during upload_file."""
    data = b"N" * 1024
    local = tmp_path / "once.bin"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    open_rb_count = [0]
    _original_open = Path.open

    def _counting_open(self, mode="r", **kwargs):
        if mode == "rb":
            open_rb_count[0] += 1
        return _original_open(self, mode, **kwargs)

    with patch.object(Path, "open", _counting_open):
        ref = s2.upload_file(local, "media/job14/once.bin")

    assert open_rb_count[0] == 1, "Expected exactly one open-for-read"
    assert ref.sha256 == _sha256(data)


# ---------------------------------------------------------------------------
# 18. test_keys_use_contracts_helpers
# ---------------------------------------------------------------------------


@mock_aws
def test_keys_use_contracts_helpers(tmp_path: Path) -> None:
    """S2Client works correctly with keys produced by kharej.contracts helpers."""
    job_id = "550e8400-e29b-41d4-a716-446655440000"
    key = make_media_key(job_id, "Shape_of_You.flac")

    data = b"O" * 256
    local = tmp_path / "Shape_of_You.flac"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    ref = s2.upload_file(local, key)

    assert ref.key == f"media/{job_id}/Shape_of_You.flac"
    assert ref.sha256 == _sha256(data)

    head = s2.head_object(key)
    assert head is not None
    assert head.key == key


# ---------------------------------------------------------------------------
# Bonus: _should_retry unit tests
# ---------------------------------------------------------------------------


def test_should_retry_endpoint_connection_error() -> None:
    exc = botocore.exceptions.EndpointConnectionError(endpoint_url="https://x.com")
    assert _should_retry(exc) is True


def test_should_retry_access_denied_returns_false() -> None:
    exc = botocore.exceptions.ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "PutObject",
    )
    assert _should_retry(exc) is False


def test_should_retry_slow_down_returns_true() -> None:
    exc = botocore.exceptions.ClientError(
        {"Error": {"Code": "SlowDown", "Message": "Slow"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
        "PutObject",
    )
    assert _should_retry(exc) is True


def test_should_retry_5xx_returns_true() -> None:
    exc = botocore.exceptions.ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "Down"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
        "PutObject",
    )
    assert _should_retry(exc) is True


def test_should_retry_nosuchkey_returns_false() -> None:
    exc = botocore.exceptions.ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "GetObject",
    )
    assert _should_retry(exc) is False


def test_should_retry_generic_exception_returns_false() -> None:
    assert _should_retry(ValueError("oops")) is False


# ---------------------------------------------------------------------------
# Bonus: _guess_mime
# ---------------------------------------------------------------------------


def test_guess_mime_known_extension() -> None:
    assert _guess_mime("track.mp3") == "audio/mpeg"
    assert _guess_mime("video.mp4") == "video/mp4"


def test_guess_mime_unknown_extension() -> None:
    assert _guess_mime("file.unknownxyz") == "application/octet-stream"


# ---------------------------------------------------------------------------
# Bonus: get_object_bytes success path
# ---------------------------------------------------------------------------


@mock_aws
def test_get_object_bytes_success(tmp_path: Path) -> None:
    data = b"P" * 512
    local = tmp_path / "small.bin"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(s3)
    s2.upload_file(local, "media/job15/small.bin")

    result = s2.get_object_bytes("media/job15/small.bin")
    assert result == data


# ---------------------------------------------------------------------------
# Bonus: download_to_file on missing key raises S2NotFound
# ---------------------------------------------------------------------------


@mock_aws
def test_download_to_file_missing_raises(tmp_path: Path) -> None:
    s3 = _make_moto_s3()
    s2 = _make_s2(s3)

    with pytest.raises(S2NotFound):
        s2.download_to_file("no/such/key.mp3", tmp_path / "out.mp3")


# ---------------------------------------------------------------------------
# Bonus: multipart progress callbacks
# ---------------------------------------------------------------------------


@mock_aws
def test_multipart_progress_callback(tmp_path: Path) -> None:
    # S3 requires each part (except last) to be >= 5 MB.
    chunk = 6 * 1024 * 1024  # 6 MB per chunk
    data = b"Q" * (3 * chunk)
    local = tmp_path / "mp_prog.bin"
    local.write_bytes(data)

    s3 = _make_moto_s3()
    s2 = _make_s2(
        s3,
        multipart_threshold_bytes=chunk,
        multipart_chunk_bytes=chunk,
        max_attempts=1,
    )

    calls: list[tuple[int, int]] = []
    s2.upload_file(local, "media/job16/mp_prog.bin", on_progress=lambda d, t: calls.append((d, t)))

    assert len(calls) >= 1
    # Last call should report all bytes done
    assert calls[-1] == (len(data), len(data))
