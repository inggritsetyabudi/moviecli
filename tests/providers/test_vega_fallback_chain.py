import pytest

from moviebox_api.constants import SubjectType
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream
from moviebox_api.source import SourceResolver, vega_fallback_chain


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


def _streams(source):
    return [ProviderStream(url="https://example.com/stream.m3u8", source=source)]


def test_default_chain_is_the_verified_modules():
    assert vega_fallback_chain() == ("vega", "vega:4khdhub", "vega:castle", "vega:1flex")


@pytest.mark.asyncio
async def test_moviebox_skips_dead_vega_and_uses_4khdhub(monkeypatch):
    """autoEmbed empty -> next verified module wins, and is reported honestly."""

    def _fake_get_provider(name=None):
        if name == "vega:4khdhub":
            return _Provider(_item(), _streams("4khdhub"))
        return _Provider(None, [])

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    monkeypatch.delenv("MOVIEBOX_VEGA_FALLBACKS", raising=False)
    resolver = SourceResolver("moviebox")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert item is not None
    assert streams == _streams("4khdhub")
    assert resolver.resolved_provider_name == "vega:4khdhub"
    assert resolver.fallback_reason is not None


@pytest.mark.asyncio
async def test_yflix_falls_back_to_vega_chain(monkeypatch):
    def _fake_get_provider(name=None):
        if name == "vega":
            return _Provider(_item(), _streams("vega"))
        return _Provider(None, [], error=RuntimeError("empty reply"))

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    monkeypatch.delenv("MOVIEBOX_VEGA_FALLBACKS", raising=False)
    resolver = SourceResolver("yflix")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert streams == _streams("vega")
    assert resolver.resolved_provider_name == "vega"


@pytest.mark.asyncio
async def test_explicit_vega_module_does_not_retry_itself(monkeypatch):
    calls = []

    def _fake_get_provider(name=None):
        calls.append(name)
        if name == "vega:castle":
            return _Provider(_item(), _streams("castle"))
        return _Provider(None, [])

    monkeypatch.setattr("moviebox_api.source.get_provider", _fake_get_provider)
    monkeypatch.delenv("MOVIEBOX_VEGA_FALLBACKS", raising=False)
    # Primary provider is constructed in __init__ too, so the first call
    # is for vega:4khdhub itself; the chain must not call it a second time.
    resolver = SourceResolver("vega:4khdhub")
    item, streams, _subtitles = await resolver.resolve("Interstellar", SubjectType.MOVIES)
    assert streams == _streams("castle")
    assert resolver.resolved_provider_name == "vega:castle"
    assert calls.count("vega:4khdhub") == 1


@pytest.mark.asyncio
async def test_env_override_replaces_chain(monkeypatch):
    monkeypatch.setenv("MOVIEBOX_VEGA_FALLBACKS", "vega:castle")
    assert vega_fallback_chain() == ("vega:castle",)


@pytest.mark.asyncio
async def test_env_override_ignores_invalid_entries(monkeypatch):
    monkeypatch.setenv("MOVIEBOX_VEGA_FALLBACKS", "bogus,,vega:castle")
    assert vega_fallback_chain() == ("vega:castle",)
