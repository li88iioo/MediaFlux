"""本地媒体来源、归档目标、任务与写操作审计的数据访问。

跨来源/目标配置以及下载请求/整理任务的写入保持同一连接、同一事务；
连接、测试数据库隔离和启动恢复事务仍由 app.database 门面唯一持有。
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import sqlite3
from collections.abc import Iterable


_LOCAL_MEDIA_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed"})


def _local_media_owner(owner: str) -> str:
    value = str(owner or "").strip()
    if not value:
        raise ValueError("owner 不能为空")
    return value


def _canonical_local_media_content_path(value: object) -> str:
    from app.modules.local_media_models import canonical_local_media_content_path

    return canonical_local_media_content_path(str(value or ""))


def _active_local_media_task_for_path(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    owner: str,
    content_path: str,
) -> sqlite3.Row | None:
    """按规范路径查找唯一活动任务，并拒绝祖先/后代范围重叠。"""
    target = Path(content_path)
    ancestors = set(target.parents)
    match: sqlite3.Row | None = None
    exact_count = 0
    overlaps = False
    for row in conn.execute(
        "SELECT id,source_id,qb_hash,content_path,trigger,status,operation_token,error "
        "FROM local_media_tasks WHERE owner=? AND status NOT IN ('completed','failed')",
        (owner,),
    ):
        try:
            candidate = Path(_canonical_local_media_content_path(row["content_path"]))
        except ValueError:
            continue
        if candidate == target:
            exact_count += 1
            match = row
        elif candidate in ancestors or target in candidate.parents:
            overlaps = True
    if exact_count > 1:
        raise RuntimeError("同一路径存在多个活动本地媒体任务，请先完成或清理旧任务")
    if overlaps:
        if match is None:
            raise ValueError("该路径与未完成的本地媒体任务范围重叠")
        raise RuntimeError("同一路径范围存在多个活动本地媒体任务，请先完成或清理旧任务")
    if match is not None and int(match["source_id"] or 0) != int(source_id):
        raise ValueError("该路径已有其他本地媒体来源的活动任务")
    return match


def _local_media_source_has_active_task(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    owner: str,
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM local_media_tasks WHERE source_id=? AND owner=? "
            "AND status NOT IN ('completed','failed') LIMIT 1",
            (int(source_id), owner),
        ).fetchone()
        is not None
    )


def _local_media_target_signature(
    rows: Iterable[sqlite3.Row | dict[str, object]],
) -> tuple:
    fields = (
        "category",
        "path",
        "provider",
        "library_id",
        "library_name",
        "server_path",
    )
    return tuple(
        sorted(tuple(str(row[field] or "") for field in fields) for row in rows)
    )


def _latest_terminal_local_media_task_for_path(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    owner: str,
    content_path: str,
) -> sqlite3.Row | None:
    """返回同来源同规范路径最近的终态任务，供显式重试复用。"""
    with closing(conn.execute(
        "SELECT id,source_id,qb_hash,content_path,trigger,status,operation_token,error "
        "FROM local_media_tasks WHERE source_id=? AND owner=? "
        "AND status IN ('completed','failed') ORDER BY id DESC",
        (int(source_id), owner),
    )) as rows:
        for row in rows:
            try:
                candidate_path = _canonical_local_media_content_path(row["content_path"])
            except ValueError:
                continue
            if candidate_path == content_path:
                return row
    return None


def _normalize_local_media_task_path(
    conn: sqlite3.Connection, row: sqlite3.Row, content_path: str
) -> None:
    if str(row["content_path"] or "") == content_path:
        return
    conn.execute(
        "UPDATE local_media_tasks SET content_path=?,updated_at=? WHERE id=?",
        (content_path, db.now(), int(row["id"])),
    )


def _bind_qb_hash_to_active_local_media_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    qb_hash: str,
    content_path: str,
    *,
    previous_hash_task_id: int | None = None,
) -> int:
    """给既有活动任务补充 qB 绑定，不重置识别或人工确认状态。"""
    current_hash = str(row["qb_hash"] or "").strip().lower()
    if current_hash and current_hash != qb_hash:
        raise ValueError("同一路径的活动任务已绑定其他 qB 任务")
    task_id = int(row["id"] or 0)
    if previous_hash_task_id is not None and int(previous_hash_task_id) != task_id:
        conn.execute(
            "UPDATE local_media_tasks SET qb_hash=NULL,updated_at=? "
            "WHERE id=? AND status IN ('completed','failed')",
            (db.now(), int(previous_hash_task_id)),
        )
    conn.execute(
        "UPDATE local_media_tasks SET qb_hash=?,content_path=?,updated_at=? WHERE id=?",
        (qb_hash, content_path, db.now(), task_id),
    )
    return task_id


def create_local_media_source(
    name: str,
    qb_profile: str,
    qb_path_prefix: str,
    local_root: str,
    enabled: int = 1,
    stable_seconds: int = 0,
    scan_enabled: int = 0,
    scan_interval_minutes: int = 10,
    *,
    owner: str = "admin",
    media_type: str = "auto",
    mode: str = "move",
    smb_user: str = "",
    smb_pass: str = "",
) -> int:
    safe_owner = _local_media_owner(owner)
    safe_name = str(name or "").strip()
    safe_root = str(local_root or "").strip()
    safe_mode = str(mode or "move").strip().lower()
    safe_media_type = str(media_type or "auto").strip().lower()
    if not safe_name or not safe_root:
        raise ValueError("本地媒体来源名称和路径不能为空")
    if safe_mode not in {"move", "preview_only"}:
        raise ValueError("本地媒体来源仅支持 move 或 preview_only")
    if safe_media_type not in {"auto", "movie", "tv", "nsfw"}:
        raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO local_media_sources(owner,name,qb_profile,qb_path_prefix,local_root,"
            "smb_user,smb_pass,"
            "enabled,stable_seconds,scan_enabled,scan_interval_minutes,media_type,mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                safe_owner,
                safe_name,
                str(qb_profile or ""),
                str(qb_path_prefix or ""),
                safe_root,
                "",
                "",
                1 if enabled else 0,
                0,
                0,
                10,
                safe_media_type,
                safe_mode,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def save_local_media_source_bundle(
    *,
    source_id: int | None = None,
    name: str,
    qb_profile: str,
    qb_path_prefix: str,
    local_root: str,
    enabled: bool,
    media_type: str,
    mode: str,
    targets: list[dict[str, str]] | None,
    owner: str = "admin",
    smb_user: str = "",
    smb_pass: str = "",
    stable_seconds: int | None = None,
    scan_enabled: bool | None = None,
    scan_interval_minutes: int | None = None,
) -> int:
    """在单个事务内保存来源与全部分类目标。"""
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    safe_name = str(name or "").strip()
    safe_root = str(local_root or "").strip()
    safe_mode = str(mode or "move").strip().lower()
    safe_media_type = str(media_type or "auto").strip().lower()
    if not safe_name or not safe_root:
        raise ValueError("本地媒体来源名称和路径不能为空")
    if safe_mode not in {"move", "preview_only"}:
        raise ValueError("本地媒体来源仅支持 move 或 preview_only")
    if safe_media_type not in {"auto", "movie", "tv", "nsfw"}:
        raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
    if any("\x00" in value for value in (safe_root, str(qb_path_prefix or ""))):
        raise ValueError("本地媒体来源路径包含非法字符")
    normalized_targets: list[dict[str, str]] | None = None
    if targets is not None:
        normalized_targets = []
        seen: set[str] = set()
        for item in targets:
            category = str(item.get("category") or "").strip().lower()
            target_path = str(item.get("path") or "").strip()
            if category not in LOCAL_MEDIA_CATEGORIES or category in seen:
                raise ValueError("目标分类无效或重复")
            if not target_path or "\x00" in target_path:
                raise ValueError("媒体库目标路径无效")
            provider = str(item.get("provider") or "").strip().lower()
            library_id = str(item.get("library_id") or "").strip()
            library_name = str(item.get("library_name") or "").strip()
            server_path = str(item.get("server_path") or "").strip()
            if provider not in {"", "jellyfin", "emby"}:
                raise ValueError("目标媒体服务器类型无效")
            if provider and not library_name:
                raise ValueError("媒体服务器和媒体库名称必须同时选择")
            if not provider and (library_id or library_name or server_path):
                raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
            if server_path:
                from app.modules.media_server_path_mapping import MediaServerPathMapping

                server_path = MediaServerPathMapping(
                    target_path, server_path
                ).server_prefix
            seen.add(category)
            normalized_targets.append(
                {
                    "category": category,
                    "path": target_path,
                    "provider": provider,
                    "library_id": library_id,
                    "library_name": library_name,
                    "server_path": server_path,
                }
            )
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if source_id is None:
            cur = conn.execute(
                "INSERT INTO local_media_sources(owner,name,qb_profile,qb_path_prefix,local_root,"
                "smb_user,smb_pass,"
                "enabled,stable_seconds,scan_enabled,scan_interval_minutes,media_type,mode,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    safe_owner,
                    safe_name,
                    str(qb_profile or "").strip(),
                    str(qb_path_prefix or "").strip(),
                    safe_root,
                    "",
                    "",
                    1 if enabled else 0,
                    0,
                    0,
                    10,
                    safe_media_type,
                    safe_mode,
                    timestamp,
                    timestamp,
                ),
            )
            saved_id = int(cur.lastrowid)
        else:
            saved_id = int(source_id)
            current = conn.execute(
                "SELECT name,qb_profile,qb_path_prefix,local_root,enabled,media_type,mode "
                "FROM local_media_sources WHERE id=? AND owner=?",
                (saved_id, safe_owner),
            ).fetchone()
            if current is None:
                raise LookupError("本地媒体来源不存在")
            requested_operational = (
                str(qb_profile or "").strip(),
                str(qb_path_prefix or "").strip(),
                safe_root,
                1 if enabled else 0,
                safe_media_type,
                safe_mode,
            )
            current_operational = (
                str(current["qb_profile"] or ""),
                str(current["qb_path_prefix"] or ""),
                str(current["local_root"] or ""),
                int(current["enabled"] or 0),
                str(current["media_type"] or "auto"),
                str(current["mode"] or "move"),
            )
            targets_changed = False
            if normalized_targets is not None:
                current_targets = conn.execute(
                    "SELECT category,path,provider,library_id,library_name,server_path "
                    "FROM local_library_targets WHERE source_id=? AND owner=?",
                    (saved_id, safe_owner),
                ).fetchall()
                targets_changed = _local_media_target_signature(
                    current_targets
                ) != _local_media_target_signature(normalized_targets)
            if (
                requested_operational != current_operational or targets_changed
            ) and _local_media_source_has_active_task(
                conn,
                source_id=saved_id,
                owner=safe_owner,
            ):
                raise ValueError("来源仍有未完成任务，不能修改运行配置")
            cur = conn.execute(
                "UPDATE local_media_sources SET name=?,qb_profile=?,qb_path_prefix=?,local_root=?,"
                "smb_user=?,smb_pass=?,"
                "enabled=?,stable_seconds=?,scan_enabled=?,scan_interval_minutes=?,media_type=?,mode=?,updated_at=? "
                "WHERE id=? AND owner=?",
                (
                    safe_name,
                    str(qb_profile or "").strip(),
                    str(qb_path_prefix or "").strip(),
                    safe_root,
                    "",
                    "",
                    1 if enabled else 0,
                    0,
                    0,
                    10,
                    safe_media_type,
                    safe_mode,
                    timestamp,
                    saved_id,
                    safe_owner,
                ),
            )
            if cur.rowcount != 1:
                raise LookupError("本地媒体来源不存在")
        if normalized_targets is not None:
            conn.execute(
                "DELETE FROM local_library_targets WHERE source_id=? AND owner=?",
                (saved_id, safe_owner),
            )
            for item in normalized_targets:
                conn.execute(
                    "INSERT INTO local_library_targets(source_id,owner,category,path,provider,library_id,library_name,server_path,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        saved_id,
                        safe_owner,
                        item["category"],
                        item["path"],
                        item["provider"],
                        item["library_id"],
                        item["library_name"],
                        item["server_path"],
                        timestamp,
                        timestamp,
                    ),
                )
        return saved_id


def get_local_media_source(source_id: int, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalMediaSource

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), _local_media_owner(owner)),
        ).fetchone()
    return LocalMediaSource.from_row(row) if row else None


def list_local_media_sources(*, owner: str = "admin", enabled_only: bool = False):
    from app.modules.local_media_models import LocalMediaSource

    sql = "SELECT * FROM local_media_sources WHERE owner=?"
    params: list[object] = [_local_media_owner(owner)]
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY id ASC"
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [LocalMediaSource.from_row(row) for row in rows]


def upsert_local_library_target(
    source_id: int,
    category: str,
    path: str,
    provider: str = "",
    library_name: str = "",
    library_id: str = "",
    server_path: str = "",
    *,
    owner: str = "admin",
) -> int:
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    safe_category = str(category or "").strip().lower()
    safe_path = str(path or "").strip()
    safe_provider = str(provider or "").strip().lower()
    safe_library_id = str(library_id or "").strip()
    safe_library_name = str(library_name or "").strip()
    safe_server_path = str(server_path or "").strip()
    if safe_category not in LOCAL_MEDIA_CATEGORIES:
        raise ValueError("不支持的本地媒体分类")
    if not safe_path:
        raise ValueError("媒体库目标路径不能为空")
    if safe_provider not in {"", "jellyfin", "emby"}:
        raise ValueError("目标媒体服务器类型无效")
    if safe_provider and not safe_library_name:
        raise ValueError("媒体服务器和媒体库名称必须同时选择")
    if not safe_provider and (safe_library_id or safe_library_name or safe_server_path):
        raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
    if safe_server_path:
        from app.modules.media_server_path_mapping import MediaServerPathMapping

        safe_server_path = MediaServerPathMapping(
            safe_path, safe_server_path
        ).server_prefix
    timestamp = db.now()
    with db.get_conn() as conn:
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")
        conn.execute(
            "INSERT INTO local_library_targets(source_id,owner,category,path,provider,library_id,library_name,server_path,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,category) DO UPDATE SET "
            "owner=excluded.owner,path=excluded.path,provider=excluded.provider,"
            "library_id=excluded.library_id,library_name=excluded.library_name,"
            "server_path=excluded.server_path,updated_at=excluded.updated_at",
            (
                int(source_id),
                safe_owner,
                safe_category,
                safe_path,
                safe_provider,
                safe_library_id,
                safe_library_name,
                safe_server_path,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_library_targets WHERE source_id=? AND category=? AND owner=?",
            (int(source_id), safe_category, safe_owner),
        ).fetchone()
        return int(row["id"])


def list_local_library_targets(source_id: int | None = None, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalLibraryTarget

    where = "owner=?"
    params: list[object] = [_local_media_owner(owner)]
    if source_id is not None:
        where += " AND source_id=?"
        params.append(int(source_id))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM local_library_targets WHERE {where} ORDER BY source_id,category,id",
            params,
        ).fetchall()
    return [LocalLibraryTarget.from_row(row) for row in rows]


def list_local_library_bindings(*, owner: str = "admin") -> list[sqlite3.Row]:
    """在同一读快照中投影全部来源与归档绑定，避免逐来源 N+1 查询。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT t.source_id,s.name AS source_name,t.category,t.path AS local_path,"
            "t.provider,t.library_id,t.library_name,t.server_path "
            "FROM local_library_targets t JOIN local_media_sources s "
            "ON s.id=t.source_id AND s.owner=t.owner WHERE t.owner=? "
            "ORDER BY s.id,t.category,t.id",
            (_local_media_owner(owner),),
        ).fetchall()


