"""豆瓣公共优先、Frodo 可选回退的探索 Provider。"""
from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from app import config as app_config
from app.clients.douban_authenticated import (
    DoubanAuthenticatedClient,
    normalize_dbcl2,
)
from app.clients.douban_frodo import DoubanFrodoClient
from app.clients.douban_public import DoubanPublicClient
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderAuthenticationError,
    ProviderError,
    ProviderHealth,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.base import DEFAULT_TIMEOUT, DiscoveryProvider, TimeoutValue
from app.logger import get_logger

logger = get_logger(__name__)

# dbcl2 凭据失败后定期复探；Frodo 凭据在进程内不会刷新，认证失败后
# 直接停用到进程重启，避免每 30 分钟重复 401/403 并刷日志。
_FALLBACK_AUTH_COOLDOWN_SECONDS = 1800.0
_FRODO_AUTH_DISABLED_UNTIL_RESTART = math.inf
_FALLBACK_FAILURE_THRESHOLD = 3
_FALLBACK_COOLDOWN_SECONDS = 300.0
_FRODO_CREDENTIAL_SOURCES = frozenset(
    {"explicit", "environment", "compatibility_default"}
)
_FRODO_AUTH_STATE_CONDITION = threading.Condition()
_FRODO_AUTH_DISABLED_CREDENTIALS: set[str] = set()
_FRODO_AUTH_IN_FLIGHT: set[str] = set()
_NO_FALLBACK_RESULT = object()

_CATEGORY_MEDIA = {
    "recommend": {"movie", "tv"},
    "discover": {"movie", "tv"},
    "movie_showing": {"movie"},
    "movie_soon": {"movie"},
    "movie_hot": {"movie"},
    "movie_top250": {"movie"},
    "tv_hot": {"tv"},
    "tv_chinese_weekly": {"tv"},
    "tv_global_weekly": {"tv"},
}
_FALLBACK_ERRORS = (
    ProviderTimeout,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderInvalidResponse,
)
_DOUBAN_IMAGE_HOSTS = {
    "img1.doubanio.com",
    "img2.doubanio.com",
    "img3.doubanio.com",
    "img9.doubanio.com",
    "qnmob3.doubanio.com",
}
_EXTERNAL_ID_RE = re.compile(r"^[0-9]+$")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_TRUE_VALUES = {"1", "true", "yes", "on", "y"}


def _frodo_credential_identity(client: Any) -> str:
    return str(getattr(client, "_credential_identity", "") or "").strip()


def _begin_frodo_auth_attempt(client: Any) -> tuple[bool, bool]:
    """按凭据指纹领取进程级探测权，返回（已领取，是否等待过）。"""
    identity = _frodo_credential_identity(client)
    if not identity:
        return True, False
    waited = False
    with _FRODO_AUTH_STATE_CONDITION:
        while identity in _FRODO_AUTH_IN_FLIGHT:
            waited = True
            _FRODO_AUTH_STATE_CONDITION.wait()
        if identity in _FRODO_AUTH_DISABLED_CREDENTIALS:
            return False, waited
        _FRODO_AUTH_IN_FLIGHT.add(identity)
        return True, waited


def _finish_frodo_auth_attempt(client: Any) -> None:
    identity = _frodo_credential_identity(client)
    if not identity:
        return
    with _FRODO_AUTH_STATE_CONDITION:
        _FRODO_AUTH_IN_FLIGHT.discard(identity)
        _FRODO_AUTH_STATE_CONDITION.notify_all()


def _disable_frodo_auth_for_process(client: Any) -> None:
    identity = _frodo_credential_identity(client)
    if not identity:
        return
    with _FRODO_AUTH_STATE_CONDITION:
        _FRODO_AUTH_DISABLED_CREDENTIALS.add(identity)


def _safe_frodo_credential_source(client: Any) -> str:
    source = str(getattr(client, "credential_source", "") or "").strip()
    return source if source in _FRODO_CREDENTIAL_SOURCES else "unknown"


