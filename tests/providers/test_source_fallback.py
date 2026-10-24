import pytest

from moviebox_api.constants import SubjectType
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle
from moviebox_api.source import SourceResolver


class _Provider:
    def __init__(self, item, streams, error=None):
        self._item = item
        self._streams = streams
        self._error = error

    async def search_best_match(self, query, subject_type, *, year=None):
        if self._error is not None:
            raise self._error
        return self._item

    async def resolve_streams(self, item, *, season=0, episode=0):
        if self._error is not None:
            raise self._error
        return self._streams

    async def resolve_subtitles(self, item, *, season=0, episode=0):
        return []


def _item(title="Interstellar"):
    return ProviderSearchResult(
        id="1", title=title, page_url="https://example.com/x", subject_type=SubjectType.MOVIES
    )


@pytest.mark.asyncio
async def test_moviebox_empty_result_falls_back_to_vega(monkeypatch):
    vega_streams = [ProviderStream(url="https://example.com/stream.m3u8", source="vega")]

    def _fake_get_provider(name=None):
        if name == "vega":
            return _Provider(_item(), vega_streams)
        return _Provider(None, [])

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    resolver = SourceResolver("moviebox")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert item is not None and item.title == "Interstellar"
    assert streams == vega_streams
    assert resolver.resolved_provider_name == "vega"
    assert resolver.fallback_reason is not None


@pytest.mark.asyncio
async def test_moviebox_error_falls_back_to_vega(monkeypatch):
    vega_streams = [ProviderStream(url="https://example.com/stream.m3u8", source="vega")]

    def _fake_get_provider(name=None):
        if name == "vega":
            return _Provider(_item(), vega_streams)
        return _Provider(None, [], error=RuntimeError("403 invalid region"))

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    resolver = SourceResolver("moviebox")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert streams == vega_streams
    assert resolver.resolved_provider_name == "vega"


@pytest.mark.asyncio
async def test_non_moviebox_provider_is_not_fallen_back(monkeypatch):
    def _fake_get_provider(name=None):
        return _Provider(None, [])

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    resolver = SourceResolver("vega")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert item is None
    assert streams == []
    assert resolver.resolved_provider_name == "vega"
