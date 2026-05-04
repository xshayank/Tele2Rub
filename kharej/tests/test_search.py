"""Tests for the Kharej search handlers and searcher modules.

All external network calls (yt-dlp, Spotify GraphQL, musicdl) and S3
operations are mocked — no live API calls are made.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is importable
_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Kharej searcher unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_youtube_search_returns_results() -> None:
    """youtube_search returns parsed result dicts from yt-dlp metadata."""
    fake_entry = {
        "id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration": 213,
        "upload_date": "20091025",
        "timestamp": 1256428800,
    }
    fake_info = {"entries": [fake_entry]}

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False): return fake_info

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from kharej.searchers.youtube import youtube_search

        results = await youtube_search("rick astley", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["video_id"] == "dQw4w9WgXcQ"
    assert "Rick Astley" in r["title"]
    assert r["duration"] == "3:33"
    assert r["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # upload date/timestamp fields must be present
    assert r["upload_date"] == "2009-10-25"
    assert r["upload_timestamp"] == 1256428800
    # No s2 passed → no thumbnail_key
    assert "thumbnail_key" not in r


@pytest.mark.asyncio
async def test_youtube_search_no_date_returns_none() -> None:
    """youtube_search returns None for upload_date/upload_timestamp when yt-dlp omits them."""
    fake_entry = {
        "id": "abc123",
        "title": "No Date Video",
        "uploader": "Someone",
        "duration": 60,
        # no "upload_date" or "timestamp" keys
    }
    fake_info = {"entries": [fake_entry]}

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False): return fake_info

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("no date", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] is None
    assert r["upload_timestamp"] is None


@pytest.mark.asyncio
async def test_youtube_search_date_only_derives_timestamp() -> None:
    """youtube_search derives upload_timestamp from upload_date when timestamp is absent."""
    fake_entry = {
        "id": "xyz789",
        "title": "Date Only Video",
        "uploader": "Channel",
        "duration": 120,
        "upload_date": "20230803",
        # no "timestamp" key
    }
    fake_info = {"entries": [fake_entry]}

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False): return fake_info

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("date only", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] == "2023-08-03"
    # timestamp derived from 2023-08-03 UTC midnight
    from datetime import datetime, timezone
    expected_ts = int(datetime(2023, 8, 3, tzinfo=timezone.utc).timestamp())
    assert r["upload_timestamp"] == expected_ts


@pytest.mark.asyncio
async def test_youtube_search_uploads_thumbnail_to_s3() -> None:
    """youtube_search with s2 client uploads thumbnail and returns key."""
    fake_entry = {
        "id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration": 213,
    }
    fake_info = {"entries": [fake_entry]}

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False): return fake_info

    fake_s2 = MagicMock()
    fake_s2.head_object = MagicMock(return_value=None)  # not cached
    fake_s2.upload_file = MagicMock()

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        with patch(
            "kharej.searchers.common.upload_thumb_to_s3",
            new=AsyncMock(return_value="thumbs/search/yt/dQw4w9WgXcQ.jpg"),
        ):
            from importlib import reload
            import kharej.searchers.youtube as yt_mod
            reload(yt_mod)
            results = await yt_mod.youtube_search("rick astley", limit=1, s2=fake_s2)

    assert len(results) == 1
    assert results[0].get("thumbnail_key") == "thumbs/search/yt/dQw4w9WgXcQ.jpg"


@pytest.mark.asyncio
async def test_youtube_search_resolves_missing_date_via_full_fetch() -> None:
    """Stage B: per-video full fetch resolves upload_date when flat search omits it."""
    flat_entry = {
        "id": "vid001",
        "title": "Missing Date Video",
        "uploader": "Channel",
        "duration": 90,
        # no upload_date or timestamp
    }

    call_count = [0]

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            call_count[0] += 1
            if "ytsearch" in url:
                return {"entries": [flat_entry]}
            # Per-video full fetch returns date
            return {"upload_date": "20230101", "timestamp": 1672531200}

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("missing date", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] == "2023-01-01"
    assert r["upload_timestamp"] == 1672531200
    assert call_count[0] >= 2  # flat search + at least one per-video fetch


@pytest.mark.asyncio
async def test_youtube_search_full_fetch_failure_falls_back_to_none() -> None:
    """Stage B: if per-video fetch raises, upload_date stays None and no crash."""
    flat_entry = {
        "id": "vid002",
        "title": "Error Video",
        "uploader": "Channel",
        "duration": 45,
        # no upload_date
    }

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            if "ytsearch" in url:
                return {"entries": [flat_entry]}
            raise RuntimeError("Full fetch failed")

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("error video", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] is None
    assert r["upload_timestamp"] is None


@pytest.mark.asyncio
async def test_youtube_search_skips_full_fetch_when_already_present() -> None:
    """Stage B is not triggered when Stage A already provides upload_date."""
    fake_entry = {
        "id": "vid003",
        "title": "Has Date Video",
        "uploader": "Channel",
        "duration": 120,
        "upload_date": "20221115",
        "timestamp": 1668470400,
    }
    fake_info = {"entries": [fake_entry]}

    extract_info_mock = MagicMock(return_value=fake_info)

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        extract_info = extract_info_mock

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("has date", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] == "2022-11-15"
    assert r["upload_timestamp"] == 1668470400
    # extract_info called exactly once (flat search only — Stage B not triggered)
    extract_info_mock.assert_called_once()


@pytest.mark.asyncio
async def test_youtube_search_empty_query_returns_empty() -> None:
    """youtube_search with yt-dlp raising an exception returns empty list."""

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            raise RuntimeError("yt-dlp failed")

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        results = await yt_mod.youtube_search("anything", limit=5)

    assert results == []


@pytest.mark.asyncio
async def test_youtube_search_stage_b_uses_cookies() -> None:
    """Stage B: _fetch_video_date includes cookiefile in ydl_opts when _COOKIES_PATH exists."""
    flat_entry = {
        "id": "vid004",
        "title": "Cookie Test Video",
        "uploader": "Channel",
        "duration": 60,
        # no upload_date — triggers Stage B
    }

    captured_opts: list[dict] = []

    class FakeYDL:
        def __init__(self, opts):
            captured_opts.append(dict(opts))

        def __enter__(self): return self
        def __exit__(self, *a): pass

        def extract_info(self, url, download=False):
            if "ytsearch" in url:
                return {"entries": [flat_entry]}
            return {"upload_date": "20240101", "timestamp": 1704067200}

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        # Patch os.path.isfile so _COOKIES_PATH appears to exist
        with patch("kharej.searchers.youtube.os.path.isfile", return_value=True):
            results = await yt_mod.youtube_search("cookie test", limit=1)

    assert len(results) == 1
    # At least two YDL instances created: one for flat search (Stage A), one for Stage B
    assert len(captured_opts) >= 2
    # The Stage B call (full-video fetch, no extract_flat) should carry the cookiefile
    stage_b_opts = [o for o in captured_opts if not o.get("extract_flat")]
    assert stage_b_opts, "No Stage B YDL call found"
    assert stage_b_opts[0].get("cookiefile") == yt_mod._COOKIES_PATH
    # Stage A should also carry the cookiefile
    stage_a_opts = [o for o in captured_opts if o.get("extract_flat")]
    assert stage_a_opts, "No Stage A YDL call found"
    assert stage_a_opts[0].get("cookiefile") == yt_mod._COOKIES_PATH


@pytest.mark.asyncio
async def test_youtube_search_stage_b_logs_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Stage B: extract_info raising logs a warning with video_id; result stays None (no crash)."""
    flat_entry = {
        "id": "vid005",
        "title": "Bot Check Video",
        "uploader": "Channel",
        "duration": 30,
        # no upload_date — triggers Stage B
    }

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def extract_info(self, url, download=False):
            if "ytsearch" in url:
                return {"entries": [flat_entry]}
            raise RuntimeError("bot check")

    with patch.dict("sys.modules", {"yt_dlp": MagicMock(YoutubeDL=FakeYDL)}):
        from importlib import reload
        import kharej.searchers.youtube as yt_mod
        reload(yt_mod)
        with caplog.at_level(logging.WARNING, logger="kharej.searchers.youtube"):
            results = await yt_mod.youtube_search("bot check test", limit=1)

    # Search must still return a result (no crash)
    assert len(results) == 1
    r = results[0]
    assert r["upload_date"] is None
    assert r["upload_timestamp"] is None

    # A warning log line must contain the video_id and the error message
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "vid005" in r.getMessage() and "bot check" in r.getMessage()
        for r in warning_records
    ), f"Expected warning with video_id and error; got: {[r.getMessage() for r in warning_records]}"


