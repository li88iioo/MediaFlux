"""TMDB 优先、豆瓣唯一精确匹配补图；元资料永不覆盖平台日期和免费事实。"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from urllib.parse import urlsplit

from app.discovery.cache import DiscoveryCache

from .douban_http import CalendarDoubanClient
from .http import _install_bounded_dns
from .models import CalendarEntry
from .tmdb_http import CalendarTMDBClient

# 与 discovery_image 的 provider key 白名单保持一致，不导入 routes 或签名配置。
_DOUBAN_IMAGE_HOSTS = frozenset({
    "img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com",
    "img9.doubanio.com", "qnmob3.doubanio.com",
})
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~!$&'()+,;=:@/-]+$")
_ID = re.compile(r"[1-9][0-9]{0,9}")
_YEAR = re.compile(r"(?:19|20)[0-9]{2}")
_POSTER = re.compile(r"[A-Za-z0-9_-]+\.(?:jpg|png|webp)")


def _title_key(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    # 只忽略空白/标点；不删除第 N 季、续作数字等身份信息。
    return "".join(c for c in value if c.isalnum())


def _empty(status="unmatched") -> dict:
    return {"tmdb_id": "", "douban_id": "", "poster_key": "", "poster_provider": "",
            "tmdb_poster_key": "", "douban_poster_key": "", "rating": None,
            "rating_source": "", "overview": "", "origin_country": None, "mapping_status": status}


def _douban_poster_key(value):
    if not isinstance(value, str) or len(value) > 1100 or re.search(r"[\s\\\x00-\x1f]", value):
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc not in _DOUBAN_IMAGE_HOSTS
                or url.username or url.password or url.port or url.query or url.fragment):
            return ""
        key = url.netloc + url.path
    except ValueError:
        return ""
    if (len(key) > 1024 or not _SAFE_PATH.fullmatch(key) or "%" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))):
        return ""
    return key


def _public(profile):
    """缓存/上游只能提供元资料白名单，不能覆盖 events/free_progress 等事实。"""
    return {key: profile.get(key, default) for key, default in _empty().items()}


def _component(profile, provider):
    result = _empty()
    for key in (f"{provider}_id", f"{provider}_poster_key", f"_{provider}_year"):
        if key in profile:
            result[key] = profile[key]
    if provider == "tmdb":
        for key in ("rating", "rating_source", "overview", "origin_country"):
            result[key] = profile.get(key, result[key])
    return result


def _keep_good_image(profile, previous, provider):
    # 已重新匹配同一作品但上游缺图/坏图时保留旧图；换作品不能沿用旧海报。
    identity, poster = f"{provider}_id", f"{provider}_poster_key"
    if profile.get(identity) and profile[identity] == previous.get(identity) and not profile.get(poster):
        profile[poster] = previous.get(poster, "")
    return profile


class CalendarMetadata:
    def __init__(self, cache: DiscoveryCache, *, client_factory=None,
                 douban_client_factory=None, budget_seconds=12):
        self.cache = cache
        self.client_factory = client_factory or CalendarTMDBClient
        self.douban_client_factory = douban_client_factory or CalendarDoubanClient
        self.budget_seconds = max(0.0, min(float(budget_seconds), 20.0))

    @staticmethod
    def key(entry):
        return DiscoveryCache.make_key("calendar-tmdb", "profile-v3", "tv", 1, {
            "source": entry.source, "id": entry.source_id, "title": entry.title,
            "year": entry.year, "category": entry.category,
        })

    def enrich(self, entries: tuple[CalendarEntry, ...]) -> list[dict]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("CalendarMetadata.enrich 必须从同步工作线程调用")
        loop = asyncio.new_event_loop()
        try:
            _install_bounded_dns(loop)
            return loop.run_until_complete(self._enrich(entries))
        finally:
            try:
                loop.run_until_complete(self._finish_loop(loop))
            finally:
                # HTTP 已在 _enrich 的 finally 关闭。不能使用 asyncio.run 的
                # 默认 executor join：已取消的 OS DNS 可能远超元资料总预算。
                # 原生 TMDB/代理 DNS 与公开来源共用全局槽位；不等待有界的遗留解析，
                # 其迟到结果不能再发起 HTTP，也不会在后续补全轮次无限累积。
                loop.close()

    @staticmethod
    async def _finish_loop(loop):
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        try:
            async with asyncio.timeout(0.2):
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await loop.shutdown_asyncgens()
        except Exception:  # noqa: BLE001, S110 -- 有界收尾不得覆盖平台事实。
            pass

    async def _enrich(self, entries: tuple[CalendarEntry, ...]) -> list[dict]:
        deadline = time.monotonic() + self.budget_seconds
        clients, result, resolved = {}, [], {}
        try:
            for entry in entries:
                key = self.key(entry)
                if key not in resolved:
                    resolved[key] = await self._entry_profile(entry, clients, deadline)
                result.append({**entry.to_dict(), **_public(resolved[key])})
        finally:
            # 清理异常或坏客户端的无界 close 不应覆盖已核验排期。
            for client in clients.values():
                if client is not None:
                    try:
                        async with asyncio.timeout(0.2):
                            await client.aclose()
                    except Exception:  # noqa: BLE001, S110 -- 清理是降级边界，不记录可能带凭据的异常。
                        pass
        return result

    async def _entry_profile(self, entry, clients, deadline):
        try:
            cached = self.cache.get(self.key(entry))
            previous = dict(cached.payload or {}) if cached.status in {"fresh", "stale"} else {}
            fresh = cached.status == "fresh"
        except Exception:  # noqa: BLE001 -- 可选缓存故障不能导致节目丢失。
            previous, fresh = {}, False
        if time.monotonic() >= deadline:
            # 已完成的缓存可直接返回；只有本轮/缓存都未处理的条目才标记 pending。
            if previous.get("tmdb_id") or previous.get("douban_id") or (fresh and previous.get("_tmdb_complete")
                                                                     and previous.get("_douban_complete")):
                return previous
            return _empty("pending")

        completed, refreshed, profiles = {}, {}, {}
        processed = False
        not_configured = False
        for provider in ("tmdb", "douban"):
            old = _component(previous, provider)
            year = entry.year or profiles.get("tmdb", {}).get("_tmdb_year", "")
            compatible = provider == "tmdb" or old.get("_douban_year") == year
            if fresh and previous.get(f"_{provider}_complete") and compatible:
                profiles[provider], completed[provider], refreshed[provider] = old, True, False
                processed = True
                continue
            profiles[provider] = old if compatible else _empty()
            completed[provider], refreshed[provider] = False, False
            if time.monotonic() >= deadline:
                continue
            processed = True
            # suggest 不提供动画/真人类型或分页总数。没有可信年份不猜同名作品，
            # 尤其“斗罗大陆”原动画与真人剧的 suggest 标题存在交叠。
            if provider == "douban" and not _YEAR.fullmatch(str(year)):
                continue
            try:
                if provider not in clients:
                    # 构造失败一轮只尝试一次，但下一次 enrich 可恢复。
                    clients[provider] = None
                    factory = self.client_factory if provider == "tmdb" else self.douban_client_factory
                    clients[provider] = factory()
                client = clients[provider]
                if client is None:
                    continue
                if provider == "tmdb" and (not client.api_key or getattr(client, "config_error", "")):
                    not_configured = True
                    continue
                # 给另一 provider 留时间；每节目每 provider 最多一次、无重试。
                request_deadline = min(deadline, time.monotonic() + 4)
                async with asyncio.timeout(max(0, request_deadline - time.monotonic())):
                    if provider == "tmdb":
                        profile = await self._search_profile(client, entry, request_deadline)
                    else:
                        profile = await self._search_douban_profile(client, entry, year, request_deadline)
                if profile.get("_incomplete"):
                    continue
                profiles[provider] = _keep_good_image(profile, old if compatible else {}, provider)
                completed[provider], refreshed[provider] = True, True
            except Exception:  # noqa: BLE001, S112 -- provider 边界不向卡片/日志泄露请求凭据。
                # 请求错误保留该 provider 的旧 good profile，不写长期 negative。
                continue

        tmdb, douban = profiles["tmdb"], profiles["douban"]
        profile = _empty()
        for provider, component in (("tmdb", tmdb), ("douban", douban)):
            for field in (f"{provider}_id", f"{provider}_poster_key"):
                profile[field] = component.get(field, "")
            profile[f"_{provider}_year"] = component.get(f"_{provider}_year", "")
            profile[f"_{provider}_complete"] = completed[provider]
        profile.update({key: tmdb.get(key, profile[key]) for key in ("rating", "rating_source", "overview", "origin_country")})
        if profile["tmdb_poster_key"]:
            profile.update(poster_key=profile["tmdb_poster_key"], poster_provider="tmdb")
        elif profile["douban_poster_key"]:
            profile.update(poster_key=profile["douban_poster_key"], poster_provider="douban")
        matched = bool(profile["tmdb_id"] or profile["douban_id"])
        profile["mapping_status"] = ("matched" if matched else "pending" if not processed else
                                     "not_configured" if not_configured else "unmatched")
        all_complete = all(completed.values())
        changed = _public(profile) != _public(previous)
        recovered = any(refreshed[p] and not previous.get(f"_{p}_complete") for p in completed)
        if (any(refreshed.values()) and (matched or all_complete)
                and (all_complete or changed or recovered)):
            try:
                self.cache.set_success(
                    self.key(entry), "calendar-tmdb", profile,
                    ttl_seconds=86400 if matched else 3600,
                    stale_seconds=7 * 86400 if matched else 3600,
                )
            except Exception:  # noqa: BLE001, S110 -- 可选缓存故障按同样边界降级。
                pass  # 缓存写入失败不能丢已核验的节目。
        return profile

    async def _search_profile(self, client, entry, deadline):
        params = {"query": entry.title, "page": 1, "include_adult": "false"}
        if entry.year:
            params["first_air_date_year"] = entry.year
        payload = await client.get("/search/tv", params, deadline_at=deadline, retries=0)
        if not isinstance(payload, dict):
            return {**_empty(), "_incomplete": True}
        rows = payload.get("results")
        page, pages, total = (payload.get(key) for key in ("page", "total_pages", "total_results"))
        if (not isinstance(rows, list) or len(rows) > 20
                or any(isinstance(value, bool) or not isinstance(value, int) for value in (page, pages, total))
                or page != 1 or pages not in {0, 1} or total != len(rows) or (pages == 0 and rows)
                or any(not self._valid_tmdb_row(row) for row in rows)):
            return {**_empty(), "_incomplete": True}
        matches = [row for row in rows if self._matches(entry, row)]
        return self._profile(matches[0]) if len(matches) == 1 else _empty()

    @staticmethod
    def _valid_tmdb_row(raw):
        return (isinstance(raw, dict) and not isinstance(raw.get("id"), bool)
                and isinstance(raw.get("id"), (int, str)) and bool(_ID.fullmatch(str(raw["id"])))
                and isinstance(raw.get("name"), str) and 0 < len(raw["name"]) <= 200 and bool(_title_key(raw["name"]))
                and isinstance(raw.get("first_air_date", ""), str)
                and isinstance(raw.get("genre_ids"), list)
                and all(type(genre) is int for genre in raw["genre_ids"])
                and type(raw.get("adult", False)) is bool)

    @staticmethod
    def _matches(entry, raw):
        if not CalendarMetadata._valid_tmdb_row(raw):
            return False
        if _title_key(entry.title) not in {_title_key(raw.get("name", "")), _title_key(raw.get("original_name", ""))}:
            return False
        if entry.year and raw.get("first_air_date", "")[:4] != entry.year:
            return False
        if (16 in raw["genre_ids"]) != (entry.category == "animation"):
            return False
        return not raw.get("adult", False)

    @staticmethod
    def _profile(raw):
        path = raw.get("poster_path")
        poster = path[1:] if isinstance(path, str) and path.startswith("/") else path
        if not isinstance(poster, str) or not _POSTER.fullmatch(poster):
            poster = ""
        rating = raw.get("vote_average")
        if isinstance(rating, bool) or not isinstance(rating, (float, int)) or not 0 <= rating <= 10:
            rating = None
        overview = raw.get("overview")
        year = raw.get("first_air_date", "")[:4]
        # 仅采信上游产地代码；缺失或无效时保持未知，不从语言和标题推断。
        countries = raw.get("origin_country")
        if (not isinstance(countries, list) or not countries
                or any(not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country) for country in countries)):
            countries = None
        return {**_empty("matched"), "tmdb_id": str(raw["id"]), "poster_key": poster,
                "tmdb_poster_key": poster, "poster_provider": "tmdb" if poster else "",
                "rating": rating, "rating_source": "tmdb" if rating is not None else "",
                "overview": overview[:2000] if isinstance(overview, str) else "",
                "origin_country": countries,
                "_tmdb_year": year if _YEAR.fullmatch(year) else ""}

    async def _search_douban_profile(self, client, entry, year, deadline):
        payload = await client.suggest(entry.title, deadline_at=deadline)
        if (not isinstance(payload, list) or len(payload) > 20
                or any(not self._valid_douban_row(row) for row in payload)):
            return {**_empty(), "_incomplete": True}
        # 先审视所有同名/同年候选（包括错误类型和无图候选），不能先过滤再挑第一张图。
        matches = [row for row in payload if _title_key(entry.title) in
                   {_title_key(row["title"]), _title_key(row.get("sub_title", ""))} and row["year"] == year]
        if len(matches) != 1 or not self._douban_is_series(matches[0]):
            return {**_empty(), "_douban_year": year}
        row = matches[0]
        poster = _douban_poster_key(row.get("img", ""))
        return {**_empty("matched"), "douban_id": row["id"], "douban_poster_key": poster,
                "poster_key": poster, "poster_provider": "douban" if poster else "", "_douban_year": year}

    @staticmethod
    def _valid_douban_row(raw):
        # 真实 suggest 的 id/year/episode 是字符串；不把 list/bool/数字强转后匹配。
        if (not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"])
                or not isinstance(raw.get("title"), str) or not 0 < len(raw["title"]) <= 200
                or not _title_key(raw["title"]) or not isinstance(raw.get("year"), str)
                or not _YEAR.fullmatch(raw["year"]) or not isinstance(raw.get("type"), str)
                or not isinstance(raw.get("episode", ""), str)
                or not isinstance(raw.get("sub_title", ""), str) or len(raw.get("sub_title", "")) > 200
                or len(raw["type"]) > 30 or len(raw.get("episode", "")) > 10):
            return False
        # movie.douban.com 的 subject 路径必须与 id 一致，不认搜索跟踪/任意站点身份。
        if "url" in raw:
            if not isinstance(raw["url"], str) or len(raw["url"]) > 2048:
                return False
            try:
                url = urlsplit(raw["url"])
                if (url.scheme != "https" or url.netloc != "movie.douban.com"
                        or url.username or url.password or url.fragment
                        or url.path != f"/subject/{raw['id']}/"):
                    return False
            except ValueError:
                return False
        return True

    @staticmethod
    def _douban_is_series(raw):
        kind = raw["type"].strip().lower()
        if kind in {"tv", "series", "电视剧"}:
            return True
        # 匿名真实样本：电视剧/动画都标为 movie，正整数 episode 才是剧集证据。
        # 空值和 unknow 不能证明是剧集，拒绝据此把电影/人物误认成追剧作品。
        return kind == "movie" and bool(re.fullmatch(r"[1-9][0-9]{0,3}", raw.get("episode", "")))
