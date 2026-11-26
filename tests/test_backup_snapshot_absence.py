"""完整快照缺席状态必须参与同一个可恢复事务；历史/部分备份不得推断删除。"""
import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.modules import backup
from tests.test_backup import make_paths

TOKEN = "data/guangya_token.json"


def seed(root, value, *, token=None, env=True):
    paths = make_paths(root)
    paths.ensure_writable_dirs()
    with sqlite3.connect(paths.database_path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES (?)", (value,))
    if env:
        paths.env_file.write_text(f"SNAPSHOT='{value}'\n")
    if token is not None:
        paths.token_file.write_text(token)
    return paths


def state(paths):
    with sqlite3.connect(paths.database_path) as conn:
        value = conn.execute("SELECT value FROM sample").fetchone()[0]
    return value, paths.env_file.read_text(), paths.token_file.read_text() if paths.token_file.exists() else None


def rewrite_manifest(archive, update):
    with zipfile.ZipFile(archive) as source:
        payloads = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(payloads["manifest.json"])
    update(manifest)
    payloads["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w") as target:
        for name, payload in payloads.items():
            target.writestr(name, payload)


@pytest.mark.parametrize("old_token", [None, "later-token"])
def test_full_snapshot_restores_token_absence_and_leaves_other_files_alone(tmp_path, old_token):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token=old_token)
    unrelated = target.token_file.with_name("unmanaged.json")
    generation = target.token_file.with_name(".guangya_token.json.generation")
    unrelated.write_text("keep")
    generation.write_text("17")
    archive = backup.create_backup(source)
    manifest = backup.verify_backup(archive).payload
    assert manifest["format_version"] == 2 and manifest["scope"] == "full"
    assert manifest["absent_entries"] == [TOKEN]
    backup.restore_backup(target, archive)
    assert state(target) == ("new", "SNAPSHOT='new'\n", None)
    assert unrelated.read_text() == "keep" and generation.read_text() == "17"
    assert not backup.recover_pending_restore(target)


def test_full_snapshot_with_token_replaces_target_token(tmp_path):
    source = seed(tmp_path / "source", "new", token="archived-token")
    target = seed(tmp_path / "target", "old", token="later-token")
    archive = backup.create_backup(source)
    assert backup.verify_backup(archive).payload["absent_entries"] == []
    backup.restore_backup(target, archive)
    assert state(target) == ("new", "SNAPSHOT='new'\n", "archived-token")


@pytest.mark.parametrize("include_settings", [True, False])
def test_partial_snapshot_never_deletes_existing_config_or_token(tmp_path, include_settings):
    source = seed(tmp_path / "source", "new", env=not include_settings)
    target = seed(tmp_path / "target", "old", token="keep-token")
    archive = backup.create_backup(source, include_settings=include_settings)
    manifest = backup.verify_backup(archive).payload
    assert manifest["scope"] == "partial" and manifest["absent_entries"] == []
    backup.restore_backup(target, archive)
    assert state(target) == ("new", "SNAPSHOT='old'\n", "keep-token")


def test_legacy_archive_missing_token_is_not_evidence_for_deletion(tmp_path):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="keep-token")
    archive = backup.create_backup(source)

    def legacy(manifest):
        manifest["format_version"] = 1
        del manifest["scope"], manifest["absent_entries"]

    rewrite_manifest(archive, legacy)
    assert backup.verify_backup(archive).payload["format_version"] == 1
    backup.restore_backup(target, archive)
    assert state(target) == ("new", "SNAPSHOT='new'\n", "keep-token")


@pytest.mark.parametrize("changes", [
    {"absent_entries": []},
    {"absent_entries": [TOKEN, TOKEN]},
    {"absent_entries": ["config/user.env"]},
    {"absent_entries": ["data/unmanaged.json"]},
    {"scope": "partial"},
    {"scope": "unknown"},
    {"format_version": 1},
])
def test_ambiguous_absence_is_rejected_without_mutating_target(tmp_path, changes):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="keep-token")
    archive = backup.create_backup(source)
    rewrite_manifest(archive, lambda manifest: manifest.update(changes))
    with pytest.raises(backup.BackupError):
        backup.verify_backup(archive)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(target, archive)
    assert state(target) == ("old", "SNAPSHOT='old'\n", "keep-token")


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_deleted_token_is_restored_by_failure_rollback_or_prepared_recovery(tmp_path, failure):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    replace = backup.os.replace

    def interrupt_after_token_rename(src, dst):
        result = replace(src, dst)
        if Path(src) == target.token_file:
            raise failure("fixture interruption after token removal")
        return result

    with (
        patch.object(backup.os, "replace", side_effect=interrupt_after_token_rename),
        pytest.raises(backup.BackupError if failure is OSError else KeyboardInterrupt),
    ):
        backup.restore_backup(target, archive)
    if failure is KeyboardInterrupt:
        assert not target.token_file.exists()
        assert backup.recover_pending_restore(target)
    assert state(target) == ("old", "SNAPSHOT='old'\n", "original-token")
    assert not backup.recover_pending_restore(target)


