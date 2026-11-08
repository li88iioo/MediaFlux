"""缺集搜索、资源确认与下载后自动复核的确定性领域生命周期。

该模块只衔接已经存在的领域仓储和下载复核服务，不参与自然语言理解，
也不执行任何未经 EffectPlan 确认的写操作。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.agent.domains.downloads.recent_submissions import (
    RecentDownloadSubmissionStore,
)
from app.repositories.agent_download_verification import (
    MissingMediaWorkflowRepository,
    SQLiteMissingMediaWorkflowRepository,
    verification_from_followup_context,
    workflow_followup_context,
    workflow_ref_from_context,
)
from app.agent.models import ToolResult
from app import database as db

logger = logging.getLogger(__name__)

MISSING_MEDIA_CANDIDATES_KEY = "missing_media_candidates"
MISSING_MEDIA_FOLLOWUPS_KEY = "missing_media_followups"


class MissingMediaWorkflowRuntime:
    """把缺集领域后置动作挂到 Kernel 的统一工具/Effect 生命周期。"""

    def __init__(
        self,
        *,
        repository: MissingMediaWorkflowRepository | None = None,
        recent_download_store: RecentDownloadSubmissionStore | None = None,
    ) -> None:
        self.repository = repository or SQLiteMissingMediaWorkflowRepository()
        self.recent_download_store = recent_download_store or RecentDownloadSubmissionStore()

    def stage_candidates(
        self,
        *,
        owner: str,
        candidates: Any,
    ) -> tuple[dict[str, Any] | None, ...]:
        """把已预检候选转为安全 follow-up context，并推进到待确认状态。"""
        if not isinstance(candidates, list):
            return ()
        contexts: list[dict[str, Any] | None] = []
        selected_refs: list[dict[str, Any]] = []
        try:
            for raw in candidates:
                if not isinstance(raw, dict):
                    contexts.append(None)
                    continue
                verification = raw.get("verification")
                if not isinstance(verification, dict):
                    contexts.append(None)
                    continue
                candidate_title = str(raw.get("candidate_title") or "").strip()
                target = str(raw.get("target") or "").strip().lower()
                workflow_ref = self.repository.select_candidate(
                    owner=owner,
                    verification=verification,
                    candidate_title=candidate_title,
                    target=target,
                )
                context = workflow_followup_context(verification, workflow_ref)
                if context is None:
                    contexts.append(None)
                    continue
                context["result_id"] = raw["result_id"]
                contexts.append(context)
                workflow = workflow_ref_from_context(context)
                if workflow is not None:
                    selected_refs.append(workflow)
        except Exception as exc:  # noqa: BLE001 - 状态镜像失败不能阻断下载预检
            self._release_refs(owner=owner, refs=selected_refs)
            logger.warning(
                "Agent 补库候选状态推进失败 type=%s", type(exc).__name__
            )
            return tuple(None for _item in candidates)
        return tuple(contexts)

    def release_followups(self, *, owner: str, followups: Any) -> None:
        """取消、失败或中断时释放仍处于 confirmation_required 的项目。"""
        refs = [
            workflow
            for context in self._iter_followups(followups)
            if (workflow := workflow_ref_from_context(context)) is not None
        ]
        self._release_refs(owner=owner, refs=refs)

    def complete_submission(
        self,
        *,
        owner: str,
        tool_name: str,
        result: Any,
        followups: Any,
    ) -> None:
        """消费原生逐项回执与已持久化复核目标，关联原 workflow；不再事后排队。"""
        if tool_name not in {"resources.download", "downloads.submit", "cloud.transfer_share"} or not isinstance(result, ToolResult):
            return
        contexts = list(self._iter_followups(followups))
        data = result.data if isinstance(result.data, dict) else {}
        if tool_name != "resources.download":
            self._capture(owner=owner, result=result, verification_context=None)
            return
        receipts = {item["result_id"]: item for item in data.get("items", [])
                    if isinstance(item, dict) and item.get("result_id")}
        for identity, raw in receipts.items():
            matching = [item for item in contexts if item.get("result_id") == identity]
            self._capture(owner=owner, result=self._batch_item_result(raw),
                          verification_context=matching[0] if matching else None)
        for context in contexts:
            raw = receipts.get(context.get("result_id"), {})
            attached = False
            try:
                request_id = raw.get("request_id")
                job = db.get_agent_download_verification(request_id) if type(request_id) is int and request_id > 0 else None
                proof = verification_from_followup_context(context)
                workflow_ref = workflow_ref_from_context(context)
                keys = ("tmdb_id", "season", "episode", "library_name")
                if (job and job["owner"] == owner and proof and workflow_ref
                        and any(all(target.get(key, "") == proof.get(key, "") for key in keys)
                                for target in job["targets"])):
                    self.repository.attach_submission(workflow_ref=workflow_ref,
                        request_id=raw["request_id"], verification_enqueued=True)
                    attached = True
            except Exception as exc:  # 已受理下载与复核意图不受 workflow 镜像失败影响
                logger.warning("Agent 下载复核关联失败 type=%s", type(exc).__name__)
            if not attached:
                self.release_followups(owner=owner, followups=(context,))

    def _capture(
        self,
        *,
        owner: str,
        result: ToolResult,
        verification_context: dict[str, Any] | None,
    ) -> None:
        try:
            self.recent_download_store.capture(
                owner=owner,
                result=result,
                verification_context=verification_context,
            )
        except Exception as exc:  # noqa: BLE001 - 最近状态不是下载执行权威
            logger.warning(
                "Agent 最近下载状态记录失败 type=%s", type(exc).__name__
            )

    @staticmethod
    def _batch_item_result(raw: dict[str, Any]) -> ToolResult:
        dispatch_status = str(raw.get("status") or "").strip().lower()
        accepted = dispatch_status in {"submitted", "partial"}
        status = (
            "accepted"
            if accepted
            else "conflict"
            if dispatch_status == "duplicate"
            else "unavailable"
        )
        return ToolResult(
            accepted,
            status,
            "下载任务已提交" if accepted else "下载任务未提交",
            data=dict(raw),
        )

    @staticmethod
    def _followup_slots(value: Any) -> list[dict[str, Any] | None]:
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [item if isinstance(item, dict) else None for item in value]

    @classmethod
    def _iter_followups(cls, value: Any) -> Iterable[dict[str, Any]]:
        for item in cls._followup_slots(value):
            if item is not None:
                yield item

    def _release_refs(
        self, *, owner: str, refs: Iterable[dict[str, Any]]
    ) -> None:
        for workflow_ref in refs:
            try:
                self.repository.release_confirmation(
                    owner=owner,
                    workflow_ref=workflow_ref,
                )
            except Exception as exc:  # noqa: BLE001 - 清理失败只记录，不遮蔽主流程
                logger.warning(
                    "Agent 补库确认状态释放失败 type=%s", type(exc).__name__
                )
