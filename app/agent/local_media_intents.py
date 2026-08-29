"""本地媒体 Agent 的无状态自然语言意图规则。

该模块只负责字符串归一化与意图提取，不访问数据库、网络、调度器或工具注册表。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_LOCAL_MEDIA_SOURCE_SCOPE_PATTERN = r"(?:本地媒体|本地整理)(?:的)?来源"
_LOCAL_MEDIA_SOURCE_DETAIL_PATTERN = re.compile(
    rf"^(?:请(?:帮我)?\s*)?(?:查看|检查|查询|显示|看看)\s*"
    rf"{_LOCAL_MEDIA_SOURCE_SCOPE_PATTERN}\s*"
    r"(?:(?:编号|序号)\s*)?[#:]?\s*(\d{1,5})\s*"
    r"(?:的\s*)?(?:详情|摘要|状态|配置|情况)?(?:一下)?[.!。！？]?$",
    re.IGNORECASE,
)
_LOCAL_MEDIA_SOURCE_LIST_PATTERN = re.compile(
    rf"^(?:请(?:帮我)?\s*)?(?:(?:列出|查看|检查|查询|显示|看看)\s*)?"
    rf"(?:(?:全部|所有)\s*)?{_LOCAL_MEDIA_SOURCE_SCOPE_PATTERN}\s*"
    r"(?:列表|摘要|状态|配置|情况|概览|汇总)?(?:一下)?[.!。！？]?$",
    re.IGNORECASE,
)
_LOCAL_MEDIA_SOURCE_CONTROL_PATTERN = re.compile(
    rf"^(?:请(?:帮我)?\s*)?"
    r"(暂停|停用|禁用|关闭|关掉|恢复|启用|开启|打开)\s*"
    rf"{_LOCAL_MEDIA_SOURCE_SCOPE_PATTERN}\s*"
    r"(?:(?:编号|序号)\s*)?[#:]?\s*(\d{1,5})\s*(?:的\s*)?"
    r"(q\s*b(?:ittorrent)?\s*(?:下载完成)?(?:自动)?接管|"
    r"下载完成(?:自动)?接管)"
    r"(?:一下)?[.!。！？]?$",
    re.IGNORECASE,
)
_LOCAL_MEDIA_SOURCE_WRITE_TOKENS = (
    "暂停", "停用", "禁用", "关闭", "关掉", "恢复", "启用", "开启", "打开",
)

_LOCAL_MEDIA_DIAGNOSIS_SCOPES = ("本地媒体", "本地整理", "本地入库")
_LOCAL_MEDIA_DIAGNOSIS_INTENTS = (
    "诊断", "检查", "查看", "状态", "健康", "异常", "失败", "待确认", "调度",
)
_LOCAL_MEDIA_DIAGNOSIS_REJECT_PHRASES = (
    "配置本地媒体", "设置本地媒体", "新增本地媒体", "删除本地媒体", "修改本地媒体",
    "更新本地媒体", "立即扫描", "开始扫描", "执行扫描", "重试本地",
    "停止本地", "启用本地", "关闭本地", "取消本地", "运行本地", "创建本地",
    "清空本地", "暂停本地", "恢复本地", "启动本地", "开启本地", "停用本地",
    "禁用本地", "执行本地",
)
_LOCAL_MEDIA_REVIEW_QUEUE_TERMS = (
    "待确认", "人工确认", "人工复核", "待复核", "审核队列", "待审核",
)
_LOCAL_MEDIA_HISTORY_TERMS = (
    "处理历史", "整理历史", "终态记录", "历史结果", "已完成和失败", "完成与失败",
)
_SUMMARY_INTENTS = (
    "汇总", "摘要", "统计", "多少", "积压", "时长", "多久", "分布", "查看", "检查",
)


def local_media_source_summary_request(message: str) -> dict[str, int] | None:
    """解析一个公开序号对应的本地媒体来源安全摘要请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _LOCAL_MEDIA_SOURCE_WRITE_TOKENS):
        return None
    matched = _LOCAL_MEDIA_SOURCE_DETAIL_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    source_number = int(matched.group(1))
    return {"source_number": source_number} if source_number > 0 else None


