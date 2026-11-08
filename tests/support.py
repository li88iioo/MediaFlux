"""MediaFlux 测试共享辅助工具。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator, NotRequired, TypedDict, TypeVar
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.scraper import Candidate, MatchResult

_TEST_DATABASE_LOCK = threading.RLock()


class FakeJsonResponse:
    """仅提供 JSON HTTP 响应，不依赖 Web 应用或运行时初始化。"""

    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.text = self._content.decode("utf-8")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class FakeHttpSession:
    """按队列返回响应，同时记录请求方法、地址与参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


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
             timestamp if status == "failed" else None, entry_id),
        )


@contextmanager
def isolated_test_database(filename: str = "test.db") -> Generator[Path, None, None]:
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


@contextmanager
def initialized_web_runtime(paths=None):
    """Opt-in real installation for ordinary, already-installed Web controls.

    Never use for fresh-installation, recovery, or changing-fence tests. The
    operation creates physical storage through the single installation transaction, not
    through test-mode schema/fence shortcuts.
    """
    from contextlib import ExitStack
    from app import config, runtime_paths
    from app.modules import first_run
    from tests.test_setup_bootstrap_support import initialize_paths

    previous_db = db.DB_PATH
    previous_mode = db._configured_test_mode
    previous_paths = runtime_paths._configured_paths
    with ExitStack() as stack:
        if paths is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="installed-web-")))
            paths = runtime_paths.RuntimePaths(
                program_dir=config.PATHS.program_dir,
                data_dir=root / "data", config_dir=root / "config",
                cache_dir=root / "cache", log_dir=root / "logs",
                strm_dir=root / "strm", trash_dir=root / "trash",
            )
        stack.callback(db.configure_database, previous_db, test_mode=previous_mode)
        stack.callback(runtime_paths.configure_runtime_paths, previous_paths)
        stack.enter_context(patch.object(config, "PATHS", paths))
        stack.enter_context(patch.object(config, "ENV_FILE", paths.env_file))
        stack.enter_context(patch.object(config, "_cache", None))
        stack.enter_context(patch.dict(os.environ, {"MEDIAFLUX_TEST_MODE": "0", "MEDIAFLUX_TEST_DB_PATH": ""}))
        stack.enter_context(patch.object(first_run, "_startup_state", None))
        runtime_paths.configure_runtime_paths(paths)
        credentials = {
            "ENV_WEB_PASSPORT": os.environ.get("ENV_WEB_PASSPORT", "admin"),
            "ENV_WEB_PASSWORD": os.environ.get("ENV_WEB_PASSWORD", "123456"),
            "WEB_SECRET_KEY": os.environ.get("WEB_SECRET_KEY", "test-secret"),
        }
        # 本夹具拥有这些安装输入；其它测试 reload(config) 留下的快照不能把它们误判为部署覆盖。
        stack.enter_context(patch.object(
            config, "_STARTUP_ENV_OVERRIDES",
            config._STARTUP_ENV_OVERRIDES.difference(credentials, {"MEDIAFLUX_INITIALIZED"}),
        ))
        with patch.dict(os.environ):
            # 仅隔离本次夹具的安装输入；退出 patch 后恢复外部实例环境。
            for key in (*credentials, "MEDIAFLUX_INITIALIZED"):
                os.environ.pop(key, None)
            os.environ["MEDIAFLUX_TEST_MODE"] = "0"
            os.environ.pop("MEDIAFLUX_TEST_DB_PATH", None)
            settings = dict(credentials, ENV_WEB_PASSWORD="fixture-install-password", AGENT_ENABLED="1")
            initialize_paths(paths, settings)
            first_run._reset_startup_state_for_tests()
            # Preserve legacy login credentials using the real controlled
            # config publisher after the installation has completed.
            config.set_and_save(credentials)
        db.configure_database(paths.database_path, test_mode=False)
        first_run._reset_startup_state_for_tests()
        try:
            yield paths
        finally:
            first_run._startup_state = None


