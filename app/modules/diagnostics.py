"""Agent 只读诊断：在白名单运维表上执行受限 SQL，并检索运行日志。

读权限由 SQLite 授权回调在引擎层强制；凭据列与各身份私有的 Agent 会话表不可读。
日志与运维表是全局数据；SDK 调用端负责校验 Web 登录身份。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from contextlib import closing
from datetime import date as calendar_date, datetime
from pathlib import Path
from typing import Any

from app import config, database as db
from app.logger import LOG_DIR
from app.sensitive_data import is_sensitive_key, redact_sensitive_text

READABLE_TABLES = frozenset({
    "task_runs", "download_log", "download_requests", "download_staging_reconcile",
    "media_subscriptions", "media_subscription_runs", "media_subscription_candidates",
    "media_subscription_notification_rules", "media_download_admissions", "media_watchlist",
    "media_external_ids", "media_title_aliases", "rss_items", "rss_entries", "rss_entry_media",
    "rss_media_bindings", "organize_confirmations", "organize_log", "organize_log_items",
    "organize_delete_audit", "organize_operation_steps", "organize_probe_queue",
    "strm_change_queue", "strm_failures", "strm_index", "strm_metadata_queue", "strm_path_cleanup",
    "strm_refresh_outbox", "strm_retired_sources", "local_media_sources", "local_media_tasks",
    "local_media_task_items", "local_media_operation_steps", "local_library_targets",
    "media_proxy_instances", "media_proxy_bindings", "media_proxy_playback_records",
    "media_proxy_playback_sessions", "recognition_knowledge", "recognition_preprocess_rules",
    "recognition_format_rules", "tmdb_lock", "tmdb_regex_rules", "gcid_import_tasks",
    "gcid_import_items", "telegram_notification_outbox", "agent_library_patrol",
})
# 订阅地址、种子与来源原文可能携带 passkey；身份与 Telegram 投递标识不属于诊断信息。
_DENIED_COLUMNS = frozenset({
    "url", "urls", "smb_user", "smb_pass", "payload_auth", "torrent_data", "source_value", "guid",
    "chat_id", "user_id", "message_id", "owner", "fingerprint", "context_fingerprint", "cookie",
})
# 原始 JSON 可能包含凭据或其他身份的 Agent 正文与投递地址，不能通过 SQL 表达式间接读取。
_DENIED_TABLE_COLUMNS = {
    "rss_entries": frozenset({"payload"}),
    "telegram_notification_outbox": frozenset({"event_json"}),
}
_SQL_CHARS, _QUERY_SECONDS = 4000, 3.0
_LOG_SCAN_BYTES = 32 * 1024 * 1024
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOG_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def column_readable(table: str, column: str) -> bool:
    return not (
        column in _DENIED_COLUMNS
        or column in _DENIED_TABLE_COLUMNS.get(table, ())
        or is_sensitive_key(column)
        or "pass" in column
        or column.endswith(("_url", "_hmac", "_digest", "_token"))
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<二进制 {len(value)} 字节>"
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.resolve_db_path()}?mode=ro", uri=True, timeout=2)


def _readable_schema() -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return [
            {
                "table": table,
                "columns": [
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info('{table}')")
                    if column_readable(table, row[1])
                ],
            }
            for table in sorted(READABLE_TABLES & present)
        ]


def query_db(sql: str = "", limit: int = 50) -> dict[str, Any]:
    sql = (sql or "").strip().rstrip(";").strip()
    if len(sql) > _SQL_CHARS:
        raise ValueError(f"sql 最多 {_SQL_CHARS} 个字符")
    if sql and not re.match(r"(?is)^(select|with)\b", sql):
        raise ValueError("只支持 SELECT 或 WITH ... SELECT 查询")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit 必须是 1–200 的整数")
    evidence = [
        {
            "source": "sqlite_readonly",
            "description": "只读连接；授权回调限定白名单表与非凭据列，凭据片段已打码。",
            "collected_at": _now(),
        }
    ]
    if not sql:
        tables = _readable_schema()
        return {
            "ok": True,
            "status": "completed",
            "summary": f"可读取 {len(tables)} 张运维表；凭据列与 Agent 私有会话表不可读",
            "data": {"tables": tables},
            "evidence": evidence,
        }
    denied: list[str] = []

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK
        if (
            action == sqlite3.SQLITE_READ
            and arg1 in READABLE_TABLES
            and (not arg2 or column_readable(arg1, arg2))
        ):
            return sqlite3.SQLITE_OK
        denied.append(
            f"{arg1}.{arg2}"
            if action == sqlite3.SQLITE_READ and arg2
            else str(arg1 or arg2 or action)
        )
        return sqlite3.SQLITE_DENY

    deadline = time.monotonic() + _QUERY_SECONDS
    try:
        with closing(_connect()) as conn:
            conn.set_authorizer(authorizer)
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 10_000
            )
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limit + 1)
            columns = [item[0] for item in cursor.description or ()]
    except sqlite3.DatabaseError as exc:
        if denied:
            raise ValueError(
                "无权读取："
                + "、".join(dict.fromkeys(denied[:5]))
                + "。只能读取诊断白名单表的非凭据列，不能用 SELECT *；不带 sql 调用可查看可读表与列。"
            ) from exc
        if "interrupted" in str(exc):
            raise ValueError(
                f"查询超过 {_QUERY_SECONDS:g} 秒已中止，请增加条件或缩小范围"
            ) from exc
        raise ValueError(f"SQL 无效：{redact_sensitive_text(str(exc))[:200]}") from exc
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "ok": True,
        "status": "completed",
        "summary": f"返回 {len(rows)} 行"
        + ("（已达 limit，可能还有更多）" if truncated else ""),
        "data": {
            "columns": columns,
            "rows": [[_cell(value) for value in row] for row in rows],
            "truncated": truncated,
        },
        "evidence": evidence,
    }


def _log_file(day: str | None) -> Path:
    if day and day != calendar_date.today().isoformat():
        return Path(LOG_DIR) / f"app.log.{day}"
    return Path(LOG_DIR) / "app.log"


def _records(path: Path) -> tuple[list[str], bool]:
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - _LOG_SCAN_BYTES))
        text = handle.read(_LOG_SCAN_BYTES).decode("utf-8", errors="replace")
    complete = size <= _LOG_SCAN_BYTES
    records: list[list[str]] = []
    for line in text.splitlines():
        if _LOG_START_RE.match(line) or (complete and not records):
            records.append([line])
        elif records:
            records[-1].append(line)
    return ["\n".join(record) for record in records], complete


def search_logs(
    query: str = "", level: str = "", date: str = "", limit: int = 50
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit 必须是 1–200 的整数")
    query = (query or "").strip()
    if len(query) > 120:
        raise ValueError("query 最多 120 个字符")
    level = (level or "").strip().upper()
    if level and level not in _LOG_LEVELS:
        raise ValueError("level 只支持 " + "、".join(_LOG_LEVELS))
    raw_date = (date or "").strip()
    if raw_date:
        try:
            raw_date = calendar_date.fromisoformat(raw_date).isoformat()
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
    path = _log_file(raw_date)
    if not path.is_file():
        return {
            "ok": False,
            "status": "not_found",
            "summary": "该日期没有运行日志",
            "error": "日志只保留最近 14 天",
        }
    minimum = _LOG_LEVELS.index(level) if level else 0
    needle = query.casefold()
    matches: deque[str] = deque(maxlen=limit)
    records, complete = _records(path)
    matched = 0
    for record in records:
        parts = record.split(" | ", 2)
        level = parts[1].strip() if len(parts) > 2 else ""
        if level in _LOG_LEVELS and _LOG_LEVELS.index(level) < minimum:
            continue
        if needle and needle not in record.casefold():
            continue
        matched += 1
        matches.append(redact_sensitive_text(record))
    return {
        "ok": True,
        "status": "completed",
        "summary": f"返回 {len(matches)} 条日志（按时间顺序，取最近的 {limit} 条以内）",
        "data": {"file": path.name, "records": list(matches), "matched_in_scan": matched,
                 "scan_complete": complete, "scan_byte_limit": _LOG_SCAN_BYTES,
                 "truncated": not complete or matched > limit},
        "evidence": [
            {
                "source": "app_log",
                "description": "读取 MediaFlux 运行日志文件末尾；凭据片段已打码。",
                "collected_at": _now(),
            }
        ],
    }


# 配置事实只在此定义；总检、单组件与SDK任务回执共用同一业务结果。
_CONFIG_COMPONENTS: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {'jellyfin': ('Jellyfin',
              '连接 Jellyfin 12 媒体库，用于媒体搜索、缺集审计和更新核对。',
              (('JELLYFIN_URL', '服务地址'), ('JELLYFIN_API_KEY', 'API Key'))),
 'emby': ('Emby / Jellyfin 10.x',
          '连接 Emby 或 Jellyfin 10.x 兼容节点，用于媒体库读取和兼容性诊断。',
          (('EMBY_URL', '服务地址'), ('EMBY_TOKEN', '访问 Token'))),
 'tmdb': ('TMDB 元数据', '提供影视映射、分集播出信息、更新核对和 TMDB 探索数据。', (('TMDB_API_KEY', 'API Key'),)),
 'qbittorrent': ('qBittorrent', '接收资源站结果并查看下载队列、传输速度与停滞任务。', (('QB_URL', '服务地址'), ('__qb_auth__', 'API Key 或用户名/密码'))),
 'strm': ('STRM',
          '把光鸭云盘目录同步为本地 STRM 索引，并维护调度与失败账本。',
          (('__strm_source__', '光鸭源目录'), ('GY_STRM_BASE_URL', '播放服务地址'), ('STRM_ROOT', '本地 STRM 输出目录'))),
 'ai_recognition': ('AI 识别回退', '在确定性规则无法识别媒体时，调用受控的 AI 服务提供候选结果。', (('__shared_ai_provider__', 'Media Agent 模型连接配置'),))}
_FEATURE_DETAILS: dict[str, dict[str, Any]] = {'discovery': {'purpose': '统一浏览放映排期、豆瓣、TMDB 与 Bangumi 榜单。',
               'blocked_capabilities': ('媒体探索页面', '豆瓣与 TMDB 榜单', 'Bangumi 放送内容')},
 'douban': {'purpose': '在媒体探索中读取豆瓣电影与电视剧榜单。', 'blocked_capabilities': ('豆瓣电影榜单', '豆瓣电视剧榜单')},
 'resource_results': {'purpose': '在媒体档案中展示多站资源检索结果和安全下载入口。', 'blocked_capabilities': ('媒体档案站点资源结果', '资源结果下载入口')},
 'indexer_search': {'purpose': '跨已启用站点搜索公开资源，并返回可确认提交的临时结果。', 'blocked_capabilities': ('多站资源搜索', '缺集资源定向搜索', '资源提交预检')}}
_CONFIG_SWITCHES = {"jellyfin": "JELLYFIN_ENABLED", "emby": "EMBY_ENABLED", "ai_recognition": "AI_RECOGNITION_ENABLED", "strm": "STRM_SCHEDULE_ENABLED"}
_FIELD_KEYS = {"__qb_auth__": ("QB_API_KEY", "QB_USERNAME", "QB_PASSWORD"), "__strm_source__": ("GY_STRM_SOURCE_DIRS",),
               "__shared_ai_provider__": ("AGENT_LLM_API_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL")}
CONFIG_COMPONENTS = (*_CONFIG_COMPONENTS, *_FEATURE_DETAILS)


def _state(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _effective_value() -> Callable[[str], str]:
    keys = {key for _, _, fields in _CONFIG_COMPONENTS.values() for field, _ in fields for key in _FIELD_KEYS.get(field, (field,))}
    values = config.get_many(tuple(keys | set(_CONFIG_SWITCHES.values())))
    return lambda key: (values.get(key) or "").strip()


def component_configuration(component: str, *, value: Callable[[str], str] | None = None) -> dict[str, Any]:
    from app import config
    from app.modules.strm import parse_strm_sources

    label, purpose, required = _CONFIG_COMPONENTS[component]
    value = value or _effective_value()
    sources, validation = parse_strm_sources(value("GY_STRM_SOURCE_DIRS"), require_nonempty=False) if component == "strm" else ([], "")
    present = {field: (bool(sources) if field == "__strm_source__" else
        bool(value("AGENT_LLM_API_URL") and value("AGENT_LLM_MODEL")) if field == "__shared_ai_provider__" else
        bool(value("QB_API_KEY") or (value("QB_USERNAME") and value("QB_PASSWORD"))) if field == "__qb_auth__" else bool(value(field)))
        for field, _ in required}
    ready = all(present.values())
    enabled = (_state(value(_CONFIG_SWITCHES[component])) if component in _CONFIG_SWITCHES else
               bool(value("QB_URL") or present["__qb_auth__"]) if component == "qbittorrent" else ready)
    if component == "strm":
        enabled = enabled or bool(sources or validation)
    status = ("disabled" if component in {"jellyfin", "emby", "ai_recognition"} and not enabled else
              "ready" if ready else "incomplete" if enabled else "not_configured")
    persisted = config.all_items()
    fields = []
    for field, field_label in required:
        keys = _FIELD_KEYS.get(field, (field,))
        managed = any(config.has_external_override(key) for key in keys)
        fields.append({"label": field_label, "configured": present[field], "managed_by_environment": managed,
                       "source": "environment" if managed else "configuration" if any(key in persisted for key in keys) else "default"})
    managed = any(row["managed_by_environment"] for row in fields) or config.has_external_override(_CONFIG_SWITCHES.get(component, ""))
    steps = ["当前必要配置已具备；如仍不可用，请运行对应的状态或连通性诊断。"] if status == "ready" else (
        ["该组件至少一项必要设置由运行环境管理，请在部署配置中核对后重启服务。"] if managed else
        [f"先在设置页启用{label}，再填写必要配置。"] if status == "disabled" else
        ["前往设置页补全缺少的字段，然后重新运行配置诊断。"])
    if component == "ai_recognition" and status == "incomplete":
        steps = ["前往设置页的 Media Agent 分区补全模型连接，再重新运行配置诊断。"]
    if component in {"jellyfin", "emby"}:
        steps.append(f"配置保存后，可让 Agent 测试{label}连接。")
    return {"component": component, "name": component, "label": label, "purpose": purpose,
            "enabled": enabled, "status": status, "probe_mode": "configuration_only", "network_accessed": False,
            "fields": fields, "required_field_labels": [row["label"] for row in fields],
            "missing_field_labels": [] if status == "disabled" else [row["label"] for row in fields if not row["configured"]],
            "managed_by_environment": managed, "blocked_capabilities": [] if status == "ready" else _CAPABILITIES[component],
            "validation_errors": [validation.replace("GY_STRM_SOURCE_DIRS", "STRM 来源")] if validation else [],
            "next_steps": steps, "agent_action": None}


_CAPABILITIES = {"jellyfin": ["Jellyfin 媒体库搜索", "剧集缺集审计", "剧集更新核对"],
    "emby": ["Emby 媒体库搜索", "兼容节点诊断", "剧集缺集审计"], "tmdb": ["TMDB 影视探索", "分集播出核验", "影视映射与更新核对"],
    "qbittorrent": ["提交资源到 qBittorrent", "下载队列诊断", "下载任务状态读取"],
    "strm": ["STRM 手动同步", "STRM 定时调度", "STRM 失败项重试"], "ai_recognition": ["AI 媒体识别回退"]}


def component(component: str) -> dict[str, Any]:
    """完整配置事实与来源；不会读取或调用外部服务。"""
    if not isinstance(component, str) or component.strip() not in CONFIG_COMPONENTS:
        raise ValueError("component 不在允许的配置组件列表中")
    component = component.strip()
    data: dict[str, Any]
    if component in _FEATURE_DETAILS:
        from app.modules.feature_config import features

        row = next(row for row in features()["features"] if row["feature"] == component)
        status = "ready" if row["availability"] == "available" else row["availability"]
        managed = row["managed_by_environment"]
        guidance = {"feature_disabled": "该功能当前已关闭，可由 Agent 发起受控开启并在确认后保存。",
                    "parent_disabled": "先开启媒体探索，再重新检查该功能。",
                    "search_disabled": "先开启多站资源搜索，再重新检查探索页资源结果。",
                    "no_enabled_sites": "请先在设置页至少启用一个资源站点。"}
        steps = [guidance[reason] for reason in row.get("reason_codes", []) if reason in guidance]
        data = {**row, "component": component, "status": status, **_FEATURE_DETAILS[component],
                "required_field_labels": [], "missing_field_labels": [], "probe_mode": "configuration_only", "network_accessed": False,
                "next_steps": ["该开关由运行环境管理，请在部署配置中调整并重启服务。"] if managed else
                    steps or ["当前功能及其依赖均可用，无需修改配置。"],
                "agent_action": {"supported": True, "tool": "settings.set_feature", "feature": component, "enabled": True,
                                 "requires_confirmation": True, "prompt": f"开启{row['label']}"} if status == "disabled" and not managed else None}
        if status == "ready":
            data["blocked_capabilities"] = []
    else:
        data = component_configuration(component)
    labels = {"ready": "已就绪", "incomplete": "配置不完整", "disabled": "已关闭", "not_configured": "未配置", "blocked": "依赖受阻"}
    return {"ok": True, "status": data["status"], "summary": f"{data['label']}：{labels.get(data['status'], data['status'])}",
            "data": data, "evidence": [{"source": "server_configuration", "description": "完整组件事实与字段来源；不返回配置键、值或凭据，不声明连通性。", "collected_at": _now()}],
            "suggestions": data["next_steps"]}


def configuration() -> dict[str, Any]:
    """完整保留各组件诊断，不再将组件事实裁剪成限制性摘要。"""
    value = _effective_value()
    components = [component_configuration(name, value=value) for name in _CONFIG_COMPONENTS]
    by_name = {row["component"]: row for row in components}
    issues = []
    for row in components:
        name = row["component"]
        if row["status"] == "incomplete":
            issues.append({"code": "strm_schedule_incomplete" if name == "strm" and _state(value("STRM_SCHEDULE_ENABLED")) else f"{name}_incomplete",
                "severity": "error", "component": name, "message": {
                    "qbittorrent": "qBittorrent 配置不完整，需要补齐：" + "、".join(row["missing_field_labels"]) + "。",
                    "strm": "STRM 来源、播放地址或输出目录缺失或无效；来源不能使用云盘根目录。",
                    "ai_recognition": "AI 识别回退已启用，请先在 Media Agent 设置中补全模型连接。",
                }.get(name, f"{row['label']}已启用，但缺少 {len(row['missing_field_labels'])} 项必要配置。"),
                "validation_errors": row["validation_errors"]})
    if not any(by_name[name]["enabled"] for name in ("jellyfin", "emby")):
        issues.append({"code": "media_server_disabled", "severity": "warning", "message": "尚未启用媒体服务器，媒体库搜索与缺集检查不可用。"})
    if by_name["tmdb"]["status"] != "ready":
        issues.append({"code": "tmdb_not_configured", "severity": "warning", "message": "TMDB 未配置，更新检查、映射和部分探索能力受限。"})
    errors = sum(row["severity"] == "error" for row in issues)
    warnings = len(issues) - errors
    return {"ok": errors == 0, "status": "attention" if errors else "degraded" if warnings else "healthy",
            "summary": f"发现 {errors} 个错误、{warnings} 个提醒" if issues else "关键配置检查通过",
            "data": {"components": components, "issues": issues, "counts": {"errors": errors, "warnings": warnings},
                     "probe_mode": "configuration_only", "network_accessed": False},
            "evidence": [{"source": "config", "description": "组件诊断共用原配置解析与业务校验；完整保留字段来源和错误。", "collected_at": _now()}],
            "suggestions": [row["message"] for row in issues]}


def pipeline(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """读取本地自动化与完整RSS公共诊断，分页只作用于RSS关注项。"""
    from app.modules.rss_subscriptions import diagnose
    from app.services import build_automation_summary

    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("limit 必须为1–100，offset必须为非负整数")
    sources: dict[str, Any] = {}
    errors = []
    for name, read in (("automation_database", build_automation_summary), ("rss", lambda: diagnose(limit, offset))):
        try:
            result = read()
            if result.get("error") or result.get("ok") is False:
                errors.append({"source": name, "code": "source_unavailable", "message": str(result.get("error") or "诊断来源不可用")})
            sources[name] = result
        except Exception as exc:
            errors.append({"source": name, "code": "source_unavailable", "exception_type": type(exc).__name__, "message": "诊断来源读取失败"})
            sources[name] = None
    aggregate = sources["automation_database"]
    if aggregate is not None:
        keys = ("downloads_active", "downloads_review", "rss_subscriptions", "media_subscriptions", "subscriptions_total", "rss_pending", "rss_failed",
                "organize_issues", "strm_failures", "strm_last_status", "strm_last_at", "issues", "issue_source", "healthy", "error")
        sources["automation_database"] = {key: aggregate[key] for key in keys if key in aggregate}
        status = str(aggregate.get("strm_last_status") or "").strip().lower()
        if status and status not in {"success", "completed", "failed", "error", "interrupted", "partial_failed", "revert_failed", "cancelled", "running", "started", "queued"}:
            sources["automation_database"]["strm_last_status"] = "unknown"
            errors.append({"source": "automation_database", "code": "invalid_status", "message": "最近STRM运行状态无法识别"})
        stamp = aggregate.get("strm_last_at")
        if stamp:
            try:
                datetime.fromisoformat(str(stamp))
            except ValueError:
                sources["automation_database"]["strm_last_at"] = None
                errors.append({"source": "automation_database", "code": "invalid_timestamp", "message": "最近STRM运行时间无效"})
    last_status = str((sources["automation_database"] or {}).get("strm_last_status") or "").strip().lower()
    attention = bool(aggregate and (aggregate.get("issues") or last_status in {"failed", "error", "interrupted", "partial_failed", "revert_failed", "cancelled"})) or bool(sources["rss"] and sources["rss"].get("data", {}).get("attention", {}).get("total"))
    observed = bool(aggregate and (aggregate.get("subscriptions_total") or aggregate.get("downloads_active") or aggregate.get("strm_last_status")))
    failed_sources = {row["source"] for row in errors if row["code"] == "source_unavailable"}
    status = "unavailable" if len(failed_sources) == len(sources) else "partial" if errors else "attention" if attention else "healthy" if observed else "not_configured"
    return {"ok": status != "unavailable", "status": status, "summary": "本地自动化链路诊断：" + status,
            "data": {"probe_mode": "local", "network_accessed": False, "sources": sources, "errors": errors},
            "evidence": [{"source": name, "description": "读取既有公共诊断；不访问外网、不启动任务。", "collected_at": _now()} for name in sources]}
