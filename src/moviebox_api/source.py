"""High-level helpers for multi-provider stream resolution."""

import os

from moviebox_api.constants import SubjectType
from moviebox_api.providers import SUPPORTED_ANIME_PROVIDERS, get_provider, normalize_provider_name
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle

# Vega modules verified live on 2026-10-24 (search_best_match + resolve,
# both a movie "Interstellar" and a series "Breaking Bad" S1E1):
#   vega (autoEmbed, the default), 4khdhub, castle, 1flex.
# Other active manifest modules failed that same test (no best match,
# zero streams, timeout, or runtime error): 1cinevood, katmovies, mod,
# uhd, movieBoxWeb, gokuHD, eonMovies, movies4u, hdhub4u, showbox,
# everything, vega (the "vega" module itself), drive, world4u,
# netflixMirror, primeMirror, disneyMirror, torrentio, zeefliz.
# The chain is tried in order and stops at the first module that returns
# playable streams, so a single dead module no longer ends the request.
# Override with MOVIEBOX_VEGA_FALLBACKS="vega,vega:4khdhub" (comma list).
DEFAULT_VEGA_FALLBACK_CHAIN = ("vega", "vega:4khdhub", "vega:castle", "vega:1flex")
ENV_VEGA_FALLBACK_CHAIN_KEY = "MOVIEBOX_VEGA_FALLBACKS"


def vega_fallback_chain() -> tuple[str, ...]:
    """Return the Vega fallback chain, honouring the env override."""

    raw = os.getenv(ENV_VEGA_FALLBACK_CHAIN_KEY, "").strip()
    if not raw:
        return DEFAULT_VEGA_FALLBACK_CHAIN
    entries = tuple(
        entry.strip() for entry in raw.split(",") if entry.strip()
    )
    valid = tuple(
        entry
        for entry in entries
        if entry == "vega" or entry.startswith("vega:")
    )
    return valid or DEFAULT_VEGA_FALLBACK_CHAIN


def _vega_module_name(provider_name: str) -> str | None:
    """Normalise a Vega provider name to its module ("vega" == autoEmbed)."""

    if provider_name == "vega":
        return "autoEmbed"
    if provider_name.startswith("vega:"):
        return provider_name.split(":", maxsplit=1)[1].strip() or None
    return None


class SourceResolver:
    """Resolve stream links from a selected provider.

    The native ``moviebox`` provider is currently unreliable: its search
    endpoint returns zero results for every title and its play/download
    endpoints answer ``403 invalid region`` from several networks, so a
    default ``moviebox`` resolution used to end with "No matching item
    found" even though the working ``vega`` provider resolves the same
    title fine.  ``yflix`` is dead too (``yflix.to`` answers with an empty
    reply).  For movies/TV, a failed or empty ``moviebox``/``yflix``/``vega``
    resolution therefore falls back through the verified Vega chain
    (:func:`vega_fallback_chain`) automatically; the provider that
    actually produced the result is recorded on
    :attr:`resolved_provider_name` so callers can show it honestly instead
    of claiming the requested provider succeeded.
    """

    def __init__(self, provider_name: str | None = None):
        self.provider_name = provider_name
        self.provider = get_provider(provider_name)
        self.resolved_provider_name: str | None = None
        self.fallback_reason: str | None = None

    async def resolve(
        self,
        title: str,
        subject_type: SubjectType,
        *,
        year: int | None = None,
        season: int = 0,
        episode: int = 0,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
    ) -> tuple[ProviderSearchResult | None, list[ProviderStream], list[ProviderSubtitle]]:
        if subject_type == SubjectType.ANIME:
            from moviebox_api.anime import resolve_anime_source_query

            selected_provider_name: str | None = None
            if self.provider_name:
                try:
                    normalized_provider_name = normalize_provider_name(self.provider_name)
                except ValueError:
                    normalized_provider_name = ''
                if normalized_provider_name in SUPPORTED_ANIME_PROVIDERS:
                    selected_provider_name = normalized_provider_name

            item, streams, subtitles, resolved_provider = await resolve_anime_source_query(
                title,
                year=year,
                season=season,
                episode=episode,
                provider_name=selected_provider_name,
            )
            self.resolved_provider_name = resolved_provider
            return (item, streams, subtitles)

        try:
            result = await self._resolve_with_provider(
                self.provider,
                title=title,
                subject_type=subject_type,
                year=year,
                season=season,
                episode=episode,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
            )
        except Exception as exc:
            result = None
            primary_error: Exception | None = exc
        else:
            primary_error = None

        primary_name = normalize_provider_name(self.provider_name) if self.provider_name else "moviebox"
        if result is not None and (result[0] is not None and result[1]):
            self.resolved_provider_name = primary_name
            return result

        # Only movies/TV fall back, and only from providers known to be
        # dead or to be a single Vega module: moviebox (search 0 results,
        # play 403 invalid region), yflix (empty reply), and any vega
        # module (one module failing must not end the request when another
        # verified module can serve the same title).  Other explicit
        # providers keep the old behaviour: their error propagates and an
        # empty result stays empty.
        primary_module = _vega_module_name(primary_name)
        is_fallback_eligible = primary_name in ("moviebox", "yflix") or primary_module is not None
        if not is_fallback_eligible:
            if primary_error is not None:
                raise primary_error
            self.resolved_provider_name = primary_name
            return result if result is not None else (None, [], [])

        reason = (
            f"{primary_name} failed ({primary_error})"
            if primary_error is not None
            else f"{primary_name} returned no playable streams"
        )
        for candidate in vega_fallback_chain():
            # Skip the module already tried as the primary provider:
            # "vega" is the default autoEmbed module.
            if primary_module is not None and _vega_module_name(candidate) == primary_module:
                continue
            try:
                fallback = await self._resolve_with_provider(
                    get_provider(candidate),
                    title=title,
                    subject_type=subject_type,
                    year=year,
                    season=season,
                    episode=episode,
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                )
            except Exception:
                fallback = None

            if fallback is not None and fallback[0] is not None and fallback[1]:
                self.resolved_provider_name = candidate
                self.fallback_reason = reason
                return fallback

        if primary_error is not None:
            raise primary_error
        self.resolved_provider_name = primary_name
        return result if result is not None else (None, [], [])

    async def _resolve_with_provider(
        self,
        provider,
        *,
        title: str,
        subject_type: SubjectType,
        year: int | None,
        season: int,
        episode: int,
        imdb_id: str | None,
        tmdb_id: int | None,
    ) -> tuple[ProviderSearchResult | None, list[ProviderStream], list[ProviderSubtitle]]:
        item: ProviderSearchResult | None = None

        id_builder = getattr(provider, 'build_item_from_ids', None)
        if callable(id_builder):
            try:
                item = await id_builder(
                    subject_type=subject_type,
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    title=title,
                    year=year,
                )
            except Exception:
                item = None

        if item is None:
            item = await provider.search_best_match(
                query=title,
                subject_type=subject_type,
                year=year,
            )
        if item is None:
            return (None, [], [])

        streams = await provider.resolve_streams(
            item,
            season=season,
            episode=episode,
        )

        subtitles = await provider.resolve_subtitles(
            item,
            season=season,
            episode=episode,
        )

        if not subtitles:
            for stream in streams:
                subtitles.extend(stream.subtitles)

        return (item, streams, subtitles)
