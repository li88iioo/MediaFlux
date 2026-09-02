"""发布名识别使用的纯文本归一化与候选清洗。"""
from __future__ import annotations

import re
import unicodedata


_MEDIA_FILE_SUFFIX = re.compile(
    r"(?i)\.(?:mkv|mp4|ts|m2ts|mts|avi|mov|m4v|webm|mpeg|mpg|wmv|flv|"
    r"vob|tp|f4v|rm|rmvb|nfo|srt|ass|ssa|sup|vtt|sub|idx|jpg|jpeg|png|webp)$"
)


def strip_media_file_suffix(value: str) -> str:
    """只剥离已知媒体或伴随文件扩展名，保留目录名中的普通点号。"""
    return _MEDIA_FILE_SUFFIX.sub("", str(value or ""))


def normalize_release_text(value: object, *, strip_chars: str = " ._-·") -> str:
    """统一发布名空白与边界分隔符，不删除任何语义词。"""
    return re.sub(r"\s+", " ", str(value or "")).strip(strip_chars)


def _unique_text(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_release_text(value)
        key = _comparison_key(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _comparison_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    # 保留汉字、日文假名与韩文/Jamo。只保留汉字会把日文标题压缩成少量
    # 共同汉字，造成不同作品在宽松模式下被误判为高度相似。
    return re.sub(
        r"[^a-z0-9\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff"
        r"\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+",
        " ",
        normalized.lower(),
    ).strip()


def _is_informative_slash_title_part(value: str) -> bool:
    """判断斜杠两侧是否都像完整片名，避免拆坏 ``Fate/stay night``。"""
    text = normalize_release_text(value, strip_chars=" ._-·/／")
    if not text:
        return False
    east_asian_chars = re.findall(
        r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff"
        r"\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]",
        text,
    )
    if len(east_asian_chars) >= 3:
        return True
    latin_words = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", text)
    latin_length = sum(len(re.sub(r"[^A-Za-z0-9]", "", word)) for word in latin_words)
    return len(latin_words) >= 2 and latin_length >= 6


def _split_title_variants(title: str) -> list[str]:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        return []
    pipe_parts = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"[|｜]", title)
        if part.strip()
    ]
    explicit_parts: list[str] = []
    for part in pipe_parts or [title]:
        slash_parts = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"[/／]", part)
            if item.strip()
        ]
        if 2 <= len(slash_parts) <= 4 and all(
            _is_informative_slash_title_part(item) for item in slash_parts
        ):
            explicit_parts.extend(slash_parts)
        else:
            explicit_parts.append(part)
    variants: list[str] = [title, *pipe_parts, *explicit_parts]
    for part in explicit_parts or [title]:
        cjk = " ".join(re.findall(r"[\u3400-\u9fff]+", part))
        # 日文混合标题不能再拆出单个汉字变体（如“僕”）；否则它会与大量
        # 无关日文作品形成假精确匹配。完整日文标题本身已经在首个变体中。
        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", part):
            cjk = ""
        latin = " ".join(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", part))
        # 中日韩标题后的季名片段（如 “2nd Attack”）不是独立英文片名；
        # 单独搜索会把它误命中 Art Attack 等无关条目。
        latin_key = re.sub(r"[^A-Za-z0-9]", "", latin)
        latin_alpha_words = re.findall(r"[A-Za-z]+", latin)
        numeric_roman_only = bool(
            re.search(r"\d", latin)
            and latin_alpha_words
            and all(
                re.fullmatch(r"(?i)[ivxlcdm]+", word)
                for word in latin_alpha_words
            )
        )
        if numeric_roman_only or (
            cjk
            and (
                re.fullmatch(r"(?i)\d{1,2}(?:st|nd|rd|th)\s+[^\s]+", latin)
                or latin_key.isdigit()
                or len(latin_key) < 3
            )
        ):
            latin = ""
        variants.extend((cjk, latin))
    return _unique_text(variants)
