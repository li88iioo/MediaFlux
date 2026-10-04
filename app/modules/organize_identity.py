"""整理目录/文件名的纯标题身份证据提取。

仅规范化本次输入，不读写缓存、不发起识别请求；证据采纳与身份确认仍由
Organizer 的规划流程负责，不能把标题提示单独视为可自动写入的证明。
"""
from __future__ import annotations

import re
import unicodedata

from app.modules.scraper import _low_information_query, extract_recognition_context
from app.modules.special_media import (
    is_special_media_name, strip_special_media_markers, title_hint_from_path,
)


_MEDIA_IDENTITY_SEPARATORS_RE = re.compile(
    r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff"
    r"\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]+"
)
_DIRECTORY_TITLE_SEGMENT_RE = re.compile(
    r"[\[【(（]([^\]】)）]{1,160})[\]】)）]"
)
_MEDIA_IDENTITY_TOKEN_RE = re.compile(
    r"[a-z0-9]+|[\u3040-\u30ff]+|[\u3400-\u9fff]+|"
    r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]+",
    re.IGNORECASE,
)


def _normalize_media_identity(value: object) -> str:
    """生成跨目录缓存与剧集证据共用的稳定媒体标题身份。"""
    normalized = unicodedata.normalize("NFC", str(value or ""))
    return _MEDIA_IDENTITY_SEPARATORS_RE.sub("", normalized.casefold())


_GENERIC_FILENAME_IDENTITY_HINTS = {
    "anime", "episode", "episodes", "ep", "file", "movie", "season",
    "show", "tv", "unknown", "video",
}
_SPECIAL_FILENAME_IDENTITY_MARKER_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:"
    r"s\d{1,3}[ ._-]*e(?:p)?[ ._-]*0{1,3}|"
    r"s0{1,3}[ ._-]*e(?:p)?[ ._-]*\d{1,3}|"
    r"nc(?:op|ed)(?:[ ._-]*\d{1,3})?|"
    r"(?:pv|cm|promo|trailer)(?:[ ._-]*\d{1,3})?|"
    r"mini[ ._-]*(?:animations?|anime)(?:[ ._-]*\d{1,3})?|"
    r"(?:ova|oav|oad|specials?|sps?|omnibus)(?:[ ._-]*\d{1,3})?|"
    r"(?:op|ed)(?:[ ._-]*\d{1,3})?"
    r")(?![a-z0-9])"
)


def _usable_filename_identity_hint(filename: str) -> str:
    """提取可支撑目录级连续剧集包识别的文件名标题。

    目录名常混入发布组、编码组或打包者标签。只有目录已经形成连续剧集
    证据时才会调用本函数。沿用识别器的信息量规则，保留短中文作品名；
    极短、纯数字和通用占位标题仍失败关闭，回退到路径标题。
    """
    context = extract_recognition_context(str(filename or ""), "")
    title = str(context.filename_title or context.normalized_title or "").strip()
    identity = _normalize_media_identity(title)
    if (
        _low_information_query(identity)
        or identity.isdigit()
        or identity in _GENERIC_FILENAME_IDENTITY_HINTS
        or not title_hint_from_path(title)
    ):
        return ""
    return title


def _special_filename_identity_hint(filename: str) -> str:
    """提取特殊集文件自身携带的作品标题，失败时由调用方回退父目录。

    ``S00E01``、``OVA``、``NCOP`` 等词只描述特殊集位置，不属于作品名。
    仅当文件名本身明确带有特殊集标记时调用本函数；去掉标记后若只剩
    空值或通用词则失败关闭，避免把裸 ``NCOP.mkv`` 当作作品标题搜索。
    """
    if not is_special_media_name(filename):
        return ""
    stem = str(filename or "").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    cleaned = strip_special_media_markers(stem)
    cleaned = _SPECIAL_FILENAME_IDENTITY_MARKER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[ ._\-]+", " ", cleaned).strip(" []()【】._-")
    if not cleaned:
        return ""
    return _usable_filename_identity_hint(f"{cleaned}.mkv")


def _recognition_identity_year(filename: str, parent_context: str) -> str:
    """返回来源文件/目录中经解析器确认的可靠年份。

    目录级剧集包会把多个文件折叠成同一个身份识别请求；该请求必须保留
    ``2008`` 之类的作品/季年份，否则精确官方别名可能重新落入多候选歧义。
    这里只接受解析器产出的 19xx/20xx 四位年份，不直接扫描原始数字，避免
    把 1080、2160、集号或版本号带入识别标题。
    """
    context = extract_recognition_context(filename, parent_context)
    for value in (context.filename_year, context.folder_year):
        year = str(value or "").strip()
        if re.fullmatch(r"(?:19|20)\d{2}", year):
            return year
    return ""


def _directory_episode_identity_hint(filename: str, parent_context: str) -> str:
    """为连续剧集包选择与短文件标题兼容的更完整目录标题。

    例如文件只有 ``Boruto - 001``，而目录包含
    ``(Boruto: Naruto Next Generations)``。只有目录候选是文件标题的严格、
    有信息量扩展时才采用；否则保持文件名标题，避免无关父目录污染识别。
    """
    file_title = _usable_filename_identity_hint(filename)
    if not file_title:
        return ""
    file_identity = _normalize_media_identity(file_title)
    file_tokens = _MEDIA_IDENTITY_TOKEN_RE.findall(
        unicodedata.normalize("NFKC", file_title).casefold()
    )

    context = extract_recognition_context(filename, parent_context)
    raw_candidates = [str(context.folder_title or "").strip()]
    raw_candidates.extend(
        match.group(1).strip()
        for match in _DIRECTORY_TITLE_SEGMENT_RE.finditer(str(parent_context or ""))
    )

    compatible: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        parsed = extract_recognition_context(
            f"{raw_candidate}.S01E01.mkv", ""
        )
        candidate = str(
            parsed.filename_title or parsed.normalized_title or raw_candidate
        ).strip()
        candidate_identity = _normalize_media_identity(candidate)
        if (
            not candidate
            or candidate_identity in seen
            or candidate_identity == file_identity
            or not candidate_identity.startswith(file_identity)
        ):
            continue
        seen.add(candidate_identity)
        extra_identity = candidate_identity[len(file_identity):]
        if len(extra_identity) < 6:
            continue

        candidate_tokens = _MEDIA_IDENTITY_TOKEN_RE.findall(
            unicodedata.normalize("NFKC", candidate).casefold()
        )
        token_extension = bool(
            file_tokens
            and candidate_tokens[:len(file_tokens)] == file_tokens
            and len(candidate_tokens) >= len(file_tokens) + 2
        )
        cjk_extension = bool(
            re.search(r"[\u3040-\u30ff\u3400-\u9fff]", file_title)
            and len(extra_identity) >= 6
        )
        if token_extension or cjk_extension:
            compatible.append((len(candidate_identity), candidate))

    if not compatible:
        return file_title
    compatible.sort(key=lambda item: (item[0], item[1].casefold()))
    return compatible[0][1]
