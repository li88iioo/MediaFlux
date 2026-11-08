from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from app.logger import get_logger

Resolver = Callable[[str, int], list[tuple]]
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
logger = get_logger(__name__)


class HttpError(Exception):
    """上游 HTTP 错误，公开代码与消息可直接交给调用边界。"""

    code = "http_error"
    default_public_message = "上游 HTTP 服务暂不可用"

    def __init__(self, detail: str = "", *, public_message: str | None = None):
        super().__init__(detail or self.default_public_message)
        self.detail = detail
        self.public_message = public_message or self.default_public_message


class HttpSecurityError(HttpError):
    code = "security_error"
    default_public_message = "上游地址未通过安全校验"


class HttpResponseTooLarge(HttpError):
    code = "response_too_large"
    default_public_message = "上游响应超过大小限制"


class HttpInvalidResponse(HttpError):
    code = "invalid_response"
    default_public_message = "上游返回了无法识别的数据"


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
class HttpResponse:
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


def _validate_response_encoding(response: httpx.Response, *, require_identity_encoding: bool) -> None:
    # 必须在流迭代/自动解压之前拒绝编码；重复头由 HTTPX 合并为逗号列表，同样拒绝。
    if (require_identity_encoding
            and response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}):
        raise HttpInvalidResponse("upstream Content-Encoding must be identity")


async def _bounded_response_bytes(
    response: httpx.Response, max_response_bytes: int, *, require_identity_encoding: bool,
) -> AsyncIterator[bytes]:
    _validate_response_encoding(response, require_identity_encoding=require_identity_encoding)
    if require_identity_encoding and response.is_stream_consumed:
        # 显式预缓冲 transport（包括 MockTransport）没有可再消费的 raw stream。
        # 只读已有 buffer，先检查完整长度；绝不调用 aread/aiter_bytes 重新解码。
        try:
            content = response.content
        except httpx.ResponseNotRead as exc:
            raise HttpInvalidResponse("consumed upstream response has no buffered body") from exc
        if len(content) > max_response_bytes:
            raise HttpResponseTooLarge(f"buffered response size exceeds {max_response_bytes}")
        for offset in range(0, len(content), 64 * 1024):
            yield content[offset:offset + 64 * 1024]
        return
    chunks = (response.aiter_raw(chunk_size=64 * 1024)
              if require_identity_encoding else response.aiter_bytes())
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_response_bytes:
            raise HttpResponseTooLarge(f"streamed response size exceeds {max_response_bytes}")
        if chunk:
            yield chunk


@dataclass(slots=True)
class HttpStreamResponse:
    """受限响应流；只能在 FixedHostHttpClient 的上下文中消费一次。"""

    url: str
    status_code: int
    headers: dict[str, str]
    _response: httpx.Response
    _max_response_bytes: int
    _consumed: bool = False
    _require_identity_encoding: bool = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._consumed:
            raise RuntimeError("response stream has already been consumed")
        self._consumed = True
        async for chunk in _bounded_response_bytes(
            self._response, self._max_response_bytes,
            require_identity_encoding=self._require_identity_encoding,
        ):
            yield chunk