def is_local_media_source_summaries_message(message: str) -> bool:
    """判断是否为本地媒体来源列表请求，避免被宽泛诊断路由抢占。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _LOCAL_MEDIA_SOURCE_WRITE_TOKENS):
        return False
    return bool(_LOCAL_MEDIA_SOURCE_LIST_PATTERN.fullmatch(normalized))


def local_media_source_trigger_control_request(
    message: str,
) -> tuple[str, dict[str, Any]] | None:
    """只接受单一公开序号、单一触发器的精确启停命令。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    matched = _LOCAL_MEDIA_SOURCE_CONTROL_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    verb, raw_number, raw_trigger = matched.groups()
    source_number = int(raw_number)
    if source_number <= 0:
        return None
    trigger_text = re.sub(r"\s+", "", raw_trigger)
    if not (trigger_text.startswith("qb") or "下载完成" in trigger_text):
        return None
    trigger = "qb_completed"
    return (
        "local_media.set_source_trigger_enabled",
        {
            "source_number": source_number,
            "trigger": trigger,
            "enabled": verb in {"恢复", "启用", "开启", "打开"},
        },
    )


def is_local_media_source_trigger_control_message(message: str) -> bool:
    """宽判定本地媒体来源控制意图，用于缺编号或缺触发器时要求澄清。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return bool(
        re.search(_LOCAL_MEDIA_SOURCE_SCOPE_PATTERN, normalized, re.IGNORECASE)
        and any(token in normalized for token in _LOCAL_MEDIA_SOURCE_WRITE_TOKENS)
    )


def is_local_media_review_queue_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(phrase in normalized for phrase in _LOCAL_MEDIA_DIAGNOSIS_REJECT_PHRASES):
        return False
    return (
        any(scope in normalized for scope in _LOCAL_MEDIA_DIAGNOSIS_SCOPES)
        and any(term in normalized for term in _LOCAL_MEDIA_REVIEW_QUEUE_TERMS)
        and any(intent in normalized for intent in _SUMMARY_INTENTS)
    )


def is_local_media_history_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(phrase in normalized for phrase in _LOCAL_MEDIA_DIAGNOSIS_REJECT_PHRASES):
        return False
    return (
        any(scope in normalized for scope in _LOCAL_MEDIA_DIAGNOSIS_SCOPES)
        and any(term in normalized for term in _LOCAL_MEDIA_HISTORY_TERMS)
        and any(intent in normalized for intent in _SUMMARY_INTENTS)
    )


def is_local_media_diagnosis_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(phrase in normalized for phrase in _LOCAL_MEDIA_DIAGNOSIS_REJECT_PHRASES):
        return False
    if (
        is_local_media_review_queue_summary_message(normalized)
        or is_local_media_history_summary_message(normalized)
    ):
        return False
    return any(scope in normalized for scope in _LOCAL_MEDIA_DIAGNOSIS_SCOPES) and any(
        intent in normalized for intent in _LOCAL_MEDIA_DIAGNOSIS_INTENTS
    )


def local_media_task_request(
    message: str,
) -> tuple[str, dict[str, Any]] | None:
    """解析本地媒体任务列表、检查、重试、精准刷新和可见性核验。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not any(scope in normalized for scope in _LOCAL_MEDIA_DIAGNOSIS_SCOPES):
        return None

    inspection = re.search(
        r"(?:检查|预览)(?:编号|序号)?\s*[#:]?\s*(\d{1,5})", normalized
    )
    if inspection and "预览" in normalized and "检查" in normalized:
        number = int(inspection.group(1))
        return (
            "local_media.preview_task",
            {"inspection_number": number},
        ) if number > 0 else None

    task_match = re.search(
        r"(?:任务|记录)(?:编号|序号)?\s*[#:]?\s*(\d{1,5})", normalized
    )
    if task_match:
        number = int(task_match.group(1))
        if number <= 0:
            return None
        arguments = {"task_number": number}
        if any(token in normalized for token in ("重试", "重新处理", "再处理", "重新排队")):
            return "local_media.retry_task", arguments
        if (
            any(token in normalized for token in (
                "刷新媒体库", "刷新入库", "刷新绑定库", "重新刷新",
            ))
            or ("刷新" in normalized and "媒体库" in normalized)
        ):
            return "local_media.refresh_task_library", arguments
        if any(token in normalized for token in (
            "入库可见", "库中可见", "是否入库", "索引了吗", "已索引", "可播放吗",
        )):
            return "local_media.verify_task_library_visibility", arguments
        if any(token in normalized for token in ("检查", "查看", "详情", "复核")):
            return "local_media.inspect_task", arguments

    if "任务" not in normalized and "记录" not in normalized:
        return None
    if not any(token in normalized for token in ("列出", "列表", "查看", "看看", "有哪些")):
        return None
    scope = "all"
    if any(token in normalized for token in ("失败", "待确认", "需人工", "需要关注")):
        scope = "attention"
    elif any(token in normalized for token in ("处理中", "进行中", "活动")):
        scope = "active"
    elif any(token in normalized for token in ("历史", "已完成")):
        scope = "history"
    return "local_media.task_summaries", {"scope": scope, "limit": 12}
