"""Agent 专用的统一资源接入入口。

只读检查把原始链接、分享访问令牌、云端 file_id 等敏感信息保存在
owner 绑定的短期内存快照中；模型和公开确认参数只接触来源类型、候选序号
与下载目标。非 Agent Telegram 继续使用原有独立 handler/store。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app import database as db
from app.agent.download_actions import download_request_public_summary
from app.agent.indexer_actions import download_target_readiness
from app.agent.indexer_candidate_actions import IndexerCandidateActions
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    normalize_resource_search_id,
)
from app.agent.registry import AgentToolError
from app.logger import redact_sensitive_text
from app.modules.download_dispatcher import (
    DownloadInput,
    create_request,
    dispatch_request,
    normalize_download_url,
    public_dispatch_summary,
    request_key,
    route_download_url,
)
from app.modules.share_transfer import (
    ShareTransferPreviewStore,
    create_share_request,
    inspect_share_for_transfer,
)

_INGEST_SOURCE_TYPES = frozenset({"auto", "direct_url", "guangya_share", "resource_candidates"})
_SUBMIT_SOURCE_TYPES = frozenset({"direct_url", "guangya_share", "resource_candidates"})
_TARGETS = frozenset({"qb", "guangya", "both"})
_MAX_RESOURCE_POSITIONS = 12
_MAX_SHARE_POSITIONS = 200


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_text(value: Any, maximum: int = 240) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    text = " ".join(text.split())
    return redact_sensitive_text(text)[:maximum]


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonnegative_int(value: Any, *, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    number = max(0, number)
    return min(number, maximum) if maximum is not None else number


@dataclass(frozen=True)
class IngestSessionSnapshot:
    session_id: str
    owner: str
    source_type: str
    public: dict[str, Any]
    private: dict[str, Any]
    fingerprint: str
    expires_at: float


class AgentIngestSessionStore:
    """按 owner 保存最新资源接入快照；与传统 Telegram 预览完全隔离。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 15 * 60,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
        share_store: ShareTransferPreviewStore | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._token_factory = token_factory
        self.share_store = share_store or ShareTransferPreviewStore(
            ttl_seconds=self.ttl_seconds,
            max_entries=self.max_entries,
        )
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, IngestSessionSnapshot] = OrderedDict()

    def _prune_locked(self, now: float) -> None:
        for owner in [
            owner for owner, snapshot in self._entries.items()
            if snapshot.expires_at <= now
        ]:
            self._discard_locked(owner)

    def _discard_locked(self, owner: str) -> None:
        snapshot = self._entries.pop(owner, None)
        if snapshot is None or snapshot.source_type != "guangya_share":
            return
        preview_id = str(snapshot.private.get("preview_id") or "")
        if not preview_id:
            return
        try:
            self.share_store.discard(preview_id, snapshot.owner, "agent")
        except ValueError:
            pass

    def capture(
        self,
        *,
        owner: str,
        source_type: str,
        public: dict[str, Any],
        private: dict[str, Any],
        identity: str,
    ) -> IngestSessionSnapshot:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("请先登录后检查资源", code="precondition_failed")
        session_id = str(self._token_factory() or "").strip()
        if not session_id:
            raise AgentToolError("暂时无法保存资源检查结果", code="unavailable")
        now = self._clock()
        snapshot = IngestSessionSnapshot(
            session_id=session_id,
            owner=owner_key,
            source_type=source_type,
            public=deepcopy(public),
            private=deepcopy(private),
            fingerprint=_fingerprint({
                "session_id": session_id,
                "owner": owner_key,
                "source_type": source_type,
                "identity": identity,
            }),
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._discard_locked(owner_key)
            self._entries[owner_key] = snapshot
            self._entries.move_to_end(owner_key)
            while len(self._entries) > self.max_entries:
                oldest = next(iter(self._entries))
                self._discard_locked(oldest)
        return deepcopy(snapshot)

    def get(
        self, *, owner: str, source_type: str = ""
    ) -> IngestSessionSnapshot | None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return None
        with self._lock:
            self._prune_locked(self._clock())
            snapshot = self._entries.get(owner_key)
            if snapshot is None or (
                source_type and snapshot.source_type != source_type
            ):
                return None
            self._entries.move_to_end(owner_key)
            return deepcopy(snapshot)

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        with self._lock:
            existed = owner_key in self._entries
            self._discard_locked(owner_key)
            return existed

    def reset(self) -> None:
        with self._lock:
            for owner in list(self._entries):
                self._discard_locked(owner)


def ingest_inspect_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset({"source_type", "input"}):
        raise AgentToolError("资源检查参数无效")
    source_type = str(arguments.get("source_type") or "auto").strip().lower()
    if source_type not in _INGEST_SOURCE_TYPES:
        raise AgentToolError("source_type 不受支持")
    value = str(arguments.get("input") or "").strip()
    if source_type == "resource_candidates":
        if value:
            raise AgentToolError("最近资源候选检查不接受 input")
    elif not value:
        raise AgentToolError("请提供需要检查的资源链接")
    if len(value) > 8192:
        raise AgentToolError("资源链接过长")
    return {"source_type": source_type, "input": value}


def _positions(
    value: Any, *, maximum: int, required: bool
) -> list[int]:
    if value is None or (isinstance(value, list) and not value):
        if not required:
            return []
        raise AgentToolError("positions 必须是非空候选序号列表")
    if not isinstance(value, list):
        raise AgentToolError("positions 必须是非空候选序号列表")
    result: list[int] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= maximum:
            raise AgentToolError(f"候选序号必须是 1 到 {maximum} 的整数")
        if raw not in result:
            result.append(raw)
    return result


def ingest_submit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset(
        {"source_type", "target", "positions", "search_id"}
    ):
        raise AgentToolError("资源提交参数无效")
    source_type = str(arguments.get("source_type") or "").strip().lower()
    if source_type not in _SUBMIT_SOURCE_TYPES:
        raise AgentToolError("source_type 仅支持 direct_url、guangya_share 或 resource_candidates")
    target = str(arguments.get("target") or "").strip().lower()
    if source_type == "guangya_share":
        target = target or "guangya"
        if target != "guangya":
            raise AgentToolError("光鸭分享只能转存到光鸭")
        positions = _positions(
            arguments.get("positions"), maximum=_MAX_SHARE_POSITIONS, required=False
        )
    else:
        if target not in _TARGETS:
            raise AgentToolError("target 仅支持 qb、guangya 或 both")
        positions = _positions(
            arguments.get("positions"),
            maximum=_MAX_RESOURCE_POSITIONS,
            required=source_type == "resource_candidates",
        )
        if source_type == "direct_url" and positions:
            raise AgentToolError("直链提交不接受 positions")
    normalized = {
        "source_type": source_type,
        "target": target,
        "positions": positions,
    }
    raw_search_id = arguments.get("search_id")
    if source_type == "resource_candidates":
        search_id = normalize_resource_search_id(raw_search_id)
        if raw_search_id not in (None, "") and not search_id:
            raise AgentToolError("search_id 不是有效的资源搜索快照标识")
        if search_id:
            normalized["search_id"] = search_id
    elif raw_search_id not in (None, ""):
        raise AgentToolError("只有资源候选提交接受 search_id")
    return normalized


def ingest_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"request_number"}:
        raise AgentToolError("资源状态查询只接受 request_number")
    value = arguments.get("request_number")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentToolError("request_number 必须是正整数")
    return {"request_number": value}


