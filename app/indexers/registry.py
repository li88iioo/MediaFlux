from __future__ import annotations

from collections.abc import Mapping

from .http import BrowserImpersonatingHttpClient, FixedHostHttpClient
from .providers.base import IndexerAdapter
from .providers.btbtla import BTBtlaAdapter
from .providers.aipan import AipanAdapter
from .providers.empire import EmpireAdapter
from .providers.kpkuang import KPkuangAdapter, KPKUANG_HOST_CONFIG, MAX_RESPONSE_BYTES as KPKUANG_MAX_BYTES
from .providers.mikan import MikanAdapter
from .providers.nyaa import NyaaAdapter
from .providers.piratebay import PirateBayAdapter


class IndexerRegistry:
    def __init__(self, adapters: Mapping[str, IndexerAdapter]):
        self._adapters = dict(adapters)
        for key, adapter in self._adapters.items():
            if key != adapter.site_id:
                raise ValueError(f"registry key {key!r} does not match adapter {adapter.site_id!r}")

    def ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def get(self, site_id: str) -> IndexerAdapter:
        return self._adapters[site_id]

    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(site_id for site_id, adapter in self._adapters.items() if adapter.default_enabled)

    async def aclose(self) -> None:
        seen: set[int] = set()
        for adapter in self._adapters.values():
            iterator = getattr(adapter, "iter_http_clients", None)
            clients = iterator() if callable(iterator) else (getattr(adapter, "http", None),)
            for client in clients:
                if client is None or id(client) in seen:
                    continue
                seen.add(id(client))
                close = getattr(client, "aclose", None)
                if close is not None:
                    await close()


def build_default_registry(
    *,
    http_clients: Mapping[str, object] | None = None,
    user_agent: str = "MediaFlux/1.0",
    nyaa_endpoint_timeout_seconds: float = 4,
    btbtla_min_interval_seconds: float = 5,
    btbtla_page_size: int = 40,
    btbtla_cache_ttl_seconds: int = 120,
    tpb_min_interval_seconds: float = 1,
) -> IndexerRegistry:
    supplied = dict(http_clients or {})
    # nyaa.si 会按来源 IP 限流；nyaa.net 为同引擎镜像，主站失败时回落。
    nyaa_http = supplied.get("nyaa") or FixedHostHttpClient(
        allowed_hosts={"nyaa.si", "nyaa.net"}, user_agent=user_agent,
        pin_resolved_address=True,
    )
    sukebei_http = supplied.get("sukebei") or FixedHostHttpClient(
        allowed_hosts={"sukebei.nyaa.si"},
        user_agent=user_agent,
        pin_resolved_address=True,
    )
    mikan_http = supplied.get("mikan") or FixedHostHttpClient(
        allowed_hosts={"mikanani.me", "mikanime.tv"},
        user_agent=user_agent,
        pin_resolved_address=True,
        # Mikan 搜索页内嵌全部剧集条目，热门作品实测 4MiB+，默认 2MiB 会截断。
        max_response_bytes=8 * 1024 * 1024,
    )
    btbtla_http = supplied.get("btbtla") or BrowserImpersonatingHttpClient(
        allowed_hosts={"www.btbtlb.com", "btbtlb.com"},
        sni_host="btbtlb.com",
    )
    tpb_http = supplied.get("tpb") or FixedHostHttpClient(
        allowed_hosts={"apibay.org"},
        user_agent=user_agent,
        pin_resolved_address=True,
    )
    aipan_http = supplied.get("aipan") or FixedHostHttpClient(
        allowed_hosts={"www.aipan.me"}, user_agent=user_agent, pin_resolved_address=True,
    )
    dygang_http = supplied.get("dygang") or FixedHostHttpClient(
        allowed_hosts={"www.dygang.tv"}, user_agent=user_agent, pin_resolved_address=True,
    )
    ys5266_http = supplied.get("ys5266") or FixedHostHttpClient(
        allowed_hosts={"www.5266ys.net"}, user_agent=user_agent, pin_resolved_address=True,
    )
    kpkuang_http = supplied.get("kpkuang") or FixedHostHttpClient(
        allowed_hosts=KPKUANG_HOST_CONFIG["allowed_hosts"],
        user_agent=user_agent, pin_resolved_address=True, max_response_bytes=KPKUANG_MAX_BYTES,
    )
    return IndexerRegistry(
        {
            "nyaa": NyaaAdapter(
                site_id="nyaa",
                site_name="Nyaa",
                base_url="https://nyaa.si/",
                http=nyaa_http,
                default_enabled=True,
                mirror_base_urls=("https://nyaa.net/",),
                endpoint_timeout_seconds=nyaa_endpoint_timeout_seconds,
            ),
            "sukebei": NyaaAdapter(
                site_id="sukebei",
                site_name="Sukebei",
                base_url="https://sukebei.nyaa.si/",
                http=sukebei_http,
                default_enabled=False,
            ),
            "mikan": MikanAdapter(http=mikan_http),
            "btbtla": BTBtlaAdapter(
                http=btbtla_http,
                min_interval_seconds=btbtla_min_interval_seconds,
                page_size=btbtla_page_size,
                cache_ttl_seconds=btbtla_cache_ttl_seconds,
            ),
            "aipan": AipanAdapter(http=aipan_http),
            "dygang": EmpireAdapter(
                site_id="dygang",
                site_name="电影港",
                base_url="https://www.dygang.tv/",
                http=dygang_http,
            ),
            "ys5266": EmpireAdapter(
                site_id="ys5266",
                site_name="5266影视",
                base_url="https://www.5266ys.net/",
                http=ys5266_http,
            ),
            "kpkuang": KPkuangAdapter(http=kpkuang_http),
            "tpb": PirateBayAdapter(
                http=tpb_http,
                min_interval_seconds=tpb_min_interval_seconds,
            ),
        }
    )
