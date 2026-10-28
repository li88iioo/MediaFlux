"""动漫更新日历：真实日期排期、有界后台单飞、逐源缓存、北京时间。"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import datetime, timedelta
import threading
import time
from zoneinfo import ZoneInfo

from app.discovery.cache import DiscoveryCache
from app.logger import get_logger
from .http import CalendarHttp
from .metadata import CalendarMetadata, _empty
from .models import CalendarEntry, SOURCE_NAMES, SourceResult, SourceUnavailable

logger = get_logger(__name__)
TIMEZONE = ZoneInfo("Asia/Shanghai")
_ENTRY_FIELDS = {field.name for field in fields(CalendarEntry)}


def calendar_dates(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(TIMEZONE)
    now = now.replace(tzinfo=TIMEZONE) if now.tzinfo is None else now.astimezone(TIMEZONE)
    today = now.date()
    return today.isoformat(), (today - timedelta(days=today.weekday())).isoformat()


def default_providers():
    from .providers.tencent import TencentCalendarProvider
    from .providers.iqiyi import IqiyiCalendarProvider
    from .providers.youku import YoukuCalendarProvider
    return {provider.source: provider for provider in (
        TencentCalendarProvider(), IqiyiCalendarProvider(), YoukuCalendarProvider(),
    )}


class CalendarService:
    def __init__(self, *, providers=None, cache=None, http_factory=CalendarHttp,
                 metadata=None, clock=None, monotonic=time.monotonic,
                 submit=None, source_timeout=35, cooldown=300):
        self.providers = default_providers() if providers is None else dict(providers)
        if set(self.providers) - SOURCE_NAMES.keys():
            raise ValueError("未知日历来源")
        self.cache = cache or DiscoveryCache()
        self.http_factory = http_factory
        self.metadata = metadata or CalendarMetadata(self.cache)
        self.clock = clock or (lambda: datetime.now(TIMEZONE))
        self.monotonic = monotonic
        self._submit = submit
        self.source_timeout = max(1.0, min(float(source_timeout), 45))
        self.cooldown = max(30.0, float(cooldown))
        self._guard = threading.Condition(threading.RLock())
        self._pending: dict[str, Future | None] = {}
        self._last_attempt: dict[str, float] = {}
        self._last_metadata_attempt: dict[str, float] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False

    @staticmethod
    def key(source):
        return DiscoveryCache.make_key("calendar:" + source, "anime-weekly-calendar-v4", "tv", 1, None)

    @property
    def closed(self):
        with self._guard:
            return self._closed

    def _now(self):
        now = self.clock()
        return now.replace(tzinfo=TIMEZONE) if now.tzinfo is None else now.astimezone(TIMEZONE)

    def get_week(self, *, force=False, wait_sources=(), cancelled=None):
        if self.closed:
            raise SourceUnavailable("日历服务正在停止")
        for source in self.providers:
            cached = self.cache.get(self.key(source))
            if (force or cached.status not in {"fresh", "error"}
                    or (cached.status == "fresh" and (cached.last_error or self._empty_partial(cached.payload)
                                                     or self._needs_platform_posters(cached.payload)))):
                # 历史空partial不算一次成功；来源失败也不能等到原1小时TTL才允许再试。
                self._schedule(source)
            elif (cached.status == "fresh" and not cached.last_error and cached.payload
                  and self._needs_metadata(cached.payload)):
                self._schedule(source, metadata_only=True)
        # 对话等待平台事实发布，页面仍立即返回。共享同一采集任务，不等待可选的海报/TMDB补全。
        deadline = time.monotonic() + self.source_timeout + 1
        with self._guard:
            waiting = set(wait_sources) & self.providers.keys()
            while waiting:
                if self._closed or (cancelled is not None and cancelled()):
                    raise SourceUnavailable("日历查询已停止")
                waiting = {source for source in waiting if source in self._pending and not (
                    (cached := self.cache.get(self.key(source))).status in {"fresh", "stale"}
                    and cached.payload and not self._empty_partial(cached.payload)
                )}
                remaining = deadline - time.monotonic()
                if not waiting or remaining <= 0:
                    break
                self._guard.wait(min(remaining, 0.25))
        today, week_start = calendar_dates(self._now())
        start = datetime.fromisoformat(week_start).date()
        labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        days = [{"date": (start + timedelta(days=i)).isoformat(), "weekday": i + 1,
                 "label": labels[i], "is_today": (start + timedelta(days=i)).isoformat() == today,
                 "items": []} for i in range(7)]
        sources, unscheduled, updated, identities = [], [], [], set()
        metadata_pending = False
        for source, name in SOURCE_NAMES.items():
            cached = self.cache.get(self.key(source))
            with self._guard:
                pending = source in self._pending
            state = {"id": source, "name": name, "status": "unavailable", "message": "尚未获取到平台节目排期",
                     "fetched_at": "", "sampled": 0, "ignored": 0}
            payload = cached.payload if cached.status in {"fresh", "stale"} else None
            if payload and payload.get("version") == 1 and payload.get("source") == source:
                stale = cached.status == "stale" or bool(cached.last_error)
                metadata_pending |= not stale and self._needs_metadata(payload)
                state.update(status="stale" if stale else payload.get("status", "partial"),
                             message=cached.last_error or str(payload.get("message", ""))[:400],
                             fetched_at=str(payload.get("fetched_at", "")),
                             sampled=int(payload.get("sampled", 0)), ignored=int(payload.get("ignored", 0)))
                if state["status"] not in {"ok", "partial", "stale"}:
                    state["status"] = "partial"
                if self._empty_partial(payload):
                    state["status"] = "loading" if pending else "unavailable"
                if state["fetched_at"]:
                    updated.append(state["fetched_at"])
                raw_entries = payload.get("entries", [])
                if not isinstance(raw_entries, list):
                    raw_entries = []
                for raw in raw_entries[:100]:
                    try:
                        if not isinstance(raw, dict) or raw.get("category") != "animation":
                            continue
                        entry = CalendarEntry(**{key: value for key, value in raw.items() if key in _ENTRY_FIELDS})
                        if entry.source != source or entry.stable_id in identities:
                            continue
                    except (ValueError, TypeError):
                        continue
                    identities.add(entry.stable_id)
                    card = {**entry.to_dict(), "source_name": name, "stale": stale,
                            **{key: raw.get(key, value) for key, value in _empty().items()}}
                    # 原始缓存属于上周且抓取失效时，不把旧规则装作本周已核验安排。
                    old_week = stale and state["fetched_at"][:10] < week_start
                    if entry.events:
                        # 只使用源给出的真实日期，不把上周事件自动改成这周，也不由进度猜排期。
                        by_date = {}
                        for event in entry.events:
                            by_date.setdefault(event.date, []).append(event)
                        for day in days:
                            events = by_date.get(day["date"], [])
                            if not events:
                                continue
                            # 同日既有会员也有非会员时，卡片主时刻优先非会员，完整分支仍保留。
                            displayed = [event for event in events if event.audience == "free"] or events
                            times = sorted({event.update_time for event in displayed if event.update_time})
                            audiences = {event.audience for event in displayed}
                            day["items"].append({**card, "events": [event.to_dict() for event in events],
                                                 "update_time": times[0] if times else "", "update_times": times,
                                                 "schedule": "；".join(dict.fromkeys(event.schedule for event in displayed)),
                                                 "schedule_audience": next(iter(audiences)) if len(audiences) == 1 else "unknown"})
                    elif entry.free_weekdays and not old_week:
                        for day in entry.free_weekdays:
                            days[day - 1]["items"].append({**card, "update_time": entry.free_update_time,
                                                         "schedule": entry.free_schedule, "schedule_audience": "free"})
                    else:
                        if old_week:
                            card = {**card, "free_weekdays": [], "schedule_note": "旧缓存，仅保留最后核验的免费进度"}
                        unscheduled.append(card)
            elif pending:
                state.update(status="loading", message="正在获取平台更新日历")
            elif cached.last_error:
                state["message"] = cached.last_error
            if source not in self.providers:
                state["message"] = "该来源尚未启用"
            sources.append(state)
        sort_key = lambda card: (card.get("update_time") or card.get("free_update_time") or "99:99", card["source"], card["title"], card["stable_id"])
        for day in days:
            day["items"].sort(key=sort_key)
        unscheduled.sort(key=sort_key)
        with self._guard:
            refreshing = bool(self._pending)
        return {"timezone": "Asia/Shanghai", "today": today, "week_start": week_start,
                "days": days, "unscheduled": unscheduled, "sources": sources,
                "refreshing": refreshing or metadata_pending or any(s["status"] == "loading" for s in sources),
                "updated_at": max(updated, default=""), "retry_after": 5,
                "items_count": len({card["stable_id"] for day in days for card in day["items"]})}

    @staticmethod
    def _has_entries(payload):
        rows = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return False
        for row in rows[:100]:
            if not isinstance(row, dict) or row.get("category") != "animation":
                continue
            try:
                CalendarEntry(**{key: value for key, value in row.items() if key in _ENTRY_FIELDS})
            except (TypeError, ValueError):
                continue
            return True
        return False

    @classmethod
    def _empty_partial(cls, payload):
        return isinstance(payload, dict) and payload.get("status") == "partial" and not cls._has_entries(payload)

    @staticmethod
    def _needs_platform_posters(payload):
        """旧版缓存仍可展示，后台补取原图；不因升级清空已有排期。"""
        rows = payload.get("entries") if isinstance(payload, dict) else None
        return isinstance(rows, list) and any(
            isinstance(row, dict) and row.get("category") == "animation"
            and "platform_poster_key" not in row for row in rows[:100]
        )

    @staticmethod
    def _needs_metadata(payload):
        rounds = payload.get("metadata_rounds", 0)
        rows = payload.get("entries", [])
        return (isinstance(rounds, int) and 0 <= rounds < 8 and isinstance(rows, list)
                and any(isinstance(row, dict) and row.get("category") == "animation"
                        and row.get("mapping_status") == "pending" for row in rows))

    def _schedule(self, source, *, metadata_only=False):
        with self._guard:
            now = self.monotonic()
            attempts = self._last_metadata_attempt if metadata_only else self._last_attempt
            previous = attempts.get(source)
            cooldown = 5 if metadata_only else self.cooldown
            if (self._closed or source in self._pending
                    or (previous is not None and now - previous < cooldown)):
                return False
            attempts[source] = now
            job = self._refresh_metadata if metadata_only else self._refresh
            self._pending[source] = None
            try:
                if self._submit is None:
                    if self._executor is None:
                        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="anime-calendar")
                    future = self._executor.submit(job, source)
                else:
                    future = self._submit(lambda: job(source))
                if future is None:
                    self._pending.pop(source, None)
                else:
                    self._pending[source] = future
                    future.add_done_callback(lambda done, source=source: self._finished(source, done))
                return True
            except Exception:
                self._pending.pop(source, None)
                logger.warning("无法启动日历刷新 source=%s", source)
                return False

    def _finished(self, source, future):
        with self._guard:
            if self._pending.get(source) is future:
                self._pending.pop(source, None)
            self._guard.notify_all()
            release = self._closed and not self._pending
        if release:
            _release(self)

    async def _fetch(self, source):
        provider = self.providers[source]
        http = self.http_factory(provider.allowed_hosts)
        try:
            async with asyncio.timeout(self.source_timeout):
                result = await provider.fetch(http)
            if not isinstance(result, SourceResult):
                raise SourceUnavailable("公开日历响应结构无效")
            if any(not isinstance(entry, CalendarEntry) or entry.source != source for entry in result.entries):
                raise SourceUnavailable("公开节目来源身份无效")
            animation = tuple(entry for entry in result.entries if entry.category == "animation")
            if len(animation) != len(result.entries):
                result = replace(result, entries=animation, status="partial",
                                 ignored=result.ignored + len(result.entries) - len(animation),
                                 message="仅保留动漫排期；" + result.message[:380])
            return result
        finally:
            await http.aclose()

    def _cached_card(self, entry):
        profile = self.cache.get(CalendarMetadata.key(entry))
        previous = profile.payload if profile.status in {"fresh", "stale"} and profile.payload else {}
        return {**entry.to_dict(), **{key: previous.get(key, value) for key, value in _empty().items()}}

    def _metadata_order(self, entries):
        today = self._now().date().isoformat()
        # 首屏今日优先；同组保持平台原顺序，后续预算轮次继续未完成条目。
        return tuple(sorted(entries, key=lambda entry: not any(event.date == today for event in entry.events)))

    def _refresh_metadata(self, source, *, initial=False):
        """首轮与续批共用补全链路，平台事实始终按原抓取时间到期。"""
        cached = self.cache.get(self.key(source))
        if (cached.status != "fresh" or cached.last_error or not cached.payload
                or (not initial and not self._needs_metadata(cached.payload))):
            return
        payload = dict(cached.payload)
        rounds = 0 if initial else payload.get("metadata_rounds", 0) + 1
        try:
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            age = max(0, int((self._now() - fetched_at).total_seconds()))
            if age >= 3600:
                return
            # 首轮检查所有节目，续批仅处理 pending，避免已处理节目抢占预算。
            pending_rows = [row for row in payload["entries"]
                            if isinstance(row, dict) and row.get("category") == "animation"
                            and (initial or row.get("mapping_status") == "pending")]
            entries = tuple(CalendarEntry(**{key: value for key, value in row.items() if key in _ENTRY_FIELDS})
                            for row in pending_rows)
            completed = {(row["source"], row["source_id"]): row
                         for row in self.metadata.enrich(self._metadata_order(entries))}
            enriched = [completed.get((row.get("source"), row.get("source_id")), row)
                        for row in payload["entries"] if isinstance(row, dict)]
        except Exception as exc:
            logger.warning("日历元资料分批补全失败 source=%s type=%s", source, type(exc).__name__)
            enriched = [dict(row, mapping_status="unmatched") if row.get("mapping_status") == "pending" else row
                        for row in payload["entries"] if isinstance(row, dict)]
            # 凭据/网络/缓存结构失败不无限重试；保留已有资料并结束本次补全。
            rounds = 8
            try:
                fetched_at = datetime.fromisoformat(payload["fetched_at"])
                age = max(0, int((self._now() - fetched_at).total_seconds()))
            except (KeyError, TypeError, ValueError):
                return
        if rounds >= 8:
            enriched = [dict(row, mapping_status="unmatched") if row.get("mapping_status") == "pending" else row for row in enriched]
        payload.update(entries=enriched, metadata_rounds=rounds)
        age = max(0, int((self._now() - fetched_at).total_seconds()))
        with self._guard:
            if not self._closed and age < 3600:
                self.cache.set_success(self.key(source), "calendar:" + source, payload,
                                       ttl_seconds=3600 - age, stale_seconds=2 * 86400 - age)

    def _fetch_sync(self, source):
        # asyncio.run 会在已超时的请求之外 join 默认 executor，等待不可取消的 OS DNS。
        # 复用元资料的有界清理模式；CalendarHttp 另用全局槽位约束遗留解析数量。
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._fetch(source))
        finally:
            try:
                loop.run_until_complete(CalendarMetadata._finish_loop(loop))
            finally:
                loop.close()

    def _refresh(self, source):
        try:
            result = self._fetch_sync(source)
            if result.status == "unavailable":
                raise SourceUnavailable(result.message or "公开数据源暂不可用")
            if result.status == "partial" and not result.entries:
                # 部分结构里没有可用记录不等于平台本周没有节目，不能正缓存一小时。
                raise SourceUnavailable("本次未取得可核验的动漫排期；" + result.message[:280])
            fetched_at = self._now().isoformat(timespec="seconds")
            payload = {"version": 1, "source": source,
                       "entries": [self._cached_card(entry) for entry in result.entries],
                       "fetched_at": fetched_at, "status": result.status, "message": result.message,
                       "sampled": result.sampled, "ignored": result.ignored}
            with self._guard:
                if self._closed:
                    return
                # 先发布平台事实；可选的TMDB查询不能阻挡免费进度进入页面。
                self.cache.set_success(self.key(source), "calendar:" + source, payload,
                                       ttl_seconds=3600, stale_seconds=2 * 86400)
                self._guard.notify_all()
            self._refresh_metadata(source, initial=True)
        except Exception as exc:
            message = str(exc)[:400] if isinstance(exc, SourceUnavailable) else "平台更新日历获取失败，请稍后重试"
            logger.warning("日历来源刷新失败 source=%s type=%s", source, type(exc).__name__)
            with self._guard:
                if not self._closed:
                    previous = self.cache.get(self.key(source))
                    self.cache.set_error(self.key(source), "calendar:" + source, message,
                                         ttl_seconds=int(self.cooldown), retry_after=int(self.cooldown),
                                         preserve_stale=self._has_entries(previous.payload))
                    self._guard.notify_all()

    def shutdown(self):
        """回收业务任务；已取消请求的有界 OS DNS 可能仍待系统返回，但不能再发 HTTP/写缓存。"""
        with self._guard:
            self._closed = True
            self._guard.notify_all()
            futures = tuple(f for f in self._pending.values() if f is not None)
            executor = self._executor
        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        with self._guard:
            drained = not self._pending
        if drained:
            _release(self)
        return drained


_service: CalendarService | None = None
_service_lock = threading.Lock()


def _release(service):
    global _service
    with _service_lock:
        if _service is service:
            _service = None


def get_calendar_service():
    global _service
    service = _service
    if service is not None and service.closed:
        service.shutdown()
    with _service_lock:
        if _service is None:
            _service = CalendarService()
        service = _service
    if service.closed:
        raise SourceUnavailable("日历服务正在停止")
    return service


def shutdown_calendar_service():
    service = _service
    return True if service is None else service.shutdown()
