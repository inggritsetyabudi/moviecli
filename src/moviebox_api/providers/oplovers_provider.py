"""Oplovers anime provider."""

from __future__ import annotations

import asyncio
import re
import time
from difflib import SequenceMatcher
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from moviebox_api.constants import SubjectType
from moviebox_api.providers.anime_common import (
    BaseAnimeProvider,
    build_anime_payload,
    configured_base_urls,
    decode_svelte_data,
    dedupe_streams,
    extract_episode_number,
    parse_year,
    provider_result_from_payload,
    quality_rank,
)
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle


class OploversProvider(BaseAnimeProvider):
    """Use Oplovers site indexes and Svelte data payloads."""

    name = 'oplovers'
    env_keys = ('MOVIEBOX_OPLOVERS_URLS', 'MOVIEBOX_OPLOVERS_URL')
    default_base_urls = ('https://coba.oploverz.ltd',)
    # The front-end host is dead at the moment, so site fetches must fail
    # fast instead of burning the full 25s provider timeout twice (async
    # + sync fallback) before the working API path is tried.
    _site_timeout = httpx.Timeout(12.0, connect=6.0)
    api_env_keys = ('MOVIEBOX_OPLOVERS_API_URLS', 'MOVIEBOX_OPLOVERS_API_URL')
    default_api_base_urls = ('https://backapi.oploverz.ac',)
    _series_index_cache: ClassVar[tuple[float, list[tuple[str, str]]] | None] = None
    _series_index_ttl_seconds: ClassVar[int] = 900

    def api_base_urls(self) -> tuple[str, ...]:
        return configured_base_urls(self.api_env_keys, self.default_api_base_urls)

    async def search(
        self,
        query: str,
        subject_type: SubjectType,
        *,
        year: int | None = None,
        limit: int = 20,
    ) -> list[ProviderSearchResult]:
        if subject_type != SubjectType.ANIME:
            return []

        # The front-end host (coba.oploverz.ltd) is currently dead, so the
        # index path raises instead of returning [].  Fall through to the
        # API path instead of letting that failure end the search.
        try:
            indexed_results = await self._search_via_index(query, year=year, limit=limit)
        except Exception:
            indexed_results = []
        if indexed_results:
            return indexed_results[:limit]

        try:
            api_results = await self._search_via_api(query, year=year, limit=limit)
        except Exception:
            api_results = []
        return api_results[:limit]

    async def list_trending(self, *, limit: int = 20) -> list[ProviderSearchResult]:
        payload = await self._api_json('/api/series?page=1')
        items = payload.get('data') if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [self._result_from_api_entry(entry) for entry in items if isinstance(entry, dict)][:limit]

    async def resolve_streams(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderStream]:
        slug = str(item.payload.get('slug') or '').strip()
        content_kind = str(item.payload.get('content_kind') or 'series').strip().lower() or 'series'
        try:
            payload = await self._content_payload(content_kind, slug)
        except Exception:
            payload = {}
        episodes = payload.get('episodes') if isinstance(payload, dict) else []
        if (not isinstance(episodes, list) or not episodes) and content_kind == 'series' and slug:
            # Front-end host is dead, but the API still serves episodes
            # (GET /api/series/<slug>/episodes, newest first, 10 per page).
            api_episode = await self._api_episode(slug, episode if episode > 0 else 1)
            episodes = [api_episode] if api_episode else []
        if not isinstance(episodes, list) or not episodes:
            return []

        target_episode = None
        requested_episode = episode if episode > 0 else 1
        for current in episodes:
            try:
                current_number = int(str(current.get('episodeNumber') or current.get('episode') or '0'))
            except ValueError:
                current_number = 0
            if current_number == requested_episode:
                target_episode = current
                break
        if target_episode is None:
            target_episode = episodes[0]

        if content_kind == 'movie':
            referer = f"{self.base_urls()[0]}/movie/{slug}"
        else:
            referer = f"{self.base_urls()[0]}/series/{slug}/episode/{target_episode.get('episodeNumber')}"
        streams: list[ProviderStream] = []

        for entry in target_episode.get('streamUrl') or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get('url') or '').strip()
            if not url:
                continue
            raw_source = str(entry.get('source') or 'stream').strip().lower() or 'stream'
            streams.extend(
                await self.expand_streams(
                    url=url,
                    source=f'oplovers:{raw_source}',
                    quality=str(entry.get('source') or '').strip() or None,
                    referer=referer,
                )
            )

        for format_entry in target_episode.get('downloadUrl') or []:
            if not isinstance(format_entry, dict):
                continue
            format_label = str(format_entry.get('format') or '').strip()
            for resolution in format_entry.get('resolutions') or []:
                if not isinstance(resolution, dict):
                    continue
                quality = str(resolution.get('quality') or '').strip() or None
                for download_link in resolution.get('download_links') or []:
                    if not isinstance(download_link, dict):
                        continue
                    url = str(download_link.get('url') or '').strip()
                    if not url:
                        continue
                    host = str(download_link.get('host') or 'download').strip().lower() or 'download'
                    streams.extend(
                        await self.expand_streams(
                            url=url,
                            source=f'oplovers:{host}',
                            quality=quality or format_label or None,
                            referer=referer,
                        )
                    )

        resolved_streams = dedupe_streams(streams)
        resolved_streams = self._prefer_non_gdrive_streams(resolved_streams)
        resolved_streams.sort(key=self._stream_sort_key, reverse=True)
        return resolved_streams

    @staticmethod
    def _prefer_non_gdrive_streams(streams: list[ProviderStream]) -> list[ProviderStream]:
        """Drop Google Drive rows when the same quality already has a stronger direct host."""

        preferred_quality_keys = {
            str(stream.quality or '').strip().lower()
            for stream in streams
            if not OploversProvider._is_google_drive_stream(stream)
        }
        if not preferred_quality_keys:
            return list(streams)

        filtered: list[ProviderStream] = []
        for stream in streams:
            quality_key = str(stream.quality or '').strip().lower()
            if quality_key in preferred_quality_keys and OploversProvider._is_google_drive_stream(stream):
                continue
            filtered.append(stream)
        return filtered

    @staticmethod
    def _is_google_drive_stream(stream: ProviderStream) -> bool:
        host = urlparse(str(stream.url or '')).netloc.lower()
        return host in {'drive.google.com', 'drive.usercontent.google.com'} or host.endswith(
            'googleusercontent.com'
        )

    @staticmethod
    def _stream_sort_key(stream: ProviderStream) -> tuple[int, int, int, str]:
        quality_text = str(stream.quality or '').strip().lower()
        has_numeric_quality = bool(re.search(r'(\d{3,4})', quality_text)) or quality_text in {
            'sd',
            'hd',
            'fhd',
            'fullhd',
            '4k',
            '2160',
            '2160p',
        }
        return (
            1 if has_numeric_quality else 0,
            quality_rank(quality_text) if has_numeric_quality else 0,
            1 if ':direct' in str(stream.source) else 0,
            0 if 'blogger' in str(stream.source).lower() else 1,
            str(stream.source or ''),
        )

    async def resolve_subtitles(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderSubtitle]:
        return []

    async def _search_via_index(
        self,
        query: str,
        *,
        year: int | None,
        limit: int,
    ) -> list[ProviderSearchResult]:
        entries = await self._series_index_entries()
        scored = [
            (self._title_score(query, title), path, title)
            for path, title in entries
        ]
        scored = [entry for entry in scored if entry[0] >= 0.42]
        scored.sort(key=lambda current: (current[0], current[2].lower()), reverse=True)
        candidate_count = min(max(limit, 6), 10)
        top_entries = scored[:candidate_count]
        if not top_entries:
            return []

        responses = await asyncio.gather(
            *(self._result_from_index_entry(path, title) for _score, path, title in top_entries),
            return_exceptions=True,
        )

        results: list[ProviderSearchResult] = []
        seen: set[str] = set()
        for response in responses:
            if isinstance(response, Exception) or response is None:
                continue
            if year is not None and response.year != year:
                continue
            if response.page_url in seen:
                continue
            seen.add(response.page_url)
            results.append(response)

        results.sort(
            key=lambda item: (
                self._result_score(query, item),
                float(item.payload.get('rating') or 0.0),
                item.year or 0,
                item.title.lower(),
            ),
            reverse=True,
        )
        return results[:limit]

    async def _series_index_entries(self) -> list[tuple[str, str]]:
        cached = self.__class__._series_index_cache
        now = time.monotonic()
        if cached and cached[0] > now:
            return list(cached[1])

        html, base_url = await self._request_text(
            '/series', referer=self.base_urls()[0], timeout=self._site_timeout
        )
        soup = BeautifulSoup(html, 'html.parser')
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for anchor in soup.select('a[href]'):
            href = str(anchor.get('href') or '').strip()
            title = ' '.join(anchor.get_text(' ', strip=True).split())
            if not title:
                continue
            if not (href.startswith('/series/') or href.startswith('/movie/')):
                continue
            if '/episode/' in href:
                continue
            path = self.absolute_url(base_url, href)
            relative_path = '/' + '/'.join(path.split('/', maxsplit=3)[3:]) if '://' in path else href
            if relative_path in seen:
                continue
            seen.add(relative_path)
            entries.append((relative_path, title))

        self.__class__._series_index_cache = (now + self._series_index_ttl_seconds, entries)
        return list(entries)

    async def _result_from_index_entry(self, path: str, fallback_title: str) -> ProviderSearchResult | None:
        content_kind, slug = self._content_path_parts(path)
        if not slug:
            return None
        payload = await self._content_payload(content_kind, slug)
        if payload:
            return self._result_from_content_payload(content_kind, slug, payload)

        page_url = f'{self.base_urls()[0]}{path}'
        fallback_payload = build_anime_payload(
            provider_name=self.name,
            title=fallback_title,
            page_url=page_url,
            description='',
            year=None,
            rating=None,
            status='',
            anime_type='TV' if content_kind == 'series' else 'Movie',
            thumbnail_url='',
            alt_titles=[],
            genres=[],
            episodes=[],
            total_episodes=0,
            extra={'slug': slug, 'content_kind': content_kind},
        )
        return provider_result_from_payload(
            item_id=slug or fallback_title,
            title=fallback_title,
            page_url=page_url,
            payload=fallback_payload,
        )

    async def _content_payload(self, content_kind: str, slug: str) -> dict[str, Any]:
        payload, _base_url = await self._request_json(
            f'/{content_kind}/{slug}/__data.json', timeout=self._site_timeout
        )
        nodes = payload.get('nodes') if isinstance(payload, dict) else []
        if not isinstance(nodes, list) or len(nodes) < 3:
            return {}
        data = nodes[2].get('data') if isinstance(nodes[2], dict) else None
        if not isinstance(data, list):
            return {}
        decoded = decode_svelte_data(data, root_index=0)
        return decoded if isinstance(decoded, dict) else {}

    async def _series_payload(self, slug: str) -> dict[str, Any]:
        return await self._content_payload('series', slug)

    async def _search_via_api(
        self,
        query: str,
        *,
        year: int | None,
        limit: int,
    ) -> list[ProviderSearchResult]:
        # Fast path first: the API serves a series directly by slug
        # (GET /api/series/<slug>), verified live for e.g. "one-piece".
        # The old code skipped this and instead returned the first page of
        # the unfiltered series list, whatever the query was.
        slug = re.sub(r'[^a-z0-9]+', '-', query.strip().lower()).strip('-')
        if slug:
            try:
                detail = await self._api_json(f'/api/series/{slug}')
            except Exception:
                detail = {}
            entry = detail.get('data') if isinstance(detail, dict) else None
            if isinstance(entry, dict) and str(entry.get('slug') or '').strip():
                mapped = self._result_from_api_entry(entry)
                if year is None or mapped.year == year:
                    return [mapped]

        # Fallback: scan the paginated series list, but only keep entries
        # that actually match the query, and stop after 20 pages so a
        # non-matching title cannot turn into a 100-page crawl.  Note the
        # API ignores the `search` parameter, so filtering must happen here.
        results: list[ProviderSearchResult] = []
        page = 1
        while len(results) < limit and page <= 20:
            search_path = f'/api/series?page={page}'
            payload = await self._api_json(search_path)
            items = payload.get('data') if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                mapped = self._result_from_api_entry(entry)
                if year is not None and mapped.year != year:
                    continue
                if self._title_score(query, mapped.title) < 0.42:
                    continue
                results.append(mapped)
                if len(results) >= limit:
                    break
            meta = payload.get('meta') if isinstance(payload, dict) else None
            if not isinstance(meta, dict):
                break
            if int(meta.get('currentPage') or 0) >= int(meta.get('lastPage') or 0):
                break
            page += 1
        results.sort(key=lambda item: self._result_score(query, item), reverse=True)
        return results[:limit]

    async def _api_json(self, path_or_url: str) -> dict[str, Any]:
        errors: list[str] = []
        for base_url in self.api_base_urls():
            target = self.absolute_url(base_url, path_or_url)
            try:
                payload, _resolved = await self._request_json(target, referer=base_url)
            except Exception as exc:
                errors.append(f'{target}: {exc}')
                continue
            if isinstance(payload, dict):
                return payload
        raise RuntimeError('; '.join(errors) or f'Failed to fetch {path_or_url}')

    async def _api_episode(self, slug: str, requested_episode: int) -> dict[str, Any] | None:
        """Fetch one episode from the API, or None if it cannot be found.

        Episodes are listed newest-first, 10 per page, so the page holding
        episode N is derived from the series' total episode count.
        """

        try:
            detail = await self._api_json(f'/api/series/{slug}')
        except Exception:
            return None
        entry = detail.get('data') if isinstance(detail, dict) else None
        total = 0
        if isinstance(entry, dict):
            try:
                total = int(entry.get('totalEpisodes') or 0)
            except (TypeError, ValueError):
                total = 0
        page = (total - requested_episode) // 10 + 1 if total and requested_episode <= total else 1
        if page < 1:
            page = 1
        try:
            payload = await self._api_json(f'/api/series/{slug}/episodes?page={page}')
        except Exception:
            return None
        items = payload.get('data') if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return None
        for episode in items:
            if not isinstance(episode, dict):
                continue
            try:
                number = int(str(episode.get('episodeNumber') or '0'))
            except ValueError:
                continue
            if number == requested_episode:
                return episode
        return None

    def _result_from_content_payload(
        self,
        content_kind: str,
        slug: str,
        payload: dict[str, Any],
    ) -> ProviderSearchResult:
        series = payload.get('series') if isinstance(payload, dict) else None
        if not isinstance(series, dict):
            return self._result_from_api_entry({'slug': slug, 'title': slug.replace('-', ' ').title()})

        page_url = f'{self.base_urls()[0]}/{content_kind}/{slug}'
        episodes = self._episode_entries_from_payload(payload, slug=slug, content_kind=content_kind)
        genres = [
            str((genre or {}).get('name') or '').strip()
            for genre in series.get('genres') or []
            if isinstance(genre, dict) and str((genre or {}).get('name') or '').strip()
        ]
        payload_data = build_anime_payload(
            provider_name=self.name,
            title=str(series.get('title') or slug.replace('-', ' ').title()).strip(),
            page_url=page_url,
            description=str(series.get('description') or '').strip(),
            year=parse_year(series.get('releaseDate')),
            rating=float(series.get('score')) if series.get('score') is not None else None,
            status=str(series.get('status') or '').strip(),
            anime_type=str(series.get('releaseType') or '').strip(),
            thumbnail_url=str(series.get('poster') or '').strip(),
            alt_titles=[str(series.get('japaneseTitle') or '').strip()],
            genres=genres,
            episodes=episodes,
            total_episodes=(
                int(series.get('totalEpisodes') or 0)
                if str(series.get('totalEpisodes') or '').isdigit()
                else len(episodes)
            ),
            extra={
                'slug': slug,
                'content_kind': content_kind,
                'studio': (
                    str((series.get('studio') or {}).get('name') or '').strip()
                    if isinstance(series.get('studio'), dict)
                    else ''
                ),
                'season_name': (
                    str((series.get('season') or {}).get('name') or '').strip()
                    if isinstance(series.get('season'), dict)
                    else ''
                ),
                'duration': str(series.get('duration') or '').strip(),
            },
        )
        return provider_result_from_payload(
            item_id=str(series.get('id') or slug),
            title=str(series.get('title') or slug.replace('-', ' ').title()).strip(),
            page_url=page_url,
            payload=payload_data,
        )

    def _episode_entries_from_payload(
        self,
        payload: dict[str, Any],
        *,
        slug: str,
        content_kind: str,
    ) -> list[dict[str, Any]]:
        episode_items = payload.get('episodes') if isinstance(payload, dict) else []
        if not isinstance(episode_items, list):
            return []

        entries: list[dict[str, Any]] = []
        for episode in episode_items:
            if not isinstance(episode, dict):
                continue
            raw_episode_number = episode.get('episodeNumber')
            try:
                episode_number = int(raw_episode_number)
            except (TypeError, ValueError):
                episode_number = (
                    extract_episode_number(str(raw_episode_number or ''))
                    or extract_episode_number(str(episode.get('title') or ''))
                    or 1
                )
            if content_kind == 'movie':
                episode_url = f'{self.base_urls()[0]}/movie/{slug}'
                episode_title = str(
                    episode.get('title') or payload.get('series', {}).get('title') or slug
                ).strip()
            else:
                episode_url = f'{self.base_urls()[0]}/series/{slug}/episode/{episode_number}'
                episode_title = str(episode.get('title') or f'Episode {episode_number}').strip()
            entries.append({'number': episode_number, 'title': episode_title, 'url': episode_url})
        return entries

    def _result_from_api_entry(self, entry: dict[str, Any]) -> ProviderSearchResult:
        slug = str(entry.get('slug') or '').strip()
        title = str(entry.get('title') or '').strip() or slug.replace('-', ' ').title()
        page_url = f"{self.base_urls()[0]}/series/{slug}"
        genres = []
        for genre in entry.get('genres') or []:
            if isinstance(genre, dict):
                value = str(genre.get('name') or '').strip()
                if value:
                    genres.append(value)
        payload = build_anime_payload(
            provider_name=self.name,
            title=title,
            page_url=page_url,
            description=str(entry.get('description') or '').strip(),
            year=parse_year(entry.get('releaseDate')),
            rating=float(entry.get('score')) if entry.get('score') is not None else None,
            status=str(entry.get('status') or '').strip(),
            anime_type=str(entry.get('releaseType') or '').strip(),
            thumbnail_url=str(entry.get('poster') or '').strip(),
            alt_titles=[str(entry.get('japaneseTitle') or '').strip()],
            genres=genres,
            episodes=[],
            total_episodes=(
                int(entry.get('totalEpisodes') or 0)
                if str(entry.get('totalEpisodes') or '').isdigit()
                else 0
            ),
            extra={
                'slug': slug,
                'content_kind': 'series',
                'studio': (
                    str((entry.get('studio') or {}).get('name') or '').strip()
                    if isinstance(entry.get('studio'), dict)
                    else ''
                ),
                'season_name': (
                    str((entry.get('season') or {}).get('name') or '').strip()
                    if isinstance(entry.get('season'), dict)
                    else ''
                ),
                'duration': str(entry.get('duration') or '').strip(),
            },
        )
        return provider_result_from_payload(
            item_id=slug or title,
            title=title,
            page_url=page_url,
            payload=payload,
        )

    def _results_match_query(self, query: str, results: list[ProviderSearchResult]) -> bool:
        if not results:
            return False
        top_score = max(self._result_score(query, item) for item in results[:5])
        return top_score >= 0.72

    def _result_score(self, query: str, item: ProviderSearchResult) -> float:
        candidates = [item.title]
        alt_titles = item.payload.get('alt_titles') if isinstance(item.payload, dict) else None
        if isinstance(alt_titles, list):
            candidates.extend(str(value).strip() for value in alt_titles if str(value).strip())
        return max(self._title_score(query, candidate) for candidate in candidates if candidate)

    @staticmethod
    def _title_score(query: str, candidate: str) -> float:
        cleaned_query = str(query).strip().lower()
        cleaned_candidate = str(candidate).strip().lower()
        if not cleaned_query or not cleaned_candidate:
            return 0.0

        query_key = re.sub(r'[^a-z0-9]+', '', cleaned_query)
        candidate_key = re.sub(r'[^a-z0-9]+', '', cleaned_candidate)
        score = max(
            SequenceMatcher(None, cleaned_query, cleaned_candidate).ratio(),
            SequenceMatcher(None, query_key, candidate_key).ratio() if query_key and candidate_key else 0.0,
        )
        if query_key and candidate_key:
            if query_key == candidate_key:
                score += 0.4
            elif candidate_key.startswith(query_key):
                score += 0.25
            elif query_key in candidate_key:
                score += 0.18
        query_tokens = {token for token in re.split(r'[^a-z0-9]+', cleaned_query) if token}
        candidate_tokens = {token for token in re.split(r'[^a-z0-9]+', cleaned_candidate) if token}
        if query_tokens and candidate_tokens:
            score += 0.25 * (len(query_tokens & candidate_tokens) / len(query_tokens))
        return score

    @staticmethod
    def _content_path_parts(path: str) -> tuple[str, str]:
        stripped = str(path or '').strip().strip('/')
        if not stripped:
            return 'series', ''
        parts = stripped.split('/')
        if len(parts) < 2:
            return 'series', ''
        content_kind = parts[0].lower()
        slug = parts[1]
        return content_kind, slug
