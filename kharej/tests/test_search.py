"""Tests for the Kharej search handlers and searcher modules.

All external network calls (yt-dlp, Spotify GraphQL, musicdl) and S3
operations are mocked — no live API calls are made.
"""

from __future__ import annotations

import asyncio
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
    # No s2 passed → no thumbnail_key
    assert "thumbnail_key" not in r


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
