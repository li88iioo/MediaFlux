"""Agent 写操作的稳定、脱敏确认展示契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.agent.models import RiskLevel, ToolResult
from app.agent.result_projection import public_tool_label, sanitize_public_text

CONTRACT_VERSION = 1

# 文案只描述可观察影响，不包含参数、路径、URL、票据或服务端内部标识。
_CONFIRMATION_COPY: dict[str, dict[str, str]] = {
    "downloads.pause_task": {
        "action": "暂停下载任务",
        "object": "你指定的 qBittorrent 任务",
        "impact": "任务会停止继续下载或做种，已下载文件不会被删除。",
        "reversibility": "可再次确认恢复该任务。",
    },
    "downloads.resume_task": {
        "action": "恢复下载任务",
        "object": "你指定的 qBittorrent 暂停任务",
        "impact": "任务会恢复下载或做种，实际速度由下载器与网络决定。",
        "reversibility": "可再次确认暂停该任务。",
    },
    "downloads.delete_task": {
        "action": "移除下载任务",
        "object": "你指定的 qBittorrent 任务",
        "impact": "只会从下载器移除任务，绝不会删除已经下载的文件。",
        "reversibility": "任务移除后不能由 MediaFlux 自动恢复；本地文件仍会保留。",
    },
    "downloads.retry_submission": {
        "action": "重新提交下载请求",
        "object": "你指定的一条下载待处理记录",
        "impact": "会使用服务端保留的原始请求创建新的下载提交；不会在确认页暴露链接、种子、路径或凭据。",
        "reversibility": "已发出的下载请求无法撤回；提交失败时原记录会继续保留在待处理列表。",
    },
    "rss.set_subscription_enabled": {
        "action": "切换 RSS 订阅状态",
        "object": "你指定的 RSS 订阅",
        "impact": "会启用或停用后续定时刷新；不会立即抓取订阅或创建下载任务。",
        "reversibility": "可再次确认切换回原状态；已经开始的刷新不会被强制中断。",
    },
    "media.set_subscription_enabled": {
        "action": "切换媒体追更状态",
        "object": "你指定的一条媒体追更订阅",
        "impact": "暂停会停止安排新检查并失效尚未提交的候选资源；恢复会让订阅重新进入检查队列。已提交下载任务和媒体文件不受影响。",
        "reversibility": "可再次确认暂停或恢复该订阅。",
    },
    "rss.set_refresh_interval": {
        "action": "调整 RSS 自动刷新周期",
        "object": "你指定的 RSS 订阅",
        "impact": "会影响后续定时刷新计划；0 分钟表示关闭自动刷新，不会立即抓取或下载。",
        "reversibility": "可再次确认修改周期或恢复自动刷新。",
    },
    "rss.delete_subscription": {
        "action": "永久删除 RSS 订阅",
        "object": "你指定的 RSS 订阅及其本地条目记录",
        "impact": "会删除订阅配置与本地条目记录；不会删除下载任务或已下载文件。",
        "reversibility": "删除不可撤销；如只想停止定时刷新，请改为停用订阅。",
    },
    "rss.refresh_subscription": {
        "action": "立即刷新 RSS 订阅",
        "object": "你选择的订阅源",
        "impact": "会抓取一次最新条目并更新订阅记录，不会自动创建下载任务。",
        "reversibility": "刷新请求无法撤回；新增条目仍可在订阅页查看和处理。",
    },
    "rss.refresh_subscriptions": {
        "action": "立即刷新多个 RSS 订阅",
        "object": "当前已启用且配置完整的订阅源",
        "impact": "会依次抓取一次最新条目并更新订阅记录，不会自动创建下载任务。",
        "reversibility": "刷新请求无法撤回；新增条目仍可在订阅页查看和处理。",
    },
    "rss.submit_pending_to_qb": {
        "action": "提交 RSS 待处理条目",
        "object": "本次预检选中的条目",
        "impact": "会在 qBittorrent 中创建下载任务，并改变这些条目的处理状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的提交请求无法撤回。",
    },
    "rss.retry_failed_to_qb": {
        "action": "重试 RSS 失败条目",
        "object": "本次预检判定可安全重试的条目",
        "impact": "会再次向 qBittorrent 提交任务，并更新失败条目的处理状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的重试请求无法撤回。",
    },
    "discovery.add_watchlist": {
        "action": "加入探索收藏",
        "object": "最近探索结果中预检通过的一个影视条目",
        "impact": "只会写入本地探索收藏记录，不会搜索资源、创建订阅或提交下载。",
        "reversibility": "可再次确认移除该收藏；媒体文件和下载任务不受影响。",
    },
    "discovery.remove_watchlist": {
        "action": "移除探索收藏",
        "object": "你按精确收藏编号选择的一条本地收藏记录",
        "impact": "只会删除本地收藏记录，不会删除媒体文件、订阅或下载任务。",
        "reversibility": "移除后不能自动恢复，但可重新搜索并再次加入收藏。",
    },
    "config.set_indexer_sites": {
        "action": "更新资源检索站点范围",
        "object": "MediaFlux 的资源站选择",
        "impact": "后续资源检索只会使用新选择的站点，不会立即发起搜索或下载。",
        "reversibility": "可随时再次修改站点选择恢复。",
    },
    "config.set_feature_state": {
        "action": "切换项目功能状态",
        "object": "本次预检指向的功能",
        "impact": "保存后会改变该功能的可用状态；不会执行下载或整理任务。",
        "reversibility": "可再次切换回原状态；环境变量锁定的配置不会被覆盖。",
    },
    "config.set_safe_policy": {
        "action": "更新安全白名单策略",
        "object": "本次预检指定的一项非敏感策略",
        "impact": "保存后只会改变该策略及相关运行时缓存；登录壁纸策略可能在后台请求 TMDB 刷新图片，不会下载媒体、整理文件或读取凭据。",
        "reversibility": "可再次确认改回原值；由环境变量管理的配置不会被覆盖。",
    },
    "telegram.send_test_notification": {
        "action": "发送 Telegram 测试通知",
        "object": "当前已配置的 Telegram 通知会话",
        "impact": "只会发送一条固定的 MediaFlux 连接测试消息；不会附带业务数据、配置内容、Token 或 Chat ID。",
        "reversibility": "消息发送后不能由 MediaFlux 撤回；如需移除，请在 Telegram 中手动删除。",
    },
    "media_proxy.set_instance_enabled": {
        "action": "切换媒体反代实例状态",
        "object": "你按公开序号指定的媒体反代实例",
        "impact": "启用会按已保存配置启动反代监听；停用可能中断正在通过该实例播放的媒体。不会修改或展示地址、端口、路径与凭据。",
        "reversibility": "可再次确认切换回原状态；已中断的播放会话不会自动恢复。",
    },
    "local_media.set_source_trigger_enabled": {
        "action": "切换本地媒体来源触发方式",
        "object": "你按公开序号指定的本地媒体来源触发器",
        "impact": "只会启用或停用 qB 下载完成自动接管或目录自动扫描；不会修改或展示来源目录、目标目录、整理规则、媒体服务器绑定、下载器配置或凭据。停用时，等待执行且依赖该触发方式的任务可能失败。",
        "reversibility": "可再次确认切换回原状态；已经开始的文件操作不会被强制中断。",
    },
    "recognition.set_rule_enabled": {
        "action": "切换识别规则状态",
        "object": "你按类型和编号指定的一条识别规则",
        "impact": "只会改变该规则是否参与后续识别；不会修改规则内容、匹配表达式、TMDB 映射、别名或优先级。",
        "reversibility": "可再次确认切换回原状态；已经完成的识别结果不会自动重跑。",
    },
    "strm.retry_failures": {
        "action": "重试 STRM 失败项",
        "object": "本次预检匹配的失败记录",
        "impact": "会重新生成或补齐对应 STRM 内容，并更新失败状态。",
        "reversibility": "可停止后续处理；已成功写入的结果不会自动回滚。",
    },
    "strm.run_once": {
        "action": "立即执行一次 STRM 同步",
        "object": "当前已配置的 STRM 来源",
        "impact": "会扫描来源并按现有规则创建或更新 STRM 内容。",
        "reversibility": "可停止后续运行；本次已写入的结果不会自动回滚。",
    },
    "strm.set_schedule_policy": {
        "action": "更新 STRM 定时同步策略",
        "object": "定时同步的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时同步，不会立即启动或中断同步任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    },
    "guangya.organize.set_schedule_policy": {
        "action": "更新光鸭定时整理策略",
        "object": "定时整理的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时整理，不会立即启动或中断整理任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    },
    "guangya.organize.run_once": {
        "action": "立即执行一次光鸭整理",
        "object": "当前已配置的整理来源",
        "impact": "会按现有规则扫描、识别并移动或整理匹配内容。",
        "reversibility": "部分移动操作可能无法自动回滚，建议先核对预检结果。",
    },
    "guangya.organize.stop": {
        "action": "停止光鸭整理任务",
        "object": "当前正在运行的整理任务",
        "impact": "会协作停止后续文件处理，正在提交的单个操作可能先完成。",
        "reversibility": "停止后可重新启动；已经完成的移动不会自动回滚。",
    },
    "guangya.organize.clean_empty": {
        "action": "清理光鸭来源空目录",
        "object": "当前已配置来源中的空文件夹",
        "impact": "只删除确认为空的目录，不删除目录内文件。",
        "reversibility": "空目录删除后不会自动恢复，但不会影响已有文件内容。",
    },
    "indexer.submit_resource": {
        "action": "提交资源下载",
        "object": "你刚才选择的资源候选",
        "impact": "会向所选下载目标创建任务；不会向模型或页面暴露下载链接。",
        "reversibility": "可在目标下载器或云盘任务中暂停或删除；提交请求无法撤回。",
    },
    "library.set_patrol_policy": {
        "action": "更新全库缺集巡检策略",
        "object": "缺集巡检的启用、通知、间隔或单轮上限",
        "impact": "会影响后续自动巡检与通知，不会立即扫描全库。",
        "reversibility": "可再次修改策略恢复；保存后下一轮巡检使用新配置。",
    },
    "library.start_episode_audit": {
        "action": "开始后台全库剧集检查",
        "object": "当前媒体服务器中的剧集目录",
        "impact": "会在后台分批检查整个媒体库并核对 TMDB 已播集数；每批有上限，但任务会持续到全库完成。不会搜索资源、创建下载或修改媒体文件。",
        "reversibility": "任务支持保存进度与安全取消；取消时当前有界批次可能先完成。",
    },
    "agent.cancel_job": {
        "action": "取消后台全库剧集检查",
        "object": "当前会话选中的后台检查任务",
        "impact": "尚未运行的任务会立即取消；运行中的任务会在当前有界批次结束后停止。",
        "reversibility": "不会删除媒体文件或已保存的只读结果；需要时可重新发起检查。",
    },
}


def _safe_time(value: str | None = None) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.isoformat(timespec="seconds")
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_contract_text(value: Any, *, fallback: str) -> str:
    return sanitize_public_text(value, limit=128) or fallback


def build_confirmation_contract(
    *,
    tool_name: str,
    risk: RiskLevel,
    preview: ToolResult,
    preflight_at: str | None = None,
) -> dict[str, Any]:
    """为一次预检生成可持久化、版本化且不含原始参数的确认文案。"""
    copy = _CONFIRMATION_COPY.get(str(tool_name or "").strip(), {})
    label = public_tool_label(tool_name)
    contract = {
        "version": CONTRACT_VERSION,
        "action": _safe_contract_text(copy.get("action"), fallback=label),
        "object": _safe_contract_text(copy.get("object"), fallback="当前预检选中的对象"),
        "impact": _safe_contract_text(
            copy.get("impact"), fallback="确认后会执行服务端预检通过的受控操作。"
        ),
        "reversibility": _safe_contract_text(
            copy.get("reversibility"), fallback="执行后可能需要在对应功能页手动撤销。"
        ),
        "preflight_at": _safe_time(preflight_at),
        "risk": risk.value,
    }
    # 预检摘要只作为补充说明，完全通过公开文本净化；不安全时直接省略。
    preview_summary = sanitize_public_text(preview.summary, limit=160)
    if preview_summary:
        contract["preflight_summary"] = preview_summary
    return contract


def sanitize_confirmation_contract(value: Any) -> dict[str, Any]:
    """重新投影已有契约，供 API、Telegram 和审计记录安全复用。"""
    if not isinstance(value, Mapping):
        return {}
    try:
        version = int(value.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != CONTRACT_VERSION:
        return {}
    risk = str(value.get("risk") or "").strip().lower()
    if risk not in {"low_write", "write", "danger"}:
        risk = "write"
    projected = {
        "version": CONTRACT_VERSION,
        "action": _safe_contract_text(value.get("action"), fallback="执行受控操作"),
        "object": _safe_contract_text(value.get("object"), fallback="当前预检选中的对象"),
        "impact": _safe_contract_text(
            value.get("impact"), fallback="确认后会执行服务端预检通过的受控操作。"
        ),
        "reversibility": _safe_contract_text(
            value.get("reversibility"), fallback="执行后可能需要在对应功能页手动撤销。"
        ),
        "preflight_at": _safe_time(str(value.get("preflight_at") or "")),
        "risk": risk,
    }
    preview_summary = sanitize_public_text(value.get("preflight_summary"), limit=160)
    if preview_summary:
        projected["preflight_summary"] = preview_summary
    return projected
