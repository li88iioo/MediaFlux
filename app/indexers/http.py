from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urljoin

import httpx

from app.logger import get_logger

from .errors import IndexerInvalidResponse, IndexerResponseTooLarge, IndexerSecurityError

Resolver = Callable[[str, int], list[tuple]]
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
logger = get_logger(__name__)


def _is_asyncio_closed_loop_error(exc: RuntimeError) -> bool:
    """只识别 asyncio.BaseEventLoop._check_closed 产生的精确异常。"""
    if type(exc) is not RuntimeError or exc.args != ("Event loop is closed",):
        return False
    traceback = exc.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        filename = code.co_filename.replace("\\", "/")
        if code.co_name == "_check_closed" and filename.endswith(
            "/asyncio/base_events.py"
        ):
            return True
        traceback = traceback.tb_next
    return False


@dataclass(frozen=True, slots=True)
class IndexerHttpResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            name, separator, value = part.strip().partition("=")
            if separator and name.lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
                break
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class IndexerHttpStreamResponse:
    """受限响应流；只能在 FixedHostHttpClient 的上下文中消费一次。"""

    url: str
    status_code: int
    headers: dict[str, str]
    _response: httpx.Response
    _max_response_bytes: int
    _consumed: bool = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._consumed:
            raise RuntimeError("response stream has already been consumed")
        self._consumed = True
        total = 0
        async for chunk in self._response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise IndexerResponseTooLarge(
                    f"streamed response size exceeds {self._max_response_bytes}"
                )
            if chunk:
                yield chunk


