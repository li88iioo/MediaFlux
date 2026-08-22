"""Agent 进程内轻量运行指标。"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime
import math
import re
import threading
from typing import Any


_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:/-]+")


class AgentMetricsCollector:
    """线程安全的低基数计数与有界延迟样本。"""

    def __init__(self, *, max_latency_samples: int = 512) -> None:
        self.max_latency_samples = max(32, int(max_latency_samples))
        self._lock = threading.RLock()
        self._counters: Counter[tuple[str, str]] = Counter()
        self._tool_counters: Counter[tuple[str, str]] = Counter()
        self._llm_counters: Counter[tuple[str, str, str]] = Counter()
        self._llm_tokens: Counter[tuple[str, str, str]] = Counter()
        self._latencies: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=self.max_latency_samples)
        )

    @staticmethod
    def _dimension(value: object, *, default: str, limit: int = 96) -> str:
        normalized = _SAFE_LABEL_RE.sub("_", str(value or "").strip())[:limit].strip("_")
        return normalized or default

    def record_query(self, *, elapsed_ms: int, ok: bool) -> None:
        with self._lock:
            self._counters[("queries", "success" if ok else "error")] += 1
            self._latencies["query"].append(max(0, int(elapsed_ms)))

    def record_tool(self, tool_name: str, *, elapsed_ms: int, ok: bool) -> None:
        name = str(tool_name or "unknown").strip()[:160] or "unknown"
        outcome = "success" if ok else "error"
        with self._lock:
            self._counters[("tools", outcome)] += 1
            self._tool_counters[(name, outcome)] += 1
            self._latencies["tool"].append(max(0, int(elapsed_ms)))

    def record_confirmation(self, outcome: str) -> None:
        normalized = str(outcome or "unknown").strip().lower()[:32] or "unknown"
        with self._lock:
            self._counters[("confirmations", normalized)] += 1

    def record_llm_request(
        self,
        protocol: str,
        model: str,
        *,
        outcome: str,
        elapsed_ms: int,
        usage: Any | None = None,
    ) -> None:
        protocol_name = self._dimension(protocol, default="unknown", limit=32)
        model_name = self._dimension(model, default="unknown")
        outcome_name = self._dimension(outcome, default="unknown", limit=48).lower()
        with self._lock:
            self._llm_counters[(protocol_name, model_name, outcome_name)] += 1
            self._latencies[f"llm:{protocol_name}:{model_name}"].append(
                max(0, int(elapsed_ms))
            )
            if usage is not None:
                for token_type, attribute in (
                    ("prompt", "prompt_tokens"),
                    ("completion", "completion_tokens"),
                    ("cached", "cached_tokens"),
                    ("reasoning", "reasoning_tokens"),
                ):
                    value = max(0, int(getattr(usage, attribute, 0) or 0))
                    self._llm_tokens[(protocol_name, model_name, token_type)] += value

    def record_query_breakdown(
        self, *, turns: int, llm_ms: int, tools_ms: int
    ) -> None:
        with self._lock:
            self._latencies["turns"].append(max(0, int(turns)))
            self._latencies["llm"].append(max(0, int(llm_ms)))
            self._latencies["tools"].append(max(0, int(tools_ms)))

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = max(0, math.ceil((len(ordered) * percentile)) - 1)
        return int(ordered[min(index, len(ordered) - 1)])

    @classmethod
    def _latency_summary(cls, values: list[int]) -> dict[str, int | float]:
        if not values:
            return {"count": 0, "avg": 0.0, "p50": 0, "p95": 0, "max": 0}
        return {
            "count": len(values),
            "avg": round(sum(values) / len(values), 2),
            "p50": cls._percentile(values, 0.50),
            "p95": cls._percentile(values, 0.95),
            "max": max(values),
        }

    def snapshot(self) -> dict[str, Any]:
        from app.database import get_sqlite_contention_metrics

        with self._lock:
            counters = dict(self._counters)
            tool_counters = dict(self._tool_counters)
            llm_counters = dict(self._llm_counters)
            llm_tokens = dict(self._llm_tokens)
            latencies = {key: list(value) for key, value in self._latencies.items()}
        providers = sorted({(protocol, model) for protocol, model, _ in llm_counters} | {
            (protocol, model) for protocol, model, _ in llm_tokens
        })
        return {
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "queries": {
                "success": counters.get(("queries", "success"), 0),
                "error": counters.get(("queries", "error"), 0),
            },
            "tools": {
                "success": counters.get(("tools", "success"), 0),
                "error": counters.get(("tools", "error"), 0),
                "by_name": {
                    name: {
                        "success": tool_counters.get((name, "success"), 0),
                        "error": tool_counters.get((name, "error"), 0),
                    }
                    for name in sorted({key[0] for key in tool_counters})
                },
            },
            "confirmations": {
                outcome: count
                for (kind, outcome), count in sorted(counters.items())
                if kind == "confirmations"
            },
            "llm": {
                "providers": [
                    {
                        "protocol": protocol,
                        "model": model,
                        "outcomes": {
                            outcome: count
                            for (item_protocol, item_model, outcome), count in sorted(llm_counters.items())
                            if item_protocol == protocol and item_model == model
                        },
                        "tokens": {
                            token_type: llm_tokens.get((protocol, model, token_type), 0)
                            for token_type in ("prompt", "completion", "cached", "reasoning")
                        },
                        "latency_ms": self._latency_summary(
                            latencies.get(f"llm:{protocol}:{model}", [])
                        ),
                    }
                    for protocol, model in providers
                ],
                "turns": self._latency_summary(latencies.get("turns", [])),
                "llm_ms": self._latency_summary(latencies.get("llm", [])),
                "tools_ms": self._latency_summary(latencies.get("tools", [])),
            },
            "sqlite_contention": get_sqlite_contention_metrics(),
            "latency_ms": {
                "query": self._latency_summary(latencies.get("query", [])),
                "tool": self._latency_summary(latencies.get("tool", [])),
            },
        }

    @staticmethod
    def _label(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP mediaflux_agent_queries_total Agent query requests.",
            "# TYPE mediaflux_agent_queries_total counter",
        ]
        for outcome, value in snapshot["queries"].items():
            lines.append(
                f'mediaflux_agent_queries_total{{outcome="{outcome}"}} {int(value)}'
            )
        lines.extend([
            "# HELP mediaflux_agent_tool_calls_total Agent tool calls.",
            "# TYPE mediaflux_agent_tool_calls_total counter",
        ])
        for name, outcomes in snapshot["tools"]["by_name"].items():
            for outcome, value in outcomes.items():
                lines.append(
                    "mediaflux_agent_tool_calls_total{"
                    f'tool="{self._label(name)}",outcome="{outcome}"}} {int(value)}'
                )
        lines.extend([
            "# HELP mediaflux_agent_confirmations_total Agent confirmation lifecycle events.",
            "# TYPE mediaflux_agent_confirmations_total counter",
        ])
        for outcome, value in snapshot["confirmations"].items():
            lines.append(
                f'mediaflux_agent_confirmations_total{{outcome="{self._label(outcome)}"}} {int(value)}'
            )
        providers = snapshot["llm"]["providers"]
        lines.extend([
            "# HELP mediaflux_agent_llm_requests_total LLM provider requests by outcome.",
            "# TYPE mediaflux_agent_llm_requests_total counter",
        ])
        for provider in providers:
            labels = (
                f'protocol="{self._label(provider["protocol"])}",'
                f'model="{self._label(provider["model"])}"'
            )
            for outcome, value in provider["outcomes"].items():
                lines.append(
                    f'mediaflux_agent_llm_requests_total{{{labels},outcome="{self._label(outcome)}"}} {int(value)}'
                )
        lines.extend([
            "# HELP mediaflux_agent_llm_tokens_total Provider-reported LLM token usage.",
            "# TYPE mediaflux_agent_llm_tokens_total counter",
        ])
        for provider in providers:
            labels = (
                f'protocol="{self._label(provider["protocol"])}",'
                f'model="{self._label(provider["model"])}"'
            )
            for token_type, value in provider["tokens"].items():
                lines.append(
                    f'mediaflux_agent_llm_tokens_total{{{labels},type="{token_type}"}} {int(value)}'
                )
        lines.extend([
            "# HELP mediaflux_agent_llm_latency_milliseconds LLM provider request latency summary.",
            "# TYPE mediaflux_agent_llm_latency_milliseconds gauge",
        ])
        for provider in providers:
            labels = (
                f'protocol="{self._label(provider["protocol"])}",'
                f'model="{self._label(provider["model"])}"'
            )
            for statistic in ("p50", "p95", "max"):
                lines.append(
                    f'mediaflux_agent_llm_latency_milliseconds{{{labels},statistic="{statistic}"}} '
                    f'{int(provider["latency_ms"][statistic])}'
                )
        lines.extend([
            "# HELP mediaflux_agent_query_breakdown Agent LLM turns and component timing summary.",
            "# TYPE mediaflux_agent_query_breakdown gauge",
        ])
        for kind in ("turns", "llm_ms", "tools_ms"):
            for statistic in ("p50", "p95", "max"):
                lines.append(
                    "mediaflux_agent_query_breakdown{"
                    f'kind="{kind}",statistic="{statistic}"}} '
                    f'{int(snapshot["llm"][kind][statistic])}'
                )
        lines.extend([
            "# HELP mediaflux_sqlite_contention_total SQLite contention events by kind and phase.",
            "# TYPE mediaflux_sqlite_contention_total counter",
        ])
        for kind, value in sorted(snapshot["sqlite_contention"].items()):
            lines.append(
                f'mediaflux_sqlite_contention_total{{kind="{self._label(kind)}"}} {int(value)}'
            )
        lines.extend([
            "# HELP mediaflux_agent_latency_milliseconds Agent query and tool latency summary.",
            "# TYPE mediaflux_agent_latency_milliseconds gauge",
        ])
        for kind, summary in snapshot["latency_ms"].items():
            for statistic in ("p50", "p95", "max"):
                lines.append(
                    "mediaflux_agent_latency_milliseconds{"
                    f'kind="{kind}",statistic="{statistic}"}} {int(summary[statistic])}'
                )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._tool_counters.clear()
            self._llm_counters.clear()
            self._llm_tokens.clear()
            self._latencies.clear()


agent_metrics = AgentMetricsCollector()
