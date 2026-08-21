"""可选的成人媒体番号识别与 MetaTube 元数据客户端。

该模块只在明确识别到高置信番号时访问用户配置的 MetaTube 服务。普通影视名称
不会发送到成人元数据服务，网络错误也不会阻断既有 TMDB 识别链路。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from app.logger import get_logger
from app.modules.scraper import Candidate, MatchResult

logger = get_logger(__name__)

_DOMAIN_TOKEN_RE = re.compile(
    r"(?i)(?<![\w@])(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|tv|cc|me|cn|xyz|site|club|info|top|vip|pro|io)"
    r"(?:[/\\][^\s\[\]【】()]*)?"
)
_FC2_RE = re.compile(r"(?i)(?<![A-Z0-9])FC2[\s._-]*(?:PPV[\s._-]*)?(\d{5,9})(?!\d)")
_NAMED_PROVIDER_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(HEYZO|HEYDOUGA)[\s._-]*(\d{3,6})(?!\d)"
)
_DATE_PROVIDER_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(1PONDO|CARIB(?:BEANCOM)?|PACOPACOMAMA|10MUSUME)"
    r"[\s._-]*(\d{6,8})[\s._-]+(\d{2,4})(?!\d)"
)
_GENERIC_CODE_RE = re.compile(
    r"(?i)(?<![A-Z0-9])([A-Z]{2,8}[A-Z0-9]{0,4})[\s._-]+(\d{3,7})(?!\d)"
)
_COMPACT_CODE_RE = re.compile(
    r"(?i)(?<![A-Z0-9])([A-Z]{2,7})(\d{3,5})(?![A-Z0-9])"
)
_CODE_PREFIX_BLOCKLIST = {
    "AAC", "AC3", "AV1", "AVC", "BDMV", "CBR", "DDP", "DTS", "DV", "DVD",
    "EP", "EPISODE", "FILM", "FLAC", "H264", "H265", "HDR", "HEVC", "MOVIE",
    "MP3", "MP4", "MPEG", "REMUX", "SAMPLE", "SDR", "SEASON", "TRUEHD", "UHD",
    "VBR", "VIDEO", "WEB", "WEBRIP", "X264", "X265",
}
_RELEASE_NOISE_RE = re.compile(
    r"(?i)(?:\b(?:1080p|2160p|720p|4k|8k|bluray|blu-ray|webrip|web-dl|h\.?26[45]|"
    r"hevc|avc|x26[45]|aac|flac|dts(?:-?hd)?|ddp?\d(?:\.\d)?|hdr10\+?|dv)\b)"
)
_SAFE_CATEGORY_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,40}$")


@dataclass(frozen=True)
class NsfwIdentifier:
    code: str
    matched_text: str
    source: str
    confidence: float = 1.0


@dataclass(frozen=True)
class MetaTubeMetadata:
    provider: str
    external_id: str
    number: str
    title: str
    release_date: str = ""
    actors: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    maker: str = ""
    label: str = ""
    director: str = ""
    series: str = ""
    summary: str = ""
    score: float = 0.0
    cover_url: str = ""
    thumb_url: str = ""
    homepage: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def year(self) -> str:
        match = re.match(r"((?:19|20)\d{2})", self.release_date)
        return match.group(1) if match else ""

    @property
    def display_title(self) -> str:
        number = normalize_code(self.number)
        title = re.sub(r"\s+", " ", str(self.title or "")).strip()
        title_key = _code_comparison_key(title)
        if number and title and any(
            alias and alias in title_key for alias in _code_alias_keys(number)
        ):
            return title
        return " ".join(part for part in (number, title) if part).strip()


def normalize_code(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").upper()).strip("-")
    text = re.sub(r"-+", "-", text)
    compact = text.replace("-", "")
    if compact.startswith("FC2PPV"):
        digits = re.sub(r"\D", "", compact[6:])
        return f"FC2-PPV-{digits}" if digits else text
    if text.startswith("FC2-") and not text.startswith("FC2-PPV-"):
        digits = re.sub(r"\D", "", text[4:])
        return f"FC2-PPV-{digits}" if digits else text
    return text


def _code_comparison_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_code(value))


def _code_alias_keys(number: str) -> tuple[str, ...]:
    """返回常见番号写法的紧凑比较键，避免标题重复追加番号。"""
    key = _code_comparison_key(number)
    aliases = [key] if key else []
    for prefix in ("1PONDO", "CARIB", "CARIBBEANCOM", "10MUSUME", "PACOPACOMAMA"):
        if key.startswith(prefix):
            remainder = key[len(prefix):]
            if len(remainder) >= 6:
                aliases.append(remainder)
            break
    return tuple(dict.fromkeys(aliases))


def clean_nsfw_release_text(value: str, strip_domains: str = "") -> str:
    text = str(value or "")
    text = _DOMAIN_TOKEN_RE.sub(" ", text)
    configured = {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[,，\s]+", str(strip_domains or ""))
        if item.strip()
    }
    for domain in sorted(configured, key=len, reverse=True):
        if not re.fullmatch(r"[a-z0-9.-]{3,120}", domain):
            continue
        text = re.sub(
            rf"(?i)(?<![\w@])(?:https?://)?(?:www\.)?{re.escape(domain)}"
            rf"(?:[/\\][^\s\[\]【】()]*)?",
            " ", text,
        )
    text = _RELEASE_NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def extract_nsfw_identifier(value: str, strip_domains: str = "") -> NsfwIdentifier | None:
    text = clean_nsfw_release_text(value, strip_domains)
    match = _FC2_RE.search(text)
    if match:
        return NsfwIdentifier(f"FC2-PPV-{match.group(1)}", match.group(0), "fc2")
    match = _DATE_PROVIDER_RE.search(text)
    if match:
        provider = match.group(1).upper()
        code = f"{provider}-{match.group(2)}-{match.group(3)}"
        return NsfwIdentifier(code, match.group(0), "date-provider")
    match = _NAMED_PROVIDER_RE.search(text)
    if match:
        return NsfwIdentifier(
            f"{match.group(1).upper()}-{match.group(2)}",
            match.group(0),
            "named-provider",
        )
    for regex, source in ((_GENERIC_CODE_RE, "generic"), (_COMPACT_CODE_RE, "compact")):
        for match in regex.finditer(text):
            prefix = match.group(1).upper()
            number = match.group(2)
            if prefix in _CODE_PREFIX_BLOCKLIST:
                continue
            if prefix.startswith(("S", "E")) and len(prefix) <= 3:
                continue
            if len(number) == 4 and 1900 <= int(number) <= 2099:
                continue
            return NsfwIdentifier(f"{prefix}-{number}", match.group(0), source)
    return None


def validate_category_name(value: str) -> str:
    category = str(value or "成人内容").strip()
    if not _SAFE_CATEGORY_RE.fullmatch(category) or category in {".", ".."}:
        raise ValueError("成人内容分类名不能包含路径分隔符或非法字符")
    return category


class MetaTubeError(RuntimeError):
    """MetaTube 可恢复错误；调用方应回退既有识别链路。"""


class MetaTubeClient:
    def __init__(
        self,
        endpoint: str,
        token: str = "",
        *,
        timeout: float = 8.0,
        session: requests.Session | None = None,
    ) -> None:
        endpoint = str(endpoint or "").strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MetaTube 服务地址必须是有效的 HTTP/HTTPS URL")
        self.endpoint = endpoint
        self.token = str(token or "").strip()
        self.timeout = max(2.0, min(float(timeout or 8.0), 30.0))
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "MediaFlux/MetaTube"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.get(
                f"{self.endpoint}{path}",
                params=params or {},
                headers=self._headers(),
                timeout=self.timeout,
                allow_redirects=False,
            )
            if 300 <= int(getattr(response, "status_code", 0) or 0) < 400:
                raise MetaTubeError("MetaTube 服务不允许 HTTP 重定向")
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise MetaTubeError("MetaTube 请求超时") from exc
        except requests.RequestException as exc:
            raise MetaTubeError("MetaTube 服务暂时不可用") from exc
        except (TypeError, ValueError) as exc:
            raise MetaTubeError("MetaTube 返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise MetaTubeError("MetaTube 返回格式无效")
        error = payload.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MetaTubeError(str(message or "MetaTube 查询失败")[:160])
        if "data" not in payload:
            raise MetaTubeError("MetaTube 返回缺少 data 字段")
        return payload.get("data")

    @staticmethod
    def _metadata(item: dict[str, Any]) -> MetaTubeMetadata | None:
        if not isinstance(item, dict):
            return None

        def scalar(value: Any) -> str:
            if value is None or isinstance(value, (dict, list, tuple, set)):
                return ""
            return str(value).strip()

        def strings(value: Any) -> tuple[str, ...]:
            values = value if isinstance(value, (list, tuple, set)) else (value,)
            return tuple(text for raw in values if (text := scalar(raw)))

        def safe_float(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0

        provider = scalar(item.get("provider"))
        external_id = scalar(item.get("id"))
        number = normalize_code(scalar(item.get("number")) or external_id)
        title = re.sub(r"\s+", " ", scalar(item.get("title"))).strip()
        if not provider or not external_id or not number or not title:
            return None
        release = scalar(item.get("release_date"))
        if release:
            try:
                release = datetime.fromisoformat(release.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                release = release[:10]
        return MetaTubeMetadata(
            provider=provider,
            external_id=external_id,
            number=number,
            title=title,
            release_date=release,
            actors=strings(item.get("actors")),
            genres=strings(item.get("genres")),
            maker=scalar(item.get("maker")),
            label=scalar(item.get("label")),
            director=scalar(item.get("director")),
            series=scalar(item.get("series")),
            summary=scalar(item.get("summary")),
            score=safe_float(item.get("score")),
            cover_url=scalar(item.get("big_cover_url")) or scalar(item.get("cover_url")),
            thumb_url=scalar(item.get("big_thumb_url")) or scalar(item.get("thumb_url")),
            homepage=scalar(item.get("homepage")),
            raw=dict(item),
        )

    def search_exact(self, code: str) -> list[MetaTubeMetadata]:
        normalized = normalize_code(code)
        data = self._get(
            "/v1/movies/search",
            params={"q": normalized, "provider": "", "fallback": "true"},
        )
        if not isinstance(data, list):
            raise MetaTubeError("MetaTube 搜索结果格式无效")
        exact: list[MetaTubeMetadata] = []
        for item in data[:50]:
            metadata = self._metadata(item)
            if metadata and normalize_code(metadata.number) == normalized:
                exact.append(metadata)
        return sorted(exact, key=lambda item: (item.provider.casefold(), item.external_id))

    def get_movie(self, provider: str, external_id: str) -> MetaTubeMetadata:
        safe_provider = quote(str(provider or "").strip(), safe="")
        safe_id = quote(str(external_id or "").strip(), safe="")
        if not safe_provider or not safe_id:
            raise MetaTubeError("MetaTube 媒体身份不完整")
        data = self._get(f"/v1/movies/{safe_provider}/{safe_id}", params={"lazy": "true"})
        metadata = self._metadata(data) if isinstance(data, dict) else None
        if metadata is None:
            raise MetaTubeError("MetaTube 详情格式无效")
        return metadata


_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, list[MetaTubeMetadata] | None, str]] = {}
_INFLIGHT: dict[str, threading.Event] = {}
_CACHE_MAX = 256


def clear_nsfw_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        waiting = list(_INFLIGHT.values())
        _INFLIGHT.clear()
    for event in waiting:
        event.set()


def _cache_key(endpoint: str, token: str, code: str) -> str:
    scope = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:12]
    return f"{endpoint.rstrip('/')}|{scope}|{normalize_code(code)}"


def _cache_get(key: str) -> tuple[list[MetaTubeMetadata] | None, str] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is None:
            return None
        expires_at, value, error = item
        if expires_at <= now:
            _CACHE.pop(key, None)
            return None
        return value, error


def _cache_put(key: str, value: list[MetaTubeMetadata] | None, error: str, ttl: int) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic() + max(1, ttl), value, error)


class NsfwRecognizer:
    def __init__(
        self,
        endpoint: str,
        token: str = "",
        *,
        strip_domains: str = "",
        timeout: float = 8.0,
        session: requests.Session | None = None,
    ) -> None:
        self.client = MetaTubeClient(endpoint, token, timeout=timeout, session=session)
        self.strip_domains = str(strip_domains or "")

    def candidates(self, value: str) -> list[Candidate]:
        identifier = extract_nsfw_identifier(value, self.strip_domains)
        if identifier is None:
            return []
        key = _cache_key(self.client.endpoint, self.client.token, identifier.code)
        cached = _cache_get(key)
        if cached is None:
            with _CACHE_LOCK:
                cached = _cache_get(key)
                pending = _INFLIGHT.get(key)
                leader = cached is None and pending is None
                if leader:
                    pending = threading.Event()
                    _INFLIGHT[key] = pending
            if cached is None and not leader:
                assert pending is not None
                pending.wait(self.client.timeout + 1.0)
                cached = _cache_get(key)
                if cached is None:
                    return []
            if cached is None and leader:
                try:
                    try:
                        rows = self.client.search_exact(identifier.code)
                    except (MetaTubeError, TypeError, ValueError, OverflowError) as exc:
                        _cache_put(key, None, str(exc), 30)
                        logger.info(
                            "MetaTube 番号查询暂不可用 code=%s reason=%s",
                            identifier.code, str(exc),
                        )
                        return []
                    _cache_put(key, rows, "", 21_600 if rows else 120)
                finally:
                    with _CACHE_LOCK:
                        finished = _INFLIGHT.pop(key, None)
                    if finished is not None:
                        finished.set()
            else:
                rows, error = cached
                if error or rows is None:
                    return []
        else:
            rows, error = cached
            if error or rows is None:
                return []
        result: list[Candidate] = []
        for item in rows:
            result.append(Candidate(
                tmdb_id="",
                title=item.display_title,
                year=item.year,
                score=1.0,
                media_type="movie",
                original_title=item.title,
                overview=item.summary,
                poster_path=item.cover_url,
                release_date=item.release_date,
                provider="metatube",
                external_id=f"{item.provider}:{item.external_id}",
                metadata=dict(item.raw),
            ))
        return result

    def match(self, filename: str, parent_path: str = "") -> MatchResult | None:
        value = " ".join(part for part in (parent_path, filename) if part)
        candidates = self.candidates(value)
        if not candidates:
            return None
        candidate = candidates[0]
        ambiguous = len(candidates) > 1
        return MatchResult(
            tmdb_id="",
            title=candidate.title,
            year=candidate.year,
            media_type="movie",
            confidence=1.0,
            candidates=candidates,
            need_confirm=ambiguous,
            error="同一番号命中多个元数据来源，请人工选择" if ambiguous else "",
            status="ambiguous" if ambiguous else "matched",
            matched_by="metatube_exact",
            threshold=1.0,
            provider="metatube",
            external_id=candidate.external_id,
            metadata=dict(candidate.metadata),
        )

    def resolve(self, external_id: str) -> tuple[MatchResult, dict[str, Any]]:
        provider, separator, item_id = str(external_id or "").partition(":")
        if not separator or not provider or not item_id:
            raise MetaTubeError("MetaTube 媒体身份无效")
        item = self.client.get_movie(provider, item_id)
        detail = dict(item.raw)
        detail.update({
            "provider": item.provider,
            "id": item.external_id,
            "number": item.number,
            "title": item.display_title,
            "original_title": item.title,
            "release_date": item.release_date,
            "overview": item.summary,
            "genres": [{"name": genre} for genre in item.genres],
            "poster_url": item.cover_url,
            "backdrop_url": item.thumb_url,
            "homepage": item.homepage,
            "actors": list(item.actors),
            "maker": item.maker,
            "label": item.label,
            "director": item.director,
            "series": item.series,
        })
        match = MatchResult(
            title=item.display_title,
            year=item.year,
            media_type="movie",
            confidence=1.0,
            status="matched",
            matched_by="metatube_manual",
            threshold=1.0,
            provider="metatube",
            external_id=f"{item.provider}:{item.external_id}",
            metadata=detail,
        )
        return match, detail
