"""探索海报同源代理；使用签名 opaque token 和固定 Provider 域名。"""
from __future__ import annotations

import logging
import random
import re
import threading
from urllib.parse import unquote, urlsplit

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer
from requests.adapters import HTTPAdapter

from app import config
from app.modules.web_secret import WebSecretUnavailable, get_web_secret
from app.logger import get_logger, log_throttled

logger = get_logger(__name__)

# ---------- 豆瓣 CDN 反爬伪装 ----------
# 豆瓣图片 CDN (doubanio.com) 对服务端代理有严格的防盗链/反爬策略：
#   - 检测 User-Agent 中的非浏览器标识 → 403
#   - 检测缺失的浏览器级 headers (Accept-Language, Sec-Fetch-*) → 403
#   - 部分 CDN 节点会 302 重定向到正确节点 → 需跟随
#   - 短时间高并发相同 IP → 429/502
# 下方策略：完整模拟浏览器请求行为，复用 Session 维持 TCP 连接池。

_BROWSER_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
)

_session_lock = threading.Lock()
_poster_session: requests.Session | None = None


def _get_poster_session() -> requests.Session:
    """复用 Session 以维持 TCP 连接池、减少握手开销并降低反爬特征。"""
    global _poster_session  # noqa: PLW0603
    if _poster_session is not None:
        return _poster_session
    with _session_lock:
        if _poster_session is not None:
            return _poster_session
        session = requests.Session()
        session.trust_env = False
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _poster_session = session
        return session


def close_poster_session() -> None:
    """关闭进程级海报连接池；重复调用安全，后续请求可按需重建。"""
    global _poster_session  # noqa: PLW0603
    with _session_lock:
        session = _poster_session
        _poster_session = None
    if session is None:
        return
    try:
        session.close()
    except Exception as exc:
        logger.warning("关闭探索海报 HTTP Session 失败 type=%s", type(exc).__name__)


def _require_discovery_enabled() -> None:
    if not config.get_bool("DISCOVERY_ENABLED", False):
        raise HTTPException(status_code=404, detail="not found")


router = APIRouter(dependencies=[Depends(_require_discovery_enabled)])
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_TOKEN_LENGTH = 2048
_SAFE_IMAGE_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/avif", "image/gif",
}
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._~!$&'()+,;=:@/-]+$")
_DOUBAN_IMAGE_HOSTS = {
    "img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com",
    "img9.doubanio.com", "qnmob3.doubanio.com",
}


