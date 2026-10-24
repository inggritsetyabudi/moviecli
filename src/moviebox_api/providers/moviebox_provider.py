"""Native Moviebox provider adapter with resilient fallback."""

from __future__ import annotations

import asyncio
import logging

from moviebox_api.constants import SubjectType
from moviebox_api.core import Search
from moviebox_api.download import DownloadableMovieFilesDetail, DownloadableTVSeriesFilesDetail
from moviebox_api.models import SearchResultsItem
from moviebox_api.providers.base import BaseStreamProvider
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle
from moviebox_api.requests import Session

logger = logging.getLogger(__name__)


class MovieboxProvider(BaseStreamProvider):
    """Adapter over the existing moviebox-api integration with fallback."""

    name = "moviebox"

    def __init__(self, session: Session | None = None):
        self._session = session or Session()

    async def search(
        self,
        query: str,
        subject_type: SubjectType,
        *,
        year: int | None = None,
        limit: int = 20,
    ) -> list[ProviderSearchResult]:
        mapped: list[ProviderSearchResult] = []
        try:
            search = Search(self._session, query, subject_type)
            results = await asyncio.wait_for(search.get_content_model(), timeout=2.5)
            for item in results.items[:limit]:
                mapped.append(
                    ProviderSearchResult(
                        id=item.subjectId,
                        title=item.title,
                        page_url=item.page_url,
                        subject_type=item.subjectType,
                        year=item.releaseDate.year,
                        payload={"raw_item": item},
                    )
                )
        except Exception as exc:
            logger.debug("Moviebox native search failed: %s", exc)
            mapped = []

        if not mapped:
            try:
                from moviebox_api.stremio.catalog import search_cinemeta_catalog

                cinemeta_items = await search_cinemeta_catalog(query, subject_type=subject_type, limit=limit)
                for item in cinemeta_items:
                    mapped.append(
                        ProviderSearchResult(
                            id=item.subjectId,
                            title=item.title,
                            page_url=f"https://www.imdb.com/title/{item.imdbId}/" if item.imdbId else "",
                            subject_type=item.subjectType,
                            year=item.year,
                            payload={"imdb_id": item.imdbId, "cinemeta_item": item},
                        )
                    )
            except Exception as exc:
                logger.debug("Cinemeta fallback search failed: %s", exc)

        if year:
            return [item for item in mapped if item.year == year] or mapped

        return mapped

    async def resolve_streams(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderStream]:
        raw_item: SearchResultsItem | None = item.payload.get("raw_item")
        if raw_item is not None:
            try:
                if item.subject_type == SubjectType.TV_SERIES and (season <= 0 or episode <= 0):
                    season = 1
                    episode = 1

                if item.subject_type == SubjectType.TV_SERIES:
                    details = DownloadableTVSeriesFilesDetail(self._session, raw_item)
                    metadata = await asyncio.wait_for(
                        details.get_content_model(season=season, episode=episode), timeout=3.0
                    )
                else:
                    details = DownloadableMovieFilesDetail(self._session, raw_item)
                    metadata = await asyncio.wait_for(
                        details.get_content_model(), timeout=3.0
                    )

                subtitles = [
                    ProviderSubtitle(
                        url=str(caption.url),
                        language=caption.lan,
                        label=caption.lanName,
                    )
                    for caption in metadata.captions
                ]

                streams = [
                    ProviderStream(
                        url=str(media.url),
                        source="moviebox",
                        quality=f"{media.resolution}P",
                        size=media.size,
                        audio=media.audio,
                        subtitles=subtitles,
                    )
                    for media in sorted(metadata.downloads, key=lambda entry: entry.resolution, reverse=True)
                ]
                if streams:
                    return streams
            except Exception as exc:
                logger.debug("Moviebox native stream resolution failed: %s", exc)

        # Resilient fallback via SourceResolver
        from moviebox_api.source import SourceResolver

        resolver = SourceResolver(provider_name="vega")
        _, streams, _ = await resolver.resolve(
            title=item.title,
            subject_type=item.subject_type,
            year=item.year,
            season=season,
            episode=episode,
            imdb_id=item.payload.get("imdb_id"),
        )
        return streams

    async def resolve_subtitles(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderSubtitle]:
        streams = await self.resolve_streams(item, season=season, episode=episode)
        if not streams:
            return []

        dedup: dict[str, ProviderSubtitle] = {}
        for stream in streams:
            for subtitle in stream.subtitles:
                dedup[subtitle.url] = subtitle
        return list(dedup.values())

