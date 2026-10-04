"""MediaFlux 测试共享辅助工具。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from app import database as db

_TEST_DATABASE_LOCK = threading.RLock()


def seed_rss_entry_state(entry_id: int, status: str) -> None:
    """仅为历史/迁移测试造状态；生产受理由下载请求事务投影，不能调用此夹具。"""
    processed = status in {"downloaded", "skipped"}
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE rss_entries SET status=?,processed=?,processed_at=?,submitted_at=?,"
            "failed_at=?,failure_code='',failure_retryable=0 WHERE id=?",
            (status, int(processed), timestamp if processed else None,
             timestamp if status in {"submitting", "downloaded", "failed"} else None,
             timestamp if status == "failed" else None, int(entry_id)),
        )


@contextmanager
def isolated_test_database(filename: str = "test.db") -> Iterator[Path]:
    """初始化临时 SQLite，并在退出时恢复此前的数据库配置。"""
    with _TEST_DATABASE_LOCK:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-test-db-") as root:
            test_path = Path(root) / Path(filename).name
            configured = db.configure_database(test_path, test_mode=True)
            try:
                db.init_db()
                yield configured
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)


class InitializedWebTestCase(unittest.TestCase):
    """显式提供已初始化 Web 测试环境，不依赖 tests 包导入副作用。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._initialized_web_env = patch.dict(
            os.environ,
            {
                "MEDIAFLUX_INITIALIZED": "1",
                "ENV_WEB_PASSPORT": "admin",
                "ENV_WEB_PASSWORD": "123456",
                "WEB_SECRET_KEY": "test-secret",
                # API 合同测试不得继承开发者本机 user.env 中的 Agent 开关。
                "AGENT_ENABLED": "1",
            },
            clear=False,
        )
        cls._initialized_web_env.start()
        from app.modules import first_run

        first_run._reset_startup_state_for_tests()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._initialized_web_env.stop()
            from app.modules import first_run

            first_run._reset_startup_state_for_tests()
        finally:
            super().tearDownClass()


class IsolatedDatabaseTestCase(InitializedWebTestCase):
    """为整个测试类提供一个已初始化且隔离的 SQLite 数据库。"""

    test_db_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._test_database_context = isolated_test_database(f"{cls.__name__}.db")
        cls.test_db_path = cls._test_database_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._test_database_context.__exit__(None, None, None)
        finally:
            super().tearDownClass()


def release_parse_result(
    fields: dict[str, object] | None = None,
    *,
    filename: str = "",
    parent_path: str = "",
    source_season: int | None = None,
    source_episode: int | None = None,
):
    """为测试替身构造正式的统一识别结果。"""
    from app.modules.scraper import RecognitionContext, ReleaseParseResult

    values = dict(fields or {})
    season = values.get("season")
    episode = values.get("episode")
    effective_season = season
    effective_episode = episode
    source_season = effective_season if source_season is None else source_season
    source_episode = effective_episode if source_episode is None else source_episode
    media_type = str(values.get("type") or values.get("media_type") or "movie")
    title = str(values.get("title") or "")
    year = str(values.get("year") or "")
    context = RecognitionContext(
        filename=filename,
        parent_path=parent_path,
        normalized_title=title,
        filename_title=title,
        filename_year=year,
        media_type=media_type,
        season=source_season,
        episode=source_episode,
    )
    return ReleaseParseResult(
        filename=filename,
        parent_path=parent_path,
        title=title,
        year=year,
        media_type=media_type,
        tmdb_id=str(values.get("tmdb_id") or ""),
        source_season=source_season,
        source_episode=source_episode,
        effective_season=effective_season,
        effective_episode=effective_episode,
        context=context,
    )


def release_parse_fields(result) -> dict[str, object]:
    """把正式识别结果转为仅供测试断言使用的字段快照。"""
    payload: dict[str, object] = {
        "title": result.title,
        "year": result.year,
        "type": result.media_type,
        "season": result.effective_season,
        "episode": result.effective_episode,
    }
    if result.tmdb_id:
        payload["tmdb_id"] = result.tmdb_id
    return payload


class PagedDirectoryTestMixin:
    """让内存目录替身显式实现正式分页协议；兼容适配只存在于测试端。"""

    def iter_dir(self, directory_id: str, *, should_stop=None, max_items=None):
        if should_stop and should_stop():
            return
        from app.clients.guangya import DirectoryEntryLimitError

        for index, item in enumerate(self.list_dir(directory_id)):
            if should_stop and should_stop():
                return
            if max_items is not None and index >= max_items:
                raise DirectoryEntryLimitError("测试目录超过调用方条目预算")
            yield item
