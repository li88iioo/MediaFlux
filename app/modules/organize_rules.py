"""所有整理入口共用的规则、自动确认判定与持久化快照。

规则集中处理配置与来源判定；仅显式传入客户端时查询有界祖先，不扫描目录树或持有任务运行态。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from app.config import get, get_bool, get_int
from app.modules.naming import (
    MOVIE_DEFAULT,
    MOVIE_DIR_DEFAULT,
    SHOW_DIR_DEFAULT,
    TV_DEFAULT,
)
from app.modules.recognition_policy import normalize_automatic_match_preset
from app.modules.scraper import MatchResult


DEFAULT_ORGANIZE_VIDEO_EXTS = (
    "mkv", "mp4", "ts", "m2ts", "mts", "avi", "mov", "m4v", "webm",
    "mpeg", "mpg", "wmv", "flv", "vob", "tp", "f4v", "rm", "rmvb",
)


DEFAULT_ORGANIZE_METADATA_EXTS = (
    "nfo", "srt", "ass", "ssa", "sup", "vtt", "sub", "idx",
    "jpg", "jpeg", "png", "webp",
)


def automatic_match_requires_confirmation(
    match: MatchResult | None, *, threshold: float = 0.9,
) -> bool:
    """统一判定无人工选择的自动整理结果是否足够安全。

    默认均衡档保持原有 90% 严格门槛。积极档只放宽“唯一 TMDB 候选因
    分数略低于 strict 阈值”这一种情况；类型/年份/季集冲突、近似并列、
    AI 未复核结果以及缺少结构化评分证据时仍失败关闭。
    """
    if match is None:
        return True
    try:
        required = float(threshold)
        confidence = float(getattr(match, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return True
    if not 0.0 < required <= 1.0 or confidence < required:
        return True

    status = str(getattr(match, "status", "") or "").strip().lower()
    need_confirm = bool(getattr(match, "need_confirm", False))
    if (not status or status == "matched") and not need_confirm:
        return False

    # 90% 及以上档位绝不覆盖识别器的显式人工确认结论。
    if required >= 0.9:
        return True
    if status not in {"low_confidence", "matched"}:
        return True
    if str(getattr(match, "provider", "") or "").strip().lower() != "tmdb":
        return True
    matched_by = str(getattr(match, "matched_by", "") or "").strip().lower()
    if matched_by and matched_by not in {"search", "title_search"}:
        return True
    if dict(getattr(match, "ai_diagnostic", None) or {}):
        return True
    if list(getattr(match, "rejected_constraints", None) or []):
        return True

    decision = dict(getattr(match, "threshold_decision", None) or {})
    if str(decision.get("reason") or "") != "below_threshold":
        return True
    try:
        if abs(float(decision.get("score")) - confidence) > 0.001:
            return True
    except (TypeError, ValueError):
        return True

    candidates = list(getattr(match, "candidates", None) or [])
    if not candidates:
        return True
    selected = getattr(candidates[0], "score_breakdown", None)
    if selected is None or list(getattr(selected, "rejected_constraints", None) or []):
        return True
    strong_title_score = max(
        float(getattr(selected, "title_score", 0.0) or 0.0),
        float(getattr(selected, "original_title_score", 0.0) or 0.0),
        float(getattr(selected, "alias_score", 0.0) or 0.0),
    )
    if strong_title_score < required:
        return True
    if len(candidates) > 1:
        second_score = float(getattr(candidates[1], "score", 0.0) or 0.0)
        if confidence - second_score < 0.08:
            return True
    return False


@dataclass
class OrganizeRules:
    target_dir_id: str = "0"
    add_kids: bool = False
    add_concert: bool = False
    region_split: bool = True
    year_split: bool = True
    small_file_mb: int = 10
    clean_empty: bool = True
    conflict_strategy: int = 1  # 1=不覆盖仅同名 2=覆盖大文件优先 3=覆盖小文件优先
    remux_first: bool = True
    resolution_first: bool = True
    dolby_first: bool = True
    keep_multi_versions: bool = False
    keep_remux_variant: bool = False
    recycle_replaced_enabled: bool = False
    link_strm: bool = True
    video_exts: str = ""
    metadata_exts: str = ""
    rename_enabled: bool = True
    media_info_enabled: bool = True
    media_probe_enabled: bool = True
    media_probe_timeout: int = 30
    movie_dir_template: str = MOVIE_DIR_DEFAULT
    movie_template: str = MOVIE_DEFAULT
    tv_template: str = TV_DEFAULT
    show_dir_template: str = SHOW_DIR_DEFAULT
    naming_scope: str = "both"
    notify_enabled: bool = True
    library_notify: bool = True
    strm_detail_notify: bool = True
    emby_refresh: bool = True
    nsfw_enabled: bool = False
    nsfw_source_ids: str = ""
    nsfw_exclusive: bool = False
    nsfw_metatube_endpoint: str = ""
    nsfw_metatube_token: str = ""
    nsfw_category_name: str = "成人内容"
    nsfw_strip_domains: str = ""
    nsfw_timeout_seconds: int = 8
    automatic_match_preset: str = "balanced"

    @classmethod
    def from_config(cls, target_dir_id: str = "") -> "OrganizeRules":
        """读取 Web/TG/自动入库共用的正式整理配置。"""
        return cls(
            target_dir_id=str(target_dir_id or get("GY_ORGANIZE_TARGET_DIR", "0") or "0"),
            add_kids=get_bool("GY_ORGANIZE_ADD_KIDS", False),
            add_concert=get_bool("GY_ORGANIZE_ADD_CONCERT", False),
            region_split=get_bool("GY_ORGANIZE_REGION_SPLIT", True),
            year_split=get_bool("GY_ORGANIZE_YEAR_SPLIT", True),
            small_file_mb=max(0, get_int("GY_ORGANIZE_SMALL_FILE_MB", 10)),
            clean_empty=get_bool("GY_ORGANIZE_CLEAN_EMPTY", True),
            conflict_strategy=max(1, min(get_int("GY_ORGANIZE_CONFLICT_STRATEGY", 1), 3)),
            remux_first=get_bool("GY_ORGANIZE_REMUX_FIRST", True),
            resolution_first=get_bool("GY_ORGANIZE_RESOLUTION_FIRST", True),
            dolby_first=get_bool("GY_ORGANIZE_DOLBY_FIRST", True),
            keep_multi_versions=get_bool("GY_ORGANIZE_KEEP_MULTI_VERSIONS", False),
            keep_remux_variant=get_bool("GY_ORGANIZE_KEEP_REMUX_VARIANT", False),
            recycle_replaced_enabled=get_bool("GY_ORGANIZE_RECYCLE_REPLACED_ENABLED", False),
            link_strm=get_bool("GY_ORGANIZE_LINK_STRM", True),
            video_exts=get("GY_ORGANIZE_VIDEO_EXTS", ""),
            metadata_exts=get("GY_ORGANIZE_METADATA_EXTS", ""),
            # 命名与媒体规格探测已统一为产品固定契约。旧环境变量继续允许
            # 留在 user.env 中，但不再影响任何新任务或历史快照恢复。
            rename_enabled=True,
            media_info_enabled=True,
            media_probe_enabled=True,
            media_probe_timeout=30,
            movie_dir_template=MOVIE_DIR_DEFAULT,
            movie_template=MOVIE_DEFAULT,
            tv_template=TV_DEFAULT,
            show_dir_template=SHOW_DIR_DEFAULT,
            naming_scope="both",
            notify_enabled=get_bool("GY_ORGANIZE_NOTIFY_ENABLED", True),
            library_notify=get_bool("GY_ORGANIZE_LIBRARY_NOTIFY", True),
            strm_detail_notify=get_bool("GY_ORGANIZE_STRM_DETAIL_NOTIFY", True),
            emby_refresh=get_bool("GY_ORGANIZE_EMBY_REFRESH", True),
            nsfw_enabled=get_bool("GY_ORGANIZE_NSFW_ENABLED", False),
            nsfw_source_ids=get("GY_ORGANIZE_NSFW_SOURCE_IDS", ""),
            nsfw_exclusive=False,
            nsfw_metatube_endpoint=get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT", ""),
            nsfw_metatube_token=get("GY_ORGANIZE_NSFW_METATUBE_TOKEN", ""),
            nsfw_category_name=get("GY_ORGANIZE_NSFW_CATEGORY_NAME", "成人内容") or "成人内容",
            nsfw_strip_domains=get("GY_ORGANIZE_NSFW_STRIP_DOMAINS", ""),
            nsfw_timeout_seconds=max(2, min(get_int("GY_ORGANIZE_NSFW_TIMEOUT_SECONDS", 8), 30)),
            automatic_match_preset=normalize_automatic_match_preset(
                get("GY_ORGANIZE_AUTOMATIC_MATCH_PRESET", "balanced")
            ),
        )

    def selected_nsfw_source_ids(self) -> frozenset[str]:
        """返回已配置的成人专用光鸭来源；异常配置按空集失败关闭。"""
        from app.modules.organize_sources import normalize_organize_source_ids

        source_ids, error = normalize_organize_source_ids(self.nsfw_source_ids)
        if error:
            return frozenset()
        return frozenset(source_ids)

    def for_source(self, source_id: str, *, client=None) -> "OrganizeRules":
        """统一正式来源、下载隔离目录及手工子目录的 NSFW 识别边界。"""
        selected = self.selected_nsfw_source_ids()
        if not self.nsfw_enabled or not selected:
            return replace(self, nsfw_enabled=False, nsfw_exclusive=False)
        source = str(source_id or "").strip()
        if source not in selected and source not in {"", "0"}:
            from app.repositories.download_requests import get_guangya_staging_parent

            parent = get_guangya_staging_parent(source)
            if parent in selected:
                source = parent
        if client is not None:
            visited: set[str] = set()
            for _ in range(96):
                if source in selected or source in {"", "0"} or source in visited:
                    break
                visited.add(source)
                try:
                    item = client.file_info(source)
                except Exception:
                    break
                source = str(
                    (item.get("parent_id") or item.get("parentId") or "")
                    if isinstance(item, dict) else getattr(item, "parent_id", "") or ""
                ).strip()
            else:
                source = ""  # 祖先链未在边界内得到证明，不启用成人识别。
        active = source in selected
        return replace(self, nsfw_enabled=active, nsfw_exclusive=active)

    def for_local_source(self, media_type: str) -> "OrganizeRules":
        """把全局规则收敛为一个本地来源的实际识别边界。"""
        selected = str(media_type or "").strip().lower() == "nsfw"
        active = bool(self.nsfw_enabled and selected)
        return replace(self, nsfw_enabled=active, nsfw_exclusive=active)


def enforce_fixed_organize_rules(rules: OrganizeRules) -> OrganizeRules:
    """覆盖已废弃的命名/探测配置，保证所有入口执行同一整理契约。"""
    return replace(
        rules,
        rename_enabled=True,
        media_info_enabled=True,
        media_probe_enabled=True,
        media_probe_timeout=30,
        movie_dir_template=MOVIE_DIR_DEFAULT,
        movie_template=MOVIE_DEFAULT,
        tv_template=TV_DEFAULT,
        show_dir_template=SHOW_DIR_DEFAULT,
        naming_scope="both",
    )


_ORGANIZE_RULE_SERVER_ONLY_FIELDS = frozenset({"nsfw_metatube_token"})


def organize_rules_snapshot(rules: OrganizeRules) -> dict[str, object]:
    """生成可持久化/返回前端的规则快照，不包含服务端密钥。"""
    payload = asdict(enforce_fixed_organize_rules(rules))
    for field_name in _ORGANIZE_RULE_SERVER_ONLY_FIELDS:
        payload.pop(field_name, None)
    return payload


def restore_organize_rules_snapshot(
    snapshot: object, *, trusted_rules: OrganizeRules | None = None,
) -> OrganizeRules:
    """从非敏感快照恢复规则；服务端字段始终取当前可信配置。"""
    if not isinstance(snapshot, dict):
        raise ValueError("整理规则快照无效")
    trusted = enforce_fixed_organize_rules(
        trusted_rules if trusted_rules is not None else OrganizeRules.from_config()
    )
    values = asdict(trusted)
    allowed_fields = set(OrganizeRules.__dataclass_fields__) - _ORGANIZE_RULE_SERVER_ONLY_FIELDS
    for key in allowed_fields:
        if key in snapshot:
            values[key] = snapshot[key]
    return enforce_fixed_organize_rules(OrganizeRules(**values))


def organize_rules_snapshot_matches(snapshot: object, current_rules: OrganizeRules) -> bool:
    """比较可执行规则；密钥轮换不复用历史值，而是使用当前服务端配置。"""
    if not isinstance(snapshot, dict):
        return False
    normalized = {
        key: value for key, value in snapshot.items()
        if key not in _ORGANIZE_RULE_SERVER_ONLY_FIELDS
    }
    return normalized == organize_rules_snapshot(current_rules)
