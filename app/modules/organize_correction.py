"""整理日志纠偏服务。

所有写操作都要求完整媒体组快照、操作令牌和版本校验，并由
OrganizeTaskManager 的统一写锁异步执行。历史或损坏快照只读展示。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.logger import get_logger
from app.modules.organize import OrganizeRules, Organizer, enforce_fixed_organize_rules
from app.modules.organize_delete_audit import (
    DeleteCandidate, execute_recycle_bin_delete, record_blocked_delete,
)
from app.modules.organize_postprocess import companion_target_name, normalize_media_number
from app.modules.scraper import Candidate, MatchResult, TMDBScraper

logger = get_logger(__name__)

BUSY_STATUSES = {"reorganizing", "returning", "reverting", "deleting"}
REORGANIZE_STATUSES = {"success", "failed", "skipped", "reverted", "interrupted"}
RETURN_STATUSES = {"success", "failed", "reverted", "interrupted"}
REVERT_STATUSES = {"success"}
DELETE_STATUSES = {"success", "failed", "skipped", "reverted", "interrupted"}


@dataclass
class CorrectionItem:
    id: int
    file_id: str
    role: str
    original_parent_id: str
    original_name: str
    current_parent_id: str
    current_name: str
    size: int = 0
    etag: str = ""


@dataclass
class AppliedTransition:
    item: CorrectionItem
    before_parent_id: str
    before_name: str
    after_parent_id: str
    after_name: str
    step_id: int


class OrganizeCorrectionService:
    def __init__(self, client: GuangYaClient | None = None,
                 scraper: TMDBScraper | None = None):
        self.client = client or GuangYaClient()
        self.scraper = scraper or TMDBScraper()
        self.organizer = Organizer(client=self.client, scraper=self.scraper)

    @staticmethod
    def _row_dict(row) -> dict:
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _decode_release_parse(raw: object) -> dict | None:
        if not raw:
            return None
        if isinstance(raw, dict):
            return dict(raw)
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _snapshot_complete(data: dict, items: list[dict]) -> tuple[bool, str]:
        if data.get("legacy_incomplete"):
            return False, "历史日志缺少原文件名或父目录快照，仅允许查看，禁止猜测式回退。"
        if not items:
            return False, "日志没有媒体组成员快照，仅允许查看。"
        videos = [item for item in items if item.get("role") == "video"]
        if len(videos) != 1:
            return False, "媒体组必须且只能包含一个主视频快照，当前记录禁止写操作。"
        for item in items:
            if not all(str(item.get(field) or "").strip() for field in (
                "file_id", "original_parent_id", "original_name",
                "current_parent_id", "current_name",
            )):
                return False, "媒体组存在不完整成员快照，当前记录禁止写操作。"
        return True, ""

    def detail(self, log_id: int) -> dict:
        row = db.get_organize_log(log_id)
        if not row:
            raise LookupError("日志不存在")
        data = self._row_dict(row)
        data["release_parse"] = self._decode_release_parse(
            data.pop("release_parse_json", "")
        )
        items = [self._row_dict(item) for item in db.list_organize_log_items(log_id)]
        steps = [self._row_dict(step) for step in db.list_organize_operation_steps(log_id)]
        complete, safety_notice = self._snapshot_complete(data, items)
        status = str(data.get("status") or "")
        busy = status in BUSY_STATUSES
        data["items"] = items
        data["operations"] = steps
        data["delete_audits"] = [
            self._row_dict(audit)
            for audit in db.list_organize_delete_audits(log_id, limit=300)
        ]
        has_reversible_step = any(
            step.get("status") == "success" and step.get("action") == "move_rename"
            for step in steps
        )
        data["allowed_actions"] = {
            "search": complete,
            "preview": complete and not busy,
            "reorganize": complete and status in REORGANIZE_STATUSES,
            "return_to_source": complete and status in RETURN_STATUSES,
            "revert": complete and has_reversible_step and status in REVERT_STATUSES,
            "delete": complete and status in DELETE_STATUSES,
        }
        if not complete:
            data["safety_notice"] = safety_notice
        elif status == "interrupted":
            data["safety_notice"] = "检测到上次进程中断；执行前会重新核验全部云端快照，不会自动续写。"
        elif status in {"partial_failed", "revert_failed"}:
            data["safety_notice"] = (
                "检测到云端操作或补偿未完整完成。系统已冻结自动写操作；"
                "请根据成员快照和操作步骤人工核对云端现状，禁止猜测式续写。"
            )
        return data

    def validate_batch(self, log_ids: list[int], action: str) -> list[dict]:
        """批量操作写入前统一校验；任何不合格成员都会阻止整个批次。"""
        unique_ids = list(dict.fromkeys(int(log_id) for log_id in log_ids))
        if len(unique_ids) != len(log_ids):
            raise ValueError("批量日志列表包含重复 ID")
        if len(unique_ids) < 2:
            raise ValueError("批量操作至少选择两条整理日志")
        if len(unique_ids) > 50:
            raise ValueError("单次批量操作最多支持 50 条整理日志")
        action_key = {
            "reorganize": "reorganize",
            "revert": "revert",
            "delete": "delete",
        }.get(action)
        if not action_key:
            raise ValueError("不支持的批量操作")
        details = [self.detail(log_id) for log_id in unique_ids]
        movies = [str(item["id"]) for item in details if item.get("media_type") != "tv"]
        if movies:
            raise ValueError(
                "批量操作仅支持剧集，选择中包含电影或未知类型日志: "
                + ", ".join(f"#{item}" for item in movies)
            )
        blocked = [
            str(item["id"]) for item in details
            if not item.get("allowed_actions", {}).get(action_key)
        ]
        if blocked:
            raise ValueError(
                "以下剧集日志当前不允许执行该批量操作: "
                + ", ".join(f"#{item}" for item in blocked)
            )
        if action == "reorganize":
            missing = [str(item["id"]) for item in details if not item.get("tmdb_id")]
            if missing:
                raise ValueError(
                    "批量改名要求每条剧集日志已有 TMDB 映射，缺失日志: "
                    + ", ".join(f"#{item}" for item in missing)
                )
        return details

    def run_batch(self, action: str, entries: list[dict], confirm_text: str = "") -> dict:
        """批量删除先全量预检并原子认领；其他操作保持逐条执行。"""
        details = self.validate_batch([entry["log_id"] for entry in entries], action)
        detail_map = {int(item["id"]): item for item in details}
        completed: list[dict] = []
        failed: list[dict] = []
        warnings: list[str] = []

        prepared_deletes: dict[int, tuple[list[CorrectionItem], list[CorrectionItem]]] = {}
        if action == "delete":
            if confirm_text != "DELETE":
                raise ValueError("请输入 DELETE 确认批量移入光鸭回收站")
            for entry in entries:
                log_id = int(entry["log_id"])
                try:
                    prepared_deletes[log_id] = self._prepare_delete_group(log_id, confirm_text)
                except Exception as exc:
                    logger.error(f"剧集批量删除预检失败 log={log_id}: {exc}")
                    return {
                        "success": False, "action": action, "requested": len(entries),
                        "completed": [], "failed": [{"log_id": log_id, "error": str(exc)}],
                        "warnings": [],
                    }
            if not db.claim_organize_log_operations_batch(
                entries, "deleting", tuple(DELETE_STATUSES)
            ):
                return {
                    "success": False, "action": action, "requested": len(entries),
                    "completed": [],
                    "failed": [{"log_id": int(entry["log_id"]),
                                "error": "日志状态或版本已变化，请刷新后重试"}
                               for entry in entries],
                    "warnings": [],
                }

        for entry in entries:
            log_id = int(entry["log_id"])
            token = str(entry["operation_token"])
            version = int(entry["expected_version"])
            detail = detail_map[log_id]
            try:
                if action == "reorganize":
                    result = self.reorganize(
                        log_id, token, version,
                        str(detail.get("tmdb_id") or ""), "tv",
                    )
                elif action == "revert":
                    result = self.revert_latest(log_id, token, version)
                else:
                    items, ordered = prepared_deletes[log_id]
                    result = self._execute_claimed_delete(log_id, token, items, ordered)
                completed.append({"log_id": log_id, "result": result})
                warnings.extend(result.get("warnings") or [])
            except Exception as exc:
                logger.error(f"剧集批量操作失败 action={action} log={log_id}: {exc}")
                failed.append({"log_id": log_id, "error": str(exc)})
        return {
            "success": not failed,
            "action": action,
            "requested": len(entries),
            "completed": completed,
            "failed": failed,
            "warnings": warnings,
        }

    def search_tmdb(self, log_id: int, query: str = "", year: str = "",
                    media_type: str = "") -> list[dict]:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["search"]:
            raise ValueError(detail.get("safety_notice") or "该日志不能人工纠偏")
        query = (query or detail.get("title") or detail.get("original_name") or "").strip()
        media_type = "tv" if media_type == "tv" else (
            "movie" if media_type == "movie" else detail.get("media_type") or "movie"
        )
        candidates = self.scraper.search_candidates(query, year, media_type)
        return [self._candidate_dict(item) for item in candidates]

    @staticmethod
    def _candidate_dict(item: Candidate) -> dict:
        return {
            "tmdb_id": item.tmdb_id,
            "title": item.title,
            "year": item.year,
            "score": item.score,
            "media_type": item.media_type,
        }

    @staticmethod
    def _to_item(row) -> CorrectionItem:
        return CorrectionItem(
            id=int(row["id"]), file_id=str(row["file_id"] or ""),
            role=str(row["role"] or "metadata"),
            original_parent_id=str(row["original_parent_id"] or ""),
            original_name=str(row["original_name"] or ""),
            current_parent_id=str(row["current_parent_id"] or ""),
            current_name=str(row["current_name"] or ""),
            size=int(row["size"] or 0), etag=str(row["etag"] or ""),
        )

    def _load_items(self, log_id: int) -> list[CorrectionItem]:
        return [self._to_item(row) for row in db.list_organize_log_items(log_id)]

    @staticmethod
    def _video(items: list[CorrectionItem]) -> CorrectionItem:
        videos = [item for item in items if item.role == "video"]
        if len(videos) != 1:
            raise ValueError("日志必须且只能包含一个主视频快照")
        return videos[0]

    def _match(self, tmdb_id: str, media_type: str, title: str = "",
               year: str = "") -> MatchResult:
        """客户端候选只用于选择 ID，标题、年份和类型以 TMDB 详情为准。"""
        media_type = "tv" if media_type == "tv" else "movie"
        result = self.scraper.match_from_tmdb(str(tmdb_id), media_type)
        if not result.tmdb_id:
            raise ValueError(result.error or "TMDB 详情不存在")
        return result

    def preview_reorganize(
        self, log_id: int, tmdb_id: str, media_type: str,
        title: str = "", year: str = "",
        season: int | None = None, episode: int | None = None,
    ) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["preview"]:
            raise ValueError(detail.get("safety_notice") or "当前状态不能重新整理")
        items = self._load_items(log_id)
        video = self._video(items)
        match = self._match(tmdb_id, media_type, title, year)
        rules = OrganizeRules.from_config()
        source_name = video.original_name or video.current_name
        parsed = self.organizer._parse_media_fields(source_name)
        parent_path = str(detail.get("original_path") or "")
        position = None
        try:
            release_parse = self.scraper.parse_media(source_name, parent_path, match)
            position = (
                release_parse.effective_season, release_parse.effective_episode
            )
        except (AttributeError, TypeError, ValueError):
            position = None
        if isinstance(position, (tuple, list)) and len(position) == 2:
            parsed_season = normalize_media_number(position[0])
            parsed_episode = normalize_media_number(position[1])
            if parsed_season is not None:
                parsed["season"] = parsed_season
            if parsed_episode is not None:
                parsed["episode"] = parsed_episode
        if parsed.get("season") is None:
            parsed["season"] = normalize_media_number(detail.get("season"))
        if parsed.get("episode") is None:
            parsed["episode"] = normalize_media_number(detail.get("episode"))
        if match.media_type != "tv" and (season is not None or episode is not None):
            raise ValueError("电影不支持季号或集号覆盖")
        if season is not None:
            parsed["season"] = normalize_media_number(season)
        if episode is not None:
            parsed["episode"] = normalize_media_number(episode)
        if (
            match.media_type == "tv"
            and parsed.get("episode") is not None
            and parsed.get("season") is None
        ):
            parsed["season"] = 1
        if isinstance(self.scraper, TMDBScraper) and match.media_type == "tv":
            tmdb_detail = self.scraper.get_detail(match.tmdb_id, match.media_type)
            validation = self.scraper.validate_position(
                tmdb_detail, match.media_type, parsed.get("season"), parsed.get("episode")
            )
            if validation.get("required") and not validation.get("passed"):
                raise ValueError(self.scraper.position_validation_error(validation))
        parsed["type"] = match.media_type
        current_file = GuangYaFile(
            video.file_id, video.current_name or video.original_name, False,
            video.size, video.etag, video.current_parent_id,
        )
        new_name = self.organizer.build_new_name(match, current_file, parsed, rules)
        main, region, resolved_year = self.organizer.classify(match, rules)
        parts = [main]
        if rules.region_split:
            parts.append(region)
        if rules.year_split and resolved_year:
            parts.append(resolved_year)
        parts.extend(self.organizer.build_media_path_parts(match, parsed, rules))
        target_path = "/".join(parts)
        planned = []
        for item in items:
            target_name = new_name if item.role == "video" else companion_target_name(
                video.current_name or video.original_name, new_name,
                item.current_name or item.original_name,
            )
            planned.append({
                "item_id": item.id, "file_id": item.file_id, "role": item.role,
                "from_parent_id": item.current_parent_id,
                "from_name": item.current_name,
                "to_name": target_name,
            })
        return {
            "log_id": log_id,
            "version": int(detail.get("version") or 1),
            "match": {
                "tmdb_id": match.tmdb_id, "title": match.title, "year": match.year,
                "media_type": match.media_type,
            },
            "target_root_id": rules.target_dir_id,
            "target_path": target_path,
            "media_dir": self.organizer.build_media_dir(match, rules),
            "file_name": new_name,
            "season": parsed.get("season"),
            "episode": parsed.get("episode"),
            "items": planned,
            "rules_snapshot": asdict(rules),
            "cloud_write": False,
        }

    def reorganize(
        self, log_id: int, operation_token: str, expected_version: int,
        tmdb_id: str, media_type: str, title: str = "", year: str = "",
        season: int | None = None, episode: int | None = None,
    ) -> dict:
        preview = self.preview_reorganize(
            log_id, tmdb_id, media_type, title, year, season, episode
        )
        items = self._load_items(log_id)
        self._verify_items(items)
        if not db.claim_organize_log_operation(
            log_id, operation_token, "reorganizing",
            tuple(REORGANIZE_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        try:
            return self._execute_reorganize(log_id, operation_token, preview, items)
        except Exception as exc:
            status = self._failure_status(log_id, "failed")
            db.update_organize_log(log_id, status=status, error=str(exc))
            raise

    def _verify_item(self, item: CorrectionItem) -> GuangYaFile:
        remote = self.client.file_info(item.file_id)
        if not remote:
            raise RuntimeError(f"云端文件不存在或详情不可用: {item.current_name or item.file_id}")
        if not str(remote.file_id or "").strip():
            raise RuntimeError(f"无法读取云端文件详情: {item.current_name or item.file_id}")
        if str(remote.file_id) != str(item.file_id):
            raise RuntimeError(f"云端文件详情不匹配: {item.current_name or item.file_id}")
        if item.current_parent_id and remote.parent_id != item.current_parent_id:
            raise RuntimeError(f"文件位置已被外部修改: {item.current_name}")
        if item.current_name and remote.name != item.current_name:
            raise RuntimeError(f"文件名已被外部修改: {item.current_name} → {remote.name}")
        if item.size and remote.size and item.size != remote.size:
            raise RuntimeError(f"文件大小已变化: {item.current_name}")
        if item.etag and remote.etag and item.etag != remote.etag:
            raise RuntimeError(f"文件校验值已变化: {item.current_name}")
        return remote

    def _verify_items(self, items: list[CorrectionItem]) -> None:
        for item in items:
            self._verify_item(item)


    def _preflight_delete_items(self, items: list[CorrectionItem]) -> list[GuangYaFile]:
        remotes = [self._verify_item(item) for item in items]
        directory_cache: dict[str, list[GuangYaFile]] = {}
        for item, remote in zip(items, remotes):
            parent_id = item.current_parent_id
            if parent_id not in directory_cache:
                directory_cache[parent_id] = self.client.list_dir(parent_id)
            entries = directory_cache[parent_id]
            if not any(entry.file_id == item.file_id for entry in entries):
                raise RuntimeError(f"删除对象扫描缺失: {item.current_name}")
            if not item.etag or int(item.size or 0) <= 0:
                raise RuntimeError(f"GCID/size 歧义，禁止删除: {item.current_name}")
            matches = [
                entry for entry in entries
                if entry.etag == item.etag
                and int(entry.size or 0) == int(item.size or 0)
            ]
            if len(matches) != 1 or matches[0].file_id != item.file_id:
                raise RuntimeError(f"GCID/size 歧义，禁止删除: {item.current_name}")
            if remote.etag != item.etag or int(remote.size or 0) != int(item.size or 0):
                raise RuntimeError(f"GCID/size 与日志快照不一致: {item.current_name}")
        return remotes

    def _ensure_target_dir(self, root_id: str, path: str) -> tuple[str, list[str]]:
        current = str(root_id or "0")
        created: list[str] = []
        for part in (piece for piece in path.split("/") if piece):
            children = self.client.list_dir(current)
            match = next((entry for entry in children if entry.is_dir and entry.name == part), None)
            if match:
                current = match.file_id
                continue
            new_id = self.client.create_dir(part, current)
            if not new_id:
                raise RuntimeError(f"创建目标目录失败: {part}")
            created.append(new_id)
            current = new_id
        return current, created

    def _cleanup_created_dirs(self, created: list[str]) -> None:
        for dir_id in reversed(created):
            try:
                if self.client.list_dir(dir_id):
                    continue
                execute_recycle_bin_delete(
                    self.client,
                    trigger="correction_empty_dir_cleanup",
                    reason="整理纠偏失败补偿：清理本次创建且仍为空的目录",
                    candidate=DeleteCandidate(
                        file_id=str(dir_id),
                        name=f"纠偏临时目录 {dir_id}",
                        parent_id="",
                    ),
                )
            except Exception as exc:
                logger.warning(f"清理本次纠偏创建的空目录失败 {dir_id}: {exc}")

    def _verify_targets_available(self, targets: list[tuple[CorrectionItem, str, str]],
                                  *, allow_group_file_ids: set[str] | None = None) -> None:
        cache: dict[str, list[GuangYaFile]] = {}
        allowed = allow_group_file_ids or set()
        for item, parent_id, name in targets:
            if parent_id not in cache:
                cache[parent_id] = self.client.list_dir(parent_id)
            conflict = next((
                entry for entry in cache[parent_id]
                if not entry.is_dir and entry.name == name
                and entry.file_id != item.file_id and entry.file_id not in allowed
            ), None)
            if conflict:
                raise RuntimeError(
                    f"目标目录存在同名文件，纠偏操作禁止自动覆盖: {name} ({conflict.file_id})"
                )

    def _snapshot_after_rollback_failure(self, item: CorrectionItem, error: Exception) -> None:
        fields = {"status": "rollback_failed", "error": str(error)}
        try:
            remote = self.client.file_info(item.file_id)
            if remote:
                fields.update(
                    current_parent_id=remote.parent_id,
                    current_name=remote.name,
                    size=remote.size,
                    etag=remote.etag,
                )
        except Exception as snapshot_exc:
            fields["error"] = f"{error}; 读取补偿后快照失败: {snapshot_exc}"
        db.update_organize_log_item(item.id, **fields)

    def _rollback_remote(self, item: CorrectionItem, current_parent_id: str,
                         current_name: str, target_parent_id: str,
                         target_name: str) -> None:
        # 先恢复名称再恢复目录，避免目标目录已有同名文件导致移动补偿失败。
        if current_name != target_name:
            self.client.rename(item.file_id, target_name)
            current_name = target_name
        if current_parent_id != target_parent_id:
            self.client.move([item.file_id], target_parent_id)

    def _apply_transition(self, item: CorrectionItem, target_parent_id: str,
                          target_name: str, step_id: int, *,
                          rename_first: bool) -> AppliedTransition:
        current_parent_id = item.current_parent_id
        current_name = item.current_name
        try:
            if rename_first and current_name != target_name:
                self.client.rename(item.file_id, target_name)
                current_name = target_name
            if current_parent_id != target_parent_id:
                self.client.move([item.file_id], target_parent_id)
                current_parent_id = target_parent_id
            if not rename_first and current_name != target_name:
                self.client.rename(item.file_id, target_name)
                current_name = target_name
        except Exception as exc:
            try:
                self._rollback_remote(
                    item, current_parent_id, current_name,
                    item.current_parent_id, item.current_name,
                )
            except Exception as rollback_exc:
                self._snapshot_after_rollback_failure(item, rollback_exc)
                raise RuntimeError(f"{exc}; 当前文件补偿失败: {rollback_exc}") from exc
            raise
        return AppliedTransition(
            item=item,
            before_parent_id=item.current_parent_id,
            before_name=item.current_name,
            after_parent_id=target_parent_id,
            after_name=target_name,
            step_id=step_id,
        )

    def _rollback_transitions(self, transitions: list[AppliedTransition]) -> None:
        for transition in reversed(transitions):
            try:
                self._rollback_remote(
                    transition.item,
                    transition.after_parent_id,
                    transition.after_name,
                    transition.before_parent_id,
                    transition.before_name,
                )
                db.update_organize_log_item(
                    transition.item.id,
                    current_parent_id=transition.before_parent_id,
                    current_name=transition.before_name,
                    status="success", error="",
                )
                db.finish_organize_operation_step(
                    transition.step_id, "rolled_back", "批次后续步骤失败，已自动补偿"
                )
            except Exception as rollback_exc:
                self._snapshot_after_rollback_failure(transition.item, rollback_exc)
                db.finish_organize_operation_step(
                    transition.step_id, "rollback_failed", str(rollback_exc)
                )
                logger.error(f"纠偏回滚失败 {transition.item.file_id}: {rollback_exc}")

    def _failure_status(self, log_id: int, default: str) -> str:
        return "partial_failed" if any(
            row["status"] in {"rollback_failed", "deleted"}
            for row in db.list_organize_log_items(log_id)
        ) else default

    def _run_post_actions(
        self,
        *,
        items: list[CorrectionItem],
        rules: OrganizeRules,
        match: dict | None = None,
        moved: int = 0,
        parent_path: str = "",
        rejected_tmdb_ids: list[str] | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        if match:
            try:
                self.scraper.confirm(
                    self._video(items).original_name,
                    match["tmdb_id"],
                    match["title"],
                    match["year"],
                    match["media_type"],
                    parent_path=parent_path,
                    rejected_tmdb_ids=rejected_tmdb_ids or [],
                )
            except Exception as exc:
                warnings.append(f"TMDB 映射锁保存失败: {exc}")
                logger.warning(warnings[-1])
        if rules.link_strm:
            try:
                Organizer._post_organize_link({"moved": moved}, rules)
            except Exception as exc:
                warnings.append(f"STRM/Jellyfin 后处理失败: {exc}")
                logger.warning(warnings[-1])
        return warnings

    def _notify_reorganize_result(self, preview: dict,
                                  items: list[CorrectionItem]) -> list[str]:
        """人工纠偏提交成功后发送媒体卡片；失败只返回 warning。"""
        warnings: list[str] = []
        try:
            from app.notifier import build_media_events, send_event
            match = preview.get("match") or {}
            video = self._video(items)
            parsed = self.organizer._parse_media_fields(
                video.original_name or video.current_name
            )
            if preview.get("season") is not None:
                parsed["season"] = preview.get("season")
            if preview.get("episode") is not None:
                parsed["episode"] = preview.get("episode")
            media_item = {
                "title": match.get("title"), "year": match.get("year"),
                "media_type": match.get("media_type"), "tmdb_id": match.get("tmdb_id"),
                "season": parsed.get("season"), "episode": parsed.get("episode"),
                "source": "光鸭云盘 · 人工识别", "category": preview.get("target_path"),
                "filename": preview.get("file_name"), "size": video.size,
            }
            events = build_media_events([media_item])
            if not events or not all(send_event(event) for event in events):
                warnings.append("人工识别媒体通知发送失败")
        except Exception as exc:
            warnings.append(f"人工识别媒体通知发送失败: {exc}")
            logger.warning(warnings[-1])
        return warnings

    def _execute_reorganize(self, log_id: int, operation_token: str,
                            preview: dict, items: list[CorrectionItem]) -> dict:
        rules = enforce_fixed_organize_rules(
            OrganizeRules(**preview["rules_snapshot"])
        )
        created_dirs: list[str] = []
        completed: list[AppliedTransition] = []
        try:
            target_id, created_dirs = self._ensure_target_dir(
                preview["target_root_id"], preview["target_path"]
            )
            targets = [
                (item, target_id, planned["to_name"])
                for item, planned in zip(items, preview["items"])
            ]
            self._verify_targets_available(targets)
            for step_index, (item, planned) in enumerate(zip(items, preview["items"]), start=1):
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, step_index, "move_rename",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, to_parent_id=target_id,
                    to_name=planned["to_name"], status="running",
                )
                try:
                    transition = self._apply_transition(
                        item, target_id, planned["to_name"], step_id,
                        rename_first=False,
                    )
                    completed.append(transition)
                    db.update_organize_log_item(
                        item.id, current_parent_id=target_id,
                        current_name=planned["to_name"], target_parent_id=target_id,
                        target_name=planned["to_name"], status="success", error="",
                    )
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception:
            self._rollback_transitions(completed)
            self._cleanup_created_dirs(created_dirs)
            raise
        match = preview["match"]
        previous_log = db.get_organize_log(log_id)
        parent_path = str(previous_log["original_path"] or "") if previous_log else ""
        previous_tmdb_id = str(previous_log["tmdb_id"] or "").strip() if previous_log else ""
        selected_tmdb_id = str(match.get("tmdb_id") or "").strip()
        rejected_tmdb_ids = (
            [previous_tmdb_id]
            if previous_tmdb_id and previous_tmdb_id != selected_tmdb_id
            else []
        )
        db.update_organize_log(
            log_id, status="success", operation_type="reorganize",
            current_parent_id=target_id, current_name=preview["file_name"],
            target_parent_id=target_id,
            new_path=preview["target_path"] + "/" + preview["file_name"],
            tmdb_id=match["tmdb_id"], media_type=match["media_type"],
            title=match["title"], year=match["year"], error="",
        )
        warnings = self._notify_reorganize_result(preview, items)
        warnings.extend(self._run_post_actions(
            items=items,
            rules=rules,
            match=match,
            moved=len(items),
            parent_path=parent_path,
            rejected_tmdb_ids=rejected_tmdb_ids,
        ))
        return {
            "success": True, "log_id": log_id, "target_dir_id": target_id,
            "item_count": len(items), "warnings": warnings,
        }

    def return_to_source(self, log_id: int, operation_token: str,
                         expected_version: int) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["return_to_source"]:
            raise ValueError(detail.get("safety_notice") or "当前状态不能送回源目录")
        items = self._load_items(log_id)
        self._verify_items(items)
        self._verify_targets_available(
            [(item, item.original_parent_id, item.original_name) for item in items],
            allow_group_file_ids={item.file_id for item in items},
        )
        if not db.claim_organize_log_operation(
            log_id, operation_token, "returning", tuple(RETURN_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        completed: list[AppliedTransition] = []
        try:
            for index, item in enumerate(items, start=1):
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, index, "return_to_source",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, to_parent_id=item.original_parent_id,
                    to_name=item.original_name, status="running",
                )
                try:
                    transition = self._apply_transition(
                        item, item.original_parent_id, item.original_name, step_id,
                        rename_first=True,
                    )
                    completed.append(transition)
                    db.update_organize_log_item(
                        item.id, current_parent_id=item.original_parent_id,
                        current_name=item.original_name, status="success", error="",
                    )
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception as exc:
            self._rollback_transitions(completed)
            db.update_organize_log(
                log_id, status=self._failure_status(log_id, "failed"), error=str(exc)
            )
            raise
        video = self._video(items)
        db.update_organize_log(
            log_id, status="reverted", operation_type="return_to_source",
            current_parent_id=video.original_parent_id,
            current_name=video.original_name, error="",
        )
        rules = OrganizeRules.from_config()
        warnings = self._run_post_actions(items=items, rules=rules, moved=len(items))
        return {
            "success": True, "log_id": log_id, "item_count": len(items),
            "warnings": warnings,
        }

    def revert_latest(self, log_id: int, operation_token: str,
                      expected_version: int) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["revert"]:
            raise ValueError("没有可安全回退的上一版操作快照")
        successful = [
            step for step in detail["operations"]
            if step.get("status") == "success" and step.get("action") == "move_rename"
        ]
        if not successful:
            raise ValueError("没有可安全回退的操作步骤")
        previous_token = successful[0]["operation_token"]
        steps = [step for step in successful if step["operation_token"] == previous_token]
        items = self._load_items(log_id)
        items_by_file = {item.file_id: item for item in items}
        targets: list[tuple[CorrectionItem, str, str]] = []
        for previous in steps:
            item = items_by_file.get(str(previous.get("file_id") or ""))
            if not item:
                raise RuntimeError("操作快照中的文件已不属于当前媒体组")
            self._verify_item(item)
            targets.append((
                item, str(previous.get("from_parent_id") or ""),
                str(previous.get("from_name") or ""),
            ))
        self._verify_targets_available(
            targets, allow_group_file_ids={item.file_id for item in items}
        )
        if not db.claim_organize_log_operation(
            log_id, operation_token, "reverting", tuple(REVERT_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        completed: list[AppliedTransition] = []
        try:
            for index, previous in enumerate(reversed(steps), start=1):
                item = items_by_file[str(previous.get("file_id") or "")]
                target_parent = str(previous.get("from_parent_id") or "")
                target_name = str(previous.get("from_name") or "")
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, index, "revert",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, to_parent_id=target_parent,
                    to_name=target_name, status="running",
                )
                try:
                    transition = self._apply_transition(
                        item, target_parent, target_name, step_id, rename_first=True,
                    )
                    completed.append(transition)
                    db.update_organize_log_item(
                        item.id, current_parent_id=target_parent,
                        current_name=target_name, status="success", error="",
                    )
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception as exc:
            self._rollback_transitions(completed)
            db.update_organize_log(
                log_id, status=self._failure_status(log_id, "revert_failed"), error=str(exc)
            )
            raise
        refreshed_items = self._load_items(log_id)
        video = self._video(refreshed_items)
        db.update_organize_log(
            log_id, status="reverted", operation_type="revert",
            current_parent_id=video.current_parent_id,
            current_name=video.current_name, error="",
        )
        rules = OrganizeRules.from_config()
        warnings = self._run_post_actions(
            items=refreshed_items, rules=rules, moved=len(completed)
        )
        return {
            "success": True, "log_id": log_id, "item_count": len(completed),
            "warnings": warnings,
        }

    def _prepare_delete_group(
        self, log_id: int, confirm_text: str,
    ) -> tuple[list[CorrectionItem], list[CorrectionItem]]:
        detail = self.detail(log_id)
        if confirm_text != "DELETE":
            raise ValueError("请输入 DELETE 确认移入光鸭回收站")
        if not detail["allowed_actions"]["delete"]:
            raise ValueError(detail.get("safety_notice") or "当前状态不能删除")
        items = self._load_items(log_id)
        ordered = sorted(items, key=lambda item: item.role == "video")
        try:
            self._preflight_delete_items(ordered)
        except Exception as exc:
            for item in ordered:
                record_blocked_delete(
                    trigger="manual", reason=str(exc),
                    candidate=DeleteCandidate(
                        item.file_id, item.current_name, item.current_parent_id,
                        item.size, item.etag,
                    ),
                    organize_log_id=log_id,
                )
            raise
        return items, ordered

    def _execute_claimed_delete(
        self, log_id: int, operation_token: str,
        items: list[CorrectionItem], ordered: list[CorrectionItem],
    ) -> dict:
        deleted = 0
        try:
            for index, item in enumerate(ordered, start=1):
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, index, "delete",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, status="running",
                )
                try:
                    execute_recycle_bin_delete(
                        self.client, trigger="manual",
                        reason="用户在整理日志中输入 DELETE 显式确认",
                        candidate=DeleteCandidate(
                            item.file_id, item.current_name, item.current_parent_id,
                            item.size, item.etag,
                        ),
                        organize_log_id=log_id,
                    )
                    deleted += 1
                    db.update_organize_log_item(item.id, status="deleted", error="")
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception as exc:
            db.update_organize_log(
                log_id, status="partial_failed" if deleted else "failed", error=str(exc)
            )
            raise
        db.update_organize_log(
            log_id, status="deleted", operation_type="delete", error=""
        )
        rules = OrganizeRules.from_config()
        warnings = self._run_post_actions(items=items, rules=rules, moved=deleted)
        return {
            "success": True, "log_id": log_id, "deleted": deleted,
            "warnings": warnings,
        }

    def delete_group(self, log_id: int, operation_token: str,
                     expected_version: int, confirm_text: str) -> dict:
        items, ordered = self._prepare_delete_group(log_id, confirm_text)
        if not db.claim_organize_log_operation(
            log_id, operation_token, "deleting", tuple(DELETE_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        return self._execute_claimed_delete(log_id, operation_token, items, ordered)



def new_operation_token() -> str:
    return uuid.uuid4().hex
