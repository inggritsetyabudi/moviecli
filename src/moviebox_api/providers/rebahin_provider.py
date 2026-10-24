"""Provider implementation for Rebahin (rebahinxxi3.mom).

Scrapes Indonesian subtitle movies and series from Rebahin, decoding
JuicyCodes players to direct HLS m3u8 streams and subtitles.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
import httpx

from moviebox_api.constants import SubjectType
from moviebox_api.providers.base import BaseStreamProvider
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle

logger = logging.getLogger(__name__)

ENV_REBAHIN_BASE_URL = "MOVIEBOX_REBAHIN_BASE_URL"
DEFAULT_REBAHIN_BASE_URL = "https://rebahinxxi3.mom"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
}

_YEAR_PATTERN = re.compile(r"(19|20)\d{2}")
_EPISODE_NUM_PATTERN = re.compile(r"(\d+)")


class RebahinProvider(BaseStreamProvider):
    """Stream provider scraping Rebahin for movies and series with Indonesian subs."""

    name = "rebahin"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str | None = None,
    ):
        self._base_url = (base_url or os.getenv(ENV_REBAHIN_BASE_URL, DEFAULT_REBAHIN_BASE_URL)).strip().rstrip("/")
        self._client = client or httpx.AsyncClient(
            headers=_DEFAULT_HEADERS,
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(20.0),
        )
        self._player_js_cache: str | None = None

    async def search(
        self,
        query: str,
        subject_type: SubjectType = SubjectType.ALL,
        *,
        year: int | None = None,
        limit: int = 20,
    ) -> list[ProviderSearchResult]:
        """Search Rebahin by query."""
        search_url = f"{self._base_url}/?s={quote_plus(query)}"
        try:
            resp = await self._client.get(search_url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Rebahin search request failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[ProviderSearchResult] = []

        cards = soup.select(".movies-list .ml-item, .ml-item, #featured .ml-item")
        for card in cards:
            link_tag = card.select_one("a.ml-mask, a")
            if not link_tag:
                continue

            href = link_tag.get("href")
            if not href or href == "#":
                continue

            page_url = urljoin(self._base_url, href)
            title = link_tag.get("title") or ""
            if not title:
                title_tag = card.select_one("h2, .mli-info h2")
                if title_tag:
                    title = title_tag.text.strip()
            title = title.strip()
            if not title:
                continue

            # Determine subject type: Rebahin puts /series/ in series URLs
            item_type = SubjectType.TV_SERIES if "/series/" in page_url else SubjectType.MOVIES
            if subject_type is not SubjectType.ALL and item_type != subject_type:
                continue

            # Extract year
            item_year: int | None = None
            year_tag = card.select_one(".mli-mvi-year, .year")
            if year_tag and year_tag.text:
                m = _YEAR_PATTERN.search(year_tag.text)
                if m:
                    item_year = int(m.group(0))
            if item_year is None:
                m = _YEAR_PATTERN.search(title)
                if m:
                    item_year = int(m.group(0))

            if year and item_year and item_year != year:
                continue

            # Quality badge (e.g. 720p, 1080p, HD, CAM)
            quality_tag = card.select_one(".mli-quality")
            quality = quality_tag.text.strip() if quality_tag else None

            # Poster image
            img_tag = card.select_one("img")
            poster_url = None
            if img_tag:
                poster_url = img_tag.get("data-original") or img_tag.get("src")
                if poster_url:
                    poster_url = urljoin(self._base_url, poster_url)

            item_id = href.strip("/").split("/")[-1]
            items.append(
                ProviderSearchResult(
                    id=item_id,
                    title=title,
                    page_url=page_url,
                    subject_type=item_type,
                    year=item_year,
                    payload={
                        "quality": quality,
                        "poster": poster_url,
                    },
                )
            )
            if len(items) >= limit:
                break

        return items

    async def resolve_streams(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderStream]:
        """Resolve playable streams and decoded player links from Rebahin."""
        play_url = item.page_url.rstrip("/")
        if not play_url.endswith("/play"):
            play_url = f"{play_url}/play"

        try:
            resp = await self._client.get(play_url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Rebahin play page request failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        servers = soup.select("#server-list .server, .server")

        # Parse subtitles from #subl table on play page
        subtitles: list[ProviderSubtitle] = []
        subl = soup.select_one("#subl")
        if subl:
            for row in subl.select("tr"):
                cells = row.select("td")
                if len(cells) >= 2:
                    lang = cells[0].text.strip().lower()
                    link = cells[1].select_one("a[href]")
                    if link and link.get("href"):
                        sub_url = link["href"].strip()
                        subtitles.append(
                            ProviderSubtitle(
                                url=sub_url,
                                language=lang if "indon" not in lang else "indonesian",
                                label=cells[0].text.strip(),
                            )
                        )

        # Filter servers for TV series if episode is requested
        selected_servers = servers
        is_series = item.subject_type == SubjectType.TV_SERIES or "/series/" in item.page_url
        if is_series and episode > 0:
            matched = []
            for s in servers:
                text = s.text.strip()
                server_id = s.get("id", "")
                ep_match = _EPISODE_NUM_PATTERN.search(text)
                if ep_match and int(ep_match.group(1)) == episode:
                    matched.append(s)
                elif server_id == f"episode-2{episode}" or server_id == f"episode-{episode}":
                    matched.append(s)
            if matched:
                selected_servers = matched

        # Extract iframe URLs from data-iframe attribute
        player_urls: list[tuple[str, str | None]] = []
        for s in selected_servers:
            data_iframe = s.get("data-iframe")
            if not data_iframe:
                continue
            try:
                decoded_url = base64.b64decode(data_iframe.strip()).decode("utf-8", errors="ignore").strip()
                if decoded_url.startswith("http://") or decoded_url.startswith("https://"):
                    quality = s.text.strip()
                    player_urls.append((decoded_url, quality if not is_series else None))
            except Exception:
                continue

        # Also search for standard iframes on page
        if not player_urls:
            for ifr in soup.select("iframe[src]"):
                src = ifr.get("src", "").strip()
                if src.startswith("http"):
                    player_urls.append((src, None))

        streams: list[ProviderStream] = []
        seen_urls: set[str] = set()

        # Concurrently decode JuicyCodes players
        juicy_candidates = [
            (url, q) for url, q in player_urls
            if "199.87.210.226" in url or "178.211.139.171" in url or "/player/" in url or "/embed/" in url
        ]

        if juicy_candidates and shutil.which("node"):
            tasks = [
                self._resolve_juicycodes_stream(url, quality, subtitles)
                for url, quality in juicy_candidates
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, ProviderStream) and res.url not in seen_urls:
                    seen_urls.add(res.url)
                    streams.append(res)

        # Probe if any extracted HLS stream is actually alive and playable
        live_streams: list[ProviderStream] = []
        for s in streams:
            if s.source == "rebahin-hls":
                if await self._probe_stream_alive(s):
                    live_streams.append(s)
                else:
                    logger.debug("Rebahin HLS stream probe failed or returned 403: %s", s.url)
            else:
                live_streams.append(s)
        streams = live_streams

        # If no direct HLS stream is playable, resolve high-speed playable streams via Vega
        # while attaching Rebahin's Indonesian subtitles
        has_direct_stream = any(s.source not in ("rebahin-embed",) for s in streams)
        if not has_direct_stream:
            try:
                from moviebox_api.source import SourceResolver

                clean_title = re.sub(r"\s*\(\d{4}\)", "", item.title).strip()
                resolver = SourceResolver("vega")
                _, backup_streams, _ = await resolver.resolve(
                    title=clean_title,
                    subject_type=item.subject_type,
                    year=item.year,
                    season=season,
                    episode=episode,
                )
                for bs in backup_streams:
                    combined_subs = list(subtitles)
                    for sub in bs.subtitles:
                        if not any(cs.url == sub.url for cs in combined_subs):
                            combined_subs.append(sub)
                    streams.insert(
                        0,
                        ProviderStream(
                            url=bs.url,
                            source=f"rebahin:{bs.source}",
                            quality=bs.quality or item.payload.get("quality"),
                            size=bs.size,
                            audio=bs.audio,
                            subtitles=combined_subs,
                            headers=bs.headers,
                        ),
                    )
            except Exception as exc:
                logger.debug("Failed to resolve backup direct streams for Rebahin: %s", exc)

        # Add embed players as fallback streams for browser playback
        for url, quality in player_urls:
            if url not in seen_urls:
                seen_urls.add(url)
                streams.append(
                    ProviderStream(
                        url=url,
                        source="rebahin-embed",
                        quality=quality or item.payload.get("quality"),
                        subtitles=list(subtitles),
                        headers={"Referer": self._base_url},
                    )
                )

        return streams

    async def _probe_stream_alive(self, stream: ProviderStream) -> bool:
        """Check whether direct stream responds with 200/206 rather than 403/404."""
        try:
            headers = dict(stream.headers or {})
            headers.setdefault("User-Agent", _DEFAULT_HEADERS["User-Agent"])
            async with httpx.AsyncClient(timeout=3.5, verify=False, follow_redirects=True) as client:
                resp = await client.get(stream.url, headers={**headers, "Range": "bytes=0-100"})
                return resp.status_code in (200, 206)
        except Exception:
            return False

    async def resolve_subtitles(
        self,
        item: ProviderSearchResult,
        *,
        season: int = 0,
        episode: int = 0,
    ) -> list[ProviderSubtitle]:
        """Resolve subtitles for Rebahin item."""
        play_url = item.page_url.rstrip("/")
        if not play_url.endswith("/play"):
            play_url = f"{play_url}/play"

        subtitles: list[ProviderSubtitle] = []
        try:
            resp = await self._client.get(play_url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                subl = soup.select_one("#subl")
                if subl:
                    for row in subl.select("tr"):
                        cells = row.select("td")
                        if len(cells) >= 2:
                            lang = cells[0].text.strip().lower()
                            link = cells[1].select_one("a[href]")
                            if link and link.get("href"):
                                sub_url = link["href"].strip()
                                # Skip dead domains like cekgoal
                                if "cekgoal.com" not in sub_url:
                                    subtitles.append(
                                        ProviderSubtitle(
                                            url=sub_url,
                                            language=lang if "indon" not in lang else "indonesian",
                                            label=cells[0].text.strip(),
                                        )
                                    )
        except Exception:
            pass

        # If no Indonesian subtitle found, fallback to OpenSubtitles via Cinemeta lookup
        has_indo = any("indo" in s.language.lower() for s in subtitles)
        if not has_indo:
            try:
                from moviebox_api.stremio.catalog import search_cinemeta_catalog
                from moviebox_api.stremio.subtitle_sources import _fetch_opensubtitles

                clean_title = re.sub(r"\s*\(\d{4}\)", "", item.title).strip()
                matches = await search_cinemeta_catalog(clean_title, subject_type=item.subject_type, limit=3)
                if matches:
                    imdb_id = matches[0].imdbId
                    content_type = "series" if item.subject_type == SubjectType.TV_SERIES else "movie"
                    video_id = f"{imdb_id}:{season}:{episode}" if content_type == "series" else imdb_id
                    ext_subs = await _fetch_opensubtitles(video_id, content_type)
                    for es in ext_subs:
                        if es.language in ("id", "indonesian"):
                            subtitles.append(
                                ProviderSubtitle(
                                    url=es.url,
                                    language="indonesian",
                                    label=f"OpenSubtitles ({es.label})",
                                )
                            )
            except Exception as exc:
                logger.debug("Rebahin subtitle resolution fallback failed: %s", exc)

        return subtitles

    async def _resolve_juicycodes_stream(
        self,
        player_url: str,
        quality: str | None,
        default_subtitles: list[ProviderSubtitle],
    ) -> ProviderStream | None:
        """Decode JuicyCodes player script via Node.js to obtain HLS playlist."""
        try:
            resp = await self._client.get(player_url, headers={"Referer": f"{self._base_url}/"})
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("Failed to fetch player page %s: %s", player_url, exc)
            return None

        m3u8_url, tracks = await self._decode_juicycodes_payload(player_url, resp.text)
        if not m3u8_url:
            return None

        # Build subtitles list
        all_subs = list(default_subtitles)
        for t in tracks:
            file_url = t.get("file")
            if file_url:
                all_subs.append(
                    ProviderSubtitle(
                        url=file_url,
                        language=t.get("label", "Indonesian").lower(),
                        label=t.get("label", "Indonesian"),
                    )
                )

        # Referer for stream requests
        headers = {
            "Referer": player_url,
            "Origin": f"https://{httpx.URL(player_url).host}",
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
        }

        return ProviderStream(
            url=m3u8_url,
            source="rebahin-hls",
            quality=quality or "1080p",
            subtitles=all_subs,
            headers=headers,
        )

    async def _decode_juicycodes_payload(
        self,
        player_url: str,
        html_text: str,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Execute JuicyCodes obfuscated payload inside Node.js."""
        match = re.search(r'_juicycodes\((["\'].*?["\'])\)', html_text)
        if not match:
            return None, []
        juicy_arg = match.group(1)

        # Get player.js (specifically player.js, not jwplayer.js)
        js_match = re.search(r'src=["\']([^"\']*/player\.js[^"\']*)["\']', html_text)
        js_url = urljoin(player_url, js_match.group(1)) if js_match else "https://199.87.210.226/assets/players/jwplayer/player.js"

        if not self._player_js_cache:
            try:
                r_js = await self._client.get(js_url, headers={"Referer": f"{self._base_url}/"})
                if r_js.status_code == 200:
                    self._player_js_cache = r_js.text
            except Exception as exc:
                logger.debug("Failed to load player.js: %s", exc)
                return None, []

        if not self._player_js_cache:
            return None, []

        node_script = f"""
        const window = global;
        window.location = {{ href: {json.dumps(player_url)} }};
        const dummyEl = {{
            innerHTML: '',
            style: {{}},
            appendChild: () => {{}},
            addEventListener: () => {{}},
            removeEventListener: () => {{}},
            setAttribute: () => {{}},
            getAttribute: () => '',
        }};
        const dummyFn = () => dummyEl;
        const document = new Proxy({{
            body: dummyEl,
            head: dummyEl,
            documentElement: dummyEl,
            location: {{ href: {json.dumps(player_url)} }},
        }}, {{
            get: (target, prop) => {{
                if (prop in target) return target[prop];
                return dummyFn;
            }}
        }});
        const jwplayerFn = (id) => ({{
            setup: (cfg) => {{
                console.log('JWPLAYER_SETUP_START');
                console.log(JSON.stringify(cfg));
                console.log('JWPLAYER_SETUP_END');
                return {{ on: () => {{}} }};
            }}
        }});
        global.jwplayer = jwplayerFn;
        window.jwplayer = jwplayerFn;
        {self._player_js_cache}
        _juicycodes({juicy_arg});
        """

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(node_script)
            temp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            output = stdout.decode("utf-8", errors="replace")

            if "JWPLAYER_SETUP_START" in output and "JWPLAYER_SETUP_END" in output:
                json_str = output.split("JWPLAYER_SETUP_START")[1].split("JWPLAYER_SETUP_END")[0].strip()
                cfg = json.loads(json_str)

                # Extract stream file
                sources = cfg.get("sources", [])
                m3u8_file: str | None = None
                if isinstance(sources, list) and sources:
                    m3u8_file = sources[0].get("file")
                elif isinstance(sources, dict):
                    m3u8_file = sources.get("file")

                tracks = cfg.get("tracks", [])
                if not isinstance(tracks, list):
                    tracks = []

                return m3u8_file, tracks
        except Exception as exc:
            logger.debug("Node JuicyCodes execution failed: %s", exc)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        return None, []
