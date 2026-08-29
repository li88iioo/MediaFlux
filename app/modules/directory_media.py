"""光鸭目录媒体组检查与执行前快照。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules.organize import OrganizeRules, Organizer, organize_rules_snapshot
from app.modules.organize_postprocess import media_role, normalized_stem
from app.modules.directory_scrape_errors import (
    DirectoryScrapeRequestError,
    DirectoryScrapeStateError,
)
from app.modules.scraper import TMDBScraper
from app.modules.subtitle_identity import plan_subtitle_companions
from app.modules.special_media import (
    is_special_directory_name,
    is_special_media_name,
    is_special_path,
)


@dataclass(frozen=True)
class MediaSnapshot:
    file_id: str
    parent_id: str
    name: str
    size: int
    etag: str
    role: str
    relative_dir: str
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True)
class DirectorySnapshot:
    file_id: str
    parent_id: str
    name: str
    etag: str
    relative_dir: str


@dataclass(frozen=True)
class PendingMedia:
    file: MediaSnapshot
    reason: str


@dataclass(frozen=True)
class _VideoIdentity:
    file: MediaSnapshot
    special: bool
    special_container: bool
    media_type: str
    title: str
    year: str
    tmdb_id: str


@dataclass(frozen=True)
class DirectoryInspection:
    directory_id: str
    directory_name: str
    media_type: str
    suggested_query: str
    videos: tuple[MediaSnapshot, ...]
    companions: tuple[MediaSnapshot, ...]
    counts: dict[str, int]
    mixed: bool
    fingerprint: str
    season: int | None = None
    episode: int | None = None
    season_inferred: bool = False
    pending_videos: tuple[PendingMedia, ...] = ()
    directories: tuple[DirectorySnapshot, ...] = ()
    requires_manual_match: bool = False
    manual_match_reason: str = ""

    @property
    def representative(self) -> MediaSnapshot:
        return min(
            self.videos,
            key=lambda item: (
                item.relative_dir.count("/"),
                len(item.name),
                item.name.casefold(),
                item.file_id,
            ),
        )


class DirectoryMediaInspector:
    def __init__(
        self,
        client: GuangYaClient | None = None,
        scraper: TMDBScraper | None = None,
    ) -> None:
        self.client = client or GuangYaClient()
        self.scraper = scraper or TMDBScraper()
        self.organizer = Organizer(client=self.client, scraper=self.scraper)

    def inspect(
        self,
        directory_id: str,
        rules: OrganizeRules,
    ) -> DirectoryInspection:
        source_id = str(directory_id or "").strip()
        if not source_id or source_id == "0":
            raise DirectoryScrapeRequestError("请选择需要刮削的目录")
        source = self.client.file_info(source_id)
        if source is None or not source.is_dir:
            raise DirectoryScrapeRequestError("所选目录不存在或已被移动")
        target_id = str(rules.target_dir_id or "").strip()
        if not target_id or target_id == "0":
            raise DirectoryScrapeStateError("请先配置光鸭整理归档目标")
        self._validate_target_outside_source(source_id, target_id)

        video_exts = self.organizer.video_exts(rules)
        metadata_exts = self.organizer.metadata_exts(rules)
        discovered_videos: list[MediaSnapshot] = []
        video_identities: list[_VideoIdentity] = []
        companions: list[MediaSnapshot] = []
        directories: list[DirectorySnapshot] = [
            self._directory_snapshot(source, "")
        ]
        visited: set[str] = set()
        scanned_entries = 0
        parsed_dir = self._parse_for_rules(source.name, rules)

        def scan(
            current_id: str,
            relative_dir: str,
            depth: int = 0,
            inherited_season: int | None = None,
        ) -> None:
            nonlocal scanned_entries
            if depth > 64:
                raise DirectoryScrapeRequestError("目录层级超过 64 层，停止刮削")
            if current_id in visited:
                raise DirectoryScrapeRequestError("目录结构存在循环引用")
            visited.add(current_id)
            for item in self.client.list_dir(current_id):
                scanned_entries += 1
                if scanned_entries > 20000:
                    raise DirectoryScrapeRequestError("目录文件超过 20000 项，请进入子目录分别刮削")
                if item.is_dir:
                    resolved_dir = self._enrich_identity(item)
                    child_rel = (
                        f"{relative_dir}/{resolved_dir.name}"
                        if relative_dir
                        else resolved_dir.name
                    )
                    directories.append(self._directory_snapshot(resolved_dir, child_rel))
                    parsed_child = self._parse_for_rules(resolved_dir.name, rules)
                    child_season = self._optional_season(parsed_child.get("season"))
                    scan(
                        resolved_dir.file_id,
                        child_rel,
                        depth + 1,
                        child_season if child_season is not None else inherited_season,
                    )
                    continue
                ext = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
                if ext in video_exts:
                    if rules.small_file_mb and (
                        0 < item.size < rules.small_file_mb * 1024 * 1024
                    ):
                        continue
                    resolved = self._enrich_identity(item)
                    parsed = self._parse_for_rules(resolved.name, rules)
                    special_container = (
                        not rules.nsfw_exclusive and is_special_path(relative_dir)
                    )
                    special_child = special_container or (
                        not rules.nsfw_exclusive and is_special_media_name(item.name)
                    )
                    if special_child:
                        parsed = dict(parsed)
                        parsed["type"] = "tv"
                        parsed["season"] = 0
                    elif (
                        inherited_season is not None
                        and parsed.get("season") is None
                        and parsed.get("episode") is not None
                    ):
                        # 裸集号文件继承所在 Season 子目录，而不是整批默认落入 S01。
                        parsed = dict(parsed)
                        parsed["type"] = "tv"
                        parsed["season"] = inherited_season
                    snapshot = self._snapshot(resolved, "video", relative_dir, parsed)
                    discovered_videos.append(snapshot)
                    parsed_type = (
                        "tv"
                        if parsed.get("type") == "tv"
                        or parsed.get("season") is not None
                        or parsed.get("episode") is not None
                        else "movie"
                    )
                    video_identities.append(_VideoIdentity(
                        file=snapshot,
                        special=special_child,
                        special_container=special_container,
                        media_type=parsed_type,
                        title=str(parsed.get("title") or "").strip(),
                        year=str(parsed.get("year") or "").strip(),
                        tmdb_id=str(parsed.get("tmdb_id") or "").strip(),
                    ))
                    continue
                if ext in metadata_exts:
                    resolved = self._enrich_identity(item)
                    companions.append(self._snapshot(
                        resolved,
                        media_role(resolved.name),
                        relative_dir,
                        {},
                    ))

        scan(source_id, "")
        discovered_videos = self._assign_special_episodes(discovered_videos)
        if not discovered_videos:
            raise DirectoryScrapeRequestError("所选目录中没有支持的视频文件")

        fingerprint_companions = list(companions)
        snapshots_by_id = {item.file_id: item for item in discovered_videos}
        normalized_identities = [
            replace(item, file=snapshots_by_id[item.file.file_id])
            for item in video_identities
        ]
        videos, pending_videos = self._select_primary_media_group(normalized_identities)
        special_only = bool(normalized_identities) and all(
            item.special for item in normalized_identities
        )
        source_identity = str(parsed_dir.get("title") or source.name or "").strip()
        requires_manual_match = special_only and self._ambiguous_special_source(source_identity)
        manual_match_reason = (
            "目录中仅有 OVA/特典等特殊内容，且缺少可确认的作品名，请人工选择 TMDB 媒体"
            if requires_manual_match else ""
        )

        if pending_videos:
            companions = self._safe_companions_for_videos(videos, companions)

        videos.sort(key=lambda item: (item.relative_dir.casefold(), item.name.casefold(), item.file_id))
        companions.sort(
            key=lambda item: (item.relative_dir.casefold(), item.name.casefold(), item.file_id)
        )
        media_type = "tv" if any(
            item.season is not None or item.episode is not None for item in videos
        ) else "movie"
        counts = {
            "video": len(videos),
            "subtitle": sum(item.role == "subtitle" for item in companions),
            "metadata": sum(item.role != "subtitle" for item in companions),
        }
        if pending_videos:
            counts["video_total"] = len(discovered_videos)
            counts["pending_video"] = len(pending_videos)
        fingerprint = self._fingerprint(
            videos,
            fingerprint_companions,
            rules,
            pending_videos=pending_videos,
            directories=directories,
        )
        if (
            parsed_dir.get("type") == "tv"
            or parsed_dir.get("season") is not None
            or parsed_dir.get("episode") is not None
        ):
            media_type = "tv"
        video_seasons = {
            int(item.season)
            for item in videos
            if item.season is not None and int(item.season) != 0
        }
        has_unseasoned_episodes = any(
            item.episode is not None and item.season is None
            for item in videos
        )
        parsed_directory_season = self._optional_season(parsed_dir.get("season"))
        season = None
        season_inferred = False
        if media_type == "tv":
            if (
                parsed_directory_season is not None
                and video_seasons
                and video_seasons != {parsed_directory_season}
            ):
                explicit = "、".join(f"S{value:02d}" for value in sorted(video_seasons))
                raise DirectoryScrapeRequestError(
                    f"目录季号与文件季号冲突（目录 S{parsed_directory_season:02d}，文件 {explicit}），"
                    "请修正季号或进入对应季目录刮削"
                )
            if parsed_directory_season is not None:
                season = parsed_directory_season
            elif len(video_seasons) == 1:
                # 同一批文件只有一个明确季号时，裸集号文件继承该季，避免误落 S01。
                season = next(iter(video_seasons))
                season_inferred = has_unseasoned_episodes
            elif not video_seasons and any(item.episode is not None for item in videos):
                # 裸集号文件为父目录续作别名提供剧集上下文。例如 ``04.mkv``
                # 位于 ``... 2nd Attack`` 时可安全解释为第二季；普通电影目录
                # 单独出现该短语时仍不会触发季号推断。
                contextual_season = None
                position_parser = getattr(self.scraper, "parse_source_position", None)
                episode_sample = next((
                    item for item in videos if item.episode is not None
                ), None)
                if episode_sample is not None and callable(position_parser):
                    try:
                        contextual_season, _ = position_parser(
                            episode_sample.name, source.name,
                        )
                        contextual_season = self._optional_season(contextual_season)
                    except Exception:
                        contextual_season = None
                season = contextual_season if contextual_season is not None else 1
                season_inferred = True
        clean_fn = getattr(self.scraper, "clean_title", None)
        clean_dir_name = clean_fn(source.name) if callable(clean_fn) else ""
        selected_ids = {item.file_id for item in videos}
        primary_titles = [
            item.title
            for item in normalized_identities
            if item.file.file_id in selected_ids
            and not item.special
            and not self._weak_identity_title(item.title)
        ]
        primary_title = primary_titles[0] if primary_titles else ""
        parsed_dir_title = str(parsed_dir.get("title") or "").strip()
        if rules.nsfw_exclusive:
            suggested_query = (
                self._nsfw_code(source.name, rules)
                or primary_title
                or parsed_dir_title
                or source.name
            )
        else:
            suggested_query = (
                (clean_dir_name if not self._weak_identity_title(clean_dir_name) else "")
                or (parsed_dir_title if not self._weak_identity_title(parsed_dir_title) else "")
                or primary_title
                or source.name
            )
        return DirectoryInspection(
            directory_id=source_id,
            directory_name=source.name,
            media_type=media_type,
            suggested_query=suggested_query,
            videos=tuple(videos),
            companions=tuple(companions),
            counts=counts,
            mixed=bool(pending_videos),
            fingerprint=fingerprint,
            season=season,
            season_inferred=season_inferred,
            pending_videos=tuple(pending_videos),
            directories=tuple(directories),
            requires_manual_match=requires_manual_match,
            manual_match_reason=manual_match_reason,
        )

    def inspect_file(
        self,
        file_id: str,
        rules: OrganizeRules,
    ) -> DirectoryInspection:
        source_id = str(file_id or "").strip()
        if not source_id or source_id == "0":
            raise DirectoryScrapeRequestError("请选择需要刮削的视频文件")
        source = self.client.file_info(source_id)
        if source is None or source.is_dir:
            raise DirectoryScrapeRequestError("所选视频文件不存在或已被移动")

        ext = source.name.rsplit(".", 1)[-1].lower() if "." in source.name else ""
        if ext not in self.organizer.video_exts(rules):
            raise DirectoryScrapeRequestError("所选文件不是当前整理规则支持的视频")
        if rules.small_file_mb and (
            0 < source.size < rules.small_file_mb * 1024 * 1024
        ):
            raise DirectoryScrapeRequestError("所选视频属于当前整理规则过滤的小文件")

        target_id = str(rules.target_dir_id or "").strip()
        if not target_id or target_id == "0":
            raise DirectoryScrapeStateError("请先配置光鸭整理归档目标")
        target = self.client.file_info(target_id)
        if target is None:
            raise DirectoryScrapeStateError("光鸭整理归档目标不存在")
        if not target.is_dir:
            raise DirectoryScrapeStateError("光鸭整理归档目标不是目录")

        parent_id = str(source.parent_id or "0")
        self._validate_target_outside_source(parent_id, target_id)

        resolved_source = self._enrich_identity(source)
        parsed = self._parse_for_rules(resolved_source.name, rules)
        videos = [self._snapshot(resolved_source, "video", "", parsed)]
        metadata_exts = self.organizer.metadata_exts(rules)
        subtitle_candidates: list[GuangYaFile] = []
        for item in self.client.list_dir(parent_id):
            if item.is_dir:
                continue
            item_ext = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            if item_ext not in metadata_exts:
                continue
            resolved = self._enrich_identity(item)
            if media_role(resolved.name) == "subtitle":
                subtitle_candidates.append(resolved)

        subtitle_result = plan_subtitle_companions(
            [resolved_source],
            subtitle_candidates,
        )
        companions = [
            self._snapshot(item.file, "subtitle", "", {})
            for item in subtitle_result.plans
        ]
        companions.sort(key=lambda item: (item.name.casefold(), item.file_id))
        media_type = (
            "tv"
            if parsed.get("type") == "tv"
            or parsed.get("season") is not None
            or parsed.get("episode") is not None
            else "movie"
        )
        season = parsed.get("season")
        episode = parsed.get("episode")
        season_inferred = bool(
            media_type == "tv" and episode is not None and season is None
        )
        if season_inferred:
            season = 1
        suggested_query = str(parsed.get("title") or "").strip() or resolved_source.name
        return DirectoryInspection(
            directory_id=parent_id,
            directory_name=resolved_source.name,
            media_type=media_type,
            suggested_query=suggested_query,
            videos=tuple(videos),
            companions=tuple(companions),
            counts={
                "video": 1,
                "subtitle": len(companions),
                "metadata": 0,
            },
            mixed=False,
            fingerprint=self._fingerprint(videos, companions, rules),
            season=season,
            episode=episode,
            season_inferred=season_inferred,
        )

    def _validate_target_outside_source(self, source_id: str, target_id: str) -> None:
        current_id = target_id
        visited: set[str] = set()
        for _ in range(64):
            if current_id == source_id:
                raise DirectoryScrapeStateError("归档目标位于所选目录内，不能执行目录刮削")
            if not current_id or current_id == "0" or current_id in visited:
                return
            visited.add(current_id)
            current = self.client.file_info(current_id)
            if current is None:
                raise DirectoryScrapeStateError("光鸭整理归档目标不存在")
            if not current.is_dir:
                raise DirectoryScrapeStateError("光鸭整理归档目标不是目录")
            current_id = str(current.parent_id or "0")
        raise DirectoryScrapeStateError("归档目标目录层级异常")

    def _parse(self, filename: str) -> dict[str, object]:
        try:
            parsed = self.scraper.parse_media(filename)
            return {
                "title": parsed.title,
                "year": parsed.year,
                "type": parsed.media_type,
                "tmdb_id": parsed.tmdb_id,
                "season": self._optional_season(parsed.effective_season),
                "episode": self._optional_episode(parsed.effective_episode),
            }
        except (AttributeError, TypeError, ValueError):
            return {}

    @staticmethod
    def _nsfw_code(value: str, rules: OrganizeRules) -> str:
        if not rules.nsfw_exclusive:
            return ""
        from app.modules.nsfw import extract_nsfw_identifier, normalize_code

        identifier = extract_nsfw_identifier(value, rules.nsfw_strip_domains)
        return normalize_code(identifier.code) if identifier is not None else ""

    def _parse_for_rules(
        self, filename: str, rules: OrganizeRules,
    ) -> dict[str, object]:
        if not rules.nsfw_exclusive:
            return self._parse(filename)

        # 成人专用来源不进入普通影视季集解析。番号末尾的数字（如 675）
        # 否则会被 guessit/通用解析器误判为 S06E75，并污染单文件检查。
        code = self._nsfw_code(filename, rules)
        if code:
            title = code
        else:
            from app.modules.nsfw import clean_nsfw_release_text

            source = str(filename or "").strip()
            stem, separator, extension = source.rpartition(".")
            if separator and extension.casefold() in self.organizer.video_exts(rules):
                source = stem
            title = clean_nsfw_release_text(
                source, rules.nsfw_strip_domains,
            ).strip(" ._@-[]()【】")
            if not title:
                title = str(self._parse(filename).get("title") or "").strip()
        return {
            "title": title,
            "year": "",
            "type": "movie",
            "tmdb_id": "",
            "season": None,
            "episode": None,
        }

    def _enrich_identity(self, item: GuangYaFile) -> GuangYaFile:
        if item.etag:
            return item
        try:
            detail = self.client.file_info(item.file_id)
        except Exception:
            detail = None
        return detail if detail is not None else item

    @classmethod
    def _assign_special_episodes(
        cls,
        videos: list[MediaSnapshot],
    ) -> list[MediaSnapshot]:
        special_indices = [
            index for index, item in enumerate(videos)
            if item.season == 0 or is_special_path(item.relative_dir)
        ]
        if not special_indices:
            return videos
        ordered = sorted(
            special_indices,
            key=lambda index: (
                videos[index].relative_dir.casefold(),
                videos[index].name.casefold(),
                videos[index].file_id,
            ),
        )
        used: set[int] = set()
        next_episode = 1
        for index in ordered:
            item = videos[index]
            desired = item.episode
            if desired is None or desired <= 0 or desired in used:
                while next_episode in used:
                    next_episode += 1
                desired = next_episode
            used.add(int(desired))
            next_episode = max(next_episode, int(desired) + 1)
            videos[index] = replace(item, season=0, episode=int(desired))
        return videos

    @classmethod
    def _select_primary_media_group(
        cls,
        identities: list[_VideoIdentity],
    ) -> tuple[list[MediaSnapshot], list[PendingMedia]]:
        """选择唯一明确的主媒体组，并隔离极少量低信息异常视频。

        明确可识别的第二部媒体仍然拒绝；只有类似 ``x.mp4``、``sample.mp4``
        这种无法建立稳定标题身份的文件，才允许留在原目录等待人工处理。
        """
        special = [item for item in identities if item.special]
        special_files = [item.file for item in special]
        regular = [item for item in identities if not item.special]
        if not regular:
            return special_files, []

        strong: list[_VideoIdentity] = []
        weak: list[_VideoIdentity] = []
        for item in regular:
            if cls._weak_identity_title(item.title):
                weak.append(item)
            else:
                strong.append(item)

        if not strong:
            # 全是 01.mkv / E01.mkv 这类弱文件名时，必须依赖目录边界保护。
            # 多季目录（Season 01/Season 02）仍属于同一媒体；Show A/Show B
            # 则不能被一次人工选择全部绑定到同一 TMDB 条目。
            parent_identities = {
                cls._weak_parent_identity(item.file.relative_dir)
                for item in regular
            }
            parent_identities.discard("")
            if len(parent_identities) > 1:
                raise DirectoryScrapeRequestError(
                    "目录包含多个低信息媒体子目录，请进入具体作品目录分别刮削"
                )
            return [item.file for item in regular] + special_files, []

        groups: list[list[_VideoIdentity]] = []
        for item in strong:
            matched = next(
                (
                    group
                    for group in groups
                    if cls._same_title_identity(
                        item.title,
                        group[0].title,
                        "tv" if "tv" in {item.media_type, group[0].media_type} else "movie",
                    )
                ),
                None,
            )
            if matched is None:
                groups.append([item])
            else:
                matched.append(item)

        pending_reasons: dict[str, str] = {}
        if len(groups) != 1:
            # 某些发布包会混入一个没有任何季集号的同名视频。它不能安全
            # 参与自动命名，但也不应拖垮其余明确剧集。仅当主组已有至少
            # 4 个明确集号、异常组全部无季集号且标题严格对齐时，才把
            # 异常文件降级为待确认；真正的第二部媒体仍保持整目录拒绝。
            primary_group = max(
                groups,
                key=lambda group: (
                    sum(item.file.episode is not None for item in group),
                    len(group),
                ),
            )
            secondary = [
                item
                for group in groups
                if group is not primary_group
                for item in group
            ]
            positioned = sum(item.file.episode is not None for item in primary_group)
            allowed_pending = min(2, max(1, (len(primary_group) + 4) // 5))
            primary_tmdb_ids = {item.tmdb_id for item in primary_group if item.tmdb_id}
            primary_years = {item.year for item in primary_group if item.year}
            same_title_unpositioned = all(
                item.file.season is None
                and item.file.episode is None
                and (not item.tmdb_id or not primary_tmdb_ids or item.tmdb_id in primary_tmdb_ids)
                and (not item.year or not primary_years or item.year in primary_years)
                and cls._unpositioned_title_matches(item.title, primary_group[0].title)
                for item in secondary
            )
            # 主剧已有足够强的连续集证据时，允许把“唯一的小离群组”留在
            # 源目录待确认。即使它有明确标题/集号，也绝不绑定到当前候选；
            # 两个完整剧集组、多个离群组或比例接近时仍失败关闭。
            isolated_outlier = bool(
                positioned >= 4
                and len(secondary) == 1
                and len(primary_group) >= 4 * len(secondary)
                and len(groups) == 2
            )
            can_defer = (
                positioned >= 4
                and len(secondary) <= allowed_pending
                and (same_title_unpositioned or isolated_outlier)
            )
            if not can_defer:
                raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")
            primary = primary_group
            weak.extend(secondary)
            pending_reasons.update({
                item.file.file_id: (
                    "标题与主剧一致但未识别到集号，已保留在源目录，请手动指定集号"
                    if same_title_unpositioned
                    else "检测到与主剧不同的单个媒体，已隔离在源目录等待人工识别"
                )
                for item in secondary
            })
        else:
            primary = groups[0]

        # 同一清洗标题下可能混入没有任何季集号的媒体文件。只要主组已经
        # 提供足够多的明确集号，就把这些无位置文件降级为待确认，而不是让
        # 它们进入执行计划并退化成电影式文件名。电影/单视频目录不会命中。
        positioned_primary = [item for item in primary if item.file.episode is not None]
        if len(positioned_primary) >= 4:
            unpositioned_primary = [
                item
                for item in primary
                if item.file.season is None and item.file.episode is None
            ]
            if unpositioned_primary:
                primary = [item for item in primary if item not in unpositioned_primary]
                weak.extend(unpositioned_primary)
                pending_reasons.update({
                    item.file.file_id: "标题与主剧一致但未识别到集号，已保留在源目录，请手动指定集号"
                    for item in unpositioned_primary
                })

        all_weak = list(weak)
        positional_weak = [
            item
            for item in all_weak
            if item.file.episode is not None and cls._numeric_episode_filename(item.file.name)
        ]
        weak = [item for item in all_weak if item not in positional_weak]
        primary.extend(positional_weak)
        tmdb_ids = {item.tmdb_id for item in primary if item.tmdb_id}
        years = {item.year for item in primary if item.year}
        if len(tmdb_ids) > 1 or len(years) > 1:
            raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")

        primary_title = primary[0].title
        for item in special:
            special_title = cls._special_identity_title(item.title)
            if item.tmdb_id and tmdb_ids and item.tmdb_id not in tmdb_ids:
                raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")
            if item.year and years and item.year not in years:
                raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")
            if (
                not item.special_container
                and special_title
                and not cls._weak_identity_title(special_title)
                and not cls._same_title_identity(special_title, primary_title, "tv")
            ):
                raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")

        if not weak:
            return [item.file for item in primary] + special_files, []

        primary_count = len(primary)
        total_regular = primary_count + len(weak)
        allowed_pending = min(2, max(1, (primary_count + 4) // 5))
        if (
            primary_count < 3
            or primary_count / total_regular < 0.8
            or len(weak) > allowed_pending
        ):
            raise DirectoryScrapeRequestError("目录包含多个不同媒体，请进入子目录分别刮削")

        pending = [
            PendingMedia(
                file=item.file,
                reason=pending_reasons.get(
                    item.file.file_id,
                    "无法从文件名确认媒体归属，已保留在源目录",
                ),
            )
            for item in weak
        ]
        return [item.file for item in primary] + special_files, pending

    @classmethod
    def _ambiguous_special_source(cls, value: str) -> bool:
        """Specials-only 目录只有在作品目录名明确时才允许自动匹配。"""
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not text or cls._weak_identity_title(text) or is_special_directory_name(text):
            return True
        key = cls._identity_key(text)
        return key in {
            "1", "root", "download", "downloads", "media", "video", "videos",
            "anime", "tv", "show", "shows", "movie", "movies", "extra", "extras",
            "下载", "下載", "媒体", "媒體", "视频", "視頻", "动漫", "動漫",
            "剧集", "劇集", "电影", "電影", "整理", "待整理",
        }

    @classmethod
    def _weak_parent_identity(cls, relative_dir: str) -> str:
        meaningful: list[str] = []
        for part in re.split(r"[/\\]+", str(relative_dir or "")):
            text = part.strip()
            if not text or is_special_directory_name(text):
                continue
            if re.fullmatch(
                r"(?i)(?:season[ ._-]*\d{1,2}|s\d{1,2}|"
                r"第[零〇一二两三四五六七八九十百0-9]{1,4}季|"
                r"disc[ ._-]*\d+|cd[ ._-]*\d+|"
                r"2160p|1080p|720p|480p|extras?)",
                text,
            ):
                continue
            meaningful.append(cls._identity_key(text))
        return "/".join(item for item in meaningful if item)

    @classmethod
    def _weak_identity_title(cls, value: str) -> bool:
        key = cls._identity_key(value)
        if not key:
            return True
        if key in {
            "x", "xx", "xxx", "test", "demo", "sample", "samples",
            "video", "movie", "episode", "ep", "clip", "unknown",
        }:
            return True
        if re.search(r"[\u4e00-\u9fff]", key):
            return len(key) < 2
        return len(key) <= 3

    @classmethod
    def _unpositioned_title_matches(cls, candidate: str, anchor: str) -> bool:
        """保守判断无集号文件是否只是主剧名前多了发布组。"""
        candidate_key = cls._identity_key(candidate)
        anchor_key = cls._identity_key(anchor)
        if not candidate_key or not anchor_key:
            return False
        if candidate_key == anchor_key:
            return True
        if len(anchor_key) < 8:
            return False
        if candidate_key.endswith(anchor_key):
            prefix_length = len(candidate_key) - len(anchor_key)
            return 0 < prefix_length <= 32
        return False

    @staticmethod
    def _numeric_episode_filename(name: str) -> bool:
        stem = str(name or "").rsplit(".", 1)[0].strip()
        return bool(re.fullmatch(r"\d{1,3}", stem))

    def _safe_companions_for_videos(
        self,
        videos: list[MediaSnapshot],
        companions: list[MediaSnapshot],
    ) -> list[MediaSnapshot]:
        """部分刮削时仅保留能唯一归属主媒体视频的伴随文件。"""
        allowed: set[str] = set()
        videos_by_dir: dict[str, list[MediaSnapshot]] = {}
        companions_by_dir: dict[str, list[MediaSnapshot]] = {}
        for video in videos:
            videos_by_dir.setdefault(video.relative_dir, []).append(video)
        for companion in companions:
            companions_by_dir.setdefault(companion.relative_dir, []).append(companion)

        for relative_dir, directory_companions in companions_by_dir.items():
            directory_videos = videos_by_dir.get(relative_dir, [])
            subtitles = [item for item in directory_companions if item.role == "subtitle"]
            subtitle_result = plan_subtitle_companions(directory_videos, subtitles)
            allowed.update(str(plan.file.file_id) for plan in subtitle_result.plans)

            non_subtitles = [item for item in directory_companions if item.role != "subtitle"]
            for companion in non_subtitles:
                companion_stem = normalized_stem(companion.name)
                if any(
                    companion_stem == normalized_stem(video.name)
                    or companion_stem.startswith(normalized_stem(video.name))
                    for video in directory_videos
                ):
                    allowed.add(companion.file_id)

        return [item for item in companions if item.file_id in allowed]

    @staticmethod
    def _special_identity_title(value: str) -> str:
        cleaned = re.sub(
            r"(?i)(?:^|[ ._\-\[\(【])(?:(?:ncop|nced)(?:[ ._-]*\d{1,3})?|"
            r"omnibus|picture[ ._-]*drama|pvs?[ ._-]*cms?|ova|oad|specials?|sps?|featurettes?)"
            r"(?=$|[ ._\-\]\)】])",
            " ",
            str(value or ""),
        )
        return re.sub(r"[\s._-]+", " ", cleaned).strip()

    @staticmethod
    def _identity_key(value) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        normalized = re.sub(
            r"\(\s*(?:仅限|僅限|限)[^()]{1,24}(?:\)|$)",
            " ",
            normalized,
        )
        return re.sub(
            r"[^a-z0-9\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff"
            r"\uac00-\ud7af]+",
            "",
            normalized,
        )

    @classmethod
    def _identity_pinyin_key(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        try:
            from pypinyin import Style, lazy_pinyin
            normalized = "".join(lazy_pinyin(normalized, style=Style.NORMAL))
        except Exception:
            pass
        return re.sub(r"[^a-z0-9]+", "", normalized.casefold())

    @classmethod
    def _same_title_identity(cls, left: str, right: str, media_type: str) -> bool:
        """剧集目录允许繁简体及单侧副标题差异，电影仍保持严格判定。"""
        if cls._identity_key(left) == cls._identity_key(right):
            return True
        if media_type != "tv":
            return False
        if cls._identity_pinyin_key(left) == cls._identity_pinyin_key(right):
            return True

        left_normalized = unicodedata.normalize("NFKC", str(left or ""))
        right_normalized = unicodedata.normalize("NFKC", str(right or ""))
        qualifier = re.compile(r"\([^()]{1,32}\)")
        left_has_qualifier = bool(qualifier.search(left_normalized))
        right_has_qualifier = bool(qualifier.search(right_normalized))
        if left_has_qualifier == right_has_qualifier:
            return False
        left_base = qualifier.sub(" ", left_normalized)
        right_base = qualifier.sub(" ", right_normalized)
        return cls._identity_pinyin_key(left_base) == cls._identity_pinyin_key(right_base)

    @staticmethod
    def _snapshot(
        item: GuangYaFile,
        role: str,
        relative_dir: str,
        parsed: dict,
    ) -> MediaSnapshot:
        return MediaSnapshot(
            file_id=str(item.file_id),
            parent_id=str(item.parent_id or "0"),
            name=str(item.name),
            size=int(item.size or 0),
            etag=str(item.etag or ""),
            role=role,
            relative_dir=relative_dir,
            season=DirectoryMediaInspector._optional_season(parsed.get("season")),
            episode=DirectoryMediaInspector._optional_episode(parsed.get("episode")),
        )

    @staticmethod
    def _directory_snapshot(item: GuangYaFile, relative_dir: str) -> DirectorySnapshot:
        return DirectorySnapshot(
            file_id=str(item.file_id),
            parent_id=str(item.parent_id or "0"),
            name=str(item.name),
            etag=str(item.etag or ""),
            relative_dir=str(relative_dir or ""),
        )

    @staticmethod
    def _optional_position(value, *, minimum: int, maximum: int) -> int | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if minimum <= number <= maximum else None

    @classmethod
    def _optional_season(cls, value) -> int | None:
        return cls._optional_position(value, minimum=0, maximum=99)

    @classmethod
    def _optional_episode(cls, value) -> int | None:
        # 解析阶段保留 E00，预览阶段会把它规范为 Specials/S00E01。
        return cls._optional_position(value, minimum=0, maximum=999)

    @staticmethod
    def _fingerprint(
        videos: list[MediaSnapshot],
        companions: list[MediaSnapshot],
        rules: OrganizeRules,
        *,
        pending_videos: list[PendingMedia] | tuple[PendingMedia, ...] = (),
        directories: list[DirectorySnapshot] | tuple[DirectorySnapshot, ...] = (),
    ) -> str:
        payload = {
            "files": [
                asdict(item)
                for item in sorted(
                    [*videos, *companions],
                    key=lambda row: (row.file_id, row.parent_id, row.name),
                )
            ],
            "pending": [
                {
                    "file": asdict(item.file),
                    "reason": item.reason,
                }
                for item in sorted(
                    pending_videos,
                    key=lambda row: (
                        row.file.file_id,
                        row.file.parent_id,
                        row.file.name,
                    ),
                )
            ],
            "directories": [
                asdict(item)
                for item in sorted(
                    directories,
                    key=lambda row: (row.file_id, row.parent_id, row.name),
                )
            ],
            "rules": organize_rules_snapshot(rules),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
