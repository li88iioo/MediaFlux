"""MediaFlux Agent 的集中式提示策略。"""
from .core import DEFAULT_AGENT_SYSTEM_PROMPT, base_system_prompt, current_date_context
from .native import (
    native_capability_selection_system_prompt,
    native_read_system_prompt,
)
from .presentation import (
    conversation_answer_system_prompt,
    conversation_stream_system_prompt,
    conversation_summary_system_prompt,
    draft_rewrite_system_prompt,
    tool_answer_system_prompt,
    tool_stream_system_prompt,
)
from .routing import (
    confirmation_route_instruction,
    orchestration_route_instruction,
    read_plan_system_prompt,
    selection_system_prompt,
)

__all__ = [
    "DEFAULT_AGENT_SYSTEM_PROMPT",
    "base_system_prompt",
    "current_date_context",
    "native_capability_selection_system_prompt",
    "native_read_system_prompt",
    "selection_system_prompt",
    "orchestration_route_instruction",
    "confirmation_route_instruction",
    "read_plan_system_prompt",
    "conversation_summary_system_prompt",
    "conversation_answer_system_prompt",
    "tool_answer_system_prompt",
    "conversation_stream_system_prompt",
    "tool_stream_system_prompt",
    "draft_rewrite_system_prompt",
]
