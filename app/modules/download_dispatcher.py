"""统一下载请求分发与幂等控制。"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from app import database as db
from app.clients.qbittorrent import (
    QBittorrentClient,
    QBTorrentExportError,
    close_qbittorrent_client,
)
from app.config import get
from app.indexers.providers.base import magnet_infohash
from app.logger import get_logger
from app.modules.offline import analyze_offline_url, detect_protocol, submit_offline

logger = get_logger(__name__)

SUPPORTED_TARGETS = {"qb", "guangya", "both"}
_URL_RE = re.compile(r"(?i)(magnet:\?\S+|ed2k://\S+|https?://\S+)")
_QB_TORRENT_ID_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GUANGYA_SHARE_PATH_RE = re.compile(
    r"/(?:s|share)/[A-Za-z0-9][A-Za-z0-9_-]*(?:/|$)",
    re.IGNORECASE,
)
_HTTP_DOWNLOAD_SUFFIXES = frozenset({
    ".torrent", ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m2ts", ".mp3", ".flac", ".aac", ".wav",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".iso", ".img", ".apk", ".exe", ".msi", ".dmg", ".pkg",
    ".pdf", ".epub", ".srt", ".ass", ".ssa", ".jpg", ".jpeg",
    ".png", ".webp",
})
_HTTP_DOWNLOAD_PATH_RE = re.compile(
    r"(?:^|[-_.])(?:download|downloads|dl|get|fetch|attachment)(?:[-_.]|$)",
    re.IGNORECASE,
)
_HTTP_DOWNLOAD_QUERY_KEYS = frozenset({
    "attachment", "dl", "download", "file", "filename",
    "response-content-disposition",
})
_HTTP_SIGNED_QUERY_KEYS = frozenset({
    "expires", "policy", "sign", "signature", "token",
    "x-amz-signature", "x-oss-signature",
})


@dataclass(frozen=True)
class DownloadInput:
    kind: str
    title: str
    source_value: str = ""
    torrent_data: bytes | None = None
    # 仅供已经从可信 RSS/索引元数据中提取到协议身份、但仍需保留原始
    # HTTP .torrent 地址的入口使用。格式必须是 btih:<40hex> 或
    # btmh:<64hex>；request_keys 会再次校验，非法提示不会参与幂等。
    identity_hint: str = ""
    # RSS enclosure 的类型仅作为读取种子的线索；身份仍必须从真实bytes校验。
    content_type: str = ""


@dataclass(frozen=True, slots=True)
class TorrentManifestFile:
    """Torrent 内一条经过路径安全校验的文件记录。"""

    path: tuple[str, ...]
    length: int

    @property
    def relative_path(self) -> str:
        return "/".join(self.path)


@dataclass(frozen=True, slots=True)
class TorrentManifest:
    """只包含整理基准所需的安全文件清单，不保留 tracker 或下载地址。"""

    name: str
    version: str
    files: tuple[TorrentManifestFile, ...]


class BencodeError(ValueError):
    pass


def extract_download_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    return match.group(1).rstrip(".,，。;；)）]") if match else ""


def is_guangya_share_url(url: str) -> bool:
    """仅识别光鸭官方域名下的 /s/ 或 /share/ 链接，防 lookalike host。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    official_host = host == "guangyapan.com" or host.endswith(".guangyapan.com")
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and official_host
        and _GUANGYA_SHARE_PATH_RE.search(unquote(parsed.path))
    )


