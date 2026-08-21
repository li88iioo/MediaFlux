"""Agent 进程内轻量运行指标。"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime
import math
import threading
from typing import Any


class AgentMetricsCollector:
    """线程安全的低基数计数与有界延迟样本。"""

    def __init__(self, *, max_latency_samples: int = 512) -> None:
        self.max_latency_samples = max(32, int(max_latency_samples))
        self._lock = threading.RLock()
        self._counters: Counter[tuple[str, str]] = Counter()
        self._tool_counters: Counter[tuple[str, str]] = Counter()
        self._latencies: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=self.max_latency_samples)
        )

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
        with self._lock:
            counters = dict(self._counters)
            tool_counters = dict(self._tool_counters)
            latencies = {key: list(value) for key, value in self._latencies.items()}
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
            self._latencies.clear()


agent_metrics = AgentMetricsCollector()
