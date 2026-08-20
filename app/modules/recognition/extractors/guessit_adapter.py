"""GuessIt 的缓存、字段收敛与误判防护适配层。"""
from __future__ import annotations

import re
from functools import lru_cache

from app.modules.recognition.extractors.deterministic import _valid_unlabeled_episode



_GUESSIT_CJK_EPISODE_WORD_COLLISION = re.compile(
    r"第\s*(?:\d{1,4}|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:集|[话話])(?=[\u3040-\u30ff\u3400-\u9fff])"
)



def _position_number(value) -> int | None:
    candidates = value if isinstance(value, (list, tuple)) else (value,)
    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        try:
            number = int(float(candidate))
        except (TypeError, ValueError, OverflowError):
            continue
        if number >= 0:
            return number
    return None

def _season_number(value) -> int | None:
    """返回 UI/整理链路支持的合法季号，过滤 GuessIt 把年份识别成季号的情况。"""
    candidates = value if isinstance(value, (list, tuple)) else (value,)
    for candidate in candidates:
        number = _position_number(candidate)
        if number is not None and 0 <= number <= 99:
            return number
    return None

@lru_cache(maxsize=4096)
def _guessit_cached(value: str) -> dict:
    """缓存 GuessIt 的纯解析结果，减少同一发布名在多阶段管道中重复解析。"""
    from guessit import guessit

    return dict(guessit(value))

def _guessit_info(value: str) -> dict:
    try:
        # 返回顶层副本，避免某个调用方补写字段污染后续识别结果。
        return dict(_guessit_cached(str(value or "")))
    except Exception:
        return {}

def _guessit_episode_is_untrusted(
    value: str,
    explicit_episode: int | None,
    guessed_episode: int | None = None,
    guessed_season: int | None = None,
) -> bool:
    """拒绝 GuessIt 把标题词、年份或分辨率误切成 fallback 集号。

    ``第15话题`` 会被 GuessIt 拆成 episode=15、episode_title=题；GuessIt
    4.4 还会把裸 ``1080`` 拆成 S10E80、把 ``[2160]`` 识别成第 2160 集。
    只有保留数字能够解释 GuessIt 实际产出的季集位置时才屏蔽 fallback，
    避免同一发布名里的 ``1080p`` 误伤真实的 ``05`` 或 ``01-24`` 集号。
    明确标记的 ``E1080``、``S01E01`` 始终由本地解析器优先处理。
    """
    if explicit_episode is not None:
        return False
    raw_value = str(value or "")
    if _GUESSIT_CJK_EPISODE_WORD_COLLISION.search(raw_value):
        return True
    for match in re.finditer(r"(?<!\d)(\d{3,4})(?!\d)", raw_value):
        token = match.group(1)
        if _valid_unlabeled_episode(int(token)):
            continue
        if guessed_episode is not None and token == str(guessed_episode):
            return True
        if (
            guessed_season is not None
            and guessed_episode is not None
            and token == f"{guessed_season}{guessed_episode:02d}"
        ):
            return True
    return False