def test_backup_never_publishes_a_partially_restored_instance(tmp_path):
    source = seed(tmp_path / "source", "new", token="new-token")
    target = seed(tmp_path / "target", "old", token="old-token")
    archive = backup.create_backup(source)
    previous = backup.create_backup(target)
    previous_bytes = previous.read_bytes()
    replace = backup.os.replace

    def interrupt_after_config(src, dst):
        replace(src, dst)
        if Path(dst) == target.env_file:
            raise KeyboardInterrupt

    with patch.object(backup.os, "replace", side_effect=interrupt_after_config), pytest.raises(KeyboardInterrupt):
        backup.restore_backup(target, archive)
    assert state(target) == ("old", "SNAPSHOT='new'\n", "old-token")

    with pytest.raises(backup.BackupError, match="恢复"):
        backup.create_backup(target, output=previous)
    assert previous.read_bytes() == previous_bytes
    assert backup.recover_pending_restore(target)
    backup.create_backup(target, output=previous)
    backup.restore_backup(source, previous)
    assert state(source) == ("old", "SNAPSHOT='old'\n", "old-token")


def test_committed_recovery_keeps_absence_and_cleans_saved_token(tmp_path):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    with (
        patch.object(backup, "_recover_committed", side_effect=KeyboardInterrupt("fixture cleanup interruption")),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.restore_backup(target, archive)
    journal = backup._read_restore_journal(target)
    assert journal is not None
    token_entry = next(entry for entry in journal["entries"] if entry["name"] == TOKEN)
    assert journal["version"] == 2 and journal["phase"] == "committed"
    assert token_entry["new"] is None and token_entry["had_target"] is True
    assert journal is not None
    _temporary, saved_token = backup._restore_artifacts(target.token_file, journal["transaction_id"])
    assert saved_token.read_text() == "original-token"
    assert backup.recover_pending_restore(target)
    assert state(target) == ("new", "SNAPSHOT='new'\n", None)
    assert not saved_token.exists() and not backup.recover_pending_restore(target)


def test_interrupted_rollback_of_absence_is_idempotent(tmp_path):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    write_journal = backup._write_restore_journal

    def stop_before_commit(paths, journal):
        if journal["phase"] == "committed":
            raise KeyboardInterrupt("fixture before commit")
        write_journal(paths, journal)

    with (
        patch.object(backup, "_write_restore_journal", side_effect=stop_before_commit),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.restore_backup(target, archive)
    replace = backup.os.replace

    def interrupt_rollback(src, dst):
        result = replace(src, dst)
        if Path(dst) == target.token_file:
            raise KeyboardInterrupt("fixture rollback interruption")
        return result

    with (
        patch.object(backup.os, "replace", side_effect=interrupt_rollback),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.recover_pending_restore(target)
    assert backup.recover_pending_restore(target)
    assert state(target) == ("old", "SNAPSHOT='old'\n", "original-token")


def test_pending_v1_journal_still_recovers_without_new_absence_semantics(tmp_path):
    source = seed(tmp_path / "source", "new", token="archived-token")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    with (
        patch.object(backup, "_recover_committed", side_effect=KeyboardInterrupt("fixture legacy pending")),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.restore_backup(target, archive)
    journal_path = backup._restore_journal_path(target)
    journal = json.loads(journal_path.read_text())
    journal["version"] = 1
    journal_path.write_text(json.dumps(journal))
    assert backup.recover_pending_restore(target)
    assert state(target) == ("new", "SNAPSHOT='new'\n", "archived-token")


@pytest.mark.parametrize("phase", ["prepared", "committed"])
def test_cold_process_recovers_token_deletion_journal(tmp_path, phase):
    import subprocess
    import sys

    from tests.test_backup import _LIFECYCLE_EXEC_BOOTSTRAP

    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    write_journal = backup._write_restore_journal

    def before_commit(paths, journal):
        if journal["phase"] == "committed":
            raise KeyboardInterrupt("fixture before commit marker")
        write_journal(paths, journal)

    fault = (
        patch.object(backup, "_write_restore_journal", side_effect=before_commit)
        if phase == "prepared" else
        patch.object(backup, "_recover_committed", side_effect=KeyboardInterrupt("fixture after commit marker"))
    )
    with fault, pytest.raises(KeyboardInterrupt):
        backup.restore_backup(target, archive)
    assert not target.token_file.exists()
    read_restore_journal_result = backup._read_restore_journal(target)
    assert read_restore_journal_result is not None
    assert read_restore_journal_result["phase"] == phase

    # exec 后无父进程模块/锁/内存状态；先安装既有测试的 Inet/DNS 阻断再导入应用。
    script = _LIFECYCLE_EXEC_BOOTSTRAP + '''
from app.runtime_paths import get_runtime_paths
from app.modules.backup import recover_pending_restore
paths = get_runtime_paths()
assert recover_pending_restore(paths)
assert not recover_pending_restore(paths)
print("COLD_DELETION_RECOVERED", flush=True)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "target")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "EXEC_GUARD_VERIFIED" in completed.stdout
    assert "COLD_DELETION_RECOVERED" in completed.stdout
    expected = ("old", "SNAPSHOT='old'\n", "original-token") if phase == "prepared" else ("new", "SNAPSHOT='new'\n", None)
    assert state(target) == expected


def test_v1_journal_cannot_claim_token_deletion(tmp_path):
    source = seed(tmp_path / "source", "new")
    target = seed(tmp_path / "target", "old", token="original-token")
    archive = backup.create_backup(source)
    with (
        patch.object(backup, "_recover_committed", side_effect=KeyboardInterrupt("fixture pending")),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.restore_backup(target, archive)
    path = backup._restore_journal_path(target)
    journal = json.loads(path.read_text())
    journal["version"] = 1
    path.write_text(json.dumps(journal))
    with pytest.raises(backup.BackupError, match="文件指纹无效"):
        backup.recover_pending_restore(target)
    assert path.exists()