def replace_local_library_targets(
    bindings: list[dict[str, object]],
    *,
    owner: str = "admin",
) -> None:
    """原子替换当前用户的全部本地归档目标，供统一媒体库页面保存。"""
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in bindings:
        source_id = int(item.get("source_id") or 0)
        category = str(item.get("category") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        provider = str(item.get("provider") or "").strip().lower()
        library_id = str(item.get("library_id") or "").strip()
        library_name = str(item.get("library_name") or "").strip()
        server_path = str(item.get("server_path") or "").strip()
        key = (source_id, category)
        if source_id <= 0 or category not in LOCAL_MEDIA_CATEGORIES or key in seen:
            raise ValueError("本地归档目标的来源或分类无效、重复")
        if not path or "\x00" in path:
            raise ValueError("本地归档目标路径无效")
        if provider not in {"", "jellyfin", "emby"}:
            raise ValueError("本地归档目标媒体服务器类型无效")
        if provider and not library_name:
            raise ValueError("媒体服务器和媒体库名称必须同时选择")
        if not provider and (library_id or library_name or server_path):
            raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
        seen.add(key)
        normalized.append(
            {
                "source_id": source_id,
                "category": category,
                "path": path,
                "provider": provider,
                "library_id": library_id,
                "library_name": library_name,
                "server_path": server_path,
            }
        )

    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_source_ids = {
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM local_media_sources WHERE owner=?",
                (safe_owner,),
            ).fetchall()
        }
        unknown = sorted(
            {int(item["source_id"]) for item in normalized} - existing_source_ids
        )
        if unknown:
            raise LookupError("本地媒体来源不存在")
        conn.execute("DELETE FROM local_library_targets WHERE owner=?", (safe_owner,))
        for item in normalized:
            conn.execute(
                "INSERT INTO local_library_targets(source_id,owner,category,path,provider,"
                "library_id,library_name,server_path,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    int(item["source_id"]),
                    safe_owner,
                    item["category"],
                    item["path"],
                    item["provider"],
                    item["library_id"],
                    item["library_name"],
                    item["server_path"],
                    timestamp,
                    timestamp,
                ),
            )


