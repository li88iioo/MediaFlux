"""光鸭整理、STRM 与计划预览共用五段 Cron 的下一次执行时间。"""
from datetime import datetime

from croniter import croniter


INVALID_CRON = "cron 表达式无效，需使用 5 段格式：分 时 日 月 周"


def next_cron_run(expr: str, base: datetime | None = None) -> datetime | None:
    if len(expr.split()) != 5:
        return None
    try:
        return croniter(expr, base or datetime.now()).get_next(datetime)
    except (ValueError, KeyError):
        return None
