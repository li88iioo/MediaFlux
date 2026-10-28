"""光鸭离线转存规则与提交入口。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.clients.guangya import GuangYaClient, close_guangya_client, single_bt_root_file
from app.config import get, get_bool, get_int
from app.indexers.providers.base import augment_public_magnet
from app.logger import get_logger, redact_sensitive_text
from app.modules.naming import sanitize_name
from app.modules.special_media import is_sample_media_path

logger = get_logger(__name__)

MAGNET_PREFIX = "magnet:?"
ED2K_PREFIX = "ed2k://"
# 光鸭“仅视频”先按清单过滤；多文件用真实 fileIndexes，单文件根直接提交。
# 默认只含视频/光盘镜像格式，避免音频、图片、文本和广告附件进入云盘。
DEFAULT_VIDEO_EXTS = (
    "mkv", "mp4", "ts", "m2ts", "mts", "iso", "avi", "mov", "m4v",
    "webm", "mpeg", "mpg", "wmv", "flv", "vob", "tp", "f4v", "rm", "rmvb",
)
DEFAULT_VIDEO_EXTS_CSV = ",".join(DEFAULT_VIDEO_EXTS)
_DEFAULT_VIDEO_EXT_SET = frozenset(DEFAULT_VIDEO_EXTS)


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
            allowed_exts=normalize_video_extensions(get("OFFLINE_ALLOWED_EXTS", "")),
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
            allowed_exts=normalize_video_extensions(str(data.get("allowed_exts", ""))),
        )


@dataclass(frozen=True)
class OfflineManifestResolution:
    response: dict
    files: list[dict]
    attempts: int
    diagnostic: str


class TorrentPreparationError(ValueError):
    """提交前的种子读取/校验失败；仅携带安全文案与稳定分类。"""

    def __init__(self, message: str, *, failure_code: str, stage: str,
                 retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.failure_code = failure_code
        self.stage = stage
        self.retryable = retryable
        self.http_status = http_status


class OfflineManifestError(RuntimeError):
    """解析请求/响应失败，不等同于请求成功但文件清单尚不可用。

    只保留错误类型、状态码与外层解析次数，不携带上游响应、URL 或凭据。
    """

    def __init__(self, exc: Exception, *, attempts: int = 1):
        current: BaseException = exc
        visited: set[int] = set()
        while id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (httpx.HTTPError, TimeoutError, ValueError)):
                break
            cause = current.__cause__ or current.__context__
            if cause is None:
                break
            current = cause
        self.attempts = attempts
        self.retry_manifest = isinstance(current, RuntimeError)
        self.error_type = type(current).__name__
        self.http_status = (
            current.response.status_code if isinstance(current, httpx.HTTPStatusError) else 0
        )
        self.failure_code = ""
        self.retryable = False
        if self.http_status == 429:
            self.failure_code = "guangya_rate_limited"
            self.retryable = True
        elif self.http_status in (401, 403):
            self.failure_code = "guangya_auth_failed"
        elif 500 <= self.http_status < 600 or isinstance(
            current, (httpx.TimeoutException, TimeoutError, httpx.TransportError, ConnectionError)
        ):
            self.failure_code = "guangya_unavailable"
            self.retryable = True

        if self.http_status:
            message = f"光鸭资源解析请求失败（HTTP {self.http_status}）"
        elif isinstance(current, (httpx.TimeoutException, TimeoutError)):
            message = "光鸭资源解析请求超时"
        elif isinstance(current, httpx.TransportError):
            message = "光鸭资源解析网络异常"
        elif isinstance(current, ValueError):
            message = "光鸭资源解析响应无效"
        else:
            message = "光鸭资源解析失败"
        super().__init__(message)

    def as_result(self) -> dict:
        result = {
            "ok": False,
            "error": f"{self}，未创建下载任务",
            "resolve_attempts": self.attempts,
            "resolve_error_type": self.error_type,
            "resolve_http_status": self.http_status,
            "resolve_diagnostic": f"解析异常={self.error_type}；HTTP={self.http_status or '-'}",
        }
        if self.failure_code:
            result["failure_code"] = self.failure_code
            result["retryable"] = self.retryable
        return result


def _manifest_failure(exc: Exception) -> dict:
    if isinstance(exc, TorrentPreparationError):
        return {
            "ok": False, "error": f"{exc}，未创建下载任务",
            "failure_code": exc.failure_code, "retryable": exc.retryable,
            "resolve_attempts": 0,
            "resolve_error_type": type(exc.__cause__ or exc).__name__,
            "resolve_http_status": exc.http_status or 0,
            "resolve_diagnostic": f"种子准备阶段={exc.stage}；HTTP={exc.http_status or '-'}",
        }
    failure = exc if isinstance(exc, OfflineManifestError) else OfflineManifestError(exc)
    return failure.as_result()


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


def _resolver_excluded_only(files: list[dict]) -> bool:
    """解析器只返回禁止选择项时，视为尚未得到可验证清单。"""
    return bool(files) and all(bool(item.get("excluded")) for item in files)


def _resolve_offline_manifest(
    client: GuangYaClient, url: str, protocol: str, *,
    torrent_data: bytes | None = None,
) -> OfflineManifestResolution:
    from_torrent = bool(torrent_data)
    source_label = "种子" if from_torrent else "磁力"
    attempts = 1
    if protocol == "magnet":
        attempts = max(1, min(get_int("OFFLINE_MAGNET_RESOLVE_ATTEMPTS", 4), 6))
    delay = max(0.0, min(float(get("OFFLINE_MAGNET_RESOLVE_DELAY_SECONDS", "0.5") or 0.5), 5.0))
    last_response: dict = {}
    last_files: list[dict] = []
    for attempt in range(1, attempts + 1):
        try:
            response = (
                client.resolve_torrent(torrent_data)
                if from_torrent and torrent_data is not None
                else client.resolve_url(url)
            )
            last_response = response if isinstance(response, dict) else {"raw_type": type(response).__name__}
            last_files = GuangYaClient.normalize_offline_files(last_response)
        except Exception as exc:
            # HTTP/网络的有界重试由 GuangYaClient._call_read 统一负责。
            # 保留 SDK 业务 RuntimeError 的磁力轮询，但最终异常不得报为空清单。
            failure = OfflineManifestError(exc, attempts=attempt)
            logger.warning(
                "光鸭%s解析失败 attempts=%s type=%s http_status=%s",
                source_label, attempt, failure.error_type, failure.http_status or "-",
            )
            if failure.retry_manifest and not from_torrent and attempt < attempts:
                if delay > 0:
                    time.sleep(delay * attempt)
                continue
            raise failure from exc
        resolver_excluded_only = _resolver_excluded_only(last_files)
        if protocol != "magnet" or (last_files and not resolver_excluded_only):
            return OfflineManifestResolution(last_response, last_files, attempt, _manifest_diagnostic(last_response))
        if attempt < attempts and delay > 0:
            time.sleep(delay * attempt)
    diagnostic = (
        f"解析器仅返回排除项；{_manifest_diagnostic(last_response)}"
        if _resolver_excluded_only(last_files)
        else _manifest_diagnostic(last_response)
    )
    logger.warning(
        "光鸭%s解析未返回可用文件清单 attempts=%s diagnostic=%s",
        source_label, attempts, diagnostic,
    )
    return OfflineManifestResolution(last_response, last_files, attempts, diagnostic)


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


def _is_http_torrent_url(url: str, content_type: str = "") -> bool:
    """HTTP 是种子的传输载体，不是交给云盘保存的媒体文件协议。"""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if (content_type or "").split(";", 1)[0].strip().lower() in {
        "application/x-bittorrent", "application/bittorrent",
    }:
        return True
    if unquote(parsed.path).lower().endswith(".torrent"):
        return True
    query = parse_qs(parsed.query)
    return any(
        unquote(value).lower().endswith(".torrent")
        for key in ("filename", "file", "name") for value in query.get(key, ())
    )


def _prepare_offline_resource(
    url: str, torrent_data: bytes | None = None, *, content_type: str = "",
) -> tuple[str, bytes | None]:
    """三个离线入口共用种子载体转换；失败不能降级成普通 HTTP 文件下载。"""
    if not _is_http_torrent_url(url, content_type) and not (
        torrent_data is not None and url.lower().startswith(("http://", "https://"))
    ):
        return url, torrent_data
    from app.modules.download_dispatcher import (
        http_torrent_infohash_hint,
        parse_torrent_metadata,
        torrent_download_input,
    )
    from app.modules.rss import _fetch_rss_payload

    # 复用 RSS 已有的公网地址校验、逐跳固定解析、有界响应和总时间预算，
    # 不再另写一套无限流式 HTTP 下载；原始 tracker/passkey 保留在种子内。
    if torrent_data is None:
        try:
            torrent_data, _headers = _fetch_rss_payload(
                url, user_agent="MediaFlux/1.0", timeout_seconds=20, allow_http=True,
            )
        except Exception as exc:
            http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                code, message, retryable = "torrent_fetch_timeout", "种子读取超时，请稍后重试", True
            elif http_status is not None:
                code, message = "torrent_http_error", f"种子下载服务器返回 HTTP {http_status}"
                retryable = http_status in {408, 425, 429} or 500 <= http_status < 600
            else:
                code, message = "torrent_fetch_failed", "种子读取失败，请检查下载地址或网络"
                retryable = isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError, OSError)) or (
                    isinstance(exc, ValueError) and isinstance(exc.__cause__, OSError)
                )
            raise TorrentPreparationError(
                message, failure_code=code, stage="fetch", retryable=retryable, http_status=http_status,
            ) from exc
    try:
        item = torrent_download_input("resource.torrent", torrent_data)
        actual_hash = parse_torrent_metadata(torrent_data)[1].lower()
    except ValueError as exc:
        raise TorrentPreparationError(
            "种子内容无效，请核对下载来源", failure_code="torrent_invalid", stage="validate",
        ) from exc
    expected_hash = http_torrent_infohash_hint(url)
    if expected_hash and actual_hash != expected_hash:
        raise TorrentPreparationError(
            "种子内容与链接中的 BT 身份不一致", failure_code="torrent_identity_mismatch", stage="validate",
        )
    return item.source_value, torrent_data


def analyze_offline_url(
    url: str, title: str = "", rules: OfflineRules | None = None, *, content_type: str = "",
) -> OfflineDecision:
    rules = rules or OfflineRules.from_config()
    cleaned = (url or "").strip()
    protocol = "magnet" if _is_http_torrent_url(cleaned, content_type) else detect_protocol(cleaned)
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
    label = (title or "离线下载").strip()
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", (task_key or "").strip()).strip("-")
    prefix = f"MF-{identity}" if identity else "MF-download"
    try:
        safe_title = sanitize_name(label)
    except ValueError:
        safe_title = "离线下载"
    return sanitize_name(f"{prefix}-{safe_title}"[:180])


def _remove_empty_staging(
    client: GuangYaClient,
    directory_id: str,
    *,
    expected_parent_id: str = "",
    expected_name: str = "",
) -> tuple[str, str]:
    if not directory_id:
        return "", ""
    try:
        info = client.file_info(directory_id)
        if info is None:
            return "completed", ""
        if not bool(getattr(info, "is_dir", False)):
            return "retained", "隔离目标已不再是目录，未自动删除"
        actual_parent = str(getattr(info, "parent_id", "") or "0")
        actual_name = str(getattr(info, "name", "") or "")
        if expected_parent_id and actual_parent != expected_parent_id:
            return "retained", "隔离目录父级已变化，未自动删除"
        if expected_name and actual_name != expected_name:
            return "retained", "隔离目录名称已变化，未自动删除"
        if client.list_dir(directory_id):
            return "retained", "临时目录仍包含文件，未自动删除"
        expected_etag = str(getattr(info, "etag", "") or "")
        try:
            expected_updated_at = max(
                0, int(getattr(info, "updated_at", 0) or 0)
            )
        except (TypeError, ValueError):
            expected_updated_at = 0
        delete_empty = getattr(client, "delete_empty_directory", None)
        supports_guarded = getattr(
            client, "supports_guarded_empty_directory_delete", None
        )
        if supports_guarded is None:
            supports_guarded = getattr(
                client, "supports_atomic_empty_directory_delete", None
            )
        if (
            not callable(delete_empty)
            or supports_guarded is False
            or (not expected_etag and not expected_updated_at)
        ):
            return (
                "retained",
                "Provider 不支持带版本与空目录复核的回收站删除，隔离目录已保留",
            )
        from app.modules.organize_delete_audit import (
            DeleteCandidate,
            execute_recycle_bin_delete,
        )

        execute_recycle_bin_delete(
            client,
            trigger="offline_staging_rollback",
            reason="离线任务明确未受理，清理已复核为空的隔离暂存目录",
            candidate=DeleteCandidate(
                file_id=directory_id,
                name=actual_name or (expected_name or "下载隔离目录"),
                parent_id=actual_parent,
                size=max(0, int(getattr(info, "size", 0) or 0)),
                gcid=expected_etag,
            ),
            safe_failure_message="离线隔离暂存目录清理失败，目录已保留",
            delete_operation=lambda: delete_empty(
                directory_id,
                expected_etag=expected_etag,
                expected_updated_at=expected_updated_at,
            ),
        )
        return "completed", ""
    except Exception as exc:
        logger.warning("光鸭临时下载目录清理失败 id=%s type=%s", directory_id, type(exc).__name__)
        return "failed", f"{type(exc).__name__}: {redact_sensitive_text(exc)[:240]}"


def submit_offline(url: str, title: str = "", client: GuangYaClient | None = None,
                   target_dir_id: str = "", target_dir_name: str = "", *,
                   isolate_task: bool = False, task_key: str = "",
                   torrent_data: bytes | None = None,
                   public_magnet_title: str | None = None,
                   on_staging_created: Callable[[dict], None] | None = None) -> dict:
    """按正式离线规则提交任务。

    自动入口先解析文件树并强制应用仅视频扩展名、排除词和最小体积。
    原始种子优先上传到光鸭解析，避免只转换为磁力后依赖 DHT 获取元数据；
    磁力无法取得可验证清单时一律失败关闭。单文件根资源通过同一筛选后普通提交；隔离目录只会
    在已经确认应下载的文件后创建，用于后续下载落稳与安全整理。
    """
    rules = OfflineRules.from_config()
    decision = analyze_offline_url(
        url, title=title, rules=rules,
        content_type="application/x-bittorrent" if torrent_data is not None else "",
    )
    if target_dir_id and decision.allowed:
        decision = OfflineDecision(
            True, decision.protocol, target_dir_id,
            (target_dir_name or "指定目标目录"), "使用指定目录",
            decision.matched_keyword,
        )
    if not decision.allowed:
        return {"ok": False, "decision": decision.as_dict(), "error": decision.reason}
    owned_client = client is None
    client = client or GuangYaClient()
    try:
        if not client.logged_in:
            return {
                "ok": False, "decision": decision.as_dict(), "error": "光鸭未登录",
                "failure_code": "guangya_auth_failed", "retryable": False,
            }

        try:
            url, torrent_data = _prepare_offline_resource(url, torrent_data)
            # 路由先基于原始输入决定；仅传输增强，原始种子（包括private）保持不变。
            if public_magnet_title is not None and torrent_data is None:
                url = augment_public_magnet(url, public_magnet_title)
            resolution = _resolve_offline_manifest(
                client,
                url,
                decision.protocol,
                torrent_data=torrent_data,
            )
            files = resolution.files
            choices = build_offline_file_choices(files, rules)
        except Exception as exc:
            return {
                "decision": decision.as_dict(), **_manifest_failure(exc),
            }

        manifest_unverifiable = not choices or _resolver_excluded_only(files)
        if decision.protocol == "magnet" and manifest_unverifiable:
            unresolved_error = (
                f"种子文件未解析到可验证文件列表（尝试 {resolution.attempts} 次），"
                if torrent_data else
                f"磁力资源连续 {resolution.attempts} 次未解析到可验证文件列表，"
            )
            return {
                "ok": False, "decision": decision.as_dict(),
                "resolve_attempts": resolution.attempts,
                "resolve_diagnostic": resolution.diagnostic,
                "error": (
                    f"{unresolved_error}已阻止整单下载；请稍后重试或更换资源。"
                ),
                "failure_code": "guangya_manifest_unavailable", "retryable": True,
            }

        selected = [int(item["index"]) for item in choices if item.get("selected")]
        if choices and not selected:
            detail = "；".join(
                f"{item.get('name') or item.get('index')}: {item.get('exclude_reason') or '不符合规则'}"
                for item in choices[:5]
            )
            return {
                "ok": False, "decision": decision.as_dict(),
                "resolve_attempts": resolution.attempts,
                "selected_count": 0, "excluded_count": len(choices),
                "error": "资源中没有符合仅视频规则的文件" + (f"（{detail}）" if detail else ""),
            }

        staging_id = ""
        staging_name = ""
        effective = decision
        if isolate_task:
            staging_name = _offline_staging_name(title, task_key)
            try:
                staging_id = (client.create_dir(staging_name, decision.target_dir_id) or "")
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
            if on_staging_created is not None:
                staging_snapshot = {
                    "id": staging_id,
                    "parent_id": (decision.target_dir_id or "0"),
                    "parent_name": (decision.target_dir_name or ""),
                    "name": staging_name,
                    "isolated": True,
                }
                try:
                    on_staging_created(dict(staging_snapshot))
                except Exception as exc:
                    cleanup_status, cleanup_error = _remove_empty_staging(
                        client,
                        staging_id,
                        expected_parent_id=(decision.target_dir_id or "0"),
                        expected_name=staging_name,
                    )
                    logger.warning(
                        "持久化光鸭隔离目录失败 task=%s type=%s",
                        (task_key or ""),
                        type(exc).__name__,
                    )
                    return {
                        "ok": False,
                        "decision": effective.as_dict(),
                        "staging": {
                            **staging_snapshot,
                            "cleanup_status": cleanup_status,
                            "cleanup_error": cleanup_error,
                        },
                        "error": "持久化任务隔离目录失败，未提交离线任务",
                    }

        # 单文件根资源的0是本地规则筛选索引，不是服务端选集索引。
        # 只依据同一严格解析器确认根资源，不能把“多文件里只选一个”当成单文件。
        single_file = single_bt_root_file(resolution.response) is not None
        selection_mode = "single_file" if single_file else "files" if choices else "legacy"
        try:
            created = (
                client.add_offline_selection(url, effective.target_dir_id, selected)
                if selection_mode == "files"
                else client.add_offline_task(
                    url=url, target_dir_id=effective.target_dir_id, task_type=effective.protocol,
                )
            )
            if isinstance(created, dict):
                ok = bool(created.get("ok"))
                task_ids = [str(item) for item in (created.get("task_ids") or []) if str(item)]
                batch_count = int(created.get("batch_count") or (1 if ok else 0))
                completed_batches = int(created.get("completed_batches") or (batch_count if ok else 0))
                partial_success = bool(created.get("partial_success") or task_ids or completed_batches)
                outcome_unknown = bool(created.get("outcome_unknown"))
                tracking_incomplete = bool(created.get("tracking_incomplete"))
                create_error = str(created.get("error") or "")
            else:
                ok = bool(created)
                task_ids = []
                batch_count = completed_batches = 1 if ok else 0
                partial_success = outcome_unknown = tracking_incomplete = False
                create_error = ""
            staging_cleanup_status = "pending" if staging_id else ""
            staging_cleanup_error = ""
            # 三类提交共用受理判断。只要已有任务或回执不明，就不能删除可能仍会写入的目录。
            if not ok and not partial_success and not outcome_unknown and not tracking_incomplete:
                staging_cleanup_status, staging_cleanup_error = _remove_empty_staging(
                    client,
                    staging_id,
                    expected_parent_id=(decision.target_dir_id or "0"),
                    expected_name=staging_name,
                )
            elif not ok and staging_id:
                staging_cleanup_status = "retained"
                staging_cleanup_error = "提交结果待核对，已保留隔离目录"
            return {
                "ok": ok, "decision": effective.as_dict(),
                "selection_mode": selection_mode,
                "unverified_manifest": False,
                "resolve_attempts": resolution.attempts,
                "selected_count": len(selected), "excluded_count": len(choices) - len(selected),
                "task_ids": task_ids, "batch_count": batch_count,
                "completed_batches": completed_batches, "partial_success": partial_success,
                "outcome_unknown": outcome_unknown,
                "tracking_incomplete": tracking_incomplete,
                "staging": {"id": staging_id, "parent_id": (decision.target_dir_id or "0"),
                            "name": staging_name, "isolated": bool(staging_id),
                            "cleanup_status": staging_cleanup_status,
                            "cleanup_error": staging_cleanup_error},
                "error": "" if ok else (create_error or "光鸭任务创建失败"),
            }
        except Exception as exc:
            # 请求异常无法证明服务端未受理；宁可保留空目录供后续核验，也不能
            # 删除一个远端可能仍会异步写入的任务目标。
            cleanup_status = "retained" if staging_id else ""
            cleanup_error = "提交结果待核对，已保留隔离目录" if staging_id else ""
            return {
                "ok": False, "decision": effective.as_dict(),
                "staging": {
                    "id": staging_id,
                    "parent_id": (decision.target_dir_id or "0"),
                    "name": staging_name,
                    "isolated": bool(staging_id),
                    "cleanup_status": cleanup_status,
                    "cleanup_error": cleanup_error,
                },
                "outcome_unknown": True,
                "error": f"光鸭任务创建失败: {exc}",
            }
    finally:
        if owned_client:
            close_guangya_client(client)


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
        "allowed_exts": list(rules.allowed_exts or sorted(DEFAULT_VIDEO_EXTS)),
        "file_filter_enforced": True,
        "file_filter_note": "资源预解析后按扩展名、最小体积和排除词生成默认选集，可在提交前调整。",
    }


def build_offline_file_choices(
    files: list[dict], rules: OfflineRules,
) -> list[dict]:
    """统一离线提交按用户规则和解析器排除项筛选文件，并排除样片。"""
    allowed_exts = set(rules.allowed_exts or DEFAULT_VIDEO_EXTS)
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
            if not reason and is_sample_media_path(item["name"]):
                reason = "样片不自动下载"
        item.update({
            "selected": not reason,
            "locked": locked,
            "exclude_reason": reason,
        })
        choices.append(item)
    return choices


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


def normalize_video_extensions(raw: str) -> tuple[str, ...]:
    """只保留 MediaFlux 支持的离线视频扩展名；空值使用内置仅视频列表。"""
    values = _extensions(raw)
    if not values:
        return DEFAULT_VIDEO_EXTS
    return tuple(item for item in values if item in _DEFAULT_VIDEO_EXT_SET)
