"""Agent 外部输入的数据泄露防护。"""
from __future__ import annotations

from app.sensitive_data import contains_sensitive_credential

__all__ = ["contains_sensitive_credential"]