class IngestActions:
    def __init__(
        self,
        *,
        store: AgentIngestSessionStore,
        recent_resource_store: RecentResourceCandidateStore,
    ) -> None:
        self.store = store
        self.recent_resource_store = recent_resource_store
        self.candidate_actions = IndexerCandidateActions(recent_resource_store)

    @staticmethod
    def _owner(context: ToolContext) -> str:
        owner = str(context.owner or "").strip()
        if not owner:
            raise AgentToolError("请先登录后使用资源接入", code="precondition_failed")
        return owner

    def inspect(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        owner = self._owner(context)
        source_type = arguments["source_type"]
        value = arguments["input"]
        if source_type == "resource_candidates":
            snapshot = self.candidate_actions.current_snapshot(context)
            candidates = snapshot.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise AgentToolError(
                    "最近资源候选不存在或已过期，请重新搜索",
                    code="precondition_failed",
                )
            public_candidates = [
                {
                    "position": int(item.get("position") or index),
                    "title": _safe_text(item.get("title"), 300),
                    "site_name": _safe_text(item.get("site_name"), 80),
                    "size_text": _safe_text(item.get("size_text"), 64),
                    "download_state": _safe_text(item.get("download_state"), 24),
                }
                for index, item in enumerate(candidates[:_MAX_RESOURCE_POSITIONS], start=1)
                if isinstance(item, dict) and str(item.get("result_id") or "").strip()
            ]
            if not public_candidates:
                raise AgentToolError("最近资源候选已失效，请重新搜索", code="precondition_failed")
            return ToolResult(
                True,
                "success",
                f"最近资源候选共 {len(public_candidates)} 项",
                data={
                    "source_type": "resource_candidates",
                    "search_id": snapshot["search_id"],
                    "count": len(public_candidates),
                    "items": public_candidates,
                },
                evidence=[Evidence(
                    "agent_resource_candidates",
                    "读取当前会话最近一次资源搜索的安全候选序号；未返回下载句柄或链接。",
                    _now(),
                )],
                suggestions=["选择候选序号和 qB、光鸭或两边后可进入提交确认。"],
            )

        try:
            routed = route_download_url(value)
        except ValueError as exc:
            raise AgentToolError(str(exc), code="precondition_failed") from exc
        if source_type == "auto":
            source_type = "guangya_share" if routed == "guangya_share" else "direct_url"
        if source_type == "guangya_share":
            if routed != "guangya_share":
                raise AgentToolError("这不是可识别的光鸭官方分享链接", code="precondition_failed")
            try:
                inspected = inspect_share_for_transfer(
                    value,
                    owner,
                    "agent",
                    store=self.store.share_store,
                )
            except Exception as exc:
                raise AgentToolError("光鸭分享暂时无法解析", code="unavailable") from exc
            share_files = [
                item
                for item in (inspected.get("files") or [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
            files = [
                {
                    "position": index,
                    "name": _safe_text(item.get("name"), 300),
                    "is_dir": bool(item.get("is_dir")),
                    "size_bytes": _nonnegative_int(item.get("size")),
                }
                for index, item in enumerate(share_files, start=1)
            ]
            if not files:
                raise AgentToolError(
                    "光鸭分享中没有可转存项目",
                    code="precondition_failed",
                )
            public = {
                "source_type": "guangya_share",
                "count": len(files),
                "items": files,
                "target_name": _safe_text(inspected.get("target_name") or "根目录", 120),
                "expires_in": min(
                    self.store.ttl_seconds,
                    _nonnegative_int(inspected.get("expires_in"), maximum=self.store.ttl_seconds),
                ),
            }
            session = self.store.capture(
                owner=owner,
                source_type="guangya_share",
                public=public,
                private={
                    "preview_id": str(inspected.get("preview_id") or ""),
                    "file_ids": [str(item.get("id") or "").strip() for item in share_files],
                    "target_id": str(inspected.get("target_id") or "0"),
                    "target_name": str(inspected.get("target_name") or "根目录"),
                },
                identity=_fingerprint({
                    "preview": inspected.get("preview_id"),
                    "share": inspected.get("share_id"),
                    "files": [item.get("id") for item in share_files],
                }),
            )
            public["expires_in"] = max(0, int(session.expires_at - time.monotonic()))
            return ToolResult(
                True,
                "success",
                f"光鸭分享已解析：{len(files)} 项可转存",
                data=public,
                evidence=[Evidence(
                    "guangya_share",
                    "服务端已解析分享并保存 owner 绑定的短期私有快照；未返回 access token、file_id 或分享链接。",
                    _now(),
                )],
                suggestions=["可选择序号后转存；不提供序号时默认转存全部项目。"],
            )

        if routed in {"guangya_share", "web"}:
            if routed == "guangya_share":
                raise AgentToolError("该链接应按光鸭分享解析", code="precondition_failed")
            raise AgentToolError(
                "这是普通网页链接，不会创建离线下载任务",
                code="precondition_failed",
            )
        try:
            item = normalize_download_url(value)
        except ValueError as exc:
            raise AgentToolError(str(exc), code="precondition_failed") from exc
        public = {
            "source_type": "direct_url",
            "kind": item.kind,
            "title": _safe_text(item.title or "下载资源", 180),
            "expires_in": self.store.ttl_seconds,
        }
        session = self.store.capture(
            owner=owner,
            source_type="direct_url",
            public=public,
            private={"download_input": item},
            identity=request_key(item),
        )
        public["expires_in"] = max(0, int(session.expires_at - time.monotonic()))
        return ToolResult(
            True,
            "success",
            "下载资源已识别，等待选择目标并确认",
            data=public,
            evidence=[Evidence(
                "download_dispatcher",
                "服务端已校验下载协议并保存 owner 绑定的短期私有快照；未返回链接或哈希。",
                _now(),
            )],
            suggestions=["选择 qB、光鸭或两边后可进入提交确认。"],
        )

    @staticmethod
    def _resource_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        positions = list(arguments["positions"])
        resource_arguments: dict[str, Any] = {
            "target": arguments["target"],
        }
        search_id = normalize_resource_search_id(arguments.get("search_id"))
        if search_id:
            resource_arguments["search_id"] = search_id
        if len(positions) == 1:
            resource_arguments["position"] = positions[0]
        else:
            resource_arguments["positions"] = positions
        return resource_arguments

    def _snapshot(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> IngestSessionSnapshot:
        owner = self._owner(context)
        snapshot = self.store.get(owner=owner, source_type=arguments["source_type"])
        if snapshot is None:
            label = "光鸭分享" if arguments["source_type"] == "guangya_share" else "下载链接"
            raise AgentToolError(
                f"最近{label}检查不存在或已过期，请重新发送链接",
                code="precondition_failed",
            )
        return snapshot

    @staticmethod
    def _submission_context(
        snapshot: IngestSessionSnapshot, arguments: dict[str, Any]
    ) -> str:
        return _fingerprint({
            "snapshot": snapshot.fingerprint,
            "source_type": arguments["source_type"],
            "target": arguments["target"],
            "positions": arguments["positions"],
            "backends": download_target_readiness(arguments["target"]),
        })

    def prepare_submit(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        if arguments["source_type"] == "resource_candidates":
            internal = self._resource_arguments(arguments)
            if len(arguments["positions"]) == 1:
                result, inner_context = self.candidate_actions.prepare_one(internal, context)
            else:
                result, inner_context = self.candidate_actions.prepare_batch(internal, context)
            # 候选解析器把本轮实际使用的 search_id 写回 internal；这里再冻结进
            # ToolRegistry 的 normalized 参数，使确认只能回到同一份快照。
            arguments["search_id"] = internal["search_id"]
            if isinstance(result.data, dict):
                result.data["source_type"] = "resource_candidates"
                result.data["search_id"] = internal["search_id"]
            return result, _fingerprint({
                "source_type": "resource_candidates",
                "arguments": arguments,
                "inner_context": inner_context,
            })

        snapshot = self._snapshot(arguments, context)
        readiness = download_target_readiness(arguments["target"])
        unavailable = [name for name, ready in readiness.items() if not ready]
        if unavailable:
            labels = {"qb": "qBittorrent", "guangya": "光鸭"}
            result = ToolResult(
                False,
                "not_configured",
                f"{'、'.join(labels[name] for name in unavailable)} 尚未配置或登录",
                data={"source_type": arguments["source_type"], "backends": readiness},
                error="所选下载目标尚未就绪。",
            )
            return result, ""

        if arguments["source_type"] == "direct_url":
            result = ToolResult(
                True,
                "confirmation_required",
                "确认后将把该资源提交到所选下载目标",
                data={
                    "source_type": "direct_url",
                    "kind": snapshot.public.get("kind"),
                    "title": snapshot.public.get("title"),
                    "target": arguments["target"],
                    "backends": readiness,
                    "effects": ["创建幂等下载请求", "提交到所选下载后端", "保留后续整理与 STRM 跟踪"],
                },
                evidence=[Evidence(
                    "agent_ingest_snapshot",
                    "已复核 owner 绑定的短期资源快照和下载后端状态；未返回原始链接或哈希。",
                    _now(),
                )],
                suggestions=["请核对资源标题和下载目标后再确认。"],
            )
            return result, self._submission_context(snapshot, arguments)

        file_ids = list(snapshot.private.get("file_ids") or [])
        positions = arguments["positions"] or list(range(1, len(file_ids) + 1))
        if not positions or any(position > len(file_ids) for position in positions):
            raise AgentToolError("分享候选序号不存在，请重新解析", code="precondition_failed")
        items = [
            item for item in snapshot.public.get("items", [])
            if isinstance(item, dict) and int(item.get("position") or 0) in positions
        ]
        frozen_arguments = dict(arguments)
        frozen_arguments["positions"] = positions
        result = ToolResult(
            True,
            "confirmation_required",
            f"确认后将转存 {len(items)} 项到光鸭",
            data={
                "source_type": "guangya_share",
                "count": len(items),
                "items": items,
                "target": "guangya",
                "target_name": snapshot.public.get("target_name"),
                "backends": readiness,
                "effects": ["按所选序号转存分享内容", "创建幂等转存请求", "按现有配置继续后处理"],
            },
            evidence=[Evidence(
                "agent_ingest_snapshot",
                "已锁定 owner 绑定的分享快照、文件序号和目标目录；未返回 access token 或 file_id。",
                _now(),
            )],
            suggestions=["请核对项目和目标目录后再确认。"],
        )
        return result, self._submission_context(snapshot, frozen_arguments)

    def execute_submit(
        self,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        if arguments["source_type"] == "resource_candidates":
            if not normalize_resource_search_id(arguments.get("search_id")):
                raise AgentToolError(
                    "资源确认缺少已冻结的搜索快照，请重新选择",
                    code="confirmation_stale",
                )
            internal = self._resource_arguments(arguments)
            # 只按 ticket 已冻结的 search_id 重新生成底层上下文；不得回落到 latest。
            if len(arguments["positions"]) == 1:
                _preview, inner_context = self.candidate_actions.prepare_one(internal, context)
            else:
                _preview, inner_context = self.candidate_actions.prepare_batch(internal, context)
            current = _fingerprint({
                "source_type": "resource_candidates",
                "arguments": arguments,
                "inner_context": inner_context,
            })
            if not secrets.compare_digest(current, str(expected_context or "")):
                raise AgentToolError("资源候选或下载目标已变化，请重新预检", code="confirmation_stale")
            if len(arguments["positions"]) == 1:
                result = self.candidate_actions.confirm_one(internal, inner_context, context)
            else:
                result = self.candidate_actions.confirm_batch(internal, inner_context, context)
            if isinstance(result.data, dict):
                result.data["source_type"] = "resource_candidates"
            return result

        snapshot = self._snapshot(arguments, context)
        frozen_arguments = dict(arguments)
        if arguments["source_type"] == "guangya_share" and not frozen_arguments["positions"]:
            frozen_arguments["positions"] = list(
                range(1, len(snapshot.private.get("file_ids") or []) + 1)
            )
        current = self._submission_context(snapshot, frozen_arguments)
        if not secrets.compare_digest(current, str(expected_context or "")):
            raise AgentToolError("资源快照或提交目标已变化，请重新预检", code="confirmation_stale")

        if arguments["source_type"] == "direct_url":
            item = snapshot.private.get("download_input")
            if not isinstance(item, DownloadInput):
                raise AgentToolError("下载资源快照已失效，请重新发送链接", code="confirmation_stale")
            created = create_request(item, "", "", origin="agent")
            request_number = int(created["id"])
            dispatched = dispatch_request(request_number, arguments["target"])
            public = public_dispatch_summary(dispatched)
            row = db.get_download_request(request_number)
            data = {
                "source_type": "direct_url",
                "request_number": request_number,
                "target": arguments["target"],
                "created": bool(created.get("created")),
                **public,
            }
            if row is not None:
                data["request"] = download_request_public_summary(row)
            ok = bool(public.get("ok"))
            status = str(public.get("status") or "failed")
            summary = (
                f"下载请求 #{request_number} 已提交"
                if ok else
                f"下载请求 #{request_number} 未完成提交"
            )
            result_status = (
                "accepted" if status in {"submitted", "partial"}
                else "conflict" if status == "duplicate"
                else "unavailable"
            )
            return ToolResult(
                ok,
                result_status,
                summary,
                data=data,
                evidence=[Evidence(
                    "download_dispatcher",
                    "通过统一幂等下载请求与既有后端分发器执行；未返回链接、哈希或后端任务标识。",
                    _now(),
                )],
                suggestions=[f"可查询资源请求 #{request_number} 的状态。"],
                error=str(public.get("error") or ""),
            )

        file_ids = list(snapshot.private.get("file_ids") or [])
        positions = frozen_arguments["positions"]
        if not positions or any(position > len(file_ids) for position in positions):
            raise AgentToolError("分享候选序号已变化，请重新解析", code="confirmation_stale")
        selected_ids = [file_ids[position - 1] for position in positions]
        try:
            result = create_share_request(
                str(snapshot.private.get("preview_id") or ""),
                selected_ids,
                str(snapshot.private.get("target_id") or "0"),
                snapshot.owner,
                user_id="agent",
                target_name=str(snapshot.private.get("target_name") or "根目录"),
                origin="agent",
                tracker_chat_id="",
                store=self.store.share_store,
            )
        except ValueError as exc:
            raise AgentToolError(str(exc), code="confirmation_stale") from exc
        request_number = int(result.get("request_id") or 0)
        success = bool(result.get("success"))
        duplicate = bool(result.get("duplicate"))
        status = str(result.get("status") or ("completed" if success else "failed"))
        data = {
            "source_type": "guangya_share",
            "request_number": request_number,
            "target": "guangya",
            "selected_count": len(positions),
            "status": "submitted" if (success or duplicate) else "failed",
            "transfer_status": status,
            "succeeded": ["guangya"] if success else [],
            "failed": [] if success or duplicate else ["guangya"],
            "created": bool(result.get("created")),
            "duplicate": duplicate,
            "target_name": _safe_text(result.get("target_dir_name") or snapshot.public.get("target_name"), 120),
        }
        row = db.get_download_request(request_number) if request_number else None
        if row is not None:
            data["request"] = download_request_public_summary(row)
        ok = success or (duplicate and status in {"pending", "submitting", "submitted", "downloading", "completed"})
        return ToolResult(
            ok,
            "accepted" if ok else status,
            (
                f"光鸭分享转存请求 #{request_number} 已受理"
                if ok else
                f"光鸭分享转存请求 #{request_number} 未完成"
            ),
            data=data,
            evidence=[Evidence(
                "share_transfer",
                "通过独立的 Agent 分享快照与既有幂等转存服务执行；未返回分享令牌或云端 file_id。",
                _now(),
            )],
            suggestions=([f"可查询资源请求 #{request_number} 的状态。"] if request_number else []),
            error=_safe_text(result.get("error"), 240),
        )

    def status(
        self, arguments: dict[str, Any], _context: ToolContext
    ) -> ToolResult:
        row = db.get_download_request(arguments["request_number"])
        if row is None:
            return ToolResult(
                False,
                "not_found",
                "没有找到这条资源请求",
                error="请核对请求编号。",
            )
        item = download_request_public_summary(row)
        return ToolResult(
            True,
            "success",
            f"资源请求 #{item['request_number']} 当前状态：{item['status']}",
            data={"request": item},
            evidence=[Evidence(
                "download_requests",
                "读取统一下载请求的脱敏阶段状态；未返回链接、路径、哈希或后端任务标识。",
                _now(),
            )],
            suggestions=[],
        )


def confirmation_required(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("资源提交需要用户确认", code="confirmation_required")
