"""光鸭离线转存规则与提交入口。"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from app.clients.guangya import GuangYaClient
from app.config import get, get_bool, get_int
from app.logger import get_logger, redact_sensitive_text
from app.modules.naming import sanitize_name

logger = get_logger(__name__)

MAGNET_PREFIX = "magnet:?"
ED2K_PREFIX = "ed2k://"
DEFAULT_MEDIA_EXTS = (
    "mkv", "mp4", "ts", "m2ts", "iso", "avi", "mov", "wmv", "flv", "rmvb",
    "mp3", "flac", "aac", "wav", "m4a",
)
DEFAULT_MEDIA_EXTS_CSV = ",".join(DEFAULT_MEDIA_EXTS)


@dataclass(frozen=True)
class OfflineRules:
    magnet_enabled: bool
    ed2k_enabled: bool
    http_enabled: bool
    target_dir_id: str
    target_dir_name: str
    secondary_enabled: bool
    secondary_dir_id: str
    secondary_dir_name: str
    secondary_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    min_file_mb: int
    allowed_exts: tuple[str, ...]

    @classmethod
    def from_config(cls) -> "OfflineRules":
        return cls(
            magnet_enabled=get_bool("OFFLINE_MAGNET_ENABLED", True),
            ed2k_enabled=get_bool("OFFLINE_ED2K_ENABLED", True),
            http_enabled=get_bool("OFFLINE_HTTP_ENABLED", False),
            target_dir_id=get("OFFLINE_TARGET_DIR", get("RSS_GY_TARGET_DIR", "0")) or "0",
            target_dir_name=get("OFFLINE_TARGET_DIR_NAME", "默认目录"),
            secondary_enabled=get_bool("OFFLINE_SECONDARY_ENABLED", False),
            secondary_dir_id=get("OFFLINE_SECONDARY_DIR", "0") or "0",
            secondary_dir_name=get("OFFLINE_SECONDARY_DIR_NAME", "二次分流目录"),
            secondary_keywords=tuple(_split(get("OFFLINE_SECONDARY_KEYWORDS", ""))),
            exclude_keywords=tuple(_split(get("OFFLINE_EXCLUDE_KEYWORDS", ""))),
            min_file_mb=max(0, get_int("OFFLINE_MIN_FILE_MB", 0)),
            allowed_exts=tuple(_extensions(get("OFFLINE_ALLOWED_EXTS", ""))),
        )

    @classmethod
    def from_mapping(cls, data: dict) -> "OfflineRules":
        """从预览表单构建规则，不写入正式配置。"""
        base = cls.from_config()
        return cls(
            magnet_enabled=_to_bool(data.get("magnet_enabled"), base.magnet_enabled),
            ed2k_enabled=_to_bool(data.get("ed2k_enabled"), base.ed2k_enabled),
            http_enabled=_to_bool(data.get("http_enabled"), base.http_enabled),
            target_dir_id=str(data.get("target_dir_id", base.target_dir_id) or "0"),
            target_dir_name=str(data.get("target_dir_name", base.target_dir_name) or "默认目录"),
            secondary_enabled=_to_bool(data.get("secondary_enabled"), base.secondary_enabled),
            secondary_dir_id=str(data.get("secondary_dir_id", base.secondary_dir_id) or "0"),
            secondary_dir_name=str(data.get("secondary_dir_name", base.secondary_dir_name) or "二次分流目录"),
            secondary_keywords=tuple(_split(str(data.get("secondary_keywords", "")))),
            exclude_keywords=tuple(_split(str(data.get("exclude_keywords", "")))),
            min_file_mb=max(0, _to_int(data.get("min_file_mb"), base.min_file_mb)),
            allowed_exts=tuple(_extensions(str(data.get("allowed_exts", "")))),
        )


@dataclass(frozen=True)
class OfflineManifestResolution:
    response: dict
    files: list[dict]
    attempts: int
    diagnostic: str


def _manifest_diagnostic(response: object) -> str:
    if not isinstance(response, dict):
        return f"响应类型 {type(response).__name__}"
    top_keys = sorted(str(key) for key in response.keys())[:12]
    data = response.get("data")
    data_keys = sorted(str(key) for key in data.keys())[:12] if isinstance(data, dict) else []
    tree_keys = [
        key for key in ("files", "fileList", "subFiles", "children", "items", "resources")
        if (key in response) or (isinstance(data, dict) and key in data)
    ]
    return (
        f"顶层字段={','.join(top_keys) or '-'}；"
        f"data字段={','.join(data_keys) or '-'}；"
        f"文件树字段={','.join(tree_keys) or '-'}"
    )


def _resolve_offline_manifest(
    client: GuangYaClient, url: str, protocol: str,
) -> OfflineManifestResolution:
    attempts = 1
    if protocol == "magnet":
        attempts = max(1, min(get_int("OFFLINE_MAGNET_RESOLVE_ATTEMPTS", 4), 6))
    delay = max(0.0, min(float(get("OFFLINE_MAGNET_RESOLVE_DELAY_SECONDS", "0.5") or 0.5), 5.0))
    last_response: dict = {}
    last_files: list[dict] = []
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.resolve_url(url)
            last_error = None
        except Exception as exc:
            last_error = exc
            last_response = {"error_type": type(exc).__name__}
            if protocol != "magnet" or attempt >= attempts:
                break
            logger.warning(
                "光鸭磁力解析暂时失败，准备重试 attempt=%s/%s error=%s",
                attempt, attempts, type(exc).__name__,
            )
            if delay > 0:
                time.sleep(delay * attempt)
            continue
        last_response = response if isinstance(response, dict) else {"raw_type": type(response).__name__}
        last_files = GuangYaClient.normalize_offline_files(last_response)
        if last_files or protocol != "magnet":
            return OfflineManifestResolution(last_response, last_files, attempt, _manifest_diagnostic(last_response))
        if attempt < attempts and delay > 0:
            time.sleep(delay * attempt)
    diagnostic = (
        f"解析异常={type(last_error).__name__}"
        if last_error is not None else _manifest_diagnostic(last_response)
    )
    logger.warning(
        "光鸭磁力解析未返回文件树 attempts=%s diagnostic=%s", attempts, diagnostic,
    )
    return OfflineManifestResolution(last_response, last_files, attempts, diagnostic)


@dataclass(frozen=True)
class OfflinePreviewSnapshot:
    preview_id: str
    url: str
    title: str
    rules: OfflineRules
    target_dir_id: str
    target_dir_name: str
    file_indexes: tuple[int, ...]
    locked_indexes: tuple[int, ...]
    expires_at: float


class OfflinePreviewStore:
    """一次性短期预览快照；提交 claim 后立即失效，避免整单重放。"""

    def __init__(
        self,
        ttl_seconds: float = 10 * 60,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        max_entries: int = 1024,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._token_factory = token_factory
        self._max_entries = max(1, int(max_entries))
        self._entries: dict[str, OfflinePreviewSnapshot] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl_seconds)

    def _prune_locked(self, now: float) -> None:
        for preview_id in [
            key for key, snapshot in self._entries.items()
            if snapshot.expires_at <= now
        ]:
            self._entries.pop(preview_id, None)

    def create(
        self,
        *,
        url: str,
        title: str,
        rules: OfflineRules,
        target_dir_id: str,
        target_dir_name: str,
        file_indexes: list[int],
        locked_indexes: list[int],
    ) -> str:
        preview_id = str(self._token_factory() or "").strip()
        if not preview_id:
            raise RuntimeError("无法生成离线预览标识")
        now = self._clock()
        snapshot = OfflinePreviewSnapshot(
            preview_id=preview_id,
            url=str(url or "").strip(),
            title=str(title or "").strip(),
            rules=rules,
            target_dir_id=str(target_dir_id or "0"),
            target_dir_name=str(target_dir_name or ""),
            file_indexes=tuple(int(value) for value in file_indexes),
            locked_indexes=tuple(int(value) for value in locked_indexes),
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._entries.pop(preview_id, None)
            self._entries[preview_id] = snapshot
            while len(self._entries) > self._max_entries:
                oldest_id = next(iter(self._entries))
                self._entries.pop(oldest_id, None)
        return preview_id

    def claim(
        self,
        preview_id: str,
        *,
        url: str,
        title: str,
        rules: OfflineRules,
    ) -> OfflinePreviewSnapshot:
        key = str(preview_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            snapshot = self._entries.get(key)
            if snapshot is None:
                raise ValueError("预览已过期或已使用，请重新解析")
            context_matches = (
                snapshot.url == str(url or "").strip()
                and snapshot.title == str(title or "").strip()
                and snapshot.rules == rules
            )
            self._entries.pop(key, None)
            if not context_matches:
                raise ValueError("预览上下文已变化，请重新解析")
            return snapshot

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._prune_locked(self._clock())
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass(frozen=True)
class OfflineDecision:
    allowed: bool
    protocol: str
    target_dir_id: str
    target_dir_name: str
    reason: str
    matched_keyword: str = ""

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "protocol": self.protocol,
            "target_dir_id": self.target_dir_id,
            "target_dir_name": self.target_dir_name,
            "reason": self.reason,
            "matched_keyword": self.matched_keyword,
        }


def analyze_offline_url(url: str, title: str = "", rules: OfflineRules | None = None) -> OfflineDecision:
    rules = rules or OfflineRules.from_config()
    cleaned = (url or "").strip()
    protocol = detect_protocol(cleaned)
    if not cleaned:
        return OfflineDecision(False, "unknown", rules.target_dir_id, rules.target_dir_name, "链接不能为空")
    enabled = {
        "magnet": rules.magnet_enabled,
        "ed2k": rules.ed2k_enabled,
        "http": rules.http_enabled,
    }
    if protocol not in enabled:
        return OfflineDecision(False, protocol, rules.target_dir_id, rules.target_dir_name, "仅支持磁力、ED2K 或 HTTP(S) 链接")
    if not enabled[protocol]:
        return OfflineDecision(False, protocol, rules.target_dir_id, rules.target_dir_name, f"{protocol.upper()} 协议已禁用")

    haystack = f"{title} {cleaned}".lower()
    for keyword in rules.exclude_keywords:
        if keyword.lower() in haystack:
            return OfflineDecision(False, protocol, rules.target_dir_id, rules.target_dir_name, "命中排除词", keyword)

    if rules.secondary_enabled and rules.secondary_dir_id not in ("", "0"):
        for keyword in rules.secondary_keywords:
            if keyword.lower() in haystack:
                return OfflineDecision(True, protocol, rules.secondary_dir_id, rules.secondary_dir_name, "命中二次分流", keyword)

    return OfflineDecision(True, protocol, rules.target_dir_id, rules.target_dir_name, "规则允许提交")


def _offline_staging_name(title: str, task_key: str) -> str:
    label = str(title or "离线下载").strip()
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", str(task_key or "").strip()).strip("-")
    prefix = f"MF-{identity}" if identity else "MF-download"
    try:
        safe_title = sanitize_name(label)
    except ValueError:
        safe_title = "离线下载"
    return sanitize_name(f"{prefix}-{safe_title}"[:180])


def _remove_empty_staging(
    client: GuangYaClient, directory_id: str
) -> tuple[str, str]:
    if not directory_id:
        return "", ""
    try:
        if client.list_dir(directory_id):
            return "retained", "临时目录仍包含文件，未自动删除"
        client.delete([directory_id])
        return "completed", ""
    except Exception as exc:
        logger.warning("光鸭临时下载目录清理失败 id=%s type=%s", directory_id, type(exc).__name__)
        return "failed", f"{type(exc).__name__}: {redact_sensitive_text(exc)[:240]}"


def submit_offline(url: str, title: str = "", client: GuangYaClient | None = None,
                   target_dir_id: str = "", target_dir_name: str = "", *,
                   isolate_task: bool = False, task_key: str = "") -> dict:
    """按正式离线规则提交任务。

    自动入口先解析文件树并强制应用扩展名、排除词和最小体积；磁力无法
    得到可验证文件树时，仅允许已启用任务隔离的入口按配置降级为整单写入。
    下载整理链可为每个请求创建独立临时目录，使完成后的整理只扫描本次任务。
    """
    rules = OfflineRules.from_config()
    decision = analyze_offline_url(url, title=title, rules=rules)
    if target_dir_id and decision.allowed:
        decision = OfflineDecision(
            True, decision.protocol, str(target_dir_id),
            str(target_dir_name or "指定目标目录"), "使用指定目录",
            decision.matched_keyword,
        )
    if not decision.allowed:
        return {"ok": False, "decision": decision.as_dict(), "error": decision.reason}
    client = client or GuangYaClient()
    if not client.logged_in:
        return {"ok": False, "decision": decision.as_dict(), "error": "光鸭未登录"}

    try:
        resolution = _resolve_offline_manifest(client, url, decision.protocol)
        files = resolution.files
        choices = build_offline_file_choices(files, rules)
    except Exception as exc:
        return {
            "ok": False, "decision": decision.as_dict(),
            "error": f"光鸭资源解析失败，未创建整单任务: {exc}",
        }

    unverified_manifest = False
    if decision.protocol == "magnet" and not choices:
        allow_fallback = isolate_task and get_bool("OFFLINE_MAGNET_UNVERIFIED_FALLBACK", True)
        if not allow_fallback:
            fallback_hint = (
                "隔离目录整单降级已关闭，可在离线转存设置中重新启用。"
                if isolate_task else
                "当前入口未启用任务隔离，不能安全执行整单降级。"
            )
            return {
                "ok": False, "decision": decision.as_dict(),
                "resolve_attempts": resolution.attempts,
                "resolve_diagnostic": resolution.diagnostic,
                "error": (
                    f"磁力资源连续 {resolution.attempts} 次未解析到可验证文件列表，"
                    f"已阻止整单下载；{fallback_hint}"
                ),
            }
        unverified_manifest = True

    selected = [int(item["index"]) for item in choices if item.get("selected")]
    if choices and not selected:
        reasons = sorted({str(item.get("exclude_reason") or "不符合规则") for item in choices})
        return {
            "ok": False, "decision": decision.as_dict(),
            "error": f"资源中没有符合下载规则的文件：{'；'.join(reasons[:3])}",
        }

    staging_id = ""
    staging_name = ""
    effective = decision
    if isolate_task:
        staging_name = _offline_staging_name(title, task_key)
        try:
            staging_id = client.create_dir(staging_name, decision.target_dir_id)
        except Exception as exc:
            return {
                "ok": False, "decision": decision.as_dict(),
                "error": f"创建任务隔离目录失败: {exc}",
            }
        if not staging_id:
            return {
                "ok": False, "decision": decision.as_dict(),
                "error": "创建任务隔离目录失败",
            }
        effective = OfflineDecision(
            True, decision.protocol, staging_id,
            f"{decision.target_dir_name} / {staging_name}",
            "使用任务隔离目录", decision.matched_keyword,
        )

    try:
        if choices:
            created = client.add_offline_selection(url, effective.target_dir_id, selected)
            ok = bool(created.get("ok"))
            task_ids = [str(item) for item in (created.get("task_ids") or []) if str(item)]
            completed_batches = int(created.get("completed_batches") or 0)
            partial_success = bool(created.get("partial_success") or task_ids or completed_batches)
            staging_cleanup_status = "pending" if staging_id else ""
            staging_cleanup_error = ""
            # 云端离线任务为异步写入；只要已有任一批次被接受，就不能依据
            # “目录当前为空”删除 staging，否则会破坏仍在写入的远端任务。
            if not ok and not partial_success:
                staging_cleanup_status, staging_cleanup_error = _remove_empty_staging(
                    client, staging_id
                )
            common = {
                "decision": effective.as_dict(),
                "selection_mode": "files",
                "unverified_manifest": False,
                "resolve_attempts": resolution.attempts,
                "selected_count": len(selected),
                "excluded_count": max(0, len(choices) - len(selected)),
                "task_ids": task_ids,
                "batch_count": int(created.get("batch_count") or 0),
                "completed_batches": completed_batches,
                "partial_success": partial_success,
                "outcome_unknown": bool(created.get("outcome_unknown")),
                "tracking_incomplete": bool(created.get("tracking_incomplete")),
                "staging": {
                    "id": staging_id,
                    "parent_id": str(decision.target_dir_id or "0"),
                    "name": staging_name if isolate_task else "",
                    "isolated": bool(staging_id),
                    "cleanup_status": staging_cleanup_status,
                    "cleanup_error": staging_cleanup_error,
                },
            }
            if not ok:
                return {
                    "ok": False, **common,
                    "error": str(created.get("error") or "光鸭任务创建失败"),
                }
            return {"ok": True, **common, "error": ""}
        # HTTP/ED2K 若上游只能确认单文件且没有文件树，保留兼容路径；磁力已在上方关闭。
        created = client.add_offline_task(
            url=url, target_dir_id=effective.target_dir_id, task_type=effective.protocol,
        )
        if isinstance(created, dict):
            ok = bool(created.get("ok"))
            task_ids = [str(item) for item in (created.get("task_ids") or []) if str(item)]
            batch_count = int(created.get("batch_count") or (1 if ok else 0))
            create_error = str(created.get("error") or "")
        else:
            ok = bool(created)
            task_ids = []
            batch_count = 1 if ok else 0
            create_error = ""
        if not ok:
            staging_cleanup_status, staging_cleanup_error = _remove_empty_staging(
                client, staging_id
            )
        else:
            staging_cleanup_status = "pending" if staging_id else ""
            staging_cleanup_error = ""
        return {
            "ok": ok, "decision": effective.as_dict(),
            "selection_mode": "legacy_unverified_magnet" if unverified_manifest else "legacy",
            "unverified_manifest": unverified_manifest,
            "resolve_attempts": resolution.attempts,
            "selected_count": 0, "excluded_count": 0,
            "task_ids": task_ids, "batch_count": batch_count,
            "staging": {"id": staging_id, "parent_id": str(decision.target_dir_id or "0"),
                        "name": staging_name, "isolated": bool(staging_id),
                        "cleanup_status": staging_cleanup_status,
                        "cleanup_error": staging_cleanup_error},
            "error": "" if ok else (create_error or "光鸭任务创建失败"),
        }
    except Exception as exc:
        cleanup_status, cleanup_error = _remove_empty_staging(client, staging_id)
        return {
            "ok": False, "decision": effective.as_dict(),
            "staging": {
                "id": staging_id,
                "parent_id": str(decision.target_dir_id or "0"),
                "name": staging_name,
                "isolated": bool(staging_id),
                "cleanup_status": cleanup_status,
                "cleanup_error": cleanup_error,
            },
            "outcome_unknown": True,
            "error": f"光鸭任务创建失败: {exc}",
        }


def preview_offline_selection(url: str, title: str = "", client: GuangYaClient | None = None,
                              rules: OfflineRules | None = None) -> dict:
    """解析离线资源并返回可调整的默认文件选集。"""
    rules = rules or OfflineRules.from_config()
    decision = analyze_offline_url(url, title=title, rules=rules)
    result = {
        **decision.as_dict(),
        "ok": False,
        "has_file_tree": False,
        "files": [],
        "default_selected_indexes": [],
        "error": "" if decision.allowed else decision.reason,
    }
    if not decision.allowed:
        return result
    client = client or GuangYaClient()
    if not client.logged_in:
        result["error"] = "光鸭未登录"
        return result
    try:
        resolution = _resolve_offline_manifest(client, url, decision.protocol)
        choices = build_offline_file_choices(resolution.files, rules)
    except Exception as exc:
        result["error"] = f"光鸭资源解析失败: {exc}"
        return result
    if decision.protocol == "magnet" and not choices:
        result["resolve_attempts"] = resolution.attempts
        result["resolve_diagnostic"] = resolution.diagnostic
        result["error"] = "磁力资源未解析到可验证的文件列表"
        return result
    result.update({
        "ok": True,
        "has_file_tree": bool(choices),
        "files": choices,
        "default_selected_indexes": [item["index"] for item in choices if item["selected"]],
        "error": "",
    })
    return result


def submit_offline_selection(url: str, selected_indexes: list[int] | None,
                             title: str = "", client: GuangYaClient | None = None,
                             rules: OfflineRules | None = None,
                             expected_target_dir_id: str = "",
                             expected_target_dir_name: str = "") -> dict:
    """重新解析资源、验证客户端索引并创建选集任务。"""
    rules = rules or OfflineRules.from_config()
    decision = analyze_offline_url(url, title=title, rules=rules)
    base = {"ok": False, "decision": decision.as_dict(), "error": ""}
    if not decision.allowed:
        return {**base, "error": decision.reason}
    if expected_target_dir_id and (
        decision.target_dir_id != str(expected_target_dir_id)
        or decision.target_dir_name != str(expected_target_dir_name)
    ):
        return {**base, "error": "预览目标已变化，请重新解析"}
    client = client or GuangYaClient()
    if not client.logged_in:
        return {**base, "error": "光鸭未登录"}
    try:
        resolution = _resolve_offline_manifest(client, url, decision.protocol)
        files = resolution.files
    except Exception as exc:
        return {**base, "error": f"光鸭资源解析失败: {exc}"}

    try:
        requested = _normalize_selected_indexes(selected_indexes or [])
    except ValueError as exc:
        return {**base, "error": str(exc)}

    if not files:
        if decision.protocol == "magnet":
            return {**base, "resolve_attempts": resolution.attempts, "resolve_diagnostic": resolution.diagnostic, "error": "磁力资源未解析到可验证的文件列表"}
        if requested:
            return {**base, "error": "资源没有可验证的文件列表，请重新预览"}
        try:
            created = client.add_offline_task(
                url=url,
                target_dir_id=decision.target_dir_id,
                task_type=decision.protocol,
            )
        except Exception as exc:
            return {**base, "error": f"光鸭任务创建失败: {exc}"}
        if isinstance(created, dict):
            ok = bool(created.get("ok"))
            task_ids = [str(item) for item in (created.get("task_ids") or []) if str(item)]
            batch_count = int(created.get("batch_count") or (1 if ok else 0))
            create_error = str(created.get("error") or "")
        else:
            ok = bool(created)
            task_ids = []
            batch_count = 1 if ok else 0
            create_error = ""
        return {
            **base,
            "ok": ok,
            "error": "" if ok else (create_error or "光鸭任务创建失败"),
            "selection_mode": "legacy",
            "selected_count": 0,
            "task_ids": task_ids,
            "batch_count": batch_count,
        }

    if not requested:
        return {**base, "error": "至少选择一个文件"}
    available = {int(item["index"]) for item in files}
    invalid = [index for index in requested if index not in available]
    if invalid:
        return {**base, "error": f"选择中包含不存在的文件索引: {', '.join(map(str, invalid))}"}
    choices = build_offline_file_choices(files, rules)
    allowed = {int(item["index"]) for item in choices if item.get("selected")}
    forbidden = [index for index in requested if index not in allowed]
    if forbidden:
        reasons = {
            int(item["index"]): str(item.get("exclude_reason") or "不符合下载规则")
            for item in choices
        }
        detail = "；".join(f"{index}: {reasons.get(index, '不符合下载规则')}" for index in forbidden)
        return {**base, "error": f"选择中包含被下载规则排除的文件: {detail}"}
    try:
        created = client.add_offline_selection(url, decision.target_dir_id, requested)
    except Exception as exc:
        return {**base, "error": f"光鸭任务创建失败: {exc}"}
    if not created.get("ok", True):
        return {
            **base,
            "partial_success": bool(created.get("partial_success")),
            "selection_mode": "files",
            "selected_count": int(created.get("selected_count", len(requested))),
            "completed_batches": int(created.get("completed_batches", 0)),
            "task_ids": list(created.get("task_ids") or []),
            "completed_indexes": list(created.get("completed_indexes") or []),
            "remaining_indexes": list(created.get("remaining_indexes") or requested),
            "failed_batch": created.get("failed_batch"),
            "batch_count": int(created.get("batch_count", 0)),
            "error": str(created.get("error") or "光鸭任务创建失败"),
        }
    return {
        **base,
        "ok": True,
        "partial_success": False,
        "error": "",
        "selection_mode": "files",
        "selected_count": int(created.get("selected_count", len(requested))),
        "task_ids": list(created.get("task_ids") or []),
        "batch_count": int(created.get("batch_count", 0)),
        "completed_batches": int(created.get("completed_batches", created.get("batch_count", 0))),
        "completed_indexes": list(created.get("completed_indexes") or requested),
        "remaining_indexes": [],
        "failed_batch": None,
    }


def rules_summary(rules: OfflineRules | None = None) -> dict:
    rules = rules or OfflineRules.from_config()
    return {
        "protocols": {
            "magnet": rules.magnet_enabled,
            "ed2k": rules.ed2k_enabled,
            "http": rules.http_enabled,
        },
        "target": {"id": rules.target_dir_id, "name": rules.target_dir_name},
        "secondary": {
            "enabled": rules.secondary_enabled,
            "target": {"id": rules.secondary_dir_id, "name": rules.secondary_dir_name},
            "keywords": list(rules.secondary_keywords),
        },
        "exclude_keywords": list(rules.exclude_keywords),
        "min_file_mb": rules.min_file_mb,
        "allowed_exts": list(rules.allowed_exts or sorted(DEFAULT_MEDIA_EXTS)),
        "file_filter_enforced": True,
        "file_filter_note": "资源预解析后按扩展名、最小体积和排除词生成默认选集，可在提交前调整。",
    }


def build_offline_file_choices(files: list[dict], rules: OfflineRules) -> list[dict]:
    """为归一化文件列表补充默认选择、锁定状态和排除原因。"""
    allowed_exts = set(rules.allowed_exts or DEFAULT_MEDIA_EXTS)
    minimum_size = max(0, rules.min_file_mb) * 1024 * 1024
    choices: list[dict] = []
    for source in files:
        item = {
            "index": int(source.get("index", 0)),
            "name": str(source.get("name", "")),
            "size": max(0, _to_int(source.get("size"), 0)),
            "excluded": bool(source.get("excluded", False)),
        }
        reason = ""
        locked = item["excluded"]
        lowered_name = item["name"].lower()
        extension = lowered_name.rsplit(".", 1)[-1] if "." in lowered_name else ""
        if item["excluded"]:
            reason = "解析器标记为排除"
        else:
            for keyword in rules.exclude_keywords:
                if keyword.lower() in lowered_name:
                    reason = f"命中排除词: {keyword}"
                    break
            if not reason and extension not in allowed_exts:
                reason = "扩展名不允许"
            if not reason and minimum_size and item["size"] < minimum_size:
                reason = f"小于 {rules.min_file_mb} MB"
        item.update({
            "selected": not reason,
            "locked": locked,
            "exclude_reason": reason,
        })
        choices.append(item)
    return choices


def validate_preview_indexes(snapshot: OfflinePreviewSnapshot, values: list[int]) -> list[int]:
    """先按预览快照限制选择范围；提交阶段仍需重新解析做第二次验证。"""
    requested = _normalize_selected_indexes(values)
    available = set(snapshot.file_indexes)
    invalid = [index for index in requested if index not in available]
    if invalid:
        raise ValueError(f"选择中包含不属于该预览的文件索引: {', '.join(map(str, invalid))}")
    locked = set(snapshot.locked_indexes)
    forbidden = [index for index in requested if index in locked]
    if forbidden:
        raise ValueError(f"选择中包含解析器禁止的文件索引: {', '.join(map(str, forbidden))}")
    if available and not requested:
        raise ValueError("至少选择一个文件")
    return requested


def detect_protocol(url: str) -> str:
    value = (url or "").strip().lower()
    if value.startswith(MAGNET_PREFIX):
        return "magnet"
    if value.startswith(ED2K_PREFIX):
        return "ed2k"
    if urlparse(value).scheme in ("http", "https"):
        return "http"
    return "unknown"


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，\n]+", raw or "") if item.strip()]


def _extensions(raw: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in _split(raw):
        ext = item.lower().lstrip(".")
        if ext and re.fullmatch(r"[a-z0-9]{1,10}", ext) and ext not in seen:
            seen.add(ext)
            result.append(ext)
    return result


def _normalize_selected_indexes(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise ValueError("文件索引必须为非负整数")
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("文件索引必须为非负整数") from exc
        if index < 0:
            raise ValueError("文件索引必须为非负整数")
        if index not in seen:
            seen.add(index)
            result.append(index)
    return result