class FixedHostHttpClient:
    """有界固定主机客户端；默认仅允许公网 HTTPS 标准端口。

    configured_origin 仅用于管理员配置的 Provider，锁定协议、主机和端口，
    允许该端点使用本机、内网或公网 HTTP(S)，不改变公共工具的默认边界。
    timeout_seconds 为客户端网络预算；启用 GET 超时重试时按尝试次数分配。
    POST 调用者可将 get_timeout_retries 设为 0，完整使用网络空闲预算。
    require_identity_encoding 为可选的读前编码限制，默认保持既有自动解压行为。
    """

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        timeout_seconds: float = 10,
        get_timeout_retries: int = 1,
        max_response_bytes: float = 2 * 1024 * 1024,
        max_redirects: float = 3,
        user_agent: str = "MediaFlux/1.0",
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        pin_resolved_address: bool = False,
        require_identity_encoding: bool = False,
        configured_origin: str | None = None,
    ):
        hosts = frozenset(host.strip().rstrip(".").lower() for host in allowed_hosts if host.strip())
        if not hosts:
            raise ValueError("allowed_hosts cannot be empty")
        if (timeout_seconds <= 0 or max_response_bytes <= 0 or max_redirects < 0
                or not isinstance(get_timeout_retries, int) or get_timeout_retries < 0):
            raise ValueError("invalid HTTP bounds")
        self.allowed_hosts = hosts
        self._configured_origin: tuple[str, str, int | None] | None = None
        if configured_origin is not None:
            origin = httpx.URL(configured_origin)
            origin_host = origin.host.rstrip(".").lower()
            if (origin.scheme not in {"http", "https"} or origin_host not in hosts
                    or origin.userinfo or origin.port == 0):
                raise ValueError("invalid configured upstream origin")
            self._configured_origin = (origin.scheme, origin_host, origin.port)
        self.max_response_bytes = int(max_response_bytes)
        self.max_redirects = int(max_redirects)
        self._resolver = resolver or self._default_resolver
        self.pin_resolved_address = pin_resolved_address
        self.require_identity_encoding = require_identity_encoding
        self.get_timeout_retries = get_timeout_retries
        if transport is None:
            # 部分站点边缘节点TCP 握手存在间歇性丢包，
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
        # GET 重试调用者在外层持有总预算。模型等 POST 调用者显式禁用
        # GET 重试后，配置值直接对应单次网络空闲超时。
        per_attempt_seconds = (
            max(2.0, float(timeout_seconds) / (get_timeout_retries + 1))
            if get_timeout_retries else float(timeout_seconds)
        )
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
            logger.warning("HTTP 客户端所属事件循环已关闭，跳过重复清理")

    def clear_cookies(self) -> None:
        """供不需要会话状态的公开元数据调用方清空服务端 Cookie。"""
        self._client.cookies.clear()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        attempt = 0
        while True:
            try:
                return await self._request(
                    "GET", url, params=params, headers=headers, max_redirects=max_redirects,
                    follow_redirects=follow_redirects,
                )
            except httpx.TimeoutException:
                if attempt == self.get_timeout_retries:
                    raise
                attempt += 1

    async def post_form(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """仅供站点搜索表单：允许标准 POST→GET 跳转，沿用逐跳主机校验。"""
        return await self._request(
            "POST", url, content=content,
            headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        )

    async def post_json(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> HttpResponse:
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
    ) -> AsyncGenerator[HttpStreamResponse, None]:
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
                _validate_response_encoding(response, require_identity_encoding=self.require_identity_encoding)
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise HttpInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise HttpSecurityError("redirect limit exceeded")
                    if response.status_code not in {307, 308}:
                        raise HttpSecurityError("unsafe POST redirect")
                    current = httpx.URL(urljoin(str(current), location))
                    continue

                declared_size = self._content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.max_response_bytes:
                    raise HttpResponseTooLarge(f"declared response size {declared_size}")
                yield HttpStreamResponse(
                    url=str(current),
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    _response=response,
                    _max_response_bytes=self.max_response_bytes,
                    _require_identity_encoding=self.require_identity_encoding,
                )
                return
        raise HttpSecurityError("redirect limit exceeded")

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
            request_headers["Host"] = current.netloc.decode("ascii")
            extensions = {"sni_hostname": current.host}
        return request_url, request_headers, extensions

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        verb = (method or "").upper()
        if verb not in {"GET", "POST"}:
            raise ValueError("unsupported HTTP method")
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if redirect_limit < 0:
            raise ValueError("max_redirects cannot be negative")
        current = httpx.URL(url)
        current_params = params
        current_json = json_body
        current_content = content

        for redirect_count in range(redirect_limit + 1):
            request_url, request_headers, extensions = await self._request_target(
                current, headers
            )
            async with self._client.stream(
                verb,
                request_url,
                params=current_params,
                json=current_json,
                content=current_content,
                headers=request_headers,
                extensions=extensions,
                follow_redirects=False,
            ) as response:
                _validate_response_encoding(response, require_identity_encoding=self.require_identity_encoding)
                current_params = None
                if follow_redirects and response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise HttpInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise HttpSecurityError("redirect limit exceeded")
                    if verb == "POST" and response.status_code not in {307, 308}:
                        if content is None:
                            raise HttpSecurityError("unsafe POST redirect")
                        verb, current_content, current_json = "GET", None, None
                        headers = {
                            key: value for key, value in (headers or {}).items()
                            if key.lower() not in {"content-type", "content-length"}
                        }
                    current = httpx.URL(urljoin(str(current), location))
                    continue

                declared_size = self._content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.max_response_bytes:
                    raise HttpResponseTooLarge(f"declared response size {declared_size}")
                body = bytearray()
                async for chunk in _bounded_response_bytes(
                    response, self.max_response_bytes,
                    require_identity_encoding=self.require_identity_encoding,
                ):
                    body.extend(chunk)
                return HttpResponse(
                    url=str(current),
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=bytes(body),
                )
        raise HttpSecurityError("redirect limit exceeded")

    def _validate_url(self, url: httpx.URL) -> None:
        self._validated_addresses(url)

    def _validated_addresses(
        self, url: httpx.URL
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if url.userinfo:
            raise HttpSecurityError("URL credentials are forbidden")
        host = (url.host or "").rstrip(".").lower()
        if host not in self.allowed_hosts:
            raise HttpSecurityError("upstream host is not registered")
        if self._configured_origin is not None:
            if (url.scheme, host, url.port) != self._configured_origin:
                raise HttpSecurityError("upstream origin differs from configured endpoint")
        else:
            if url.scheme.lower() != "https":
                raise HttpSecurityError("only HTTPS upstream URLs are allowed")
            if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
                raise HttpSecurityError("localhost is forbidden")
            if url.port not in {None, 443}:
                raise HttpSecurityError("non-standard upstream ports are forbidden")
        port = url.port or (443 if url.scheme == "https" else 80)
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
            raise HttpSecurityError("upstream host did not resolve")
        for address in addresses:
            if self._configured_origin is None and not address.is_global:
                raise HttpSecurityError("upstream host resolved to a non-public address")
        return sorted(addresses, key=lambda item: (item.version, str(item)))

    def _resolve(self, host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            records = self._resolver(host, port)
        except OSError as exc:
            raise HttpSecurityError("upstream DNS resolution failed") from exc
        addresses = []
        for record in records:
            try:
                sockaddr = record[4]
                addresses.append(ipaddress.ip_address(sockaddr[0]))
            except (IndexError, TypeError, ValueError) as exc:
                raise HttpSecurityError("upstream DNS returned an invalid address") from exc
        return addresses

    @staticmethod
    def _content_length(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            size = int(value)
        except (TypeError, ValueError) as exc:
            raise HttpInvalidResponse("invalid Content-Length") from exc
        if size < 0:
            raise HttpInvalidResponse("negative Content-Length")
        return size


class BrowserImpersonatingHttpClient:
    """Fixed-host curl_cffi client with persistent cookies and optional SNI/Host split."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        timeout_seconds: float = 15,
        max_response_bytes: float = 2 * 1024 * 1024,
        max_redirects: float = 3,
        resolver: Resolver | None = None,
        session_factory=None,
        sni_host: str = "",
        warmup_url: str = "",
        cookies: Mapping[str, str] | None = None,
    ):
        hosts = frozenset(host.strip().rstrip(".").lower() for host in allowed_hosts if host.strip())
        if not hosts or timeout_seconds <= 0 or max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("invalid browser HTTP client bounds")
        self.allowed_hosts = hosts
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.max_redirects = int(max_redirects)
        self._resolver = resolver or FixedHostHttpClient._default_resolver
        self.sni_host = (sni_host or "").strip().rstrip(".").lower()
        if self.sni_host and self.sni_host not in hosts:
            raise ValueError("sni_host must be included in allowed_hosts")
        self.warmup_url = (warmup_url or "").strip()
        self._cookies = {key: value for key, value in dict(cookies or {}).items() if key}
        self._session_factory = session_factory or self._default_session_factory
        self._session = None
        # 互斥覆盖真实的同步会话生命周期，而不是可提前取消的异步等待。
        self._lock = threading.Lock()
        self._warmed = False

    @staticmethod
    def _default_session_factory():
        try:
            from curl_cffi.requests import Session
        except ImportError as exc:
            raise RuntimeError("curl_cffi is required for browser indexer transport") from exc
        return Session(trust_env=False)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            session = self._session
            if session is None:
                self._warmed = False
                return
            # close 失败时保留同一个 session，供 IndexerService 的下一轮
            # aclose 真正重试；关闭期间继续持锁，禁止并发 get 创建替代会话。
            session.close()
            self._session = None
            self._warmed = False

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int | None = None,
    ) -> HttpResponse:
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if redirect_limit < 0:
            raise ValueError("max_redirects cannot be negative")
        current = httpx.URL(url)
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(
                self._get, current, params, headers, redirect_limit, cancelled,
            )
        finally:
            cancelled.set()

    def _get(
        self,
        current: httpx.URL,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        redirect_limit: int,
        cancelled: threading.Event,
    ) -> HttpResponse:
        current_params = params
        with self._lock:
            if cancelled.is_set():
                raise asyncio.CancelledError
            if self._session is None:
                self._session = self._session_factory()
                if self._cookies:
                    self._session.cookies.update(self._cookies)
            if self.warmup_url and not self._warmed:
                self._request_once(httpx.URL(self.warmup_url), None, {"Referer": self.warmup_url})
                self._warmed = True
            for redirect_count in range(redirect_limit + 1):
                if cancelled.is_set():
                    raise asyncio.CancelledError
                response = self._request_once(current, current_params, headers)
                current_params = None
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise HttpInvalidResponse("redirect response omitted Location")
                    if redirect_count >= redirect_limit:
                        raise HttpSecurityError("redirect limit exceeded")
                    current = httpx.URL(urljoin(str(current), location))
                    continue
                return response
        raise HttpSecurityError("redirect limit exceeded")

    def _request_once(self, logical_url: httpx.URL, params, headers) -> HttpResponse:
        session = self._session
        assert session is not None
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
        if not hasattr(session, "curl_options"):
            session.curl_options = {}
        previous_resolve = session.curl_options.get(CurlOpt.RESOLVE)
        connect_host = (request_url.host or "")
        connect_address = str(connect_addresses[0])
        if ":" in connect_address:
            connect_address = f"[{connect_address}]"
        session.curl_options[CurlOpt.RESOLVE] = [
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
                response = session.get(
                    str(request_url),
                    params=params,
                    headers=request_headers,
                    impersonate="chrome",
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    content_callback=collect,
                )
                if overflow:
                    raise HttpResponseTooLarge(
                        f"response size exceeds {self.max_response_bytes}"
                    )
            except Exception as exc:
                if overflow:
                    raise HttpResponseTooLarge(
                        f"response size exceeds {self.max_response_bytes}"
                    ) from exc
                raise
        finally:
            if previous_resolve is None:
                session.curl_options.pop(CurlOpt.RESOLVE, None)
            else:
                session.curl_options[CurlOpt.RESOLVE] = previous_resolve
        response_headers = {str(key).lower(): str(value) for key, value in dict(response.headers or {}).items()}
        bounded_body = bytes(body)
        # 测试替身或旧 curl_cffi 若未调用 content_callback，仍保持兼容；
        # 正常生产路径由 callback 在接收过程中实施硬上限。
        if not bounded_body:
            fallback_body = bytes(getattr(response, "content", b"") or b"")
            if len(fallback_body) > self.max_response_bytes:
                raise HttpResponseTooLarge(
                    f"response size exceeds {self.max_response_bytes}"
                )
            bounded_body = fallback_body
        declared_size = FixedHostHttpClient._content_length(response_headers.get("content-length"))
        if declared_size is not None and declared_size > self.max_response_bytes:
            raise HttpResponseTooLarge(f"declared response size {declared_size}")
        return HttpResponse(
            url=str(logical_url),
            status_code=int(response.status_code),
            headers=response_headers,
            body=bounded_body,
        )

    def _validated_addresses(
        self, url: httpx.URL,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if url.scheme.lower() != "https" or url.username or url.password:
            raise HttpSecurityError("only registered HTTPS URLs are allowed")
        host = (url.host or "").rstrip(".").lower()
        if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise HttpSecurityError("localhost is forbidden")
        if host not in self.allowed_hosts or (url.port or 443) != 443:
            raise HttpSecurityError("upstream host is not registered")
        try:
            records = self._resolver(host, 443)
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        except (OSError, IndexError, TypeError, ValueError) as exc:
            raise HttpSecurityError("upstream DNS validation failed") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise HttpSecurityError("upstream host resolved to a non-public address")
        return sorted(addresses, key=lambda address: (address.version, int(address)))

    def _validate_url(self, url: httpx.URL) -> None:
        self._validated_addresses(url)
