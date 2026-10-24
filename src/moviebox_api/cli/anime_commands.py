"""Click commands for anime search, source inspection, and playback/download."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click

from moviebox_api.anime import (
    anime_default_season,
    anime_has_episode_flow,
    anime_item_from_provider_result,
    anime_provider_order,
    fetch_anime_external_subtitles,
    resolve_anime_source_query,
    resolve_anime_sources,
    search_best_anime_item,
    select_stream_by_quality,
)
from moviebox_api.cli.helpers import command_context_settings, prepare_start, process_download_runner_params
from moviebox_api.constants import (
    CURRENT_WORKING_DIR,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_READ_TIMEOUT_ATTEMPTS,
    DEFAULT_TASKS,
    DEFAULT_TASKS_LIMIT,
    DOWNLOAD_PART_EXTENSION,
    DOWNLOAD_QUALITIES,
    DOWNLOAD_REQUEST_HEADERS,
    DownloadMode,
)
from moviebox_api.download import CaptionFileDownloader, MediaFileDownloader
from moviebox_api.helpers import get_event_loop
from moviebox_api.language import normalize_language_id
from moviebox_api.models import CaptionFileMetadata, MediaFileMetadata
from moviebox_api.providers import SUPPORTED_ANIME_PROVIDERS
from moviebox_api.providers.anime_common import quality_rank
from moviebox_api.providers.models import ProviderSearchResult, ProviderStream, ProviderSubtitle
from moviebox_api.pydantic_compat import HttpUrl
from moviebox_api.stremio.subtitle_sources import (
    SUBDL_API_KEY_ENV,
    SUBSOURCE_API_KEY_ENV,
    subtitle_source_is_configured,
)
from moviebox_api.tui.playback import play_stream

_ANIME_PROVIDER_HELP = f"Anime provider. Supported: {', '.join(SUPPORTED_ANIME_PROVIDERS)}"
_ANIME_PLAYBACK_TARGETS = (
    'auto',
    'mpv',
    'mpvex',
    'vlc',
    'mx-pro',
    'mx-free',
    'web-player',
    'browser',
)
_ANIME_SUBTITLE_SOURCES = ('none', 'provider', 'opensubtitles', 'subdl', 'subsource', 'all')
_DEFAULT_ANIME_PROVIDER = SUPPORTED_ANIME_PROVIDERS[0]


@dataclass(slots=True)
class _AnimeResolutionContext:
    item: Any
    provider_item: ProviderSearchResult
    provider_name: str
    season: int
    episode: int
    streams: list[ProviderStream]
    provider_subtitles: list[ProviderSubtitle]


@click.command(context_settings=command_context_settings)
@click.argument('title')
@click.option(
    '-p',
    '--provider',
    type=click.Choice(SUPPORTED_ANIME_PROVIDERS, case_sensitive=False),
    default=_DEFAULT_ANIME_PROVIDER,
    show_default=True,
    help=_ANIME_PROVIDER_HELP,
)
@click.option(
    '-y',
    '--year',
    type=click.INT,
    default=0,
    show_default=True,
    help='Release year filter',
)
@click.option(
    '-s',
    '--season',
    type=click.IntRange(0, 1000),
    default=1,
    show_default=True,
    help='Season/cour number for episodic anime',
)
@click.option(
    '-e',
    '--episode',
    type=click.IntRange(0, 5000),
    default=1,
    show_default=True,
    help='Episode number for episodic anime',
)
@click.option(
    '-q',
    '--quality',
    type=click.Choice(DOWNLOAD_QUALITIES, case_sensitive=False),
    default='BEST',
    show_default=True,
    help='Preferred quality for the recommended stream',
)
@click.option(
    '-sub',
    '--subtitle',
    'subtitle_source',
    type=click.Choice(_ANIME_SUBTITLE_SOURCES, case_sensitive=False),
    default='provider',
    show_default=True,
    help='Subtitle source selection',
)
@click.option(
    '-x',
    '--language',
    multiple=True,
    default=['Indonesian'],
    show_default=True,
    help='Preferred subtitle language order',
)
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.help_option('-h', '--help')
def source_anime_command(
    title: str,
    provider: str,
    year: int,
    season: int,
    episode: int,
    quality: str,
    subtitle_source: str,
    language: tuple[str, ...],
    json_output: bool,
) -> None:
    """Resolve anime streams and subtitles from Indonesian source providers."""

    try:
        context = _resolve_anime_context(
            title=title,
            provider=provider,
            year=year or None,
            season=season,
            episode=episode,
        )
    except Exception as exc:
        click.echo(f'Anime resolution failed ({provider}): {exc}')
        return

    selected_stream = select_stream_by_quality(context.streams, quality.upper())
    subtitle_entries = _resolve_subtitles(
        item=context.item,
        provider_subtitles=context.provider_subtitles,
        selected_stream=selected_stream,
        subtitle_source=subtitle_source,
        preferred_languages=list(language),
        season=context.season,
        episode=context.episode,
    )

    payload = _build_anime_payload(context, selected_stream=selected_stream, subtitles=subtitle_entries)
    if json_output:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Requested provider: {provider}")
    click.echo(f"Resolved provider: {context.provider_name}")
    year_text = f" ({context.provider_item.year})" if context.provider_item.year else ""
    click.echo(f"Item: {context.provider_item.title}{year_text}")
    click.echo(f"Page: {context.provider_item.page_url}")
    if anime_has_episode_flow(context.item):
        click.echo(f"Episode: S{context.season:02d}E{context.episode:02d}")
    click.echo(f"Streams: {len(context.streams)}")

    for index, stream in enumerate(context.streams, start=1):
        quality_label = f" [{stream.quality}]" if stream.quality else ''
        click.echo(f"{index}. {stream.source}{quality_label}")
        click.echo(f"   {stream.url}")
        if stream.headers:
            click.echo(f"   headers: {', '.join(stream.headers.keys())}")

    if selected_stream is not None:
        click.echo(f"Recommended stream: {selected_stream.source} -> {selected_stream.url}")

    if subtitle_entries:
        click.echo(f"Subtitles: {len(subtitle_entries)}")
        for index, subtitle_entry in enumerate(subtitle_entries, start=1):
            label = str(subtitle_entry.get('label') or '')
            enc = sys.stdout.encoding or 'utf-8'
            safe_label = label.encode(enc, errors='replace').decode(enc)
            click.echo(
                f"  {index}. {subtitle_entry['source']} [{subtitle_entry['language_id']}] "
                f"{safe_label}"
            )


@click.command(context_settings=command_context_settings)
@click.argument('title')
@click.option(
    '-p',
    '--provider',
    type=click.Choice(SUPPORTED_ANIME_PROVIDERS, case_sensitive=False),
    default=_DEFAULT_ANIME_PROVIDER,
    show_default=True,
    help=_ANIME_PROVIDER_HELP,
)
@click.option(
    '-y',
    '--year',
    type=click.INT,
    default=0,
    show_default=True,
    help='Release year filter',
)
@click.option(
    '-s',
    '--season',
    type=click.IntRange(0, 1000),
    default=1,
    show_default=True,
    help='Season/cour number for episodic anime',
)
@click.option(
    '-e',
    '--episode',
    type=click.IntRange(0, 5000),
    default=1,
    show_default=True,
    help='Episode number for episodic anime',
)
@click.option(
    '-q',
    '--quality',
    type=click.Choice(DOWNLOAD_QUALITIES, case_sensitive=False),
    default='BEST',
    show_default=True,
    help='Preferred quality',
)
@click.option(
    '-x',
    '--language',
    multiple=True,
    default=['Indonesian'],
    show_default=True,
    help='Preferred subtitle language order',
)
@click.option(
    '--audio',
    type=click.STRING,
    default='',
    show_default=False,
    help='Preferred audio label when multiple tracks are exposed',
)
@click.option(
    '-d',
    '--dir',
    type=click.Path(exists=True, file_okay=False),
    default=CURRENT_WORKING_DIR,
    show_default=True,
    help='Directory for saving the anime episode or movie',
)
@click.option(
    '-D',
    '--caption-dir',
    type=click.Path(exists=True, file_okay=False),
    default=CURRENT_WORKING_DIR,
    show_default=True,
    help='Directory for saving subtitle files',
)
@click.option(
    '-m',
    '--mode',
    type=click.Choice(DownloadMode.map().keys(), case_sensitive=False),
    default=DownloadMode.AUTO.value,
    show_default=True,
    help='Start mode for file downloads',
)
@click.option(
    '-t',
    '--tasks',
    type=click.IntRange(1, DEFAULT_TASKS_LIMIT),
    default=DEFAULT_TASKS,
    show_default=True,
    help='Number of concurrent tasks to carry out the download',
)
@click.option(
    '-P',
    '--part-dir',
    type=click.Path(exists=True, file_okay=False, writable=True, resolve_path=True),
    default=CURRENT_WORKING_DIR,
    show_default=True,
    help='Directory for temporarily saving download parts',
)
@click.option(
    '-E',
    '--part-extension',
    default=DOWNLOAD_PART_EXTENSION,
    show_default=True,
    help='Filename extension for download parts',
)
@click.option(
    '-N',
    '--chunk-size',
    type=click.INT,
    default=DEFAULT_CHUNK_SIZE,
    show_default=True,
    help='Streaming download chunk size in kilobytes',
)
@click.option(
    '-R',
    '--timeout-retry-attempts',
    type=click.INT,
    default=DEFAULT_READ_TIMEOUT_ATTEMPTS,
    show_default=True,
    help='Number of times to retry download upon read timeout',
)
@click.option(
    '-B',
    '--merge-buffer-size',
    type=click.IntRange(1, 102400),
    show_default=True,
    help='Buffer size for merging separated files in kilobytes [default: chunk size]',
)
@click.option(
    '-X',
    '--stream-via',
    type=click.Choice(_ANIME_PLAYBACK_TARGETS, case_sensitive=False),
    default=None,
    show_default=True,
    help='Stream directly using the selected player target instead of downloading',
)
@click.option(
    '-sub',
    '--subtitle',
    'subtitle_source',
    type=click.Choice(_ANIME_SUBTITLE_SOURCES, case_sensitive=False),
    default='provider',
    show_default=True,
    help='Subtitle source selection',
)
@click.option(
    '-c',
    '--colour',
    default='cyan',
    show_default=True,
    help='Progress bar display colour',
)
@click.option('-U', '--ascii', is_flag=True, help='Use ASCII progress bar characters')
@click.option('-z', '--disable-progress-bar', is_flag=True, help='Do not show download progress bar')
@click.option('--leave/--no-leave', default=False, show_default=True, help='Keep progress bar leaves')
@click.option('-S', '--simple', is_flag=True, help='Show only percentage and bar in progress bar')
@click.option('-T', '--test', is_flag=True, help='Test stream accessibility without downloading')
@click.option('-V', '--verbose', count=True, default=0, help='Show more detailed interactive texts')
@click.option('-Q', '--quiet', is_flag=True, help='Disable interactive progress texts')
@click.option('--json', 'json_output', is_flag=True, help='Output resolved payload as JSON without executing')
@click.help_option('-h', '--help')
def download_anime_command(
    title: str,
    provider: str,
    year: int,
    season: int,
    episode: int,
    quality: str,
    language: tuple[str, ...],
    audio: str,
    dir: Path,
    caption_dir: Path,
    subtitle_source: str,
    verbose: int,
    quiet: bool,
    stream_via: str | None,
    json_output: bool,
    **download_runner_params: Any,
) -> None:
    """Download or stream an anime episode/movie via anime providers."""

    prepare_start(quiet, verbose=verbose)

    try:
        context = _resolve_anime_context(
            title=title,
            provider=provider,
            year=year or None,
            season=season,
            episode=episode,
        )
    except Exception as exc:
        click.echo(f'Anime resolution failed ({provider}): {exc}')
        return

    selected_stream = select_stream_by_quality(
        context.streams,
        quality.upper(),
        prefer_direct=not bool(stream_via),
        audio=audio,
    )
    if selected_stream is None:
        click.echo('No stream matched the requested anime filters.')
        return

    subtitle_entries = _resolve_subtitles(
        item=context.item,
        provider_subtitles=context.provider_subtitles,
        selected_stream=selected_stream,
        subtitle_source=subtitle_source,
        preferred_languages=list(language),
        season=context.season,
        episode=context.episode,
    )

    payload = _build_anime_payload(context, selected_stream=selected_stream, subtitles=subtitle_entries)
    if json_output:
        click.echo(json.dumps(payload, indent=2))
        return

    headers = _merged_headers(selected_stream.headers)
    filename_base = _build_media_basename(
        title=context.provider_item.title,
        year=context.provider_item.year,
        season=context.season,
        episode=context.episode,
        episodic=anime_has_episode_flow(context.item),
        quality=selected_stream.quality,
    )

    if stream_via:
        temp_dir = (
            tempfile.TemporaryDirectory(prefix='moviebox-anime-subtitles-')
            if subtitle_entries
            else None
        )
        try:
            subtitle_paths = _save_subtitles(
                subtitle_entries,
                Path(temp_dir.name) if temp_dir is not None else caption_dir,
                headers,
                filename_base,
                {},
            )
            result = play_stream(
                selected_stream.url,
                headers,
                subtitle_paths,
                subtitle_urls=[entry['url'] for entry in subtitle_entries],
                target_id=stream_via,
                media_title=context.provider_item.title,
                allow_browser_fallback=True,
            )
            click.echo(result.message)
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        return

    media_file = MediaFileMetadata(
        id=hashlib.sha1(selected_stream.url.encode(), usedforsecurity=False).hexdigest(),
        url=cast(HttpUrl, selected_stream.url),
        resolution=quality_rank(selected_stream.quality),
        size=0,
        audio=selected_stream.audio,
    )
    media_filename = f"{filename_base}.{media_file.ext or 'mp4'}"
    runner_params = process_download_runner_params(download_runner_params)
    media_result = get_event_loop().run_until_complete(
        MediaFileDownloader(dir=dir, request_headers=headers).run(
            media_file=media_file,
            filename=media_filename,
            **runner_params,
        )
    )

    saved_media_to = getattr(media_result, 'saved_to', None)
    if saved_media_to is not None:
        click.echo(f'Saved media: {saved_media_to}')
    else:
        click.echo('Media request completed.')

    subtitle_paths = _save_subtitles(subtitle_entries, caption_dir, headers, filename_base, runner_params)
    if subtitle_paths:
        for subtitle_path in subtitle_paths:
            click.echo(f'Saved subtitle: {subtitle_path}')


def _resolve_anime_context(
    *,
    title: str,
    provider: str,
    year: int | None,
    season: int,
    episode: int,
) -> _AnimeResolutionContext:
    normalized_provider = anime_provider_order(provider)[0]
    requested_season = season if season > 0 else 1
    requested_episode = episode if episode > 0 else 1

    anime_item = get_event_loop().run_until_complete(search_best_anime_item(title, year=year))

    provider_item: ProviderSearchResult | None = None
    streams: list[ProviderStream] = []
    provider_subtitles: list[ProviderSubtitle] = []
    resolved_provider = normalized_provider

    if anime_item is not None:
        if not anime_has_episode_flow(anime_item):
            requested_season = 0
            requested_episode = 0

        try:
            event_loop = get_event_loop()
            resolved_result = event_loop.run_until_complete(
                resolve_anime_sources(
                    anime_item,
                    provider_name=normalized_provider,
                    season=requested_season,
                    episode=requested_episode,
                )
            )
            provider_item, streams, provider_subtitles, resolved_provider = resolved_result
        except Exception:
            provider_item = None
            streams = []
            provider_subtitles = []

    if provider_item is None or not streams:
        provider_item, streams, provider_subtitles, resolved_provider = get_event_loop().run_until_complete(
            resolve_anime_source_query(
                title,
                year=year,
                season=requested_season,
                episode=requested_episode,
                provider_name=normalized_provider,
            )
        )

    if provider_item is None or not streams:
        raise RuntimeError('No streams returned by anime providers')

    anime_item = anime_item_from_provider_result(provider_item)
    if not anime_has_episode_flow(anime_item):
        requested_season = 0
        requested_episode = 0

    return _AnimeResolutionContext(
        item=anime_item,
        provider_item=provider_item,
        provider_name=resolved_provider,
        season=requested_season,
        episode=requested_episode,
        streams=streams,
        provider_subtitles=provider_subtitles,
    )


def _subtitle_entry(*, url: str, language: str, label: str, source: str) -> dict[str, str]:
    language_id = normalize_language_id(language)
    return {
        'url': url,
        'language': language,
        'language_id': language_id,
        'label': label or language_id,
        'source': source,
    }


def _collect_provider_subtitles(
    provider_subtitles: list[ProviderSubtitle],
    selected_stream: ProviderStream | None,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    stream_subtitles = selected_stream.subtitles if selected_stream is not None else []
    for subtitle in [*provider_subtitles, *stream_subtitles]:
        subtitle_url = str(getattr(subtitle, 'url', '')).strip()
        if not subtitle_url:
            continue
        language = str(getattr(subtitle, 'language', 'unknown')).strip() or 'unknown'
        label = str(getattr(subtitle, 'label', '')).strip() or language
        entries.append(_subtitle_entry(url=subtitle_url, language=language, label=label, source='provider'))
    return entries


def _resolve_external_sources(subtitle_source: str) -> list[str]:
    if subtitle_source == 'opensubtitles':
        return ['opensubtitles']
    if subtitle_source == 'subdl':
        if subtitle_source_is_configured('subdl'):
            return ['subdl']
        raise RuntimeError(
            f'SubDL secret missing. Set {SUBDL_API_KEY_ENV} or run `moviebox secret-set {SUBDL_API_KEY_ENV}`'
        )
    if subtitle_source == 'subsource':
        if subtitle_source_is_configured('subsource'):
            return ['subsource']
        raise RuntimeError(
            'SubSource secret missing. '
            f'Set {SUBSOURCE_API_KEY_ENV} or run `moviebox secret-set {SUBSOURCE_API_KEY_ENV}`'
        )
    if subtitle_source == 'all':
        selected = ['opensubtitles']
        if subtitle_source_is_configured('subdl'):
            selected.append('subdl')
        if subtitle_source_is_configured('subsource'):
            selected.append('subsource')
        return selected
    return []


def _preferred_language_ids(preferred_languages: list[str]) -> list[str]:
    values: list[str] = []
    for language in preferred_languages:
        language_id = normalize_language_id(language)
        if language_id == 'unknown' or language_id in values:
            continue
        values.append(language_id)
    if not values:
        values = ['ind', 'eng']
    else:
        for fallback in ('ind', 'eng'):
            if fallback not in values:
                values.append(fallback)
    return values


def _resolve_subtitles(
    *,
    item: Any,
    provider_subtitles: list[ProviderSubtitle],
    selected_stream: ProviderStream | None,
    subtitle_source: str,
    preferred_languages: list[str],
    season: int,
    episode: int,
) -> list[dict[str, str]]:
    if subtitle_source == 'none':
        return []

    preferred_language_ids = _preferred_language_ids(preferred_languages)
    provider_entries = _collect_provider_subtitles(provider_subtitles, selected_stream)
    collected: list[dict[str, str]] = []
    if subtitle_source in {'provider', 'all'}:
        collected.extend(provider_entries)

    external_source_name = subtitle_source
    if subtitle_source == 'provider' and not provider_entries:
        external_source_name = 'all'

    if external_source_name in {'opensubtitles', 'subdl', 'subsource', 'all'}:
        sources = _resolve_external_sources(external_source_name)
        if sources:
            external_subtitles = get_event_loop().run_until_complete(
                fetch_anime_external_subtitles(
                    item,
                    season=season,
                    episode=episode,
                    sources=sources,
                    preferred_languages=preferred_languages,
                )
            )
            for subtitle in external_subtitles:
                collected.append(
                    _subtitle_entry(
                        url=subtitle.url,
                        language=subtitle.language,
                        label=subtitle.label,
                        source=subtitle.source,
                    )
                )

    deduped: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for entry in collected:
        subtitle_url = entry['url']
        if subtitle_url in seen_urls:
            continue
        seen_urls.add(subtitle_url)
        deduped.append(entry)

    matching_languages = [
        entry for entry in deduped if entry['language_id'] in preferred_language_ids
    ]
    return matching_languages or deduped


def _merged_headers(stream_headers: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = dict(DOWNLOAD_REQUEST_HEADERS)
    for key, value in stream_headers.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            headers[key_text] = value_text
    return headers


def _sanitize_filename(value: str) -> str:
    safe = ''.join(character for character in value if character.isalnum() or character in ' ._-()').strip()
    return safe or 'anime'


def _build_media_basename(
    *,
    title: str,
    year: int | None,
    season: int,
    episode: int,
    episodic: bool,
    quality: str | None,
) -> str:
    parts = [title]
    if year:
        parts.append(f'({year})')
    if episodic:
        parts.append(f'S{season:02d}E{episode:02d}')
    if quality:
        parts.append(str(quality))
    return _sanitize_filename(' '.join(parts))


def _save_subtitles(
    subtitles: list[dict[str, str]],
    output_dir: Path,
    headers: dict[str, str],
    filename_base: str,
    runner_params: dict[str, Any],
) -> list[Path]:
    saved_paths: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, subtitle_entry in enumerate(subtitles, start=1):
        caption_file = CaptionFileMetadata(
            id=hashlib.sha1(subtitle_entry['url'].encode(), usedforsecurity=False).hexdigest(),
            lan=subtitle_entry['language_id'] or 'sub',
            lanName=subtitle_entry['label'],
            url=cast(HttpUrl, subtitle_entry['url']),
            size=0,
            delay=0,
        )
        caption_filename = (
            f"{filename_base}.{index:02d}.{caption_file.lan}.{caption_file.ext or 'srt'}"
        )
        result = get_event_loop().run_until_complete(
            CaptionFileDownloader(dir=output_dir, request_headers=headers, tasks=1).run(
                caption_file=caption_file,
                filename=caption_filename,
                file_size=1,
                suppress_incompatible_error=True,
                **runner_params,
            )
        )
        saved_to = getattr(result, 'saved_to', None)
        if saved_to is not None:
            saved_paths.append(Path(saved_to))
    return saved_paths


def _stream_payload(stream: ProviderStream) -> dict[str, Any]:
    return {
        'url': stream.url,
        'source': stream.source,
        'quality': stream.quality,
        'size': stream.size,
        'audio': stream.audio,
        'audio_tracks': stream.audio_tracks,
        'headers': stream.headers,
        'subtitles': [
            {
                'url': subtitle.url,
                'language': subtitle.language,
                'label': subtitle.label,
            }
            for subtitle in stream.subtitles
        ],
    }


def _build_anime_payload(
    context: _AnimeResolutionContext,
    *,
    selected_stream: ProviderStream | None,
    subtitles: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        'provider': context.provider_name,
        'requested_episode': {
            'season': context.season,
            'episode': context.episode,
        },
        'item': {
            'id': context.provider_item.id,
            'title': context.provider_item.title,
            'year': context.provider_item.year,
            'page_url': context.provider_item.page_url,
            'payload': context.provider_item.payload,
        },
        'streams': [_stream_payload(stream) for stream in context.streams],
        'selected_stream': _stream_payload(selected_stream) if selected_stream is not None else None,
        'subtitles': subtitles,
    }
