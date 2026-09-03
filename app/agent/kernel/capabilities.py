"""原子工具目录与本地词法能力召回。"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DANGER = "danger"


ToolCallable = Callable[..., Any]
Validator = Callable[[dict[str, Any]], dict[str, Any]]
Availability = Callable[[Mapping[str, Any]], bool]
Authorizer = Callable[[Mapping[str, Any]], bool]


def identity_validator(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments must be an object")
    return dict(arguments)


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelToolSpec:
    name: str
    domain: str
    description: str
    input_schema: Mapping[str, Any]
    effect: ToolEffect
    examples: tuple[str, ...] = ()
    cost: float = 1.0
    context_requirements: frozenset[str] = frozenset()
    validator: Validator = identity_validator
    read: ToolCallable | None = None
    prepare: ToolCallable | None = None
    execute_confirmed: ToolCallable | None = None
    verify: ToolCallable | None = None
    availability: Availability | None = None
    authorize: Authorizer | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    model_name: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        domain = str(self.domain or "").strip().casefold()
        description = str(self.description or "").strip()
        if not name or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,119}", name):
            raise ValueError(f"invalid tool name: {name!r}")
        if not domain or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", domain):
            raise ValueError(f"invalid tool domain: {domain!r}")
        if not description:
            raise ValueError("tool description cannot be empty")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("input_schema must be an object")
        if self.effect is ToolEffect.READ:
            if (
                self.read is None
                or self.prepare is not None
                or self.execute_confirmed is not None
            ):
                raise ValueError("READ tool must expose only a read handler")
        elif (
            self.prepare is None
            or self.execute_confirmed is None
            or self.read is not None
        ):
            raise ValueError(
                "WRITE/DANGER tool must expose prepare and execute_confirmed handlers"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "description", description)
        object.__setattr__(
            self,
            "examples",
            tuple(str(item).strip() for item in self.examples if str(item).strip()),
        )
        object.__setattr__(
            self,
            "context_requirements",
            frozenset(
                str(item).strip().casefold()
                for item in self.context_requirements
                if str(item).strip()
            ),
        )
        object.__setattr__(self, "cost", max(0.0, float(self.cost)))
        model_name = str(
            self.model_name or name.replace(".", "__").replace("-", "_")
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", model_name):
            raise ValueError(f"invalid provider tool name: {model_name!r}")
        object.__setattr__(self, "model_name", model_name)

    def model_definition(self) -> dict[str, Any]:
        description = self.description
        if self.effect is not ToolEffect.READ:
            description += " 此操作只生成冻结预览，必须由用户确认后才会执行。"
        return {
            "name": self.model_name,
            "description": description[:800],
            "parameters": dict(self.input_schema),
        }


class ToolCatalog:
    """只保存工具事实，不理解用户自然语言。"""

    def __init__(self, tools: Iterable[KernelToolSpec] = ()) -> None:
        self._tools: dict[str, KernelToolSpec] = {}
        self._aliases: dict[str, str] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: KernelToolSpec) -> None:
        if tool.name in self._tools or tool.model_name in self._aliases:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        self._aliases[tool.model_name] = tool.name

    def get(self, name: str) -> KernelToolSpec:
        try:
            key = str(name or "").strip()
            return self._tools[self._aliases.get(key, key)]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def visible(
        self, context: Mapping[str, Any] | None = None
    ) -> tuple[KernelToolSpec, ...]:
        runtime = context or {}
        result: list[KernelToolSpec] = []
        available_kinds = {
            str(item).strip().casefold()
            for item in runtime.get("reference_kinds", ())
            if str(item).strip()
        }
        for tool in self._tools.values():
            if tool.context_requirements and not tool.context_requirements.issubset(
                available_kinds
            ):
                continue
            if tool.availability is not None:
                try:
                    if not bool(tool.availability(runtime)):
                        continue
                except Exception as exc:  # noqa: BLE001 - optional plug-in boundary
                    logger.warning(
                        "Agent 工具可用性检查失败 tool=%s type=%s",
                        tool.name,
                        type(exc).__name__,
                    )
                    continue
            result.append(tool)
        return tuple(result)

    def has(self, name: str) -> bool:
        key = str(name or "").strip()
        return key in self._tools or key in self._aliases

    def __len__(self) -> int:
        return len(self._tools)


_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_NEGATED_PHRASE_RE = re.compile(
    r"(?:不要|无需|不需要|不使用|禁止|别用|排除)\s*([^，。；;,.!?！？\n]{1,32})",
    re.IGNORECASE,
)


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _tokens(value: object) -> list[str]:
    text = _normalize(value)
    tokens: list[str] = []
    for chunk in _WORD_RE.findall(text):
        if not chunk:
            continue
        if "\u3400" <= chunk[0] <= "\u9fff":
            if len(chunk) == 1:
                tokens.append(chunk)
            for width in (2, 3, 4):
                if len(chunk) >= width:
                    tokens.extend(
                        chunk[index : index + width]
                        for index in range(len(chunk) - width + 1)
                    )
            if len(chunk) <= 12:
                tokens.append(chunk)
        else:
            tokens.append(chunk)
            tokens.extend(
                part for part in re.split(r"[._/-]+", chunk) if part and part != chunk
            )
    return tokens


def _negated_tokens(value: object) -> Counter[str]:
    text = _normalize(value)
    result: Counter[str] = Counter()
    for match in _NEGATED_PHRASE_RE.finditer(text):
        result.update(_tokens(match.group(1)))
    return result


@dataclass(frozen=True, slots=True)
class CapabilitySelection:
    tools: tuple[KernelToolSpec, ...]
    scores: Mapping[str, float]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(tool.model_name for tool in self.tools)


class CapabilityRetriever:
    """BM25 风格候选召回；只缩小能力集合，不裁决最终意图。"""

    def __init__(self, *, minimum: int = 6, maximum: int = 8) -> None:
        if minimum < 1 or maximum < minimum or maximum > 24:
            raise ValueError("invalid capability retrieval bounds")
        self.minimum = minimum
        self.maximum = maximum

    def retrieve(
        self,
        message: str,
        catalog: ToolCatalog,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> CapabilitySelection:
        visible = list(catalog.visible(context))
        if not visible:
            return CapabilitySelection((), {})
        query_tokens = _tokens(message)
        query_counts = Counter(query_tokens)
        negated_counts = _negated_tokens(message)
        for token, count in negated_counts.items():
            remaining = query_counts.get(token, 0) - count
            if remaining > 0:
                query_counts[token] = remaining
            else:
                query_counts.pop(token, None)
        documents: dict[str, list[str]] = {}
        negative_documents: dict[str, Counter[str]] = {}
        document_frequency: Counter[str] = Counter()
        for tool in visible:
            metadata_terms: list[str] = []
            for metadata_value in tool.metadata.values():
                if isinstance(metadata_value, str):
                    metadata_terms.append(metadata_value)
                elif isinstance(metadata_value, (list, tuple, set, frozenset)):
                    metadata_terms.extend(str(item) for item in metadata_value)
            document = " ".join(
                (
                    tool.name.replace(".", " "),
                    tool.model_name.replace("_", " "),
                    tool.domain,
                    tool.description,
                    *tool.examples,
                    *metadata_terms,
                )
            )
            tokens = _tokens(document)
            documents[tool.name] = tokens
            negative_documents[tool.name] = Counter(
                _tokens(
                    " ".join(
                        (
                            tool.name.replace(".", " "),
                            tool.model_name.replace("_", " "),
                            *tool.examples,
                        )
                    )
                )
            )
            document_frequency.update(set(tokens))
        average_length = sum(len(tokens) for tokens in documents.values()) / max(
            1, len(documents)
        )
        total_docs = len(documents)
        normalized_message = _normalize(message)
        scores: dict[str, float] = {}
        for tool in visible:
            tokens = documents[tool.name]
            counts = Counter(tokens)
            score = 0.0
            for token, query_frequency in query_counts.items():
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                df = document_frequency[token]
                idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.5 * (
                    1.0 - 0.75 + 0.75 * len(tokens) / max(1.0, average_length)
                )
                score += (
                    idf * ((frequency * 2.5) / denominator) * min(2, query_frequency)
                )
            normalized_name = _normalize(tool.name)
            normalized_domain = _normalize(tool.domain)
            if normalized_name in normalized_message:
                score += 8.0
            if normalized_domain and normalized_domain in normalized_message:
                score += 3.0
            for example in tool.examples:
                normalized_example = _normalize(example)
                if normalized_example and (
                    normalized_example in normalized_message
                    or normalized_message in normalized_example
                ):
                    score += 5.0
            negative_overlap = 0.0
            negative_document = negative_documents[tool.name]
            for token, negative_frequency in negated_counts.items():
                frequency = negative_document.get(token, 0)
                if not frequency:
                    continue
                df = document_frequency[token]
                idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
                negative_overlap += idf * min(frequency, negative_frequency)
            score -= negative_overlap * 6.0
            score -= min(2.0, tool.cost * 0.05)
            scores[tool.name] = score
        ranked = sorted(
            visible,
            key=lambda item: (-scores[item.name], item.cost, item.name),
        )
        positive = sum(1 for tool in ranked if scores[tool.name] > 0)
        # 为工具声明的直接上下游能力预留少量位置。召回器仍不决定意图，
        # 但避免候选恰好占满时把写入预检/确认工具挤掉。
        reserve = min(2, max(0, self.maximum - self.minimum))
        base_limit = max(self.minimum, self.maximum - reserve)
        count = min(base_limit, max(self.minimum, positive))
        selected = list(ranked[:count])
        selected_names = {tool.name for tool in selected}
        related_candidates: list[KernelToolSpec] = []
        for source in tuple(selected):
            related = source.metadata.get("related_tools", ())
            if isinstance(related, str):
                related = (related,)
            if not isinstance(related, (list, tuple, set, frozenset)):
                continue
            for related_name in related:
                if not catalog.has(str(related_name)):
                    continue
                target = catalog.get(str(related_name))
                if target.name in selected_names or target not in visible:
                    continue
                related_candidates.append(target)
                selected_names.add(target.name)
        # 保持来源工具的相关性顺序；高排名来源声明的邻接能力优先。
        selected.extend(related_candidates[: self.maximum - len(selected)])
        selected.sort(
            key=lambda item: (-scores.get(item.name, 0.0), item.cost, item.name)
        )
        return CapabilitySelection(tuple(selected[: self.maximum]), scores)