@pytest.mark.asyncio
async def test_spotify_search_returns_categories() -> None:
    """spotify_search returns tracks, albums, playlists from spotify_search_multi."""
    fake_data = {
        "tracks": [
            {"title": "Song A", "artists": "Artist A", "url": "https://open.spotify.com/track/AAAAAAAAAAAAAAAAAAAAAA", "cover_url": "", "type": "track"}
        ],
        "albums": [
            {"name": "Album X", "artists": "Artist A", "url": "https://open.spotify.com/album/BBBBBBBBBBBBBBBBBBBBBB", "cover_url": "", "type": "album"}
        ],
        "playlists": [],
    }
    with patch(
        "rubetunes.spotify_meta.spotify_search_multi",
        return_value=fake_data,
    ):
        from importlib import reload
        import kharej.searchers.spotify as sp_mod
        reload(sp_mod)
        result = await sp_mod.spotify_search("artist a", limit_per_category=3)

    assert "tracks" in result
    assert "albums" in result
    assert "playlists" in result
    assert result["tracks"][0]["title"] == "Song A"
    assert result["albums"][0]["name"] == "Album X"


@pytest.mark.asyncio
async def test_musicdl_search_returns_text_results() -> None:
    """musicdl_search returns plain text results with no cover images."""
    from rubetunes.providers.musicdl.models import MusicdlSearchResult, MusicdlTrack

    fake_track = MusicdlTrack(
        song_name="Bohemian Rhapsody",
        singers="Queen",
        source="NeteaseMusicClient",
        duration="5:55",
    )
    fake_result = MusicdlSearchResult(query="queen", tracks=[fake_track], total=1)

    with patch(
        "rubetunes.providers.musicdl.client.MusicdlClient.search",
        new=AsyncMock(return_value=fake_result),
    ):
        from importlib import reload
        import kharej.searchers.musicdl as mdl_mod
        reload(mdl_mod)
        results = await mdl_mod.musicdl_search("queen", limit=5)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Bohemian Rhapsody"
    assert r["artist"] == "Queen"
    assert r["source"] == "NeteaseMusicClient"
    # musicdl results have no thumbnail/cover fields
    assert "thumbnail_key" not in r
    assert "cover_key" not in r


