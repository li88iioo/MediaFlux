"""Media Agent 全局功能开关。"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace

from app import config
from app.logger import get_logger

logger = get_logger(__name__)

RUNTIME_FENCE_KEY = "agent.runtime_fence.v1"


class RuntimeFenceError(RuntimeError):
    """Missing, changing or mismatched durable runtime authorization."""


@dataclass(frozen=True, slots=True)
class RuntimeFence:
    v: int
    incarnation: str
    epoch: int
    phase: str
    config_digest: str


def read_runtime_fence(conn: sqlite3.Connection) -> RuntimeFence:
    row = conn.execute("SELECT value FROM settings_kv WHERE key=?", (RUNTIME_FENCE_KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row else None
        if not isinstance(data, dict) or set(data) != {"v", "incarnation", "epoch", "phase", "config_digest"}:
            raise ValueError
        if type(data["v"]) is not int or data["v"] != 1:
            raise ValueError
        if type(data["epoch"]) is not int or not 0 < data["epoch"] < 2**63:
            raise ValueError
        if data["phase"] not in ("stable", "changing"):
            raise ValueError
        for key, size in (("incarnation", 32), ("config_digest", 64)):
            value = data[key]
            if not isinstance(value, str) or len(value) != size or any(c not in "0123456789abcdef" for c in value):
                raise ValueError
        return RuntimeFence(**data)
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeFenceError("runtime_fence_unavailable") from exc


def require_runtime_fence(conn: sqlite3.Connection, expected: RuntimeFence) -> None:
    """Read on the caller's transaction; never open a second connection."""
    current = read_runtime_fence(conn)
    if (
        not isinstance(expected, RuntimeFence)
        or type(expected.epoch) is not int
        or type(expected.v) is not int
        or current.phase != "stable"
        or current != expected
    ):
        raise RuntimeFenceError("runtime_fence_changed")


def _write_fence(conn: sqlite3.Connection, fence: RuntimeFence) -> None:
    conn.execute(
        "UPDATE settings_kv SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key=?",
        (json.dumps(asdict(fence), sort_keys=True), RUNTIME_FENCE_KEY),
    )


def begin_runtime_change(conn: sqlite3.Connection, expected: RuntimeFence) -> RuntimeFence:
    if not conn.in_transaction:
        raise RuntimeFenceError("runtime_fence_transaction_required")
    require_runtime_fence(conn, expected)
    if expected.epoch >= 2**63 - 1:
        raise RuntimeFenceError("runtime_fence_exhausted")
    changing = replace(expected, epoch=expected.epoch + 1, phase="changing")
    _write_fence(conn, changing)
    return changing


def finish_runtime_change(conn: sqlite3.Connection, changing: RuntimeFence, config_digest: str) -> RuntimeFence:
    if not conn.in_transaction:
        raise RuntimeFenceError("runtime_fence_transaction_required")
    if changing.phase != "changing" or read_runtime_fence(conn) != changing:
        raise RuntimeFenceError("runtime_fence_changed")
    stable = replace(changing, phase="stable", config_digest=config_digest)
    _write_fence(conn, stable)
    return stable


def begin_installation_fence(conn: sqlite3.Connection, config_digest: str) -> RuntimeFence:
    """INSERT-only, caller-owned transaction; never reset existing authority."""
    if not conn.in_transaction:
        raise RuntimeFenceError("runtime_fence_transaction_required")
    if len(config_digest) != 64 or any(c not in "0123456789abcdef" for c in config_digest):
        raise RuntimeFenceError("runtime_snapshot_invalid")
    fence = RuntimeFence(1, secrets.token_hex(16), 1, "changing", config_digest)
    conn.execute("INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)", (RUNTIME_FENCE_KEY, json.dumps(asdict(fence), sort_keys=True)))
    return fence


def initialize_runtime_fence(*, defer_initial_setup: bool = False) -> RuntimeFence | None:
    """Called only after schema initialization; restarting never grants a new epoch."""
    from app import database as db
    with config.runtime_config_read_guard():
        if not db.resolve_db_path().is_file():
            raise RuntimeFenceError("runtime_database_unavailable")
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT 1 FROM settings_kv WHERE key=?", (RUNTIME_FENCE_KEY,)).fetchone()
            if row:
                if defer_initial_setup:
                    raise RuntimeFenceError("existing_runtime_requires_controlled_config_recovery")
                fence = read_runtime_fence(conn)
                snapshot = config.capture_runtime_config_snapshot()
                if fence.phase != "stable":
                    raise RuntimeFenceError("runtime_fence_changing")
                if fence.config_digest != snapshot.config_digest:
                    raise RuntimeFenceError("runtime_snapshot_mismatch")
                return fence
            if defer_initial_setup:
                raise RuntimeFenceError("explicit_installation_required")
            snapshot = config.capture_runtime_config_snapshot()
            fence = RuntimeFence(1, secrets.token_hex(16), 1, "stable", snapshot.config_digest)
            conn.execute(
                "INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",
                (RUNTIME_FENCE_KEY, json.dumps(asdict(fence), sort_keys=True)),
            )
            return fence