class InitializedWebTestCase(unittest.TestCase):
    """每个测试类使用真实初始化的独立安装，不能依赖其它用例先创建 schema。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # class cleanup 在子类 setUpClass 失败时同样执行；不遗留安装目录或配置。
        cls.enterClassContext(initialized_web_runtime())


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


class ReleaseParseFields(TypedDict):
    title: str
    year: str
    type: str
    season: int | None
    episode: int | None
    tmdb_id: NotRequired[str]


def release_parse_result(
    fields: dict[str, Any] | None = None,
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


def release_parse_fields(result) -> ReleaseParseFields:
    """把正式识别结果转为仅供测试断言使用的字段快照。"""
    payload: ReleaseParseFields = {
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

    def list_dir(self, directory_id: str, /) -> list[GuangYaFile]:
        raise NotImplementedError

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


_ReceiptT = TypeVar("_ReceiptT")

def assert_present(value: _ReceiptT | None) -> _ReceiptT:
    """验证正向测试中的回执存在，允许在推导式中保留这一断言。"""
    assert value is not None
    return value


class _ParsingScraper:
    @staticmethod
    def _fields(filename: str) -> dict[str, object]:
        tmdb = re.search(r"(?i)tmdb-(\d+)", filename)
        season_episode = re.search(r"(?i)S(\d{1,2})E(\d{1,3})", filename)
        if season_episode:
            return {
                "type": "tv",
                "title": re.split(r"(?i)[._ -]S\d{1,2}E\d{1,3}", filename)[0],
                "season": int(season_episode.group(1)),
                "episode": int(season_episode.group(2)),
                "tmdb_id": tmdb.group(1) if tmdb else "",
            }
        stem = filename.rsplit(".", 1)[0]
        title = re.sub(r"[._ -]+(?:19|20)\d{2}.*$", "", stem)
        return {
            "type": "movie", "title": title, "season": None, "episode": None,
            "tmdb_id": tmdb.group(1) if tmdb else "",
        }

    @classmethod
    def parse_media(cls, filename: str, parent_path: str = "", match=None, *, filename_only=False):
        return release_parse_result(
            cls._fields(filename), filename=filename, parent_path=parent_path
        )



class _DirectoryScrapeTMDB:
    confirm: Mock
    api_key = "tmdb-test-key"

    def __init__(self):
        self.search_calls: list[tuple[str, str, str]] = []
        self.detail_calls: list[tuple[str, str]] = []
        self.auto_result = MatchResult(
            tmdb_id="1726",
            title="钢铁侠",
            year="2008",
            media_type="movie",
            confidence=0.96,
            status="matched",
            matched_by="search",
            threshold=0.9,
        )

    def search_candidates(
        self,
        query: str,
        year: str = "",
        media_type: str = "movie",
    ) -> list[Candidate]:
        self.search_calls.append((query, year, media_type))
        if media_type == "tv":
            return [
                Candidate(
                    "123",
                    "钢铁侠：装甲冒险",
                    "2009",
                    0.81,
                    "tv",
                    original_title="Iron Man: Armored Adventures",
                )
            ]
        return [
            Candidate(
                "1726",
                "钢铁侠",
                "2008",
                0.96,
                "movie",
                original_title="Iron Man",
                overview="托尼·斯塔克制造钢铁战甲。",
                poster_path="/movie.jpg",
                backdrop_path="/movie-bg.jpg",
                release_date="2008-04-30",
            )
        ]

    def get_detail_with_credits(self, tmdb_id: str, media_type: str) -> dict:
        self.detail_calls.append((tmdb_id, media_type))
        if media_type == "tv":
            return {
                "id": int(tmdb_id),
                "name": "钢铁侠：装甲冒险",
                "original_name": "Iron Man: Armored Adventures",
                "first_air_date": "2009-04-24",
                "overview": "少年托尼·斯塔克的装甲冒险。",
                "vote_average": 7.2,
                "poster_path": "/tv.jpg",
                "backdrop_path": "/tv-bg.jpg",
                "genres": [{"name": "动画"}],
                "created_by": [{"name": "Craig Kyle"}],
                "credits": {"cast": [{"name": "Adrian Petriw"}], "crew": []},
            }
        return {
            "id": int(tmdb_id),
            "title": "钢铁侠",
            "original_title": "Iron Man",
            "release_date": "2008-04-30",
            "overview": "托尼·斯塔克制造钢铁战甲。",
            "vote_average": 7.6,
            "poster_path": "/movie.jpg",
            "backdrop_path": "/movie-bg.jpg",
            "genres": [{"name": "动作"}, {"name": "科幻"}],
            "credits": {
                "cast": [
                    {"name": "小罗伯特·唐尼"},
                    {"name": "格温妮斯·帕特洛"},
                ],
                "crew": [{"name": "乔恩·费儒", "job": "Director"}],
            },
        }

    def match_from_tmdb(self, tmdb_id: str, media_type: str) -> MatchResult:
        detail = self.get_detail_with_credits(tmdb_id, media_type)
        title = detail.get("name") or detail.get("title") or ""
        date = detail.get("first_air_date") or detail.get("release_date") or ""
        return MatchResult(
            tmdb_id=tmdb_id,
            title=title,
            year=str(date)[:4],
            media_type=media_type,
            confidence=1.0,
            locked=True,
            status="matched",
            matched_by="tmdb_id",
            threshold=1.0,
        )

    def parse_media(self, filename: str, parent_path: str = "", match=None, *, filename_only=False):
        return release_parse_result(
            _ParsingScraper._fields(filename),
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str, *, force_refresh=False) -> dict:
        return self.get_detail_with_credits(tmdb_id, media_type)

    def match(self, _filename: str, _parent_path: str = "") -> MatchResult:
        return self.auto_result


def _prepare_batch(_service, result_ids, target, *, scope):
    assert scope == "agent"
    return {"ok": True, "status": "confirmation_required", "summary": "等待整批确认", "data": {
        "count": len(result_ids), "target": target,
        "resources": [{"result_id": result_id, "title": str(result_id)} for result_id in result_ids],
    }}, {"target": target, "resources": [{"result_id": identity, "target": target} for identity in result_ids]}


@contextmanager
def isolated_indexer_results():
    """候选链只使用内存结果仓库，不启动真实索引器客户端或线程。"""
    from app.indexers.result_store import IndexerResultStore

    results = IndexerResultStore()
    service = SimpleNamespace(
        result_store=results,
        get_result=lambda identity, **_kwargs: results.get(identity),
    )
    with (
        patch("app.indexers.runtime.get_indexer_service", return_value=service),
        patch("app.agent.kernel.ports.sdk_effects.get_indexer_service", return_value=service),
    ):
        yield service