def _matches_image_magic(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    if content_type == "image/avif":
        if len(content) < 12 or content[4:8] != b"ftyp":
            return False
        brands = {content[8:12]}
        brands.update(content[offset:offset + 4] for offset in range(16, min(len(content), 64), 4))
        return bool(brands & {b"avif", b"avis"})
    return False


def _serializer() -> URLSafeSerializer:
    try:
        secret = get_web_secret()
    except WebSecretUnavailable as exc:
        raise HTTPException(status_code=503, detail="poster signing unavailable") from exc
    return URLSafeSerializer(secret, salt="mediaflux-discovery-poster-v1")


def _canonical_poster_key(provider: str, poster_key: str) -> tuple[str, str]:
    provider = str(provider or "").strip().lower()
    raw = str(poster_key or "").strip().lstrip("/")
    decoded = unquote(raw)
    if (
        not raw or len(raw) > 1024 or raw != decoded or "%" in raw or "://" in raw
        or "@" in raw.split("/", 1)[0]
        or not _SAFE_PATH_RE.fullmatch(raw)
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise HTTPException(status_code=400, detail="invalid poster key")
    if provider == "tmdb":
        return provider, raw
    host, separator, path = raw.partition("/")
    host = host.lower()
    if provider == "douban":
        if not separator or host not in _DOUBAN_IMAGE_HOSTS or not path:
            raise HTTPException(status_code=400, detail="invalid douban poster key")
        return provider, f"{host}/{path}"
    if provider == "bangumi":
        if not separator or host != "lain.bgm.tv" or not path:
            raise HTTPException(status_code=400, detail="invalid bangumi poster key")
        return provider, f"{host}/{path}"
    raise HTTPException(status_code=404, detail="unknown poster provider")


def encode_poster_token(provider: str, poster_key: str) -> str:
    provider, key = _canonical_poster_key(provider, poster_key)
    return _serializer().dumps({"v": 1, "provider": provider, "key": key})


def decode_poster_token(provider: str, token: str) -> str:
    if not token or len(token) > _MAX_TOKEN_LENGTH or "/" in token:
        raise HTTPException(status_code=400, detail="invalid poster token")
    try:
        payload = _serializer().loads(token)
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail="invalid poster token") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HTTPException(status_code=400, detail="invalid poster token")
    signed_provider = str(payload.get("provider") or "")
    if signed_provider != str(provider or "").strip().lower():
        raise HTTPException(status_code=400, detail="poster provider mismatch")
    _, key = _canonical_poster_key(signed_provider, str(payload.get("key") or ""))
    return key


def _upstream_url(provider: str, poster_key: str) -> str:
    provider, key = _canonical_poster_key(provider, poster_key)
    if provider == "tmdb":
        return f"https://image.tmdb.org/t/p/w500/{key}"
    if provider == "douban":
        return f"https://{key}"
    if provider == "bangumi":
        return f"https://{key}"
    raise HTTPException(status_code=404, detail="unknown poster provider")


def _douban_headers() -> dict[str, str]:
    """构建完整的浏览器级请求头，绕过豆瓣 CDN 反爬检测。"""
    return {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Referer": "https://movie.douban.com/",
        "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "User-Agent": random.choice(_BROWSER_UA_POOL),
    }


def _default_headers() -> dict[str, str]:
    """非豆瓣源的通用图片请求头。"""
    ua = config.get("BANGUMI_USER_AGENT", "")
    if not ua:
        ua = random.choice(_BROWSER_UA_POOL)
    return {
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": ua,
    }


@router.get("/discovery-poster/{provider}/{token}", name="discovery_image.poster")
def poster(request: Request, provider: str, token: str):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=401, detail="unauthorized")
    poster_key = decode_poster_token(provider, token)
    url = _upstream_url(provider, poster_key)
    is_douban = str(provider or "").strip().lower() == "douban"
    headers = _douban_headers() if is_douban else _default_headers()

    # 豆瓣 CDN 有时尝试不同的图片主机可以绕过单节点限流
    alternate_urls: list[str] = []
    if is_douban:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        for alt_host in _DOUBAN_IMAGE_HOSTS:
            if alt_host != host:
                alt_url = url.replace(f"https://{host}/", f"https://{alt_host}/", 1)
                alternate_urls.append(alt_url)
        # 打乱备用 CDN 节点顺序，分散请求压力
        random.shuffle(alternate_urls)

    session = _get_poster_session()
    upstream = None
    try:
        last_status = None
        # 第 1 轮：请求原始 URL（最多重试 1 次）
        for attempt in range(2):
            try:
                upstream = session.get(
                    url, headers=headers, timeout=(5, 15), stream=True,
                    allow_redirects=is_douban,  # 豆瓣允许跟随 CDN 302 重定向
                )
                if upstream.status_code == 200:
                    break
                last_status = upstream.status_code
                if attempt == 0 and upstream.status_code in {403, 429, 500, 502, 503, 504}:
                    upstream.close()
                    upstream = None
                    # 重试前切换 UA 以降低指纹重复概率
                    if is_douban:
                        headers["User-Agent"] = random.choice(_BROWSER_UA_POOL)
                    continue
            except requests.RequestException as req_exc:
                if attempt == 0:
                    continue
                raise req_exc

        # 第 2 轮：如果原始主机 403/502，尝试备用 CDN 节点（仅豆瓣）
        if is_douban and (upstream is None or upstream.status_code != 200) and alternate_urls:
            for alt_url in alternate_urls[:2]:  # 最多尝试 2 个备用节点
                if upstream is not None:
                    upstream.close()
                    upstream = None
                headers["User-Agent"] = random.choice(_BROWSER_UA_POOL)
                try:
                    upstream = session.get(
                        alt_url, headers=headers, timeout=(5, 15), stream=True,
                        allow_redirects=True,
                    )
                    if upstream.status_code == 200:
                        break
                    last_status = upstream.status_code
                except requests.RequestException:
                    upstream = None
                    continue

        if upstream is None or upstream.status_code != 200:
            status_code = upstream.status_code if upstream else (last_status or "none")
            log_throttled(
                logger,
                logging.WARNING,
                f"discovery-image-status:{provider}:{status_code}",
                "探索海报代理上游异常 provider=%s status=%s",
                provider,
                status_code,
            )
            raise HTTPException(status_code=502, detail="upstream image failed")
        content_type = str(upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in _SAFE_IMAGE_MIME_TYPES:
            raise HTTPException(status_code=502, detail="invalid upstream content type")
        try:
            content_length = int(upstream.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=502, detail="upstream image too large")
        chunks: list[bytes] = []
        total = 0
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_IMAGE_BYTES:
                raise HTTPException(status_code=502, detail="upstream image too large")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not _matches_image_magic(content_type, content):
            raise HTTPException(status_code=502, detail="upstream image content mismatch")
        return Response(
            content=content, media_type=content_type,
            headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
        )
    except HTTPException:
        raise
    except requests.RequestException as exc:
        log_throttled(
            logger,
            logging.WARNING,
            f"discovery-image-error:{provider}:{type(exc).__name__}",
            "探索海报代理失败 provider=%s type=%s",
            provider,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="upstream image failed") from exc
    finally:
        if upstream is not None:
            upstream.close()