# ---------------------------------------------------------------------------
# Dispatcher integration: handle_search_request
# ---------------------------------------------------------------------------


def _make_dispatcher():
    """Build a Dispatcher with all external dependencies mocked."""
    from kharej.access_control import AccessControl
    from kharej.dispatcher import Dispatcher
    from kharej.settings import KharejSettings

    settings = KharejSettings()
    rubika = MagicMock()
    rubika.send = AsyncMock()
    s2 = MagicMock()
    s2.head_object = MagicMock(return_value={"size": 100})
    access = AccessControl(settings=settings)
    return Dispatcher(rubika=rubika, s2=s2, settings=settings, access=access), rubika


@pytest.mark.asyncio
async def test_dispatcher_search_youtube_sends_result() -> None:
    """Dispatcher.handle_search_request sends SearchResult for youtube."""
    from kharej.contracts import SearchRequest, SearchResult

    dispatcher, rubika = _make_dispatcher()

    msg = SearchRequest(
        ts=datetime.now(tz=timezone.utc),
        request_id="req-001",
        platform="youtube",
        query="test",
        limit=2,
    )

    with patch(
        "kharej.searchers.youtube.youtube_search",
        new=AsyncMock(
            return_value=[
                {"title": "T", "channel": "C", "duration": "1:00", "video_id": "xyz", "url": "https://www.youtube.com/watch?v=xyz"}
            ]
        ),
    ):
        await dispatcher.handle_search_request(msg)

    rubika.send.assert_awaited_once()
    sent = rubika.send.call_args[0][0]
    assert isinstance(sent, SearchResult)
    assert sent.request_id == "req-001"
    assert sent.platform == "youtube"
    assert len(sent.results) > 0


@pytest.mark.asyncio
async def test_dispatcher_search_unsupported_platform_sends_failed() -> None:
    """Dispatcher.handle_search_request sends SearchFailed for unknown platform."""
    from kharej.contracts import SearchFailed, SearchRequest

    dispatcher, rubika = _make_dispatcher()

    # Manually craft a message with a bad platform by bypassing Pydantic validation
    msg = SearchRequest(
        ts=datetime.now(tz=timezone.utc),
        request_id="req-bad",
        platform="youtube",  # valid for Pydantic; we'll override below
        query="test",
        limit=2,
    )
    object.__setattr__(msg, "platform", "unsupported_platform")

    await dispatcher.handle_search_request(msg)

    rubika.send.assert_awaited_once()
    sent = rubika.send.call_args[0][0]
    assert isinstance(sent, SearchFailed)
    assert sent.request_id == "req-bad"
    assert "unsupported" in sent.error.lower()


@pytest.mark.asyncio
async def test_dispatcher_search_exception_sends_failed() -> None:
    """Dispatcher.handle_search_request sends SearchFailed if searcher raises."""
    from kharej.contracts import SearchFailed, SearchRequest

    dispatcher, rubika = _make_dispatcher()

    msg = SearchRequest(
        ts=datetime.now(tz=timezone.utc),
        request_id="req-err",
        platform="musicdl",
        query="crash test",
        limit=5,
    )

    with patch(
        "kharej.searchers.musicdl.musicdl_search",
        new=AsyncMock(side_effect=RuntimeError("musicdl exploded")),
    ):
        await dispatcher.handle_search_request(msg)

    rubika.send.assert_awaited_once()
    sent = rubika.send.call_args[0][0]
    assert isinstance(sent, SearchFailed)
    assert "musicdl exploded" in sent.error
