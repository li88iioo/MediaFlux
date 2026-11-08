"""腾讯官方追漫日历：本周七天动漫排期与免费进度独立，不请求播放器。"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from app.discovery.calendar.models import (
    CalendarEntry,
    CalendarEvent,
    SourceResult,
    SourceUnavailable,
)
from app.discovery.calendar.posters import platform_poster_key

_API = "https://pbaccess.video.qq.com/trpc.vector_layout.page_view.PageService/getPage?video_appid=3000010&vversion_platform=2"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CID = re.compile(r"[a-z0-9]{15}")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_MODULE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_NON_FEATURE = re.compile(r"预告|花絮|试看|片花|剪辑|trailer|preview", re.IGNORECASE)


def _text(value, limit=400):
    if (not isinstance(value, str) or len(value) > limit
            or re.search(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f]", value)):
        return ""
    return value.strip()


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"[0-9]{8}", value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    return None


def _body(*, day=None, module_id=None):
    params: dict = {"page_type": "channel", "page_id": "100119", "scene": "channel", "new_mark_label_enabled": "1"}
    if day is not None:
        params.update(week=day.strftime("%Y%m%d"), un_mod_id=module_id, un_module_key="")
    return {
        "page_params": params,
        "page_bypass_params": {
            "params": {"platform_id": "2", "caller_id": "3000010", "data_mode": "default",
                       "user_mode": "default", "specified_strategy": "", **params},
            "scene": "channel", "app_version": "", "abtest_bypass_id": "",
        },
        "page_context": None,
    }


def _modules(payload):
    if (not isinstance(payload, dict) or type(payload.get("ret")) is not int or payload["ret"] != 0):
        raise SourceUnavailable("腾讯公开排期接口未返回有效结果")
    data = payload.get("data")
    cards = data.get("CardList") if isinstance(data, dict) else None
    return [card for card in cards[:100] if isinstance(card, dict)] if isinstance(cards, list) else []


def _children(module):
    children = module.get("children_list")
    group = children.get("list") if isinstance(children, dict) else None
    cards = group.get("cards") if isinstance(group, dict) else None
    return [card for card in cards[:400] if isinstance(card, dict)] if isinstance(cards, list) else []


def _calendar(modules):
    return next((module for module in modules if module.get("type") == "channel_play_schedule"), None)


def _navigation(module, week):
    """日期和模块参数来自官方导航；不跟随上游 URL 或广告/实验上下文。"""
    module_id = module.get("id")
    if not isinstance(module_id, str) or not _MODULE_ID.fullmatch(module_id):
        return {}, None
    days, selected = {}, set()
    for child in _children(module):
        params = child.get("params")
        if child.get("type") != "navigation" or not isinstance(params, dict):
            continue
        day = _date(params.get("week"))
        if day is None or day not in week:
            continue
        key = _text(params.get("data_key"), 500)
        try:
            query = parse_qs(key, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            continue
        if (query.get("week") != [day.strftime("%Y%m%d")]
                or query.get("page_id") != ["100119"]
                or query.get("un_mod_id") != [module_id]
                or query.get("un_module_key") != [""]):
            continue
        days[day] = module_id
        if params.get("selected") == "1":
            selected.add(day)
    return days, next(iter(selected)) if len(selected) == 1 else None


def _programme(params):
    cid, title = params.get("cid"), _text(params.get("title"), 200)
    if (not isinstance(cid, str) or not _CID.fullmatch(cid) or not title
            or _NON_FEATURE.search(title) or str(params.get("type")) != "3"):
        return None
    published = _published(params)
    return {"source_id": cid, "title": title, "category": "animation",
            "url": f"https://v.qq.com/x/cover/{cid}.html",
            "year": str(published.year) if published and 1900 <= published.year <= 2099 else ""}


def _calendar_events(params, day, today):
    schedule = _text(params.get("update_notify_desc")) or _text(params.get("sub_title"))
    if not schedule or _NON_FEATURE.search(schedule):
        return ()
    # 官方日历里的当日/未来首播不等于预告片更新；只接受有同日首播日期的节目计划。
    if (params.get("is_trailer") != "0"
            and not (params.get("is_trailer") == "1" and day >= today
                     and _date(params.get("publish_date")) == day and "首播" in schedule)):
        return ()
    events = []
    for field, audience in (("pay_time", "member"), ("free_time", "free")):
        clock = params.get(field)
        if isinstance(clock, str) and _TIME.fullmatch(clock):
            events.append(CalendarEvent(date=day.isoformat(), update_time=clock,
                                        schedule=schedule, audience=audience))
    # free_episode/pay_episode 包括未来日期的排期目标，不等于已播/已免费进度。
    return tuple(events)


def _published(params):
    value = _date(params.get("publish_date"))
    if value is not None:
        return value
    timestamp = params.get("hollywood_online")
    value = _date(timestamp[:10]) if isinstance(timestamp, str) else None
    source_year = str(params.get("year", ""))
    if value and re.fullmatch(r"(?:19|20)[0-9]{2}", source_year) and source_year != str(value.year):
        return None  # 旧作品新上架年份不能当作首次播出年份。
    return value


class TencentCalendarProvider:
    source = "tencent"
    allowed_hosts = frozenset({"pbaccess.video.qq.com"})

    def __init__(self, *, clock=None):
        """clock 返回 datetime 或 date；默认使用 Asia/Shanghai 当前时间。"""
        self._clock = clock or (lambda: datetime.now(_SHANGHAI))

    def _today(self):
        now = self._clock()
        if isinstance(now, datetime):
            return (now.replace(tzinfo=_SHANGHAI) if now.tzinfo is None else now.astimezone(_SHANGHAI)).date()
        if isinstance(now, date):
            return now
        raise ValueError("clock 必须返回日期或时间")

    async def fetch(self, http) -> SourceResult:
        today = self._today()
        monday = today - timedelta(days=today.weekday())
        week = tuple(monday + timedelta(days=i) for i in range(7))
        programmes, collected = {}, defaultdict(set)
        poster_keys, poster_conflicts = defaultdict(set), set()
        sampled, ignored, covered = set(), 0, set()
        failed = False

        def add(params, events):
            nonlocal ignored
            programme = _programme(params)
            if programme is None:
                ignored += 1
                return
            cid = programme["source_id"]
            sampled.add(cid)
            if not events or (cid not in programmes and len(programmes) >= 100):
                ignored += 1
                return
            previous = programmes.setdefault(cid, programme)
            # 不改变既有 CID/排期合并；元信息或有效原图冲突时只放弃封面。
            if previous != programme:
                poster_conflicts.add(cid)
            key = platform_poster_key(self.source, params.get("image_url"))
            if key and len(poster_keys[cid]) < 2:
                poster_keys[cid].add(key)
            collected[cid].update(events)

        def read_day(module, day):
            children = module.get("children_list")
            group = children.get("list") if isinstance(children, dict) else None
            if not isinstance(group, dict) or not isinstance(group.get("cards"), list):
                raise SourceUnavailable("腾讯当日排期结构无效")
            covered.add(day)
            for child in _children(module):
                params = child.get("params")
                if child.get("type") != "poster" or not isinstance(params, dict):
                    continue
                add(params, _calendar_events(params, day, today))

        try:
            initial = _modules(await http.post_json(_API, json_body=_body()))
            calendar = _calendar(initial)
            if calendar is not None:
                navigation, initial_day = _navigation(calendar, week)
                if initial_day is not None:
                    read_day(calendar, initial_day)
                # 仅动漫本周导航：一次默认日 + 最多六次切日，共不超过七次 POST。
                for day in sorted(day for day in navigation if day != initial_day)[:6]:
                    modules = _modules(await http.post_json(
                        _API, json_body=_body(day=day, module_id=navigation[day])))
                    module = _calendar(modules)
                    if module is not None and module.get("id") == navigation[day]:
                        response_days, _ = _navigation(module, week)
                        if day not in response_days:
                            raise SourceUnavailable("腾讯切日响应日期未核验")
                        # selected 可仍是“今天”；请求日必须同时属于响应已核验的本周导航。
                        read_day(module, day)
        except Exception:  # noqa: BLE001 -- 源隔离边界；保留已核验日期，不捕获取消信号。
            # 停止后续请求，但不丢弃已核验日期。取消信号仍向上传播；异常细节不回显。
            failed = True
        entries = tuple(CalendarEntry(source=self.source, **programme,
                                      platform_poster_key=(next(iter(poster_keys[cid]))
                                          if cid not in poster_conflicts and len(poster_keys[cid]) == 1 else ""),
                                      evidence="腾讯官方 channel_play_schedule：日期取官方 week 导航/查询，会员与非会员时刻分别取 pay_time/free_time；不把排期集数当已免费进度。",
                                      events=tuple(sorted(collected[cid], key=lambda e: (e.date, e.update_time, e.audience, e.schedule)))[:50])
                        for cid, programme in programmes.items())
        if failed and not entries:
            return SourceResult(status="unavailable", sampled=len(sampled), ignored=ignored,
                                message="腾讯公开周排期读取失败或要求验证，已停止请求；未取得可用排期。")
        failure_note = "后续接口读取失败或要求验证，已停止请求；仅保留已核验事件，覆盖不全。" if failed else ""
        return SourceResult(entries=entries,
                            status="ok" if covered == set(week) and not ignored and not failed else "partial",
                            sampled=len(sampled), ignored=ignored,
                            covered_dates=tuple(day.isoformat() for day in sorted(covered)),
                            message=f"腾讯官方追漫周历已读取本周{len(covered)}/7天，范围限该官方周历，不代表全站片库或全部排期。{failure_note}未纳入排期记录{ignored}条。会员/免费排期独立，未核验的免费进度留空。")
