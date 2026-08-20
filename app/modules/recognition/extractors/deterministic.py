"""无网络、无数据库副作用的确定性季集位置提取器。"""
from __future__ import annotations

import re

from app.modules.special_media import special_media_position



_ENGLISH_ORDINAL_SEASON_TOKEN = re.compile(
    r"(?i)\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth)[ ._-]+season\b"
)

_ENGLISH_ORDINAL_SEASONS = {
    word: index
    for index, word in enumerate((
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
        "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
        "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
        "nineteenth", "twentieth",
    ), start=1)
}

_ORDINAL_ATTACK_SEASON_TOKEN = re.compile(
    r"(?i)(?<!\d)(\d{1,2})(?:st|nd|rd|th)[ ._-]+attack"
    r"(?=\s*(?:[（(【\[].{0,48}[）)】\]])?\s*(?:-|e(?:p(?:isode)?)?|第|[【\[]|$))"
)

_SEASON_TOKEN = re.compile(
    r"(?i)(?:\bseason[ ._-]*|\bs)(\d{1,2})(?:\b|e\d{1,4}(?:v\d+)?)"
    r"|第\s*(\d{1,2})\s*季"
    r"|(?<!\d)(\d{1,2})(?:st|nd|rd|th)[ ._-]*season\b"
)

_BRACKET_TV_SEASON_TOKEN = re.compile(
    r"(?i)[\[【]\s*(?:tv|тв)\s*-\s*(?P<season>0[1-9]|[1-9]\d?)\s*[\]】]"
)

_EPISODE_TOKEN = re.compile(
    r"(?i)\bs\d{1,2}e(\d{1,4})(?:v\d+)?"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
    r"|(?:\be(?:p(?:isode)?)?[ ._-]*)(\d{1,4})(?:v\d+)?"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
    r"|第\s*(\d{1,3})\s*(?:集|[话話])"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
)

_CHINESE_EPISODE_TOKEN = re.compile(
    r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*(?:集|[话話])"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
)

_CHINESE_SEASON_TOKEN = re.compile(r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*季")

_SEASON_RANGE_TOKEN = re.compile(
    r"(?i)(?:\bs\d{1,2}\s*[-~～–—]\s*s\d{1,2}\b"
    r"|第\s*[零〇一二两三四五六七八九十\d]{1,3}\s*"
    r"[-~～–—]\s*[零〇一二两三四五六七八九十\d]{1,3}\s*季)"
)

_SPECIAL_EPISODE_TOKEN = re.compile(
    r"(?i)(?:^|[ ._\-\[\(【])(?:"
    r"ova(?:[ ._-]*(\d{1,3}))?"
    r"|oav(?:[ ._-]*(\d{1,3}))?"
    r"|oad(?:[ ._-]*(\d{1,3}))?"
    r"|sp(?:[ ._-]*(\d{1,3}))?"
    r"|specials?(?:[ ._-]*(\d{1,3}))?"
    r"|(?:特别篇|特別篇|番外篇?|特典)(?:[ ._-]*(\d{1,3}))?"
    r")(?=$|[ ._\-\]\)】])"
)

_BARE_EPISODE_SUFFIX = re.compile(
    r"(?i)(?:^|\s+-\s+|[._]+)(\d{1,4})(?:v\d+)?"
    r"(?:[._]+)?(?=\s*(?:[\[【(（]|$))"
)

_BRACKET_EPISODE_TOKEN = re.compile(r"(?i)[\[【(（]\s*(\d{1,4})(?:v\d+)?\s*[\]】)）]")

_RELEASE_X_POSITION = re.compile(
    r"(?i)(?<![A-Za-z0-9])(\d{1,2})x(\d{1,3})(?![A-Za-z0-9])"
)

_RELEASE_X_NUMERIC_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(\d{1,4})x(\d{1,4})(?![A-Za-z0-9])"
)

_RELEASE_X_ASPECT_RATIOS = frozenset({(4, 3), (16, 9), (21, 9), (32, 9)})

_UNLABELED_EPISODE_RESERVED_VALUES = {
    360, 480, 576, 720, 1080, 1440, 2160, 4320,
}



def _extract_number(pattern: re.Pattern, text: str) -> int | None:
    match = pattern.search(text or "")
    if not match:
        return None
    for value in match.groups():
        if value:
            return int(value)
    return None

def _chinese_number(value: str) -> int | None:
    """解析常见中文季号（零至九十九）；无法确认时返回 ``None``。"""
    text = str(value or "").strip().replace("两", "二").replace("〇", "零")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if not text:
        return None
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        number = tens * 10 + ones
    elif all(ch in digits for ch in text):
        number = 0
        for ch in text:
            number = number * 10 + digits[ch]
    else:
        return None
    return number if 0 <= number <= 99 else None

