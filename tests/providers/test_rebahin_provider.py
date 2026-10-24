import base64
import pytest
import httpx

from moviebox_api.constants import SubjectType
from moviebox_api.providers.rebahin_provider import RebahinProvider


def test_rebahin_provider_metadata():
    provider = RebahinProvider()
    assert provider.name == "rebahin"
    assert "rebahinxxi3.mom" in provider._base_url


@pytest.mark.asyncio
async def test_rebahin_search_parsing(monkeypatch: pytest.MonkeyPatch):
    provider = RebahinProvider()

    search_html = """
    <div class="movies-list">
        <div class="ml-item">
            <a href="/nonton-avatar-the-way-of-water-2022-sub-indo/" class="ml-mask" title="Avatar: The Way of Water (2022)">
                <span class="mli-quality">720p</span>
                <img src="/poster.jpg" />
                <span class="mli-mvi-year">2022</span>
            </a>
        </div>
        <div class="ml-item">
            <a href="/series/nonton-avatar-the-last-airbender-live-action/" class="ml-mask" title="Avatar: The Last Airbender: Season 1 (2024)">
                <span class="mli-quality">HD</span>
                <span class="mli-mvi-year">2024</span>
            </a>
        </div>
    </div>
    """

    class _FakeClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, text=search_html, request=httpx.Request("GET", url))

    provider._client = _FakeClient()

    results = await provider.search("Avatar", SubjectType.ALL)
    assert len(results) == 2
    assert results[0].title == "Avatar: The Way of Water (2022)"
    assert results[0].year == 2022
    assert results[0].subject_type == SubjectType.MOVIES
    assert results[0].payload["quality"] == "720p"

    assert results[1].title == "Avatar: The Last Airbender: Season 1 (2024)"
    assert results[1].year == 2024
    assert results[1].subject_type == SubjectType.TV_SERIES

    # Filter by TV_SERIES
    tv_results = await provider.search("Avatar", SubjectType.TV_SERIES)
    assert len(tv_results) == 1
    assert tv_results[0].subject_type == SubjectType.TV_SERIES


@pytest.mark.asyncio
async def test_rebahin_resolve_streams_embed(monkeypatch: pytest.MonkeyPatch):
    provider = RebahinProvider()

    raw_embed_1 = "https://199.87.210.226/player/abc123xyz"
    raw_embed_2 = "https://bbfilm.xyz/v/test12345"
    b64_1 = base64.b64encode(raw_embed_1.encode()).decode()
    b64_2 = base64.b64encode(raw_embed_2.encode()).decode()

    play_html = f"""
    <div id="server-list">
        <div class="server" data-iframe="{b64_1}">720p</div>
        <div class="server" data-iframe="{b64_2}">720p</div>
    </div>
    <div id="subl">
        <table>
            <tr><td>indonsia</td><td><a href="https://example.com/sub.vtt">Download</a></td></tr>
        </table>
    </div>
    """

    class _FakeClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, text=play_html, request=httpx.Request("GET", url))

    provider._client = _FakeClient()

    # Disable node decoding for this unit test so it tests embed fallback
    monkeypatch.setattr("shutil.which", lambda prog: None)

    from moviebox_api.providers.models import ProviderSearchResult
    item = ProviderSearchResult(
        id="test-movie",
        title="Test Movie (2024)",
        page_url="https://rebahinxxi3.mom/test-movie-2024/",
        subject_type=SubjectType.MOVIES,
    )

    streams = await provider.resolve_streams(item)
    assert len(streams) == 2
    assert streams[0].url == raw_embed_1
    assert streams[0].source == "rebahin-embed"
    assert streams[0].quality == "720p"
    assert len(streams[0].subtitles) == 1
    assert streams[0].subtitles[0].language == "indonesian"
    assert streams[0].subtitles[0].url == "https://example.com/sub.vtt"

    assert streams[1].url == raw_embed_2


@pytest.mark.asyncio
async def test_rebahin_resolve_series_episode(monkeypatch: pytest.MonkeyPatch):
    provider = RebahinProvider()

    raw_embed_ep1 = "https://199.87.210.226/player/ep1"
    raw_embed_ep2 = "https://199.87.210.226/player/ep2"
    b64_1 = base64.b64encode(raw_embed_ep1.encode()).decode()
    b64_2 = base64.b64encode(raw_embed_ep2.encode()).decode()

    play_html = f"""
    <div id="server-list">
        <div class="server" id="episode-21" data-iframe="{b64_1}">E 1</div>
        <div class="server" id="episode-22" data-iframe="{b64_2}">E 2</div>
    </div>
    """

    class _FakeClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, text=play_html, request=httpx.Request("GET", url))

    provider._client = _FakeClient()
    monkeypatch.setattr("shutil.which", lambda prog: None)

    from moviebox_api.providers.models import ProviderSearchResult
    item = ProviderSearchResult(
        id="test-series",
        title="Test Series (2024)",
        page_url="https://rebahinxxi3.mom/series/test-series-2024/",
        subject_type=SubjectType.TV_SERIES,
    )

    # Request episode 2
    streams_ep2 = await provider.resolve_streams(item, episode=2)
    assert len(streams_ep2) == 1
    assert streams_ep2[0].url == raw_embed_ep2
