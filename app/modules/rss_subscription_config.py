"""RSS 订阅配置的唯一规范化与调度唤醒入口。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.logger import get_logger
from app.modules.media_identity import normalize_tmdb_id
from app.modules.rss import validate_rss_source_urls

logger = get_logger(__name__)


class RSSSubscriptionConfigError(ValueError):
    """RSS 订阅配置不符合服务端白名单约束。"""


_MAX_NAME = 160
_MAX_EXCLUDE = 1000
_MAX_CRON = 120
_MAX_PATH = 1000
_MAX_REFRESH_INTERVAL_MINUTES = 10_080
_ALLOWED_PARSERS = {"mikan"}
_ALLOWED_ACTIONS = {"subscribe", "download"}
_ALLOWED_DOWNLOAD_METHODS = {"", "qb", "guangya"}
_TEXT_FIELDS = {
    "name",
    "urls",
    "exclude_keywords",
    "refresh_cron",
    "parser",
    "action",
    "download_method",
    "qb_save_path",
    "gy_target_dir",
    "gy_target_dir_name",
    "media_tmdb_id",
}


_CREATE_FIELDS = _TEXT_FIELDS | {
    "enabled",
    "refresh_interval_minutes",
    "media_default_season",
    "skip_existing_episodes",
}


def _text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RSSSubscriptionConfigError(f"{field} 必须是字符串")
    text = value.strip()
    if required and not text:
        raise RSSSubscriptionConfigError(f"{field} 不能为空")
    if len(text) > maximum:
        raise RSSSubscriptionConfigError(f"{field} 过长")
    return text


def _bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise RSSSubscriptionConfigError(f"{field} 必须是布尔值")
    return value


def _integer(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RSSSubscriptionConfigError(f"{field} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RSSSubscriptionConfigError(f"{field} 必须是整数") from exc
    if not minimum <= result <= maximum:
        raise RSSSubscriptionConfigError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _mapping_value(current: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    if current is None:
        return default
    try:
        return current[key]
    except (KeyError, TypeError, IndexError):
        return default


def _media_fields(data: Mapping[str, Any], current: Mapping[str, Any] | None) -> dict[str, Any]:
    media_keys = {"media_tmdb_id", "media_default_season", "skip_existing_episodes"}
    if current is not None and not media_keys.intersection(data):
        return {}
    raw_tmdb_id = data.get(
        "media_tmdb_id",
        _mapping_value(current, "media_tmdb_id", ""),
    )
    tmdb_id = _text(raw_tmdb_id, field="media_tmdb_id", maximum=10)
    skip_existing = data.get(
        "skip_existing_episodes",
        bool(_mapping_value(current, "skip_existing_episodes", False)),
    )
    skip_existing = _bool(skip_existing, field="skip_existing_episodes")
    if skip_existing and not tmdb_id:
        raise RSSSubscriptionConfigError("启用媒体库去重前必须填写 TMDB ID")
    if tmdb_id:
        try:
            tmdb_id = normalize_tmdb_id(tmdb_id)
        except ValueError as exc:
            raise RSSSubscriptionConfigError(str(exc)) from exc
    default_season = _integer(
        data.get(
            "media_default_season",
            _mapping_value(current, "media_default_season", 1),
        ),
        field="media_default_season",
        minimum=0,
        maximum=100,
    )
    return {
        "media_tmdb_id": tmdb_id,
        "media_default_season": default_season,
        "skip_existing_episodes": 1 if skip_existing else 0,
    }


def normalize_rss_subscription_create(
    data: Mapping[str, Any], *, allow_target_paths: bool = True
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RSSSubscriptionConfigError("订阅配置必须是对象")
    unknown = set(data) - _CREATE_FIELDS
    if unknown:
        raise RSSSubscriptionConfigError("订阅配置包含未支持字段")
    name = _text(data.get("name"), field="name", maximum=_MAX_NAME, required=True)
    urls = validate_rss_source_urls(
        _text(data.get("urls"), field="urls", maximum=8000, required=True)
    )
    parser = _text(data.get("parser", "mikan"), field="parser", maximum=32) or "mikan"
    if parser not in _ALLOWED_PARSERS:
        raise RSSSubscriptionConfigError("RSS 解析器无效")
    action = _text(data.get("action", "subscribe"), field="action", maximum=24) or "subscribe"
    if action not in _ALLOWED_ACTIONS:
        raise RSSSubscriptionConfigError("RSS 下载策略无效")
    method = _text(
        data.get("download_method", ""), field="download_method", maximum=24
    ).casefold()
    if method not in _ALLOWED_DOWNLOAD_METHODS:
        raise RSSSubscriptionConfigError("下载目标无效")
    interval = _integer(
        data.get("refresh_interval_minutes", 0),
        field="refresh_interval_minutes",
        minimum=0,
        maximum=_MAX_REFRESH_INTERVAL_MINUTES,
    )
    fields: dict[str, Any] = {
        "name": name,
        "urls": urls,
        "exclude_keywords": _text(
            data.get("exclude_keywords", ""),
            field="exclude_keywords",
            maximum=_MAX_EXCLUDE,
        ),
        "refresh_cron": _text(
            data.get("refresh_cron", ""), field="refresh_cron", maximum=_MAX_CRON
        ),
        "parser": parser,
        "action": action,
        "enabled": 1 if _bool(data.get("enabled", True), field="enabled") else 0,
        "refresh_interval_minutes": interval,
        "download_method": method,
        "qb_save_path": "",
        "gy_target_dir": "",
        "gy_target_dir_name": "",
    }
    if allow_target_paths:
        fields.update({
            "qb_save_path": _text(
                data.get("qb_save_path", ""), field="qb_save_path", maximum=_MAX_PATH
            ),
            "gy_target_dir": _text(
                data.get("gy_target_dir", ""), field="gy_target_dir", maximum=_MAX_PATH
            ),
            "gy_target_dir_name": _text(
                data.get("gy_target_dir_name", ""),
                field="gy_target_dir_name",
                maximum=_MAX_NAME,
            ),
        })
    fields.update(_media_fields(data, None))
    return fields


def normalize_rss_subscription_update(
    data: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
    allow_target_paths: bool = True,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RSSSubscriptionConfigError("订阅配置必须是对象")
    allowed = set(_TEXT_FIELDS) | {
        "enabled",
        "refresh_interval_minutes",
        "media_default_season",
        "skip_existing_episodes",
    }
    unknown = set(data) - allowed
    if unknown:
        raise RSSSubscriptionConfigError("订阅配置包含未支持字段")
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = _text(
            data["name"], field="name", maximum=_MAX_NAME, required=True
        )
    if "urls" in data:
        fields["urls"] = validate_rss_source_urls(
            _text(data["urls"], field="urls", maximum=8000, required=True)
        )
    if "exclude_keywords" in data:
        fields["exclude_keywords"] = _text(
            data["exclude_keywords"],
            field="exclude_keywords",
            maximum=_MAX_EXCLUDE,
        )
    if "refresh_cron" in data:
        fields["refresh_cron"] = _text(
            data["refresh_cron"], field="refresh_cron", maximum=_MAX_CRON
        )
    if "parser" in data:
        parser = _text(data["parser"], field="parser", maximum=32)
        if parser not in _ALLOWED_PARSERS:
            raise RSSSubscriptionConfigError("RSS 解析器无效")
        fields["parser"] = parser
    if "action" in data:
        action = _text(data["action"], field="action", maximum=24)
        if action not in _ALLOWED_ACTIONS:
            raise RSSSubscriptionConfigError("RSS 下载策略无效")
        fields["action"] = action
    if "download_method" in data:
        method = _text(
            data["download_method"], field="download_method", maximum=24
        ).casefold()
        if method not in _ALLOWED_DOWNLOAD_METHODS:
            raise RSSSubscriptionConfigError("下载目标无效")
        fields["download_method"] = method
    if "enabled" in data:
        fields["enabled"] = 1 if _bool(data["enabled"], field="enabled") else 0
    if "refresh_interval_minutes" in data:
        fields["refresh_interval_minutes"] = _integer(
            data["refresh_interval_minutes"],
            field="refresh_interval_minutes",
            minimum=0,
            maximum=_MAX_REFRESH_INTERVAL_MINUTES,
        )
    for field in ("qb_save_path", "gy_target_dir", "gy_target_dir_name"):
        if field not in data:
            continue
        if not allow_target_paths:
            raise RSSSubscriptionConfigError("Agent 不接受任意下载路径或云端目录标识")
        fields[field] = _text(
            data[field],
            field=field,
            maximum=_MAX_NAME if field.endswith("_name") else _MAX_PATH,
        )
    fields.update(_media_fields(data, current))
    if not fields:
        raise RSSSubscriptionConfigError("至少需要修改一个订阅字段")
    return fields


def wake_rss_scheduler() -> bool:
    """Best-effort 唤醒调度器；保存语义不因后台线程故障回滚。"""
    try:
        from app.modules.rss_scheduler import get_rss_scheduler

        get_rss_scheduler().reload()
        return True
    except Exception as exc:  # noqa: BLE001 - 调度器唤醒不得回滚已提交配置
        logger.warning("RSS 调度器唤醒失败 type=%s", type(exc).__name__)
        return False