def create_local_media_task(
    source_id: int,
    qb_hash: str,
    content_path: str,
    *,
    owner: str = "admin",
    trigger: str = "qb_completed",
    operation_token: str = "",
) -> int:
    import uuid

    from app.modules.local_media_models import LOCAL_MEDIA_TRIGGERS

    safe_owner = _local_media_owner(owner)
    safe_trigger = str(trigger or "").strip().lower()
    safe_path = _canonical_local_media_content_path(content_path)
    normalized_hash = str(qb_hash or "").strip().lower() or None
    if safe_trigger not in LOCAL_MEDIA_TRIGGERS:
        raise ValueError("不支持的本地媒体任务触发方式")
    timestamp = db.now()
    token = str(operation_token or uuid.uuid4().hex)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")

        active = _active_local_media_task_for_path(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
            content_path=safe_path,
        )
        hash_task = None
        if normalized_hash:
            hash_task = conn.execute(
                "SELECT id,source_id,qb_hash,content_path,trigger,status,operation_token "
                "FROM local_media_tasks WHERE source_id=? AND qb_hash=? AND owner=?",
                (int(source_id), normalized_hash, safe_owner),
            ).fetchone()

        if hash_task is not None:
            task_id = int(hash_task["id"])
            terminal = (
                str(hash_task["status"] or "") in _LOCAL_MEDIA_TERMINAL_TASK_STATUSES
            )
            if terminal:
                if active is not None:
                    return _bind_qb_hash_to_active_local_media_task(
                        conn,
                        active,
                        normalized_hash,
                        safe_path,
                        previous_hash_task_id=task_id,
                    )
                if (
                    _canonical_local_media_content_path(hash_task["content_path"])
                    != safe_path
                ):
                    raise ValueError("相同 qB 任务对应的内容路径不一致")
                _normalize_local_media_task_path(conn, hash_task, safe_path)
                return task_id

            if (
                _canonical_local_media_content_path(hash_task["content_path"])
                != safe_path
            ):
                raise ValueError("相同 qB 任务对应的内容路径不一致")
            if active is not None and int(active["id"]) != task_id:
                raise RuntimeError(
                    "同一路径存在多个活动本地媒体任务，请先完成或清理旧任务"
                )
            _normalize_local_media_task_path(conn, hash_task, safe_path)
            return task_id

        if active is not None:
            if normalized_hash:
                return _bind_qb_hash_to_active_local_media_task(
                    conn, active, normalized_hash, safe_path
                )
            _normalize_local_media_task_path(conn, active, safe_path)
            return int(active["id"])

        cur = conn.execute(
            "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,trigger,status,"
            "operation_token,created_at,updated_at) VALUES(?,?,?,?,?,'waiting_stable',?,?,?)",
            (
                safe_owner,
                int(source_id),
                normalized_hash,
                safe_path,
                safe_trigger,
                token,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def create_and_link_qb_local_media_task(
    request_id: int,
    source_id: int,
    qb_hash: str,
    content_path: str,
    *,
    owner: str = "admin",
) -> tuple[int, bool]:
    """原子创建/复用 qB 本地整理任务并绑定下载请求。

    规范化内容路径是活动任务的唯一准入身份；qB hash 只补充下载绑定。
    返回 ``(task_id, restarted)``。新下载请求命中同 hash 的旧终态任务时，
    会开启新的 attempt；已经绑定到该任务的请求只复用当前状态。
    """
    import uuid

    safe_owner = _local_media_owner(owner)
    safe_path = _canonical_local_media_content_path(content_path)
    normalized_hash = str(qb_hash or "").strip().lower()
    if not normalized_hash:
        raise ValueError("qB 任务标识不能为空")

    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request_row = conn.execute(
            "SELECT local_import_status,local_import_target FROM download_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()
        if request_row is None:
            raise LookupError("下载请求不存在")
        local_status = str(request_row["local_import_status"] or "")
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")

        active = _active_local_media_task_for_path(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
            content_path=safe_path,
        )
        hash_task = conn.execute(
            "SELECT id,source_id,qb_hash,content_path,trigger,status,operation_token "
            "FROM local_media_tasks WHERE source_id=? AND qb_hash=? AND owner=?",
            (int(source_id), normalized_hash, safe_owner),
        ).fetchone()
        if local_status not in {"", "pending"}:
            # 完成后的重复交付只确认既有绑定，不重开任务或改写历史请求。
            if (
                hash_task is not None
                and request_row["local_import_target"] == f"local-media-task:{hash_task['id']}"
                and _canonical_local_media_content_path(hash_task["content_path"]) == safe_path
            ):
                return int(hash_task["id"]), False
            raise ValueError("下载请求的本地入库状态已结束")
        restarted = False

        if hash_task is None:
            if active is not None:
                task_id = _bind_qb_hash_to_active_local_media_task(
                    conn, active, normalized_hash, safe_path
                )
            else:
                cur = conn.execute(
                    "INSERT INTO local_media_tasks("
                    "owner,source_id,qb_hash,content_path,trigger,status,operation_token,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'waiting_stable',?,?,?)",
                    (
                        safe_owner,
                        int(source_id),
                        normalized_hash,
                        safe_path,
                        "qb_completed",
                        uuid.uuid4().hex,
                        timestamp,
                        timestamp,
                    ),
                )
                task_id = int(cur.lastrowid)
        else:
            task_id = int(hash_task["id"])
            existing_target = f"local-media-task:{task_id}"
            already_linked = (
                str(request_row["local_import_target"] or "") == existing_target
            )
            terminal = (
                str(hash_task["status"] or "") in _LOCAL_MEDIA_TERMINAL_TASK_STATUSES
            )
            hash_path = _canonical_local_media_content_path(hash_task["content_path"])

            if not terminal:
                if hash_path != safe_path:
                    raise ValueError("相同 qB 任务对应的内容路径不一致")
                if active is not None and int(active["id"]) != task_id:
                    raise RuntimeError(
                        "同一路径存在多个活动本地媒体任务，请先完成或清理旧任务"
                    )
                _normalize_local_media_task_path(conn, hash_task, safe_path)
            elif already_linked:
                if hash_path != safe_path:
                    raise ValueError("相同 qB 任务对应的内容路径不一致")
                if active is not None:
                    raise RuntimeError("同一路径存在新的活动任务，请刷新下载请求状态")
                _normalize_local_media_task_path(conn, hash_task, safe_path)
            elif active is not None:
                task_id = _bind_qb_hash_to_active_local_media_task(
                    conn,
                    active,
                    normalized_hash,
                    safe_path,
                    previous_hash_task_id=int(hash_task["id"]),
                )
            else:
                conn.execute(
                    "UPDATE local_media_tasks SET content_path=?,trigger='qb_completed',"
                    "status='waiting_stable',stable_since='',snapshot_digest='',rules_snapshot='',"
                    "recognition_summary='',tmdb_id='',media_type='',season_override=NULL,"
                    "episode_override=NULL,numbering_mode='auto',title='',year='',"
                    "operation_token=?,error='',warning='',"
                    "completed_at=NULL,version=version+1,updated_at=? WHERE id=? AND owner=?",
                    (safe_path, uuid.uuid4().hex, timestamp, task_id, safe_owner),
                )
                conn.execute(
                    "DELETE FROM local_media_task_items WHERE task_id=?",
                    (task_id,),
                )
                restarted = True

        target = f"local-media-task:{task_id}"
        current_target = str(request_row["local_import_target"] or "")
        if current_target and current_target != target:
            raise ValueError("下载请求已绑定其他本地整理任务")
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status='pending',local_import_target=?,"
            "qb_content_path=?,local_import_error='',local_import_started_at="
            "COALESCE(NULLIF(local_import_started_at,''),?),"
            "local_import_completed_at=NULL,updated_at=? "
            "WHERE id=? AND COALESCE(local_import_status,'') IN ('','pending')",
            (target, safe_path, timestamp, timestamp, int(request_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("下载请求的本地入库状态已变化")
        reconcile_local_media_downloads(conn, task_id=task_id)
        return task_id, restarted


def list_download_requests_for_local_media_task(task_id: int) -> list[sqlite3.Row]:
    """返回绑定到同一本地整理任务的下载事务。"""
    target = f"local-media-task:{int(task_id)}"
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM download_requests WHERE local_import_target=? ORDER BY id",
            (target,),
        ).fetchall()


def get_local_media_task(task_id: int, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalMediaTask

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM local_media_tasks WHERE id=? AND owner=?",
            (int(task_id), _local_media_owner(owner)),
        ).fetchone()
    return LocalMediaTask.from_row(row) if row else None


def list_local_media_tasks(*, owner: str = "admin", status: str = "", limit: int = 200):
    from app.modules.local_media_models import LOCAL_TASK_STATUSES, LocalMediaTask

    sql = "SELECT * FROM local_media_tasks WHERE owner=?"
    params: list[object] = [_local_media_owner(owner)]
    if status:
        if status not in LOCAL_TASK_STATUSES:
            raise ValueError("不支持的本地媒体任务状态")
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [LocalMediaTask.from_row(row) for row in rows]


def list_local_media_qb_write_conflicts(qb_hashes: list[str]) -> list[sqlite3.Row]:
    """返回正在修改文件/刷新媒体库的 qB 关联任务，供所有控制入口共用。

    schema 或 SQLite 状态不可用时异常必须上抛，由控制层对 resume/delete
    失败关闭；这里不能把“无法判断”伪装成“没有冲突”。
    """
    normalized = sorted(
        {
            str(value or "").strip().casefold()
            for value in qb_hashes
            if str(value or "").strip()
        }
    )
    if not normalized:
        return []
    if len(normalized) > 200:
        raise ValueError("单次最多检查 200 个 qBittorrent 任务")
    placeholders = ",".join("?" for _ in normalized)
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id,qb_hash,status FROM local_media_tasks "
            f"WHERE lower(COALESCE(qb_hash,'')) IN ({placeholders}) "
            "AND status IN ('moving','verifying','refreshing','rolling_back') "
            "ORDER BY id",
            normalized,
        ).fetchall()


def list_waiting_local_media_tasks(*, owner: str = "admin", limit: int = 500):
    """按进入顺序领取待处理任务，避免最新任务窗口长期饿死旧任务。"""
    from app.modules.local_media_models import LocalMediaTask

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM local_media_tasks WHERE owner=? AND status='waiting_stable' "
            "ORDER BY id ASC LIMIT ?",
            (_local_media_owner(owner), max(1, min(int(limit), 1000))),
        ).fetchall()
    return [LocalMediaTask.from_row(row) for row in rows]


def delete_local_media_tasks(
    task_ids: list[int], *, owner: str = "admin"
) -> dict[str, int]:
    """删除指定的非运行中本地整理日志；关联条目和步骤由外键级联清理。"""
    from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES

    safe_owner = _local_media_owner(owner)
    normalized_ids: list[int] = []
    for raw_task_id in task_ids:
        task_id = int(raw_task_id)
        if task_id > 0 and task_id not in normalized_ids:
            normalized_ids.append(task_id)
    if not normalized_ids:
        return {"requested": 0, "deleted": 0, "skipped_busy": 0, "missing": 0}
    if len(normalized_ids) > 500:
        raise ValueError("单次最多清除 500 条本地整理日志")

    placeholders = ",".join("?" for _ in normalized_ids)
    busy_placeholders = ",".join("?" for _ in LOCAL_BUSY_TASK_STATUSES)
    with db.get_conn() as conn:
        deleted = int(
            conn.execute(
                f"DELETE FROM local_media_tasks WHERE owner=? AND id IN ({placeholders}) "
                f"AND status NOT IN ({busy_placeholders})",
                [safe_owner, *normalized_ids, *LOCAL_BUSY_TASK_STATUSES],
            ).rowcount
        )
        remaining_rows = conn.execute(
            f"SELECT id,status FROM local_media_tasks WHERE owner=? AND id IN ({placeholders})",
            [safe_owner, *normalized_ids],
        ).fetchall()
        remaining = {int(row["id"]): str(row["status"]) for row in remaining_rows}
    return {
        "requested": len(normalized_ids),
        "deleted": deleted,
        "skipped_busy": sum(
            status in LOCAL_BUSY_TASK_STATUSES for status in remaining.values()
        ),
        "missing": len(normalized_ids) - deleted - len(remaining),
    }


def get_local_media_diagnostic_summary(
    *, owner: str = "admin"
) -> dict[str, dict[str, int]]:
    """只读聚合本地媒体运行状态，不读取路径、标题、哈希或错误正文。"""
    safe_owner = _local_media_owner(owner)
    with db.get_conn() as conn:
        source_row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,"
            "SUM(CASE WHEN enabled=1 THEN 0 ELSE 1 END) AS disabled,"
            "SUM(CASE WHEN mode='move' THEN 1 ELSE 0 END) AS move_mode,"
            "SUM(CASE WHEN mode='preview_only' THEN 1 ELSE 0 END) AS preview_only_mode,"
            "SUM(CASE WHEN enabled=1 AND NOT EXISTS ("
            "SELECT 1 FROM local_library_targets t WHERE t.source_id=s.id AND t.owner=s.owner"
            ") THEN 1 ELSE 0 END) AS enabled_without_targets "
            "FROM local_media_sources s WHERE owner=?",
            (safe_owner,),
        ).fetchone()
        task_row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN status='waiting_stable' THEN 1 ELSE 0 END) AS waiting_stable,"
            "SUM(CASE WHEN status IN ('recognizing','moving','verifying','refreshing','rolling_back') "
            "THEN 1 ELSE 0 END) AS active,"
            "SUM(CASE WHEN status='requires_manual' THEN 1 ELSE 0 END) AS requires_manual,"
            "SUM(CASE WHEN status='planned' THEN 1 ELSE 0 END) AS planned,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
            "SUM(CASE WHEN trigger='qb_completed' THEN 1 ELSE 0 END) AS qb_completed,"
            "SUM(CASE WHEN trigger='scan' THEN 1 ELSE 0 END) AS scan,"
            "SUM(CASE WHEN trigger='manual' THEN 1 ELSE 0 END) AS manual "
            "FROM local_media_tasks WHERE owner=?",
            (safe_owner,),
        ).fetchone()

    def counts(row, keys: tuple[str, ...]) -> dict[str, int]:
        return {key: max(0, int(row[key] or 0)) for key in keys}

    return {
        "sources": counts(
            source_row,
            (
                "total",
                "enabled",
                "disabled",
                "move_mode",
                "preview_only_mode",
                "enabled_without_targets",
            ),
        ),
        "tasks": counts(
            task_row,
            (
                "total",
                "waiting_stable",
                "active",
                "requires_manual",
                "planned",
                "failed",
                "completed",
                "qb_completed",
                "scan",
                "manual",
            ),
        ),
    }


