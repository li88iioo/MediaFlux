"""本地媒体自动整理的持久化模型与状态约束。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Any

LocalTaskStatus = Literal[
    "waiting_stable",
    "recognizing",
    "requires_manual",
    "planned",
    "moving",
    "verifying",
    "refreshing",
    "completed",
    "rolling_back",
    "failed",
]

LOCAL_TASK_STATUSES: frozenset[str] = frozenset(
    {
        "waiting_stable",
        "recognizing",
        "requires_manual",
        "planned",
        "moving",
        "verifying",
        "refreshing",
        "completed",
        "rolling_back",
        "failed",
    }
)
LOCAL_BUSY_TASK_STATUSES: frozenset[str] = frozenset(
    {
        "waiting_stable",
        "recognizing",
        "planned",
        "moving",
        "verifying",
        "refreshing",
        "rolling_back",
    }
)
LOCAL_MEDIA_CATEGORIES: frozenset[str] = frozenset(
    {"default", "movie", "tv", "anime", "documentary", "variety", "concert", "kids"}
)
LOCAL_MEDIA_TRIGGERS: frozenset[str] = frozenset({"qb_completed", "scan", "manual"})


def _value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


@dataclass(frozen=True)
class LocalMediaSource:
    id: int
    owner: str
    name: str
    qb_profile: str
    qb_path_prefix: str
    local_root: str
    enabled: bool
    stable_seconds: int
    scan_enabled: bool
    scan_interval_minutes: int
    media_type: str
    mode: str
    created_at: str
    updated_at: str
    smb_user: str = ""
    smb_pass: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LocalMediaSource":
        return cls(
            id=int(row["id"]), owner=str(row["owner"]), name=str(row["name"]),
            qb_profile=str(_value(row, "qb_profile")),
            qb_path_prefix=str(_value(row, "qb_path_prefix")),
            local_root=str(row["local_root"]), enabled=bool(row["enabled"]),
            stable_seconds=int(_value(row, "stable_seconds", 300)),
            scan_enabled=bool(_value(row, "scan_enabled", 0)),
            scan_interval_minutes=int(_value(row, "scan_interval_minutes", 10)),
            media_type=str(_value(row, "media_type", "auto")),
            mode=str(_value(row, "mode", "move")),
            created_at=str(row["created_at"]), updated_at=str(_value(row, "updated_at")),
            smb_user=str(_value(row, "smb_user", "")),
            smb_pass=str(_value(row, "smb_pass", "")),
        )


@dataclass(frozen=True)
class LocalLibraryTarget:
    id: int
    source_id: int
    owner: str
    category: str
    path: str
    provider: str
    library_id: str
    library_name: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LocalLibraryTarget":
        return cls(
            id=int(row["id"]), source_id=int(row["source_id"]), owner=str(row["owner"]),
            category=str(row["category"]), path=str(row["path"]),
            provider=str(_value(row, "provider")), library_id=str(_value(row, "library_id")),
            library_name=str(_value(row, "library_name")),
            created_at=str(row["created_at"]), updated_at=str(_value(row, "updated_at")),
        )


@dataclass(frozen=True)
class LocalMediaTask:
    id: int
    owner: str
    source_id: int
    qb_hash: str
    content_path: str
    trigger: str
    status: LocalTaskStatus
    operation_token: str
    snapshot_digest: str
    rules_snapshot: str
    tmdb_id: str
    media_type: str
    season_override: int | None
    episode_override: int | None
    title: str
    year: str
    attempts: int
    version: int
    error: str
    warning: str
    stable_since: str
    created_at: str
    updated_at: str
    completed_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LocalMediaTask":
        return cls(
            id=int(row["id"]), owner=str(row["owner"]), source_id=int(row["source_id"]),
            qb_hash=str(_value(row, "qb_hash")), content_path=str(row["content_path"]),
            trigger=str(row["trigger"]), status=str(row["status"]),  # type: ignore[arg-type]
            operation_token=str(row["operation_token"]), snapshot_digest=str(_value(row, "snapshot_digest")),
            rules_snapshot=str(_value(row, "rules_snapshot")),
            tmdb_id=str(_value(row, "tmdb_id")), media_type=str(_value(row, "media_type")),
            season_override=(
                None if _value(row, "season_override", None) is None
                else int(_value(row, "season_override"))
            ),
            episode_override=(
                None if _value(row, "episode_override", None) is None
                else int(_value(row, "episode_override"))
            ),
            title=str(_value(row, "title")), year=str(_value(row, "year")),
            attempts=int(_value(row, "attempts", 0)),
            version=int(_value(row, "version", 1)), error=str(_value(row, "error")),
            warning=str(_value(row, "warning")), stable_since=str(_value(row, "stable_since")),
            created_at=str(row["created_at"]), updated_at=str(_value(row, "updated_at")),
            completed_at=str(_value(row, "completed_at")),
        )
