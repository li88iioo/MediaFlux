"""媒体探测缓存的数据访问。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试数据库与连接补丁兼容。"""
    from app import database

    return database


_MEDIA_PROFILE_FIELDS = frozenset({
    "resolution",
    "dynamic_range",
    "video_codec",
    "bit_depth",
    "fps",
    "audio_codec",
    "audio_channels",
    "source",
    # 仅用于读取升级前的成功缓存；当前 MediaProfile 不再生成码率命名字段。
    "video_bitrate_bps",
    "overall_bitrate_bps",
    "bitrate_source",
    "dolby_vision",
    "atmos",
})


def _decode_payload(payload: str) -> dict | None:
    try:
        data = json.loads(str(payload or ""))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_failure_payload(payload: str) -> bool:
    """失败缓存只属于具体文件版本，不能跨 file_id 扩散。"""
    data = _decode_payload(payload)
    return bool(data and data.get("_media_probe_cache") == "failure")


def _is_success_payload(payload: str) -> bool:
    """识别可由 MediaProfile 恢复的成功缓存，避免把损坏数据当作成功。"""
    data = _decode_payload(payload)
    return bool(
        data is not None
        and data.get("_media_probe_cache") != "failure"
        and set(data).issubset(_MEDIA_PROFILE_FIELDS)
    )


def get_media_probe_cache(
    file_id: str, etag: str, size: int, *, allow_fingerprint_fallback: bool = False
) -> str:
    """按文件版本读取；云盘调用方可显式复用相同内容指纹的成功缓存。"""
    normalized_file_id = str(file_id)
    normalized_etag = str(etag or "")
    normalized_size = int(size or 0)
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM media_probe_cache WHERE file_id=? AND etag=? AND size=?",
            (normalized_file_id, normalized_etag, normalized_size),
        ).fetchone()
        exact_payload = str(row["payload"] or "") if row else ""
        if exact_payload and (
            not allow_fingerprint_fallback
            or not normalized_etag
            or not _is_failure_payload(exact_payload)
        ):
            return exact_payload
        if not allow_fingerprint_fallback or not normalized_etag:
            return exact_payload
        rows = conn.execute(
            "SELECT payload FROM media_probe_cache WHERE etag=? AND size=? "
            "ORDER BY updated_at DESC, file_id DESC",
            (normalized_etag, normalized_size),
        ).fetchall()
        for candidate in rows:
            payload = str(candidate["payload"] or "")
            if payload and not _is_failure_payload(payload):
                return payload
        return exact_payload


def get_media_probe_cache_many(
    versions: list[tuple[str, str, int]],
    *,
    allow_fingerprint_fallback: bool = False,
) -> dict[tuple[str, str, int], str]:
    """批量读取缓存；云盘调用方可显式按内容指纹复用成功结果。"""
    requested = {
        (str(file_id), str(etag or ""), int(size or 0))
        for file_id, etag, size in versions
        if str(file_id or "").strip()
    }
    if not requested:
        return {}

    file_ids = sorted({item[0] for item in requested})
    result: dict[tuple[str, str, int], str] = {}
    exact_failures: dict[tuple[str, str, int], str] = {}
    with _database().get_conn() as conn:
        # SQLite 默认变量上限在不同发行版间存在差异，保守分块避免超限。
        for offset in range(0, len(file_ids), 400):
            chunk = file_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT file_id,etag,size,payload FROM media_probe_cache "
                f"WHERE file_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                key = (
                    str(row["file_id"] or ""),
                    str(row["etag"] or ""),
                    int(row["size"] or 0),
                )
                payload = str(row["payload"] or "")
                if key in requested and payload:
                    if allow_fingerprint_fallback and _is_failure_payload(payload):
                        exact_failures[key] = payload
                    else:
                        result[key] = payload

        if not allow_fingerprint_fallback:
            return result

        unresolved = [key for key in requested if key not in result and key[1]]
        fingerprints = sorted({(key[1], key[2]) for key in unresolved})
        fallback_payloads: dict[tuple[str, int], str] = {}
        # 每个内容指纹单独沿复合索引按新到旧扫描。相比多 fingerprint 的
        # OR + 全局 ORDER BY，这不会构造临时排序表，也能在首个成功缓存处停止。
        for etag, size in fingerprints:
            rows = conn.execute(
                "SELECT payload FROM media_probe_cache WHERE etag=? AND size=? "
                "ORDER BY updated_at DESC, file_id DESC",
                (etag, size),
            ).fetchall()
            for row in rows:
                payload = str(row["payload"] or "")
                if payload and not _is_failure_payload(payload):
                    fallback_payloads[(etag, size)] = payload
                    break
        for key in unresolved:
            payload = fallback_payloads.get((key[1], key[2]), "")
            if payload:
                result[key] = payload
            elif key in exact_failures:
                result[key] = exact_failures[key]
    return result


def upsert_media_probe_cache(file_id: str, etag: str, size: int, payload: str) -> None:
    database = _database()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO media_probe_cache(file_id,etag,size,payload,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET etag=excluded.etag,size=excluded.size,"
            "payload=excluded.payload,updated_at=excluded.updated_at",
            (
                str(file_id), str(etag or ""), int(size or 0), str(payload), database.now(),
            ),
        )


def upsert_media_probe_failure_cache(
    file_id: str, etag: str, size: int, payload: str
) -> bool:
    """原子写入失败缓存；同一文件版本的成功结果拥有永久优先级。"""
    database = _database()
    normalized_file_id = str(file_id)
    normalized_etag = str(etag or "")
    normalized_size = int(size or 0)
    normalized_payload = str(payload)
    with database.get_conn() as conn:
        # 锁住 read-check-write 窗口，防止迟到失败覆盖刚完成的成功探测。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT etag,size,payload FROM media_probe_cache WHERE file_id=?",
            (normalized_file_id,),
        ).fetchone()
        if (
            row
            and str(row["etag"] or "") == normalized_etag
            and int(row["size"] or 0) == normalized_size
            and _is_success_payload(str(row["payload"] or ""))
        ):
            return False
        conn.execute(
            "INSERT INTO media_probe_cache(file_id,etag,size,payload,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET etag=excluded.etag,size=excluded.size,"
            "payload=excluded.payload,updated_at=excluded.updated_at",
            (
                normalized_file_id, normalized_etag, normalized_size,
                normalized_payload, database.now(),
            ),
        )
        return True