def _local_media_age_bucket_sql(timestamp_expression: str) -> str:
    return (
        "CASE "
        f"WHEN {timestamp_expression}='' THEN 'unknown' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-1 hour') THEN 'under_1h' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-1 day') THEN '1h_to_24h' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-7 days') THEN '1d_to_7d' "
        "ELSE 'over_7d' END"
    )


def get_local_media_review_queue_summary(*, owner: str = "admin") -> dict[str, object]:
    """聚合待人工确认队列；只返回数量、触发来源和年龄分桶。"""
    safe_owner = _local_media_owner(owner)
    age_sql = _local_media_age_bucket_sql(
        "COALESCE(NULLIF(updated_at,''),created_at,'')"
    )
    with db.get_conn() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM local_media_tasks WHERE owner=? AND status='requires_manual'",
                (safe_owner,),
            ).fetchone()[0]
        )
        trigger_rows = conn.execute(
            "SELECT trigger,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status='requires_manual' GROUP BY trigger",
            (safe_owner,),
        ).fetchall()
        age_rows = conn.execute(
            f"SELECT {age_sql} AS age_bucket,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status='requires_manual' GROUP BY age_bucket",
            (safe_owner,),
        ).fetchall()
    return {
        "total": total,
        "by_trigger": {
            str(row["trigger"] or "unknown"): int(row["count"] or 0)
            for row in trigger_rows
        },
        "age_buckets": {
            str(row["age_bucket"]): int(row["count"] or 0) for row in age_rows
        },
    }


