#!/usr/bin/env python3
"""Media Agent 只读生产就绪诊断。默认不访问网络、不写配置或数据库。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.agent.domain_catalog import build_tool_specs
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.ports import catalog_from_tool_specs
from app.clients.openai_compatible import (
    PROTOCOLS,
    normalize_provider_location,
    resolve_protocol,
)
from app.database import resolve_db_path
from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport
from app.modules.web_secret import configured_web_secret
from app.runtime_paths import get_runtime_paths

_REQUIRED_AGENT_TABLES = frozenset(
    {
        "agent_confirmations",
        "agent_kernel_sessions",
        "agent_kernel_refs",
        "agent_kernel_events",
        "agent_jobs",
        "agent_rate_limit_buckets",
        "agent_session_context",
    }
)
_MIN_SECRET_LENGTH = 32
_MIN_SCRAPE_KEY_LENGTH = 24


def _agent_config_check() -> DiagnosticCheck:
    if not config.get_bool("AGENT_ENABLED", False):
        return DiagnosticCheck(
            "agent.config",
            "warning",
            "Media Agent 总开关当前关闭。",
            "如需使用 Agent，请先在设置中启用并完成 Provider 检查。",
        )
    return DiagnosticCheck("agent.config", "ok", "Media Agent 总开关已启用。")


def _provider_check() -> DiagnosticCheck:
    if not config.get_bool("AGENT_LLM_ENABLED", False):
        return DiagnosticCheck(
            "agent.provider",
            "warning",
            "LLM 原生规划当前关闭，Media Agent 自然语言入口不可用。",
            "如需使用 Agent，请启用 LLM 并运行设置页能力测试。",
        )
    base_url = config.get("AGENT_LLM_API_URL", "").strip()
    model = config.get("AGENT_LLM_MODEL", "").strip()
    protocol = (
        config.get("AGENT_LLM_PROTOCOL", "auto").strip().lower().replace("-", "_")
    )
    missing = [
        name
        for name, value in (("API Base URL", base_url), ("模型", model))
        if not value
    ]
    if missing:
        return DiagnosticCheck(
            "agent.provider",
            "error",
            f"LLM 已启用，但缺少：{'、'.join(missing)}。",
            "补齐 Provider 配置后，在设置页运行能力测试。",
        )
    if protocol not in PROTOCOLS:
        return DiagnosticCheck(
            "agent.provider",
            "error",
            "LLM 协议配置无效。",
            "协议请选择 auto、responses、chat_completions 或 anthropic_messages。",
        )
    try:
        normalize_provider_location(base_url, https_only=True, public_only=True)
        resolved = resolve_protocol(protocol, base_url)
    except ValueError:
        return DiagnosticCheck(
            "agent.provider",
            "error",
            "LLM Provider 地址未通过 HTTPS/公网安全校验。",
            "使用无内嵌凭据、无查询参数的公网 HTTPS Base URL。",
        )
    key_state = (
        "API Key 已配置"
        if config.get("AGENT_LLM_API_KEY", "").strip()
        else "API Key 未配置"
    )
    return DiagnosticCheck(
        "agent.provider",
        "ok",
        f"Provider 静态配置有效（协议 {resolved}，{key_state}）；本次未联网。",
        "请在设置页运行能力测试确认结构化输出、工具调用与流式输出。",
    )


def _database_check() -> DiagnosticCheck:
    path = resolve_db_path()
    if not path.is_file():
        return DiagnosticCheck(
            "agent.database",
            "error",
            "运行数据库不存在。",
            "先正常启动一次 MediaFlux 以初始化数据库。",
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        return DiagnosticCheck(
            "agent.database",
            "error",
            "数据库只读完整性检查失败。",
            "停止写入任务后备份数据库，并查看 SQLite/文件权限日志。",
        )
    finally:
        if connection is not None:
            connection.close()
    missing = sorted(_REQUIRED_AGENT_TABLES - tables)
    if not quick_check or str(quick_check[0]).lower() != "ok":
        return DiagnosticCheck(
            "agent.database",
            "error",
            "SQLite quick_check 未通过。",
            "立即备份数据库并执行离线完整性修复。",
        )
    if missing:
        return DiagnosticCheck(
            "agent.database",
            "error",
            f"数据库缺少 {len(missing)} 个 Agent 必需表。",
            "运行当前版本的数据库初始化/迁移后重试。",
        )
    return DiagnosticCheck(
        "agent.database",
        "ok",
        f"SQLite quick_check 通过，{len(_REQUIRED_AGENT_TABLES)} 个 Agent 必需表齐全。",
    )


def _web_secret_check() -> DiagnosticCheck:
    secret = configured_web_secret()
    if secret:
        valid = (
            _MIN_SECRET_LENGTH <= len(secret) <= 256
            and secret.isascii()
            and all(33 <= ord(char) <= 126 for char in secret)
        )
        if valid:
            return DiagnosticCheck(
                "agent.web_secret", "ok", "WEB_SECRET_KEY 已安全配置。"
            )
        return DiagnosticCheck(
            "agent.web_secret",
            "error",
            "WEB_SECRET_KEY 已配置但格式或长度不安全。",
            "改用 32–256 位可打印 ASCII 随机值并重启服务。",
        )

    fallback = get_runtime_paths().config_dir / ".web-secret-key"
    try:
        metadata = fallback.lstat()
    except FileNotFoundError:
        status = (
            "error"
            if config.get("APP_ENV", "development").strip().lower() == "production"
            else "warning"
        )
        return DiagnosticCheck(
            "agent.web_secret",
            status,
            "未配置 WEB_SECRET_KEY，且尚无持久化回退密钥。",
            "生产环境请显式配置至少 32 位随机 WEB_SECRET_KEY。",
        )
    except OSError:
        return DiagnosticCheck(
            "agent.web_secret",
            "error",
            "无法检查持久化 Web Secret 文件。",
            "检查配置目录权限与文件类型。",
        )
    safe_file = stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
    if os.name == "posix":
        safe_file = safe_file and not (metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    if not safe_file:
        return DiagnosticCheck(
            "agent.web_secret",
            "error",
            "持久化 Web Secret 文件类型或权限不安全。",
            "移除不安全文件，并让目标服务账户重新生成或显式配置密钥。",
        )
    return DiagnosticCheck(
        "agent.web_secret", "ok", "持久化 Web Secret 文件存在且权限收紧。"
    )


def _key_capabilities_check() -> DiagnosticCheck:
    scrape_key = config.get("AGENT_METRICS_SCRAPE_KEY", "").strip()
    if scrape_key and len(scrape_key) < _MIN_SCRAPE_KEY_LENGTH:
        return DiagnosticCheck(
            "agent.keys",
            "error",
            "独立 metrics scrape key 已配置但长度不足。",
            f"使用至少 {_MIN_SCRAPE_KEY_LENGTH} 位随机值；不要复用 Web 或 Provider 密钥。",
        )
    states = [
        "metrics scrape key 已配置" if scrape_key else "metrics scrape key 未配置",
        "Provider key 已配置"
        if config.get("AGENT_LLM_API_KEY", "").strip()
        else "Provider key 未配置",
    ]
    status = "ok" if scrape_key else "warning"
    suggestion = (
        ""
        if scrape_key
        else "如需无会话 Prometheus 抓取，请配置独立 AGENT_METRICS_SCRAPE_KEY。"
    )
    return DiagnosticCheck("agent.keys", status, "；".join(states) + "。", suggestion)


def _tool_capabilities_check() -> DiagnosticCheck:
    try:
        catalog = catalog_from_tool_specs(build_tool_specs())
        all_tools = catalog.visible({})
        read_tools = [tool for tool in all_tools if tool.effect is ToolEffect.READ]
        effect_tools = [
            tool for tool in all_tools if tool.effect is not ToolEffect.READ
        ]
        aliases = [tool.model_name for tool in all_tools]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate model aliases")
    except Exception:  # noqa: BLE001 - diagnostic must report every catalog build failure
        return DiagnosticCheck(
            "agent.capabilities",
            "error",
            "Agent Kernel 能力目录无法构建。",
            "检查最近的分域工具定义、模型别名与 EffectPlan 契约变更。",
        )
    if not all_tools or not read_tools or not effect_tools:
        return DiagnosticCheck(
            "agent.capabilities",
            "error",
            "Agent Kernel 能力集合不完整。",
            "检查只读工具与需要 EffectPlan 确认的写入工具。",
        )
    return DiagnosticCheck(
        "agent.capabilities",
        "ok",
        f"Kernel 能力目录可用：{len(all_tools)} 项能力，{len(read_tools)} 项只读，{len(effect_tools)} 项确认后动作。",
    )


def run_agent_diagnostics() -> DiagnosticReport:
    """执行离线、只读且不回显任何密钥值的 Agent 诊断。"""
    return DiagnosticReport(
        (
            _agent_config_check(),
            _provider_check(),
            _database_check(),
            _web_secret_check(),
            _key_capabilities_check(),
            _tool_capabilities_check(),
        )
    )


def _exit_code(report: DiagnosticReport) -> int:
    return 1 if any(check.status == "error" for check in report.checks) else 0


def _text_report(report: DiagnosticReport) -> str:
    icons = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    lines = ["MediaFlux Agent Doctor（离线只读）"]
    for check in report.checks:
        lines.append(f"[{icons[check.status]}] {check.key}: {check.message}")
        if check.suggestion:
            lines.append(f"  建议: {check.suggestion}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON 报告")
    args = parser.parse_args(argv)
    report = run_agent_diagnostics()
    if args.json:
        payload = {"ok": _exit_code(report) == 0, **report.as_dict()}
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(_text_report(report))
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
