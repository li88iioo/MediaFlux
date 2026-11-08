"""平台追漫排期与免费进度分离；未知免费进度不能由会员数据补齐。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date as calendar_date
import re
from urllib.parse import urlsplit

from .posters import canonical_platform_poster_key

SOURCE_NAMES = {"tencent": "腾讯视频", "iqiyi": "爱奇艺", "youku": "优酷"}
PAGE_HOSTS = {
    "tencent": {"v.qq.com", "m.v.qq.com"},
    "iqiyi": {"www.iqiyi.com", "m.iqiyi.com"},
    "youku": {"www.youku.com", "v.youku.com", "m.youku.com", "youku.com"},
}


class SourceUnavailable(RuntimeError):
    """上游暂不可用，错误中不携带原始响应或凭据。"""


@dataclass(frozen=True)
class CalendarEvent:
    """平台真实日期排期；排期受众与非会员免费进度是独立事实。"""
    date: str
    update_time: str = ""
    schedule: str = ""
    audience: str = "unknown"

    def __post_init__(self):
        if (not isinstance(self.date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", self.date)
                or calendar_date.fromisoformat(self.date).isoformat() != self.date):
            raise ValueError("平台排期日期无效")
        if self.update_time and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.update_time):
            raise ValueError("平台排期时间无效")
        if not isinstance(self.schedule, str) or not self.schedule.strip() or len(self.schedule) > 400:
            raise ValueError("缺少平台排期说明")
        if self.audience not in {"unknown", "free", "member"}:
            raise ValueError("平台排期受众无效")

    @property
    def weekday(self):
        return calendar_date.fromisoformat(self.date).isoweekday()

    def to_dict(self):
        return {**asdict(self), "weekday": self.weekday}


@dataclass(frozen=True)
class CalendarEntry:
    source: str
    source_id: str
    title: str
    category: str
    url: str
    year: str = ""
    free_progress: str = ""
    free_weekdays: tuple[int, ...] = ()
    free_update_time: str = ""
    free_schedule: str = ""
    evidence: str = ""
    events: tuple[CalendarEvent, ...] = ()
    platform_poster_key: str = ""

    def __post_init__(self):
        if self.source not in SOURCE_NAMES or self.category not in {"tv", "animation"}:
            raise ValueError("不支持的日历来源或类型")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.source_id):
            raise ValueError("节目身份无效")
        parsed = urlsplit(self.url)
        if (parsed.scheme != "https" or parsed.hostname not in PAGE_HOSTS[self.source]
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ValueError("节目地址不在来源白名单")
        for field, limit in (("title", 200), ("free_progress", 120), ("free_schedule", 400), ("evidence", 400)):
            value = getattr(self, field)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("节目字段无效")
        events = tuple(CalendarEvent(**event) if isinstance(event, dict) else event for event in self.events)
        if len(events) > 50 or any(not isinstance(event, CalendarEvent) for event in events):
            raise ValueError("平台排期事件无效")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "platform_poster_key",
                           canonical_platform_poster_key(self.source, self.platform_poster_key))
        if not self.title.strip() or not self.evidence.strip() or not (events or self.free_progress or self.free_schedule):
            raise ValueError("缺少平台排期或可核验的免费信息")
        if any(isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7 for day in self.free_weekdays):
            raise ValueError("免费更新星期无效")
        days = tuple(sorted(set(self.free_weekdays)))
        if days and not self.free_schedule:
            raise ValueError("星期缺少免费排期依据")
        if self.free_update_time and (not days or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.free_update_time)):
            raise ValueError("免费更新时间无效")
        if self.year and not re.fullmatch(r"(?:19|20)\d{2}", self.year):
            raise ValueError("年份无效")
        object.__setattr__(self, "free_weekdays", days)

    @property
    def stable_id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return {**asdict(self), "stable_id": self.stable_id}


@dataclass(frozen=True)
class SourceResult:
    entries: tuple[CalendarEntry, ...] = ()
    status: str = "ok"
    message: str = ""
    sampled: int = 0
    ignored: int = 0
    # 已实际读取的日期；与来源是否覆盖平台全部节目是两回事。
    covered_dates: tuple[str, ...] = ()

    def __post_init__(self):
        if self.status not in {"ok", "partial", "unavailable"}:
            raise ValueError("日历来源状态无效")
        if len(self.entries) > 100:
            raise ValueError("来源节目数量超过上限")
        if len(self.covered_dates) > 7 or any(
            not isinstance(day, str) or calendar_date.fromisoformat(day).isoformat() != day
            for day in self.covered_dates
        ):
            raise ValueError("日历已读取日期无效")