def get_local_media_history_summary(*, owner: str = "admin") -> dict[str, object]:
    """聚合本地媒体终态历史；不读取标题、路径、标识或错误正文。"""
    safe_owner = _local_media_owner(owner)
    timestamp_expression = (
        "COALESCE(NULLIF(completed_at,''),NULLIF(updated_at,''),created_at,'')"
    )
    age_sql = _local_media_age_bucket_sql(timestamp_expression)
    with db.get_conn() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM local_media_tasks WHERE owner=? AND status IN ('completed','failed')",
                (safe_owner,),
            ).fetchone()[0]
        )
        status_rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY status",
            (safe_owner,),
        ).fetchall()
        trigger_rows = conn.execute(
            "SELECT trigger,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY trigger",
            (safe_owner,),
        ).fetchall()
        age_rows = conn.execute(
            f"SELECT {age_sql} AS age_bucket,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY age_bucket",
            (safe_owner,),
        ).fetchall()
    return {
        "total": total,
        "by_status": {
            str(row["status"]): int(row["count"] or 0) for row in status_rows
        },
        "by_trigger": {
            str(row["trigger"] or "unknown"): int(row["count"] or 0)
            for row in trigger_rows
        },
        "age_buckets": {
            str(row["age_bucket"]): int(row["count"] or 0) for row in age_rows
        },
    }