def _is_private_or_local_host(hostname: str) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".local", ".localhost")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def is_probable_http_download_url(url: str) -> bool:
    """区分明确下载直链与普通网页，避免 Telegram 分享网页时误建任务。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False

    path = unquote(parsed.path or "")
    basename = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if any(basename.endswith(suffix) for suffix in _HTTP_DOWNLOAD_SUFFIXES):
        return True

    # 局域网/本机的无扩展名 URL 更可能是管理页面或 API；不得仅凭
    # /download 一类页面名自动触发写操作。明确文件后缀仍保留直链能力。
    if _is_private_or_local_host(parsed.hostname):
        return False

    query_keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    if query_keys & _HTTP_DOWNLOAD_QUERY_KEYS:
        return True
    if path and query_keys & _HTTP_SIGNED_QUERY_KEYS:
        return True

    path_segments = [segment for segment in path.split("/") if segment]
    return any(_HTTP_DOWNLOAD_PATH_RE.search(segment) for segment in path_segments)


def route_download_url(url: str) -> str:
    """路由分享、下载直链和普通网页；普通网页交给 Agent/忽略。"""
    if is_guangya_share_url(url):
        return "guangya_share"
    item = normalize_download_url(url)
    if item.kind == "http" and not is_probable_http_download_url(url):
        return "web"
    return item.kind


def normalize_download_url(url: str) -> DownloadInput:
    value = (url or "").strip()
    protocol = detect_protocol(value)
    if protocol == "unknown":
        raise ValueError("仅支持 magnet、ED2K 或 HTTP(S) 链接")
    _validate_download_url(value, protocol)
    title = _title_from_url(value)
    return DownloadInput(kind=protocol, title=title, source_value=value)


def _validate_download_url(value: str, protocol: str) -> None:
    """拒绝只有协议前缀、但不具备可下载标识的伪链接。"""
    if protocol == "magnet":
        if not magnet_infohash(value):
            raise ValueError("磁力链接缺少有效的 BTIH/BTMH 信息哈希")
        return
    if protocol == "ed2k":
        parts = value.split("|")
        if (
            len(parts) < 6
            or parts[1].lower() != "file"
            or not parts[2].strip()
            or not parts[3].isdigit()
            or int(parts[3]) <= 0
            or not re.fullmatch(r"[0-9a-fA-F]{32}", parts[4])
        ):
            raise ValueError("ED2K 链接格式无效或缺少文件哈希")
        return
    if protocol == "http":
        parsed = urlparse(value)
        if not parsed.hostname:
            raise ValueError("HTTP(S) 链接缺少有效域名")


def torrent_download_input(filename: str, data: bytes) -> DownloadInput:
    name, _torrent_id, magnet_xt, _v2_hash = _torrent_metadata(data)
    title = name or (filename or "torrent").rsplit(".", 1)[0]
    magnet = f"magnet:?xt={magnet_xt}"
    if title:
        magnet += f"&dn={quote(title)}"
    return DownloadInput(kind="torrent", title=title, source_value=magnet, torrent_data=data)


_BTIH_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_BTIH_BASE32_RE = re.compile(r"^[A-Z2-7]{32}$", re.IGNORECASE)
_BTMH_SHA256_RE = re.compile(r"^1220[0-9a-fA-F]{64}$")


def http_torrent_infohash_hint(value: str) -> str:
    """读取 HTTP 种子地址路径中显式携带的 v1 infohash。"""
    try:
        path = unquote(urlparse(str(value or "")).path)
    except ValueError:
        return ""
    for part in reversed([item for item in path.split("/") if item]):
        match = re.fullmatch(r"(?i)([0-9a-f]{40})(?:\.torrent)?", part)
        if match:
            return match.group(1).lower()
    return ""


def _hash_request_identity(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _magnet_request_identity(source_value: str) -> tuple[str, str, str]:
    xt_values = [
        str(value or "").strip()
        for value in parse_qs(urlparse(source_value).query).get("xt", [])
    ]
    # Hybrid magnet 优先 v1 BTIH；只返回同命名空间的 xt，避免把任意 BTMH
    # 与一个不相关 BTIH 通过历史别名错误合并。
    for xt in xt_values:
        prefix = "urn:btih:"
        if not xt.lower().startswith(prefix):
            continue
        raw = xt[len(prefix):]
        if _BTIH_HEX_RE.fullmatch(raw):
            return "btih", raw.lower(), xt.lower()
        if _BTIH_BASE32_RE.fullmatch(raw):
            try:
                return "btih", base64.b32decode(raw.upper()).hex(), xt.lower()
            except (ValueError, TypeError):
                continue
    for xt in xt_values:
        prefix = "urn:btmh:"
        if not xt.lower().startswith(prefix):
            continue
        multihash = xt[len(prefix):]
        if _BTMH_SHA256_RE.fullmatch(multihash):
            return "btmh", multihash[4:].lower(), xt.lower()
    return "", "", ""


def prepare_download_input(item: DownloadInput, target: str) -> DownloadInput:
    """在统一防重前校验HTTP种子内容；qB单通道仍可直接接受原链接。"""
    if target not in {"guangya", "both"} or item.kind != "http":
        return item
    from app.modules.offline import _is_http_torrent_url, _prepare_offline_resource

    if not item.torrent_data and not _is_http_torrent_url(item.source_value, item.content_type):
        return item
    try:
        _url, payload = _prepare_offline_resource(
            item.source_value, item.torrent_data, content_type=item.content_type,
        )
    except Exception as exc:
        # 传输错误可能带认证URL；只对外暴露稳定安全的错误，不回退HTTP。
        raise ValueError("种子读取或校验失败，请核对资源后重试") from exc
    return replace(item, torrent_data=payload, identity_hint="", content_type="application/x-bittorrent")


def request_keys(item: DownloadInput) -> tuple[str, ...]:
    """返回内容协议身份对应的规范请求 key。

    BTIH 与 BTMH 使用独立命名空间；BTIH 统一 hex/base32 表示，v2 保留完整
    SHA-256，避免与同前 20 bytes 的 v1 infohash 误去重。Hybrid torrent
    同时登记经原始 info 字典证明的 BTIH/BTMH 身份。
    """
    kind = str(item.kind or "").strip().lower()
    source_value = str(item.source_value or "").strip()
    namespace = ""
    identity = ""
    verified_btmh_identity = ""
    if kind in {"torrent", "http"} and item.torrent_data:
        _name, torrent_id, magnet_xt, v2_hash = _torrent_metadata(item.torrent_data)
        if magnet_xt.lower().startswith("urn:btmh:1220"):
            namespace = "btmh"
            identity = magnet_xt[len("urn:btmh:1220"):].lower()
        else:
            namespace = "btih"
            identity = torrent_id.lower()
            # Hybrid torrent 的 raw info 同时证明 v1/v2 身份映射；仅在这一
            # 可信输入上登记 BTMH 身份，避免把任意外部 BTIH/BTMH 错误合并。
            verified_btmh_identity = v2_hash.lower()
    elif kind == "magnet":
        namespace, identity, _original_xt = _magnet_request_identity(source_value)

    identities: list[str] = []
    identity_hint = str(getattr(item, "identity_hint", "") or "").strip().lower()
    if kind == "http" and not item.torrent_data and (
        re.fullmatch(r"btih:[0-9a-f]{40}", identity_hint)
        or re.fullmatch(r"btmh:[0-9a-f]{64}", identity_hint)
    ):
        identities.append(identity_hint)
    if namespace and identity:
        identities.append(f"{namespace}:{identity}")
        if namespace == "btih" and verified_btmh_identity:
            identities.append(f"btmh:{verified_btmh_identity}")
    elif kind == "magnet":
        query = parse_qs(urlparse(source_value).query)
        xt = str((query.get("xt") or [""])[0] or "").lower()
        identities.append(f"magnet:{xt or source_value.lower()}")
    else:
        identities.append(f"{kind}:{source_value}")

    if kind == "http" and namespace:
        # 原URL也是兼容别名，保留历史条目与来源；canonical使用已校验BT内容。
        identities.append(f"http:{source_value}")
    return tuple(dict.fromkeys(_hash_request_identity(value) for value in identities))


def request_source_alias_key(item: DownloadInput) -> str:
    """保留HTTP来源用于历史兼容；它不是BT内容的等价证明。"""
    return _hash_request_identity(f"http:{item.source_value.strip()}") if item.kind == "http" else ""


def request_key(item: DownloadInput) -> str:
    return request_keys(item)[0]


def create_request(
    item: DownloadInput,
    chat_id: str,
    message_id: str,
    origin: str = "telegram",
    *,
    user_id: str = "",
    supersede_request_id: int | None = None,
    admission_id: int | None = None,
    initial_targets: str = "",
    retry_source_request_id: int | None = None,
    retry_source_status: str = "",
) -> dict[str, Any]:
    keys = request_keys(item)
    req_id, created = db.create_download_request(
        keys[0], item.kind, title=item.title,
        source_value=item.source_value, torrent_data=item.torrent_data, content_type=item.content_type,
        chat_id=str(chat_id), user_id=str(user_id), message_id=str(message_id), origin=origin,
        supersede_request_id=supersede_request_id,
        alternate_request_keys=keys[1:],
        source_alias_key=request_source_alias_key(item),
        admission_id=admission_id,
        initial_targets=initial_targets,
        retry_source_request_id=retry_source_request_id,
        retry_source_status=retry_source_status,
    )
    row = db.get_download_request(req_id)
    return {"id": req_id, "created": created, "status": row["status"] if row else ""}


_ACTIVE_BACKEND_STATUSES = {"submitting", "submitted", "downloading", "outcome_unknown"}


def _guangya_outcome_unknown(result: dict[str, Any]) -> bool:
    """光鸭可能已接收部分或全部请求时，禁止把结果降级为可自动重试的失败。"""
    if result.get("ok"):
        return False
    return bool(
        result.get("outcome_unknown")
        or result.get("partial_success")
        or result.get("tracking_incomplete")
        or result.get("task_id")
        or result.get("task_ids")
    )


def _backend_submission_status(source: str, result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "submitted"
    if source == "qb" and result.get("failure_code") == "qb_outcome_unknown":
        return "outcome_unknown"
    if source == "guangya" and _guangya_outcome_unknown(result):
        return "outcome_unknown"
    return "failed"


def _submission_has_unknown(results: dict[str, dict[str, Any]]) -> bool:
    return any(
        _backend_submission_status(source, result) == "outcome_unknown"
        for source, result in results.items()
    )


def _finalize_submission_state(
    request_id: int,
    claimed_targets: tuple[str, ...] | list[str],
    updates: dict[str, Any],
) -> tuple[str, str]:
    committed_status = db.finalize_download_request_submission(
        int(request_id), claimed_targets, **updates
    )
    if committed_status is not None:
        return str(committed_status), ""

    current = db.get_download_request(int(request_id))
    current_status = str(current["status"] or "manual_review") if current else "manual_review"
    notice = (
        "下载后端返回结果时请求状态已被恢复或人工接管；"
        "本次迟到结果未覆盖当前记录，请核对远端任务"
    )
    logger.warning(
        "忽略下载提交迟到结果 request=%s targets=%s current_status=%s",
        int(request_id),
        ",".join(claimed_targets),
        current_status,
    )
    return current_status, notice


def _submission_log_error(result: dict[str, Any], late_notice: str) -> str:
    error = str(result.get("error") or "")
    if not late_notice:
        return error
    return f"{error}；{late_notice}" if error else late_notice


def _public_dispatch_targets(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    targets = set(value)
    return [target for target in ("qb", "guangya") if target in targets]


def _public_guangya_failure(error: str) -> str:
    """把内部光鸭错误归类为固定公开文案，禁止透传上游响应或资源地址。"""
    http_failure = re.search(r"光鸭资源解析请求失败（HTTP ([45][0-9]{2})）", error)
    if http_failure:
        return f"光鸭资源解析请求失败（HTTP {http_failure.group(1)}）"
    rules = (
        ("文件违规", "光鸭返回：文件违规"),
        ("光鸭资源解析请求超时", "光鸭资源解析请求超时"),
        ("光鸭资源解析网络异常", "光鸭资源解析网络异常"),
        ("光鸭资源解析响应无效", "光鸭资源解析响应无效"),
        ("光鸭未登录", "光鸭未登录"),
        ("资源中没有符合下载规则的文件", "光鸭未找到符合下载规则的文件"),
        ("种子文件未解析到可验证文件列表", "光鸭种子未解析到有效文件列表"),
        (
            "未解析到可验证文件列表",
            "光鸭暂未取得磁力文件清单，未创建下载任务；"
            "请稍后仅重试光鸭，或上传原始种子",
        ),
        ("创建任务隔离目录失败", "光鸭隔离目录创建失败"),
        ("光鸭资源解析失败", "光鸭资源解析失败"),
        ("光鸭任务创建失败", "光鸭任务创建失败"),
    )
    for marker, public_message in rules:
        if marker in error:
            return public_message
    return "光鸭提交失败"


def public_download_error(
    *,
    succeeded: Any = (),
    failed: Any = (),
    duplicate: bool = False,
    existing_status: Any = "",
    can_resubmit: bool = False,
    review_required: bool = False,
    error: Any = "",
) -> str:
    """把提交结果或持久化请求行归一为安全的公共错误原因。"""
    succeeded_targets = _public_dispatch_targets(succeeded)
    failed_targets = _public_dispatch_targets(failed)
    if duplicate:
        normalized_status = str(existing_status or "").strip().lower()
        if can_resubmit:
            return "已有历史任务"
        if normalized_status in _ACTIVE_BACKEND_STATUSES | {"pending"}:
            return "任务正在处理"
        return {"manual_review": "等待核对"}.get(
            normalized_status, "该下载请求已提交或正在处理"
        )
    if review_required:
        review_messages = (
            "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
            "部分下载后端已提交，其余结果待核对，请先核对下载器，勿直接重复提交",
        )
        return review_messages[bool(succeeded_targets)]
    if failed_targets == ["guangya"]:
        return _public_guangya_failure(str(error or ""))
    return {
        (False, False): "下载提交失败",
        (False, True): "下载提交失败",
        (True, False): "",
        (True, True): "部分下载目标提交失败",
    }[bool(succeeded_targets), bool(failed_targets)]

def public_dispatch_summary(result: dict[str, Any]) -> dict[str, Any]:
    """投影稳定的公共下载结果；绝不复制 dispatcher 的原始错误和后端详情。"""
    duplicate = bool(result.get("duplicate"))
    succeeded = _public_dispatch_targets(result.get("succeeded"))
    failed = _public_dispatch_targets(result.get("failed"))
    review_required = bool(
        result.get("review_required")
        or result.get("outcome_unknown")
        or str(result.get("status") or "") == "manual_review"
    )

    if duplicate:
        status = "duplicate"
    elif review_required:
        status = "manual_review"
    elif succeeded and failed:
        status = "partial"
    elif succeeded:
        status = "submitted"
    else:
        status = "failed"
    error = public_download_error(
        succeeded=succeeded,
        failed=failed,
        duplicate=duplicate,
        existing_status=result.get("existing_status"),
        can_resubmit=bool(result.get("can_resubmit")),
        review_required=review_required,
        error=result.get("error"),
    )

    summary = {
        "ok": status in {"submitted", "partial"},
        "status": status,
        "succeeded": succeeded,
        "failed": failed,
        "duplicate": duplicate,
        "error": error,
    }
    if duplicate:
        summary.update({
            "existing_status": str(result.get("existing_status") or ""),
            "can_resubmit": bool(result.get("can_resubmit")),
            "resubmit_target": str(result.get("resubmit_target") or ""),
        })
    return summary


def _export_qb_torrent_for_resubmit(row, torrent_id: str) -> bytes | None:
    """尽力从 qB 5.x 恢复种子；失败时由调用者继续走本地或 Magnet 回退。"""
    qb_url = get("QB_URL", "").strip()
    if not qb_url or not _QB_TORRENT_ID_RE.fullmatch(str(torrent_id or "")):
        return None
    request_id = 0
    try:
        request_id = int(row["id"] or 0)
    except (KeyError, TypeError, ValueError):
        request_id = 0
    client = None
    try:
        client = QBittorrentClient(
            url=qb_url,
            username=get("QB_USERNAME"),
            password=get("QB_PASSWORD"),
            api_key=get("QB_API_KEY"),
        )
        payload = client.export_torrent(str(torrent_id).lower())
        _title, exported_id = parse_torrent_metadata(payload)
        if exported_id.lower() != str(torrent_id).lower():
            logger.warning("qB 导出种子身份不匹配 request=%s", request_id)
            return None
        return payload
    except QBTorrentExportError as exc:
        logger.warning(
            "qB 导出种子不可用 request=%s code=%s",
            request_id,
            exc.code,
        )
    except (TypeError, ValueError):
        logger.warning("qB 导出种子结构无效 request=%s", request_id)
    except Exception as exc:
        logger.warning(
            "qB 导出种子异常 request=%s type=%s",
            request_id,
            type(exc).__name__,
        )
    finally:
        close_qbittorrent_client(client)
    return None


def _recover_guangya_magnet_torrent(row) -> bytes | None:
    """只读复用本请求已绑定 qB 任务的元数据，不能借机创建或重投 qB。"""
    if str(row["kind"] or "") != "magnet":
        return None
    if str(row["qb_status"] or "") not in {"submitted", "downloading", "completed"}:
        return None
    source_value = str(row["source_value"] or "")
    torrent_id = str(row["qb_task_id"] or "").strip().lower()
    if not torrent_id or torrent_id != magnet_infohash(source_value):
        return None
    payload = _export_qb_torrent_for_resubmit(row, torrent_id)
    if payload is None:
        return None

    # 导出器核对的是 qB 的 40 位 TorrentID；纯 v2 的身份还必须核对完整
    # BTMH，不能把同前 20 bytes 的另一个 SHA-256 当作原资源。
    original = DownloadInput(kind="magnet", title="", source_value=source_value)
    recovered = DownloadInput(kind="torrent", title="", torrent_data=payload)
    if request_key(original) not in request_keys(recovered):
        logger.warning("光鸭重试种子与原磁力身份不匹配 request=%s", int(row["id"]))
        return None
    logger.info("光鸭重试使用已校验的 qB 种子元数据 request=%s", int(row["id"]))
    return payload


def _request_content_type(row) -> str:
    # 兼容旧请求和调用方的旧字段快照；有缓存时仍由实际bytes校验BT身份。
    if row["kind"] == "http" and row["torrent_data"]:
        return "application/x-bittorrent"
    try:
        return str(row["content_type"] or "")
    except (KeyError, IndexError):
        return ""


def download_resubmit_capabilities(
    row,
    *,
    allow_completed: bool = False,
) -> dict[str, dict[str, Any]]:
    """返回旧下载请求可重新提交的目标，不暴露原始链接或种子内容。

    仍在处理或提交结果未知的后端不得创建第二份任务；另一个已经失败或进入
    ``manual_review`` 的后端仍可单独恢复。
    """
    kind = str(row["kind"] or "").strip().lower()
    source_value = str(row["source_value"] or "").strip()
    torrent_data = row["torrent_data"]
    qb_task_id = str(row["qb_task_id"] or "").strip().lower()
    qb_status = str(row["qb_status"] or "").strip().lower()
    gy_status = str(row["gy_status"] or "").strip().lower()
    retryable = kind in {"magnet", "torrent", "ed2k", "http"}

    qb_configured = bool(get("QB_URL", "").strip())
    qb_export_available = bool(
        qb_configured and _QB_TORRENT_ID_RE.fullmatch(qb_task_id)
    )
    torrent_magnet_available = bool(
        kind == "torrent" and source_value and magnet_infohash(source_value)
    )
    qb_enabled = retryable and qb_configured
    torrent_error = ""
    if kind == "torrent":
        local_torrent_available = bool(torrent_data)
        if local_torrent_available:
            try:
                parse_torrent_metadata(torrent_data)
            except (TypeError, ValueError):
                local_torrent_available = False
                torrent_error = "原始种子数据已损坏"
        qb_enabled = qb_enabled and bool(
            local_torrent_available or qb_export_available or torrent_magnet_available
        )
    else:
        qb_enabled = qb_enabled and bool(source_value)
    if not retryable:
        qb_reason = "此请求无法重新提交到 qBittorrent"
    elif not qb_configured:
        qb_reason = "尚未配置 qBittorrent"
    elif kind == "torrent" and not qb_enabled:
        if torrent_error:
            qb_reason = "原始种子已损坏，且没有可导出的 qB 任务或 Magnet 备选"
        else:
            qb_reason = "原始种子已按保留策略清理，且没有可导出的 qB 任务或 Magnet 备选"
    elif torrent_error and not (qb_export_available or torrent_magnet_available):
        qb_reason = torrent_error
    elif not source_value:
        qb_reason = "原始下载地址已不可用"
    else:
        qb_reason = ""
    if qb_status in _ACTIVE_BACKEND_STATUSES:
        qb_enabled = False
        qb_reason = "qBittorrent 任务仍在处理或提交结果待确认，请勿重复提交"
    elif qb_status == "resubmitted":
        qb_enabled = False
        qb_reason = "qBittorrent 已重新提交"
    elif qb_status == "completed" and not allow_completed:
        qb_enabled = False
        qb_reason = "qBittorrent 已完成"

    gy_enabled = False
    gy_reason = "此请求无法重新提交到光鸭"
    if retryable and source_value:
        decision = analyze_offline_url(
            source_value, title=str(row["title"] or ""), content_type=_request_content_type(row),
        )
        gy_enabled = bool(decision.allowed)
        gy_reason = "" if gy_enabled else str(decision.reason or "当前规则不允许提交")
    elif retryable:
        gy_reason = "原始下载地址已不可用"
    if gy_status in _ACTIVE_BACKEND_STATUSES:
        gy_enabled = False
        gy_reason = "光鸭任务仍在处理或提交结果待确认，请勿重复提交"
    elif gy_status == "resubmitted":
        gy_enabled = False
        gy_reason = "光鸭任务已重新提交"
    elif gy_status == "completed" and not allow_completed:
        gy_enabled = False
        gy_reason = "光鸭任务已完成"

    both_enabled = qb_enabled and gy_enabled
    if both_enabled:
        both_reason = ""
    elif qb_status in _ACTIVE_BACKEND_STATUSES or gy_status in _ACTIVE_BACKEND_STATUSES:
        both_reason = "已有下载目标仍在处理或提交结果待确认，不能同时重复提交"
    elif qb_status == "resubmitted" or gy_status == "resubmitted":
        both_reason = "至少一个下载目标已重新提交"
    elif not allow_completed and (
        qb_status == "completed" or gy_status == "completed"
    ):
        both_reason = "至少一个下载目标已完成"
    else:
        both_reason = "qBittorrent 与光鸭必须同时可用"
    return {
        "qb": {"enabled": qb_enabled, "reason": qb_reason},
        "guangya": {"enabled": gy_enabled, "reason": gy_reason},
        "both": {"enabled": both_enabled, "reason": both_reason},
    }


def resubmit_download_request(
    source_request_id: int,
    targets: str,
    *,
    allow_completed: bool = False,
    origin: str = "web",
) -> dict[str, Any]:
    """安全重试指定目标；活动请求仅补投失败目标，其余创建历史 successor。"""
    if targets not in SUPPORTED_TARGETS:
        return {"ok": False, "error": "下载目标无效"}
    source_row = db.get_download_request(int(source_request_id))
    if not source_row:
        return {"ok": False, "error": "原下载请求不存在"}

    capabilities = download_resubmit_capabilities(
        source_row,
        allow_completed=allow_completed,
    )
    target_capability = capabilities.get(targets) or {}
    if not target_capability.get("enabled"):
        return {
            "ok": False,
            "error": str(target_capability.get("reason") or "当前目标不可重新提交"),
        }

    # 部分成功请求仍需跟踪另一个后端，不能创建同源 successor，也不能
    # 因两个方向的分支不同而绕过取消检查。统一复用已有目标级 CAS；
    # 另一端即使结果未知也只保留观察，绝不随本次重试再次提交。
    selected_prefix = "gy" if targets == "guangya" else "qb"
    peer_prefix = "qb" if targets == "guangya" else "gy"
    if (
        targets in {"qb", "guangya"}
        and str(source_row["status"] or "") in {"submitted", "downloading"}
        and str(source_row[f"{selected_prefix}_status"] or "") == "failed"
        and str(source_row[f"{peer_prefix}_status"] or "") in {
            "submitted", "downloading", "completed", "outcome_unknown",
        }
    ):
        retry_kwargs: dict[str, str] = {}
        if targets == "qb" and str(source_row["kind"] or "") == "http":
            previous_hash = str(source_row["qb_task_id"] or "").strip().lower()
            hint = (
                previous_hash if _QB_TORRENT_ID_RE.fullmatch(previous_hash)
                else http_torrent_infohash_hint(str(source_row["source_value"] or ""))
            )
            if hint:
                retry_kwargs["qb_task_id_hint"] = hint
        result = dispatch_missing_targets(int(source_request_id), targets, **retry_kwargs)
        return {
            **result,
            "source_request_id": int(source_request_id),
            "request_id": int(source_request_id),
            "created": False,
            "targets": targets,
            "source_attention_preserved": not bool(result.get("ok")),
        }

    source_kind = str(source_row["kind"] or "")
    source_value = str(source_row["source_value"] or "").strip()
    source_torrent_data = source_row["torrent_data"]
    source_qb_task_id = str(source_row["qb_task_id"] or "").strip().lower()
    qb_task_id_hint = ""
    if source_kind == "http":
        qb_task_id_hint = (
            source_qb_task_id
            if _QB_TORRENT_ID_RE.fullmatch(source_qb_task_id)
            else http_torrent_infohash_hint(source_value)
        )
    item_kind = source_kind
    item_torrent_data = source_torrent_data
    if source_kind == "magnet" and targets in {"guangya", "both"}:
        recovered = _recover_guangya_magnet_torrent(source_row)
        if recovered is not None:
            item_kind = "torrent"
            item_torrent_data = recovered
    if source_kind == "torrent":
        local_torrent_valid = False
        try:
            if source_torrent_data is not None:
                parse_torrent_metadata(source_torrent_data)
                local_torrent_valid = True
        except (TypeError, ValueError):
            local_torrent_valid = False

        # 光鸭优先使用 MediaFlux 缓存，避免无意义依赖 qB；qB/both 在已有远端
        # 任务时优先从 qB 5.x 导出，以验证重新提交不依赖本地长期保存 BLOB。
        exported_torrent = None
        qb_task_id = source_qb_task_id
        should_export_first = targets in {"qb", "both"}
        if _QB_TORRENT_ID_RE.fullmatch(qb_task_id) and (
            should_export_first or not local_torrent_valid
        ):
            exported_torrent = _export_qb_torrent_for_resubmit(
                source_row,
                qb_task_id,
            )

        if exported_torrent is not None:
            item_torrent_data = exported_torrent
        elif local_torrent_valid:
            item_torrent_data = source_torrent_data
        elif source_value and magnet_infohash(source_value):
            # qB 导出和本地缓存都不可用时，保留 Magnet 最后回退；对于光鸭，
            # 后续若仍无法解析文件列表，会进入明确的失败/人工处理状态。
            item_kind = "magnet"
            item_torrent_data = None
        else:
            return {
                "ok": False,
                "error": "原始种子已不可用，且无法从 qBittorrent 恢复",
            }
    item = DownloadInput(
        kind=item_kind,
        title=str(source_row["title"] or ""),
        source_value=str(source_row["source_value"] or ""),
        torrent_data=item_torrent_data,
        identity_hint=f"btih:{qb_task_id_hint}" if qb_task_id_hint else "",
        content_type=_request_content_type(source_row),
    )
    if source_kind == "http" and (
        item.torrent_data or item.content_type.split(";", 1)[0].strip().lower() == "application/x-bittorrent"
    ):
        from app.repositories.download_requests import check_download_request_torrent_identity

        try:
            # 已验证HTTP种子即使重试qB，也必须先恢复原内容；普通qB直链不受影响。
            item = prepare_download_input(item, "guangya")
            check_download_request_torrent_identity(
                int(source_request_id), request_keys(item),
                source_alias_key=request_source_alias_key(item),
            )
        except ValueError:
            return {
                "ok": False, "source_attention_preserved": True,
                "error": "原种子读取失败或内容身份已变化，未重新提交，请核对下载来源",
            }
    source_status = str(source_row["status"] or "").strip().lower()
    from app.repositories.download_requests import DownloadRequestRetryFenceError

    try:
        created = create_request(
            item,
            str(source_row["chat_id"] or ""),
            f"resubmit:{int(source_request_id)}",
            origin=str(origin or "web"),
            user_id=str(source_row["user_id"] or ""),
            # manual_review 不属于普通终态，必须显式指定被接管请求；completed、
            # failed、cancelled 走仓储层既有的终态归档与新建逻辑。
            supersede_request_id=(
                int(source_request_id) if source_status == "manual_review" else None
            ),
            # successor 认领和媒体准入/候选接管复用同一事务。
            initial_targets=targets,
            retry_source_request_id=int(source_request_id),
            retry_source_status=source_status,
        )
    except DownloadRequestRetryFenceError as exc:
        return {
            "ok": False,
            "retry_blocked": True,
            "source_request_id": int(source_request_id),
            "source_attention_preserved": True,
            "error": str(exc),
        }
    successor_id = int(created.get("id") or 0)
    if not successor_id:
        return {"ok": False, "error": "重新创建下载请求失败"}
    if not created.get("created"):
        return {
            "ok": False,
            "duplicate": True,
            "request_id": successor_id,
            "error": "同一资源已有下载任务正在处理",
        }

    dispatch_kwargs: dict[str, str] = {}
    if qb_task_id_hint and targets in {"qb", "both"}:
        dispatch_kwargs["qb_task_id_hint"] = qb_task_id_hint
    successor = db.get_download_request(successor_id)
    if successor is None:
        return {
            "ok": False,
            "source_request_id": int(source_request_id),
            "request_id": successor_id,
            "source_attention_preserved": True,
            "error": "新下载请求已认领但无法读取，未调用下载后端",
        }
    claimed_targets = ("qb", "guangya") if targets == "both" else (targets,)
    result = _dispatch_claimed_targets(
        successor, claimed_targets, **dispatch_kwargs,
    )
    if result.get("ok"):
        db.mark_download_request_resubmitted(
            int(source_request_id),
            successor_request_id=successor_id,
            targets=targets,
        )
    else:
        # successor 只有在至少一个目标明确接收后，才能解决旧请求的人工核验义务。
        # 普通失败或 outcome_unknown 都必须保留原 attention，避免用户遗漏可能已被
        # 远端接收的旧任务并再次重复提交。
        error = str(result.get("error") or "新请求未成功提交")
        result = {
            **result,
            "error": f"{error}；原请求仍保留在待处理列表，请先核对原下载任务",
        }
    return {
        **result,
        "source_request_id": int(source_request_id),
        "request_id": successor_id,
        "created": True,
        "targets": targets,
        "source_attention_preserved": not bool(result.get("ok")),
    }



def _qb_submit_overrides(
    *,
    save_path: str | None,
    category: str | None,
    runtime_config: dict[str, Any] | None,
    task_id_hint: str,
) -> dict[str, Any]:
    """只传递真实 qB 覆盖值，保持默认提交器及测试替身的旧调用契约。"""
    overrides: dict[str, Any] = {}
    if save_path is not None:
        overrides["save_path"] = save_path
    if category is not None:
        overrides["category"] = category
    if runtime_config is not None:
        overrides["runtime_config"] = runtime_config
    if task_id_hint:
        overrides["task_id_hint"] = task_id_hint
    return overrides


def dispatch_missing_targets(
    request_id: int,
    targets: str,
    *,
    gy_target_dir: str = "",
    gy_target_name: str = "",
    qb_save_path: str | None = None,
    qb_category: str | None = None,
    qb_runtime_config: dict[str, Any] | None = None,
    qb_task_id_hint: str = "",
    rss_item_id: int | None = None,
    log_path: str | None = None,
    prepared_input: DownloadInput | None = None,
) -> dict[str, Any]:
    """向已有资源请求补充尚未提交的后端，不重置已成功目标。"""
    if targets not in SUPPORTED_TARGETS:
        return {"handled": False, "ok": False, "error": "下载目标无效"}
    row = db.get_download_request(int(request_id))
    if not row:
        return {"handled": False, "ok": False, "error": "下载请求不存在"}
    prepared_row = None
    if prepared_input is not None:
        # 仅携带当前 HTTP 来源的已校验内容；不在认领前写回，不覆盖已知 qB 身份。
        if (targets not in {"guangya", "both"} or row["kind"] != "http"
                or prepared_input.kind != "http" or not prepared_input.torrent_data
                or prepared_input.source_value != row["source_value"]):
            return {"handled": True, "ok": False, "error": "准备好的种子与下载来源不一致"}
        try:
            _name, torrent_id, _magnet_xt, v2_hash = _torrent_metadata(prepared_input.torrent_data)
            qb_identity = str(row["qb_task_id"] or "").strip().lower()
            if (_QB_TORRENT_ID_RE.fullmatch(qb_identity)
                    and qb_identity not in {torrent_id.lower(), v2_hash.lower()}):
                raise ValueError("BT identity changed")
        except ValueError:
            return {"handled": True, "ok": False, "error": "种子内容与已受理下载不一致，未补充提交"}
        prepared_row = {**dict(row), "torrent_data": prepared_input.torrent_data,
                        "content_type": "application/x-bittorrent"}
    claimed = db.claim_download_request_targets(int(request_id), targets)
    if not claimed:
        return {"handled": False, "ok": False, "duplicate": True, "error": "该目标已提交或正在处理"}
    if prepared_row is not None and "guangya" in claimed:
        # _submit_guangya 仍须在云端副作用前通过仓储的认领状态/内容身份原子绑定。
        row = prepared_row

    result = _dispatch_claimed_targets(
        row, tuple(claimed),
        gy_target_dir=gy_target_dir, gy_target_name=gy_target_name,
        qb_save_path=qb_save_path, qb_category=qb_category,
        qb_runtime_config=qb_runtime_config, qb_task_id_hint=qb_task_id_hint,
        rss_item_id=rss_item_id, log_path=log_path,
    )
    return {"handled": True, "duplicate": False, **result}


def dispatch_request(
    request_id: int,
    targets: str,
    *,
    gy_target_dir: str = "",
    gy_target_name: str = "",
    qb_save_path: str | None = None,
    qb_category: str | None = None,
    qb_runtime_config: dict[str, Any] | None = None,
    qb_task_id_hint: str = "",
    rss_item_id: int | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    if targets not in SUPPORTED_TARGETS:
        return {"ok": False, "error": "下载目标无效"}
    row = db.get_download_request(request_id)
    if not row:
        return {"ok": False, "error": "下载请求不存在"}
    if not db.claim_download_request(request_id, targets):
        return {"ok": False, "duplicate": True, "error": "该请求已提交或正在处理"}

    claimed = ("qb", "guangya") if targets == "both" else (targets,)
    return _dispatch_claimed_targets(
        row, claimed,
        gy_target_dir=gy_target_dir, gy_target_name=gy_target_name,
        qb_save_path=qb_save_path, qb_category=qb_category,
        qb_runtime_config=qb_runtime_config, qb_task_id_hint=qb_task_id_hint,
        rss_item_id=rss_item_id, log_path=log_path,
    )


def _dispatch_claimed_targets(
    row,
    claimed_targets: tuple[str, ...],
    *,
    gy_target_dir: str = "",
    gy_target_name: str = "",
    qb_save_path: str | None = None,
    qb_category: str | None = None,
    qb_runtime_config: dict[str, Any] | None = None,
    qb_task_id_hint: str = "",
    rss_item_id: int | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    """两个认领入口共用唯一提交/结果转换/日志执行链，根状态只由仓储原子归并。"""
    request_id = int(row["id"])
    title = str(row["title"] or "未命名任务")
    source_value = str(row["source_value"] or "")
    results: dict[str, dict[str, Any]] = {}
    if "qb" in claimed_targets:
        results["qb"] = _safe_submit(
            "qBittorrent",
            _submit_qb,
            row,
            **_qb_submit_overrides(
                save_path=qb_save_path,
                category=qb_category,
                runtime_config=qb_runtime_config,
                task_id_hint=qb_task_id_hint,
            ),
        )
    if "guangya" in claimed_targets:
        results["guangya"] = _safe_submit(
            "光鸭云盘", _submit_guangya, row,
            target_dir_id=gy_target_dir,
            target_dir_name=gy_target_name,
        )

    succeeded = [name for name, result in results.items() if result.get("ok")]
    failed = [name for name, result in results.items() if not result.get("ok")]
    has_unknown = _submission_has_unknown(results)
    error = "; ".join(
        f"{name}: {results[name].get('error', '提交失败')}" for name in failed
    )
    updates: dict[str, Any] = {"error": error}
    if "qb" in results:
        updates["qb_status"] = _backend_submission_status("qb", results["qb"])
        updates["qb_task_id"] = results["qb"].get("task_id", "")
    if "guangya" in results:
        gy_result = results["guangya"]
        updates["gy_status"] = _backend_submission_status("guangya", gy_result)
        task_ids = [str(item) for item in (gy_result.get("task_ids") or []) if str(item)]
        updates["gy_task_ids"] = json.dumps(task_ids, ensure_ascii=False)
        updates["gy_task_id"] = task_ids[0] if task_ids else str(gy_result.get("task_id") or "")
        updates["gy_batch_count"] = int(gy_result.get("batch_count") or len(task_ids))
        decision = gy_result.get("decision") or {}
        staging = gy_result.get("staging") or {}
        updates["gy_target_dir"] = decision.get("target_dir_id", "")
        updates["gy_target_name"] = decision.get("target_dir_name", "")
        updates["gy_isolated"] = 1 if staging.get("isolated") else 0
        updates["gy_staging_parent_dir"] = str(staging.get("parent_id") or "")
        updates["gy_staging_name"] = str(staging.get("name") or "")
        updates["gy_staging_cleanup_status"] = str(
            staging.get("cleanup_status")
            or ("pending" if staging.get("isolated") and (
                gy_result.get("ok") or gy_result.get("partial_success")
            ) else "")
        )
        updates["gy_staging_cleanup_error"] = str(
            staging.get("cleanup_error") or ""
        )[:500]
        updates["gy_expected_file_count"] = max(0, int(gy_result.get("selected_count") or 0))
        updates["gy_settle_observed_file_count"] = 0
        updates["gy_settle_attempts"] = 0
        updates["gy_settle_snapshot"] = ""
        updates["gy_settle_stable_count"] = 0
        updates["gy_selection_mode"] = str(gy_result.get("selection_mode") or "")
        updates["gy_unverified_manifest"] = 1 if gy_result.get("unverified_manifest") else 0
    status, late_notice = _finalize_submission_state(
        request_id, list(claimed_targets), updates
    )

    for source, result in results.items():
        db.add_download_log(
            source=source,
            title=title,
            path=source_value if log_path is None else str(log_path),
            rss_item_id=rss_item_id,
            request_id=request_id,
            backend_task_id=str(result.get("task_id") or ""),
            status=_backend_submission_status(source, result),
            error=_submission_log_error(result, late_notice),
        )
    if late_notice:
        return {
            "ok": False, "request_id": request_id, "status": status,
            "succeeded": succeeded, "failed": failed, "results": results,
            "error": late_notice, "outcome_unknown": True,
            "review_required": True, "stale_result": True,
        }
    return {
        "ok": bool(succeeded), "request_id": request_id, "status": status,
        "succeeded": succeeded, "failed": failed, "results": results,
        "error": error, "outcome_unknown": has_unknown,
        "review_required": has_unknown,
    }


def _torrent_metadata(data: bytes) -> tuple[str, str, str, str]:
    decoder = _BencodeDecoder(data)
    value = decoder.decode()
    info_span = decoder.info_span
    if not isinstance(value, dict) or info_span is None:
        raise BencodeError("种子文件结构无效")
    info = value.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("种子文件缺少 info 字典")
    raw_name = info.get(b"name.utf-8") or info.get(b"name") or b""
    if isinstance(raw_name, bytes):
        name = raw_name.decode("utf-8", errors="replace")
    else:
        name = str(raw_name or "")
    start, finish = info_span
    raw_info = data[start:finish]
    is_v2 = info.get(b"meta version") == 2
    has_v1_pieces = isinstance(info.get(b"pieces"), bytes)
    if is_v2 and not has_v1_pieces:
        v2_hash = hashlib.sha256(raw_info).hexdigest()
        return name.strip(), v2_hash[:40], f"urn:btmh:1220{v2_hash}", v2_hash
    v1_hash = hashlib.sha1(raw_info).hexdigest()
    v2_hash = hashlib.sha256(raw_info).hexdigest() if is_v2 else ""
    return name.strip(), v1_hash, f"urn:btih:{v1_hash}", v2_hash


def parse_torrent_metadata(data: bytes) -> tuple[str, str]:
    """返回标题与 qB Web API 使用的 40 字符 TorrentID。"""
    name, torrent_id, _magnet_xt, _v2_hash = _torrent_metadata(data)
    return name, torrent_id


_TORRENT_MAX_BYTES = 10 * 1024 * 1024
_BENCODE_MAX_DEPTH = 64
_BENCODE_MAX_VALUES = 250_000
_TORRENT_MANIFEST_MAX_FILES = 100_000


class _BencodeDecoder:
    """下载身份与文件清单唯一的有界解码器，保留顶层 info 的原始字节范围。"""

    def __init__(self, data: bytes):
        if not isinstance(data, bytes) or not data:
            raise BencodeError("种子文件为空")
        if len(data) > _TORRENT_MAX_BYTES:
            raise BencodeError("种子文件超过 10MB 限制")
        self.data = data
        self.values = 0
        self.info_span: tuple[int, int] | None = None

    def decode(self):
        try:
            value, end = self._decode(0, 0)
        except BencodeError:
            raise
        except ValueError as exc:
            raise BencodeError("Bencode 数字格式无效或过大") from exc
        if end != len(self.data):
            raise BencodeError("种子文件包含尾随数据")
        return value

    def _touch(self) -> None:
        self.values += 1
        if self.values > _BENCODE_MAX_VALUES:
            raise BencodeError("种子文件结构过大")

    def _decode(self, index: int, depth: int):
        if depth > _BENCODE_MAX_DEPTH:
            raise BencodeError("种子文件嵌套过深")
        if index >= len(self.data):
            raise BencodeError("Bencode 数据不完整")
        self._touch()
        token = self.data[index:index + 1]
        if token == b"i":
            end = self.data.find(b"e", index + 1)
            if end < 0:
                raise BencodeError("整数未结束")
            raw = self.data[index + 1:end]
            if (
                not raw
                or raw == b"-0"
                or (raw.startswith(b"0") and len(raw) > 1)
                or (raw.startswith(b"-") and raw[1:2] == b"0" and len(raw) > 2)
                or not re.fullmatch(rb"-?[0-9]+", raw)
            ):
                raise BencodeError("整数格式无效")
            return int(raw), end + 1
        if token == b"l":
            result = []
            cursor = index + 1
            while True:
                if cursor >= len(self.data):
                    raise BencodeError("列表未结束")
                if self.data[cursor:cursor + 1] == b"e":
                    return result, cursor + 1
                value, cursor = self._decode(cursor, depth + 1)
                result.append(value)
        if token == b"d":
            result = {}
            cursor = index + 1
            while True:
                if cursor >= len(self.data):
                    raise BencodeError("字典未结束")
                if self.data[cursor:cursor + 1] == b"e":
                    return result, cursor + 1
                key, cursor = self._decode(cursor, depth + 1)
                if not isinstance(key, bytes):
                    raise BencodeError("字典键必须是字节串")
                if key in result:
                    raise BencodeError("种子文件包含重复字典键")
                value_start = cursor
                result[key], cursor = self._decode(cursor, depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (value_start, cursor)
        if b"0" <= token <= b"9":
            colon = self.data.find(b":", index)
            if colon < 0:
                raise BencodeError("字节串长度无效")
            raw_length = self.data[index:colon]
            if (
                not raw_length
                or (raw_length.startswith(b"0") and len(raw_length) > 1)
                or not raw_length.isdigit()
            ):
                raise BencodeError("字节串长度无效")
            length = int(raw_length)
            start, end = colon + 1, colon + 1 + length
            if end > len(self.data):
                raise BencodeError("字节串越界")
            return self.data[start:end], end
        raise BencodeError("未知 Bencode 标记")


def _manifest_text(value: object, *, field: str) -> str:
    if not isinstance(value, bytes):
        raise BencodeError(f"{field} 必须是 UTF-8 字节串")
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BencodeError(f"{field} 不是有效 UTF-8") from exc
    text = unicodedata.normalize("NFKC", text)
    if not text:
        raise BencodeError(f"{field} 不能为空")
    if text in {".", ".."} or any(char in text for char in ("/", "\\", "\x00")):
        raise BencodeError(f"{field} 包含不安全路径片段")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise BencodeError(f"{field} 包含控制字符")
    return text


def _manifest_length(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BencodeError("文件长度无效")
    return value


def _manifest_path(parts: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(parts, list) or not parts:
        raise BencodeError(f"{field} 必须包含至少一个路径片段")
    return tuple(
        _manifest_text(part, field=f"{field}[{index}]")
        for index, part in enumerate(parts)
    )


def _v1_manifest_files(info: dict, name: str) -> list[TorrentManifestFile]:
    raw_files = info.get(b"files")
    if raw_files is None:
        return [TorrentManifestFile(path=(name,), length=_manifest_length(info.get(b"length")))]
    if not isinstance(raw_files, list) or not raw_files:
        raise BencodeError("多文件种子的 files 清单无效")
    files: list[TorrentManifestFile] = []
    for index, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise BencodeError(f"files[{index}] 必须是字典")
        raw_path = entry.get(b"path.utf-8") or entry.get(b"path")
        files.append(TorrentManifestFile(
            path=_manifest_path(raw_path, field=f"files[{index}].path"),
            length=_manifest_length(entry.get(b"length")),
        ))
    return files


def _v2_manifest_files(info: dict, name: str) -> list[TorrentManifestFile]:
    tree = info.get(b"file tree")
    if not isinstance(tree, dict) or not tree:
        raise BencodeError("v2 种子缺少 file tree")
    files: list[TorrentManifestFile] = []

    def walk(node: dict, prefix: tuple[str, ...], depth: int) -> None:
        if depth > _BENCODE_MAX_DEPTH:
            raise BencodeError("file tree 嵌套过深")
        leaf = node.get(b"")
        if leaf is not None:
            if not isinstance(leaf, dict):
                raise BencodeError("file tree 叶子结构无效")
            path = prefix or (name,)
            files.append(TorrentManifestFile(
                path=path, length=_manifest_length(leaf.get(b"length")),
            ))
            if len(files) > _TORRENT_MANIFEST_MAX_FILES:
                raise BencodeError("种子文件数量过多")
        for raw_component, child in node.items():
            if raw_component == b"":
                continue
            component = _manifest_text(raw_component, field="file tree 路径")
            if not isinstance(child, dict):
                raise BencodeError("file tree 子节点必须是字典")
            walk(child, prefix + (component,), depth + 1)

    walk(tree, (), 0)
    if not files:
        raise BencodeError("v2 种子没有文件")
    return files


def parse_torrent_manifest(data: bytes) -> TorrentManifest:
    """安全解析 v1/v2/hybrid Torrent 的文件清单。

    该接口只返回标题、协议版本、相对文件路径和长度；不会返回 tracker、
    magnet、passkey 等敏感下载信息，可安全用于整理识别基准。
    """
    value = _BencodeDecoder(data).decode()
    if not isinstance(value, dict):
        raise BencodeError("种子文件结构无效")
    info = value.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("种子文件缺少 info 字典")
    raw_name = info.get(b"name.utf-8") or info.get(b"name")
    name = _manifest_text(raw_name, field="种子名称")
    is_v2 = info.get(b"meta version") == 2
    has_v1 = isinstance(info.get(b"pieces"), bytes) or b"files" in info or b"length" in info
    if is_v2:
        version = "hybrid" if has_v1 else "v2"
        files = _v2_manifest_files(info, name)
    else:
        version = "v1"
        files = _v1_manifest_files(info, name)
    if len(files) > _TORRENT_MANIFEST_MAX_FILES:
        raise BencodeError("种子文件数量过多")
    seen: set[tuple[str, ...]] = set()
    for item in files:
        key = tuple(component.casefold() for component in item.path)
        if key in seen:
            raise BencodeError("种子文件包含重复路径")
        seen.add(key)
    return TorrentManifest(name=name, version=version, files=tuple(files))


def _safe_submit(name: str, submitter, row, **kwargs) -> dict[str, Any]:
    try:
        result = submitter(row, **kwargs)
        if not isinstance(result, dict):
            return {"ok": False, "task_id": "", "error": f"{name} 返回结果无效"}
        return result
    except Exception as exc:
        logger.exception("%s 下载提交异常 request=%s type=%s", name, row["id"], type(exc).__name__)
        return {"ok": False, "task_id": "", "error": str(exc) or f"{name} 提交异常"}


def _submit_qb(
    row,
    *,
    save_path: str | None = None,
    category: str | None = None,
    runtime_config: dict[str, Any] | None = None,
    task_id_hint: str = "",
) -> dict[str, Any]:
    runtime = runtime_config if isinstance(runtime_config, dict) else None
    runtime_keys = {
        "QB_URL": "url",
        "QB_USERNAME": "username",
        "QB_PASSWORD": "password",
        "QB_API_KEY": "api_key",
    }

    def configured(name: str, fallback: str = "") -> str:
        if runtime is None:
            return str(get(name, fallback) or "")
        return str(runtime.get(runtime_keys[name], fallback) or "")

    qb_url = configured("QB_URL")
    if not qb_url.strip():
        return {"ok": False, "error": "未配置 qBittorrent"}
    try:
        timeout = max(
            1,
            int(runtime.get("timeout") or 10) if runtime is not None else 10,
        )
    except (TypeError, ValueError):
        timeout = 10
    client = QBittorrentClient(
        url=qb_url,
        username=configured("QB_USERNAME"),
        password=configured("QB_PASSWORD"),
        api_key=configured("QB_API_KEY"),
        timeout=timeout,
    )
    default_category = (
        runtime.get("category")
        if runtime is not None
        else get("TG_QB_CATEGORY", get("RSS_QB_CATEGORY", ""))
    )
    default_save_path = (
        runtime.get("default_save_path")
        if runtime is not None
        else get("TG_QB_SAVE_PATH", get("RSS_QB_SAVE_PATH", ""))
    )
    resolved_category = str(category if category is not None else default_category or "")
    resolved_save_path = str(
        save_path if save_path is not None else default_save_path or ""
    )
    torrents = row["torrent_data"] if row["kind"] in {"torrent", "http"} else None
    urls = "" if torrents else str(row["source_value"] or "")
    try:
        result = client.add_torrent_detailed(
            urls=urls,
            save_path=resolved_save_path,
            category=resolved_category,
            torrents=torrents,
        )
        failure_code = str(result.failure_code or "")
        error = ""
        if not result.ok:
            error = (
                "qB 已接收请求的结果无法确认，请等待系统核验，勿直接重复提交"
                if failure_code == "qb_outcome_unknown"
                else "qB 提交失败"
            )
        normalized_hint = str(task_id_hint or "").strip().lower()
        if row["kind"] != "http" or not _QB_TORRENT_ID_RE.fullmatch(
            normalized_hint
        ):
            normalized_hint = ""
        return {
            "ok": bool(result.ok),
            "task_id": (
                result.task_ids[0]
                if result.task_ids
                else torrent_identity(row) or normalized_hint
            ),
            "task_ids": list(result.task_ids),
            "failure_code": failure_code,
            "retryable": bool(result.retryable),
            "error": error,
        }
    finally:
        close_qbittorrent_client(client)


def _submit_guangya(row, *, target_dir_id: str = "", target_dir_name: str = "") -> dict[str, Any]:
    from app.modules.organize_sources import list_nsfw_download_sources

    saved = dict(row)
    requested = str(saved.get("gy_requested_target_dir") or saved.get("gy_staging_parent_dir") or "")
    target_dir_id = str(target_dir_id or requested)
    nsfw_sources = {source["id"]: source["name"] for source in list_nsfw_download_sources()}
    nsfw = bool(saved.get("gy_requested_nsfw")) or target_dir_id in nsfw_sources
    if nsfw and target_dir_id not in nsfw_sources:
        return {"ok": False, "error": "原 NSFW 下载来源已停用或移除，请重新选择目录，未提交下载"}
    if target_dir_id:
        # 在解析种子/创建隔离目录之前持久化选择，早期失败也不能丢失重试路由。
        db.update_download_request(int(row["id"]), gy_requested_target_dir=target_dir_id,
                                   gy_requested_nsfw=int(nsfw))
        target_dir_name = target_dir_name or nsfw_sources.get(target_dir_id, "")
    torrent_data = (
        row["torrent_data"] if row["kind"] in {"torrent", "http"}
        else _recover_guangya_magnet_torrent(row)
    )
    if row["kind"] == "http":
        # Telegram历史待提交请求可能先于选后端建立；在任何云盘副作用前补齐身份。
        item = prepare_download_input(DownloadInput(
            kind="http", title=str(row["title"] or ""),
            source_value=str(row["source_value"] or ""), torrent_data=torrent_data,
            content_type=_request_content_type(row),
        ), "guangya")
        torrent_data = item.torrent_data
        if torrent_data is not None:
            from app.repositories.download_requests import bind_verified_torrent_identity

            owner = bind_verified_torrent_identity(
                int(row["id"]), item.source_value, torrent_data, request_keys(item),
                source_alias_key=request_source_alias_key(item),
            )
            if owner != int(row["id"]):
                return {
                    "ok": False, "duplicate": True, "existing_request_id": owner,
                    "error": f"相同BT内容已由请求 #{owner} 处理，未重复提交",
                }

    def persist_staging(snapshot: dict) -> None:
        parent_name = str(snapshot.get("parent_name") or target_dir_name or "指定目标目录")
        staging_name = str(snapshot.get("name") or "")
        if not db.bind_download_request_guangya_staging(
            int(row["id"]),
            staging_id=str(snapshot.get("id") or ""),
            parent_id=str(snapshot.get("parent_id") or "0"),
            staging_name=staging_name,
            target_name=f"{parent_name} / {staging_name}" if parent_name else staging_name,
        ):
            raise RuntimeError("下载请求已失效，拒绝绑定隔离目录")

    result = submit_offline(
        str(row["source_value"] or ""),
        title=str(row["title"] or ""),
        target_dir_id=target_dir_id,
        target_dir_name=target_dir_name,
        isolate_task=True,
        task_key=str(row["id"]),
        torrent_data=torrent_data,
        on_staging_created=persist_staging,
    )
    task_ids = [str(item) for item in (result.get("task_ids") or []) if str(item)]
    return {**result, "task_id": task_ids[0] if task_ids else ""}


def torrent_identity(row) -> str:
    try:
        if row["kind"] in {"torrent", "http"} and row["torrent_data"]:
            return parse_torrent_metadata(row["torrent_data"])[1]
        if row["kind"] == "magnet":
            return magnet_infohash(str(row["source_value"] or "")) or ""
    except Exception:
        return ""
    return ""


def _title_from_url(url: str) -> str:
    if url.lower().startswith("magnet:?"):
        dn = (parse_qs(urlparse(url).query).get("dn") or [""])[0]
        return unquote(dn) or "磁力任务"
    path = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    return path or ("ED2K 任务" if url.lower().startswith("ed2k://") else "链接任务")
