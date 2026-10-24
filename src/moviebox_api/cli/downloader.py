"""Gets the work done - downloads media with flexible flow control"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

import httpx
from throttlebuster import DownloadedFile
from throttlebuster.constants import DOWNLOAD_PART_EXTENSION

from moviebox_api.cli.helpers import (
    get_caption_file_or_raise,
    media_player_name_func_map,
    perform_search_and_get_item,
)
from moviebox_api.constants import (
    CURRENT_WORKING_DIR,
    DEFAULT_CAPTION_LANGUAGE,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TASKS,
    DOWNLOAD_QUALITIES,
    DownloadQualitiesType,
    SubjectType,
)
from moviebox_api.core import Session, TVSeriesDetails
from moviebox_api.download import (
    CaptionFileDownloader,
    DownloadableMovieFilesDetail,
    DownloadableTVSeriesFilesDetail,
    MediaFileDownloader,
    resolve_media_file_to_be_downloaded,
)
from moviebox_api.exceptions import ZeroCaptionFileError, ZeroSearchResultsError
from moviebox_api.helpers import assert_instance, assert_membership, get_event_loop
from moviebox_api.models import DownloadableFilesMetadata, MediaFileMetadata, SearchResultsItem
from moviebox_api.providers import DEFAULT_PROVIDER, ENVIRONMENT_PROVIDER_KEY, normalize_provider_name
from moviebox_api.source import SourceResolver

__all__ = ["Downloader"]


class Downloader:
    """Controls the movie/series download process"""

    def __init__(self, session: Session = None):
        """Constructor for `Downloader`

        Args:
            session (Session, optional): MovieboxAPI httpx request session . Defaults to Session().
        """
        self._session = session if session else Session()
        assert_instance(self._session, Session, "session")

    @staticmethod
    def _normalise_audio_label(audio_label: str | None) -> str:
        if not audio_label:
            return ""
        return audio_label.strip().lower()

    @classmethod
    def _matches_audio_filter(cls, media_file: MediaFileMetadata, requested_audio: str) -> bool:
        requested = cls._normalise_audio_label(requested_audio)
        if not requested:
            return True

        available = cls._normalise_audio_label(media_file.audio)
        if not available:
            return False

        return requested in available or available in requested

    @classmethod
    def _resolve_target_media_file(
        cls,
        downloadable_details: DownloadableFilesMetadata,
        quality: DownloadQualitiesType,
        requested_audio: str | None,
    ) -> MediaFileMetadata:
        normalized_audio = cls._normalise_audio_label(requested_audio)
        if not normalized_audio:
            return resolve_media_file_to_be_downloaded(quality, downloadable_details)

        downloads_with_audio = [
            media_file
            for media_file in downloadable_details.downloads
            if isinstance(media_file.audio, str) and media_file.audio.strip()
        ]
        if not downloads_with_audio:
            return resolve_media_file_to_be_downloaded(quality, downloadable_details)

        filtered_downloads = [
            media_file
            for media_file in downloads_with_audio
            if cls._matches_audio_filter(media_file, normalized_audio)
        ]

        if not filtered_downloads:
            available_audio = sorted(
                {
                    media_file.audio.strip()
                    for media_file in downloads_with_audio
                    if isinstance(media_file.audio, str) and media_file.audio.strip()
                }
            )
            available = ", ".join(available_audio) if available_audio else "none"
            raise ValueError(
                f"Requested audio '{requested_audio}' was not found. Available audio tracks: {available}"
            )

        filtered_details = downloadable_details.model_copy(update={"downloads": filtered_downloads})
        return resolve_media_file_to_be_downloaded(quality, filtered_details)

    @staticmethod
    def _make_downloaded_file(url: str, path: Path) -> DownloadedFile:
        return DownloadedFile(
            url=str(url),
            saved_to=Path(path),
            expected_size=0,
            size=path.stat().st_size if path.exists() else 0,
            duration=0.0,
            merge_duration=0.0,
        )

    async def _download_movie_via_resolver(
        self,
        provider_name: str,
        title: str,
        year: int | None,
        quality: str,
        dir: Path | str,
        caption_dir: Path | str,
        download_caption: bool,
        caption_only: bool,
        stream_via: Literal["mpv", "vlc"] | None,
        test: bool = False,
    ) -> tuple[DownloadedFile | httpx.Response | None, list[DownloadedFile | httpx.Response] | None]:
        resolver = SourceResolver(provider_name)
        item, streams, subtitles = await resolver.resolve(
            title=title,
            subject_type=SubjectType.MOVIES,
            year=year if year and year > 0 else None,
        )
        if not item or not streams:
            raise ZeroSearchResultsError(f"No playable streams found for '{title}' via {provider_name}")

        chosen_stream = streams[0]
        if quality != "BEST":
            for s in streams:
                if s.quality and quality.lower() in s.quality.lower():
                    chosen_stream = s
                    break

        subtitles_dir = tempfile.mkdtemp() if stream_via else caption_dir
        subtitle_details_items: list[DownloadedFile] = []
        if (download_caption or caption_only or stream_via) and subtitles and not test:
            async with httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True) as client:
                for idx, sub in enumerate(subtitles[:3]):
                    try:
                        sub_resp = await client.get(sub.url)
                        if sub_resp.status_code == 200:
                            ext = ".vtt" if ".vtt" in sub.url else ".srt"
                            clean_title = re.sub(r"[^\w\-_\. ]", "_", item.title)
                            sub_file = Path(subtitles_dir) / f"{clean_title}_{sub.language}_{idx}{ext}"
                            sub_file.write_bytes(sub_resp.content)
                            subtitle_details_items.append(self._make_downloaded_file(sub.url, sub_file))
                    except Exception:
                        pass

        if caption_only and not stream_via:
            return (None, subtitle_details_items)

        if stream_via:
            return media_player_name_func_map[stream_via](
                chosen_stream.url, subtitle_details_items, subtitles_dir
            )

        actual_provider = resolver.resolved_provider_name or provider_name
        clean_title = re.sub(r"[^\w\-_\. ]", "_", item.title)
        target_path = Path(dir) / f"{clean_title}_{chosen_stream.quality or 'stream'}.mp4"
        if test:
            import click
            click.echo(
                f"Test mode: successfully verified stream for '{item.title}' via {actual_provider}.\n"
                f"  Stream source: {chosen_stream.source} [{chosen_stream.quality or 'default'}]\n"
                f"  Stream URL: {chosen_stream.url}\n"
                f"  Subtitles found: {len(subtitles)}"
            )
            return (self._make_downloaded_file(chosen_stream.url, target_path), subtitle_details_items)

        import click
        click.echo(
            f"Downloading '{item.title}' via {actual_provider} [{chosen_stream.quality or 'stream'}]..."
        )
        async with httpx.AsyncClient(timeout=300.0, verify=False, follow_redirects=True, headers=chosen_stream.headers) as client:
            async with client.stream("GET", chosen_stream.url) as resp:
                resp.raise_for_status()
                with open(target_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        return (self._make_downloaded_file(chosen_stream.url, target_path), subtitle_details_items)

    async def _download_tv_series_via_resolver(
        self,
        provider_name: str,
        title: str,
        season: int,
        episode: int,
        year: int | None,
        quality: str,
        dir: Path | str,
        caption_dir: Path | str,
        download_caption: bool,
        caption_only: bool,
        stream_via: Literal["mpv", "vlc"] | None,
        test: bool = False,
    ):
        resolver = SourceResolver(provider_name)
        item, streams, subtitles = await resolver.resolve(
            title=title,
            subject_type=SubjectType.TV_SERIES,
            year=year if year and year > 0 else None,
            season=season,
            episode=episode,
        )
        if not item or not streams:
            raise ZeroSearchResultsError(
                f"No playable streams found for '{title}' S{season}E{episode} via {provider_name}"
            )

        chosen_stream = streams[0]
        if quality != "BEST":
            for s in streams:
                if s.quality and quality.lower() in s.quality.lower():
                    chosen_stream = s
                    break

        subtitles_dir = tempfile.mkdtemp() if stream_via else caption_dir
        subtitle_details_items: list[DownloadedFile] = []
        if (download_caption or caption_only or stream_via) and subtitles and not test:
            async with httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True) as client:
                for idx, sub in enumerate(subtitles[:3]):
                    try:
                        sub_resp = await client.get(sub.url)
                        if sub_resp.status_code == 200:
                            ext = ".vtt" if ".vtt" in sub.url else ".srt"
                            clean_title = re.sub(r"[^\w\-_\. ]", "_", item.title)
                            sub_file = Path(subtitles_dir) / f"{clean_title}_S{season}E{episode}_{sub.language}_{idx}{ext}"
                            sub_file.write_bytes(sub_resp.content)
                            subtitle_details_items.append(self._make_downloaded_file(sub.url, sub_file))
                    except Exception:
                        pass

        if caption_only and not stream_via:
            return {season: {episode: {"movie": None, "captions": subtitle_details_items}}}

        if stream_via:
            media_player_name_func_map[stream_via](
                chosen_stream.url, subtitle_details_items, subtitles_dir
            )
            return {season: {episode: {"movie": None, "captions": subtitle_details_items}}}

        actual_provider = resolver.resolved_provider_name or provider_name
        clean_title = re.sub(r"[^\w\-_\. ]", "_", item.title)
        target_path = Path(dir) / f"{clean_title}_S{season}E{episode}_{chosen_stream.quality or 'stream'}.mp4"
        if test:
            import click
            click.echo(
                f"Test mode: successfully verified stream for '{item.title}' S{season}E{episode} via {actual_provider}.\n"
                f"  Stream source: {chosen_stream.source} [{chosen_stream.quality or 'default'}]\n"
                f"  Stream URL: {chosen_stream.url}\n"
                f"  Subtitles found: {len(subtitles)}"
            )
            return {season: {episode: {"movie": self._make_downloaded_file(chosen_stream.url, target_path), "captions": subtitle_details_items}}}

        import click
        click.echo(
            f"Downloading '{item.title}' S{season}E{episode} via {actual_provider} [{chosen_stream.quality or 'stream'}]..."
        )
        async with httpx.AsyncClient(timeout=300.0, verify=False, follow_redirects=True, headers=chosen_stream.headers) as client:
            async with client.stream("GET", chosen_stream.url) as resp:
                resp.raise_for_status()
                with open(target_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        return {season: {episode: {"movie": self._make_downloaded_file(chosen_stream.url, target_path), "captions": subtitle_details_items}}}

    async def download_movie(
        self,
        title: str,
        year: int | None = None,
        yes: bool = False,
        dir: Path | str = CURRENT_WORKING_DIR,
        caption_dir: Path | str = CURRENT_WORKING_DIR,
        quality: DownloadQualitiesType = "BEST",
        movie_filename_tmpl: str = MediaFileDownloader.movie_filename_template,
        caption_filename_tmpl: str = CaptionFileDownloader.movie_filename_template,
        language: tuple[str] = (DEFAULT_CAPTION_LANGUAGE,),
        audio: str | None = None,
        download_caption: bool = False,
        caption_only: bool = False,
        stream_via: Literal["mpv", "vlc"] | None = None,
        provider: str | None = None,
        search_function: callable = perform_search_and_get_item,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        tasks: int = DEFAULT_TASKS,
        part_dir: Path | str = CURRENT_WORKING_DIR,
        part_extension: str = DOWNLOAD_PART_EXTENSION,
        merge_buffer_size: int | None = None,
        ignore_missing_caption: bool = False,
        **run_kwargs,
    ) -> tuple[
        DownloadedFile | httpx.Response | None,
        list[DownloadedFile | httpx.Response] | None,
    ]:
        """Search movie by name and proceed to download it or stream it."""
        assert_membership(quality, DOWNLOAD_QUALITIES)

        provider_name = normalize_provider_name(provider or os.getenv(ENVIRONMENT_PROVIDER_KEY, DEFAULT_PROVIDER))
        return await self._download_movie_via_resolver(
            provider_name=provider_name,
            title=title,
            year=year,
            quality=quality,
            dir=dir,
            caption_dir=caption_dir,
            download_caption=download_caption,
            caption_only=caption_only,
            stream_via=stream_via,
            test=bool(run_kwargs.get("test", False)),
        )

        assert callable(search_function), (
            f"Value for search_function must be callable not {type(search_function)}"
        )

        MediaFileDownloader.movie_filename_template = movie_filename_tmpl
        CaptionFileDownloader.movie_filename_template = caption_filename_tmpl

        try:
            target_movie = await search_function(
                self._session,
                title=title,
                year=year,
                subject_type=SubjectType.MOVIES,
                yes=yes,
            )
        except Exception:
            return await self._download_movie_via_resolver(
                provider_name=DEFAULT_PROVIDER,
                title=title,
                year=year,
                quality=quality,
                dir=dir,
                caption_dir=caption_dir,
                download_caption=download_caption,
                caption_only=caption_only,
                stream_via=stream_via,
                test=bool(run_kwargs.get("test", False)),
            )

        assert isinstance(target_movie, SearchResultsItem), (
            f"Search function {search_function.__name__} must return an instance of "
            f"{SearchResultsItem} not {type(target_movie)}"
        )

        downloadable_details_inst = DownloadableMovieFilesDetail(self._session, target_movie)

        downloadable_details = await downloadable_details_inst.get_content_model()

        target_media_file = self._resolve_target_media_file(downloadable_details, quality, audio)

        subtitle_details_items: list[DownloadedFile] = []

        subtitles_dir = tempfile.mkdtemp() if stream_via else caption_dir

        if download_caption or caption_only:
            for lang in language:
                try:
                    target_caption_file = get_caption_file_or_raise(downloadable_details, lang)

                except (ZeroCaptionFileError, ValueError):
                    if ignore_missing_caption:
                        continue
                    raise

                caption_downloader = CaptionFileDownloader(
                    dir=subtitles_dir,
                    chunk_size=chunk_size,
                    tasks=tasks,
                    part_dir=part_dir,
                    part_extension=part_extension,
                    merge_buffer_size=merge_buffer_size,
                )

                subtitle_details = await caption_downloader.run(
                    caption_file=target_caption_file,
                    filename=target_movie,
                    **run_kwargs,
                )

                subtitle_details_items.append(subtitle_details)

            if caption_only and not stream_via:
                # terminate
                return (None, subtitle_details_items)

        if stream_via:
            return media_player_name_func_map[stream_via](
                str(target_media_file.url), subtitle_details_items, subtitles_dir
            )

        movie_downloader = MediaFileDownloader(
            dir=dir,
            chunk_size=chunk_size,
            tasks=tasks,
            part_dir=part_dir,
            part_extension=part_extension,
            merge_buffer_size=merge_buffer_size,
        )

        movie_details = await movie_downloader.run(
            media_file=target_media_file, filename=target_movie, **run_kwargs
        )
        return (movie_details, subtitle_details_items)

    async def download_tv_series(
        self,
        title: str,
        season: int,
        episode: int,
        year: int | None = None,
        yes: bool = False,
        dir: Path | str = CURRENT_WORKING_DIR,
        caption_dir: Path | str = CURRENT_WORKING_DIR,
        quality: DownloadQualitiesType = "BEST",
        episode_filename_tmpl: str = MediaFileDownloader.series_filename_template,
        caption_filename_tmpl: str = CaptionFileDownloader.series_filename_template,
        language: tuple = (DEFAULT_CAPTION_LANGUAGE,),
        audio: str | None = None,
        download_caption: bool = False,
        caption_only: bool = False,
        stream_via: Literal["mpv", "vlc"] | None = None,
        provider: str | None = None,
        limit: int = 1,
        search_function: callable = perform_search_and_get_item,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        tasks: int = DEFAULT_TASKS,
        part_dir: Path | str = CURRENT_WORKING_DIR,
        part_extension: str = DOWNLOAD_PART_EXTENSION,
        merge_buffer_size: int | None = None,
        ignore_missing_caption: bool = False,
        auto_mode: bool = False,
        format: Literal["group", "struct"] | None = None,
        **run_kwargs,
    ) -> dict[
        int,
        dict[
            int,
            dict[str, DownloadedFile | httpx.Response | list[DownloadedFile | httpx.Response]],
        ],
    ]:
        """Search tv-series by name and proceed to download or stream its episodes.

        Args:
            title (str): Complete or partial tv-series name.
            season (int): Target season number of the tv-series.
            episode (int): Target episode number of the tv-series.
            year (int|None, optional): `releaseDate.year` filter for the tv-series. Defaults to None.
            yes (bool, optional): Proceed with the first item in the results instead of prompting confirmation. Defaults to False.
            dir (Path|str, optional): Directory for saving the movie file to. Defaults to CURRENT_WORKING_DIR.
            caption_dir (Path|str, optional): Directory for saving the caption files to. Defaults to CURRENT_WORKING_DIR.
            quality (DownloadQualitiesType, optional): Episode quality such as `720p` or simply `BEST` etc. Defaults to 'BEST'.
            episode_filename_tmpl (str, optional): Template for generating episode filename. Defaults to MediaFileDownloader.series_filename_template.
            caption_filename_tmpl (str, optional): Template for generating caption filename. Defaults to CaptionFileDownloader.series_filename_template.
            language (tuple, optional): Languages to download captions in. Defaults to (DEFAULT_CAPTION_LANGUAGE,).
            download_caption (bool, optional): Whether to download caption or not. Defaults to False.
            caption_only (bool, optional): Whether to ignore episode files or not. Defaults to False.
            stream_via (Literal["mpv", "vlc"], optional): Stream directly in chosen media played instead of downloading. Defaults to None.
            limit (int, optional): Number of episodes to download including the offset episode. Defaults to 1.
            search_function (callable, optional): Accepts `session`, `title`, `year`, `subject_type` & `yes` and returns item.
            chunk_size (int, optional): Streaming download chunk size in kilobytes. Defaults to DEFAULT_CHUNK_SIZE.
            tasks (int, optional): Number of tasks to carry out the download. Defaults to DEFAULT_TASKS.
            part_dir (Path | str, optional): Directory for temporarily saving the downloaded file-parts to. Defaults to CURRENT_WORKING_DIR.
            part_extension (str, optional): Filename extension for download parts. Defaults to DOWNLOAD_PART_EXTENSION.
            merge_buffer_size (int|None, optional). Buffer size for merging the separated files in kilobytes. Defaults to chunk_size.
            auto_mode (bool, optional). Iterate over seasons as well. When limit is 1 (default), download entire tv series. Defaults to False.
            format(Literal["filename", "group", "struct"] | None, optional): Ways of formating filename and saving the episodes. Defaults to None
                group -> Organize episodes into separate folders based on seasons e.g Merlin/S1/Merlin S1E2.mp4
                struct -> Save episodes in a hierarchical directory structure e.g Merlin (2009)/S1/E1.mp4

        run_kwargs: Other keyword arguments for `MediaFileDownloader.run`

        Returns:
             dict[int, dict[str, DownloadedFile | httpx.Response  | list[DownloadedFile | httpx.Response ]]]: Episode number and downloaded episode file details and caption files.
        """  # noqa: E501

        assert_membership(quality, DOWNLOAD_QUALITIES)

        provider_name = normalize_provider_name(provider or os.getenv(ENVIRONMENT_PROVIDER_KEY, DEFAULT_PROVIDER))
        return await self._download_tv_series_via_resolver(
            provider_name=provider_name,
            title=title,
            season=season,
            episode=episode,
            year=year,
            quality=quality,
            dir=dir,
            caption_dir=caption_dir,
            download_caption=download_caption,
            caption_only=caption_only,
            stream_via=stream_via,
            test=bool(run_kwargs.get("test", False)),
        )

        assert callable(search_function), (
            f"Value for search_function must be callable not {type(search_function)}"
        )

        MediaFileDownloader.series_filename_template = episode_filename_tmpl
        CaptionFileDownloader.series_filename_template = caption_filename_tmpl

        group = False

        match format:
            case "group":
                group = True

            case "struct":
                MediaFileDownloader.series_filename_template = "E{episode}.{ext}"
                CaptionFileDownloader.series_filename_template = "E{episode}.{lan}.{ext}"
                group = True

        try:
            target_tv_series = await search_function(
                self._session,
                title=title,
                year=year,
                subject_type=SubjectType.TV_SERIES,
                yes=yes,
            )
        except Exception:
            return await self._download_tv_series_via_resolver(
                provider_name=DEFAULT_PROVIDER,
                title=title,
                season=season,
                episode=episode,
                year=year,
                quality=quality,
                dir=dir,
                caption_dir=caption_dir,
                download_caption=download_caption,
                caption_only=caption_only,
                stream_via=stream_via,
                test=bool(run_kwargs.get("test", False)),
            )
        assert isinstance(target_tv_series, SearchResultsItem), (
            f"Search function {search_function.__name__} must return an instance of "
            f"{SearchResultsItem} not {type(target_tv_series)}"
        )

        downloadable_files = DownloadableTVSeriesFilesDetail(self._session, target_tv_series)

        subtitles_dir = tempfile.mkdtemp() if stream_via else caption_dir

        caption_downloader = CaptionFileDownloader(
            dir=dir if group else subtitles_dir,
            chunk_size=chunk_size,
            tasks=tasks,
            part_dir=part_dir,
            part_extension=part_extension,
            merge_buffer_size=merge_buffer_size,
            group_series=group,
        )

        media_file_downloader = MediaFileDownloader(
            dir=dir,
            chunk_size=chunk_size,
            tasks=tasks,
            part_dir=part_dir,
            part_extension=part_extension,
            merge_buffer_size=merge_buffer_size,
            group_series=group,
        )

        core_tv_series_details = TVSeriesDetails(target_tv_series, self._session)

        tv_series_details_model = await core_tv_series_details.get_json_details_extractor_model()
        series_resource = tv_series_details_model.resource

        async def download_episodes_per_season(
            season_number: int,
            first_episode_number: int,
            episode_limit: int,
        ):
            response = {}
            for episode_count in range(episode_limit):
                current_episode = first_episode_number + episode_count

                downloadable_files_detail = await downloadable_files.get_content_model(
                    season=season_number, episode=current_episode
                )

                current_episode_details = {}
                caption_details_items: list[DownloadedFile] = []

                if caption_only or download_caption:
                    for lang in language:
                        try:
                            target_caption_file = get_caption_file_or_raise(downloadable_files_detail, lang)

                        except (ZeroCaptionFileError, ValueError):
                            if ignore_missing_caption:
                                continue
                            raise

                        caption_details = await caption_downloader.run(
                            caption_file=target_caption_file,
                            filename=target_tv_series,
                            season=season_number,
                            episode=current_episode,
                            **run_kwargs,
                        )

                        caption_details_items.append(caption_details)

                    if caption_only and not stream_via:
                        # Avoid downloading tv-series
                        continue

                # Download or stream series

                current_episode_details["captions"] = caption_details_items

                target_media_file = self._resolve_target_media_file(downloadable_files_detail, quality, audio)

                if stream_via:
                    media_player_name_func_map[stream_via](
                        str(target_media_file.url), caption_details_items, subtitles_dir
                    )

                    continue

                tv_series_details = await media_file_downloader.run(
                    media_file=target_media_file,
                    filename=target_tv_series,
                    season=season_number,
                    episode=current_episode,
                    **run_kwargs,
                )

                current_episode_details["movie"] = tv_series_details
                response[current_episode] = current_episode_details

            return response

        if auto_mode:
            if series_resource.total_seasons < season:
                raise RuntimeError(
                    f"The target season {season} exceeds the available "
                    f"tv series seasons {series_resource.total_seasons}."
                )

            total_episodes = 0
            downloaded_episodes_count = 0
            response_jar = {}

            target_seasons = series_resource.seasons[season - 1 :]

            for index, series_season in enumerate(target_seasons):
                new_episodes_count = series_season.maxEp

                if index == 0:
                    # episode offset
                    if series_season.maxEp < episode:
                        raise RuntimeError(
                            f"The target episode offset {episode} for season {series_season.se}"
                            f" is greater than the available episodes {series_season.maxEp}"
                        )

                    else:
                        new_episodes_count -= episode - 1

                total_episodes += new_episodes_count

            if limit != 1:
                if limit > total_episodes:
                    logging.warning(
                        f"You have set total episodes limit to {limit} but only {total_episodes} "
                        f"episodes are available starting from the offset ({season=}, {episode=}"
                        "). The former will be ignored."
                    )
                    limit = total_episodes

            else:
                limit = total_episodes

            logging.info(
                f"Process overview - Seasons: {len(target_seasons)}, total episodes: {total_episodes}, "
                f"episodes download limit: {limit} "
            )

            for index, target_season in enumerate(target_seasons):
                if index == 0:
                    first_episode_number = episode  # declared by user
                    episodes_limit = target_season.maxEp - (episode - 1)  # 1 = index 0

                    if episodes_limit > limit:
                        episodes_limit = limit

                else:
                    first_episode_number = 1

                    remaining_episodes_amount = limit - downloaded_episodes_count

                    if target_season.maxEp > remaining_episodes_amount:
                        episodes_limit = remaining_episodes_amount

                    else:
                        episodes_limit = target_season.maxEp

                downloaded_episodes_details = await download_episodes_per_season(
                    season_number=target_season.se,
                    first_episode_number=first_episode_number,
                    episode_limit=episodes_limit,
                )
                response_jar[target_season.se] = downloaded_episodes_details

                downloaded_episodes_count += episodes_limit

            return response_jar

        else:
            try:
                target_season = series_resource.get_season_by_number(season)
            except ValueError:
                if not series_resource.seasons:
                    raise

                fallback_season = sorted(series_resource.seasons, key=lambda current: current.se)[0]
                logging.warning(
                    "Requested season %s is unavailable. Falling back to season %s.",
                    season,
                    fallback_season.se,
                )
                season = fallback_season.se
                target_season = fallback_season

            assert episode <= target_season.maxEp, (
                f"The chosen episode offset {episode} exceeds the available episodes {target_season.maxEp}"
            )

            available_episodes = target_season.maxEp - (episode - 1)  # offset

            if limit > available_episodes:
                logging.warning(
                    f"You have set episodes limit to {limit} but only {available_episodes} "
                    f"episodes are available for season {season}, starting from the offset {episode}. "
                    "The former will be ignored."
                )
                limit = available_episodes

            logging.info(
                f"Season {target_season.se} details - Total episodes: {target_season.maxEp}, "
                f"episodes download limit: {limit}"
            )

            downloaded_tv_series_details = await download_episodes_per_season(
                season_number=season, first_episode_number=episode, episode_limit=limit
            )

            return {season: downloaded_tv_series_details}

    def download_movie_sync(
        self,
        *args,
        **kwargs,
    ) -> tuple[
        DownloadedFile | httpx.Response | None,
        list[DownloadedFile | httpx.Response] | None,
    ]:
        """Synchronously search movie by name and proceed to download or stream it."""
        return get_event_loop().run_until_complete(self.download_movie(*args, **kwargs))

    def download_tv_series_sync(
        self,
        *args,
        **kwargs,
    ) -> dict[
        int,
        dict[str, DownloadedFile | httpx.Response | list[DownloadedFile | httpx.Response]],
    ]:
        """Synchronously search tv-series by name and proceed to download or stream its episodes."""
        return get_event_loop().run_until_complete(self.download_tv_series(*args, **kwargs))
