"""确定性安全边界与跨领域通用用户文案。

领域工具的真实结果摘要仍由各工具返回；这里只集中会在多个路由重复出现的权限、
确认和限流文案，避免编排流程夹杂大段重复中文。
"""
from __future__ import annotations

LOGIN_REQUIRED_ACTION = "该动作需要在已登录会话中确认"
LOGIN_REQUIRED_CONFIG_ACTION = "该配置动作需要在已登录会话中确认"
LOGIN_REQUIRED_RSS_REFRESH = "刷新 RSS 订阅需要在已登录会话中确认"
CONFIRM_CHANGE_IN_AGENT = "请通过 Agent 页面重新提交，并在预检后确认修改。"
CONFIRM_EXECUTION_IN_AGENT = "请通过 Agent 页面重新提交，并在只读预检后确认执行。"
AGENT_RATE_LIMITED = "Agent 请求过于频繁，请稍后重试"


def login_confirmation_suggestions(*, change: bool = False) -> list[str]:
    return [CONFIRM_CHANGE_IN_AGENT if change else CONFIRM_EXECUTION_IN_AGENT]
