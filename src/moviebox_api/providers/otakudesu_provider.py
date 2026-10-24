"""Otakudesu anime provider.

Chosen as the third anime provider because the current public domain
(otakudesu.blog) is reachable without a JavaScript gate on search/detail
pages and exposes predictable series metadata and episode links, unlike
the Kuronime/Neonime options observed during implementation.  The old
otakudesu.pl domain was dropped from the defaults in 2026-10-24 after it
started answering with an empty reply; it can still be re-added with
MOVIEBOX_OTAKUDESU_URLS if it comes back.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from moviebox_api.constants import SubjectType
from moviebox_api.providers.anime_common import (
    BaseAnimeProvider,
    build_anime_payload,
    dedupe_streams,
    extract_episode_number,
    extract_subtitle_links,
    first_http_url,
    parse_year,
    provider_result_from_payload,
    quality_rank,
    title_match_score,
)
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle


class OtakudesuProvider(BaseAnimeProvider):
    """Scrape Otakudesu search and episode embed pages."""

    name = 'otakudesu'
    env_keys = ('MOVIEBOX_OTAKUDESU_URLS', 'MOVIEBOX_OTAKUDESU_URL')
    default_base_urls = ('https://otakudesu.blog',)

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

        search_path = f'/?s={query.strip().replace(" ", "+")}'
        html, base_url = await self._request_text(search_path, referer=self.base_urls()[0])
        soup = BeautifulSoup(html, 'html.parser')
        series_urls: list[str] = []
        anchors = soup.select('article a[href], .chivsrc li h2 a[href], .venutama li h2 a[href]')
        for anchor in anchors:
            href = str(anchor.get('href') or '').strip()
            if not href or ('/series/' not in href and '/anime/' not in href):
                continue
            absolute_url = self.absolute_url(base_url, href)
            if absolute_url not in series_urls:
                series_urls.append(absolute_url)
            if len(series_urls) >= limit:
                break

        results = await self._load_series_results(series_urls)
        if year is not None:
            results = [item for item in results if item.year == year]
        results.sort(key=lambda item: self._result_score(query, item), reverse=True)
        return results[:limit]

    async def resolve_streams(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderStream]:
        episode_entry = self._episode_entry(item, episode=episode)
        if episode_entry is None:
            return []

        episode_url = str(episode_entry.get('url') or '').strip()
        html, _base_url = await self._request_text(episode_url, referer=item.page_url)
        soup = BeautifulSoup(html, 'html.parser')
        subtitles = extract_subtitle_links(html)
        streams: list[ProviderStream] = []

        for download_url, quality_label, source_label in self._download_links(soup):
            streams.extend(
                await self.expand_streams(
                    url=download_url,
                    source=source_label,
                    quality=quality_label,
                    referer=episode_url,
                    subtitles=subtitles,
                )
            )

        for iframe in soup.select('iframe[src]'):
            src = str(iframe.get('src') or '').strip()
            if not src or 'facebook.com' in src:
                continue
            streams.extend(
                await self.expand_streams(
                    url=src,
                    source='otakudesu:embed',
                    referer=episode_url,
                    subtitles=subtitles,
                )
            )

        if not streams:
            embed_url = first_http_url(html)
            if embed_url and 'facebook.com' not in embed_url:
                streams.extend(
                    await self.expand_streams(
                        url=embed_url,
                        source='otakudesu:embed',
                        referer=episode_url,
                        subtitles=subtitles,
                    )
                )

        return sorted(
            dedupe_streams(streams),
            key=lambda stream: quality_rank(stream.quality),
            reverse=True,
        )

    async def resolve_subtitles(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderSubtitle]:
        episode_entry = self._episode_entry(item, episode=episode)
        if episode_entry is None:
            return []
        episode_url = str(episode_entry.get('url') or '').strip()
        html, _base_url = await self._request_text(episode_url, referer=item.page_url)
        return extract_subtitle_links(html)

    async def _load_series_results(self, urls: list[str]) -> list[ProviderSearchResult]:
        results: list[ProviderSearchResult] = []
        for url in urls:
            try:
                results.append(await self._load_series_result(url))
            except Exception:
                continue
        return results

    async def _load_series_result(self, url: str) -> ProviderSearchResult:
        html, _base_url = await self._request_text(url, referer=self.base_urls()[0])
        soup = BeautifulSoup(html, 'html.parser')

        specs = self._parse_specs(soup)
        title = str(specs.get('judul') or self._title_from_html(soup, url)).strip()
        genres = [
            anchor.get_text(' ', strip=True)
            for anchor in soup.select('.genxed a, .infozingle a[rel=tag], .set a[rel=tag]')
            if anchor.get_text(' ', strip=True)
        ]
        description = (
            self._first_text(soup, '.entry-content')
            or self._first_text(soup, '.sinopc')
            or self._meta_content(soup, 'description')
        )
        poster = ''
        image = soup.select_one('.thumb img[src], .wp-post-image[src]')
        if image and image.get('src'):
            poster = str(image.get('src')).strip()

        episodes = []
        seen_urls: set[str] = set()
        for anchor in soup.select('a[href]'):
            href = str(anchor.get('href') or '').strip()
            if 'episode-' not in href:
                continue
            absolute_url = href if href.startswith('http') else self.absolute_url(self.base_urls()[0], href)
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            episode_text = anchor.get_text(' ', strip=True) or absolute_url.rsplit('/', maxsplit=2)[-2]
            episodes.append(
                {
                    'number': (
                        extract_episode_number(absolute_url)
                        or extract_episode_number(episode_text)
                        or 1
                    ),
                    'title': episode_text,
                    'url': absolute_url,
                }
            )

        payload = build_anime_payload(
            provider_name=self.name,
            title=title,
            page_url=url,
            description=description,
            year=parse_year(specs.get('released') or specs.get('tanggal rilis')),
            rating=self._parse_rating(specs.get('skor') or specs.get('rating')),
            status=specs.get('status', ''),
            anime_type=specs.get('type') or specs.get('tipe', ''),
            thumbnail_url=poster,
            alt_titles=[],
            genres=genres,
            episodes=episodes,
            total_episodes=len(episodes),
            extra={
                'studio': specs.get('studio', ''),
                'season_name': specs.get('season', ''),
                'duration': specs.get('duration', ''),
            },
        )
        return provider_result_from_payload(item_id=url, title=title, page_url=url, payload=payload)

    @staticmethod
    def _title_from_html(soup: BeautifulSoup, url: str) -> str:
        title_tag = soup.select_one('title')
        if title_tag:
            raw = title_tag.get_text(' ', strip=True)
            cleaned = re.split(
                r"\s+(?:\||-|\u2013|\u2014)\s+",
                raw.split('?', maxsplit=1)[0],
                maxsplit=1,
            )[0].strip()
            cleaned = re.sub(r'\s+subtitle indonesia$', '', cleaned, flags=re.I).strip()
            if cleaned and 'Page not found' not in cleaned:
                return cleaned
        header = soup.select_one('h1')
        if header:
            text = header.get_text(' ', strip=True)
            if text and 'Page not found' not in text:
                return text
        return url.rstrip('/').split('/')[-1].replace('-', ' ').title()

    @staticmethod
    def _parse_specs(soup: BeautifulSoup) -> dict[str, str]:
        specs: dict[str, str] = {}
        for span in soup.select('.spe span, .infozingle p span'):
            text = span.get_text(' ', strip=True)
            if ':' not in text:
                continue
            key, value = text.split(':', maxsplit=1)
            specs[key.strip().lower()] = value.strip()
        return specs

    @staticmethod
    def _first_text(soup: BeautifulSoup, selector: str, fallback: str = '') -> str:
        element = soup.select_one(selector)
        return element.get_text(' ', strip=True) if element else fallback

    @staticmethod
    def _meta_content(soup: BeautifulSoup, name: str) -> str:
        element = soup.select_one(f'meta[name="{name}"][content]')
        if element is None:
            element = soup.select_one(f'meta[property="og:{name}"][content]')
        return str(element.get('content') or '').strip() if element else ''

    @staticmethod
    def _parse_rating(value: str | None) -> float | None:
        matched = re.search(r'(\d+(?:\.\d+)?)', str(value or ''))
        if not matched:
            return None
        return float(matched.group(1))

    @staticmethod
    def _download_links(soup: BeautifulSoup) -> list[tuple[str, str | None, str]]:
        links: list[tuple[str, str | None, str]] = []
        for item in soup.select('.download li'):
            quality_label = OtakudesuProvider._quality_label(item.get_text(' ', strip=True))
            for anchor in item.select('a[href]'):
                href = str(anchor.get('href') or '').strip()
                if not href:
                    continue
                host_label = re.sub(r'[^a-z0-9]+', '-', anchor.get_text(' ', strip=True).lower()).strip('-')
                source_label = f"otakudesu:{host_label or 'download'}"
                links.append((href, quality_label, source_label))
        return links

    @staticmethod
    def _quality_label(value: str) -> str | None:
        matched = re.search(r'(\d{3,4}p)', str(value or ''), flags=re.I)
        if not matched:
            return None
        return matched.group(1).lower()

    @staticmethod
    def _episode_entry(item: ProviderSearchResult, *, episode: int) -> dict[str, str] | None:
        episodes = item.payload.get('episodes') if isinstance(item.payload, dict) else None
        if not isinstance(episodes, list) or not episodes:
            return None

        target_episode = episode if episode > 0 else 1
        for entry in episodes:
            try:
                if int(entry.get('number') or 0) == target_episode:
                    return entry
            except (TypeError, ValueError):
                continue
        return episodes[0]

    @staticmethod
    def _result_score(query: str, item: ProviderSearchResult) -> float:
        return title_match_score(query, [item.title])
