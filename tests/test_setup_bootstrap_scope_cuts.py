"""目标身份、精确 fsync 失败与真实 SQLite checkpoint 竞争。"""
import errno
import json
from contextlib import closing
import sqlite3

import pytest

from tests.test_setup_bootstrap import cold_run, initialize


def _scope_action(cut):
    from app.modules import backup
    from app import database
    from app.runtime_paths import get_runtime_paths
    from tests.test_setup_bootstrap_support import initialize_paths
    paths = get_runtime_paths()
    original_sync, original_init = backup._fsync_directory, database.init_db
    retained = []
    def sync(directory):
        if cut in ('db_fsync', 'db_fsync_unsupported') and directory == paths.database_path.parent and paths.database_path.exists():
            raise OSError(errno.EINVAL if cut == 'db_fsync_unsupported' else errno.EIO, 'injected strict DB directory fsync failure')
        original_sync(directory)
    def initialize_stage(*, default_notification_chat_id: str = ""):
        original_init(default_notification_chat_id=default_notification_chat_id)
        if cut == 'env_cas_late':
            paths.env_file.write_bytes(b'KEEP=competitor\n')
        elif cut == 'checkpoint_busy':
            conn = sqlite3.connect(database.resolve_db_path())
            conn.execute('BEGIN')
            conn.execute('SELECT count(*) FROM settings_kv').fetchone()
            retained.append(conn)
    backup._fsync_directory, database.init_db = sync, initialize_stage
    try:
        try:
            initialize_paths(paths)
        except Exception:  # noqa: BLE001 - 故障注入边界需要检查任意失败后的状态
            assert database.DB_PATH == paths.database_path
            return 2
        return 0
    finally:
        for conn in retained:
            conn.close()


@pytest.mark.parametrize('cut', ['env_cas_late', 'db_fsync', 'db_fsync_unsupported', 'checkpoint_busy'])
def test_selected_scope_and_durability_controls(tmp_path, cut):
    if cut == 'env_cas_late':
        (tmp_path / 'config').mkdir()
        (tmp_path / 'config/user.env').write_text('KEEP=value\n')
    assert cold_run(tmp_path, _scope_action, cut) == {'result': 2}
    if cut in ('db_fsync', 'db_fsync_unsupported'):
        with closing(sqlite3.connect(tmp_path / 'db/mediaflux.db')) as conn, conn:
            assert json.loads(conn.execute("SELECT value FROM settings_kv WHERE key='agent.runtime_fence.v1'").fetchone()[0])['phase'] == 'changing'
        assert initialize(tmp_path) == {'result': 2}
    else:
        assert not (tmp_path / 'db/mediaflux.db').exists()
    if cut == 'env_cas_late':
        assert (tmp_path / 'config/user.env').read_bytes() == b'KEEP=competitor\n'
    else:
        assert not (tmp_path / 'config/user.env').exists()
