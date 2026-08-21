"""Agent 公开自然语言的共享流式投影状态机。

本模块只处理已经取得的安全 Agent 响应如何选择 Provider 文本流、如何按完整
可读片段发布，以及如何把最终文本合并回响应。传输层租约、HTTP/TG I/O、历史
落库和确认交互仍由各自适配器负责。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.llm_router import normalize_streamed_answer
from app.agent.result_projection import (
    is_public_text_safe,
    public_stream_readable_prefix_length,
)
from app.clients.openai_compatible import ProviderStreamError

StreamFactory = Callable[..., AsyncIterator[str]]
ResultProjector = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AgentPresentationDelta:
    """一个已经跨过完整句边界并通过公开文本校验的增量。"""

    delta: str
    cumulative: str


class PublicNarrativeValidationError(ProviderStreamError):
    """Provider 文本包含不允许公开的内容。"""


class PublicNarrativeProjector:
    """把 Provider token 流收敛为可安全公开的完整文本片段。"""

    def __init__(self) -> None:
        self._accumulated = ""
        self._published_length = 0
        self._pending_error: PublicNarrativeValidationError | None = None

    @property
    def emitted(self) -> bool:
        return self._published_length > 0

    @property
    def accumulated(self) -> str:
        return self._accumulated

    def feed(self, value: Any) -> AgentPresentationDelta | None:
        delta = str(value or "")
        if not delta:
            return None
        candidate = self._accumulated + delta
        stable_length = public_stream_readable_prefix_length(candidate)
        stable_candidate = candidate[:stable_length]
        if stable_candidate.strip() and not is_public_text_safe(stable_candidate):
            raise PublicNarrativeValidationError(
                "Agent 流式回答未通过公开文本校验"
            )

        self._accumulated = candidate
        if candidate.strip() and not is_public_text_safe(candidate):
            # 先允许调用方发布已经越过完整句边界的安全前缀，再由
            # raise_pending_error() 终止本次流。这样 Web/TG 都不会丢失
            # 已经公开的安全内容，也不会泄漏 URL、路径等非公开尾部。
            self._pending_error = PublicNarrativeValidationError(
                "Agent 流式回答未通过公开文本校验"
            )
        if stable_length <= self._published_length:
            return None

        public_delta = candidate[self._published_length:stable_length]
        self._published_length = stable_length
        if not public_delta.strip():
            return None
        return AgentPresentationDelta(
            delta=public_delta,
            cumulative=stable_candidate,
        )

    def raise_pending_error(self) -> None:
        """在调用方处理本轮安全增量后，抛出延迟的公开校验错误。"""
        error = self._pending_error
        self._pending_error = None
        if error is not None:
            raise error

    def finalize(self, *, require_emitted: bool = False) -> str:
        """返回完整规范化回答；TG 可要求至少已公开过一个完整片段。"""
        self.raise_pending_error()
        if require_emitted and not self.emitted:
            return ""
        answer = normalize_streamed_answer(self._accumulated)
        if self._accumulated.strip() and not answer:
            raise ProviderStreamError("Agent 流式回答未通过最终校验")
        return answer

    def published_answer(self) -> str:
        """返回已经公开的安全前缀，用于流中断后的保守收口。"""
        if not self.emitted:
            return ""
        return normalize_streamed_answer(
            self._accumulated[: self._published_length]
        )


def select_agent_answer_stream(
    message: str,
    response: dict[str, Any],
    *,
    owner: str,
    tool_stream_factory: StreamFactory,
    conversation_stream_factory: StreamFactory,
) -> AsyncIterator[str] | None:
    """按统一规则决定安全响应是否允许进入 Provider narrative 流。"""
    mode = str(response.get("mode") or "")
    if mode in {"confirmation_required", "confirmed_action"}:
        return None
    # Native Agent 或同步 presenter 已经生成过安全 narrative 时，直接复用；
    # 流式适配器不应为同一用户请求再次调用 Provider。
    if isinstance(response.get("presentation"), dict):
        return None
    if isinstance(response.get("tool_call"), dict) and isinstance(
        response.get("result"), dict
    ):
        return tool_stream_factory(message, response, owner=owner)
    if (
        mode == "conversation"
        and str((response.get("result") or {}).get("status") or "") != "partial"
    ):
        return conversation_stream_factory(message, response, owner=owner)
    return None


def apply_streamed_answer(
    response: dict[str, Any],
    answer: str,
    *,
    result_projector: ResultProjector | None = None,
) -> dict[str, Any]:
    """合并自然语言，但保留工具事实、确认票据和结构化结果。"""
    presented = dict(response)
    if isinstance(response.get("tool_call"), dict):
        previous = response.get("presentation")
        presentation = dict(previous) if isinstance(previous, dict) else {}
        presentation.update(
            {
                "version": 1,
                "source": "llm",
                "kind": "narrative",
                "narrative": answer,
            }
        )
        presented["presentation"] = presentation
        return presented

    if str(response.get("mode") or "") == "conversation":
        result = response.get("result")
        if isinstance(result, dict):
            streamed_result = dict(result)
            streamed_result["summary"] = answer
            presented["result"] = streamed_result
            if result_projector is not None:
                presented["display"] = result_projector(streamed_result)
    return presented
