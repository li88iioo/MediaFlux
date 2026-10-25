"""Telegram 整理候选确认与安全重跑。

通知按钮只携带短 token；源文件快照、候选和整理规则均持久化在 SQLite。
用户确认后仍走 Organizer 的计划、冲突、日志、STRM 与媒体库刷新链路。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from sqlite3 import Row

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile, close_guangya_client
from app.config import get
from app.logger import get_logger
from app.modules.directory_scrape import FixedMatchScraper
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.nsfw import (
    MetaTubeError,
    NsfwRecognizer,
    build_clean_title_candidate,
    extract_nsfw_identifier,
    normalize_code,
)
from app.modules.organize import (
    Organizer,
    OrganizeRules,
    enforce_fixed_organize_rules,
    organize_rules_snapshot,
    organize_rules_snapshot_changes,
    restore_organize_rules_snapshot,
)
from app.modules.scraper import MatchResult, TMDBScraper
from app.modules.telegram_notification_center import (
    deserialize_notification_event,
    serialize_notification_event,
)
from app.notifier import (
    NOTIFICATION_SECTION_BREAK,
    NotificationAction,
    NotificationEvent,
    safe_int,
)

logger = get_logger(__name__)
_CONFIRMATION_TTL_HOURS = 24
_MAX_CANDIDATES = 3
_DISPATCH_POLL_SECONDS = 1.0
_CONFIRMATION_MAINTENANCE_SECONDS = 5 * 60.0
_DELIVERY_LEASE_SECONDS = 120
_DELIVERY_RETRY_SECONDS = (2, 8, 30, 120, 600)
_dispatch_guard = threading.Lock()
_dispatch_stop = threading.Event()
_dispatch_wakeup = threading.Event()
_dispatch_thread: threading.Thread | None = None
_dispatch_accepting = False
_review_guard = threading.Lock()
_review_stop = threading.Event()
_review_wakeup = threading.Event()
_review_thread: threading.Thread | None = None
_review_accepting = False
_rollup_guard = threading.Lock()
_TERMINAL_CONFIRMATION_STATUSES = frozenset({
    "completed", "failed", "expired", "cancelled",
})
_RETRY_SELECTED_INDEX_KEY = "_retry_selected_index"
_NOTIFICATION_SUPPRESSED_KEY = "_notification_suppressed"


class ConfirmationRetryableError(RuntimeError):
    """外部服务瞬时失败；可基于同一快照签发新的显式重试票据。"""


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _confirmation_strm_debounce_seconds(payload: dict) -> int:
    """批量候选使用更长静默窗，合并连续人工确认的 STRM 后处理。"""
    rollup = payload.get("organize_rollup")
    actionable_groups = safe_int(
        rollup.get("actionable_groups") if isinstance(rollup, dict) else 0,
        0,
        minimum=0,
    )
    default_seconds = 30 if actionable_groups > 1 else 8
    configured = (
        get("GY_ORGANIZE_CONFIRM_STRM_DEBOUNCE_SECONDS", "") or ""
    ).strip()
    if not configured:
        return default_seconds
    try:
        return max(0, min(int(float(configured)), 30))
    except (TypeError, ValueError, OverflowError):
        return default_seconds


def _candidate_provider(candidate: dict) -> str:
    provider = str(candidate.get("provider") or "").strip().lower()
    if not provider and str(candidate.get("tmdb_id") or "").strip():
        provider = "tmdb"
    return provider


def _candidate_external_id(candidate: dict) -> str:
    return str(
        candidate.get("external_id") or candidate.get("tmdb_id") or ""
    ).strip()


def _valid_confirmation_candidate(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    media_type = str(candidate.get("media_type") or "").strip().lower()
    if provider == "tmdb":
        return bool(str(candidate.get("tmdb_id") or "").strip() and media_type in {"movie", "tv"})
    if provider in {"metatube", "clean_title"}:
        return bool(external_id and media_type == "movie")
    return False


def semantic_candidate_category(candidate: dict) -> str:
    """把 provider、媒体大类与题材合并成用户可理解的候选身份。"""
    if _candidate_provider(candidate) in {"metatube", "clean_title"}:
        return "成人内容"
    media_type = str(candidate.get("media_type") or "").strip().lower()
    genre_ids = {
        int(value) for value in (candidate.get("genre_ids") or [])
        if str(value).isdigit()
    }
    if media_type == "movie":
        if 16 in genre_ids:
            return "电影 · 动画"
        if 99 in genre_ids:
            return "电影 · 纪录片"
        return "电影"
    if media_type == "tv":
        if 16 in genre_ids:
            return "剧集 · 动漫"
        if 99 in genre_ids:
            return "剧集 · 纪录片"
        if genre_ids.intersection({10763, 10764, 10767}):
            return "剧集 · 综艺"
        return "剧集"
    return "未知类型"


def _candidate_identity_label(candidate: dict) -> str:
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    if provider == "metatube":
        return f"MetaTube {external_id}" if external_id else "MetaTube"
    if provider == "clean_title":
        return f"清洗标题 {external_id}" if external_id else "清洗标题"
    if provider == "tmdb":
        return f"TMDB {external_id}" if external_id else "TMDB"
    return external_id or "未知来源"


def _candidate_display_name(candidate: dict, fallback: str = "待确认媒体") -> str:
    return str(
        candidate.get("title") or _candidate_external_id(candidate) or fallback
    ).strip()


def _safe_label(candidate: dict, index: int) -> str:
    if _candidate_provider(candidate) == "clean_title":
        code = _candidate_external_id(candidate)
        return "清洗标题后入库" + (f" · {code}" if code else "")
    title = _candidate_display_name(candidate, f"候选 {index + 1}")
    if len(title) > 18:
        title = f"{title[:17].rstrip()}…"
    return f"{index + 1}  {title} · {_candidate_identity_label(candidate)}"


def _candidate_summary_lines(group: dict) -> tuple[str, ...]:
    lines: list[str] = []
    for index, candidate in enumerate((group.get("candidates") or [])[:_MAX_CANDIDATES]):
        title = _candidate_display_name(candidate, f"候选 {index + 1}")
        year = str(candidate.get("year") or "").strip()
        score = max(0.0, min(float(candidate.get("score") or 0.0), 1.0))
        support = max(0, int(candidate.get("support") or 0))
        heading = f"{index + 1}. {title}" + (f" ({year})" if year else "")
        identity = (
            f"{_candidate_identity_label(candidate)} · "
            f"{semantic_candidate_category(candidate)} · 匹配 {score:.0%}"
        )
        if support:
            identity += f" · 支持 {support} 个文件"
        lines.append(f"{heading}\n{identity}")
    return tuple(lines)

def _fingerprint(payload: dict) -> str:
    # 父任务/消息绑定只描述通知投影，不属于文件快照身份。重复发布同一
    # 候选时必须继续替代旧按钮，不能因新的汇总 task_id 产生新指纹。
    stable_payload = {
        key: value
        for key, value in payload.items()
        if key not in {
            "organize_task_id", "organize_rollup", "download_request_ids", "_telegram_message_id",
            _RETRY_SELECTED_INDEX_KEY, _NOTIFICATION_SUPPRESSED_KEY,
        }
    }
    encoded = json.dumps(
        stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _confirmation_kind(payload: dict) -> str:
    """旧记录没有 kind；缺省值必须永久保持为光鸭整理。"""
    kind = str(payload.get("kind") or "guangya").strip().lower()
    if kind not in {"guangya", "local_media"}:
        raise ValueError("确认任务类型无效，请重新执行整理")
    return kind


def _recognition_review_is_enabled() -> bool:
    """延迟读取配置，避免整理模块与 Agent Kernel 形成启动期耦合。"""
    try:
        from app.modules.agent_recognition_review import recognition_review_enabled

        return recognition_review_enabled()
    except Exception as exc:  # noqa: BLE001 - 自动复核不可影响人工链路
        logger.warning(
            "读取 Agent 主动复核配置失败 type=%s", type(exc).__name__
        )
        return False


def _clean_review_candidate(candidate: dict) -> bool:
    return str(candidate.get("provider") or "").strip().lower() == "clean_title"


def _validate_agent_clean_authorization(payload: dict, candidate: dict) -> None:
    from app.modules.nsfw_clean_review import (
        inspect_nsfw_clean_candidate,
        nsfw_clean_review_enabled,
    )

    if not _recognition_review_is_enabled() or not nsfw_clean_review_enabled():
        raise DirectoryScrapeConflictError("未授权或已关闭光鸭 NSFW 自动清洗，保留人工确认")
    evidence = inspect_nsfw_clean_candidate(payload, candidate)
    if not evidence["ok"]:
        raise DirectoryScrapeConflictError(evidence["summary"])


def _clean_confirmation_retry_is_current(payload: dict, client) -> bool:
    """只给仍有效的冻结材料签发人工按钮，避免过期快照形成确认死循环。"""
    try:
        rules = OrganizeRules.from_config().for_source(str(payload.get("source_dir_id") or ""))
        if organize_rules_snapshot_changes(payload.get("rules"), rules, files=payload.get("files")):
            return False
        if client is not None:
            for item in (*payload.get("files", []), *payload.get("companions", [])):
                _validate_snapshot(client, item, role="待确认文件")
        return True
    except Exception:  # noqa: BLE001 - 无法证明快照有效时只允许重新扫描。
        return False


class _AgentCleanWriteBoundary:
    """只缩小既有执行器的授权；当前文件已开写后交由原事务补偿完成。"""
    def __init__(self, payload: dict, candidate: dict):
        self.payload, self.candidate = payload, candidate
        self.media_write_attempted = False

    def __call__(self, plan, stage: str, *, target_files=()) -> None:
        _validate_agent_clean_authorization(self.payload, self.candidate)
        source = str(self.payload.get("source_dir_id") or "")
        rules = OrganizeRules.from_config().for_source(source)
        if organize_rules_snapshot_changes(self.payload.get("rules"), rules, files=self.payload.get("files")):
            raise DirectoryScrapeConflictError("来源或整理规则已变化，保留人工确认")
        if plan.action != "move" or plan.conflict_decision not in {"new", "coexist"}:
            raise DirectoryScrapeConflictError("目标存在冲突或需要替换，自动清洗不授权覆盖或回收")
        # 目标中已有本组之外的文件（含孤立字幕/NFO）也交人工，避免移动伴随
        # 文件时触发 provider 隐式同名覆盖；同组已移动数字分段仍允许继续。
        allowed_ids = {
            str(item.get("file_id") or "")
            for item in (*self.payload.get("files", []), *self.payload.get("companions", []))
        }
        if any(str(item.file_id) not in allowed_ids for item in target_files):
            raise DirectoryScrapeConflictError("目标目录已有其他文件，自动清洗转人工核对")
        if stage == "commit":
            self.media_write_attempted = True


def _episode_research_receipt_from_row(row) -> dict | None:
    if row is None:
        return None
    try:
        audit = json.loads(str(row["review_result_json"] or "{}"))
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise DirectoryScrapeConflictError("识别复核审计损坏，请重新执行整理") from exc
    if not isinstance(audit, dict) or audit.get("entry_mode") != "episode_research":
        return None
    receipt = audit.get("episode_research_receipt")
    if not isinstance(receipt, dict):
        raise DirectoryScrapeConflictError("季集研究缺少有效证据回执")
    return receipt


def _episode_research_for_execution(token: str, payload: dict, selected_index: int, actor: str) -> dict | None:
    if actor != "agent":
        return None  # 人工候选按钮不隐式授权使用研究映射。
    row = db.get_organize_confirmation(token)
    receipt = _episode_research_receipt_from_row(row)
    if receipt is None:
        return None
    if (row is None or str(row["confirmation_actor"] or "") != "agent"
            or str(row["status"] or "") != "running"
            or row["selected_index"] != selected_index or str(row["fingerprint"] or "") != _fingerprint(payload)):
        raise DirectoryScrapeConflictError("季集研究执行所有权或文件快照已变化")
    from app.modules.episode_research_service import revalidate_episode_research_receipt
    return revalidate_episode_research_receipt(payload, receipt, expected_candidate_index=selected_index)


class _AgentEpisodeWriteBoundary:
    """研究只授权已验证的新归档位置，不授权替换、删除或越过关闭开关。"""
    def __init__(self, payload: dict, proposal: dict, *, client):
        self.payload = copy.deepcopy(payload)
        self.client = client
        self.media_write_attempted = False
        self.contexts: dict[str, dict] = {}
        files = list(self.payload.get("files") or [])
        self.frozen = {str(item["file_id"]): item for item in (*files, *self.payload.get("companions", []))}
        self.expected = {
            str(files[row["file_index"]]["file_id"]): row for row in proposal["mappings"]
        }

    def bind_target_context(self, plan, target_id: str, *, companions, target_names) -> None:
        if not (target_id or ""):
            raise DirectoryScrapeConflictError("研究计划缺少有效目标目录")
        final_names = [str(name).casefold() for name in target_names]
        if len(set(final_names)) != len(final_names):
            raise DirectoryScrapeConflictError("本次视频与伴随文件目标名相互冲突")
        names = {str(plan.original_name).casefold(), *final_names}
        companion_ids = []
        for item in companions:
            frozen = self.frozen.get(str(item.file_id))
            if frozen is None:
                raise DirectoryScrapeConflictError("伴随文件不属于冻结研究范围")
            _validate_snapshot(self.client, frozen, role="研究伴随文件")
            companion_ids.append(str(item.file_id))
            names.add(str(item.name).casefold())
        self.contexts[str(plan.file_id)] = {"target_id": target_id, "companions": companion_ids, "names": names}

    def _assert_authorized(self, plan) -> None:
        from app.modules.episode_research_service import episode_research_enabled
        if not episode_research_enabled():
            raise DirectoryScrapeConflictError("复杂季集研究已关闭，未授权后续文件变更")
        source = str(self.payload.get("source_dir_id") or "")
        rules = OrganizeRules.from_config().for_source(source)
        if organize_rules_snapshot_changes(self.payload.get("rules"), rules, files=self.payload.get("files")):
            raise DirectoryScrapeConflictError("来源或整理规则已变化，保留人工确认")
        row = self.expected.get(str(plan.file_id))
        if row is None or (plan.season, plan.episode) != (row["target_season"], row["target_episode"]):
            raise DirectoryScrapeConflictError("整理计划与已验证的季集研究映射不一致")
        if (plan.source_season, plan.source_episode) != (row["source_season"], row["source_episode"]):
            raise DirectoryScrapeConflictError("源发布编号已变化，研究映射失效")
        if plan.action != "move" or plan.conflict_decision not in {"new", "coexist"}:
            raise DirectoryScrapeConflictError("研究映射不授权覆盖、替换或删除已有文件")

    def __call__(self, plan, stage: str, *, target_files=()) -> None:
        self._assert_authorized(plan)
        # 始终核对原冻结授权，而不是研究完成后重新扫描得到的新size/etag。
        frozen = self.frozen.get(str(plan.file_id))
        if frozen is None:
            raise DirectoryScrapeConflictError("源视频不属于冻结研究范围")
        _validate_snapshot(self.client, frozen, role="研究视频")
        context = self.contexts.get(str(plan.file_id))
        if stage == "commit":
            if context is None:
                raise DirectoryScrapeConflictError("研究计划缺少写前目标上下文")
            for file_id in context["companions"]:
                _validate_snapshot(self.client, self.frozen[file_id], role="研究伴随文件")
            # commit调用原本不传target_files；必须鲜读，不能沿用同包上一集的库存。
            target_files = self.client.list_dir(context["target_id"])
        allowed_ids = {str(plan.file_id), *(context["companions"] if context else ())}
        # 同剧其他集允许存在；已移动的本包前集也不是本次同名覆盖的豁免对象。
        names = set(context["names"]) if context else {str(plan.new_name or "").casefold()}
        for item in self.payload.get("companions", []):
            if isinstance(item, dict) and str(item.get("video_file_id") or "") == str(plan.file_id):
                names.add(str(item.get("name") or "").casefold())
        stem = str(plan.new_name or "").rsplit(".", 1)[0].casefold()
        if any(str(item.file_id) not in allowed_ids and (
            str(item.name).casefold() in names or (stem and str(item.name).casefold().startswith(stem+"."))
        ) for item in target_files):
            raise DirectoryScrapeConflictError("目标存在同名媒体或伴随文件，研究映射保留人工确认")
        if stage == "commit":
            self.media_write_attempted = True

    def before_companion_write(self, plan, item, target_id: str, target_name: str) -> None:
        self._assert_authorized(plan)
        context = self.contexts.get(str(plan.file_id))
        if context is None or target_id != context["target_id"] or str(item.file_id) not in context["companions"]:
            raise DirectoryScrapeConflictError("伴随文件目标超出冻结研究范围")
        _validate_snapshot(self.client, self.frozen[str(item.file_id)], role="研究伴随文件")
        names = {str(item.name).casefold(), target_name.casefold()}
        if any(str(other.file_id) != str(item.file_id) and str(other.name).casefold() in names
               for other in self.client.list_dir(target_id)):
            raise DirectoryScrapeConflictError("伴随文件写前发现同名目标，未授权覆盖")


def _persist_confirmation_actions(
    payload: dict,
    *,
    chat_id: str = "",
    review_ready: bool = False,
    notification_suppressed: bool = False,
) -> tuple[NotificationAction, ...]:
    persisted_payload = dict(payload)
    if notification_suppressed:
        persisted_payload[_NOTIFICATION_SUPPRESSED_KEY] = True
    candidates = [
        dict(item) for item in (persisted_payload.get("candidates") or [])
    ]
    files = list(persisted_payload.get("files") or [])
    allow_skip_terminal = bool(persisted_payload.get("allow_skip_terminal"))
    if not files or (not candidates and not allow_skip_terminal):
        return ()
    resolved_chat = (chat_id or get("TG_CHAT_ID", "") or "").strip()
    token = secrets.token_urlsafe(12)
    db.create_organize_confirmation(
        token=token,
        fingerprint=_fingerprint(persisted_payload),
        chat_id=resolved_chat,
        source_name=str(persisted_payload.get("source_name") or ""),
        directory_path=str(persisted_payload.get("directory") or "/"),
        payload=persisted_payload,
        expires_at=_timestamp(
            datetime.now(timezone.utc).astimezone()
            + timedelta(hours=_CONFIRMATION_TTL_HOURS)
        ),
        organize_task_id=str(persisted_payload.get("organize_task_id") or ""),
        review_requested=_recognition_review_is_enabled(),
        review_ready=review_ready,
    )
    if review_ready:
        wake_recognition_review_dispatcher()
    actions = [
        NotificationAction(_safe_label(candidate, index), f"orgc:{token}:{index}")
        for index, candidate in enumerate(candidates)
    ]
    if allow_skip_terminal:
        actions.append(NotificationAction("跳过此组", f"orgc:{token}:skip"))
    else:
        actions.append(NotificationAction("暂不处理", f"orgc:{token}:cancel"))
    return tuple(actions)


def _persist_confirmation_retry(
    payload: dict,
    *,
    selected_index: int,
    chat_id: str,
) -> tuple[str, NotificationAction]:
    """基于冻结 payload 和既有选择签发一张新的单次重试票据。"""
    candidates = list(payload.get("candidates") or [])
    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("候选参数无效")
    candidate = dict(candidates[selected_index])
    retry_payload = dict(payload)
    retry_payload[_RETRY_SELECTED_INDEX_KEY] = selected_index
    resolved_chat = (chat_id or get("TG_CHAT_ID", "") or "").strip()
    retry_token = secrets.token_urlsafe(12)
    db.create_organize_confirmation(
        token=retry_token,
        fingerprint=_fingerprint(retry_payload),
        chat_id=resolved_chat,
        source_name=str(retry_payload.get("source_name") or ""),
        directory_path=str(retry_payload.get("directory") or "/"),
        payload=retry_payload,
        expires_at=_timestamp(
            datetime.now(timezone.utc).astimezone()
            + timedelta(hours=_CONFIRMATION_TTL_HOURS)
        ),
        organize_task_id=str(retry_payload.get("organize_task_id") or ""),
    )
    return retry_token, NotificationAction(
        f"重新尝试 · {_safe_label(candidate, selected_index)}",
        f"orgc:{retry_token}:{selected_index}",
    )


def confirmation_token_from_event(event: NotificationEvent) -> str:
    """从任一人工确认按钮提取持久化 token。"""
    for action in event.actions:
        parts = (action.callback_data or "").split(":", 2)
        if len(parts) == 3 and parts[0] == "orgc" and parts[1]:
            return parts[1]
    return ""


def publish_confirmation_event(
    event: NotificationEvent,
    *,
    chat_id: str = "",
    token: str = "",
    message_id: int | None = None,
    terminal: bool = False,
    error: bool = False,
    deliver_now: bool = True,
    expected_confirmation_delivery: tuple[int, int] | None = None,
) -> bool:
    """把候选卡及其终态写入同一个可靠 Telegram 消息线程。"""
    from app.modules.telegram_notification_center import publish_notification_thread
    from app.modules.telegram_notification_policy import (
        NotificationImportance,
        NotificationTopic,
    )

    resolved_token = (token or confirmation_token_from_event(event)).strip()
    if not resolved_token:
        return False
    importance = (
        NotificationImportance.ERROR if error else
        NotificationImportance.RESULT if terminal else
        NotificationImportance.ACTION
    )
    try:
        result = publish_notification_thread(
            f"confirmation:{resolved_token}",
            event,
            topic=NotificationTopic.CONFIRMATION,
            importance=importance,
            chat_id=chat_id,
            preferred_message_id=(message_id or 0),
            deliver_now=deliver_now,
            expected_confirmation_delivery=expected_confirmation_delivery,
        )
    finally:
        if not terminal:
            try:
                if db.activate_organize_confirmation_review(resolved_token):
                    wake_recognition_review_dispatcher()
            except Exception as exc:  # noqa: BLE001 - 自动复核不得破坏人工通知
                logger.warning(
                    "候选卡发布后激活 Agent 复核失败 token=%s type=%s",
                    resolved_token[:6],
                    type(exc).__name__,
                )
    return bool(result)


def _terminal_status_label(value: object, *, media_library: bool = False) -> str:
    """为确认卡的后续状态补充稳定、不过度重复的终态提示。"""
    text = str(value or "").strip()
    if not text or any(marker in text for marker in ("✅", "❌", "⚠️", "⏳", "⏭️", "🎯")):
        return text
    if any(marker in text for marker in ("失败", "错误", "未完成")):
        return f"{text} ❌"
    if any(marker in text for marker in ("部分", "警告", "需处理")):
        return f"{text} ⚠️"
    if any(marker in text for marker in ("排队", "等待", "运行中", "同步中")):
        return f"{text} ⏳"
    if "跳过" in text:
        return f"{text} ⏭️"
    if any(marker in text for marker in ("完成", "成功", "已刷新")):
        return f"{text} {'🎯' if media_library else '✅'}"
    return text


def update_confirmation_lifecycle_downstream(
    token: str,
    *,
    chat_id: str = "",
    strm_status: str,
    media_refresh: str,
    partial: bool = False,
    error: str = "",
) -> bool:
    """在人工确认卡的同一消息上补齐 STRM 与媒体库终态。"""
    from app.modules.telegram_notification_center import get_notification_thread_event
    from app.modules.telegram_notification_policy import NotificationTopic

    thread_key = f"confirmation:{(token or '').strip()}"
    previous = get_notification_thread_event(
        thread_key, topic=NotificationTopic.CONFIRMATION, chat_id=chat_id,
    )
    if previous is None:
        return False
    fields = list(previous.fields)
    updates = (
        (("STRM 状态", "STRM"), "STRM 状态", _terminal_status_label(strm_status)),
        (("媒体库刷新", "媒体库"), "媒体库刷新", _terminal_status_label(
            media_refresh, media_library=True,
        )),
    )
    for aliases, label, value in updates:
        replaced = False
        for index, (current_label, _current_value) in enumerate(fields):
            if str(current_label) in aliases:
                fields[index] = (label, value)
                replaced = True
                break
        if not replaced:
            fields.append((label, value))
    row = db.get_organize_confirmation(token)
    keep_actions = (row is not None and str(row["status"] or "") == "pending")
    title = "⚠️ 人工确认整理链路部分完成" if partial or error else previous.title
    footer = str(error or previous.footer)[:300]
    event = NotificationEvent(
        title, fields=tuple(fields), lines=previous.lines, footer=footer,
        actions=previous.actions if keep_actions else (),
        layout=previous.layout, field_emojis=previous.field_emojis,
    )
    return publish_confirmation_event(
        event, chat_id=chat_id, token=token, terminal=True,
        error=bool(partial or error),
    )


def create_confirmation_actions(
    group: dict,
    rules: OrganizeRules,
    *,
    source_name: str = "",
    chat_id: str = "",
    review_ready: bool = False,
    notification_suppressed: bool = False,
) -> tuple[NotificationAction, ...]:
    """持久化候选组；无元数据时仍返回可终结待确认状态的跳过按钮。"""
    candidates = [
        dict(item) for item in (group.get("candidates") or [])[:_MAX_CANDIDATES]
        if _valid_confirmation_candidate(item)
    ]
    files = [dict(item) for item in (group.get("files") or [])]
    if not files:
        return ()
    group_rules = group.get("rules")
    if isinstance(group_rules, dict):
        effective_rules = restore_organize_rules_snapshot(
            group_rules, trusted_rules=rules,
        )
    else:
        effective_rules = enforce_fixed_organize_rules(OrganizeRules(**asdict(rules)))
    payload = {
        "version": 2,
        "allow_skip_terminal": True,
        "source_dir_id": str(group.get("source_dir_id") or ""),
        "source_name": str(source_name or group.get("source_name") or ""),
        "directory": str(group.get("directory") or "/"),
        "source_parent_id": str(group.get("source_parent_id") or "0"),
        "identity": str(group.get("identity") or ""),
        "reason": str(group.get("reason") or ""),
        "multipart_strategy": str(group.get("multipart_strategy") or ""),
        "files": files,
        "companions": [dict(item) for item in (group.get("companions") or [])],
        "candidates": candidates,
        "rules": organize_rules_snapshot(effective_rules),
    }
    organize_task_id = str(group.get("organize_task_id") or "").strip()
    organize_rollup = group.get("organize_rollup")
    if organize_task_id:
        payload["organize_task_id"] = organize_task_id
    if isinstance(organize_rollup, dict):
        payload["organize_rollup"] = dict(organize_rollup)
    if "download_request_ids" in group:
        request_ids = group["download_request_ids"]
        if (not isinstance(request_ids, list) or not request_ids or len(request_ids) > 100
                or any(type(item) is not int or item <= 0 for item in request_ids)):
            raise ValueError("下载请求关联无效")
        payload["download_request_ids"] = list(dict.fromkeys(request_ids))
    return _persist_confirmation_actions(
        payload,
        chat_id=chat_id,
        review_ready=review_ready,
        notification_suppressed=notification_suppressed,
    )


def create_local_media_confirmation_actions(
    task,
    source,
    preview: dict,
    *,
    owner: str = "admin",
    chat_id: str = "",
    review_ready: bool = False,
    notification_suppressed: bool = False,
) -> tuple[NotificationAction, ...]:
    """为本地待确认任务生成与光鸭相同协议的 TG 候选按钮。"""
    if task is None or source is None or str(getattr(task, "status", "")) != "requires_manual":
        return ()
    reason = str(preview.get("reason") or getattr(task, "error", "") or "").strip()
    raw_candidates = list(preview.get("candidates") or [])
    if not raw_candidates and isinstance(preview.get("candidate"), dict):
        raw_candidates = [preview["candidate"]]
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        media_type = str(candidate.get("media_type") or "").strip().lower()
        provider = str(candidate.get("provider") or "tmdb").strip().lower()
        key = (tmdb_id, media_type)
        if (
            not tmdb_id
            or media_type not in {"movie", "tv"}
            or provider != "tmdb"
            or key in seen
        ):
            continue
        seen.add(key)
        candidate["tmdb_id"] = tmdb_id
        candidate["media_type"] = media_type
        try:
            candidate["score"] = float(
                candidate.get("score", candidate.get("confidence", 0.0)) or 0.0
            )
        except (TypeError, ValueError):
            candidate["score"] = 0.0
        candidates.append(candidate)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    if not candidates:
        return ()
    if (
        candidates[0].get("media_type") == "tv"
        and "缺少集数" in reason
        and getattr(task, "episode_override", None) is None
    ):
        return ()

    def safe_name(value: object) -> str:
        text = str(value or "").strip().replace("\\", "/")
        return text.rsplit("/", 1)[-1] if text else ""

    files = []
    for item in list(preview.get("files") or []):
        if not isinstance(item, dict):
            continue
        name = safe_name(item.get("name"))
        if name:
            file_item = {"name": name}
            if getattr(task, "season_override", None) is not None:
                file_item["season"] = task.season_override
            if getattr(task, "episode_override", None) is not None:
                file_item["episode"] = task.episode_override
            files.append(file_item)
    if not files:
        name = safe_name(getattr(task, "content_path", ""))
        if name:
            file_item = {"name": name}
            if getattr(task, "season_override", None) is not None:
                file_item["season"] = task.season_override
            if getattr(task, "episode_override", None) is not None:
                file_item["episode"] = task.episode_override
            files.append(file_item)
    if not files:
        return ()
    expected_digest = str(
        preview.get("snapshot_digest") or getattr(task, "snapshot_digest", "") or ""
    ).strip()
    rules_snapshot = str(
        preview.get("rules_snapshot") or getattr(task, "rules_snapshot", "") or ""
    ).strip()
    if not expected_digest or not rules_snapshot:
        return ()
    payload = {
        "version": 1,
        "kind": "local_media",
        "owner": (owner or "admin"),
        "local_task_id": int(task.id),
        "local_task_version": int(task.version),
        "local_source_id": int(task.source_id),
        "source_name": str(getattr(source, "name", "") or "本地媒体"),
        "directory": safe_name(getattr(task, "content_path", "")) or "本地媒体",
        "reason": reason,
        "files": files,
        "candidates": candidates,
        "rules_snapshot": rules_snapshot,
        "snapshot_digest": expected_digest,
        "season_override": getattr(task, "season_override", None),
        "episode_override": getattr(task, "episode_override", None),
        "numbering_mode": str(getattr(task, "numbering_mode", "auto") or "auto"),
    }
    return _persist_confirmation_actions(
        payload,
        chat_id=chat_id,
        review_ready=review_ready,
        notification_suppressed=notification_suppressed,
    )


def schedule_guangya_recognition_reviews(
    stats: dict,
    rules: OrganizeRules,
    *,
    source_name: str = "",
    chat_id: str = "",
) -> int:
    """在通知关闭时也持久化 Agent 复核；人工日志仍保持原有待确认状态。"""
    if not _recognition_review_is_enabled():
        return 0
    groups, _actionable_count = Organizer._validated_task_confirmation_groups(stats)
    scheduled = 0
    for group in groups:
        if not list(group.get("candidates") or []):
            continue
        try:
            actions = create_confirmation_actions(
                group,
                rules,
                source_name=source_name,
                chat_id=chat_id,
                review_ready=True,
                notification_suppressed=True,
            )
        except Exception as exc:  # noqa: BLE001 - 单组失败不能阻断整理终态
            logger.warning(
                "光鸭 Agent 复核任务创建失败 type=%s", type(exc).__name__
            )
            continue
        if actions:
            scheduled += 1
    return scheduled


def schedule_local_media_recognition_review(
    task,
    source,
    preview: dict,
    *,
    owner: str = "admin",
    chat_id: str = "",
) -> bool:
    """为无通知或静默本地任务创建同一冻结确认记录。"""
    if not _recognition_review_is_enabled():
        return False
    return bool(
        create_local_media_confirmation_actions(
            task,
            source,
            preview,
            owner=owner,
            chat_id=chat_id,
            review_ready=True,
            notification_suppressed=True,
        )
    )


def _decode_row(row) -> dict:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError("确认任务数据损坏，请重新执行整理") from exc
    if not isinstance(payload, dict):
        raise ValueError("确认任务数据损坏，请重新执行整理")
    return payload


def _finalize_guangya_manual_logs(
    payload: dict, *, status: str, error: str, confirmation_actor: str = "human",
) -> None:
    """同步确认终态到光鸭整理时间线；本地媒体使用自身任务状态机。"""
    try:
        if _confirmation_kind(payload) != "guangya":
            return
        file_ids = [
            str(item.get("file_id") or "").strip()
            for item in (payload.get("files") or [])
            if isinstance(item, dict) and str(item.get("file_id") or "").strip()
        ]
        if file_ids:
            db.finalize_pending_organize_logs(
                "guangya",
                file_ids,
                status=status,
                error=error,
                confirmation_actor=confirmation_actor,
            )
    except Exception as exc:
        # 确认操作的终态已经持久化，日志投影同步失败只能降级告警，
        # 不能让用户收到“取消/失败处理失败”的错误回执。
        logger.warning(
            "同步人工确认日志终态失败 status=%s type=%s",
            status,
            type(exc).__name__,
        )


def _decode_json_object(value: object) -> dict:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _latest_confirmation_rows(rows: list) -> list:
    """同一父任务重复投递时只统计每个文件快照的最新候选卡。"""
    latest: dict[str, Row] = {}
    for row in rows:
        fingerprint = str(row["fingerprint"] or "").strip()
        key = fingerprint or "token:" + str(row["token"] or "")
        current = latest.get(key)
        if current is None or int(row["id"]) > int(current["id"]):
            latest[key] = row
    return sorted(latest.values(), key=lambda item: int(item["id"]))


def _confirmation_rollup(rows: list) -> tuple[dict, dict] | None:
    """在全部候选进入终态后，生成一次父任务汇总增量。"""
    latest_rows = _latest_confirmation_rows(rows)
    if not latest_rows:
        return None

    baseline: dict = {}
    payloads: dict[int, dict] = {}
    for row in reversed(latest_rows):
        payload = _decode_json_object(row["payload_json"])
        payloads[int(row["id"])] = payload
        candidate = payload.get("organize_rollup")
        if not baseline and isinstance(candidate, dict):
            baseline = dict(candidate)
    if safe_int(baseline.get("version"), 0, minimum=0) != 1:
        return None

    expected_groups = safe_int(
        baseline.get("actionable_groups"), 0, minimum=0,
    )
    if expected_groups <= 0 or len(latest_rows) < expected_groups:
        return None
    # 一个 task_id 正常只发布一批候选。若兼容调用重复投递了更多记录，
    # 以最新基线对应的最后一批为准，避免旧卡重复计数。
    if len(latest_rows) > expected_groups:
        latest_rows = latest_rows[-expected_groups:]
    if any(
        str(row["status"] or "") not in _TERMINAL_CONFIRMATION_STATUSES
        for row in latest_rows
    ):
        return None

    outcomes = {
        "groups": len(latest_rows),
        "resolved_files": 0,
        "moved": 0,
        "metadata": 0,
        "skipped": 0,
        "failed": 0,
        "expired": 0,
    }
    for row in latest_rows:
        payload = payloads.get(int(row["id"])) or _decode_json_object(
            row["payload_json"]
        )
        file_count = len([
            item for item in (payload.get("files") or [])
            if isinstance(item, dict)
        ])
        outcomes["resolved_files"] += file_count
        status = str(row["status"] or "")
        result = _decode_json_object(row["result_json"])
        if status == "completed":
            moved = safe_int(result.get("moved"), 0, minimum=0)
            skipped = safe_int(result.get("skipped"), 0, minimum=0)
            failed = safe_int(result.get("failed"), 0, minimum=0)
            unresolved = safe_int(result.get("need_confirm"), 0, minimum=0)
            outcomes["moved"] += moved
            outcomes["metadata"] += safe_int(
                result.get("metadata_moved"), 0, minimum=0,
            )
            outcomes["skipped"] += skipped
            outcomes["failed"] += failed
            # 防御旧版本/异常执行留下的“completed + need_confirm”记录：
            # 候选卡已经终结，剩余文件不能从父汇总中凭空消失，应按未解决
            # 失败显式计入。正常新链路会在写入前阻止这种状态产生。
            unresolved_capacity = max(0, file_count - moved - skipped - failed)
            outcomes["failed"] += min(unresolved, unresolved_capacity)
        elif status == "cancelled":
            outcomes["skipped"] += file_count
        elif status == "expired":
            outcomes["expired"] += file_count
        else:
            outcomes["failed"] += file_count
    return baseline, outcomes


def _maybe_update_parent_organize_rollup(
    organize_task_id: str, *, chat_id: str = "",
) -> bool:
    """幂等收口一条原始 /organize 汇总；未全部结束时保持不动。"""
    parent_id = (organize_task_id or "").strip()
    if not parent_id:
        return False
    with _rollup_guard:
        rows = db.list_organize_confirmations_for_task(
            parent_id, chat_id=(chat_id or ""),
        )
        if not rows or not any(not bool(row["rollup_applied"]) for row in rows):
            return False
        rollup = _confirmation_rollup(rows)
        if rollup is None:
            return False
        baseline, outcomes = rollup
        from app.modules.telegram_organize_lifecycle import (
            update_organize_lifecycle_confirmations,
        )

        accepted = bool(update_organize_lifecycle_confirmations(
            parent_id,
            chat_id=(chat_id or ""),
            baseline=baseline,
            outcomes=outcomes,
        ))
        if accepted:
            db.mark_organize_confirmation_rollup_applied(
                parent_id, chat_id=(chat_id or ""),
            )
        return accepted


def _reconcile_unapplied_parent_rollups(*, limit: int = 50) -> int:
    updated = 0
    for row in db.list_unapplied_organize_confirmation_tasks(limit=limit):
        try:
            if _maybe_update_parent_organize_rollup(
                str(row["organize_task_id"] or ""),
                chat_id=str(row["chat_id"] or ""),
            ):
                updated += 1
        except Exception as exc:  # noqa: BLE001 - 单个父汇总失败不能中断维护循环。
            logger.warning(
                "人工确认父汇总恢复失败 task=%s type=%s",
                str(row["organize_task_id"] or "")[:32],
                type(exc).__name__,
            )
    return updated


def _expired_confirmation_event(payload: dict, row) -> NotificationEvent:
    directory = str(
        payload.get("directory") or row["directory_path"] or "/"
    )
    file_count = len([
        item for item in (payload.get("files") or []) if isinstance(item, dict)
    ])
    return NotificationEvent(
        "⌛ 人工确认已过期",
        fields=(
            ("目标媒体", payload.get("identity") or "待确认媒体"),
            ("所在目录", directory),
            NOTIFICATION_SECTION_BREAK,
            ("涉及文件", f"{file_count} 个视频"),
            ("处理状态", "候选有效期已结束，文件保持原位"),
            ("附带说明", "重新执行整理时会再次尝试识别。"),
        ),
        layout="relaxed",
    )


def _publish_expired_confirmation(row) -> bool:
    payload = _decode_json_object(row["payload_json"])
    chat_id = str(row["chat_id"] or "")
    token = str(row["token"] or "")
    event = _expired_confirmation_event(payload, row)
    delivery_enabled = _confirmation_delivery_enabled(payload)
    expired = db.expire_organize_confirmation_with_delivery(
        token,
        event_json=serialize_notification_event(event),
        chat_id=chat_id,
        message_id=_confirmation_message_id(payload),
        enqueue_delivery=delivery_enabled,
    )
    if expired is None:
        return False
    if delivery_enabled:
        _dispatch_due_confirmation_delivery(token)
    _finalize_guangya_manual_logs(
        payload,
        status="skipped",
        error="人工确认已过期",
        confirmation_actor="",
    )
    _maybe_update_parent_organize_rollup(
        str(row["organize_task_id"] or ""), chat_id=chat_id,
    )
    return True


def _expire_due_pending_confirmations(
    *, token: str = "", limit: int = 100,
) -> int:
    rows = db.list_due_pending_organize_confirmations(token=token, limit=limit)
    expired = 0
    for row in rows:
        try:
            expired += int(_publish_expired_confirmation(row))
        except Exception as exc:  # noqa: BLE001 - 单卡投影失败不能阻塞其它过期项。
            logger.warning(
                "人工确认过期收口失败 token=%s type=%s",
                str(row["token"] or "")[:6],
                type(exc).__name__,
            )
    return expired


def _raise_if_confirmation_expired(row) -> None:
    if (
        str(row["status"] or "") == "pending"
        and str(row["expires_at"] or "") <= db.now()
    ):
        _expire_due_pending_confirmations(
            token=str(row["token"] or ""), limit=1,
        )
        raise ValueError("确认操作已过期，请重新执行整理")


def _run_confirmation_maintenance() -> bool:
    """低频处理未点击过期与进程中断后尚未回写的父汇总。"""
    expired = _expire_due_pending_confirmations(limit=100)
    _reconcile_unapplied_parent_rollups(limit=50)
    return expired >= 100


def cancel_confirmation(
    token: str, *, chat_id: str, message_id: int | str | None = None
) -> dict:
    current = db.get_organize_confirmation(token)
    if current is None:
        raise ValueError("确认操作不存在或已失效")
    expected_chat = str(current["chat_id"] or "")
    if expected_chat and expected_chat != (chat_id or ""):
        raise ValueError("确认操作不存在或已失效")
    _raise_if_confirmation_expired(current)
    payload = _decode_row(current)
    directory = str(current["directory_path"] or "/")
    try:
        resolved_message_id = int(message_id or 0)
    except (TypeError, ValueError):
        resolved_message_id = 0
    terminal_event = NotificationEvent(
        "⏸️ 已暂不处理",
        fields=(
            ("所在目录", directory),
            NOTIFICATION_SECTION_BREAK,
            ("处理状态", "文件保持原位（本次待确认状态已结束）"),
            ("附带说明", "需要时可重新执行整理生成新候选。"),
        ),
        layout="relaxed",
    )
    delivery_enabled = _confirmation_delivery_enabled(payload)
    db.cancel_organize_confirmation(
        token,
        chat_id=chat_id,
        event_json=serialize_notification_event(terminal_event),
        message_id=resolved_message_id or None,
        enqueue_delivery=delivery_enabled,
        resolution="deferred",
    )
    if delivery_enabled:
        _dispatch_due_confirmation_delivery(token)
    _finalize_guangya_manual_logs(
        payload, status="skipped", error="用户选择暂不处理",
    )
    try:
        _maybe_update_parent_organize_rollup(
            str(current["organize_task_id"] or ""), chat_id=(chat_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 终态已落库，汇总投影只做补偿。
        logger.warning(
            "人工确认暂不处理后父汇总更新失败 token=%s type=%s",
            token[:6], type(exc).__name__,
        )
    return {"cancelled": True, "directory": directory}


def skip_confirmation(
    token: str, *, chat_id: str, message_id: int | str | None = None
) -> dict:
    """显式跳过无可用元数据的光鸭待确认组，并同步结束日志状态。"""
    current = db.get_organize_confirmation(token)
    if current is None:
        raise ValueError("确认操作不存在或已失效")
    expected_chat = str(current["chat_id"] or "")
    if expected_chat and expected_chat != (chat_id or ""):
        raise ValueError("确认操作不存在或已失效")
    _raise_if_confirmation_expired(current)
    payload = _decode_row(current)
    if _confirmation_kind(payload) != "guangya" or not bool(
        payload.get("allow_skip_terminal")
    ):
        raise ValueError("该确认操作不支持跳过")
    directory = str(current["directory_path"] or "/")
    try:
        resolved_message_id = int(message_id or 0)
    except (TypeError, ValueError):
        resolved_message_id = 0
    terminal_event = NotificationEvent(
        "⏭️ 跳过待确认项",
        fields=(
            ("目标媒体", payload.get("identity") or "未识别媒体"),
            ("所在目录", directory),
            NOTIFICATION_SECTION_BREAK,
            ("涉及文件", f"{len(payload.get('files') or [])} 个视频"),
            ("处理状态", "文件保持原位（本次待确认状态已结束）"),
            ("附带说明", "以后重新执行整理时仍会再次尝试识别。"),
        ),
        layout="relaxed",
    )
    delivery_enabled = _confirmation_delivery_enabled(payload)
    db.cancel_organize_confirmation(
        token,
        chat_id=chat_id,
        event_json=serialize_notification_event(terminal_event),
        message_id=resolved_message_id or None,
        enqueue_delivery=delivery_enabled,
        resolution="skipped",
    )
    reason = "用户选择跳过：暂无可用元数据"
    _finalize_guangya_manual_logs(payload, status="skipped", error=reason)
    if delivery_enabled:
        _dispatch_due_confirmation_delivery(token)
    try:
        _maybe_update_parent_organize_rollup(
            str(current["organize_task_id"] or ""), chat_id=(chat_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 终态已落库，汇总投影只做补偿。
        logger.warning(
            "人工确认跳过后父汇总更新失败 token=%s type=%s",
            token[:6], type(exc).__name__,
        )
    return {"skipped": True, "directory": directory}


def _confirmation_result(row, payload: dict, candidate: dict) -> dict:
    status = str(row["status"])
    queue_position = (
        db.get_organize_confirmation_queue_position(int(row["id"]))
        if status == "queued" else 0
    )
    return {
        "task_id": str(row["task_id"] or f"queue-{int(row['id']):06d}"),
        "candidate": candidate,
        "directory": str(
            payload.get("directory") or payload.get("source_name") or "待确认媒体"
        ),
        "file_count": len(payload.get("files") or []),
        "scope_summary": Organizer._confirmation_scope_summary(payload),
        "source_name": str(payload.get("source_name") or ""),
        "media_type": str(candidate.get("media_type") or ""),
        "status": status,
        "queue_position": queue_position,
    }


def _confirmation_selected_event(row, payload: dict, candidate: dict) -> NotificationEvent:
    """随确认认领持久化接纳状态，所有排队项及时清除候选按钮。"""
    identity_label = {"metatube": "MetaTube", "clean_title": "清洗标题"}.get(
        str(candidate.get("provider") or "").strip().lower(), "TMDB",
    )
    return NotificationEvent(
        f"✅ 已选择：{_candidate_display_name(candidate, '')}",
        fields=(
            ("目标媒体", _candidate_display_name(candidate, "")),
            ("类型", semantic_candidate_category(candidate)),
            (identity_label, str(candidate.get("external_id") or candidate.get("tmdb_id") or "")),
            ("涉及文件", Organizer._confirmation_scope_summary(payload)),
            ("存储来源", str(payload.get("source_name") or payload.get("directory") or "/")),
            NOTIFICATION_SECTION_BREAK,
            ("队列", str(row["task_id"] or f"queue-{int(row['id']):06d}")),
            ("处理状态", "已接纳，尚未完成"),
        ),
        footer="可以继续选择其他待确认媒体，系统会按点击顺序串行整理。",
        layout="relaxed",
    )


def _selected_candidate(row) -> tuple[dict, dict, int]:
    payload = _decode_row(row)
    candidates = list(payload.get("candidates") or [])
    selected_index = int(row["selected_index"] if row["selected_index"] is not None else -1)
    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("候选参数无效")
    return payload, dict(candidates[selected_index]), selected_index


def _dispatch_confirmation_token(token: str) -> dict:
    """尝试领取并启动一个排队任务；统一写锁繁忙时原样放回队列。"""
    row = db.claim_queued_organize_confirmation(token)
    if row is None:
        return {"ok": False, "claimed": False}
    try:
        payload, candidate, selected_index = _selected_candidate(row)
    except Exception as exc:
        message = "确认任务数据损坏，请重新执行整理"
        raw_payload = _decode_json_object(row["payload_json"])
        delivery_enabled = _confirmation_delivery_enabled(raw_payload)
        failure_event = NotificationEvent(
            "❌ Telegram 确认整理失败",
            fields=(
                ("所在目录", str(row["directory_path"] or "/")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer="请重新执行整理生成新候选。",
            layout="relaxed",
        )
        chat_id = str(row["chat_id"] or "")
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=None,
            retryable=False,
            enqueue_delivery=delivery_enabled,
        )
        if delivery_enabled:
            _dispatch_due_confirmation_delivery(token)
        logger.warning(
            "Telegram 排队整理数据损坏 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
        try:
            _maybe_update_parent_organize_rollup(
                str(row["organize_task_id"] or ""), chat_id=chat_id,
            )
        except Exception as rollup_exc:  # noqa: BLE001 - 不覆盖已持久化失败终态。
            logger.warning(
                "损坏确认任务父汇总更新失败 token=%s type=%s",
                token[:6], type(rollup_exc).__name__,
            )
        return {"ok": False, "claimed": True, "terminal": True, "error": message}

    from app.modules.organize_tasks import get_organize_manager

    reference = str(
        payload.get("directory") or payload.get("source_name") or "待确认媒体"
    )
    chat_id = str(row["chat_id"] or "")
    actor = str(row["confirmation_actor"] or "human").strip().lower()
    if actor not in {"human", "agent"}:
        actor = "human"
    try:
        task = get_organize_manager().start_operation(
            "Agent 确认整理" if actor == "agent" else "Telegram 确认整理",
            reference,
            lambda: _execute_confirmation(
                token, payload, candidate,
                selected_index=selected_index, chat_id=chat_id, actor=actor,
            ),
        )
    except Exception as exc:
        db.requeue_organize_confirmation(token, str(exc or "整理任务提交失败"))
        logger.warning(
            "Telegram 排队整理提交异常 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
        return {"ok": False, "claimed": True, "busy": True, "error": str(exc)}

    if not task.get("ok"):
        error = str(task.get("error") or "统一整理队列暂时繁忙")
        db.requeue_organize_confirmation(token, error)
        return {"ok": False, "claimed": True, "busy": True, "error": error}

    task_id = str(row["task_id"] or task.get("task_id") or "")
    return {
        "ok": True,
        "claimed": True,
        "task_id": task_id,
        "worker_task_id": str(task.get("task_id") or ""),
    }


def _dispatch_next_queued_confirmation() -> dict:
    row = db.get_next_queued_organize_confirmation()
    if row is None:
        return {"ok": False, "idle": True}
    return _dispatch_confirmation_token(str(row["token"] or ""))


def _delivery_timestamp(delay_seconds: int = 0) -> str:
    return _timestamp(datetime.now() + timedelta(seconds=delay_seconds))


def _dispatch_due_confirmation_delivery(token: str = "") -> bool:
    current = _delivery_timestamp()
    stale_before = _delivery_timestamp(-_DELIVERY_LEASE_SECONDS)
    item = db.claim_due_organize_confirmation_delivery(
        current_time=current, stale_before=stale_before, token=token
    )
    if item is None:
        return False

    delivery_id = int(item["id"])
    generation = int(item["lease_generation"])
    try:
        event = deserialize_notification_event(item["event_json"])
    except ValueError:
        event = NotificationEvent(
            "❌ Telegram 整理结果回执异常",
            footer="回执内容读取失败，请在日志记录中核对本次整理结果。",
            layout="relaxed",
        )
    chat_id = str(item["chat_id"] or "")
    token = str(item["confirmation_token"] or "").strip()
    confirmation = db.get_organize_confirmation(token)
    terminal = confirmation is None or confirmation["status"] not in {"queued", "running"}
    try:
        # 接纳与终态复用同一回执；移交事务校验租约代数，迟到的接纳不能
        # 覆盖更晚的终态。发送、编辑与 message_id 仍由统一 outbox 管理。
        accepted = publish_confirmation_event(
            event,
            chat_id=chat_id,
            token=token,
            message_id=item["message_id"],
            terminal=terminal,
            error=str(event.title).startswith(("❌", "⚠️")),
            deliver_now=terminal,
            expected_confirmation_delivery=(delivery_id, generation),
        )
    except Exception as exc:
        accepted = False
        logger.warning(
            "Telegram 整理回执移交失败 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
    if accepted:
        completed = db.complete_organize_confirmation_delivery(
            delivery_id, expected_lease_generation=generation, sent_at=current
        )
        if not completed:
            logger.info(
                "Telegram 整理回执已移交，但投递租约已变化 token=%s",
                token[:6],
            )
        return True

    attempts = max(0, int(item["attempts"] or 0))
    delay = _DELIVERY_RETRY_SECONDS[min(attempts, len(_DELIVERY_RETRY_SECONDS) - 1)]
    db.retry_organize_confirmation_delivery(
        delivery_id,
        expected_lease_generation=generation,
        next_attempt_at=_delivery_timestamp(delay),
        error="UnifiedNotificationHandoffFailed",
    )
    return True


def _confirmation_dispatch_loop(
    stop_event: threading.Event | None = None,
    wakeup_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or _dispatch_stop
    wakeup_event = wakeup_event or _dispatch_wakeup
    next_maintenance_at = 0.0
    while not stop_event.is_set():
        try:
            # stop() 可能在 while 条件检查后立刻触发；查询前再次确认，
            # 避免测试库/应用资源已经开始释放时仍访问 SQLite。
            if stop_event.is_set():
                break
            now_monotonic = time.monotonic()
            if now_monotonic >= next_maintenance_at:
                has_expiry_backlog = _run_confirmation_maintenance()
                next_maintenance_at = (
                    now_monotonic
                    if has_expiry_backlog
                    else now_monotonic + _CONFIRMATION_MAINTENANCE_SECONDS
                )
            if _dispatch_due_confirmation_delivery():
                continue
            row = db.get_next_queued_organize_confirmation()
            if row is None:
                wakeup_event.wait(2.0)
                wakeup_event.clear()
                continue

            if stop_event.is_set():
                break
            token = str(row["token"] or "")
            result = _dispatch_confirmation_token(token)
            if result.get("ok"):
                while not stop_event.is_set():
                    current = db.get_organize_confirmation(token)
                    if current is None or str(current["status"] or "") != "running":
                        break
                    _dispatch_due_confirmation_delivery()
                    wakeup_event.wait(_DISPATCH_POLL_SECONDS)
                    wakeup_event.clear()
                continue
        except Exception as exc:
            logger.error(
                "Telegram 整理确认队列调度异常 type=%s",
                type(exc).__name__,
                exc_info=True,
            )

        wakeup_event.wait(_DISPATCH_POLL_SECONDS)
        wakeup_event.clear()


def _process_recognition_review_row(row) -> str:
    """处理一条已领取记录；只返回审计状态，不暴露模型上下文。"""
    token = str(row["token"] or "")
    if not _recognition_review_is_enabled():
        db.complete_organize_confirmation_review(
            token,
            status="cancelled",
            result={
                "reason_code": "disabled",
                "summary": "Agent 主动复核已关闭，保留人工确认",
            },
        )
        return "cancelled"
    payload = _decode_row(row)
    from app.modules.agent_recognition_review import review_confirmation_payload

    decision = review_confirmation_payload(payload)
    audit = decision.audit_payload()
    if not decision.approved:
        db.complete_organize_confirmation_review(
            token, status=decision.status, result=audit
        )
        return decision.status

    # 用户可能在模型复核期间关闭开关。执行所有权竞争前再读取一次配置，
    # 保证关闭动作立即阻止新的自动确认，而不是等下一条任务才生效。
    if not _recognition_review_is_enabled():
        db.complete_organize_confirmation_review(
            token,
            status="cancelled",
            result={
                **audit,
                "reason_code": "disabled_before_confirmation",
                "summary": "Agent 主动复核已关闭，保留人工确认",
            },
        )
        return "cancelled"

    # 先保存不含工具原始载荷的最小审计，再由数据库原子竞争 pending
    # 所有权。人工若已确认，Agent 会安全退出。
    if not db.stage_organize_confirmation_review_result(token, audit):
        logger.info(
            "Agent 识别复核审计落库时已失去所有权 token=%s", token[:6]
        )
        return "ownership_lost"
    assert decision.candidate_index is not None
    try:
        start_confirmation(
            token,
            decision.candidate_index,
            chat_id=str(row["chat_id"] or ""),
            actor="agent",
        )
    except ValueError as exc:
        current = db.get_organize_confirmation(token)
        if (
            current is not None
            and str(current["review_status"] or "") == "running"
        ):
            db.complete_organize_confirmation_review(
                token,
                status="cancelled",
                result={
                    **audit,
                    "reason_code": "ownership_lost",
                    "summary": "人工操作或快照变化已先完成，Agent 未执行",
                },
            )
        logger.info(
            "Agent 识别复核未取得执行权 token=%s reason=%s",
            token[:6],
            str(exc)[:120],
        )
        return "ownership_lost"
    logger.info(
        "Agent 识别复核已提交 token=%s candidate=%s confidence=%.3f",
        token[:6],
        decision.candidate_index,
        decision.confidence,
    )
    return "approved"


def _recognition_review_loop(
    stop_event: threading.Event | None = None,
    wakeup_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or _review_stop
    wakeup_event = wakeup_event or _review_wakeup
    try:
        recovered = db.recover_interrupted_organize_confirmation_reviews()
        if recovered:
            logger.info("恢复 Agent 主动识别复核 count=%s", recovered)
    except Exception as exc:  # noqa: BLE001 - 启动恢复失败不影响人工确认
        logger.warning(
            "恢复 Agent 主动识别复核失败 type=%s", type(exc).__name__
        )

    while not stop_event.is_set():
        token: str = ""
        try:
            row = db.claim_next_organize_confirmation_review()
            if row is None:
                wakeup_event.wait(_DISPATCH_POLL_SECONDS)
                wakeup_event.clear()
                continue
            token = str(row["token"] or "")
            _process_recognition_review_row(row)
        except Exception as exc:  # noqa: BLE001 - 失败关闭，保留人工按钮
            logger.warning(
                "Agent 主动识别复核队列异常 type=%s",
                type(exc).__name__,
                exc_info=True,
            )
            if token:
                try:
                    db.complete_organize_confirmation_review(
                        token,
                        status="failed",
                        result={
                            "reason_code": "worker_error",
                            "summary": "Agent 复核异常，已保留人工确认",
                            "failure_code": type(exc).__name__,
                        },
                    )
                except Exception:
                    logger.warning(
                        "Agent 复核失败回执保存异常 token=%s",
                        token[:6],
                        exc_info=True,
                    )
        wakeup_event.wait(_DISPATCH_POLL_SECONDS)
        wakeup_event.clear()

def start_recognition_review_dispatcher() -> None:
    """启动独立只读复核消费者；不占用文件整理执行线程。"""
    global _review_thread, _review_accepting, _review_stop, _review_wakeup
    with _review_guard:
        _review_accepting = True
        active = (
            _review_thread is not None
            and _review_thread.is_alive()
            and not _review_stop.is_set()
        )
        if not active:
            _review_stop = threading.Event()
            _review_wakeup = threading.Event()
            _review_thread = threading.Thread(
                target=_recognition_review_loop,
                args=(_review_stop, _review_wakeup),
                name="agent-recognition-review",
                daemon=True,
            )
            _review_thread.start()
    _review_wakeup.set()


def wake_recognition_review_dispatcher() -> bool:
    """唤醒复核消费者；应用关闭期间不会擅自重建线程。"""
    with _review_guard:
        if not _review_accepting or _review_stop.is_set():
            return False
        thread = _review_thread
        if thread is None or not thread.is_alive():
            return False
        _review_wakeup.set()
        return True


def stop_recognition_review_dispatcher(timeout: float = 2.0) -> bool:
    """停止复核消费者；未处理记录在下次启动时恢复。"""
    global _review_thread, _review_accepting
    with _review_guard:
        _review_accepting = False
        stop_event = _review_stop
        wakeup_event = _review_wakeup
        stop_event.set()
        wakeup_event.set()
        thread = _review_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(max(0.0, float(timeout)))
    stopped = thread is None or not thread.is_alive()
    with _review_guard:
        if _review_thread is thread and stopped:
            _review_thread = None
    return stopped


def start_confirmation_dispatcher() -> None:
    """在应用启动阶段启用持久化确认队列消费者；重复调用安全。"""
    global _dispatch_thread, _dispatch_accepting, _dispatch_stop, _dispatch_wakeup
    with _dispatch_guard:
        _dispatch_accepting = True
        active = (
            _dispatch_thread is not None
            and _dispatch_thread.is_alive()
            and not _dispatch_stop.is_set()
        )
        if not active:
            _dispatch_stop = threading.Event()
            _dispatch_wakeup = threading.Event()
            _dispatch_thread = threading.Thread(
                target=_confirmation_dispatch_loop,
                args=(_dispatch_stop, _dispatch_wakeup),
                name="telegram-organize-confirmations",
                daemon=True,
            )
            _dispatch_thread.start()
    _dispatch_wakeup.set()
    start_recognition_review_dispatcher()


def wake_confirmation_dispatcher() -> bool:
    """唤醒已启用的消费者；关机期间不会清除停止信号或重建线程。"""
    with _dispatch_guard:
        if not _dispatch_accepting or _dispatch_stop.is_set():
            return False
        thread = _dispatch_thread
        if thread is None or not thread.is_alive():
            return False
        _dispatch_wakeup.set()
        return True


def stop_confirmation_dispatcher(timeout: float = 2.0) -> bool:
    """停止队列消费者；返回是否已退出，queued 项留待下次启动。"""
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_guard:
        _dispatch_accepting = False
        stop_event = _dispatch_stop
        wakeup_event = _dispatch_wakeup
        stop_event.set()
        wakeup_event.set()
        thread = _dispatch_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(max(0.0, float(timeout)))
    stopped = thread is None or not thread.is_alive()
    with _dispatch_guard:
        if _dispatch_thread is thread and stopped:
            _dispatch_thread = None
    review_stopped = stop_recognition_review_dispatcher(timeout=timeout)
    return (stopped and review_stopped)


def start_confirmation(
    token: str,
    selected_index: int,
    *,
    chat_id: str,
    actor: str = "human",
) -> dict:
    """持久化确认选择；人工与 Agent 竞争同一个一次性 pending 所有权。"""
    normalized_actor = (actor or "human").strip().lower()
    if normalized_actor not in {"human", "agent"}:
        raise ValueError("确认执行者无效")
    preview = db.get_organize_confirmation(token)
    if preview is None:
        raise ValueError("确认操作不存在或已失效")
    expected_chat = str(preview["chat_id"] or "")
    if expected_chat and expected_chat != (chat_id or ""):
        raise ValueError("确认操作不存在或已失效")
    _raise_if_confirmation_expired(preview)

    payload = _decode_row(preview)
    candidates = list(payload.get("candidates") or [])
    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("候选参数无效")
    retry_selected_index = payload.get(_RETRY_SELECTED_INDEX_KEY)
    if retry_selected_index is not None:
        try:
            bound_index = int(retry_selected_index)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("确认任务数据损坏，请重新执行整理") from exc
        if selected_index != bound_index:
            raise ValueError("该重试确认已绑定其他候选")
    candidate = dict(candidates[selected_index])
    status = str(preview["status"] or "pending")
    if normalized_actor == "agent" and _clean_review_candidate(candidate) and status == "pending":
        _validate_agent_clean_authorization(payload, candidate)
    if normalized_actor == "agent" and status == "pending":
        receipt = _episode_research_receipt_from_row(preview)
        if receipt is not None:
            from app.modules.episode_research_service import (
                revalidate_episode_research_receipt,
            )
            revalidate_episode_research_receipt(payload, receipt, expected_candidate_index=selected_index)

    if status in {"queued", "running", "completed"}:
        if (
            normalized_actor == "agent"
            and str(preview["confirmation_actor"] or "") != "agent"
        ):
            raise ValueError("人工操作已先取得确认所有权")
        if int(preview["selected_index"] if preview["selected_index"] is not None else -1) != selected_index:
            raise ValueError("该媒体已选择其他候选，不能重复修改")
        return _confirmation_result(
            preview, payload, candidate
        ) | {"replayed": True}
    if status != "pending":
        raise ValueError("该确认操作已处理")

    try:
        row = db.claim_organize_confirmation(
            token,
            chat_id=chat_id,
            selected_index=selected_index,
            actor=normalized_actor,
            event_json=(
                serialize_notification_event(_confirmation_selected_event(preview, payload, candidate))
                if _confirmation_delivery_enabled(payload) else ""
            ),
            message_id=_confirmation_message_id(payload),
        )
    except ValueError:
        # 两个相同回调可同时读到 pending；数据库只允许一个认领成功。
        # 失败方重新读取真实状态，同候选按幂等重放处理，不误报“已处理”。
        current = db.get_organize_confirmation(token)
        if current is not None and str(current["status"] or "") in {"queued", "running", "completed"}:
            if (
                normalized_actor == "agent"
                and str(current["confirmation_actor"] or "") != "agent"
            ):
                raise ValueError("人工操作已先取得确认所有权")
            current_index = int(
                current["selected_index"]
                if current["selected_index"] is not None else -1
            )
            if current_index == selected_index:
                current_payload = _decode_row(current)
                current_candidate = dict(
                    list(current_payload.get("candidates") or [])[selected_index]
                )
                return _confirmation_result(
                    current,
                    current_payload,
                    current_candidate,
                ) | {"replayed": True}
            if str(current["status"] or "") in {"queued", "running"}:
                raise ValueError("该媒体已选择其他候选，不能重复修改")
        if current is not None and str(current["status"] or "") == "expired":
            try:
                _publish_expired_confirmation(current)
            except Exception as exc:  # noqa: BLE001 - 数据库终态优先，通知异步补偿。
                logger.warning(
                    "人工确认点击过期收口失败 token=%s type=%s",
                    token[:6], type(exc).__name__,
                )
            raise ValueError("确认操作已过期，请重新执行整理")
        raise
    # 原子入队时已经绑定 queue_id；此后不得清空 error，worker 可能已经失败。
    _dispatch_due_confirmation_delivery(token)
    _dispatch_next_queued_confirmation()
    wake_confirmation_dispatcher()
    current = db.get_organize_confirmation(token) or row
    return _confirmation_result(current, payload, candidate)


def _validate_snapshot(client: GuangYaClient, item: dict, *, role: str) -> None:
    file_id = str(item.get("file_id") or "").strip()
    current = client.file_info(file_id) if file_id else None
    if current is None or current.is_dir:
        raise DirectoryScrapeConflictError(f"{role}已不存在，请重新执行整理")
    mismatches = []
    if (current.name or "") != str(item.get("name") or ""):
        mismatches.append("文件名")
    expected_parent = str(item.get("parent_id") or "")
    if expected_parent and (current.parent_id or "") != expected_parent:
        mismatches.append("所在目录")
    if (current.size or 0) != int(item.get("size") or 0):
        mismatches.append("文件大小")
    expected_etag = str(item.get("etag") or "")
    if expected_etag and (current.etag or "") != expected_etag:
        mismatches.append("ETag")
    if mismatches:
        raise DirectoryScrapeConflictError(
            f"{role}在通知后发生变化（{'、'.join(mismatches)}），请重新执行整理"
        )


def _validate_metatube_confirmation_identity(
    payload: dict, detail: dict, rules: OrganizeRules,
) -> None:
    source_codes: set[str] = set()
    source_values = [str(payload.get("directory") or "")]
    source_values.extend(
        str(item.get("name") or "")
        for item in (payload.get("files") or [])
        if isinstance(item, dict)
    )
    for value in source_values:
        identifier = extract_nsfw_identifier(value, rules.nsfw_strip_domains)
        if identifier is not None:
            source_codes.add(normalize_code(identifier.code))
    resolved_code = normalize_code(str(detail.get("number") or ""))
    if not source_codes:
        raise ValueError("待确认文件未提取到可校验番号，不能套用 MetaTube 元数据")
    if not resolved_code or resolved_code not in source_codes:
        raise ValueError("MetaTube 候选番号与待确认文件不一致")


def _resolve_guangya_confirmation_candidate(
    payload: dict, candidate: dict, rules: OrganizeRules,
) -> tuple[TMDBScraper, MatchResult, dict, str]:
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    media_type = str(candidate.get("media_type") or "").strip().lower()
    scraper = TMDBScraper()
    try:
        if provider == "tmdb":
            tmdb_id = str(candidate.get("tmdb_id") or "").strip()
            if not tmdb_id or media_type not in {"movie", "tv"}:
                raise ValueError("TMDB 候选媒体参数无效")
            try:
                detail = scraper.get_detail_with_credits(tmdb_id, media_type)
                match = scraper.match_from_tmdb(tmdb_id, media_type)
            except Exception as exc:
                raise ConfirmationRetryableError(
                    "TMDB 服务暂时不可用，请稍后重试"
                ) from exc
            if not detail or not match.tmdb_id or match.need_confirm:
                raise ConfirmationRetryableError(
                    "TMDB 候选暂时无法确认，请稍后重试"
                )
        elif provider == "metatube":
            if media_type != "movie" or not external_id:
                raise ValueError("MetaTube 候选媒体参数无效")
            if not rules.nsfw_exclusive:
                raise ValueError("当前来源不是成人专用来源，已拒绝 MetaTube 候选")
            if not (rules.nsfw_metatube_endpoint or "").strip():
                raise ValueError("MetaTube 服务地址未配置")
            recognizer = None
            try:
                recognizer = NsfwRecognizer(
                    rules.nsfw_metatube_endpoint,
                    rules.nsfw_metatube_token,
                    strip_domains=rules.nsfw_strip_domains,
                    timeout=rules.nsfw_timeout_seconds,
                )
                match, detail = recognizer.resolve(external_id)
            except MetaTubeError as exc:
                raise ConfirmationRetryableError(
                    "MetaTube 服务暂时不可用，请稍后重试"
                ) from exc
            finally:
                close = getattr(recognizer, "close", None)
                if callable(close):
                    close()
            _validate_metatube_confirmation_identity(payload, detail, rules)
        elif provider == "clean_title":
            if media_type != "movie" or not external_id:
                raise ValueError("清洗标题候选参数无效")
            if not rules.nsfw_exclusive:
                raise ValueError("当前来源不是成人专用来源，已拒绝清洗标题入库")
            seed = next((
                str(item.get("name") or "")
                for item in (payload.get("files") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ), str(payload.get("directory") or ""))
            fallback = build_clean_title_candidate(seed, rules.nsfw_strip_domains)
            if fallback is None:
                raise ValueError("待确认文件未提取到有效番号，不能清洗标题入库")
            resolved_id = (fallback.get("external_id") or "").strip()
            if normalize_code(external_id) != normalize_code(resolved_id):
                raise ValueError("清洗标题候选番号与待确认文件不一致")
            # 标题由服务端根据原始文件重新生成，不能信任回传候选中的可修改文本。
            title = (fallback.get("title") or resolved_id).strip()
            detail = {
                **dict(fallback.get("metadata") or {}),
                "number": resolved_id,
                "title": title,
                "fallback": True,
            }
            match = MatchResult(
                title=title,
                media_type="movie",
                confidence=1.0,
                provider="clean_title",
                external_id=resolved_id,
                metadata=detail,
                status="matched",
            )
        else:
            raise ValueError("候选媒体来源无效")
        match.locked = True
        match.need_confirm = False
        match.matched_by = "telegram_confirmation"
        return scraper, match, detail, provider
    except Exception:
        scraper.close()
        raise


def _record_confirmation_learning(
    scraper: TMDBScraper,
    payload: dict,
    candidate: dict,
    match,
) -> list[str]:
    """Best-effort persist an explicit Telegram choice without affecting cloud writes."""
    warnings: list[str] = []
    selected_tmdb_id = str(candidate.get("tmdb_id") or match.tmdb_id or "").strip()
    rejected_tmdb_ids = list(dict.fromkeys(
        str(item.get("tmdb_id") or "").strip()
        for item in (payload.get("candidates") or [])
        if str(item.get("tmdb_id") or "").strip()
        and str(item.get("tmdb_id") or "").strip() != selected_tmdb_id
    ))
    parent_path = str(payload.get("directory") or "").strip()
    seen_names: set[str] = set()
    for item in payload.get("files") or []:
        raw_name = str((item or {}).get("name") or "").strip()
        if not raw_name or raw_name in seen_names:
            continue
        seen_names.add(raw_name)
        try:
            scraper.confirm(
                raw_name,
                selected_tmdb_id,
                str(match.title or candidate.get("title") or "").strip(),
                str(match.year or candidate.get("year") or "").strip(),
                str(match.media_type or candidate.get("media_type") or "").strip(),
                parent_path=parent_path,
                rejected_tmdb_ids=rejected_tmdb_ids,
            )
        except Exception as exc:
            warnings.append(f"人工确认识别知识保存失败: {raw_name}")
            logger.warning(
                "Telegram 人工确认识别知识保存失败 parent=%s type=%s",
                parent_path or "根目录",
                type(exc).__name__,
            )
    return warnings


def _confirmation_unresolved_error(stats: dict) -> str:
    """返回固定候选仍无法形成安全计划时的终态错误。"""
    unresolved = safe_int(stats.get("need_confirm"), 0, minimum=0)
    if unresolved <= 0:
        return ""
    reasons = [
        str(item or "").strip()
        for item in (stats.get("confirmations") or [])
        if str(item or "").strip()
    ]
    detail = reasons[0] if reasons else "所选候选仍未通过安全整理校验"
    return (
        f"所选媒体仍有 {unresolved} 个文件无法完成安全规划：{detail}。"
        "文件保持原位，请检查文件名中的季集编号后重新整理"
    )


def _confirmation_source_display(payload: dict) -> str:
    source = str(payload.get("source_name") or "").strip()
    directory = str(payload.get("directory") or "").strip()
    if directory and directory != "/" and directory.startswith("/"):
        return directory  # 兼容旧的非根绝对路径载荷。
    if directory not in ("", "/"):
        return f"{source.rstrip('/')}/{directory}" if source else directory
    return source or ("/" if directory == "/" else "路径未记录")


def _confirmation_result_event(
    payload: dict, candidate: dict, stats: dict, *, actor: str = "human"
) -> NotificationEvent:
    moved = safe_int(stats.get("moved"), 0, minimum=0)
    metadata = safe_int(stats.get("metadata_moved"), 0, minimum=0)
    skipped = safe_int(stats.get("skipped"), 0, minimum=0)
    failed = safe_int(stats.get("failed"), 0, minimum=0)
    unresolved = safe_int(stats.get("need_confirm"), 0, minimum=0)
    warnings = len(list(stats.get("warnings") or []))
    strm = stats.get("strm") if isinstance(stats.get("strm"), dict) else {}
    if strm.get("ok"):
        strm_label, refresh_label = "已排队", "等待 STRM 完成"
    elif strm.get("skipped"):
        strm_label, refresh_label = "已跳过", "未触发"
    elif strm:
        strm_label, refresh_label = "启动失败", "未触发"
    else:
        strm_label, refresh_label = "未启用或无变更", "未触发"
    partial = bool(
        failed
        or unresolved
        or warnings
        or stats.get("empty_dir_cleanup_failed")
        or stats.get("source_dir_cleanup_failed")
        or (strm and not strm.get("ok") and not strm.get("skipped"))
    )
    result_label = (
        f"已移动 {moved} · 元数据 {metadata} · 跳过 {skipped} · 失败 {failed}"
    )
    if unresolved:
        result_label += f" · 待确认 {unresolved}"
    actor_label = "Agent 确认" if actor == "agent" else "人工确认"
    # 下载目录收尾在确认终态落盘后执行，也可能延迟重试；不要把收尾前的
    # 数量冻结进确认 outbox，最终结果沿用下载事务自己的通知/记录。
    download_cleanup = bool(stats.get("download_staging_identity"))
    return NotificationEvent(
        f"⚠️ {actor_label}整理部分完成" if partial else f"✅ {actor_label}整理完成",
        fields=(
            ("目标媒体", _candidate_display_name(candidate)),
            *(( ("入库方式", "清洗入库 · 无完整元数据"), ) if _clean_review_candidate(candidate) else ()),
            ("源文件目录", _confirmation_source_display(payload)),
            NOTIFICATION_SECTION_BREAK,
            ("执行结果", result_label),
            *(( ("目录清理", "由下载流程核验收尾，结果见下载记录"), ) if download_cleanup else ()),
            *(( ("空目录清理", f"已清理 {safe_int(stats.get('empty_dirs_cleaned'), 0, minimum=0)} 个"), )
              if "empty_dirs_cleaned" in stats and not download_cleanup else ()),
            *(( ("目录保留说明", "；".join(stats["empty_dir_cleanup_reasons"])), )
              if stats.get("empty_dir_cleanup_reasons") and not download_cleanup else ()),
            ("STRM 状态", _terminal_status_label(strm_label)),
            ("媒体库刷新", _terminal_status_label(
                refresh_label, media_library=True,
            )),
        ),
        layout="relaxed",
    )


def _confirmation_message_id(payload: dict) -> int | None:
    try:
        message_id = int(payload.get("_telegram_message_id") or 0)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def _confirmation_delivery_enabled(payload: dict) -> bool:
    return not bool(payload.get(_NOTIFICATION_SUPPRESSED_KEY))


def _local_confirmation_result_event(
    payload: dict, candidate: dict, result: dict, *, actor: str = "human"
) -> NotificationEvent:
    moved = len(list(result.get("moved") or []))
    deleted = len(list(result.get("deleted_junk") or []))
    warnings = len(list(result.get("warnings") or []))
    refresh_status = str(result.get("media_refresh_status") or "")
    partial = bool(warnings or refresh_status == "failed")
    if actor == "agent":
        event_title = (
            "⚠️ 本地媒体 Agent 确认整理部分完成"
            if partial
            else "✅ 本地媒体 Agent 确认整理完成"
        )
    else:
        event_title = (
            "⚠️ 本地媒体确认整理部分完成"
            if partial
            else "✅ 本地媒体确认整理完成"
        )
    return NotificationEvent(
        event_title,
        fields=(
            ("目标媒体", _candidate_display_name(candidate)),
            ("存储来源", payload.get("source_name") or "本地媒体"),
            NOTIFICATION_SECTION_BREAK,
            ("执行结果", f"已移动 {moved} · 清理 {deleted} · 警告 {warnings}"),
            ("媒体库刷新", _terminal_status_label({
                "completed": "已刷新", "queued": "已排队",
                "failed": "刷新失败", "skipped": "未启用",
            }.get(refresh_status, "已处理"), media_library=True)),
        ),
        layout="relaxed",
    )


def _execute_local_media_confirmation(
    token: str, payload: dict, candidate: dict, *,
    selected_index: int, chat_id: str, actor: str = "human"
) -> dict:
    claimed_task = False
    task_id = safe_int(payload.get("local_task_id"), 0, minimum=0)
    delivery_enabled = _confirmation_delivery_enabled(payload)
    qb_client = None
    try:
        if task_id <= 0:
            raise ValueError("本地媒体确认任务无效，请前往 Web 重新处理")
        owner = str(payload.get("owner") or "admin").strip() or "admin"
        expected_version = safe_int(payload.get("local_task_version"), 0, minimum=0)
        expected_source_id = safe_int(payload.get("local_source_id"), 0, minimum=0)
        expected_digest = str(payload.get("snapshot_digest") or "").strip()
        rules_snapshot = str(payload.get("rules_snapshot") or "").strip()
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        media_type = str(candidate.get("media_type") or "").strip().lower()
        if not tmdb_id or media_type not in {"movie", "tv"}:
            raise ValueError("候选媒体参数无效")
        if expected_version <= 0 or expected_source_id <= 0 or not expected_digest or not rules_snapshot:
            raise ValueError("本地媒体确认快照无效，请前往 Web 重新处理")

        task = db.get_local_media_task(task_id, owner=owner)
        if task is None or task.status != "requires_manual":
            raise ValueError("本地媒体任务已变化，请前往 Web 查看最新状态")
        if task.version != expected_version or task.source_id != expected_source_id:
            raise ValueError("本地媒体任务已更新，请前往 Web 重新确认")
        source = db.get_local_media_source(task.source_id, owner=owner)
        if source is None:
            raise ValueError("本地媒体来源已删除，请前往 Web 重新配置")

        from app.modules.local_media_scheduler import get_local_media_scheduler

        scheduler = get_local_media_scheduler()
        inspection = scheduler.service.inspect_source(owner, task.source_id, task.content_path)
        if str(inspection.get("digest") or "") != expected_digest:
            raise ValueError("源文件在通知后发生变化，请前往 Web 重新检查")
        if not db.claim_local_media_confirmation_task(
            task_id,
            owner=owner,
            expected_version=expected_version,
            expected_snapshot_digest=(task.snapshot_digest or ""),
            confirmed_snapshot_digest=expected_digest,
            tmdb_id=tmdb_id,
            media_type=media_type,
            rules_snapshot=rules_snapshot,
            season_override=payload.get("season_override"),
            episode_override=payload.get("episode_override"),
            numbering_mode=str(payload.get("numbering_mode") or "auto"),
            title=str(candidate.get("title") or ""),
            year=str(candidate.get("year") or ""),
            confirmation_actor=actor,
        ):
            raise ValueError("本地媒体任务已被其他操作认领，请前往 Web 查看最新状态")
        claimed_task = True
        current = db.get_local_media_task(task_id, owner=owner)
        if current is None:
            raise ValueError("本地媒体任务不存在")
        qb_client = scheduler.qb_factory() if current.qb_hash else None
        result = scheduler.service.execute_task(owner, task_id, qb_client=qb_client)
        if str(result.get("status") or "") != "completed":
            reason = str(
                (result.get("preview") or {}).get("reason")
                or "本地媒体仍需补充季集等信息"
            )
            raise ValueError(f"{reason}；请前往 Web 继续处理")
        terminal_event = _local_confirmation_result_event(
            payload, candidate, result, actor=actor
        )
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            event_json=serialize_notification_event(terminal_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            enqueue_delivery=delivery_enabled,
        )
        if delivery_enabled:
            _dispatch_due_confirmation_delivery(token)
        return {"candidate": candidate, "stats": result, "local_task_id": task_id}
    except Exception as exc:
        message = str(exc or "本地媒体确认整理失败").strip() or "本地媒体确认整理失败"
        if claimed_task and task_id > 0:
            try:
                current = db.get_local_media_task(
                    task_id, owner=str(payload.get("owner") or "admin")
                )
                if current is not None and current.status == "recognizing":
                    failure_status = (
                        "requires_manual"
                        if bool(getattr(exc, "requires_manual", False))
                        else "failed"
                    )
                    db.update_local_media_task(
                        task_id,
                        owner=current.owner,
                        status=failure_status,
                        error=message,
                    )
                    current = db.get_local_media_task(task_id, owner=current.owner)
            except Exception:
                logger.warning(
                    "本地媒体确认失败状态保存异常 task=%s", task_id, exc_info=True
                )
        logger.warning(
            "本地媒体确认整理失败 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
        failure_event = NotificationEvent(
            "❌ 本地媒体 Agent 确认整理失败"
            if actor == "agent"
            else "❌ 本地媒体确认整理失败",
            fields=(
                ("目标文件", payload.get("directory") or "本地媒体"),
                ("候选媒体", _candidate_display_name(candidate, "")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer="请前往 Web 的本地媒体待确认页继续处理。",
            layout="relaxed",
        )
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            retryable=False,
            enqueue_delivery=delivery_enabled,
        )
        if delivery_enabled:
            _dispatch_due_confirmation_delivery(token)
        raise
    finally:
        close = getattr(qb_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as close_exc:
                logger.warning(
                    "关闭本地媒体确认 qB 客户端失败 task=%s type=%s",
                    task_id,
                    type(close_exc).__name__,
                )


def _execute_confirmation(
    token: str, payload: dict, candidate: dict, *,
    selected_index: int, chat_id: str, actor: str = "human"
) -> dict:
    try:
        if _confirmation_kind(payload) == "local_media":
            return _execute_local_media_confirmation(
                token,
                payload,
                candidate,
                selected_index=selected_index,
                chat_id=chat_id,
                actor=actor,
            )
        return _execute_guangya_confirmation(
            token,
            payload,
            candidate,
            selected_index=selected_index,
            chat_id=chat_id,
            actor=actor,
        )
    finally:
        # 成功、不可重试失败都在各自执行器内先落终态；由唯一出口尝试
        # 收口父汇总。写前可重试失败会以相同指纹签发新的 pending 行，
        # 最新行尚未终结，因此不会把旧 token 的失败提前计入父汇总。
        row = db.get_organize_confirmation(token)
        if row is not None and str(row["status"] or "") in (
            _TERMINAL_CONFIRMATION_STATUSES
        ):
            try:
                _maybe_update_parent_organize_rollup(
                    str(row["organize_task_id"] or ""),
                    chat_id=str(row["chat_id"] or chat_id or ""),
                )
            except Exception as exc:  # noqa: BLE001 - 不让投影失败反转整理结果。
                logger.warning(
                    "人工确认执行后父汇总更新失败 token=%s type=%s",
                    token[:6], type(exc).__name__,
                )


def _natural_sort_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", (value or ""))
    )


def _confirmed_multipart_overrides(payload: dict, files: list[dict]) -> dict[object, int]:
    if str(payload.get("multipart_strategy") or "") != "sequence":
        return {}
    ordered = sorted(
        files, key=lambda item: _natural_sort_key(str(item.get("name") or ""))
    )
    directory = str(payload.get("directory") or "")
    overrides: dict[object, int] = {}
    for index, item in enumerate(ordered, 1):
        name = str(item.get("name") or "")
        overrides[name] = index
        overrides[(directory, name)] = index
    return overrides


def _confirmation_notification_threads(
    token: str, payload: dict, *, chat_id: str,
) -> list[dict[str, object]]:
    threads: list[dict[str, object]] = [{
        "topic": "confirmation",
        "thread_key": f"confirmation:{token}",
        "token": token,
        "chat_id": (chat_id or ""),
        "topic_enabled": True,
    }]
    organize_task_id = str(payload.get("organize_task_id") or "").strip()
    if organize_task_id and isinstance(payload.get("organize_rollup"), dict):
        threads.append({
            "topic": "organize",
            "thread_key": f"organize:{organize_task_id}",
            "task_id": organize_task_id,
            "chat_id": (chat_id or ""),
            "topic_enabled": True,
        })
    return threads


def _finalize_confirmed_downloads(payload: dict, client, stats: dict, rules: OrganizeRules,
                                  *, confirmation_token: str = '') -> list[int]:
    """完成确认后收口原下载业务；通知rollup不是删除权限或业务身份。"""

    from app.modules.organize_tasks import OrganizeTaskManager
    from app.repositories.download_staging import (
        complete_staging_confirmation_phase,
        requests_for_download_confirmation,
    )

    request_ids: list[int] = []
    cleanup_ids: list[int] = []
    try:
        rows = requests_for_download_confirmation(payload)
        for row in rows:
            if (row.get("gy_status") != "completed"
                    or row.get("status") not in ("completed", "submitted", "downloading", "manual_review")
                    or row.get("organize_status") in ("resubmitted", "cleared", "failed", "stopped")
                    or row.get("attention_cleared_at")):
                continue
            request_ids.append(int(row["id"]))
            if not confirmation_token:
                closed = complete_staging_confirmation_phase(row)
                if closed and not any(stats.get(key) for key in ("failed", "need_confirm", "stopped", "scan_errors", "audit_failures")):
                    cleanup_ids.append(int(row["id"]))
        if not request_ids:
            return []  # 普通手动确认无下载业务，不能误入下载维护或污染成功统计。
        if confirmation_token:
            # 新确认的意图已与终态原子持久化；这里只是当前写锁内的尽力快速路径。
            from app.modules.download_staging_reconcile import reconcile_with_client
            from app.modules.organize_tasks import get_organize_manager
            report = reconcile_with_client(
                client, rules=rules, confirmation_token=confirmation_token,
                writer_lock=get_organize_manager()._lock,
            )
        else:
            # 保留既有直接调用契约；历史自动补偿由持久队列单独选取，不重跑确认。
            if not cleanup_ids:
                if request_ids:
                    Organizer._append_reason(stats, "empty_dir_cleanup_reasons", "原下载整理阶段尚未成功收口，已保留隔离目录", limit=8)
                return request_ids
            # 绝不能传 organizer.client：候选 Scoped 视图看不到未选择的文件。
            report = OrganizeTaskManager._cleanup_download_staging(
                client, cleanup_ids,
                [{"id": str(row["gy_target_dir"]), "name": str(row["gy_staging_name"])}
                 for row in rows if int(row["id"]) in cleanup_ids],
                rules=rules,
            )
        stats["empty_dirs_cleaned"] = int(stats.get("empty_dirs_cleaned") or 0) + int(report.get("cleaned") or 0)
        for reason in report.get("reasons", []):
            Organizer._append_reason(stats, "empty_dir_cleanup_reasons", reason, limit=8)
        failures = sum(int(report.get(key) or 0) for key in ("scan_failures", "delete_failures", "unsupported", "unavailable"))
        if failures and not confirmation_token:
            stats["empty_dir_cleanup_failed"] = int(stats.get("empty_dir_cleanup_failed") or 0) + failures
        stats["download_staging_cleanup"] = {key: value for key, value in report.items() if not key.startswith("_")}
    except Exception as exc:  # noqa: BLE001 - 快速路径失败不取消已提交终态/持久意图。
        logger.warning("人工确认后下载目录收尾失败 type=%s", type(exc).__name__)
        # 清理的瞬时失败不能污染确认成功证据，否则恢复队列会被自己的失败字段冻结。
        stats["download_staging_cleanup"] = {"deferred": True, "reason": "收尾暂不可用，将由持久队列复核"}
        Organizer._append_reason(stats, "empty_dir_cleanup_reasons", "下载目录收尾暂不可用，将由后台退避复核", limit=8)
    return request_ids


def _cleanup_confirmed_source(payload: dict, organizer: Organizer, rules: OrganizeRules, stats: dict) -> None:
    """仅沿本次成功移动的原父链收尾；不扫描其他子树或接管下载隔离目录。"""
    if not rules.clean_empty or not stats.get("moved") or any(
        stats.get(key) for key in (
            "failed", "need_confirm", "stopped", "scan_errors",
            "replacement_cleanup_failed", "audit_failures",
        )
    ):
        return
    from app.modules.download_staging_cleanup import source_inside_protected_staging
    from app.modules.organize_tasks import _cleanup_manual_source_root

    try:
        source_id = str(payload.get("source_dir_id") or "").strip()
        parent_id = str(payload.get("source_parent_id") or "").strip()
        if not source_id or not parent_id:
            raise RuntimeError("来源目录身份缺失，已保留空目录")
        staging_ids = set(db.list_protected_guangya_staging_ids())
        if source_inside_protected_staging(organizer.client, parent_id, staging_ids):
            Organizer._append_reason(
                stats, "empty_dir_cleanup_reasons", "下载隔离目录由下载收尾处理，未执行通用清理", limit=8,
            )
            return
        # 先核实整条父链属于冻结来源，再从里向外删空；不能越过原来源边界。
        parents: list[str] = []
        current = parent_id
        while current not in parents and len(parents) < 64:
            parents.append(current)
            if current == source_id:
                break
            if current == "0":
                raise RuntimeError("原父目录已不在确认来源内，已保留空目录")
            info = organizer.client.file_info(current)
            if info is None or not info.is_dir or not str(info.parent_id or "").strip():
                raise RuntimeError("来源父链无法确认，已保留空目录")
            current = str(info.parent_id)
        else:
            raise RuntimeError("来源父链异常，已保留空目录")
        for directory_id in parents:
            cleaned = int(stats.get("empty_dirs_cleaned", 0) or 0)
            _cleanup_manual_source_root(
                organizer, directory_id, "", rules, stats, trigger_type="manual",
            )
            if int(stats.get("empty_dirs_cleaned", 0) or 0) == cleaned:
                break  # 非空、受保护或清理失败时，不再向上尝试。
    except Exception as exc:  # noqa: BLE001 - 收尾不能把已提交的入库变成失败或重放移动。
        logger.warning("确认入库后源目录收尾失败 type=%s", type(exc).__name__)
        stats["source_dir_cleanup_failed"] = int(stats.get("source_dir_cleanup_failed", 0) or 0) + 1
        Organizer._append_reason(
            stats, "empty_dir_cleanup_reasons", f"源目录收尾失败，已保留: {exc}", limit=8,
        )


def _execute_guangya_confirmation(
    token: str, payload: dict, candidate: dict, *,
    selected_index: int, chat_id: str, actor: str = "human"
) -> dict:
    # running 状态由 claim_queued_organize_confirmation 原子授予；worker 不得
    # 无条件重新取得所有权，否则重启恢复后的 failed 终态会被迟到执行覆盖。
    client = None
    scraper = None
    organizer = None
    write_started = False
    operation_token = f"recognition-confirm:{token}"
    delivery_enabled = _confirmation_delivery_enabled(payload)
    clean_boundary = (
        _AgentCleanWriteBoundary(payload, candidate)
        if actor == "agent" and _clean_review_candidate(candidate) else None
    )
    episode_boundary = None
    write_boundary = clean_boundary
    try:
        if clean_boundary is not None:
            _validate_agent_clean_authorization(payload, candidate)
        source_dir_id = str(payload.get("source_dir_id") or "").strip()
        current_rules = OrganizeRules.from_config().for_source(source_dir_id)
        changes = organize_rules_snapshot_changes(
            payload.get("rules"), current_rules, files=payload.get("files"),
        )
        if changes:
            labels = {
                "small_file_mb": f"小文件过滤阈值 {current_rules.small_file_mb} MB（含不合格或大小未知的视频）",
                "target_dir_id": "归档目标目录",
                "conflict_strategy": "文件覆盖策略",
                "nsfw_source_ids": "成人来源范围",
                "nsfw_enabled": "成人识别开关",
            }
            fields = "、".join(labels.get(key, key) for key in changes)
            raise DirectoryScrapeConflictError(f"整理规则已变化（{fields}），请重新执行整理后再确认")

        from app.repositories.download_staging import requests_for_download_confirmation
        download_owners = requests_for_download_confirmation(payload, strict=True)
        if "download_request_ids" in payload and not download_owners:
            raise DirectoryScrapeConflictError("原下载请求关联已变化，请重新核对整理任务")
        if any(
            row.get("gy_status") != "completed"
            or row.get("status") not in ("completed", "submitted", "downloading", "manual_review")
            or row.get("organize_status") in ("resubmitted", "cleared", "stopped", "failed")
            or row.get("attention_cleared_at")
            for row in download_owners
        ):
            raise DirectoryScrapeConflictError("原下载请求已取消、重新提交或尚未下载完成，未执行整理")

        files = [dict(item) for item in (payload.get("files") or [])]
        companions = [dict(item) for item in (payload.get("companions") or [])]
        source_snapshots = {
            str(item.get("file_id") or ""): GuangYaFile(
                str(item.get("file_id") or ""), str(item.get("name") or ""), False,
                int(item.get("size") or 0), str(item.get("etag") or ""),
                str(item.get("parent_id") or "0"),
            )
            for item in (*files, *companions)
        }
        parent_id = str(payload.get("source_parent_id") or "0")
        if not files or any(str(item.get("parent_id") or "0") != parent_id for item in files):
            raise DirectoryScrapeConflictError("待确认文件作用域无效，请重新执行整理")

        client = GuangYaClient()
        for item in files:
            _validate_snapshot(client, item, role="待确认视频")
        for item in companions:
            _validate_snapshot(client, item, role="伴随文件")

        scraper, match, detail, provider = _resolve_guangya_confirmation_candidate(
            payload, candidate, current_rules,
        )

        position_overrides: dict[str | tuple[str, str], tuple[int | None, int | None]] = {
            str(item.get("name") or ""): (item.get("season"), item.get("episode"))
            for item in files
        }
        episode_proposal = _episode_research_for_execution(token, payload, selected_index, actor)
        if episode_proposal is not None:
            if provider != "tmdb" or match.tmdb_id != episode_proposal["tmdb_id"]:
                raise DirectoryScrapeConflictError("当前候选身份与季集研究不一致")
            # 联网重验耗时期间可能发生同ID内容变化；再次验证整包后才创建计划。
            for item in files:
                _validate_snapshot(client, item, role="研究视频")
            for item in companions:
                _validate_snapshot(client, item, role="研究伴随文件")
            position_overrides: dict[str | tuple[str, str], tuple[int | None, int | None]] = {
                str(files[row["file_index"]]["name"]): (row["target_season"], row["target_episode"])
                for row in episode_proposal["mappings"]
            }
            episode_boundary = _AgentEpisodeWriteBoundary(payload, episode_proposal, client=client)
            write_boundary = episode_boundary
        multipart_overrides = _confirmed_multipart_overrides(payload, files)
        allowed_ids = {
            str(item.get("file_id") or "") for item in (*files, *companions)
            if str(item.get("file_id") or "")
        }
        # 季集研究仍不扩大删除授权；清洗入库遵循清空目录开关，旧版本替换
        # 继续由写边界禁止。源空目录只在成功后沿已确认父链复核，不扫描其他子树。
        execution_rules = replace(current_rules, clean_empty=False) if episode_boundary else current_rules
        organizer = Organizer(
            client=client,
            before_plan_write=write_boundary,
            scraper=FixedMatchScraper(
                scraper,
                match,
                detail,
                preserve_specials=True,
                position_overrides=position_overrides,
                multipart_overrides=multipart_overrides,
                map_source_positions=provider == "tmdb" and episode_proposal is None,
            ),
        )
        organizer._validate_target_outside_source(parent_id, current_rules.target_dir_id)
        plans, _preview_stats = organizer.organize(
            parent_id,
            execution_rules,
            dry_run=True,
            post_actions=False,
            source_name=str(payload.get("directory") or payload.get("source_name") or ""),
            require_complete_scan=True,
            source_member_ids=frozenset(allowed_ids),
            source_snapshots=source_snapshots,
            group_pipeline=False,
            # 预览在线探测并预热缓存，保证随后只读缓存的执行阶段命名一致。
            media_probe_cache_only=False,
        )
        planned_ids = {str(plan.file_id) for plan in plans}
        expected_ids = {str(item.get("file_id") or "") for item in files}
        if planned_ids != expected_ids:
            raise DirectoryScrapeConflictError("待确认文件集合已变化，请重新执行整理")
        unresolved_error = _confirmation_unresolved_error(_preview_stats)
        if unresolved_error:
            # 人工按钮只确认媒体身份，并不授权绕过 TMDB 季集边界。固定
            # 候选若仍无法为全部文件形成安全计划，必须在任何写入前失败
            # 关闭；否则会出现“已完成 / 已移动 0”且父汇总吞掉文件的假成功。
            raise DirectoryScrapeConflictError(unresolved_error)

        if write_boundary is not None:
            for plan in plans:
                write_boundary(plan, "prepare")
        from app.modules.organize_probe_notifications import build_notification_context
        notification_context = build_notification_context(
            confirmation_token=token,
            chat_id=chat_id,
            # 下载归属来自上方严格校验后的 DB owners，不能直接信任卡片 IDs。
            download_request_ids=[int(row["id"]) for row in download_owners],
            notification_threads=_confirmation_notification_threads(token, payload, chat_id=chat_id),
            notify_enabled=delivery_enabled and execution_rules.notify_enabled,
            topic_enabled=delivery_enabled and execution_rules.notify_enabled and execution_rules.library_notify,
            # 无 rollup 的业务 taskID 不授权创建额外父线程；现有 threads 已限定通知范围。
            task_id="",
        )
        # 从这里开始 Organizer 可以调用真实 provider 写接口。即使异常看似
        # 瞬时，也不能再自动签发重试票据，必须先核对实际落盘结果。
        write_started = True
        _plans, stats = organizer.organize(
            parent_id,
            execution_rules,
            dry_run=False,
            post_actions=False,
            source_name=str(payload.get("directory") or payload.get("source_name") or ""),
            require_complete_scan=True,
            source_member_ids=frozenset(allowed_ids),
            source_snapshots=source_snapshots,
            group_pipeline=False,
            # 执行阶段只读缓存，保持与确认预览一致；缓存由预览阶段预热。
            media_probe_cache_only=True,
            operation_token=operation_token,
            notification_context=notification_context,
        )
        db.mark_organize_logs_confirmation_actor(operation_token, actor)
        if write_boundary is not None and not write_boundary.media_write_attempted:
            raise DirectoryScrapeConflictError("执行前检查未通过，未移动媒体，保留人工确认")
        if provider == "tmdb" and actor == "human":
            learning_warnings = _record_confirmation_learning(
                scraper, payload, candidate, match
            )
            if learning_warnings:
                stats.setdefault("warnings", []).extend(learning_warnings)
        scope_name = str(
            payload.get("directory")
            or payload.get("source_name")
            or ("Agent 确认" if actor == "agent" else "TG 人工确认")
        )
        confirm_debounce = _confirmation_strm_debounce_seconds(payload)
        if download_owners:
            from app.repositories.download_staging import staging_identity_snapshot
            # 记录执行前身份，禁止终态提交期间的同源重绑定获得新的清理授权。
            stats["download_staging_identity"] = staging_identity_snapshot(download_owners[0])
        if download_owners or not execution_rules.clean_empty:
            stats["download_staging_policy"] = "cleanup_empty" if execution_rules.clean_empty else "retained"
        if not download_owners:
            _cleanup_confirmed_source(payload, organizer, execution_rules, stats)
        terminal_event = _confirmation_result_event(
            payload, candidate, stats, actor=actor
        )
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json=json.dumps(stats, ensure_ascii=False, default=str),
            event_json=serialize_notification_event(terminal_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            enqueue_delivery=delivery_enabled,
        )
        download_request_ids = _finalize_confirmed_downloads(
            payload, client, stats, execution_rules, confirmation_token=token,
        )
        if download_request_ids:
            db.update_organize_confirmation(token, result_json=json.dumps(stats, ensure_ascii=False, default=str))
        # 先把候选卡收敛为整理终态，再排队 STRM；即使 debounce=0，
        # 后续刷新也只会在同一条终态消息上补字段，不会被较旧内容覆盖。
        if delivery_enabled:
            _dispatch_due_confirmation_delivery(token)
        try:
            Organizer.trigger_post_actions(
                stats,
                current_rules,
                source_name=scope_name,
                chat_id=chat_id,
                notify_result=False,
                strm_debounce_seconds=confirm_debounce,
                **({"download_request_ids": download_request_ids} if download_request_ids else {}),
                notification_threads=(
                    _confirmation_notification_threads(
                        token, payload, chat_id=chat_id,
                    )
                    if delivery_enabled
                    else []
                ),
            )
        except Exception as post_exc:
            warning = f"STRM 后处理启动失败：{post_exc}"
            stats.setdefault("warnings", []).append(warning)
            db.update_organize_confirmation(
                token,
                result_json=json.dumps(stats, ensure_ascii=False, default=str),
            )
            if delivery_enabled:
                update_confirmation_lifecycle_downstream(
                    token,
                    chat_id=chat_id,
                    strm_status="启动失败",
                    media_refresh="未触发",
                    partial=True,
                    error=warning,
                )
            organize_task_id = str(
                payload.get("organize_task_id") or ""
            ).strip()
            if organize_task_id:
                from app.modules.telegram_organize_lifecycle import (
                    update_organize_lifecycle_downstream,
                )

                update_organize_lifecycle_downstream(
                    organize_task_id,
                    chat_id=chat_id,
                    strm_status="启动失败",
                    media_refresh="未触发",
                    partial=True,
                    error=warning,
                )
            logger.warning(
                "人工确认整理已完成但后处理启动失败 token=%s type=%s",
                token[:6],
                type(post_exc).__name__,
            )
        if download_request_ids:
            try:
                from app.modules.telegram_download_lifecycle import (
                    publish_download_lifecycle,
                )
                for request_id in download_request_ids:
                    publish_download_lifecycle(request_id, stats=stats)
            except Exception as exc:
                logger.warning("人工确认后下载事务通知刷新失败 type=%s", type(exc).__name__)
        return {"candidate": candidate, "stats": stats}
    except Exception as exc:
        current = db.get_organize_confirmation(token)
        if current is not None and str(current["status"] or "") in (
            _TERMINAL_CONFIRMATION_STATUSES
        ):
            logger.warning(
                "人工确认整理在终态后抛出异常 token=%s status=%s type=%s",
                token[:6],
                str(current["status"] or ""),
                type(exc).__name__,
            )
            raise
        message = str(exc or "Telegram 确认整理失败").strip() or "Telegram 确认整理失败"
        retryable = (
            not write_started
            and not isinstance(exc, (DirectoryScrapeConflictError, ValueError))
        )
        if write_started:
            db.mark_organize_logs_confirmation_actor(operation_token, actor)
        logger.warning(
            "Telegram 确认整理失败 token=%s type=%s retryable=%s",
            token[:6],
            type(exc).__name__,
            retryable,
        )
        terminal_failure_event = NotificationEvent(
            "❌ Agent 确认整理失败"
            if actor == "agent"
            else "❌ Telegram 确认整理失败",
            fields=(
                ("所在目录", payload.get("directory") or "/"),
                ("候选媒体", _candidate_display_name(candidate, "")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer="请重新执行整理生成新候选。",
            layout="relaxed",
        )
        actions: tuple[NotificationAction, ...] = ()
        clean_handoff = clean_boundary is not None and not clean_boundary.media_write_attempted
        clean_retry = clean_handoff and _clean_confirmation_retry_is_current(payload, client)
        if clean_retry or (clean_boundary is None and retryable and delivery_enabled):
            try:
                _retry_token, retry_action = _persist_confirmation_retry(
                    payload,
                    selected_index=selected_index,
                    chat_id=chat_id,
                )
                if clean_handoff:
                    actions = (
                        NotificationAction(f"人工确认 · {_safe_label(candidate, selected_index)}", retry_action.callback_data),
                        NotificationAction("跳过此组", f"orgc:{_retry_token}:skip"),
                    )
                else:
                    actions = (retry_action,)
            except Exception as retry_exc:  # noqa: BLE001 - 新票据失败必须 fail closed。
                logger.warning(
                    "Telegram 确认整理新重试票据创建失败 token=%s type=%s",
                    token[:6],
                    type(retry_exc).__name__,
                )

        failure_event = NotificationEvent(
            "⚠️ Agent 清洗复核转人工" if clean_handoff else terminal_failure_event.title,
            fields=terminal_failure_event.fields,
            footer=(
                "旧确认已失效；请点击下方新按钮重新确认。"
                if actions else terminal_failure_event.footer
            ),
            actions=actions,
            layout=terminal_failure_event.layout,
        )
        # 旧 token 一旦被选择便永久进入终态；终态和最终按钮卡在同一事务
        # 写入领域 outbox，再由唯一的 Telegram 通知中心接管发送与重试。
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            retryable=False,
            enqueue_delivery=delivery_enabled,
        )
        if delivery_enabled:
            _dispatch_due_confirmation_delivery(token)
        if not actions:
            _finalize_guangya_manual_logs(
                payload, status="failed", error=message, confirmation_actor=actor,
            )
        raise
    finally:
        close = getattr(organizer, "close", None)
        if callable(close):
            close()
        close = getattr(scraper, "close", None)
        if callable(close):
            close()
        close_guangya_client(client)


def confirmation_event(
    title: str,
    fields: dict,
    group: dict,
    rules: OrganizeRules,
    *,
    source_name: str,
    chat_id: str,
) -> NotificationEvent:
    actions = create_confirmation_actions(
        group, rules, source_name=source_name, chat_id=chat_id
    )
    reason = str(group.get("reason") or "匹配结果需要人工确认").strip()
    expiry_hint = (
        f"候选有效期为 {_CONFIRMATION_TTL_HOURS} 小时；到期未处理将自动结束，"
        "源文件保持原位。"
    )
    if list(group.get("candidates") or []):
        footer = (
            f"{reason}\n\n请选择候选继续整理，或跳过此组。"
            f"\n\n{expiry_hint}"
        )
    else:
        footer = (
            f"{reason}\n\n当前没有可用元数据。可跳过此组；文件保持原位，"
            f"本次待确认状态会结束。\n\n{expiry_hint}"
        )
    return NotificationEvent(
        title=title,
        fields=tuple(fields.items()),
        lines=_candidate_summary_lines(group),
        footer=footer,
        actions=actions,
        layout="relaxed",
    )
