"""发布名识别阶段共享的无副作用值对象。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReleaseParseToken:
    """发布名解析产生的结构化令牌。

    令牌只描述“看到了什么”，不直接决定匹配结果；这样诊断、规则学习和
    TMDB 候选评分可以复用同一份无副作用解析输出。
    """

    kind: str
    value: str
    source: str = "filename"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "source": self.source}


@dataclass(frozen=True)
class ReleaseParseEvidence:
    """发布名解析证据；confidence 只表示证据可靠性，不等同最终匹配分。"""

    kind: str
    source: str
    value: object = None
    confidence: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
        }


# 这两个公开类型长期从 ``app.modules.scraper`` 暴露。保留历史模块名可让
# 旧 pickle、反射代码和日志标识继续解析，同时新代码可以从本模块直接导入。
ReleaseParseToken.__module__ = "app.modules.scraper"
ReleaseParseEvidence.__module__ = "app.modules.scraper"