_runtime_generation_lock = threading.Lock()
_runtime_admission_lock = threading.RLock()
_runtime_generation = 0
_telegram_runtime_generation = 0


def is_agent_enabled() -> bool:
    """Media Agent 总开关（AGENT_LLM_ENABLED）：所有 AI 能力共用，默认关闭；子功能只在开启时生效。

    读取调用方当前的配置快照；逐次模型请求另用 is_agent_llm_enabled 读取最新值。
    """

    return config.get_bool("AGENT_LLM_ENABLED", False)


def is_agent_llm_enabled() -> bool:
    """每次发送读取最新有效模型开关，不继承调用者的旧配置快照。"""
    with config.use_runtime_config_snapshot(config.capture_runtime_config_snapshot()):
        return config.get_bool("AGENT_LLM_ENABLED", False)


def recognition_review_enabled() -> bool:
    """整理候选的 Agent 只读复核是否可用；配置异常时按关闭处理，人工链路不受影响。"""
    try:
        return bool(
            is_agent_enabled()
            and config.get_bool("AGENT_RECOGNITION_REVIEW_ENABLED", False)
            and (config.get("AGENT_LLM_API_URL", "") or "").strip()
            and (config.get("AGENT_LLM_MODEL", "") or "").strip()
        )
    except Exception as exc:  # noqa: BLE001 - 可选复核不能阻断整理
        logger.warning("读取 Agent 主动复核配置失败 type=%s", type(exc).__name__)
        return False


def current_agent_runtime_generation(*, channel: str = "") -> int:
    """返回当前进程内 Agent 运行代次，用于隔离开关切换前的旧任务。"""
    with _runtime_generation_lock:
        return _runtime_generation + (_telegram_runtime_generation if channel == "telegram" else 0)


def invalidate_agent_runtime_generation(*, changed_keys: Iterable[str] | None = None) -> int:
    """配置发布只撤销受影响渠道；不带配置范围的撤销仍作用于整个 Agent。"""
    global _runtime_generation, _telegram_runtime_generation
    keys = set(changed_keys) if changed_keys is not None else None
    with _runtime_generation_lock:
        if keys is None or keys - {"AGENT_STREAM_DISPLAY_ENABLED", "TG_AGENT_ENABLED"}:
            _runtime_generation += 1
        elif "TG_AGENT_ENABLED" in keys:
            _telegram_runtime_generation += 1
        return _runtime_generation


def agent_runtime_generation_is_current(generation: int, *, channel: str = "") -> bool:
    """仅严格整数代次与当前值一致时允许继续发布副作用。"""
    if not isinstance(generation, int) or isinstance(generation, bool):
        return False
    with _runtime_generation_lock:
        return generation == _runtime_generation + (_telegram_runtime_generation if channel == "telegram" else 0)


class AgentRuntimeDisabled(RuntimeError):
    """Agent 运行态已关闭，新的受控写操作不得进入。"""


@contextmanager
def agent_runtime_transition() -> Generator[None, None, None]:
    """串行化运行态开关切换与不可逆确认写操作的准入。"""
    with _runtime_admission_lock:
        yield


@contextmanager
def agent_runtime_admission(
    *,
    require_telegram: bool = False,
    expected_generation: int | None = None,
    agent_enabled_check: Callable[[], bool] | None = None,
    telegram_enabled_check: Callable[[], bool] | None = None,
) -> Generator[int, None, None]:
    """在线性化边界内准入并执行一次确认写操作。

    持锁期间关闭动作会等待；若关闭先完成，则新的确认会被拒绝。这样不会
    出现“关闭已返回成功，旧确认随后才开始产生副作用”的 TOCTOU。
    """
    with _runtime_admission_lock:
        if not (agent_enabled_check or is_agent_enabled)():
            raise AgentRuntimeDisabled("Media Agent 已关闭")
        telegram_enabled = telegram_enabled_check or (
            lambda: config.get_bool("TG_AGENT_ENABLED", False)
        )
        if require_telegram and not telegram_enabled():
            raise AgentRuntimeDisabled("Telegram Agent 已关闭")
        generation = current_agent_runtime_generation()
        if expected_generation is not None and (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation != generation
        ):
            raise AgentRuntimeDisabled("Media Agent 状态已变化")
        yield generation


@contextmanager
def agent_runtime_effect_admission(generation: int) -> Generator[None, None, None]:
    """线性化后台外部副作用与 Agent 总开关切换。

    后台任务在领取时已经记录运行代次；这里不重复解释子功能配置，只保证
    总开关关闭并返回后，旧代次不会再开始 Telegram 等不可撤销外部动作。
    """
    with _runtime_admission_lock:
        if not agent_runtime_generation_is_current(generation):
            raise AgentRuntimeDisabled("Media Agent 运行代次已失效")
        yield
