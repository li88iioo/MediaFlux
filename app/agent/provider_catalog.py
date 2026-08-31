"""Agent Provider Gateway 的静态 operation catalog。"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.agent.models import RiskLevel
from app.agent.provider_models import ProviderGatewayError, ProviderOperationSpec

_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,5}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


def _terms(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    latin = set(re.findall(r"[a-z0-9_]{2,}", text))
    cjk = "".join(char for char in text if "\u3400" <= char <= "\u9fff")
    grams = {cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))}
    return latin | grams


class ProviderCatalog:
    def __init__(self) -> None:
        self._operations: dict[str, ProviderOperationSpec] = {}

    def register(self, spec: ProviderOperationSpec) -> None:
        operation = str(spec.operation_id or "").strip()
        provider = str(spec.provider or "").strip()
        if not _OPERATION_RE.fullmatch(operation):
            raise ValueError(f"invalid provider operation: {operation}")
        if not _PROVIDER_RE.fullmatch(provider):
            raise ValueError(f"invalid provider name: {provider}")
        if operation in self._operations:
            raise ValueError(f"duplicate provider operation: {operation}")
        if not isinstance(spec.risk, RiskLevel):
            raise TypeError(f"invalid provider operation risk: {operation}")
        if not _KIND_RE.fullmatch(str(spec.result_kind or "")):
            raise ValueError(f"invalid provider result kind: {operation}")
        if not isinstance(spec.parameters, dict) or spec.parameters.get("type") != "object":
            raise ValueError(f"invalid provider parameter schema: {operation}")
        if not 1 <= int(spec.max_items) <= 500:
            raise ValueError(f"invalid provider max_items: {operation}")
        if not 1 <= int(spec.timeout_seconds) <= 120:
            raise ValueError(f"invalid provider timeout: {operation}")
        properties = spec.parameters.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError(f"invalid provider properties: {operation}")
        if set(spec.reference_arguments) - set(properties):
            raise ValueError(f"unknown provider reference argument: {operation}")
        self._operations[operation] = spec

    def get(self, operation: str) -> ProviderOperationSpec:
        try:
            return self._operations[str(operation or "").strip()]
        except KeyError as exc:
            raise ProviderGatewayError(
                "该 Provider 操作未开放", code="operation_not_allowed"
            ) from exc

    def list(
        self,
        *,
        provider: str = "",
        intent: str = "",
        risk: RiskLevel | None = None,
        limit: int = 16,
    ) -> list[ProviderOperationSpec]:
        normalized_provider = str(provider or "").strip()
        candidates = [
            spec for spec in self._operations.values()
            if (not normalized_provider or spec.provider == normalized_provider)
            and (risk is None or spec.risk is risk)
        ]
        query_terms = _terms(intent)
        if query_terms:
            scored: list[tuple[int, str, ProviderOperationSpec]] = []
            for spec in candidates:
                haystack = " ".join((
                    spec.operation_id,
                    spec.description,
                    " ".join(spec.domains),
                    " ".join(spec.examples),
                ))
                score = len(query_terms & _terms(haystack))
                if score:
                    scored.append((score, spec.operation_id, spec))
            if scored:
                candidates = [item[2] for item in sorted(
                    scored, key=lambda item: (-item[0], item[1])
                )]
        return sorted(candidates, key=lambda item: item.operation_id)[:max(1, min(limit, 32))]

    def operations(self) -> Iterable[ProviderOperationSpec]:
        return tuple(self._operations[name] for name in sorted(self._operations))