def _config_get(config: Mapping[str, Any] | None, key: str, default: str = "") -> str:
    if config is None:
        return str(app_config.get(key, default) or "")
    return str(config.get(key, default) or "")


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_VALUES


def _config_bool(config: Mapping[str, Any] | None, key: str, default: bool) -> bool:
    if config is None:
        return app_config.get_bool(key, default)
    return _bool_value(config.get(key), default)


def _image_key(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("large") or value.get("normal") or value.get("small")
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or host not in _DOUBAN_IMAGE_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    path = parsed.path.lstrip("/")
    if not path or ".." in path.split("/"):
        return ""
    return f"{host}/{path}"


def _score(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value") if value.get("value") is not None else value.get("score")
    try:
        score = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return score if score is not None and math.isfinite(score) else None


class DoubanProvider(DiscoveryProvider):
    name = "douban"

    def __init__(
        self,
        *,
        enabled: bool | str | int | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        dbcl2: str | None = None,
        base_url: str = "https://frodo.douban.com",
        session: requests.Session | Any | None = None,
        clock: Callable[[], float] | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        page_size: int = 20,
        config: Mapping[str, Any] | None = None,
        public_client: Any | None = None,
        frodo_client: Any | None = None,
        frodo_client_factory: Callable[[], Any] | None = None,
        authenticated_client: Any | None = None,
        authenticated_client_factory: Callable[[], Any] | None = None,
    ):
        self.enabled = (
            _config_bool(config, "DISCOVERY_DOUBAN_ENABLED", True)
            if enabled is None
            else _bool_value(enabled, True)
        )
        self._config = config
        self.api_key = None if api_key is None else str(api_key or "").strip()
        self.api_secret = None if api_secret is None else str(api_secret or "").strip()
        self._dbcl2_from_config = dbcl2 is None
        self.dbcl2 = "" if dbcl2 is None else normalize_dbcl2(dbcl2)

        # 保留旧构造参数/属性，网络主机仍由两个已批准客户端固定控制。
        self.base_url = str(base_url or "https://frodo.douban.com").rstrip("/")
        self.session = session or requests.Session()
        self.clock = clock or time.time
        self.timeout = timeout
        try:
            normalized_page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("page_size must be an integer") from exc
        self.page_size = max(1, min(normalized_page_size, 100))

        if public_client is None:
            public_kwargs: dict[str, Any] = {
                "session": self.session,
                "timeout": timeout,
                "page_size": self.page_size,
            }
            if clock is not None:
                public_kwargs["clock"] = clock
            self.public_client = DoubanPublicClient(**public_kwargs)
        else:
            self.public_client = public_client

        if frodo_client is not None and frodo_client_factory is not None:
            raise ValueError("frodo_client and frodo_client_factory are mutually exclusive")
        if authenticated_client is not None and authenticated_client_factory is not None:
            raise ValueError(
                "authenticated_client and authenticated_client_factory are mutually exclusive"
            )
        self._frodo_client = frodo_client
        self._frodo_client_factory = frodo_client_factory or self._build_frodo_client
        self._frodo_lock = threading.Lock()
        self._authenticated_client = authenticated_client
        self._authenticated_client_injected = authenticated_client is not None
        self._authenticated_client_factory = (
            authenticated_client_factory or self._build_authenticated_client
        )
        self._authenticated_lock = threading.Lock()

        self._breaker_lock = threading.Lock()
        self._fallback_condition = threading.Condition(self._breaker_lock)
        self._consecutive_failures = 0
        self._open_until = 0.0
        # 回退客户端各自独立熔断：kind -> (连续失败数, 冷却截止时间)。
        self._fallback_breakers: dict[str, tuple[int, float]] = {}
        # 熔断检查与实际网络请求之间也要单飞，避免并发首探重复请求/日志。
        self._fallback_in_flight: set[str] = set()

    def _load_dbcl2(self) -> None:
        """每次调用都对齐当前配置值：用户在设置中更新 Cookie 必须免重启生效。"""
        if not self._dbcl2_from_config:
            return
        try:
            current = normalize_dbcl2(_config_get(self._config, "DOUBAN_DBCL2"))
        except ValueError:
            current = ""
        if current != self.dbcl2:
            self.dbcl2 = current
            # 凭据变化后旧客户端与旧的认证熔断都不再有意义。
            self._authenticated_client = None
            self._record_fallback_success("dbcl2")

    def _build_frodo_client(self) -> DoubanFrodoClient:
        return DoubanFrodoClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            session=self.session,
            clock=self.clock,
            timeout=self.timeout,
            page_size=self.page_size,
        )

    def _build_authenticated_client(self) -> DoubanAuthenticatedClient:
        return DoubanAuthenticatedClient(
            dbcl2=self.dbcl2,
            clock=self.clock,
            timeout=self.timeout,
            page_size=self.page_size,
        )

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise ProviderNotConfigured("豆瓣探索 Provider 未启用")

    def _public_circuit_error(self) -> ProviderUnavailable | None:
        now = float(self.clock())
        with self._breaker_lock:
            if self._open_until > now:
                return ProviderUnavailable(
                    "豆瓣公共 Provider 熔断中",
                    retry_after=max(1, int(math.ceil(self._open_until - now))),
                )
            if self._open_until:
                self._open_until = 0.0
                self._consecutive_failures = 0
        return None

    def _record_public_failure(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._open_until = float(self.clock()) + 300.0

    @staticmethod
    def _counts_toward_public_breaker(error: ProviderError) -> bool:
        return not (
            isinstance(error, ProviderInvalidResponse)
            and getattr(error, "request_attempted", True) is False
        )

    def _record_public_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def _begin_fallback_attempt(self, kind: str) -> tuple[bool, bool, int]:
        """领取实例级探测权，必要时等待并继承刚完成探测的冷却状态。"""
        waited = False
        with self._fallback_condition:
            while kind in self._fallback_in_flight:
                waited = True
                self._fallback_condition.wait()

            now = float(self.clock())
            _failures, open_until = self._fallback_breakers.get(kind, (0, 0.0))
            if open_until > now:
                retry_after = (
                    0
                    if math.isinf(open_until)
                    else max(1, int(math.ceil(open_until - now)))
                )
                return False, waited, retry_after
            if open_until:
                self._fallback_breakers.pop(kind, None)
            self._fallback_in_flight.add(kind)
            return True, False, 0

    def _finish_fallback_attempt(self, kind: str) -> None:
        with self._fallback_condition:
            self._fallback_in_flight.discard(kind)
            self._fallback_condition.notify_all()

    def _inherit_process_frodo_disable(self) -> None:
        with self._breaker_lock:
            self._fallback_breakers["frodo"] = (
                0, _FRODO_AUTH_DISABLED_UNTIL_RESTART
            )

    def _record_fallback_failure(self, kind: str, error: Exception) -> None:
        now = float(self.clock())
        auth_failure = isinstance(error, ProviderAuthenticationError)
        with self._breaker_lock:
            failures, open_until = self._fallback_breakers.get(kind, (0, 0.0))
            # 只记录 closed -> open 的状态转换。并发中稍晚返回的失败不得重复告警。
            if open_until > now:
                return
            if auth_failure:
                cooldown = (
                    _FRODO_AUTH_DISABLED_UNTIL_RESTART
                    if kind == "frodo"
                    else _FALLBACK_AUTH_COOLDOWN_SECONDS
                )
                self._fallback_breakers[kind] = (0, now + cooldown)
            else:
                failures += 1
                if failures < _FALLBACK_FAILURE_THRESHOLD:
                    self._fallback_breakers[kind] = (failures, 0.0)
                    return
                cooldown = _FALLBACK_COOLDOWN_SECONDS
                self._fallback_breakers[kind] = (0, now + cooldown)

        if auth_failure and kind == "frodo":
            _disable_frodo_auth_for_process(self._frodo_client)
            source = _safe_frodo_credential_source(self._frodo_client)
            logger.warning(
                "豆瓣 frodo 回退认证失败，当前进程内已停用 "
                "reason=authentication credential_source=%s "
                "action=检查凭据覆盖、User-Agent和系统时间后重启",
                source,
            )
            return
        logger.warning(
            "豆瓣 %s 回退暂时停用 cooldown=%ss reason=%s",
            kind, int(cooldown), getattr(error, "code", "error"),
        )

    def _record_fallback_success(self, kind: str) -> None:
        with self._breaker_lock:
            self._fallback_breakers.pop(kind, None)

    def _attempt_fallback(
        self,
        kind: str,
        load_client: Callable[[], Any | None],
        fetch: Callable[[Any], Any],
        normalize: Callable[[Any], Any],
    ) -> tuple[Any, bool, int]:
        """执行一次受熔断和单飞保护的回退，返回结果、是否尝试及 retry_after。"""
        acquired, joined_attempt, retry_after = self._begin_fallback_attempt(kind)
        if not acquired:
            return _NO_FALLBACK_RESULT, joined_attempt, retry_after
        try:
            try:
                client = load_client()
            except ProviderError as exc:
                return _NO_FALLBACK_RESULT, True, exc.retry_after
            except Exception:
                return _NO_FALLBACK_RESULT, True, 0

            if client is None:
                return _NO_FALLBACK_RESULT, False, 0

            frodo_process_acquired = False
            if kind == "frodo":
                frodo_process_acquired, joined_process_attempt = (
                    _begin_frodo_auth_attempt(client)
                )
                if not frodo_process_acquired:
                    self._inherit_process_frodo_disable()
                    return _NO_FALLBACK_RESULT, joined_process_attempt, 0

            try:
                try:
                    result = normalize(fetch(client))
                except ProviderError as exc:
                    self._record_fallback_failure(kind, exc)
                    return _NO_FALLBACK_RESULT, True, exc.retry_after
                except Exception:
                    return _NO_FALLBACK_RESULT, True, 0

                self._record_fallback_success(kind)
                return result, True, 0
            finally:
                if frodo_process_acquired:
                    _finish_frodo_auth_attempt(client)
        finally:
            self._finish_fallback_attempt(kind)

    @staticmethod
    def _validate_list_request(
        category: str,
        media_type: str,
        page: int,
        filters: Mapping[str, Any] | None,
    ) -> tuple[str, str, int, dict[str, Any]]:
        normalized_category = str(category or "").strip().lower()
        normalized_type = str(media_type or "").strip().lower()
        allowed_media = _CATEGORY_MEDIA.get(normalized_category)
        if allowed_media is None:
            raise ProviderInvalidResponse("不支持的豆瓣分类")
        if normalized_type not in allowed_media:
            raise ProviderInvalidResponse("豆瓣分类与媒体类型不匹配")
        try:
            normalized_page = int(page)
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣页码无效") from exc
        if normalized_page < 1:
            raise ProviderInvalidResponse("豆瓣页码无效")
        if filters is None:
            normalized_filters: dict[str, Any] = {}
        elif isinstance(filters, Mapping):
            normalized_filters = dict(filters)
        else:
            raise ProviderInvalidResponse("豆瓣筛选参数无效")
        return normalized_category, normalized_type, normalized_page, normalized_filters

    @staticmethod
    def _validate_detail_request(external_id: str, media_type: str) -> tuple[str, str]:
        subject_id = str(external_id or "").strip()
        normalized_type = str(media_type or "").strip().lower()
        if not _EXTERNAL_ID_RE.fullmatch(subject_id):
            raise ProviderInvalidResponse("豆瓣条目 ID 无效")
        if normalized_type not in {"movie", "tv"}:
            raise ProviderInvalidResponse("豆瓣媒体类型无效")
        return subject_id, normalized_type

    def _frodo_fallback_client(self) -> Any | None:
        if self._frodo_client is not None:
            return self._frodo_client if bool(getattr(self._frodo_client, "configured", False)) else None
        if self.api_key is not None or self.api_secret is not None:
            if not (self.api_key and self.api_secret):
                return None
        with self._frodo_lock:
            if self._frodo_client is None:
                self._frodo_client = self._frodo_client_factory()
            client = self._frodo_client
        return client if bool(getattr(client, "configured", False)) else None

    def _authenticated_fallback_client(self) -> Any | None:
        if self._authenticated_client_injected:
            client = self._authenticated_client
            return client if bool(getattr(client, "configured", False)) else None
        with self._authenticated_lock:
            # 已缓存客户端也要复核配置：Cookie 更新后立即重建，免重启生效。
            self._load_dbcl2()
            if not self.dbcl2:
                return None
            if self._authenticated_client is None:
                self._authenticated_client = self._authenticated_client_factory()
            client = self._authenticated_client
        return client if bool(getattr(client, "configured", False)) else None

    @staticmethod
    def _dual_failure(retry_after: int = 0) -> ProviderUnavailable:
        error = ProviderUnavailable(
            "豆瓣可用数据源均不可用",
            retry_after=retry_after,
        )
        error.__cause__ = None
        error.__context__ = None
        error.__traceback__ = None
        return error

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: dict[str, Any] | None,
    ) -> DiscoveryPage:
        self._ensure_enabled()
        category, media_type, page, filters = self._validate_list_request(
            category, media_type, page, filters
        )

        public_error: ProviderError | None = self._public_circuit_error()
        if public_error is None:
            try:
                raw_page = self.public_client.list_items(category, media_type, page, filters)
                result = self._page(raw_page, page, media_type, source="public")
            except _FALLBACK_ERRORS as exc:
                public_error = exc
                if self._counts_toward_public_breaker(exc):
                    self._record_public_failure()
            else:
                self._record_public_success()
                return result

        fallback_retry_after = 0
        fallback_attempted = False

        result, attempted, retry_after = self._attempt_fallback(
            "frodo",
            self._frodo_fallback_client,
            lambda client: client.list_items(category, media_type, page, filters),
            lambda raw: self._page(raw, page, media_type, source="frodo-fallback"),
        )
        fallback_attempted |= attempted
        fallback_retry_after = max(fallback_retry_after, retry_after)
        if result is not _NO_FALLBACK_RESULT:
            return result

        result, attempted, retry_after = self._attempt_fallback(
            "dbcl2",
            self._authenticated_fallback_client,
            lambda client: client.list_items(category, media_type, page, filters),
            lambda raw: self._page(raw, page, media_type, source="dbcl2-fallback"),
        )
        fallback_attempted |= attempted
        fallback_retry_after = max(fallback_retry_after, retry_after)
        if result is not _NO_FALLBACK_RESULT:
            return result

        if fallback_attempted:
            raise self._dual_failure(fallback_retry_after) from None
        assert public_error is not None
        raise public_error.with_traceback(None)

    def get_detail(self, external_id: str, media_type: str) -> MediaCard:
        self._ensure_enabled()
        external_id, media_type = self._validate_detail_request(external_id, media_type)

        public_error: ProviderError | None = self._public_circuit_error()
        if public_error is None:
            try:
                raw = self.public_client.get_detail(external_id, media_type)
                card = self._required_card(raw, media_type)
            except _FALLBACK_ERRORS as exc:
                public_error = exc
                if self._counts_toward_public_breaker(exc):
                    self._record_public_failure()
            else:
                self._record_public_success()
                return card

        fallback_retry_after = 0
        fallback_attempted = False

        card, attempted, retry_after = self._attempt_fallback(
            "frodo",
            self._frodo_fallback_client,
            lambda client: client.get_detail(external_id, media_type),
            lambda raw: self._required_card(raw, media_type),
        )
        fallback_attempted |= attempted
        fallback_retry_after = max(fallback_retry_after, retry_after)
        if card is not _NO_FALLBACK_RESULT:
            return card

        card, attempted, retry_after = self._attempt_fallback(
            "dbcl2",
            self._authenticated_fallback_client,
            lambda client: client.get_detail(external_id, media_type),
            lambda raw: self._required_card(raw, media_type),
        )
        fallback_attempted |= attempted
        fallback_retry_after = max(fallback_retry_after, retry_after)
        if card is not _NO_FALLBACK_RESULT:
            return card

        if fallback_attempted:
            raise self._dual_failure(fallback_retry_after) from None
        assert public_error is not None
        raise public_error.with_traceback(None)

    def _page(
        self,
        raw_page: Any,
        page: int,
        fallback_type: str,
        *,
        source: str,
    ) -> DiscoveryPage:
        raw_items = getattr(raw_page, "items", None)
        if not isinstance(raw_items, (tuple, list)):
            raise ProviderInvalidResponse("豆瓣列表响应结构无效")
        cards = tuple(
            card
            for raw in raw_items
            if isinstance(raw, Mapping)
            if (card := self._card(raw, fallback_type)) is not None
        )
        upstream_source = str(getattr(raw_page, "source", "") or "").strip().lower()
        if (
            source == "public"
            and upstream_source in {"public-json", "public-html"}
            and page == 1
            and len(cards) < 3
        ):
            raise ProviderInvalidResponse("豆瓣公共首页结果不完整")
        return DiscoveryPage(
            items=cards,
            page=page,
            has_more=bool(getattr(raw_page, "has_more", False)),
            provider=ProviderHealth(
                name=self.name,
                status="healthy" if source == "public" else "degraded",
                message=source,
            ),
        )

    def _required_card(self, raw: Any, fallback_type: str) -> MediaCard:
        card = self._card(raw, fallback_type) if isinstance(raw, Mapping) else None
        if card is None:
            raise ProviderInvalidResponse("豆瓣详情响应结构无效")
        return card

    @staticmethod
    def _card(raw: Mapping[str, Any], fallback_type: str) -> MediaCard | None:
        external_id = str(raw.get("id") or raw.get("subject_id") or "").strip()
        title = str(raw.get("title") or raw.get("name") or "").strip()
        if not _EXTERNAL_ID_RE.fullmatch(external_id) or not title:
            return None
        raw_type = str(raw.get("media_type") or raw.get("type") or fallback_type).strip().lower()
        media_type = raw_type if raw_type in {"movie", "tv"} else fallback_type
        release_date = str(raw.get("release_date") or raw.get("date") or "").strip()
        explicit_year = str(raw.get("year") or "").strip()
        year_match = _YEAR_RE.search(
            explicit_year or release_date or str(raw.get("card_subtitle") or "")
        )
        return MediaCard(
            provider="douban",
            external_id=external_id,
            media_type=media_type,
            title=title,
            original_title=str(
                raw.get("original_title") or raw.get("original_name") or ""
            ).strip(),
            year=year_match.group(0) if year_match else "",
            overview=str(
                raw.get("overview")
                or raw.get("intro")
                or raw.get("abstract")
                or raw.get("description")
                or ""
            ).strip(),
            poster_key=_image_key(
                raw.get("poster_url")
                or raw.get("pic")
                or raw.get("cover")
                or raw.get("image")
            ),
            rating=_score(raw.get("rating")),
            rating_source="douban",
            release_date=release_date,
            douban_id=external_id,
        )
