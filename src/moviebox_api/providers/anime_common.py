"""Shared helpers for anime stream providers."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from moviebox_api.constants import SubjectType
from moviebox_api.providers.base import BaseStreamProvider
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle

_PROVIDER_TIMEOUT = httpx.Timeout(25.0, connect=15.0)
_PROVIDER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5,id;q=0.4",
}
_URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+', flags=re.I)
_YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_EPISODE_PATTERN = re.compile(r"(?:episode|ep)\D{0,6}(\d{1,4})", flags=re.I)
_SUBTITLE_URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+\.(?:srt|ass|vtt)[^\s"\'<>]*', flags=re.I)
_MEDIA_URL_PATTERN = re.compile(
    r'(?:https?://|/)[^"\'\s<>]+\.(?:m3u8|mpd|mp4|mkv|webm|avi|mov|m4v|ts)[^"\'\s<>]*',
    flags=re.I,
)
_PACKER_PATTERN = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)(?:,\d+,\{\})?\)\)$",
    flags=re.S,
)
_BLOGGER_FSID_PATTERN = re.compile(r'"FdrFJe":"([^"]+)"')
_BLOGGER_BL_PATTERN = re.compile(r'"cfb2h":"([^"]+)"')
_BLOGGER_RESPONSE_LINE_PATTERN = re.compile(r'^\[\["wrb\.fr".*$', flags=re.M)
_HLS_ATTRIBUTE_PATTERN = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]+)')
_BLOGGER_ITAG_QUALITY_MAP = {
    18: '360p',
    22: '720p',
    37: '1080p',
    43: '360p',
    44: '480p',
    45: '720p',
    46: '1080p',
    59: '480p',
}


@dataclass(slots=True)
class ResolvedMediaCandidate:
    """A direct media candidate extracted from a wrapper host."""

    url: str
    quality: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    stream_type: str | None = None


def configured_base_urls(env_keys: tuple[str, ...], defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve candidate base URLs from environment variables or defaults."""

    values: list[str] = []
    for env_key in env_keys:
        raw_value = os.getenv(env_key, "").strip()
        if not raw_value:
            continue
        for value in raw_value.split(","):
            candidate = value.strip().rstrip("/")
            if candidate and candidate not in values:
                values.append(candidate)

    if values:
        return tuple(values)

    deduped_defaults: list[str] = []
    for value in defaults:
        candidate = str(value).strip().rstrip("/")
        if candidate and candidate not in deduped_defaults:
            deduped_defaults.append(candidate)
    return tuple(deduped_defaults)


def first_http_url(value: str) -> str | None:
    """Extract the first HTTP(S) URL from arbitrary text."""

    matched = _URL_PATTERN.search(value or "")
    return matched.group(0) if matched else None


def extract_episode_number(value: str) -> int | None:
    """Extract an episode number from text or a URL."""

    matched = _EPISODE_PATTERN.search(value or "")
    if not matched:
        return None
    try:
        return int(matched.group(1))
    except ValueError:
        return None


def parse_year(value: Any) -> int | None:
    """Parse a release year from arbitrary provider metadata."""

    if isinstance(value, int) and 1900 <= value <= 2200:
        return value

    matched = _YEAR_PATTERN.search(str(value or ""))
    if not matched:
        return None
    return int(matched.group(1))


def title_match_score(query: str, candidates: list[str]) -> float:
    """Compute a relevance score between a query and one or more title candidates."""

    cleaned_query = str(query or '').strip().lower()
    if not cleaned_query:
        return 0.0

    query_key = re.sub(r'[^a-z0-9]+', '', cleaned_query)
    query_tokens = {token for token in re.split(r'[^a-z0-9]+', cleaned_query) if token}
    best_score = 0.0
    for candidate in candidates:
        cleaned_candidate = str(candidate or '').strip().lower()
        if not cleaned_candidate:
            continue
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
        candidate_tokens = {token for token in re.split(r'[^a-z0-9]+', cleaned_candidate) if token}
        if query_tokens and candidate_tokens:
            score += 0.25 * (len(query_tokens & candidate_tokens) / len(query_tokens))
        if score > best_score:
            best_score = score
    return best_score



def quality_rank(value: str | None) -> int:
    """Convert quality labels to sortable numeric values."""

    if not value:
        return 720

    text = str(value).strip().lower()
    matched = re.search(r"(\d{3,4})", text)
    if matched:
        return int(matched.group(1))
    if text in {"sd", "360", "360p"}:
        return 360
    if text in {"480", "480p"}:
        return 480
    if text in {"hd", "720", "720p"}:
        return 720
    if text in {"fhd", "fullhd", "1080", "1080p"}:
        return 1080
    if text in {"uhd", "4k", "2160", "2160p"}:
        return 2160
    return 720


