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

def _split_title_variants(title: str) -> list[str]:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        return []
    explicit_parts = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"[|｜]", title)
        if part.strip()
    ]
    variants: list[str] = [title, *explicit_parts]
    for part in explicit_parts or [title]:
        cjk = " ".join(re.findall(r"[\u3400-\u9fff]+", part))
        # 日文混合标题不能再拆出单个汉字变体（如“僕”）；否则它会与大量
        # 无关日文作品形成假精确匹配。完整日文标题本身已经在首个变体中。
        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", part):
            cjk = ""
        latin = " ".join(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", part))
        # 中日韩标题后的季名片段（如 “2nd Attack”）不是独立英文片名；
        # 单独搜索会把它误命中 Art Attack 等无关条目。
        if cjk:
            latin_key = re.sub(r"[^A-Za-z0-9]", "", latin)
            if (
                re.fullmatch(r"(?i)\d{1,2}(?:st|nd|rd|th)\s+[^\s]+", latin)
                or latin_key.isdigit()
                or len(latin_key) < 3
            ):
                latin = ""
        variants.extend((cjk, latin))
    return _unique_text(variants)