def prepare_manual_local_media_task(
    source_id: int,
    content_path: str,
    *,
    snapshot_digest: str,
    owner: str = "admin",
    tmdb_id: str = "",
    media_type: str = "",
    rules_snapshot: str = "",
    season_override: int | None = None,
    episode_override: int | None = None,
    numbering_mode: str = "auto",
) -> int:
    """原子创建或重置可重试的手动任务；活动任务绝不被改回等待态。"""
    from app.modules.local_media_models import renew_local_media_operation_token

    if not str(snapshot_digest).startswith("preview:"):
        raise ValueError("缺少已确认的整理计划，请重新生成预览")
    safe_owner = _local_media_owner(owner)
    safe_path = _canonical_local_media_content_path(content_path)
    normalized_type = str(media_type or "").strip().lower()
    from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode

    raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
    if raw_numbering_mode not in NUMBERING_MODES:
        raise ValueError("剧集编号模式无效")
    normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if normalized_type and normalized_type not in {"movie", "tv"}:
        raise ValueError("媒体类型必须是 movie 或 tv")
    if season_override is not None:
        if isinstance(season_override, bool) or not isinstance(season_override, int):
            raise ValueError("季数必须是整数")
        if not 0 <= season_override <= 99:
            raise ValueError("季数超出允许范围")
    if episode_override is not None:
        if isinstance(episode_override, bool) or not isinstance(episode_override, int):
            raise ValueError("集数必须是整数")
        if not 1 <= episode_override <= 999:
            raise ValueError("集数超出允许范围")
    if normalized_type == "movie" and (
        season_override is not None or episode_override is not None
    ):
        raise ValueError("电影任务不能指定季数或集数")
    if normalized_type == "movie":
        normalized_numbering_mode = "auto"
    timestamp = db.now()
    token = renew_local_media_operation_token()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")
        active = _active_local_media_task_for_path(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
            content_path=safe_path,
        )
        if active is not None and active["status"] != "requires_manual":
            raise ValueError("该目录已有任务正在处理中")
        existing = active or _latest_terminal_local_media_task_for_path(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
            content_path=safe_path,
        )
        if existing and existing["status"] in {"failed", "requires_manual"}:
            if db.is_interrupted_local_media_write_error(existing["error"]):
                raise ValueError("上次本地整理在写入期间中断，请先核验文件并通过任务重试确认")
            token = renew_local_media_operation_token(existing["operation_token"])
            task_id = int(existing["id"])
            cur = conn.execute(
                "UPDATE local_media_tasks SET content_path=?,status='waiting_stable',"
                "stable_since='',snapshot_digest=?,"
                "recognition_summary='',rules_snapshot=?,tmdb_id=?,media_type=?,"
                "season_override=?,episode_override=?,numbering_mode=?,title='',year='',"
                "operation_token=?,confirmation_actor='',error='',warning='',completed_at=NULL,"
                "version=version+1,updated_at=? WHERE id=? AND owner=? AND status IN ('failed','requires_manual')",
                (
                    safe_path,
                    snapshot_digest,
                    str(rules_snapshot or ""),
                    str(tmdb_id or "").strip(),
                    normalized_type,
                    season_override,
                    episode_override,
                    normalized_numbering_mode,
                    token,
                    timestamp,
                    task_id,
                    safe_owner,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("任务状态已变化，请刷新后重试")
            conn.execute(
                "DELETE FROM local_media_task_items WHERE task_id=?",
                (task_id,),
            )
            return task_id
        cur = conn.execute(
            "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,trigger,status,"
            "operation_token,snapshot_digest,rules_snapshot,tmdb_id,media_type,season_override,episode_override,"
            "numbering_mode,created_at,updated_at) "
            "VALUES(?,?,NULL,?,'manual','waiting_stable',?,?,?,?,?,?,?,?,?,?)",
            (
                safe_owner,
                int(source_id),
                safe_path,
                token,
                snapshot_digest,
                str(rules_snapshot or ""),
                str(tmdb_id or "").strip(),
                normalized_type,
                season_override,
                episode_override,
                normalized_numbering_mode,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def claim_local_media_task(
    task_id: int,
    *,
    expected: str = "waiting_stable",
    next_status: str = "recognizing",
    owner: str = "admin",
) -> bool:
    from app.modules.local_media_models import LOCAL_TASK_STATUSES

    if expected not in LOCAL_TASK_STATUSES or next_status not in LOCAL_TASK_STATUSES:
        raise ValueError("不支持的本地媒体任务状态")
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE local_media_tasks SET status=?,attempts=attempts+1,version=version+1,"
            "confirmation_actor='',error='',updated_at=? WHERE id=? AND owner=? AND status=?",
            (next_status, timestamp, int(task_id), _local_media_owner(owner), expected),
        )
        return cur.rowcount == 1


def claim_local_media_confirmation_task(
    task_id: int,
    *,
    owner: str = "admin",
    expected_version: int,
    expected_snapshot_digest: str = "",
    confirmed_snapshot_digest: str,
    tmdb_id: str,
    media_type: str,
    rules_snapshot: str,
    season_override: int | None = None,
    episode_override: int | None = None,
    numbering_mode: str = "auto",
    title: str = "",
    year: str = "",
    confirmation_actor: str = "human",
) -> bool:
    """把仍然有效的本地待确认任务原子转换为执行态。"""
    if not confirmed_snapshot_digest or confirmed_snapshot_digest.startswith("preview:"):
        raise ValueError("本地媒体确认源快照无效，请重新检查")
    normalized_type = str(media_type or "").strip().lower()
    normalized_tmdb_id = str(tmdb_id or "").strip()
    from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode

    raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
    if raw_numbering_mode not in NUMBERING_MODES:
        raise ValueError("剧集编号模式无效")
    normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if not normalized_tmdb_id or normalized_type not in {"movie", "tv"}:
        raise ValueError("候选媒体参数无效")
    normalized_actor = str(confirmation_actor or "human").strip().lower()
    if normalized_actor not in {"human", "agent"}:
        raise ValueError("确认执行者无效")
    if isinstance(expected_version, bool) or int(expected_version) <= 0:
        raise ValueError("本地媒体任务版本无效")
    for value, minimum, maximum, label in (
        (season_override, 0, 99, "季数"),
        (episode_override, 1, 999, "集数"),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}超出允许范围")
    if normalized_type == "movie":
        season_override = None
        episode_override = None
        normalized_numbering_mode = "auto"

    where = "id=? AND owner=? AND status='requires_manual' AND version=?"
    params: list[object] = [
        confirmed_snapshot_digest,
        str(rules_snapshot or ""),
        normalized_tmdb_id,
        normalized_type,
        season_override,
        episode_override,
        normalized_numbering_mode,
        str(title or ""),
        str(year or ""),
        normalized_actor,
        db.now(),
        int(task_id),
        _local_media_owner(owner),
        int(expected_version),
    ]
    expected_digest = str(expected_snapshot_digest or "").strip()
    if expected_digest:
        where += " AND snapshot_digest=?"
        params.append(expected_digest)
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE local_media_tasks SET status='recognizing',attempts=attempts+1,"
            "snapshot_digest=?,recognition_summary='',rules_snapshot=?,tmdb_id=?,media_type=?,"
            "season_override=?,episode_override=?,numbering_mode=?,"
            "title=?,year=?,confirmation_actor=?,error='',warning='',completed_at=NULL,"
            "version=version+1,updated_at=? "
            f"WHERE {where}",
            params,
        )
        return cur.rowcount == 1


def reconcile_local_media_downloads(
    conn: sqlite3.Connection, *, task_id: int | None = None,
) -> int:
    """在任务事务内投影权威终态；启动时也用同一语句修复历史未收束请求。"""
    completed_at = "CASE WHEN t.status='planned' THEN NULL ELSE COALESCE(t.completed_at,t.updated_at) END"
    return conn.execute(
        "UPDATE download_requests AS r SET local_import_status=t.status,"
        "local_import_error=COALESCE(t.error,''),local_import_completed_at=" + completed_at + ","
        "updated_at=t.updated_at FROM local_media_tasks AS t "
        "WHERE t.id=CAST(substr(r.local_import_target,18) AS INTEGER) "
        "AND r.local_import_target='local-media-task:' || t.id "
        "AND COALESCE(r.status,'')!='cancelled' "
        "AND COALESCE(r.local_import_status,'') IN ('','pending','requires_manual','failed','planned') "
        "AND t.status IN ('completed','failed','requires_manual','planned') "
        "AND (r.local_import_status IS NOT t.status "
        "OR r.local_import_error IS NOT COALESCE(t.error,'') "
        "OR r.local_import_completed_at IS NOT (" + completed_at + "))"
        + (" AND t.id=?" if task_id is not None else ""),
        (task_id,) if task_id is not None else (),
    ).rowcount


def update_local_media_task(task_id: int, *, owner: str = "admin", **fields) -> bool:
    from app.modules.local_media_models import LOCAL_TASK_STATUSES

    if "content_path" in fields:
        raise ValueError("本地媒体任务路径只能通过原子准入接口设置")
    allowed = {
        "status",
        "stable_since",
        "snapshot_digest",
        "rules_snapshot",
        "recognition_summary",
        "tmdb_id",
        "media_type",
        "season_override",
        "episode_override",
        "numbering_mode",
        "title",
        "year",
        "error",
        "warning",
        "confirmation_actor",
        "completed_at",
    }
    sets: list[str] = []
    params: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "status" and value not in LOCAL_TASK_STATUSES:
            raise ValueError("不支持的本地媒体任务状态")
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.extend(["version=version+1", "updated_at=?"])
    params.extend([db.now(), int(task_id), _local_media_owner(owner)])
    with db.get_conn() as conn:
        cur = conn.execute(
            f"UPDATE local_media_tasks SET {', '.join(sets)} WHERE id=? AND owner=?",
            params,
        )
        if cur.rowcount == 1:
            reconcile_local_media_downloads(conn, task_id=int(task_id))
        return cur.rowcount == 1


def add_local_media_task_item(
    task_id: int,
    source_path: str,
    target_path: str = "",
    *,
    role: str = "metadata",
    media_group: str = "",
    action: str = "move",
    size: int = 0,
    mtime_ns: int = 0,
    device: int = 0,
    inode: int = 0,
    owner: str = "admin",
) -> int:
    safe_owner = _local_media_owner(owner)
    timestamp = db.now()
    with db.get_conn() as conn:
        task = conn.execute(
            "SELECT id FROM local_media_tasks WHERE id=? AND owner=?",
            (int(task_id), safe_owner),
        ).fetchone()
        if not task:
            raise LookupError("本地媒体任务不存在")
        conn.execute(
            "INSERT INTO local_media_task_items(task_id,owner,source_path,target_path,role,media_group,"
            "action,size,mtime_ns,device,inode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id,source_path) DO UPDATE SET target_path=excluded.target_path,"
            "role=excluded.role,media_group=excluded.media_group,action=excluded.action,size=excluded.size,"
            "mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,updated_at=excluded.updated_at",
            (
                int(task_id),
                safe_owner,
                str(source_path),
                str(target_path),
                str(role),
                str(media_group),
                str(action),
                max(0, int(size)),
                int(mtime_ns),
                int(device),
                int(inode),
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_media_task_items WHERE task_id=? AND source_path=? AND owner=?",
            (int(task_id), str(source_path), safe_owner),
        ).fetchone()
        return int(row["id"])


def list_local_media_task_items(
    task_id: int, *, owner: str = "admin"
) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM local_media_task_items WHERE task_id=? AND owner=? ORDER BY id",
            (int(task_id), _local_media_owner(owner)),
        ).fetchall()


def add_local_media_operation_step(
    task_id: int,
    operation_token: str,
    step_index: int,
    action: str,
    source_path: str = "",
    target_path: str = "",
    *,
    owner: str = "admin",
) -> int:
    safe_owner = _local_media_owner(owner)
    with db.get_conn() as conn:
        task = conn.execute(
            "SELECT id FROM local_media_tasks WHERE id=? AND owner=?",
            (int(task_id), safe_owner),
        ).fetchone()
        if not task:
            raise LookupError("本地媒体任务不存在")
        conn.execute(
            "INSERT INTO local_media_operation_steps(task_id,operation_token,step_index,action,"
            "source_path,target_path,status) VALUES(?,?,?,?,?,?,'pending') "
            "ON CONFLICT(task_id,operation_token,step_index) DO UPDATE SET "
            "action=excluded.action,source_path=excluded.source_path,target_path=excluded.target_path,"
            "status='pending',error='',started_at=NULL,finished_at=NULL",
            (
                int(task_id),
                str(operation_token),
                int(step_index),
                str(action),
                str(source_path),
                str(target_path),
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_media_operation_steps WHERE task_id=? AND operation_token=? AND step_index=?",
            (int(task_id), str(operation_token), int(step_index)),
        ).fetchone()
        return int(row["id"])


def update_local_media_operation_step(
    step_id: int,
    status: str,
    *,
    error: str = "",
) -> bool:
    safe_status = str(status or "").strip().lower()
    if safe_status not in {"pending", "running", "completed", "failed", "rolled_back"}:
        raise ValueError("不支持的本地媒体操作步骤状态")
    timestamp = db.now()
    assignments = ["status=?", "error=?"]
    params: list[object] = [safe_status, str(error or "")[:1000]]
    if safe_status == "running":
        assignments.append("started_at=COALESCE(started_at,?)")
        params.append(timestamp)
    if safe_status in {"completed", "failed", "rolled_back"}:
        assignments.append("finished_at=?")
        params.append(timestamp)
    params.append(int(step_id))
    with db.get_conn() as conn:
        cur = conn.execute(
            f"UPDATE local_media_operation_steps SET {', '.join(assignments)} WHERE id=?",
            params,
        )
        return cur.rowcount == 1


def list_local_media_operation_steps(
    task_id: int, *, owner: str = "admin"
) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT steps.* FROM local_media_operation_steps AS steps "
            "JOIN local_media_tasks AS tasks ON tasks.id=steps.task_id "
            "WHERE steps.task_id=? AND tasks.owner=? ORDER BY steps.step_index,steps.id",
            (int(task_id), _local_media_owner(owner)),
        ).fetchall()


def update_local_media_source(
    source_id: int, *, owner: str = "admin", **fields
) -> bool:
    allowed = {
        "name",
        "qb_profile",
        "qb_path_prefix",
        "local_root",
        "enabled",
        "media_type",
        "mode",
    }
    normalized_fields: list[tuple[str, object]] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "mode" and value not in {"move", "preview_only"}:
            raise ValueError("本地媒体来源仅支持 move 或 preview_only")
        if key == "media_type" and value not in {"auto", "movie", "tv", "nsfw"}:
            raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
        if key == "enabled":
            value = 1 if value else 0
        else:
            value = str(value or "").strip()
        if key in {"name", "local_root"} and not value:
            raise ValueError("本地媒体来源名称和路径不能为空")
        normalized_fields.append((key, value))
    if not normalized_fields:
        return False
    sets = [f"{key}=?" for key, _value in normalized_fields]
    params = [value for _key, value in normalized_fields]
    sets.append("updated_at=?")
    safe_owner = _local_media_owner(owner)
    params.extend([db.now(), int(source_id), safe_owner])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT name,qb_profile,qb_path_prefix,local_root,enabled,media_type,mode "
            "FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if current is None:
            return False
        operational_changed = any(
            key != "name" and current[key] != value for key, value in normalized_fields
        )
        if operational_changed and _local_media_source_has_active_task(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
        ):
            raise ValueError("来源仍有未完成任务，不能修改运行配置")
        cur = conn.execute(
            f"UPDATE local_media_sources SET {', '.join(sets)} WHERE id=? AND owner=?",
            params,
        )
        return cur.rowcount == 1


def delete_local_media_source(source_id: int, *, owner: str = "admin") -> bool:
    safe_owner = _local_media_owner(owner)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _local_media_source_has_active_task(
            conn,
            source_id=int(source_id),
            owner=safe_owner,
        ):
            raise ValueError("来源仍有未完成任务，不能删除")
        cur = conn.execute(
            "DELETE FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        )
        return cur.rowcount == 1


def reset_local_media_task(
    task_id: int,
    *,
    owner: str = "admin",
    tmdb_id: str | None = None,
    media_type: str | None = None,
    season_override: int | None = None,
    episode_override: int | None = None,
    preview_digest: str = "",
    rules_snapshot: str = "",
    numbering_mode: str | None = None,
    confirm_interrupted_write: bool = False,
    expected_version: int | None = None,
    expected_status: str | None = None,
) -> bool:
    """统一重试事务；Web 显式核验与 Agent 版本条件共用同一状态重置。"""
    from app.modules.local_media_models import renew_local_media_operation_token

    safe_status = None if expected_status is None else str(expected_status).strip().lower()
    if safe_status is not None and safe_status not in {"failed", "requires_manual"}:
        return False
    if expected_version is not None and (
        isinstance(expected_version, bool) or int(expected_version) < 1
    ):
        return False
    normalized_type = (
        None if media_type is None else str(media_type or "").strip().lower()
    )
    normalized_numbering_mode = None
    if numbering_mode is not None:
        from app.modules.episode_mapping import (
            NUMBERING_MODES,
            normalize_numbering_mode,
        )

        raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
        if raw_numbering_mode not in NUMBERING_MODES:
            raise ValueError("剧集编号模式无效")
        normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if normalized_type and normalized_type not in {"movie", "tv"}:
        raise ValueError("媒体类型必须是 movie 或 tv")
    for value, minimum, maximum, label in (
        (season_override, 0, 99, "季数"),
        (episode_override, 1, 999, "集数"),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}超出允许范围")
    if normalized_type == "movie" and (
        season_override is not None or episode_override is not None
    ):
        raise ValueError("电影任务不能指定季数或集数")
    if normalized_type == "movie":
        normalized_numbering_mode = "auto"
    if preview_digest and (not preview_digest.startswith("preview:") or not rules_snapshot):
        raise ValueError("季集纠偏缺少有效预览，请重新确认")
    assignments = [
        "status='waiting_stable'",
        "stable_since=''",
        "recognition_summary=''",
        "title=''",
        "year=''",
        "operation_token=?",
        "confirmation_actor=''",
        "error=''",
        "warning=''",
        "completed_at=NULL",
        "version=version+1",
        "updated_at=?",
    ]
    params: list[object] = ["", db.now()]
    if preview_digest:
        assignments.extend(["snapshot_digest=?", "rules_snapshot=?"])
        params.extend([preview_digest, rules_snapshot])
    else:
        assignments.append("snapshot_digest=CASE WHEN snapshot_digest LIKE 'preview:%' THEN snapshot_digest ELSE '' END")
    if tmdb_id is not None:
        assignments.append("tmdb_id=?")
        params.append(str(tmdb_id or "").strip())
    if normalized_type is not None:
        assignments.append("media_type=?")
        params.append(normalized_type)
        if normalized_type == "movie":
            assignments.extend(["season_override=NULL", "episode_override=NULL"])
    if season_override is not None:
        assignments.append("season_override=?")
        params.append(season_override)
    if episode_override is not None:
        assignments.append("episode_override=?")
        params.append(episode_override)
    if normalized_numbering_mode is not None:
        assignments.append("numbering_mode=?")
        params.append(normalized_numbering_mode)
    safe_owner = _local_media_owner(owner)
    params.extend([int(task_id), safe_owner])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status,error,version,operation_token FROM local_media_tasks WHERE id=? AND owner=?",
            (int(task_id), safe_owner),
        ).fetchone()
        if current is None or str(current["status"] or "") not in {
            "failed",
            "requires_manual",
        }:
            return False
        if (
            expected_version is not None and int(current["version"]) != int(expected_version)
        ) or (safe_status is not None and current["status"] != safe_status):
            return False
        if db.is_interrupted_local_media_write_error(current["error"]) and not bool(
            confirm_interrupted_write
        ):
            return False
        params[0] = renew_local_media_operation_token(current["operation_token"])
        cur = conn.execute(
            f"UPDATE local_media_tasks SET {', '.join(assignments)} "
            "WHERE id=? AND owner=? AND status IN ('failed','requires_manual')",
            params,
        )
        if cur.rowcount != 1:
            return False
        # 新 attempt 不得继承旧计划，否则刷新范围和可见性核验会读到过期路径。
        conn.execute(
            "DELETE FROM local_media_task_items WHERE task_id=?",
            (int(task_id),),
        )
        return True


def reset_local_media_task_if_current(
    task_id: int,
    *,
    owner: str = "admin",
    expected_version: int,
    expected_status: str,
    confirm_interrupted_write: bool = False,
    season_override: int | None = None,
    episode_override: int | None = None,
    preview_digest: str = "",
    rules_snapshot: str = "",
) -> bool:
    """以强制版本条件委托统一重试事务，可选写入明确季集映射。"""
    return reset_local_media_task(
        task_id,
        owner=owner,
        expected_version=0 if expected_version is None else expected_version,
        expected_status=str(expected_status or ""),
        confirm_interrupted_write=confirm_interrupted_write,
        season_override=season_override,
        episode_override=episode_override,
        preview_digest=preview_digest,
        rules_snapshot=rules_snapshot,
    )


def _recover_after_restart(conn, timestamp: str, *, writer_available: bool) -> None:
    """只在调用方持有 writer 锁时恢复任务；关联请求始终投影权威状态。"""
    if writer_available:
        conn.execute(
            "UPDATE local_media_tasks SET status='failed',"
            "error=CASE WHEN COALESCE(error,'')='' THEN ? ELSE error END,"
            "completed_at=COALESCE(completed_at,?),updated_at=? "
            "WHERE status IN ('recognizing','planned')",
            (db._LOCAL_MEDIA_INTERRUPTED_PREWRITE_ERROR, timestamp, timestamp),
        )
        conn.execute(
            "UPDATE local_media_tasks SET status='requires_manual',"
            "error=CASE WHEN COALESCE(error,'')='' THEN ? "
            "ELSE ? || '；原错误：' || substr(error,1,350) END,"
            "completed_at=NULL,updated_at=? "
            "WHERE status IN ('moving','verifying','refreshing','rolling_back')",
            (
                f"{db.LOCAL_MEDIA_INTERRUPTED_WRITE_ERROR_PREFIX}，文件及 qB 状态需人工核验",
                db.LOCAL_MEDIA_INTERRUPTED_WRITE_ERROR_PREFIX,
                timestamp,
            ),
        )
        conn.execute(
            "UPDATE local_media_operation_steps SET status='failed',"
            "error=CASE WHEN COALESCE(error,'')='' THEN "
            "'进程中断，步骤结果需要人工核验' ELSE error END,"
            "finished_at=COALESCE(finished_at,?) WHERE status='running'",
            (timestamp,),
        )
    reconcile_local_media_downloads(conn)


# 在函数定义后绑定门面，兼容 repository-first 导入；运行期始终使用同一连接/时钟所有者。
from app import database as db  # noqa: E402