class FixedHostHttpClient:
    """Bounded HTTP client that can only reach explicit public HTTPS hosts."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        timeout_seconds: float = 10,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 3,
        user_agent: str = "MediaFlux/1.0",
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        pin_resolved_address: bool = False,
    ):
        hosts = frozenset(str(host).strip().rstrip(".").lower() for host in allowed_hosts if str(host).strip())
        if not hosts:
            raise ValueError("allowed_hosts cannot be empty")
        if timeout_seconds <= 0 or max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("invalid HTTP bounds")
        self.allowed_hosts = hosts
        self.max_response_bytes = int(max_response_bytes)
        self.max_redirects = int(max_redirects)
        self._resolver = resolver or self._default_resolver
        self.pin_resolved_address = bool(pin_resolved_address)
        if transport is None:
            # 部分站点边缘节点（如 1lou 的 CDN）TCP 握手存在间歇性丢包，
            # 首连即卡满超时。retries 只重试连接建立阶段，请求一旦发出
            # 不会重放，对非幂等语义安全。
            transport = httpx.AsyncHTTPTransport(
                retries=2,
                # 多逻辑 Host 固定到同一 IP 时禁止跨 Host/SNI 复用；单 Host
                # 客户端可安全复用 TLS 连接，避免每次搜索重新握手。
                limits=httpx.Limits(max_keepalive_connections=0)
                if self.pin_resolved_address and len(self.allowed_hosts) > 1
                else httpx.Limits(),
            )
        # 单次尝试用一半预算：服务层以 timeout_seconds 为总信封，只有单次
        # 尝试更短，读超时后的 GET 整请求重试才有机会在信封内完成。
        per_attempt_seconds = max(2.0, float(timeout_seconds) / 2)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                per_attempt_seconds, connect=min(4.0, per_attempt_seconds)
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"},
        )

    @staticmethod
    def _default_resolver(host: str, port: int) -> list[tuple]:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except RuntimeError as exc:
            if not _is_asyncio_closed_loop_error(exc):
                raise
            # 旧 loop 已关闭后无法再调度 socket 回调；HTTPX 已先把 client
            # 标记为 closed。记录该降级，但不让应用/TestClient 停机失败。
            logger.warning("索引器 HTTP 客户端所属事件循环已关闭，跳过重复清理")

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> IndexerHttpResponse:
        try:
            return await self._request(
                "GET", url, params=params, headers=headers, max_redirects=max_redirects
            )
        except httpx.TimeoutException:
            # 部分站点边缘节点会偶发“握手成功但不回响应头”。GET 幂等，
            # 读超时整请求重试一次；POST 不重试，避免重复副作用。
            return await self._request(
                "GET", url, params=params, headers=headers, max_redirects=max_redirects
            )

    async def post_json(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> IndexerHttpResponse:
        return await self._request(
            "POST", url, json_body=dict(json), headers=headers, max_redirects=max_redirects
        )

    @asynccontextmanager
    async def stream_post_json(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> AsyncIterator[IndexerHttpStreamResponse]:
        """以与普通请求相同的 SSRF/DNS 约束暴露有界 POST 响应流。"""
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if redirect_limit < 0:
            raise ValueError("max_redirects cannot be negative")
        current = httpx.URL(url)
        current_json: Mapping[str, Any] | None = dict(json)

        for redirect_count in range(redirect_limit + 1):
            request_url, request_headers, extensions = await self._request_target(
                current, headers
            )
            async with self._client.stream(
                "POST",
                request_url,
                json=current_json,
                headers=request_headers,
                extensions=extensions,
                follow_redirects=False,
            ) as response:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise IndexerInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise IndexerSecurityError("redirect limit exceeded")
                    if response.status_code not in {307, 308}:
                        raise IndexerSecurityError("unsafe POST redirect")
                    current = httpx.URL(urljoin(str(current), location))
                    continue

                declared_size = self._content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.max_response_bytes:
                    raise IndexerResponseTooLarge(f"declared response size {declared_size}")
                yield IndexerHttpStreamResponse(
                    url=str(current),
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    _response=response,
                    _max_response_bytes=self.max_response_bytes,
                )
                return
        raise IndexerSecurityError("redirect limit exceeded")

    async def _request_target(
        self,
        current: httpx.URL,
        headers: Mapping[str, str] | None,
    ) -> tuple[httpx.URL, dict[str, str], dict[str, str] | None]:
        addresses = await asyncio.to_thread(self._validated_addresses, current)
        request_url = current
        request_headers = dict(headers or {})
        extensions = None
        if self.pin_resolved_address:
            request_url = current.copy_with(host=str(addresses[0]))
            request_headers["Host"] = current.host
            extensions = {"sni_hostname": current.host}
        return request_url, request_headers, extensions

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> IndexerHttpResponse:
        verb = str(method or "").upper()
        if verb not in {"GET", "POST"}:
            raise ValueError("unsupported HTTP method")
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if redirect_limit < 0:
            raise ValueError("max_redirects cannot be negative")
        current = httpx.URL(url)
        current_params = params
        current_json = json_body

        for redirect_count in range(redirect_limit + 1):
            request_url, request_headers, extensions = await self._request_target(
                current, headers
            )
            async with self._client.stream(
                verb,
                request_url,
                params=current_params,
                json=current_json,
                headers=request_headers,
                extensions=extensions,
                follow_redirects=False,
            ) as response:
                current_params = None
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise IndexerInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise IndexerSecurityError("redirect limit exceeded")
                    if verb == "POST" and response.status_code not in {307, 308}:
                        raise IndexerSecurityError("unsafe POST redirect")
                    current = httpx.URL(urljoin(str(current), location))
                    continue

                declared_size = self._content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.max_response_bytes:
                    raise IndexerResponseTooLarge(f"declared response size {declared_size}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise IndexerResponseTooLarge(
                            f"streamed response size exceeds {self.max_response_bytes}"
                        )
                return IndexerHttpResponse(
                    url=str(current),
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=bytes(body),
                )
        raise IndexerSecurityError("redirect limit exceeded")

    def _validate_url(self, url: httpx.URL) -> None:
        self._validated_addresses(url)

    def _validated_addresses(
        self, url: httpx.URL
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if url.scheme.lower() != "https":
            raise IndexerSecurityError("only HTTPS upstream URLs are allowed")
        if url.username or url.password:
            raise IndexerSecurityError("URL credentials are forbidden")
        host = (url.host or "").rstrip(".").lower()
        if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise IndexerSecurityError("localhost is forbidden")
        if host not in self.allowed_hosts:
            raise IndexerSecurityError("upstream host is not registered")
        port = url.port or 443
        if port != 443:
            raise IndexerSecurityError("non-standard upstream ports are forbidden")
        return self._validate_resolved_addresses(host, port)

    def _validate_resolved_addresses(
        self, host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = [literal] if literal is not None else self._resolve(host, port)
        if not addresses:
            raise IndexerSecurityError("upstream host did not resolve")
        for address in addresses:
            if not address.is_global:
                raise IndexerSecurityError("upstream host resolved to a non-public address")
        return sorted(addresses, key=lambda item: (item.version, str(item)))

    def _resolve(self, host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            records = self._resolver(host, port)
        except (OSError, socket.gaierror) as exc:
            raise IndexerSecurityError("upstream DNS resolution failed") from exc
        addresses = []
        for record in records:
            try:
                sockaddr = record[4]
                addresses.append(ipaddress.ip_address(sockaddr[0]))
            except (IndexError, TypeError, ValueError) as exc:
                raise IndexerSecurityError("upstream DNS returned an invalid address") from exc
        return addresses

    @staticmethod
    def _content_length(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            size = int(value)
        except (TypeError, ValueError) as exc:
            raise IndexerInvalidResponse("invalid Content-Length") from exc
        if size < 0:
            raise IndexerInvalidResponse("negative Content-Length")
        return size


class BrowserImpersonatingHttpClient:
    """Fixed-host curl_cffi client with persistent cookies and optional SNI/Host split."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        timeout_seconds: float = 15,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 3,
        resolver: Resolver | None = None,
        session_factory=None,
        sni_host: str = "",
        warmup_url: str = "",
        cookies: Mapping[str, str] | None = None,
    ):
        hosts = frozenset(str(host).strip().rstrip(".").lower() for host in allowed_hosts if str(host).strip())
        if not hosts or timeout_seconds <= 0 or max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("invalid browser HTTP client bounds")
        self.allowed_hosts = hosts
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.max_redirects = int(max_redirects)
        self._resolver = resolver or FixedHostHttpClient._default_resolver
        self.sni_host = str(sni_host or "").strip().rstrip(".").lower()
        if self.sni_host and self.sni_host not in hosts:
            raise ValueError("sni_host must be included in allowed_hosts")
        self.warmup_url = str(warmup_url or "").strip()
        self._cookies = {str(key): str(value) for key, value in dict(cookies or {}).items() if str(key)}
        self._session_factory = session_factory or self._default_session_factory
        self._session = None
        self._lock = asyncio.Lock()
        self._warmed = False

    @staticmethod
    def _default_session_factory():
        try:
            from curl_cffi.requests import Session
        except ImportError as exc:
            raise RuntimeError("curl_cffi is required for browser indexer transport") from exc
        return Session(trust_env=False)

    async def aclose(self) -> None:
        async with self._lock:
            session, self._session = self._session, None
            self._warmed = False
        if session is not None:
            await asyncio.to_thread(session.close)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> IndexerHttpResponse:
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if redirect_limit < 0:
            raise ValueError("max_redirects cannot be negative")
        current = httpx.URL(url)
        current_params = params
        async with self._lock:
            if self._session is None:
                self._session = self._session_factory()
                if self._cookies:
                    self._session.cookies.update(self._cookies)
            if self.warmup_url and not self._warmed:
                await asyncio.to_thread(self._request_once, httpx.URL(self.warmup_url), None, {"Referer": self.warmup_url})
                self._warmed = True
            for redirect_count in range(redirect_limit + 1):
                response = await asyncio.to_thread(self._request_once, current, current_params, headers)
                current_params = None
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise IndexerInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise IndexerSecurityError("redirect limit exceeded")
                    current = httpx.URL(urljoin(str(current), location))
                    continue
                return response
        raise IndexerSecurityError("redirect limit exceeded")

    def _request_once(self, logical_url: httpx.URL, params, headers) -> IndexerHttpResponse:
        logical_addresses = self._validated_addresses(logical_url)
        request_url = logical_url
        request_headers = dict(headers or {})
        if self.sni_host:
            request_headers.setdefault("Host", logical_url.host or "")
            request_url = logical_url.copy_with(host=self.sni_host)
            # 实际连接目标与逻辑 Host 必须遵守同一固定白名单和公网 DNS 契约。
            connect_addresses = self._validated_addresses(request_url)
        else:
            connect_addresses = logical_addresses
        request_headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        try:
            from curl_cffi.const import CurlOpt
            from curl_cffi.curl import CURL_WRITEFUNC_ERROR
        except ImportError as exc:  # pragma: no cover - 默认 session 已要求 curl_cffi
            raise RuntimeError("curl_cffi is required for browser indexer transport") from exc
        if not hasattr(self._session, "curl_options"):
            self._session.curl_options = {}
        previous_resolve = self._session.curl_options.get(CurlOpt.RESOLVE)
        connect_host = str(request_url.host or "")
        connect_address = str(connect_addresses[0])
        if ":" in connect_address:
            connect_address = f"[{connect_address}]"
        self._session.curl_options[CurlOpt.RESOLVE] = [
            f"{connect_host}:443:{connect_address}"
        ]
        body = bytearray()
        overflow = False

        def collect(chunk: bytes) -> int:
            nonlocal overflow
            if not chunk:
                return 0
            if len(body) + len(chunk) > self.max_response_bytes:
                overflow = True
                # CFFI callback 不得抛 Python 异常，否则会向 stderr 打印
                # “Exception ignored from cffi callback”。返回 libcurl 的写入
                # 中止 sentinel，再由调用边界翻译为领域异常。
                return CURL_WRITEFUNC_ERROR
            body.extend(chunk)
            return len(chunk)

        try:
            try:
                response = self._session.get(
                    str(request_url),
                    params=params,
                    headers=request_headers,
                    impersonate="chrome",
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    content_callback=collect,
                )
                if overflow:
                    raise IndexerResponseTooLarge(
                        f"response size exceeds {self.max_response_bytes}"
                    )
            except Exception as exc:
                if overflow:
                    raise IndexerResponseTooLarge(
                        f"response size exceeds {self.max_response_bytes}"
                    ) from exc
                raise
        finally:
            if previous_resolve is None:
                self._session.curl_options.pop(CurlOpt.RESOLVE, None)
            else:
                self._session.curl_options[CurlOpt.RESOLVE] = previous_resolve
        response_headers = {str(key).lower(): str(value) for key, value in dict(response.headers or {}).items()}
        bounded_body = bytes(body)
        # 测试替身或旧 curl_cffi 若未调用 content_callback，仍保持兼容；
        # 正常生产路径由 callback 在接收过程中实施硬上限。
        if not bounded_body:
            fallback_body = bytes(getattr(response, "content", b"") or b"")
            if len(fallback_body) > self.max_response_bytes:
                raise IndexerResponseTooLarge(
                    f"response size exceeds {self.max_response_bytes}"
                )
            bounded_body = fallback_body
        declared_size = FixedHostHttpClient._content_length(response_headers.get("content-length"))
        if declared_size is not None and declared_size > self.max_response_bytes:
            raise IndexerResponseTooLarge(f"declared response size {declared_size}")
        if len(bounded_body) > self.max_response_bytes:
            raise IndexerResponseTooLarge(f"response size exceeds {self.max_response_bytes}")
        return IndexerHttpResponse(
            url=str(logical_url),
            status_code=int(response.status_code),
            headers=response_headers,
            body=bounded_body,
        )

    def _validated_addresses(
        self, url: httpx.URL,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if url.scheme.lower() != "https" or url.username or url.password:
            raise IndexerSecurityError("only registered HTTPS URLs are allowed")
        host = (url.host or "").rstrip(".").lower()
        if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise IndexerSecurityError("localhost is forbidden")
        if host not in self.allowed_hosts or (url.port or 443) != 443:
            raise IndexerSecurityError("upstream host is not registered")
        try:
            records = self._resolver(host, 443)
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        except (OSError, socket.gaierror, IndexError, TypeError, ValueError) as exc:
            raise IndexerSecurityError("upstream DNS validation failed") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise IndexerSecurityError("upstream host resolved to a non-public address")
        return sorted(addresses, key=lambda address: (address.version, int(address)))

    def _validate_url(self, url: httpx.URL) -> None:
        self._validated_addresses(url)
