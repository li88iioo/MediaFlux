from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from urllib.parse import urlsplit

from ..errors import (
    IndexerError,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
)
from ..models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
    ResolvedDownload,
)
from .base import IndexerAdapter, fixed_host_join


class GeneralAdapter(IndexerAdapter):
    """一个公共站点入口，保留子站来源和失败证据，不引入第二套搜索/下载管线。"""

    site_id = "btbtla"
    site_name = "综合"
    base_url = "https://www.btbtlb.com/"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=True, download_kinds=("magnet", "torrent"))

    def __init__(self, members: tuple[IndexerAdapter, ...], *, timeout_seconds: float = 9):
        self.members = members
        self.timeout_seconds = timeout_seconds
        self._cooldowns: dict[str, tuple[float, IndexerProviderError]] = {}

    def iter_http_clients(self) -> tuple[object, ...]:
        return tuple(client for member in self.members for client in member.iter_http_clients())

    def search_timeout_overhead_seconds(self) -> float:
        # BT 影视既有请求间隔预算不因合并站点而缩短；新源与其并行。
        return max((member.search_timeout_overhead_seconds() for member in self.members), default=0)

    async def _search_member(self, member: IndexerAdapter, request: IndexerSearchRequest) -> IndexerPage:
        cooldown = self._cooldowns.get(member.site_id)
        if cooldown is not None and time.monotonic() < cooldown[0]:
            return IndexerPage([], request.page, False, member.capabilities.pagination_supported, (cooldown[1],))
        try:
            async with asyncio.timeout(self.timeout_seconds + member.search_timeout_overhead_seconds()):
                await member.wait_for_search_slot(request)
                page = await member.search(request)
            self._cooldowns.pop(member.site_id, None)
            return page
        except TimeoutError:
            error = IndexerTimeout()
        except IndexerError as exc:
            error = exc
        except Exception:
            error = IndexerUnavailable()
        public_error = IndexerProviderError(self.site_id, error.code, f"{member.site_name}：{error.public_message}")
        # 单站频控/故障独立冷却，不阻断其余综合来源，也不随别名检索反复敲打。
        self._cooldowns[member.site_id] = (time.monotonic() + 30, public_error)
        return IndexerPage([], request.page, False, member.capabilities.pagination_supported, (public_error,))

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        members = tuple(member for member in self.members if request.page == 1 or member.capabilities.pagination_supported)
        pages = await asyncio.gather(*(self._search_member(member, request) for member in members))
        # 排序、跨站 infohash 去重仍只由 IndexerService 执行，不能用去季集后的片名合并。
        return IndexerPage(
            items=[replace(item, site_id=self.site_id) for page in pages for item in page.items],
            page=request.page,
            has_more=any(page.has_more for page in pages),
            pagination_supported=True,
            errors=tuple(dict.fromkeys(error for page in pages for error in page.errors)),
        )

    def _member_for_result(self, stored_result: IndexerItem) -> IndexerAdapter:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        # 来源取冻结结果的已校验URL，不相信展示名称，也不跨子站借用客户端。
        host = urlsplit(stored_result.detail_url or "").hostname
        for member in self.members:
            for base in (member.base_url, *getattr(member, "mirror_base_urls", ())):
                if host == urlsplit(base).hostname:
                    fixed_host_join(base, stored_result.detail_url or "")
                    return member
        raise IndexerSecurityError("result origin is not a registered general source")

    def http_client_for_result(self, stored_result: IndexerItem):
        member = self._member_for_result(stored_result)
        return member.http_client_for_result(replace(stored_result, site_id=member.site_id))

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        member = self._member_for_result(stored_result)
        return await member.resolve(replace(stored_result, site_id=member.site_id))
