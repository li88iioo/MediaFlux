"""本轮历史 probe 缓存恢复、单批读取一致性与行读取成本。"""

from __future__ import annotations

import tests  # noqa: F401
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import database as db
from app.repositories import media_probe as cache
from tests.support import isolated_test_database


@pytest.fixture(autouse=True)
def no_network():
    with (
        patch(
            "socket.socket.connect",
            side_effect=AssertionError("real network forbidden"),
        ),
        patch(
            "socket.create_connection",
            side_effect=AssertionError("real network forbidden"),
        ),
    ):
        yield


@pytest.mark.parametrize("reader", ["single", "batch"])
@pytest.mark.parametrize("exact", [False, True])
def test_history_valid_fingerprint_cache_survives_newer_corrupt_rows(reader, exact):
    with isolated_test_database():
        good = json.dumps(
            {
                "resolution": "1080p",
                "video_codec": "H.264",
                "video_bitrate_bps": 8000000,
            }
        )
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_probe_cache(file_id,etag,size,payload,updated_at) VALUES(?,?,?,?,?)",
                [
                    ("old-good", "same-content", 100, good, "2025-01-01 00:00:00"),
                    (
                        "new-corrupt",
                        "same-content",
                        100,
                        "{truncated",
                        "2026-01-01 00:00:00",
                    ),
                    *(
                        (("wanted", "same-content", 100, "[]", "2026-02-01 00:00:00"),)
                        if exact
                        else ()
                    ),
                ],
            )
        key = ("wanted", "same-content", 100)
        if reader == "single":
            result = cache.get_media_probe_cache(*key, allow_fingerprint_fallback=True)
        else:
            result = cache.get_media_probe_cache_many(
                [key], allow_fingerprint_fallback=True
            ).get(key)
        assert result == good, "损坏的新记录不得遮蔽同内容的有效历史缓存"
        from app.modules.media_probe import media_profile_from_cache

        assert result is not None
        media_profile_from_cache_result = media_profile_from_cache(result)
        assert media_profile_from_cache_result is not None
        assert media_profile_from_cache_result.resolution == "1080p"


@contextmanager
def count_fingerprint_rows():
    """计数实际取出的 fallback 行，不把 SQL 条数冒充结果集读取成本。"""
    original = db.get_conn
    counter = {"rows": 0}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            for row in self.cursor:
                counter["rows"] += 1
                yield row

        def close(self):
            self.cursor.close()

        def fetchall(self):
            rows = self.cursor.fetchall()
            counter["rows"] += len(rows)
            return rows

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            cursor = self.conn.execute(sql, parameters)
            if "WHERE etag=? AND size=?" in sql:
                return Cursor(cursor)
            return cursor

    @contextmanager
    def connection():
        with original() as conn:
            yield Connection(conn)

    with patch.object(db, "get_conn", side_effect=connection):
        yield counter


@pytest.mark.parametrize("reader", ["single", "batch"])
def test_history_fingerprint_reads_stop_at_first_valid_result(reader):
    with isolated_test_database():
        payload = json.dumps({"resolution": "1080p"})
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_probe_cache(file_id,etag,size,payload,updated_at) VALUES(?,?,?,?,?)",
                (
                    (f"alias-{n:05d}", "same", 100, payload, "2026-01-01 00:00:00")
                    for n in range(2000)
                ),
            )
        with count_fingerprint_rows() as counter:
            key = ("new-id", "same", 100)
            if reader == "single":
                result = cache.get_media_probe_cache(
                    *key, allow_fingerprint_fallback=True
                )
            else:
                result = cache.get_media_probe_cache_many(
                    [key], allow_fingerprint_fallback=True
                )[key]
        assert result == payload
        assert counter["rows"] == 1, counter


def test_history_missing_fingerprint_preserves_exact_failure_backoff():
    with isolated_test_database():
        failure = json.dumps(
            {"_media_probe_cache": "failure", "retry_after": 9999999999}
        )
        cache.upsert_media_probe_cache("file", "", 20, failure)
        key = ("file", "", 20)
        assert (
            cache.get_media_probe_cache(*key, allow_fingerprint_fallback=True)
            == failure
        )
        assert cache.get_media_probe_cache_many(
            [key], allow_fingerprint_fallback=True
        ) == {key: failure}
