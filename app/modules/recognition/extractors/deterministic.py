"""无网络、无数据库副作用的确定性季集位置提取器。"""
from __future__ import annotations

import re

from app.modules.special_media import fractional_episode_position, special_media_position



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
    # Python 的 ``\b`` 会把 CJK 视为单词字符，导致 ``间谍过家家Season 3``
    # 与 ``我的英雄学院S4`` 无法命中。这里只禁止前一位是 ASCII 字母/数字，
    # 既兼容中日文标题紧贴季标，也不会从 ``Preseason`` 等英文单词内部截取。
    r"(?i)(?:(?<![A-Za-z0-9])season(?:[ ._]*|-(?=\d))|"
    r"(?<![A-Za-z0-9])s)"
    r"(\d{1,2})(?:\b|e\d{1,4}(?:v\d+)?)"
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
    r"|第\s*(\d{1,4})\s*(?:集|[话話])"
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
    # 多语种整季包常写成 ``Show - 01 MULTI [1080p]``。MULTI 只在
    # 已有发布分隔符、明确裸集号且后接技术括号/结尾时消费，不能作为
    # 全局噪声删除，否则 ``The Multi`` 一类正式标题会被破坏。
    r"(?:\s+multi)?"
    r"(?:[._]+)?(?:\s*(?:fin(?:al)?|end|complete|完结|完))?"
    r"(?=\s*(?:[\[【(（]|$))"
)

# 多季合集里的文件常写成 ``Show - S2 08 MULTI [1080p]``：S2 是季号，
# 后续裸数字是季内集号。必须同时具备独立 S 标记、集号、发布尾部边界，
# 避免把标题中的普通字母数字组合或分辨率解释成季集。
_SEASON_BARE_EPISODE_SUFFIX = re.compile(
    r"(?i)(?:^|\s+-\s+|[._]+)"
    r"s(?P<season>\d{1,2})[ ._-]+"
    r"(?P<episode>\d{1,4})(?:v\d+)?"
    r"(?:\s+multi)?"
    r"(?:[._]+)?(?:\s*(?:fin(?:al)?|end|complete|完结|完))?"
    r"(?=\s*(?:[\[【(（]|$))"
)

_BRACKET_EPISODE_TOKEN = re.compile(r"(?i)[\[【(（]\s*(\d{1,4})(?:v\d+)?\s*[\]】)）]")

# 少量动画发布使用 ``Title - <03> [1080p]``。只有空格包围的发布分隔符、
# 完整尖括号和后续技术括号/结尾同时成立时才接受，避免误读正式标题中的
# HTML/数学样式尖括号。
_ANGLE_EPISODE_TOKEN = re.compile(
    r"(?i)\s+-\s+<\s*(\d{1,4})(?:v\d+)?\s*>"
    r"(?=\s*(?:[\[【(（]|$))"
)

# 动画发布常同时给出季内编号与绝对编号：``20(92)``、``[01(64)]``。
# 两者都应以第一个数字作为当前季集号，括号内数字仅作为绝对编号证据；
# 否则通用括号规则会错误地把 ``[01(64)]`` 识别成第 64 集。
_UNDERSCORE_DUAL_EPISODE_TOKEN = re.compile(
    r"(?i)[\[【]\s*(\d{1,4})(?:v\d+)?\s*_\s*(\d{1,4})\s*[\]】]"
)
_DUAL_EPISODE_PATTERNS = (
    re.compile(
        r"(?i)[\[【]\s*(\d{1,4})(?:v\d+)?\s*[（(]\s*"
        r"(\d{1,4})\s*[）)]\s*[\]】]"
    ),
    re.compile(
        r"(?i)(?:^|\s+-\s+|[._]+)(\d{1,4})(?:v\d+)?\s*"
        r"[（(]\s*(\d{1,4})\s*[）)]"
        r"(?=\s*(?:[\[【(（]|$))"
    ),
    _UNDERSCORE_DUAL_EPISODE_TOKEN,
)

# 少量发布把季度与季内集号放在一个括号，再以相邻括号给出绝对集号：
# ``[4th - 12][總第78]``。三段证据必须同时存在，避免把普通 ``4th``、
# 日期或技术编号猜成季集位置。
_ORDINAL_SEASON_LOCAL_TOTAL_TOKEN = re.compile(
    r"(?ix)[\[【(（]\s*"
    r"(?P<season>\d{1,2})(?:st|nd|rd|th)\s*[-_.:：]\s*"
    r"(?P<episode>\d{1,4})(?:v\d+)?\s*[\]】)）]\s*"
    r"[\[【(（]\s*(?:总|總)\s*第?\s*"
    r"(?P<absolute>\d{1,4})\s*[\]】)）]"
)

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
    ordinal_total = _ORDINAL_SEASON_LOCAL_TOTAL_TOKEN.search(text or "")
    if ordinal_total:
        return int(ordinal_total.group("season"))
    season_bare = _SEASON_BARE_EPISODE_SUFFIX.search(text or "")
    if season_bare:
        return int(season_bare.group("season"))
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
    """解析显式集号，并兼容中文数字、双编号及规格标签前的 ``[01]``。"""
    # 小数集属于特别篇语义。若继续套用裸数字规则，``12.5`` 会被错误解析
    # 成第 5 集；其最终整数位置由特别篇统一分配器决定。
    if fractional_episode_position(text) is not None:
        return None
    ordinal_total = _ORDINAL_SEASON_LOCAL_TOTAL_TOKEN.search(text or "")
    if ordinal_total:
        local_episode = int(ordinal_total.group("episode"))
        absolute_episode = int(ordinal_total.group("absolute"))
        if (
            _valid_unlabeled_episode(local_episode)
            and _valid_unlabeled_episode(absolute_episode)
            and absolute_episode >= local_episode
        ):
            return local_episode
    season_bare = _SEASON_BARE_EPISODE_SUFFIX.search(text or "")
    if season_bare:
        bare_episode = int(season_bare.group("episode"))
        if _valid_unlabeled_episode(bare_episode):
            return bare_episode
    explicit = _extract_number(_EPISODE_TOKEN, text)
    if explicit is not None:
        return explicit
    chinese = _CHINESE_EPISODE_TOKEN.search(text or "")
    if chinese:
        return _chinese_number(chinese.group(1))
    compact = _parse_release_x_position(text)
    if compact is not None:
        return compact[1]
    source = str(text or "")
    angle = _ANGLE_EPISODE_TOKEN.search(source)
    if angle:
        number = int(angle.group(1))
        if _valid_unlabeled_episode(number):
            return number
    for pattern in _DUAL_EPISODE_PATTERNS:
        dual = pattern.search(source)
        if not dual:
            continue
        if pattern is _UNDERSCORE_DUAL_EPISODE_TOKEN and not (
            _SEASON_TOKEN.search(source)
            or _BRACKET_TV_SEASON_TOKEN.search(source)
            or _CHINESE_SEASON_TOKEN.search(source)
            or _ENGLISH_ORDINAL_SEASON_TOKEN.search(source)
        ):
            # ``[720_1080]`` 等尺寸/编码私有标签不能在没有明确季度证据时
            # 被解释为双集号。
            continue
        local_episode, absolute_episode = (int(value) for value in dual.groups())
        if (
            _valid_unlabeled_episode(local_episode)
            and _valid_unlabeled_episode(absolute_episode)
            and absolute_episode >= local_episode
        ):
            return local_episode
    bare_match = _BARE_EPISODE_SUFFIX.search(source)
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
    if fractional_episode_position(text) is not None:
        return None
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