def anime_content_subject_type(anime_type: str | None, episode_count: int | None) -> SubjectType:
    """Best-effort mapping from provider anime type to movie/series semantics."""

    lowered = str(anime_type or "").strip().lower()
    if lowered in {"movie", "film"}:
        return SubjectType.MOVIES
    if episode_count and episode_count > 1:
        return SubjectType.TV_SERIES
    if lowered in {"tv", "ona", "ova", "special", "music"}:
        return SubjectType.TV_SERIES
    return SubjectType.TV_SERIES if episode_count else SubjectType.MOVIES


def normalize_episode_entries(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate and sort provider episode metadata."""

    normalized: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        url = str(episode.get("url") or "").strip()
        if not url:
            continue

        number = episode.get("number")
        if not isinstance(number, int):
            number = extract_episode_number(url) or extract_episode_number(str(episode.get("title") or ""))
        key = str(number or url)
        normalized[key] = {
            "number": number or 1,
            "title": str(episode.get("title") or f"Episode {number or 1}").strip(),
            "url": url,
        }

    return sorted(normalized.values(), key=lambda current: int(current.get("number") or 0))


def season_map_from_episodes(episodes: list[dict[str, Any]]) -> dict[int, int]:
    """Build a simple season map for anime flows."""

    normalized = normalize_episode_entries(episodes)
    if not normalized:
        return {}
    return {1: len(normalized)}


def build_anime_payload(
    *,
    provider_name: str,
    title: str,
    page_url: str,
    description: str = "",
    year: int | None = None,
    rating: float | None = None,
    status: str = "",
    anime_type: str = "",
    thumbnail_url: str = "",
    alt_titles: list[str] | None = None,
    genres: list[str] | None = None,
    episodes: list[dict[str, Any]] | None = None,
    total_episodes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a consistent metadata payload for anime items."""

    normalized_episodes = normalize_episode_entries(list(episodes or []))
    episode_count = (
        total_episodes
        if isinstance(total_episodes, int) and total_episodes >= 0
        else len(normalized_episodes)
    )
    payload = {
        "provider_name": provider_name,
        "title": title,
        "page_url": page_url,
        "description": description.strip(),
        "year": year,
        "rating": float(rating) if rating is not None else None,
        "status": status.strip(),
        "anime_type": anime_type.strip(),
        "thumbnail_url": thumbnail_url.strip(),
        "alt_titles": [value for value in (alt_titles or []) if value],
        "genres": [value for value in (genres or []) if value],
        "episodes": normalized_episodes,
        "episode_count": episode_count,
        "season_map": season_map_from_episodes(normalized_episodes),
        "content_subject_type": anime_content_subject_type(anime_type, episode_count),
    }
    if extra:
        payload.update(extra)
    return payload


def provider_result_from_payload(
    *,
    item_id: str,
    title: str,
    page_url: str,
    payload: dict[str, Any],
) -> ProviderSearchResult:
    """Create a ProviderSearchResult using the shared anime payload schema."""

    return ProviderSearchResult(
        id=item_id,
        title=title,
        page_url=page_url,
        subject_type=SubjectType.ANIME,
        year=parse_year(payload.get("year")),
        payload=payload,
    )


def extract_subtitle_links(html: str) -> list[ProviderSubtitle]:
    """Extract direct subtitle URLs from provider HTML when present."""

    subtitles: list[ProviderSubtitle] = []
    seen: set[str] = set()
    for matched in _SUBTITLE_URL_PATTERN.findall(html or ""):
        url = matched.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        subtitles.append(
            ProviderSubtitle(
                url=url,
                language="id",
                label=urlparse(url).path.rsplit("/", maxsplit=1)[-1],
            )
        )
    return subtitles


def decoded_iframe_url(value: str) -> str | None:
    """Decode base64 iframe snippets used by some anime sites."""

    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    if raw_value.startswith(("http://", "https://")):
        return raw_value

    padded = raw_value + ("=" * (-len(raw_value) % 4))
    try:
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        return None

    soup = BeautifulSoup(decoded, "html.parser")
    iframe = soup.select_one("iframe[src]")
    if iframe and iframe.get("src"):
        return str(iframe.get("src")).strip()
    return first_http_url(decoded)


def extract_nested_embed_urls(html: str, *, base_url: str) -> list[str]:
    """Extract iframe-like nested wrapper URLs from embed pages."""

    soup = BeautifulSoup(html or '', 'html.parser')
    urls: list[str] = []
    seen: set[str] = set()
    for selector, attribute in (
        ('iframe[src]', 'src'),
        ('iframe[data-src]', 'data-src'),
        ('video[src]', 'src'),
        ('source[src]', 'src'),
    ):
        for element in soup.select(selector):
            raw_value = str(element.get(attribute) or '').strip()
            candidate = first_http_url(raw_value) or decoded_iframe_url(raw_value) or raw_value
            if not candidate:
                continue
            absolute_url = urljoin(base_url, candidate)
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            urls.append(absolute_url)
    return urls


async def extract_nested_embed_media_candidates(
    provider: BaseAnimeProvider,
    url: str,
    html: str,
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve wrappers like DesuStream by following their nested iframe targets."""

    parsed = urlparse(url)
    base_url = f'{parsed.scheme}://{parsed.netloc}'
    candidates: list[ResolvedMediaCandidate] = []
    seen: set[str] = set()
    for nested_url in extract_nested_embed_urls(html, base_url=base_url):
        if not nested_url or nested_url == url or nested_url in seen:
            continue
        seen.add(nested_url)
        nested_host = urlparse(nested_url).netloc.lower()
        nested_path = urlparse(nested_url).path.lower()
        if (
            nested_host.endswith(('googlevideo.com', 'googleusercontent.com'))
            or nested_path.endswith(
                ('.m3u8', '.mpd', '.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v', '.ts')
            )
        ):
            candidates.append(ResolvedMediaCandidate(url=nested_url))
            continue
        candidates.extend(
            await resolve_wrapped_stream_candidates(provider, nested_url, referer=referer or url)
        )
    return dedupe_media_candidates(candidates)


def _stream_dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    if 'googlevideo.com' in parsed.netloc.lower():
        query_values = parse_qs(parsed.query)
        video_id = (query_values.get('id') or [''])[0]
        itag = (query_values.get('itag') or [''])[0]
        if video_id and itag:
            return f'googlevideo:{video_id}:{itag}'
    return url


def dedupe_streams(streams: list[ProviderStream]) -> list[ProviderStream]:
    """Deduplicate provider streams by semantic media identity while preserving order."""

    deduped: list[ProviderStream] = []
    seen: set[str] = set()
    for stream in streams:
        url = str(stream.url).strip()
        key = _stream_dedupe_key(url)
        if not url or key in seen:
            continue
        seen.add(key)
        deduped.append(stream)
    return deduped


def decode_svelte_data(entries: list[Any], root_index: int = 0) -> Any:
    """Decode SvelteKit devalue array payloads into regular Python objects."""

    cache: dict[int, Any] = {}

    def resolve_reference(index: int) -> Any:
        if index in cache:
            return cache[index]
        if index < 0 or index >= len(entries):
            return index
        resolved = resolve(entries[index], from_slot=True)
        cache[index] = resolved
        return resolved

    def resolve(value: Any, *, from_slot: bool = False) -> Any:
        if isinstance(value, int):
            if from_slot:
                return value
            return resolve_reference(value)
        if isinstance(value, list):
            return [resolve(item, from_slot=False) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item, from_slot=False) for key, item in value.items()}
        return value

    return resolve_reference(root_index)


class BaseAnimeProvider(BaseStreamProvider):
    """Base provider with retry and domain-rotation helpers for anime sites."""

    env_keys: tuple[str, ...] = ()
    default_base_urls: tuple[str, ...] = ()

    def base_urls(self) -> tuple[str, ...]:
        """Return all configured base URLs for the provider."""

        return configured_base_urls(self.env_keys, self.default_base_urls)

    def absolute_url(self, base_url: str, value: str) -> str:
        """Convert provider-relative URLs into absolute URLs."""

        cleaned = str(value or "").strip()
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        return urljoin(base_url.rstrip("/") + "/", cleaned.lstrip("/"))

    @staticmethod
    def _request_sync(
        target_url: str,
        *,
        headers: dict[str, str],
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Run a blocking GET request for providers that misbehave on async clients."""

        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=timeout or _PROVIDER_TIMEOUT,
        ) as client:
            response = client.get(target_url)
        response.raise_for_status()
        return response

    async def _request(
        self,
        path_or_url: str,
        *,
        referer: str | None = None,
        accept: str | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> tuple[httpx.Response, str]:
        """Request HTML or JSON content while rotating candidate domains."""

        errors: list[str] = []
        for base_url in self.base_urls():
            target_url = self.absolute_url(base_url, path_or_url)
            headers = dict(_PROVIDER_HEADERS)
            headers["Referer"] = referer or base_url
            if accept:
                headers["Accept"] = accept
            # One async attempt plus one sync fallback per domain.  The old
            # code retried that whole pair a second time, so a single dead
            # domain (e.g. oplovers' old front-end host) could hang a search
            # for ~100s before the next fallback was even tried.
            try:
                async with httpx.AsyncClient(
                    headers=headers,
                    follow_redirects=True,
                    timeout=timeout or _PROVIDER_TIMEOUT,
                ) as client:
                    response = await client.get(target_url)
                response.raise_for_status()
                return response, base_url
            except Exception as exc:
                errors.append(f"{target_url}: async {exc}")
                try:
                    response = await asyncio.to_thread(
                        self._request_sync,
                        target_url,
                        headers=headers,
                        timeout=timeout or _PROVIDER_TIMEOUT,
                    )
                    return response, base_url
                except Exception as sync_exc:
                    errors.append(f"{target_url}: sync {sync_exc}")
                    continue

        raise RuntimeError("; ".join(errors) or f"Failed to fetch {path_or_url}")

    async def _request_text(
        self,
        path_or_url: str,
        *,
        referer: str | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> tuple[str, str]:
        response, base_url = await self._request(path_or_url, referer=referer, timeout=timeout)
        return response.text, base_url

    async def _request_json(
        self,
        path_or_url: str,
        *,
        referer: str | None = None,
        accept: str = "application/json,text/plain;q=0.9,*/*;q=0.8",
        timeout: httpx.Timeout | None = None,
    ) -> tuple[Any, str]:
        response, base_url = await self._request(
            path_or_url, referer=referer, accept=accept, timeout=timeout
        )
        return response.json(), base_url

    async def _post_text(
        self,
        url: str,
        *,
        data: str,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Send a form POST request and return the response body."""

        request_headers = dict(_PROVIDER_HEADERS)
        request_headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
        request_headers['X-Same-Domain'] = '1'
        request_headers['Referer'] = referer or url
        for key, value in dict(headers or {}).items():
            if key and value:
                request_headers[str(key)] = str(value)

        async with httpx.AsyncClient(
            headers=request_headers,
            follow_redirects=True,
            timeout=_PROVIDER_TIMEOUT,
        ) as client:
            response = await client.post(url, content=data)
        response.raise_for_status()
        return response.text

    @staticmethod
    def _direct_stream_source(source: str, stream_type: str | None) -> str:
        """Annotate direct stream labels with HLS/DASH markers when known."""

        normalized_type = str(stream_type or '').strip().lower()
        if normalized_type in {'hls', 'dash'} and normalized_type not in source.lower():
            return f'{source}:{normalized_type}:direct'
        return f'{source}:direct'

    def make_stream(
        self,
        *,
        url: str,
        source: str,
        quality: str | None = None,
        headers: dict[str, str] | None = None,
        subtitles: list[ProviderSubtitle] | None = None,
    ) -> ProviderStream:
        """Build a provider stream with shared header defaults."""

        merged_headers = {key: value for key, value in dict(headers or {}).items() if key and value}
        if "User-Agent" not in merged_headers:
            merged_headers["User-Agent"] = _PROVIDER_HEADERS["User-Agent"]
        return ProviderStream(
            url=url,
            source=source,
            quality=quality,
            headers=merged_headers,
            subtitles=list(subtitles or []),
        )

    async def expand_streams(
        self,
        *,
        url: str,
        source: str,
        quality: str | None = None,
        referer: str | None = None,
        subtitles: list[ProviderSubtitle] | None = None,
    ) -> list[ProviderStream]:
        """Expand known wrapper hosts into direct media URLs when possible."""

        direct_candidates = await resolve_wrapped_stream_candidates(self, url, referer=referer)
        if direct_candidates:
            expanded_candidates: list[ResolvedMediaCandidate] = []
            for candidate in direct_candidates:
                variant_candidates = await expand_hls_master_playlist_candidates(
                    candidate.url,
                    referer=referer or url,
                    headers=candidate.headers,
                )
                if variant_candidates:
                    expanded_candidates.extend(variant_candidates)
                else:
                    expanded_candidates.append(candidate)

            return dedupe_streams(
                [
                    self.make_stream(
                        url=candidate.url,
                        source=self._direct_stream_source(source, candidate.stream_type),
                        quality=candidate.quality or quality,
                        headers=(candidate.headers or {"Referer": referer or url}),
                        subtitles=subtitles,
                    )
                    for candidate in dedupe_media_candidates(expanded_candidates)
                ]
            )

        return [
            self.make_stream(
                url=url,
                source=source,
                quality=quality,
                headers={"Referer": referer or url},
                subtitles=subtitles,
            )
        ]


def _unpack_packer_script(script: str) -> str:
    matched = _PACKER_PATTERN.search(script)
    if not matched:
        return ""

    payload, alphabet_size, symbol_count, replacements = matched.groups()
    base = int(alphabet_size)
    count = int(symbol_count)
    values = replacements.split('|')
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'

    def encode(number: int) -> str:
        if number == 0:
            return '0'
        encoded = ''
        current = number
        while current:
            current, remainder = divmod(current, base)
            encoded = alphabet[remainder] + encoded
        return encoded

    unpacked = payload
    for index in range(count - 1, -1, -1):
        if index >= len(values) or not values[index]:
            continue
        unpacked = re.sub(r'\b' + re.escape(encode(index)) + r'\b', values[index], unpacked)
    return unpacked


def _extract_packer_scripts(html: str) -> list[str]:
    scripts: list[str] = []
    start_index = 0
    needle = 'eval(function(p,a,c,k,e,d){'
    while True:
        start_index = html.find(needle, start_index)
        if start_index == -1:
            break
        end_index = html.find('</script>', start_index)
        if end_index == -1:
            scripts.append(html[start_index:].strip())
            break
        scripts.append(html[start_index:end_index].strip())
        start_index = end_index + len('</script>')
    return scripts


def extract_acefile_redirect_urls(html: str, *, base_url: str) -> list[str]:
    """Extract no-login AceFile redirect endpoints from packed page scripts."""

    urls: list[str] = []
    seen: set[str] = set()
    for script in _extract_packer_scripts(html or ''):
        unpacked = _unpack_packer_script(script)
        if not unpacked:
            continue
        unpacked = unpacked.replace('\\/', '/').replace(r'\/', '/')
        for matched in re.findall(r"[\"'](/service/redirect/[^\"']+)", unpacked):
            candidate = urljoin(base_url, matched.strip())
            if candidate and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
    return urls


async def _resolve_first_redirect_location(
    url: str,
    *,
    referer: str | None = None,
) -> str | None:
    headers = dict(_PROVIDER_HEADERS)
    headers['Referer'] = referer or url
    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=6.0),
        ) as client:
            response = await client.get(url)
    except Exception:
        return None

    location = str(response.headers.get('location') or '').strip()
    if location:
        return urljoin(str(response.url), location)
    if response.status_code in {200, 206}:
        content_type = str(response.headers.get('content-type') or '').lower()
        if 'text/html' not in content_type:
            return str(response.url)
    return None


async def extract_acefile_media_candidates(
    url: str,
    html: str,
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve AceFile share pages into direct Google Drive download URLs."""

    parsed = urlparse(url)
    base_url = f'{parsed.scheme}://{parsed.netloc}'
    candidates: list[ResolvedMediaCandidate] = []
    seen: set[str] = set()
    for redirect_url in extract_acefile_redirect_urls(html, base_url=base_url):
        direct_url = await _resolve_first_redirect_location(redirect_url, referer=url)
        if not direct_url or direct_url in seen:
            continue
        seen.add(direct_url)
        candidates.append(ResolvedMediaCandidate(url=direct_url))
    return candidates


def extract_filedon_media_urls(html: str) -> list[str]:
    """Extract signed direct media URLs from Filedon wrapper pages."""

    decoded_html = unescape(html or '').replace('\\/', '/')
    urls: list[str] = []
    seen: set[str] = set()
    for matched in re.findall(r'"url"\s*:\s*"(https?://[^"]+)"', decoded_html):
        candidate = matched.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if (
            '.mp4' not in lowered
            and '.m3u8' not in lowered
            and '.mpd' not in lowered
            and 'response-content-disposition' not in lowered
        ):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls



def _filedon_data_page_payload(html: str) -> dict[str, Any]:
    """Extract the Inertia page payload from a Filedon public share page."""

    soup = BeautifulSoup(html or '', 'html.parser')
    app_root = soup.select_one('#app[data-page]')
    if app_root is None:
        return {}

    raw_payload = str(app_root.get('data-page') or '').strip()
    if not raw_payload:
        return {}

    try:
        payload = json.loads(unescape(raw_payload))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _filedon_share_slug(url: str, html: str) -> str | None:
    """Extract the public share slug from a Filedon view URL or payload."""

    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split('/') if part]
    if len(path_parts) >= 2 and path_parts[0] in {'view', 'embed', 'download', 'force-download'}:
        slug = str(path_parts[1]).strip()
        if slug:
            return slug

    payload = _filedon_data_page_payload(html)
    props = payload.get('props') if isinstance(payload, dict) else {}
    if not isinstance(props, dict):
        return None
    sharing = props.get('sharing')
    if not isinstance(sharing, dict):
        return None
    slug = str(sharing.get('slug') or '').strip()
    return slug or None


def _filedon_download_url_from_html(html: str) -> str | None:
    """Extract the signed Cloudflare R2 download URL from a Filedon page."""

    payload = _filedon_data_page_payload(html)
    props = payload.get('props') if isinstance(payload, dict) else {}
    if not isinstance(props, dict):
        return None
    flash = props.get('flash')
    if not isinstance(flash, dict):
        return None
    download_url = str(flash.get('download_url') or '').strip()
    return download_url or None


async def extract_filedon_media_candidates(
    url: str,
    html: str,
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve Filedon shares into signed Cloudflare R2 media URLs."""

    direct_urls = extract_filedon_media_urls(html)
    if direct_urls:
        return [ResolvedMediaCandidate(url=direct_url) for direct_url in direct_urls]

    slug = _filedon_share_slug(url, html)
    if not slug:
        return []

    parsed = urlparse(url)
    base_url = f'{parsed.scheme}://{parsed.netloc}'
    headers = dict(_PROVIDER_HEADERS)
    headers['Referer'] = referer or url

    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_PROVIDER_TIMEOUT,
        ) as client:
            await client.get(url)
            await client.get(urljoin(base_url, '/sanctum/csrf-cookie'))

            xsrf_token = unquote(str(client.cookies.get('XSRF-TOKEN') or '').strip())
            post_headers = dict(headers)
            post_headers['Origin'] = base_url
            post_headers['Referer'] = url
            post_headers['X-Requested-With'] = 'XMLHttpRequest'
            if xsrf_token:
                post_headers['X-XSRF-TOKEN'] = xsrf_token

            response = await client.post(
                urljoin(base_url, f'/download/{slug}'),
                headers=post_headers,
            )
            if response.status_code not in {302, 303}:
                return []

            redirect_url = urljoin(base_url, str(response.headers.get('location') or url))
            redirect_response = await client.get(redirect_url)
    except Exception:
        return []

    direct_url = _filedon_download_url_from_html(redirect_response.text)
    if not direct_url:
        return []
    return [ResolvedMediaCandidate(url=direct_url)]


def pixeldrain_direct_url(url: str) -> str | None:
    """Convert Pixeldrain share links into direct API file URLs."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if 'pixeldrain.com' not in host:
        return None

    matched = re.match(r'^/(?:u|l)/([A-Za-z0-9]+)', parsed.path)
    if matched:
        return f'{parsed.scheme or "https"}://{parsed.netloc}/api/file/{matched.group(1)}'
    if re.match(r'^/api/file/[A-Za-z0-9]+', parsed.path):
        return url
    return None



def google_drive_direct_url(url: str) -> str | None:
    """Convert common Google Drive share links into direct download URLs."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if 'drive.google.com' not in host:
        return None

    matched = re.match(r'^/file/d/([^/]+)', parsed.path)
    if matched:
        return f'https://drive.google.com/uc?export=download&id={matched.group(1)}'

    query_id = parse_qs(parsed.query).get('id', [])
    if query_id:
        return f'https://drive.google.com/uc?export=download&id={query_id[0]}'
    return None


def _blogger_rpc_metadata(html: str) -> tuple[str, str]:
    f_sid_match = _BLOGGER_FSID_PATTERN.search(html)
    bl_match = _BLOGGER_BL_PATTERN.search(html)
    return (
        str(f_sid_match.group(1) if f_sid_match else '').strip(),
        str(bl_match.group(1) if bl_match else '').strip(),
    )


def _blogger_batchexecute_body(token: str) -> str:
    payload = [[['WcwnYd', json.dumps([token, '', 0], separators=(',', ':')), None, 'generic']]]
    return urlencode({'f.req': json.dumps(payload, separators=(',', ':'))}) + '&'


def _blogger_quality_label(url: str, itag_data: Any) -> str | None:
    itag = None
    if isinstance(itag_data, list) and itag_data:
        try:
            itag = int(itag_data[0])
        except (TypeError, ValueError):
            itag = None
    if itag is None:
        query_values = parse_qs(urlparse(url).query).get('itag', [])
        if query_values:
            try:
                itag = int(query_values[0])
            except ValueError:
                itag = None
    return _BLOGGER_ITAG_QUALITY_MAP.get(itag)


def parse_blogger_batchexecute_response(response_text: str) -> list[ResolvedMediaCandidate]:
    """Parse Blogger batchexecute responses into direct Googlevideo URLs."""

    line_match = _BLOGGER_RESPONSE_LINE_PATTERN.search(response_text or '')
    if not line_match:
        return []

    try:
        envelope = json.loads(line_match.group(0))
    except json.JSONDecodeError:
        return []

    payload_text = ''
    for entry in envelope:
        if isinstance(entry, list) and len(entry) >= 3 and entry[0] == 'wrb.fr' and entry[1] == 'WcwnYd':
            payload_text = str(entry[2] or '')
            break
    if not payload_text:
        return []

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return []

    format_entries = payload[2] if isinstance(payload, list) and len(payload) >= 3 else []
    candidates: list[ResolvedMediaCandidate] = []
    seen: set[str] = set()
    for format_entry in format_entries:
        if not isinstance(format_entry, list) or not format_entry:
            continue
        direct_url = str(format_entry[0] or '').strip()
        if not direct_url or direct_url in seen:
            continue
        seen.add(direct_url)
        quality = _blogger_quality_label(
            direct_url,
            format_entry[1] if len(format_entry) > 1 else None,
        )
        candidates.append(ResolvedMediaCandidate(url=direct_url, quality=quality))
    return candidates


async def extract_blogger_media_candidates(
    provider: BaseAnimeProvider,
    url: str,
    html: str,
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve Blogger wrapper pages into direct Googlevideo URLs."""

    token = parse_qs(urlparse(url).query).get('token', [])
    f_sid, bl = _blogger_rpc_metadata(html)
    if not token or not f_sid or not bl:
        return []

    parsed = urlparse(url)
    endpoint = (
        f'{parsed.scheme}://{parsed.netloc}/_/BloggerVideoPlayerUi/data/batchexecute?'
        f'rpcids=WcwnYd&source-path=%2Fvideo.g&f.sid={f_sid}&bl={bl}&hl=en-US&_reqid=58858&rt=c'
    )
    response_text = await provider._post_text(
        endpoint,
        data=_blogger_batchexecute_body(token[0]),
        referer=referer or url,
    )
    return parse_blogger_batchexecute_response(response_text)


def _media_probe_headers(referer: str | None, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        'User-Agent': _PROVIDER_HEADERS['User-Agent'],
        'Accept': '*/*',
        'Range': 'bytes=0-0',
    }
    if referer:
        headers['Referer'] = referer
    for key, value in dict(extra_headers or {}).items():
        if key and value:
            headers[str(key)] = str(value)
    return headers


def _media_candidate_key(candidate: ResolvedMediaCandidate) -> str:
    parsed = urlparse(candidate.url)
    if 'googlevideo.com' in parsed.netloc.lower():
        query_values = parse_qs(parsed.query)
        video_id = (query_values.get('id') or [''])[0]
        itag = (query_values.get('itag') or [''])[0]
        if video_id and itag:
            return f'googlevideo:{video_id}:{itag}'
    return candidate.url


def dedupe_media_candidates(candidates: list[ResolvedMediaCandidate]) -> list[ResolvedMediaCandidate]:
    """Deduplicate direct media candidates while preserving order."""

    deduped: list[ResolvedMediaCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _media_candidate_key(candidate)
        if not candidate.url or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


async def validate_media_candidates(
    candidates: list[ResolvedMediaCandidate],
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Best-effort validation so wrappers only return reachable media URLs."""

    candidates = dedupe_media_candidates(candidates)
    if not candidates:
        return []

    async def _probe(candidate: ResolvedMediaCandidate) -> ResolvedMediaCandidate | None:
        headers = _media_probe_headers(referer, candidate.headers)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(12.0, connect=6.0),
            ) as client:
                async with client.stream('GET', candidate.url, headers=headers) as response:
                    status_code = response.status_code
                    content_type = str(response.headers.get('content-type') or '').lower()
        except Exception:
            return None

        if status_code not in {200, 206}:
            return None
        if 'text/html' in content_type or content_type.startswith('audio/'):
            return None

        stream_type = candidate.stream_type
        if 'mpegurl' in content_type or 'application/x-mpegurl' in content_type:
            stream_type = 'hls'
        elif 'application/dash+xml' in content_type:
            stream_type = 'dash'

        return ResolvedMediaCandidate(
            url=candidate.url,
            quality=candidate.quality,
            headers=dict(candidate.headers),
            stream_type=stream_type,
        )

    validated = [
        candidate
        for candidate in await asyncio.gather(*(_probe(candidate) for candidate in candidates))
        if candidate is not None
    ]
    return dedupe_media_candidates(validated)


def _hls_attribute_map(line: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for key, raw_value in _HLS_ATTRIBUTE_PATTERN.findall(line or ''):
        value = str(raw_value).strip()
        if value.startswith('\"') and value.endswith('\"'):
            value = value[1:-1]
        attributes[str(key).strip().upper()] = value
    return attributes


def _hls_quality_label(attributes: dict[str, str], url: str) -> str | None:
    resolution = str(attributes.get('RESOLUTION') or '').strip().lower()
    if 'x' in resolution:
        height = resolution.rsplit('x', maxsplit=1)[-1]
        if height.isdigit():
            return f'{int(height)}p'

    for key in ('NAME', 'VIDEO'):
        value = str(attributes.get(key) or '').strip().lower()
        matched = re.search(r'(\d{3,4})', value)
        if matched:
            return f"{matched.group(1)}p"

    path = urlparse(url).path.lower()
    matched = re.search(r'(\d{3,4})p', path)
    if matched:
        return f"{matched.group(1)}p"
    return None


def extract_hls_variant_candidates(
    playlist_text: str,
    *,
    base_url: str,
    headers: dict[str, str] | None = None,
) -> list[ResolvedMediaCandidate]:
    """Extract variant playlists from an HLS master manifest."""

    lines = [line.strip() for line in str(playlist_text or '').splitlines() if line.strip()]
    candidates: list[ResolvedMediaCandidate] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith('#EXT-X-STREAM-INF:'):
            index += 1
            continue

        uri = ''
        next_index = index + 1
        while next_index < len(lines):
            candidate_line = lines[next_index]
            if not candidate_line.startswith('#'):
                uri = candidate_line
                break
            next_index += 1

        if uri:
            attributes = _hls_attribute_map(line.partition(':')[2])
            absolute_url = urljoin(base_url, uri)
            candidates.append(
                ResolvedMediaCandidate(
                    url=absolute_url,
                    quality=_hls_quality_label(attributes, absolute_url),
                    headers=dict(headers or {}),
                )
            )
        index = next_index + 1

    return dedupe_media_candidates(candidates)


async def expand_hls_master_playlist_candidates(
    url: str,
    *,
    referer: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve HLS master playlists into quality-specific variant URLs."""

    if not urlparse(url).path.lower().endswith('.m3u8'):
        return []

    request_headers = {key: value for key, value in dict(headers or {}).items() if key and value}
    if 'User-Agent' not in request_headers:
        request_headers['User-Agent'] = _PROVIDER_HEADERS['User-Agent']
    if referer and 'Referer' not in request_headers:
        request_headers['Referer'] = referer

    try:
        async with httpx.AsyncClient(
            headers=request_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=6.0),
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
    except Exception:
        return []

    content_type = str(response.headers.get('content-type') or '').lower()
    playlist_text = response.text
    if 'text/html' in content_type or '#EXT-X-STREAM-INF' not in playlist_text:
        return []

    return extract_hls_variant_candidates(
        playlist_text,
        base_url=str(response.url),
        headers=request_headers,
    )


def extract_vidhide_media_urls(html: str, *, base_url: str) -> list[str]:
    """Extract direct HLS or MP4 URLs from VidHide-style packed player pages."""

    script_start = html.find('eval(function(p,a,c,k,e,d){')
    if script_start == -1:
        return []

    script_end = html.find('</script>', script_start)
    script_text = html[script_start:script_end] if script_end != -1 else html[script_start:]
    unpacked = _unpack_packer_script(script_text)
    if not unpacked:
        return []

    urls: list[str] = []
    seen: set[str] = set()

    links_match = re.search(r'var\s+links\s*=\s*(\{.*?\});', unpacked, flags=re.S)
    links_payload: dict[str, Any] = {}
    if links_match:
        raw_payload = links_match.group(1)
        try:
            links_payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            links_payload = {
                key: value
                for key, value in re.findall(r'["\']([^"\']+)["\']\s*:\s*["\']([^"\']+)["\']', raw_payload)
            }

    preferred_keys = ['hls4', 'hls3', 'hls2', 'hls1', 'mp4', 'file']
    ordered_keys = preferred_keys + sorted(
        key for key in links_payload.keys() if key not in preferred_keys
    )
    for key in ordered_keys:
        value = str(links_payload.get(key) or '').strip()
        if not value:
            continue
        candidate = urljoin(base_url, value)
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)

    if urls:
        return urls

    for matched in _MEDIA_URL_PATTERN.findall(unpacked):
        candidate = urljoin(base_url, matched.strip())
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls


async def resolve_wrapped_stream_candidates(
    provider: BaseAnimeProvider,
    url: str,
    *,
    referer: str | None = None,
) -> list[ResolvedMediaCandidate]:
    """Resolve known wrapper hosts like Blogger, Filedon, or VidHide into direct media URLs."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return []

    pixeldrain_url = pixeldrain_direct_url(url)
    if pixeldrain_url:
        return [ResolvedMediaCandidate(url=pixeldrain_url)]

    google_drive_url = google_drive_direct_url(url)
    if google_drive_url:
        return await validate_media_candidates(
            [ResolvedMediaCandidate(url=google_drive_url)],
            referer=referer or url,
        )

    if 'mega.' in host:
        return []

    try:
        html, _resolved_base = await provider._request_text(url, referer=referer or url)
    except Exception:
        return []

    if 'blogger.com' in host:
        return await validate_media_candidates(
            await extract_blogger_media_candidates(provider, url, html, referer=referer),
            referer=referer or url,
        )
    if 'desustream' in host or 'desuarcg' in host:
        return await validate_media_candidates(
            await extract_nested_embed_media_candidates(provider, url, html, referer=referer),
            referer=referer or url,
        )
    if 'acefile.co' in host:
        return await validate_media_candidates(
            await extract_acefile_media_candidates(url, html, referer=referer),
            referer=referer or url,
        )
    if 'filedon.co' in host:
        return await validate_media_candidates(
            await extract_filedon_media_candidates(url, html, referer=referer),
            referer=referer or url,
        )
    if 'vidhide' in host:
        return await validate_media_candidates(
            [
                ResolvedMediaCandidate(url=media_url)
                for media_url in extract_vidhide_media_urls(html, base_url=f'{parsed.scheme}://{parsed.netloc}')
            ],
            referer=referer or url,
        )
    return []


async def resolve_wrapped_stream_urls(
    provider: BaseAnimeProvider,
    url: str,
    *,
    referer: str | None = None,
) -> list[str]:
    """Backward-compatible wrapper returning only the direct URLs."""

    candidates = await resolve_wrapped_stream_candidates(provider, url, referer=referer)
    return [candidate.url for candidate in candidates]
