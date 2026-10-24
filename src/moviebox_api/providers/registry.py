"""Provider registry and selection helpers."""

import os

from moviebox_api.providers.base import BaseStreamProvider
from moviebox_api.providers.moviebox_provider import MovieboxProvider
from moviebox_api.providers.oplovers_provider import OploversProvider
from moviebox_api.providers.otakudesu_provider import OtakudesuProvider
from moviebox_api.providers.rebahin_provider import RebahinProvider
from moviebox_api.providers.samehadaku_provider import SamehadakuProvider
from moviebox_api.providers.vega_provider import VegaProvider
from moviebox_api.providers.yflix_provider import YflixProvider

ENVIRONMENT_PROVIDER_KEY = "MOVIEBOX_PROVIDER"
DEFAULT_PROVIDER = "vega"
SUPPORTED_MEDIA_PROVIDERS = ("vega", "rebahin", "moviebox", "yflix")
SUPPORTED_ANIME_PROVIDERS = ("samehadaku", "oplovers", "otakudesu")
SUPPORTED_PROVIDERS = SUPPORTED_MEDIA_PROVIDERS + SUPPORTED_ANIME_PROVIDERS


def normalize_provider_name(value: str | None) -> str:
    if value is None:
        return DEFAULT_PROVIDER

    raw_value = value.strip()
    if not raw_value:
        return DEFAULT_PROVIDER

    provider_name, separator, provider_suffix = raw_value.partition(":")
    normalized_provider_name = provider_name.strip().lower()

    if separator:
        if normalized_provider_name != "vega":
            allowed = ", ".join(SUPPORTED_PROVIDERS)
            raise ValueError(
                f"Unsupported provider '{value}'. Choose from: {allowed} "
                "or use dynamic syntax vega:<providerValue>"
            )

        dynamic_value = provider_suffix.strip()
        if not dynamic_value:
            raise ValueError("Invalid vega provider format. Use 'vega:<providerValue>'")
        return f"vega:{dynamic_value}"

    if normalized_provider_name not in SUPPORTED_PROVIDERS:
        allowed = ", ".join(SUPPORTED_PROVIDERS)
        raise ValueError(
            f"Unsupported provider '{value}'. Choose from: {allowed} "
            "or use dynamic syntax vega:<providerValue>"
        )
    return normalized_provider_name


def get_provider(name: str | None = None) -> BaseStreamProvider:
    selected = normalize_provider_name(name or os.getenv(ENVIRONMENT_PROVIDER_KEY, DEFAULT_PROVIDER))

    if selected.startswith("vega:"):
        dynamic_provider_value = selected.split(":", maxsplit=1)[1]
        return VegaProvider(provider_value=dynamic_provider_value)

    if selected == "moviebox":
        return MovieboxProvider()
    if selected == "yflix":
        return YflixProvider()
    if selected == "vega":
        return VegaProvider()
    if selected == "rebahin":
        return RebahinProvider()
    if selected == "samehadaku":
        return SamehadakuProvider()
    if selected == "oplovers":
        return OploversProvider()
    if selected == "otakudesu":
        return OtakudesuProvider()

    raise RuntimeError(f"Unsupported provider '{selected}'")
