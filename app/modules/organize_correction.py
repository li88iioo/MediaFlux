"""整理日志纠偏服务。

所有写操作都要求完整媒体组快照、操作令牌和版本校验，并由
OrganizeTaskManager 的统一写锁异步执行。历史或损坏快照只读展示。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from app import database as db
from app.clients.guangya import (
    GuangYaClient,
    GuangYaFile,
    GuangYaReadRejected,
    close_guangya_client,
)
from app.logger import get_logger
from app.modules.guangya_compensation import restore_guangya_file
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.nsfw import (
    CleanTitleCandidate,
    build_clean_title_candidate,
    normalize_code,
)
from app.modules.organize import (
    OrganizePlan,
    Organizer,
    OrganizeRules,
    organize_rules_snapshot,
    organize_rules_snapshot_changes,
    restore_organize_rules_snapshot,
)
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
    record_blocked_delete,
)
from app.modules.organize_identity import _usable_filename_identity_hint
from app.modules.organize_postprocess import (
    companion_target_name,
    normalize_media_number,
)
from app.modules.recognition.cleaner import _comparison_key
from app.modules.scraper import (
    Candidate,
    MatchResult,
    TMDBScraper,
    extract_recognition_context,
)
from app.modules.strm import cleanup_deleted_cloud_files

logger = get_logger(__name__)

BUSY_STATUSES = {"reorganizing", "returning", "reverting", "deleting"}
REORGANIZE_STATUSES = {"success", "failed", "skipped", "manual", "reverted", "interrupted"}
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
        self._owns_client = client is None
        self._owns_scraper = scraper is None
        self.client = client if client is not None else GuangYaClient()
        self.scraper = scraper if scraper is not None else TMDBScraper()
        self.organizer = Organizer(client=self.client, scraper=self.scraper)
        self._closed = False

    def close(self) -> None:
        """释放纠偏服务内部创建的客户端，保留注入依赖所有权。"""
        if self._closed:
            return
        self._closed = True
        self.organizer.close()
        if self._owns_scraper:
            close = getattr(self.scraper, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning(
                        "关闭整理纠偏 TMDB Scraper 失败 type=%s",
                        type(exc).__name__,
                    )
        if self._owns_client:
            close_guangya_client(self.client)

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
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _confirmed_position(release_parse: object) -> dict | None:
        if not isinstance(release_parse, dict):
            return None
        position = release_parse.get("manual_position")
        if not isinstance(position, dict) or position.get("source") != "manual_correction" or position.get("version") != 1:
            return None
        season, episode = position.get("season"), position.get("episode")
        if isinstance(season, bool) or not isinstance(season, int) or not 0 <= season <= 999:
            return None
        if episode is not None and (isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 999):
            return None
        return dict(position)

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

    def _rules_for_log(self, data: dict) -> OrganizeRules:
        """已落库的成人身份不随来源失效而退回 TMDB；未识别日志仍按来源判断。"""
        rules = OrganizeRules.from_config()
        if str(data.get("provider") or "").strip().lower() in {"metatube", "clean_title"}:
            # 仅约束这条历史日志的人工纠偏，不改变全局自动扫描开关。
            return replace(rules, nsfw_enabled=True, nsfw_exclusive=True)
        return rules.for_source(str(data.get("source_dir_id") or ""), client=self.client)

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
        data["related_logs"] = [dict(row) for row in db.list_related_organize_logs(log_id)]
        data["operations"] = steps
        data["delete_audits"] = [
            self._row_dict(audit)
            for audit in db.list_organize_delete_audits(log_id, limit=300)
        ]
        has_reversible_step = any(
            step.get("status") == "success" and step.get("action") == "move_rename"
            for step in steps
        ) or (complete and status in REVERT_STATUSES and bool(db.list_latest_reversible_organize_steps(log_id)))
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
        rules = self._rules_for_log(data)
        nsfw_only = rules.nsfw_exclusive
        data["recognition"] = {
            "provider": "metatube" if nsfw_only else "tmdb",
            "label": "MetaTube / 清洗标题" if nsfw_only else "TMDB",
            "nsfw_only": nsfw_only,
            "query_placeholder": (
                "输入番号或包含番号的文件名" if nsfw_only else "输入片名或剧名"
            ),
        }
        return data

    def validate_batch(self, log_ids: list[int], action: str) -> list[dict]:
        """批量操作写入前统一校验；任何不合格成员都会阻止整个批次。"""
        unique_ids = list(dict.fromkeys(log_id for log_id in log_ids))
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
        if movies and action != "reorganize":
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
        return details

    @staticmethod
    def _batch_candidate(candidate: dict | None) -> dict:
        if not isinstance(candidate, dict):
            raise ValueError("请先选择正确作品并预览整批纠正结果")
        if "episode" in candidate:
            raise ValueError("批量纠正必须保留每条记录的集号，不能统一覆盖集号")
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        provider = str(candidate.get("provider") or "tmdb").strip().lower()
        external_id = str(candidate.get("external_id") or tmdb_id).strip()
        media_type = str(candidate.get("media_type") or "").strip()
        if (provider not in {"tmdb", "metatube", "clean_title"}
                or not external_id or media_type not in {"tv", "movie"}):
            raise ValueError("请选择有效的媒体候选")
        if provider == "tmdb":
            if not external_id.isdigit() or int(external_id) <= 0 or (tmdb_id and tmdb_id != external_id):
                raise ValueError("TMDB ID 无效或候选身份不一致")
            tmdb_id = external_id
        season = candidate.get("season")
        if season is not None and (type(season) is not int or not 0 <= season <= 999):
            raise ValueError("季号必须为 0–999 的整数")
        return {
            "tmdb_id": tmdb_id, "media_type": media_type,
            "title": str(candidate.get("title") or "").strip(),
            "year": str(candidate.get("year") or "").strip(),
            "provider": provider, "external_id": external_id, "season": season,
        }

    def preview_batch(self, entries: list[dict], candidate: dict | None) -> dict:
        """对明确候选统一做无写入预检；保留各文件位置，不采用旧身份。"""
        selected = self._batch_candidate(candidate)
        ids = [entry["log_id"] for entry in entries]
        if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 50:
            raise ValueError("请选择 2–50 条不同的整理日志")
        items: list[dict] = []
        title_groups: dict[str, list[int]] = {}
        destinations: dict[tuple[str, str, str], int] = {}
        source_files: dict[str, int] = {}
        directory_cache: dict[str, list[GuangYaFile]] = {}
        errors: dict[int, str] = {}
        for entry in entries:
            log_id = int(entry["log_id"])
            row = {"log_id": log_id, "original_name": ""}
            try:
                detail = self.detail(log_id)
                row["original_name"] = detail.get("original_name") or detail.get("current_name") or ""
                if detail["version"] != entry["expected_version"]:
                    raise ValueError("日志状态或版本已变化，请刷新后重新预览")
                if not detail.get("allowed_actions", {}).get("reorganize"):
                    raise ValueError(detail.get("safety_notice") or "当前日志不允许纠正识别")
                members = self._load_items(log_id)
                video = self._video(members)
                row["original_name"] = video.original_name
                context = extract_recognition_context(video.original_name, str(detail.get("original_path") or ""))
                # 只用原始标题分组，不用可能识别错的旧 title/TMDB ID。
                title = _usable_filename_identity_hint(video.original_name) or context.folder_title
                key = _comparison_key(title).replace(" ", "")
                if key:
                    title_groups.setdefault(key + ":" + (context.filename_year or context.folder_year or ""), []).append(log_id)
                else:
                    # 裸集号没有标题时，仅在同一原始目录中允许人工指定同一作品。
                    title_groups.setdefault("directory:" + video.original_parent_id, []).append(log_id)
                self._verify_items(members)
                preview = self.preview_reorganize(log_id, **selected)
                if not preview["target_root_id"]:
                    raise ValueError("尚未配置归档目标目录")
                target_dir = self.organizer._find_existing_dir_chain(
                    preview["target_root_id"], preview["target_path"], directory_cache,
                )
                if target_dir is not None:
                    self._verify_targets_available([
                        (member, target_dir, planned["to_name"])
                        for member, planned in zip(members, preview["items"])
                    ])
                if preview["match"]["media_type"] == "tv" and preview.get("episode") is None:
                    raise ValueError("无法确定该文件集号，请先在单条详情中修正季集后再批量处理")
                for member in preview["items"]:
                    file_id = str(member["file_id"])
                    target = (str(preview["target_root_id"]), preview["target_path"].casefold(), member["to_name"].casefold())
                    other = source_files.get(file_id) or destinations.get(target)
                    if other is not None:
                        message = f"与日志 #{other} 指向同一文件或同一目标，请分开处理"
                        errors[log_id] = message
                        errors[other] = f"与日志 #{log_id} 指向同一文件或同一目标，请分开处理"
                    source_files[file_id] = log_id
                    destinations[target] = log_id
                row.update(preview)
            except Exception as exc:
                errors[log_id] = str(exc)
            items.append(row)
        if len(title_groups) > 1:
            for log_ids in title_groups.values():
                for log_id in log_ids:
                    errors.setdefault(log_id, "选中了不同作品或无法证明同一作品的文件，请按原始标题分组纠正")
        for row in items:
            if row["log_id"] in errors:
                row["error"] = errors[row["log_id"]]
        digest = hashlib.sha256(json.dumps(
            {"entries": entries, "candidate": selected, "items": items},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest() if not errors else ""
        return {"items": items, "errors": [{"log_id": key, "error": value} for key, value in errors.items()],
                "can_execute": not errors, "preview_digest": digest}

    def run_batch(self, action: str, entries: list[dict], confirm_text: str = "", *,
                  candidate: dict | None = None, preview_digest: str = "") -> dict:
        """纠正识别先复核整批预览；复用单条写链路，保留逐条失败回执。"""
        previews: dict[int, dict] = {}
        if action == "reorganize":
            batch = self.preview_batch(entries, candidate)
            if not batch["can_execute"] or not preview_digest or batch["preview_digest"] != preview_digest:
                raise ValueError("批量预览已失效或存在错误，请重新预览并确认")
            previews = {item["log_id"]: item for item in batch["items"]}
        else:
            self.validate_batch([entry["log_id"] for entry in entries], action)
        completed: list[dict] = []
        failed: list[dict] = []
        warnings: list[str] = []

        prepared_deletes: dict[int, list[CorrectionItem]] = {}
        if action == "delete":
            if confirm_text != "DELETE":
                raise ValueError("请输入 DELETE 确认批量移入光鸭回收站")
            for entry in entries:
                log_id = int(entry["log_id"])
                try:
                    prepared_deletes[log_id] = self._prepare_delete_group(log_id, confirm_text)
                except Exception as exc:
                    logger.error("剧集批量删除预检失败 log=%s type=%s", log_id, type(exc).__name__)
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
            try:
                if action == "reorganize":
                    result = self._execute_reorganize(log_id, token, version, previews[log_id])
                elif action == "revert":
                    result = self.revert_latest(log_id, token, version)
                else:
                    result = self._execute_claimed_delete(log_id, token, prepared_deletes[log_id])
                completed.append({"log_id": log_id, "result": result})
                warnings.extend(result.get("warnings") or [])
            except Exception as exc:
                logger.error("剧集批量操作失败 action=%s log=%s type=%s", action, log_id, type(exc).__name__)
                failed.append({"log_id": log_id, "error": str(exc)})
        return {
            "success": not failed,
            "action": action,
            "requested": len(entries),
            "completed": completed,
            "failed": failed,
            "warnings": warnings,
        }

    def search_candidates(self, log_id: int, query: str = "", year: str = "",
                          media_type: str = "") -> list[dict | CleanTitleCandidate]:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["search"]:
            raise ValueError(detail.get("safety_notice") or "该日志不能人工纠偏")
        query = (query or detail.get("title") or detail.get("original_name") or "").strip()
        rules = self._rules_for_log(detail)
        if rules.nsfw_exclusive:
            with self.organizer._nsfw_recognizer_lease(rules) as recognizer:
                if recognizer is None:
                    raise ValueError("成人来源未配置可用的 MetaTube 服务")
                candidates = recognizer.candidates(query)
            if candidates:
                return [self._candidate_dict(item) for item in candidates]
            seed = (
                query or detail.get("original_name") or detail.get("current_name") or ""
            )
            fallback = build_clean_title_candidate(
                str(seed), rules.nsfw_strip_domains,
            )
            return [fallback] if fallback is not None else []
        media_type = "tv" if media_type == "tv" else (
            "movie" if media_type == "movie" else detail.get("media_type") or "movie"
        )
        candidates = self.scraper.search_candidates(query, year, media_type)
        return [self._candidate_dict(item) for item in candidates]

    @staticmethod
    def _candidate_dict(item: Candidate) -> dict:
        provider = str(getattr(item, "provider", "") or "tmdb").strip().lower()
        external_id = str(getattr(item, "external_id", "") or item.tmdb_id or "").strip()
        return {
            "tmdb_id": item.tmdb_id,
            "title": item.title,
            "year": item.year,
            "score": item.score,
            "media_type": item.media_type,
            "provider": provider,
            "external_id": external_id,
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
               year: str = "", *, provider: str = "", external_id: str = "",
               rules: OrganizeRules | None = None,
               source_values: tuple[str, ...] = ()) -> MatchResult:
        """按来源边界解析人工候选；成人来源绝不接受 TMDB 回退。"""
        effective_rules = rules or OrganizeRules.from_config().for_source("")
        normalized_provider = (provider or "").strip().lower()
        if effective_rules.nsfw_exclusive:
            if normalized_provider not in {"metatube", "clean_title"} or not (external_id or "").strip():
                raise ValueError("请选择有效的成人内容候选")
            if normalized_provider == "clean_title":
                source_seed = next((value for value in source_values if (value or "").strip()), "")
                fallback = build_clean_title_candidate(
                    source_seed, effective_rules.nsfw_strip_domains,
                )
                if fallback is None:
                    raise ValueError("原文件名无法生成安全的清洗标题")
                resolved_code = normalize_code(fallback.get("external_id") or "")
                if normalize_code(external_id) != resolved_code:
                    raise ValueError("清洗标题候选番号与原文件不一致，请重新生成")
                # Web 请求中的 title 可被修改；清洗入库始终以原文件服务端重算结果为准。
                resolved_title = (fallback.get("title") or resolved_code).strip()
                metadata = {
                    **dict(fallback.get("metadata") or {}),
                    "number": resolved_code,
                    "title": resolved_title,
                    "fallback": True,
                }
                return MatchResult(
                    title=resolved_title, media_type="movie", confidence=1.0,
                    provider="clean_title", external_id=resolved_code,
                    metadata=metadata, status="matched", locked=True,
                )
            with self.organizer._nsfw_recognizer_lease(effective_rules) as recognizer:
                if recognizer is None:
                    raise ValueError("成人来源未配置可用的 MetaTube 服务")
                # 人工纠偏允许改正原来的错误番号，身份来自所选条目的真实详情。
                result, _ = recognizer.resolve(external_id.strip())
            return result
        if normalized_provider not in {"", "tmdb"}:
            raise ValueError("普通媒体来源只允许使用 TMDB 候选")
        media_type = "tv" if media_type == "tv" else "movie"
        resolved_tmdb_id = (tmdb_id or external_id or "").strip()
        result = self.scraper.match_from_tmdb(resolved_tmdb_id, media_type)
        if not result.tmdb_id:
            raise ValueError(result.error or "TMDB 详情不存在")
        return result

    def _verify_nsfw_target_identity(self, match: MatchResult, target_path: str) -> None:
        if Organizer._match_provider(match) not in {"metatube", "clean_title"}:
            return
        # 成人纠偏始终为 movie；target_path 即媒体根目录，身份只来自历史记录。
        plan = OrganizePlan(
            file_id="", original_name="", original_path="", match=match,
            media_root_path=target_path, identity_guard_required=True,
        )
        reason = self.organizer._identity_guard_reason(plan, {}, {})
        if reason:
            raise ValueError(reason)

    def preview_reorganize(
        self, log_id: int, tmdb_id: str, media_type: str,
        title: str = "", year: str = "",
        season: int | None = None, episode: int | None = None,
        *, provider: str = "", external_id: str = "",
    ) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["preview"]:
            raise ValueError(detail.get("safety_notice") or "当前状态不能重新整理")
        items = self._load_items(log_id)
        video = self._video(items)
        rules = self._rules_for_log(detail)
        source_name = video.original_name or video.current_name
        parent_path = str(detail.get("original_path") or "")
        match = self._match(
            tmdb_id, media_type, title, year,
            provider=provider, external_id=external_id, rules=rules,
            source_values=(video.original_name, video.current_name, parent_path),
        )
        parsed = self.organizer._parse_media_fields(source_name)
        if rules.nsfw_exclusive:
            from app.modules.nsfw import extract_nsfw_multipart
            multipart = extract_nsfw_multipart(
                source_name, rules.nsfw_strip_domains,
            )
            if multipart is not None and multipart.part_index is not None:
                parsed["part"] = multipart.part_index
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
        confirmed = self._confirmed_position(detail.get("release_parse")) if match.media_type == "tv" else None
        if confirmed is not None:
            # 人工位置是独立于原始发布名的已确认事实；只有显式新覆盖才改变它。
            parsed["season"], parsed["episode"] = confirmed["season"], confirmed.get("episode")
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
        if Organizer._match_provider(match) == "tmdb" and isinstance(self.scraper, TMDBScraper) and match.media_type == "tv":
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
        if rules.region_split and Organizer._match_provider(match) not in {"metatube", "clean_title"}:
            parts.append(region)
        if rules.year_split and resolved_year:
            parts.append(resolved_year)
        parts.extend(self.organizer.build_media_path_parts(match, parsed, rules))
        target_path = "/".join(parts)
        self._verify_nsfw_target_identity(match, target_path)
        self._verify_items(items)
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
                "provider": Organizer._match_provider(match),
                "external_id": Organizer._match_external_id(match),
            },
            "target_root_id": rules.target_dir_id,
            "target_path": target_path,
            "media_dir": self.organizer.build_media_dir(match, rules),
            "file_name": new_name,
            "season": parsed.get("season"),
            "episode": parsed.get("episode"),
            "manual_position": {
                "version": 1, "source": "manual_correction",
                "season": parsed.get("season"), "episode": parsed.get("episode"),
            } if match.media_type == "tv" and (confirmed is not None or season is not None or episode is not None) else None,
            "items": planned,
            "rules_snapshot": organize_rules_snapshot(rules),
            "cloud_write": False,
        }

    def reorganize(
        self, log_id: int, operation_token: str, expected_version: int,
        tmdb_id: str, media_type: str, title: str = "", year: str = "",
        season: int | None = None, episode: int | None = None,
        *, provider: str = "", external_id: str = "",
    ) -> dict:
        preview = self.preview_reorganize(
            log_id, tmdb_id, media_type, title, year, season, episode,
            provider=provider, external_id=external_id,
        )
        return self._execute_reorganize(log_id, operation_token, expected_version, preview)

    def _verify_item(self, item: CorrectionItem) -> GuangYaFile:
        remote = self.client.file_info(item.file_id)
        if not remote:
            raise DirectoryScrapeConflictError("云端文件不存在或详情不可用，请先刷新云盘目录核对，不要重复提交整理。")
        if not (remote.file_id or "").strip():
            raise DirectoryScrapeConflictError("无法读取云端文件详情，请稍后刷新云盘目录重试。")
        if remote.file_id != item.file_id:
            raise DirectoryScrapeConflictError("云端文件详情不匹配，已停止整理，请核对当前文件。")
        if item.current_parent_id and remote.parent_id != item.current_parent_id:
            raise DirectoryScrapeConflictError("文件位置已变化，当前日志快照已过期；请查看同一文件的其它整理记录，或在当前云盘目录重新识别。")
        if item.current_name and remote.name != item.current_name:
            raise DirectoryScrapeConflictError("文件名已变化，当前日志快照已过期；请查看同一文件的其它整理记录，或在当前云盘目录重新识别。")
        if item.size and remote.size and item.size != remote.size:
            raise DirectoryScrapeConflictError("文件大小已变化，当前日志快照已过期，请在当前云盘目录重新识别。")
        if item.etag and remote.etag and item.etag != remote.etag:
            raise DirectoryScrapeConflictError("文件校验值已变化，当前日志快照已过期，请在当前云盘目录重新识别。")
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
            if not item.etag or (item.size or 0) <= 0:
                raise RuntimeError(f"GCID/size 歧义，禁止删除: {item.current_name}")
            matches = [
                entry for entry in entries
                if entry.etag == item.etag
                and (entry.size or 0) == (item.size or 0)
            ]
            if len(matches) != 1 or matches[0].file_id != item.file_id:
                raise RuntimeError(f"GCID/size 歧义，禁止删除: {item.current_name}")
            if remote.etag != item.etag or (remote.size or 0) != (item.size or 0):
                raise RuntimeError(f"GCID/size 与日志快照不一致: {item.current_name}")
        return remotes

    def _ensure_target_dir(self, root_id: str, path: str) -> tuple[str, list[str]]:
        current = (root_id or "0")
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
                        file_id=dir_id,
                        name=f"纠偏临时目录 {dir_id}",
                        parent_id="",
                    ),
                )
            except Exception as exc:
                logger.warning("清理本次纠偏创建的空目录失败 type=%s", type(exc).__name__)

    def _restore_directory_targets(self, log_id: int, operation_token: str,
                                   directory_ids: list[str], *, recreate: bool = True) -> dict[str, str]:
        """只重建成功清理凭据中的目录；同名外来目录及未知创建绝不接管。"""
        resolved: dict[str, str] = {}
        visiting: set[str] = set()

        def resolve(directory_id: str) -> str:
            if directory_id in resolved:
                return resolved[directory_id]
            if directory_id in visiting or len(visiting) >= 64:
                raise RuntimeError("回退目录凭据存在循环或过深，需人工核验")
            visiting.add(directory_id)
            with db.get_conn() as conn:
                audit = conn.execute(
                    "SELECT * FROM organize_delete_audit WHERE file_id=? ORDER BY id DESC LIMIT 1",
                    (directory_id,),
                ).fetchone()
                restored = conn.execute(
                    "SELECT * FROM organize_operation_steps WHERE action='restore_directory' "
                    "AND from_parent_id=? ORDER BY id DESC LIMIT 1", (directory_id,),
                ).fetchone()
            if audit is None or (not recreate and restored is None):
                # 清理只跟随已有恢复凭据；未由整理清理的目录不猜路径、不创建。
                result = directory_id
            else:
                if (audit['status'] != 'success' or audit['trigger'] not in (
                        'empty_dir_cleanup', 'download_staging_cleanup', 'correction_empty_dir_cleanup',
                        'return_to_source_empty_dir_cleanup', 'reorganize_empty_dir_cleanup') or not audit['parent_id'] or not audit['file_name']):
                    info = self.client.file_info(directory_id)
                    # 清理被阻止时原目录可能仍真实存在：保留原 ID 的既有送回契约，
                    # 但回收站详情不算存在，必须仍在其父目录成员中；此分支绝不建目录。
                    if (info is not None and info.is_dir and info.parent_id
                            and any(entry.file_id == directory_id and entry.is_dir and entry.name == info.name
                                    for entry in self.client.list_dir(info.parent_id))):
                        visiting.remove(directory_id)
                        resolved[directory_id] = directory_id
                        return directory_id
                    raise RuntimeError("原目录缺少完整的成功清理凭据，禁止猜测重建")
                name = str(audit['file_name'])
                if name in ('.', '..') or any(char in name for char in ('/', '\\', '\x00')):
                    raise RuntimeError("原目录清理凭据中的名称无效")
                parent_id = resolve(str(audit['parent_id']))
                parent = self.client.file_info(parent_id)
                if parent_id != '0' and (parent is None or not parent.is_dir):
                    raise RuntimeError("回退目录的已知父目录不存在，禁止盲建")
                children = self.client.list_dir(parent_id)
                original = next((entry for entry in children if entry.file_id == directory_id), None)
                if original is not None:
                    if not original.is_dir or original.name != name:
                        raise RuntimeError("原目录身份已经变化，禁止回退")
                    result = directory_id
                elif restored is not None:
                    if restored['status'] != 'success':
                        raise RuntimeError("上次目录恢复结果未知，必须人工核验，禁止重放")
                    result = resolve(str(restored['to_parent_id']))
                    current = self.client.file_info(result)
                    if (current is None or not current.is_dir or current.parent_id != parent_id
                            or current.name != name or not any(entry.file_id == result for entry in self.client.list_dir(parent_id))):
                        raise RuntimeError("已恢复目录的身份或位置已变化，禁止接管同名目录")
                else:
                    try:
                        info = self.client.file_info(directory_id)
                    except GuangYaReadRejected as exc:
                        # 只在完整成功清理凭据、父目录可读且原 ID 已不在父列表时，
                        # 允许按凭据重建。149 绝不用于判空、放行删除或跳过父级校验。
                        if exc.code != "149":
                            raise
                        info = None
                    if info is not None and (info.name != name or info.parent_id != str(audit['parent_id'])):
                        raise RuntimeError("原目录已被外部移动或改名，禁止猜测重建")
                    if any(entry.name == name for entry in children):
                        raise RuntimeError("回退路径存在无恢复凭据的同名条目，禁止合并或覆盖")
                    step_id = db.add_organize_operation_step(
                        log_id, operation_token + ':directories', len(resolved) + 1, 'restore_directory',
                        file_id=directory_id, from_parent_id=directory_id, from_name=name,
                        to_parent_id=parent_id, to_name=name, status='running',
                    )
                    try:
                        result = (self.client.create_dir(name, parent_id) or '')
                        current = self.client.file_info(result) if result else None
                        matches = [entry for entry in self.client.list_dir(parent_id) if entry.name == name]
                        if (current is None or not current.is_dir or current.parent_id != parent_id
                                or current.name != name or len(matches) != 1 or matches[0].file_id != result):
                            raise RuntimeError("目录恢复的返回身份未确认，必须人工核验")
                        with db.get_conn() as conn:
                            changed = conn.execute(
                                "UPDATE organize_operation_steps SET to_parent_id=?,status='success',finished_at=? "
                                "WHERE id=? AND status='running'", (result, db.now(), step_id),
                            )
                            if changed.rowcount != 1:
                                raise RuntimeError("目录恢复凭据未能可靠提交，必须人工核验")
                    except Exception as exc:
                        db.finish_organize_operation_step(step_id, 'failed', str(exc))
                        raise
            visiting.remove(directory_id)
            resolved[directory_id] = result
            return result

        for parent_id in directory_ids:
            resolve(parent_id)
        return resolved

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
        fields: dict[str, Any] = {"status": "rollback_failed", "error": str(error),
                  "current_parent_id": "", "current_name": ""}
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
        restore_guangya_file(
            self.client,
            GuangYaFile(item.file_id, target_name, False, item.size, item.etag, target_parent_id),
        )

    def _apply_transition(self, item: CorrectionItem, target_parent_id: str,
                          target_name: str, step_id: int, *,
                          rename_first: bool) -> AppliedTransition:
        current_parent_id = item.current_parent_id
        current_name = item.current_name
        write_attempted = False
        try:
            if rename_first and current_name != target_name:
                write_attempted = True
                self.client.rename(item.file_id, target_name)
                current_name = target_name
            if current_parent_id != target_parent_id:
                write_attempted = True
                self.client.move([item.file_id], target_parent_id)
                current_parent_id = target_parent_id
            if not rename_first and current_name != target_name:
                write_attempted = True
                self.client.rename(item.file_id, target_name)
                current_name = target_name
        except Exception as exc:
            if not write_attempted:
                raise
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

    @staticmethod
    def _archive_cleanup_protected_ids(
        items: list[CorrectionItem], rules: OrganizeRules,
    ) -> tuple[set[str], str]:
        """返回送回源目录时绝不能删除的永久根目录。"""
        from app.modules.organize_tasks import _protected_organize_root_ids

        protected, error = _protected_organize_root_ids(rules)
        protected.update((item.original_parent_id or "").strip() for item in items)
        protected.discard("")
        return protected, error

    @staticmethod
    def _directory_belongs_to_target_root(
        directory: GuangYaFile,
        *,
        target_root_id: str,
        forbidden_ancestor_ids: set[str],
        read_directory,
    ) -> bool:
        """只接受祖先链仍明确到达目标根且未进入来源/保护子树的目录。"""
        normalized_target = (target_root_id or "").strip()
        if not normalized_target:
            return False
        current_id = (directory.file_id or "").strip()
        ancestor_id = (directory.parent_id or "").strip()
        visited = {current_id}
        for _ in range(32):
            if ancestor_id in forbidden_ancestor_ids:
                return False
            if ancestor_id == normalized_target:
                return True
            if not ancestor_id or ancestor_id in visited:
                return False
            visited.add(ancestor_id)
            ancestor = read_directory(ancestor_id)
            if (
                ancestor is None
                or not bool(getattr(ancestor, "is_dir", False))
                or str(getattr(ancestor, "file_id", "") or "") != ancestor_id
            ):
                return False
            next_parent = str(getattr(ancestor, "parent_id", "") or "").strip()
            if not next_parent or next_parent == ancestor_id:
                return False
            ancestor_id = next_parent
        return False

    def _capture_archive_cleanup_directories(
        self,
        *,
        detail: dict,
        items: list[CorrectionItem],
        rules: OrganizeRules,
    ) -> tuple[list[GuangYaFile], set[str], list[str]]:
        """在移动前记录本日志精确命中的目标叶目录及媒体根。

        只沿日志 ``new_path`` 能证明的媒体层级向上读取，最多清理
        ``Season N`` 和它的媒体根；分类、地区、年份和归档根永不进入候选。
        """
        protected, config_error = self._archive_cleanup_protected_ids(items, rules)
        if config_error:
            return [], protected, [
                "整理源目录配置异常，已跳过目标空目录清理以保证安全"
            ]

        raw_path = str(detail.get("new_path") or "").replace("\\", "/")
        path_parts = [part.strip() for part in raw_path.split("/") if part.strip()]
        directory_parts = path_parts[:-1]
        if not directory_parts:
            return [], protected, []

        leaf_name = directory_parts[-1]
        has_season_layer = bool(
            str(detail.get("media_type") or "") == "tv"
            and (
                detail.get("season") is not None
                or leaf_name.casefold() == "specials"
                or re.fullmatch(r"(?i)season\s+\d+", leaf_name)
            )
        )
        media_root_index = -2 if has_season_layer else -1
        if len(directory_parts) < abs(media_root_index):
            return [], protected, [
                "整理目标路径缺少可验证的媒体目录层级，空目录已安全保留"
            ]
        media_root_name = directory_parts[media_root_index]
        identity_marker = Organizer._identity_marker(media_root_name)
        tmdb_id = str(detail.get("tmdb_id") or "").strip()
        provider = str(detail.get("provider") or "").strip().lower()
        external_id = str(detail.get("external_id") or "").strip()
        if not provider and tmdb_id:
            provider = "tmdb"
        if not external_id:
            external_id = tmdb_id
        expected_marker = Organizer._match_identity_tag(MatchResult(
            tmdb_id=tmdb_id,
            provider=provider,
            external_id=external_id,
        )).casefold()
        if (
            not expected_marker
            or not identity_marker
            or identity_marker != expected_marker
        ):
            return [], protected, [
                "整理目标路径缺少匹配的媒体身份标识，空目录已安全保留"
            ]
        cleanup_levels = min(len(directory_parts), 2 if has_season_layer else 1)
        file_info = getattr(self.client, "file_info", None)
        if not callable(file_info):
            return [], protected, [
                "云盘接口无法核验整理目标目录，已安全保留空目录"
            ]
        target_root_id = (rules.target_dir_id or "").strip()
        if not target_root_id:
            return [], protected, [
                "整理目标根目录配置为空，已安全保留空目录"
            ]

        snapshot_cache: dict[str, GuangYaFile] = {}

        def read_directory(directory_id: str) -> GuangYaFile | None:
            cached = snapshot_cache.get(directory_id)
            if cached is not None:
                return cached
            current = self.client.file_info(directory_id)
            if (
                current is None
                or not bool(getattr(current, "is_dir", False))
                or str(getattr(current, "file_id", "") or "") != directory_id
            ):
                return None
            snapshot_cache[directory_id] = current
            return current

        ancestry_cache: dict[str, bool] = {}
        forbidden_ancestor_ids = protected - {target_root_id}

        def belongs_to_target_root(media_root: GuangYaFile) -> bool:
            media_root_id = (media_root.file_id or "").strip()
            if media_root_id not in ancestry_cache:
                ancestry_cache[media_root_id] = self._directory_belongs_to_target_root(
                    media_root,
                    target_root_id=target_root_id,
                    forbidden_ancestor_ids=forbidden_ancestor_ids,
                    read_directory=read_directory,
                )
            return ancestry_cache[media_root_id]

        captured: list[GuangYaFile] = []
        seen: set[str] = set()
        warnings: list[str] = []
        target_parent_ids = list(dict.fromkeys(
            (item.current_parent_id or "").strip() for item in items
            if (item.current_parent_id or "").strip()
        ))
        for leaf_id in target_parent_ids:
            current_id = leaf_id
            branch: list[GuangYaFile] = []
            for level in range(cleanup_levels):
                if not current_id or current_id in protected:
                    break
                expected_name = directory_parts[-1 - level]
                try:
                    current = read_directory(current_id)
                except Exception as exc:
                    logger.warning(
                        "纠偏前读取旧归档目录失败 level=%s type=%s",
                        level, type(exc).__name__,
                    )
                    warnings.append("无法核验部分整理目标目录，已安全保留")
                    break
                if current is None or (current.name or "") != expected_name:
                    warnings.append("整理目标目录与日志快照不一致，已安全保留")
                    break
                branch.append(current)
                next_parent = (current.parent_id or "").strip()
                if not next_parent or next_parent == current_id:
                    break
                current_id = next_parent
            if len(branch) != cleanup_levels:
                continue
            try:
                belongs = belongs_to_target_root(branch[-1])
            except Exception as exc:  # noqa: BLE001 - 收尾核验失败仅保留目录，不阻止纠偏。
                logger.warning("核验旧归档祖先失败 type=%s", type(exc).__name__)
                warnings.append("无法核验整理目标目录的祖先，已安全保留")
                continue
            if not belongs:
                warnings.append("整理目标目录不属于当前整理目标根，已安全保留")
                continue
            for current in branch:
                directory_id = (current.file_id or "").strip()
                if directory_id in seen:
                    continue
                captured.append(current)
                seen.add(directory_id)
        return captured, protected, list(dict.fromkeys(warnings))

    def _cleanup_archive_directories(
        self,
        *,
        log_id: int,
        directories: list[GuangYaFile],
        protected: set[str],
        target_root_id: str,
        trigger: str = "return_to_source_empty_dir_cleanup",
    ) -> tuple[int, list[str]]:
        """纠偏提交后，自底向上安全回收本日志移空的旧归档目录。"""
        if not directories:
            return 0, []
        delete_empty = getattr(self.client, "delete_empty_directory", None)
        guarded_capability = getattr(
            self.client, "supports_guarded_empty_directory_delete", None
        )
        if guarded_capability is None:
            guarded_capability = getattr(
                self.client, "supports_atomic_empty_directory_delete", None
            )
        if not callable(delete_empty) or guarded_capability is not True:
            audit_failed = False
            for snapshot in directories:
                directory_id = (snapshot.file_id or "").strip()
                if not directory_id or directory_id in protected:
                    continue
                try:
                    record_blocked_delete(
                        trigger=trigger,
                        reason="当前 Provider 不支持带版本与空目录复核的回收站删除",
                        candidate=DeleteCandidate(
                            file_id=directory_id,
                            name=(snapshot.name or "整理目标空目录"),
                            parent_id=(snapshot.parent_id or ""),
                            gcid=(snapshot.etag or ""),
                        ),
                        organize_log_id=log_id,
                    )
                except Exception as exc:
                    audit_failed = True
                    logger.warning(
                        "记录送回空目录保留审计失败 type=%s",
                        type(exc).__name__,
                    )
            warnings = [
                "当前云盘接口不支持安全的空目录回收站清理，整理目标目录已保留"
            ]
            if audit_failed:
                warnings.append("目标空目录保留审计写入失败，请查看服务日志")
            return 0, warnings

        # 同一媒体目录可能同时包含本日志留下的多个季目录。先按候选链深度
        # 删除所有叶目录，再重新检查媒体根，避免根目录因过早检查而残留。
        unique_directories: list[GuangYaFile] = []
        directory_by_id: dict[str, GuangYaFile] = {}
        for snapshot in directories:
            directory_id = (snapshot.file_id or "").strip()
            if not directory_id or directory_id in directory_by_id:
                continue
            directory_by_id[directory_id] = snapshot
            unique_directories.append(snapshot)

        def candidate_depth(snapshot: GuangYaFile) -> int:
            depth = 0
            seen_ids = {(snapshot.file_id or "").strip()}
            parent_id = (snapshot.parent_id or "").strip()
            while parent_id in directory_by_id and parent_id not in seen_ids:
                depth += 1
                seen_ids.add(parent_id)
                parent_id = (
                    directory_by_id[parent_id].parent_id or ""
                ).strip()
            return depth

        ordered_directories = [
            snapshot for _, snapshot in sorted(
                enumerate(unique_directories),
                key=lambda pair: (-candidate_depth(pair[1]), pair[0]),
            )
        ]
        atomic_capability = getattr(
            self.client, "supports_atomic_empty_directory_delete", None
        )
        cleanup_strategy = (
            "Provider 原子校验后移入回收站"
            if atomic_capability is True
            else "双重版本与空目录复核后移入回收站"
        )

        cleaned = 0
        warnings: list[str] = []
        for snapshot in ordered_directories:
            directory_id = (snapshot.file_id or "").strip()
            if not directory_id or directory_id in protected:
                continue
            try:
                if self.client.list_dir(directory_id):
                    record_blocked_delete(
                        trigger=trigger,
                        reason="目录仍含其他内容，已保留",
                        candidate=DeleteCandidate(
                            file_id=directory_id,
                            name=(snapshot.name or "整理目标目录"),
                            parent_id=(snapshot.parent_id or ""),
                            gcid=(snapshot.etag or ""),
                        ),
                        organize_log_id=log_id,
                    )
                    warnings.append("部分整理目标目录仍含其他内容，已保留")
                    continue
                current = self.client.file_info(directory_id)
                if (
                    current is None
                    or not bool(getattr(current, "is_dir", False))
                    or str(getattr(current, "file_id", "") or "") != directory_id
                    or str(getattr(current, "name", "") or "") != (snapshot.name or "")
                    or str(getattr(current, "parent_id", "") or "")
                    != (snapshot.parent_id or "")
                ):
                    raise RuntimeError("目录身份或位置已变化")
                expected_etag = str(getattr(current, "etag", "") or "")
                try:
                    expected_updated_at = max(
                        0, int(getattr(current, "updated_at", 0) or 0)
                    )
                except (TypeError, ValueError):
                    expected_updated_at = 0
                if not expected_etag and expected_updated_at <= 0:
                    raise RuntimeError("目录缺少可验证版本")
                normalized_target_root = (target_root_id or "").strip()
                if not self._directory_belongs_to_target_root(
                    current,
                    target_root_id=normalized_target_root,
                    forbidden_ancestor_ids=protected - {normalized_target_root},
                    read_directory=self.client.file_info,
                ):
                    raise RuntimeError("目录已不属于当前整理目标根")
                execute_recycle_bin_delete(
                    self.client,
                    trigger=trigger,
                    reason=(
                        "纠偏成功后清理本日志移空的旧归档目录；"
                        f"{cleanup_strategy}"
                    ),
                    candidate=DeleteCandidate(
                        file_id=directory_id,
                        name=(current.name or "整理目标空目录"),
                        parent_id=(current.parent_id or ""),
                        gcid=expected_etag,
                    ),
                    organize_log_id=log_id,
                    safe_failure_message="目标目录状态已变化，已安全保留",
                    delete_operation=lambda current_id=directory_id,
                    current_etag=expected_etag,
                    current_updated_at=expected_updated_at: delete_empty(
                        current_id,
                        expected_etag=current_etag,
                        expected_updated_at=current_updated_at,
                    ),
                )
                cleaned += 1
            except Exception as exc:
                logger.warning(
                    "纠偏后清理旧归档空目录失败 type=%s",
                    type(exc).__name__,
                )
                warnings.append("部分整理目标空目录未能清理，目录已安全保留")
        return cleaned, list(dict.fromkeys(warnings))

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
        if match and str(match.get("provider") or "tmdb").strip().lower() == "tmdb":
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

    def _download_recovery_snapshot(self, source_dir_id: str) -> dict | None:
        from app.repositories.download_staging import list_staging_cleanup_requests

        rows = list_staging_cleanup_requests(source_id=source_dir_id) if source_dir_id else []
        if len(rows) != 1:
            return None
        row = rows[0]
        if row['organize_status'] in ('failed', 'partial') or int(row['organize_started'] or 0) < 0:
            return row
        return None

    def _verified_download_recovery(self, expected: dict | None) -> dict | None:
        if expected is None:
            return None
        from app.modules.download_staging_cleanup import _empty_tree, cleanup_report
        from app.repositories.download_staging_reconcile import (
            reorganized_source_evidence,
        )

        members = reorganized_source_evidence(str(expected['gy_target_dir']))
        if not members:
            raise RuntimeError("原下载仍有未完成或缺少凭据的媒体组，未恢复失败请求")
        # 文件 ID、位置、名称、大小和 etag 都需与已提交成员一致；不能只信 success。
        for member in members:
            remote = self._verify_item(self._to_item(member))
            if (remote.is_dir or remote.size != member['size']
                    or (remote.etag or '').lower() != str(member['etag']).lower()):
                raise RuntimeError("重整成员的云端身份尚未核实，未恢复失败请求")
        # 复用现有严格空树扫描，不让未记日志的残留、分页/身份异常被成功记录掩盖。
        _empty_tree(self.client, expected, set(), cleanup_report())
        return {'request': expected, 'members': members}

    def _finalize_reorganized_source(
        self, source_dir_id: str, rules: OrganizeRules, *, request_snapshot: dict | None = None,
        cleanup_source: bool = True,
    ) -> list[str]:
        """成功提交后闭环旧确认，并复用现有来源/下载隔离目录收尾。"""
        from app.modules.download_staging_cleanup import source_inside_protected_staging
        from app.modules.organize_confirmations import _finalize_confirmed_downloads
        from app.modules.organize_tasks import (
            OrganizeTaskManager,
            _cleanup_manual_source_root,
            _protected_organize_root_ids,
        )
        from app.repositories.download_staging import list_staging_cleanup_requests
        from app.repositories.download_staging_reconcile import (
            reconcile_reorganized_confirmations,
        )

        source_dir_id = (source_dir_id or "").strip()
        if not source_dir_id:
            return []
        stats: dict = {}
        warnings: list[str] = []
        try:
            recovery = self._verified_download_recovery(request_snapshot)
            tokens = reconcile_reorganized_confirmations([source_dir_id], recovery=recovery)
            # 已入库作品纠正的是当前归档位置，不应重读早已清理的下载根。
            # 尚有确认/失败下载恢复工作时，仍走原有严格下载收尾。
            if not cleanup_source and not tokens and request_snapshot is None:
                return []
            # 必须在持久收尾可能删除 staging 根之前判定来源归属；通用扫描
            # 永不绕过下载确认、请求身份或结果未知的删除保护。
            staging_ids = set(db.list_protected_guangya_staging_ids())
            staging_source = source_dir_id in staging_ids or (
                rules.clean_empty and source_inside_protected_staging(
                    self.client, source_dir_id, staging_ids,
                )
            )
            for token in tokens:
                confirmation = db.get_organize_confirmation(token)
                assert confirmation is not None
                _finalize_confirmed_downloads(
                    json.loads(confirmation["payload_json"]), self.client, stats, rules,
                    confirmation_token=token,
                )
            if staging_source and not tokens:
                # 普通整理/冲突跳过后可能没有确认卡；只收尾原本已完成的下载，
                # 无成功确认依据的 requires_manual 仍由原安全规则保护。
                rows = list_staging_cleanup_requests(source_id=source_dir_id)
                completed = [row for row in rows if row.get("organize_status") == "completed"]
                if completed:
                    report = OrganizeTaskManager._cleanup_download_staging(
                        self.organizer.client, [int(row["id"]) for row in completed],
                        [{"id": str(row["gy_target_dir"]), "name": str(row["gy_staging_name"])}
                         for row in completed], rules=rules,
                    )
                    for reason in report.get("reasons", []):
                        Organizer._append_reason(stats, "empty_dir_cleanup_reasons", reason, limit=8)
            if rules.clean_empty and not staging_source:
                cleanup_source_id = self._restore_directory_targets(
                    0, "", [source_dir_id], recreate=False,
                )[source_dir_id]
                protected, error = _protected_organize_root_ids(rules)
                if error:
                    raise RuntimeError(error)
                report = self.organizer.clean_empty_dirs(
                    cleanup_source_id, with_report=True, with_diagnostics=True,
                    protected_source_ids=protected,
                )
                for reason in report.get("reasons", []):
                    Organizer._append_reason(stats, "empty_dir_cleanup_reasons", reason, limit=8)
                # 子树扫描或删除失败时保留源根，不破坏已提交业务或补偿现场。
                if not any(report.get(key) for key in (
                    "scan_failures", "delete_failures", "unsupported", "unavailable",
                )):
                    _cleanup_manual_source_root(
                        self.organizer, cleanup_source_id, "", rules, stats, trigger_type="manual",
                    )
        except Exception as exc:  # noqa: BLE001 - 收尾失败不能回滚已持久化的整理结果。
            reason = (
                f"{exc}（{exc.operation}）"
                if isinstance(exc, GuangYaReadRejected) else type(exc).__name__
            )
            logger.warning("重新整理后源目录收尾失败: %s", reason)
            warnings.append(f"源目录收尾失败，已保留未删除目录: {reason}")
        warnings.extend(stats.get("empty_dir_cleanup_reasons", []))
        return warnings

    def _notify_reorganize_result(
        self,
        preview: dict,
        items: list[CorrectionItem],
        rules: OrganizeRules | None = None,
    ) -> list[str]:
        """人工纠偏提交成功后发送媒体卡片；失败只返回 warning。"""
        if rules is not None and not (rules.notify_enabled and rules.library_notify):
            return []
        warnings: list[str] = []
        try:
            from app.modules.telegram_notification_center import (
                publish_notification_event,
            )
            from app.modules.telegram_notification_policy import (
                NotificationImportance,
                NotificationTopic,
            )
            from app.notifier import build_media_events
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
                "provider": match.get("provider"), "external_id": match.get("external_id"),
                "season": parsed.get("season"), "episode": parsed.get("episode"),
                "source": "光鸭云盘 · 人工识别", "category": preview.get("target_path"),
                "filename": preview.get("file_name"), "size": video.size,
            }
            events = build_media_events([media_item])
            logical_base = str(
                preview.get("operation_token")
                or preview.get("log_id")
                or getattr(video, "log_id", "")
                or getattr(video, "file_id", "")
                or time.time_ns()
            )
            accepted = bool(events)
            for index, event in enumerate(events, start=1):
                accepted = bool(publish_notification_event(
                    f"organize-correction:{logical_base}:{index}",
                    event,
                    topic=NotificationTopic.ORGANIZE,
                    importance=NotificationImportance.RESULT,
                    topic_enabled=True,
                )) and accepted
            if not accepted:
                warnings.append("人工识别媒体通知发送失败")
        except Exception as exc:
            warnings.append(f"人工识别媒体通知发送失败: {exc}")
            logger.warning(warnings[-1])
        return warnings

    def _execute_reorganize(self, log_id: int, operation_token: str,
                            expected_version: int, preview: dict) -> dict:
        items = self._load_items(log_id)
        self._verify_items(items)
        current_rules = self._rules_for_log(self.detail(log_id))
        if organize_rules_snapshot_changes(preview["rules_snapshot"], current_rules):
            raise ValueError("整理规则已变化，请重新预览并确认")
        rules = restore_organize_rules_snapshot(preview["rules_snapshot"], trusted_rules=current_rules)
        self._verify_nsfw_target_identity(MatchResult(**preview["match"]), preview["target_path"])
        before = db.capture_organize_business_snapshot(log_id)
        previous_log = db.get_organize_log(log_id)
        assert previous_log is not None
        # 回退可能回到上一部作品的归档，也可能回到最初来源；用恢复后的
        # 业务路径和目标父目录区分，不能把所有 reverted 都当成原始来源。
        already_archived = previous_log["status"] == "success" or bool(
            previous_log["status"] == "reverted" and previous_log["new_path"]
            and previous_log["current_parent_id"]
            and previous_log["current_parent_id"] == previous_log["target_parent_id"]
            and previous_log["current_parent_id"] != previous_log["original_parent_id"]
        )
        cleanup_directories, cleanup_protected, cleanup_warnings = ([], set(), [])
        request_snapshot = self._download_recovery_snapshot(str(previous_log['source_dir_id'] or ''))
        parent_path = str(previous_log["original_path"] or "") if previous_log else ""
        previous_tmdb_id = str(before["log"].get("tmdb_id") or "").strip()
        match = preview["match"]
        selected_tmdb_id = str(match.get("tmdb_id") or "").strip()
        rejected_tmdb_ids = [previous_tmdb_id] if previous_tmdb_id and previous_tmdb_id != selected_tmdb_id else []
        release_parse = self._decode_release_parse(before["log"].get("release_parse_json")) or {}
        if preview.get("manual_position") is not None:
            release_parse["manual_position"] = dict(preview["manual_position"])
        elif match["media_type"] != "tv":
            release_parse.pop("manual_position", None)
        if not db.claim_organize_log_operation(
            log_id, operation_token, "reorganizing", tuple(REORGANIZE_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        created_dirs: list[str] = []
        completed: list[AppliedTransition] = []
        try:
            target_id, created_dirs = self._ensure_target_dir(preview["target_root_id"], preview["target_path"])
            targets = [(item, target_id, planned["to_name"]) for item, planned in zip(items, preview["items"])]
            self._verify_targets_available(targets)
            vacated_items = [item for item in items if item.current_parent_id != target_id]
            if rules.clean_empty and already_archived and vacated_items:
                cleanup_directories, cleanup_protected, cleanup_warnings = (
                    self._capture_archive_cleanup_directories(
                        detail=dict(previous_log), items=vacated_items, rules=rules,
                    )
                )
            for step_index, (item, planned) in enumerate(zip(items, preview["items"]), start=1):
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, step_index, "move_rename",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, to_parent_id=target_id,
                    to_name=planned["to_name"], status="running",
                    # 每次操作只存一份完整前像，不按成员数量二次复制。
                    state_before=before if step_index == 1 else None,
                )
                try:
                    transition = self._apply_transition(item, target_id, planned["to_name"], step_id, rename_first=False)
                    completed.append(transition)
                    db.update_organize_log_item(
                        item.id, current_parent_id=target_id, current_name=planned["to_name"],
                        target_parent_id=target_id, target_name=planned["to_name"], status="success", error="",
                    )
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
            if not db.update_organize_log(
                log_id, status="success", operation_type="reorganize",
                current_parent_id=target_id, current_name=preview["file_name"], target_parent_id=target_id,
                new_path=preview["target_path"] + "/" + preview["file_name"], tmdb_id=match["tmdb_id"],
                provider=str(match.get("provider") or ""), external_id=str(match.get("external_id") or ""),
                media_type=match["media_type"], title=match["title"], year=match["year"],
                season=preview.get("season") if match["media_type"] == "tv" else None,
                episode=preview.get("episode") if match["media_type"] == "tv" else None,
                release_parse_json=json.dumps(release_parse, ensure_ascii=False), error="",
            ):
                raise RuntimeError("重整业务结果未能持久化")
        except Exception as exc:
            self._rollback_transitions(completed)
            if completed and self._failure_status(log_id, "failed") != "partial_failed":
                try:
                    if not db.restore_organize_business_snapshot(log_id, before):
                        raise RuntimeError("重整失败后的业务前像未恢复")
                except Exception as restore_exc:
                    for transition in completed:
                        db.update_organize_log_item(
                            transition.item.id, status="rollback_failed",
                            error=f"业务前像恢复失败，必须人工核验: {type(restore_exc).__name__}",
                        )
            self._cleanup_created_dirs(created_dirs)
            db.update_organize_log(log_id, status=self._failure_status(log_id, "failed"), error=str(exc))
            raise
        empty_dirs_cleaned, directory_warnings = self._cleanup_archive_directories(
            log_id=log_id, directories=cleanup_directories,
            protected=cleanup_protected | {target_id}, target_root_id=rules.target_dir_id,
            trigger="reorganize_empty_dir_cleanup",
        )
        warnings = [*cleanup_warnings, *directory_warnings]
        warnings.extend(self._finalize_reorganized_source(
            str(previous_log["source_dir_id"] or "") if previous_log else "", rules,
            request_snapshot=request_snapshot, cleanup_source=not already_archived,
        ))
        warnings.extend(self._notify_reorganize_result(preview, items, rules))
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
            "item_count": len(items), "empty_dirs_cleaned": empty_dirs_cleaned,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def return_to_source(self, log_id: int, operation_token: str,
                         expected_version: int) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["return_to_source"]:
            raise ValueError(detail.get("safety_notice") or "当前状态不能送回源目录")
        items = self._load_items(log_id)
        self._verify_items(items)
        rules = OrganizeRules.from_config()
        cleanup_directories, cleanup_protected, cleanup_warnings = (
            self._capture_archive_cleanup_directories(
                detail=detail, items=items, rules=rules,
            )
        )
        targets = [(item, item.original_parent_id, item.original_name) for item in items]
        if not db.claim_organize_log_operation(
            log_id, operation_token, "returning", tuple(RETURN_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        completed: list[AppliedTransition] = []
        targets_verified = False
        try:
            restored = self._restore_directory_targets(
                log_id, operation_token, [parent for _item, parent, _name in targets],
            )
            self._verify_targets_available(
                [(item, restored[parent], name) for item, parent, name in targets],
                allow_group_file_ids={item.file_id for item in items},
            )
            targets_verified = True
            for index, item in enumerate(items, start=1):
                parent_id = restored[item.original_parent_id]
                step_id = db.add_organize_operation_step(
                    log_id, operation_token, index, "return_to_source",
                    file_id=item.file_id, from_parent_id=item.current_parent_id,
                    from_name=item.current_name, to_parent_id=parent_id,
                    to_name=item.original_name, status="running",
                )
                try:
                    transition = self._apply_transition(
                        item, parent_id, item.original_name, step_id,
                        rename_first=True,
                    )
                    completed.append(transition)
                    db.update_organize_log_item(
                        item.id, current_parent_id=parent_id,
                        current_name=item.original_name, status="success", error="",
                    )
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception as exc:
            self._rollback_transitions(completed)
            db.update_organize_log(
                log_id, status=self._failure_status(log_id, "failed" if targets_verified else detail["status"]), error=str(exc)
            )
            raise
        video = self._video(items)
        db.update_organize_log(
            log_id, status="reverted", operation_type="return_to_source",
            current_parent_id=restored[video.original_parent_id],
            current_name=video.original_name, error="",
        )
        empty_dirs_cleaned, directory_warnings = (
            self._cleanup_archive_directories(
                log_id=log_id,
                directories=cleanup_directories,
                protected=cleanup_protected,
                target_root_id=rules.target_dir_id,
            )
        )
        warnings = [*cleanup_warnings, *directory_warnings]
        warnings.extend(
            self._run_post_actions(items=items, rules=rules, moved=len(items))
        )
        if not cleanup_directories:
            cleanup_status = "retained" if cleanup_warnings else "not_applicable"
        elif empty_dirs_cleaned >= len(cleanup_directories):
            cleanup_status = "cleaned"
        elif empty_dirs_cleaned > 0:
            cleanup_status = "partial"
        else:
            cleanup_status = "retained"
        return {
            "success": True, "log_id": log_id, "item_count": len(items),
            "empty_dirs_cleaned": empty_dirs_cleaned,
            "empty_dir_cleanup_status": cleanup_status,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def revert_latest(self, log_id: int, operation_token: str,
                      expected_version: int) -> dict:
        detail = self.detail(log_id)
        if not detail["allowed_actions"]["revert"]:
            raise ValueError("没有可安全回退的上一版操作快照")
        steps = [dict(row) for row in db.list_latest_reversible_organize_steps(log_id)]
        if not steps:
            raise ValueError("没有可安全回退的操作步骤")
        snapshots = [str(step.get("state_before_json") or "") for step in steps if step.get("state_before_json")]
        if len(set(snapshots)) > 1:
            raise ValueError("最近操作包含不一致的业务前像，必须人工核验")
        try:
            snapshot = json.loads(snapshots[0]) if snapshots else None
        except (TypeError, ValueError) as exc:
            raise ValueError("最近操作的业务前像损坏，必须人工核验") from exc
        legacy_warning = "" if snapshot is not None else "历史步骤缺少业务前像，本次仅回退文件位置和名称；原媒体身份需人工核验"
        items = self._load_items(log_id)
        items_by_file = {item.file_id: item for item in items}
        if {str(step.get("file_id") or "") for step in steps} != set(items_by_file):
            raise ValueError("最近操作的媒体组步骤不完整，禁止只回退部分成员")
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
        if not db.claim_organize_log_operation(
            log_id, operation_token, "reverting", tuple(REVERT_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        completed: list[AppliedTransition] = []
        targets_verified = False
        try:
            restored = self._restore_directory_targets(
                log_id, operation_token, [parent for _item, parent, _name in targets],
            )
            self._verify_targets_available(
                [(item, restored[parent], name) for item, parent, name in targets],
                allow_group_file_ids={item.file_id for item in items},
            )
            targets_verified = True
            if snapshot is not None:
                for state in [snapshot['log'], *snapshot['items']]:
                    parent = state.get('target_parent_id')
                    if parent in restored:
                        state['target_parent_id'] = restored[parent]
            for index, previous in enumerate(reversed(steps), start=1):
                item = items_by_file[str(previous.get("file_id") or "")]
                target_parent = restored[str(previous.get("from_parent_id") or "")]
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
            refreshed_items = self._load_items(log_id)
            video = self._video(refreshed_items)
            finish_fields = {
                "status": "reverted", "operation_type": "revert",
                "current_parent_id": video.current_parent_id, "current_name": video.current_name,
                "error": legacy_warning,
            }
            if snapshot is not None:
                persisted = db.restore_organize_business_snapshot(log_id, snapshot, **finish_fields)
            else:
                # 同一执行器的历史降级：只承诺步骤中可证实的路径逆操作，不猜身份。
                persisted = db.update_organize_log(log_id, **finish_fields)
            if not persisted:
                raise RuntimeError("回退后的业务状态未能持久化")
        except Exception as exc:
            self._rollback_transitions(completed)
            db.update_organize_log(
                log_id, status=self._failure_status(log_id, "revert_failed" if targets_verified else detail["status"]), error=str(exc)
            )
            raise
        rules = OrganizeRules.from_config()
        warnings = ([legacy_warning] if legacy_warning else []) + self._run_post_actions(
            items=refreshed_items, rules=rules, moved=len(completed)
        )
        return {
            "success": True, "log_id": log_id, "item_count": len(completed),
            "warnings": warnings,
        }

    def _prepare_delete_group(
        self, log_id: int, confirm_text: str,
    ) -> list[CorrectionItem]:
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
        return ordered

    def _execute_claimed_delete(
        self, log_id: int, operation_token: str,
        ordered: list[CorrectionItem],
    ) -> dict:
        deleted: set[str] = set()
        error: Exception | None = None
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
                    deleted.add(item.file_id)
                    db.update_organize_log_item(item.id, status="deleted", error="")
                    db.finish_organize_operation_step(step_id, "success")
                except Exception as exc:
                    db.finish_organize_operation_step(step_id, "failed", str(exc))
                    raise
        except Exception as exc:
            error = exc
        warnings: list[str] = []
        rules = OrganizeRules.from_config()
        if deleted and rules.link_strm:
            try:
                cleanup = cleanup_deleted_cloud_files(deleted, allow_emby=rules.emby_refresh)
                warnings = list(cleanup.get("error_samples") or [])
            except Exception as exc:
                logger.warning("日志删除后 STRM 收尾失败 log=%s type=%s", log_id, type(exc).__name__)
                warnings.append("云端文件已移入回收站，但 STRM 本地收尾未完成，请核对后同步")
        if error is not None:
            message = "；".join([str(error), *warnings])
            db.update_organize_log(
                log_id, status="partial_failed" if deleted else "failed", error=message,
            )
            raise RuntimeError(message) from error
        db.update_organize_log(
            log_id, status="deleted", operation_type="delete", error="；".join(warnings)
        )
        return {
            "success": True, "log_id": log_id, "deleted": len(deleted),
            "warnings": warnings,
        }

    def delete_group(self, log_id: int, operation_token: str,
                     expected_version: int, confirm_text: str) -> dict:
        ordered = self._prepare_delete_group(log_id, confirm_text)
        if not db.claim_organize_log_operation(
            log_id, operation_token, "deleting", tuple(DELETE_STATUSES), expected_version,
        ):
            raise RuntimeError("日志状态或版本已变化，请刷新后重试")
        return self._execute_claimed_delete(log_id, operation_token, ordered)



def new_operation_token() -> str:
    return uuid.uuid4().hex
