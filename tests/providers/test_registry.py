import pytest

from moviebox_api.providers.registry import (
    DEFAULT_PROVIDER,
    ENVIRONMENT_PROVIDER_KEY,
    get_provider,
    normalize_provider_name,
)
from moviebox_api.providers.vega_provider import VegaProvider


def test_normalize_provider_name_defaults_to_moviebox():
    assert normalize_provider_name(None) == DEFAULT_PROVIDER
    assert normalize_provider_name("  ") == DEFAULT_PROVIDER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("moviebox", "moviebox"),
        ("YFLIX", "yflix"),
        ("Vega", "vega"),
        ("rebahin", "rebahin"),
        ("REBAHIN", "rebahin"),
        ("samehadaku", "samehadaku"),
        ("OPLOVERS", "oplovers"),
        ("Otakudesu", "otakudesu"),
    ],
)
def test_normalize_provider_name_supported_values(raw: str, expected: str):
    assert normalize_provider_name(raw) == expected


def test_normalize_provider_name_supports_dynamic_vega():
    assert normalize_provider_name("vega:autoEmbed") == "vega:autoEmbed"
    assert normalize_provider_name("VEGA:my-provider") == "vega:my-provider"
    assert normalize_provider_name("VEGA : autoEmbed") == "vega:autoEmbed"


def test_normalize_provider_name_rejects_invalid_dynamic_vega():
    with pytest.raises(ValueError, match="Invalid vega provider format"):
        normalize_provider_name("vega:")


def test_normalize_provider_name_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported provider"):
        normalize_provider_name("unknown-provider")


def test_get_provider_supports_dynamic_vega_value():
    provider = get_provider("vega:autoEmbed")
    assert isinstance(provider, VegaProvider)
    assert provider.selected_provider_value == "autoEmbed"


def test_get_provider_reads_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENVIRONMENT_PROVIDER_KEY, "vega:autoEmbed")
    provider = get_provider()
    assert isinstance(provider, VegaProvider)
    assert provider.selected_provider_value == "autoEmbed"


def test_get_provider_rebahin():
    from moviebox_api.providers.rebahin_provider import RebahinProvider
    provider = get_provider("rebahin")
    assert isinstance(provider, RebahinProvider)
    assert provider.name == "rebahin"