def _release_x_match_is_aspect_ratio(match: re.Match[str]) -> bool:
    """仅把无补零的常见 ``16x9`` token 视为画面比例。

    ``4x03`` / ``16x09`` 仍可表达真实季集，避免为了过滤比例误伤
    合法紧凑编号。
    """
    season = int(match.group(1))
    episode = int(match.group(2))
    return bool(
        (season, episode) in _RELEASE_X_ASPECT_RATIOS
        and match.group(1) == str(season)
        and match.group(2) == str(episode)
    )

def _parse_release_x_position(
    value: str,
) -> tuple[int, int, tuple[int, int]] | None:
    """解析独立的 ``1x02`` 紧凑季集标记。

    该格式必须是完整 token，避免从 ``Title1x02``、分辨率或年份中截取。
    常见画面比例也失败关闭；标准 ``SxxEyy``/中文季集规则仍拥有更高优先级。
    """
    for match in _RELEASE_X_POSITION.finditer(str(value or "")):
        season = int(match.group(1))
        episode = int(match.group(2))
        if not (1 <= season <= 99 and 1 <= episode <= 999):
            continue
        if _release_x_match_is_aspect_ratio(match):
            continue
        return season, episode, match.span()
    return None

def _has_unaccepted_release_x_position(value: str) -> bool:
    """阻止 GuessIt 把尺寸、年份或越界 ``NxM`` token 当成季集。"""
    source = str(value or "")
    return bool(
        _RELEASE_X_NUMERIC_TOKEN.search(source)
        and _parse_release_x_position(source) is None
    )

def _extract_explicit_season(text: str, *, episode_context: bool = False) -> int | None:
    # ``S01-S03`` / ``第一-三季`` 是整季范围，不是单个 Season 01。
    # 在数据模型尚未表达季范围前保持未知，防止整个合集误归档到第一季。
    if _SEASON_RANGE_TOKEN.search(text or ""):
        return None
    word_match = _ENGLISH_ORDINAL_SEASON_TOKEN.search(text or "")
    if word_match:
        return _ENGLISH_ORDINAL_SEASONS.get(word_match.group(1).casefold())
    numeric = _extract_number(_SEASON_TOKEN, text)
    if numeric is not None:
        return numeric
    bracket_tv = _BRACKET_TV_SEASON_TOKEN.search(text or "")
    if bracket_tv:
        return int(bracket_tv.group("season"))
    match = _CHINESE_SEASON_TOKEN.search(text or "")
    if match:
        return _chinese_number(match.group(1))
    compact = _parse_release_x_position(text)
    if compact is not None:
        return compact[0]
    # ``2nd Attack`` 只有同时存在明确集号且紧邻标题尾部时才解释为第二季；
    # 普通电影名或 ``Attack on ...`` 不参与该推断。
    attack = _ORDINAL_ATTACK_SEASON_TOKEN.search(text or "")
    if attack and (episode_context or _extract_episode(text) is not None):
        return int(attack.group(1))
    return None

def _valid_unlabeled_episode(number: int) -> bool:
    """校验没有 ``E``/``EP`` 标签的裸集号。

    长篇动画会出现四位集号，但四位年份与常见分辨率更常见。显式
    ``SxxEyyyy`` 不走这里，因此只对裸数字执行保守排除。
    """
    return bool(
        1 <= number <= 9999
        and not 1900 <= number <= 2099
        and number not in _UNLABELED_EPISODE_RESERVED_VALUES
    )

def _extract_episode(text: str) -> int | None:
    """解析显式集号，并兼容中文数字及规格标签前的 ``[01]``。"""
    explicit = _extract_number(_EPISODE_TOKEN, text)
    if explicit is not None:
        return explicit
    chinese = _CHINESE_EPISODE_TOKEN.search(text or "")
    if chinese:
        return _chinese_number(chinese.group(1))
    compact = _parse_release_x_position(text)
    if compact is not None:
        return compact[1]
    bare_match = _BARE_EPISODE_SUFFIX.search(str(text or ""))
    if bare_match:
        bare = int(bare_match.group(1))
        if _valid_unlabeled_episode(bare):
            return bare
    for match in _BRACKET_EPISODE_TOKEN.finditer(str(text or "")):
        number = int(match.group(1))
        # 跳过年份和常见分辨率后继续检查后续括号，例如 ``[2026][04]``。
        if _valid_unlabeled_episode(number):
            return number
    return None

def _extract_special_episode(text: str) -> int | None:
    shared_position = special_media_position(text)
    if shared_position is not None:
        return shared_position
    match = _SPECIAL_EPISODE_TOKEN.search(text or "")
    if not match:
        return None
    for value in match.groups():
        if value:
            return int(value)
    return 1
