"""无标签 NSFW 目录的本地历史身份守卫。"""
from __future__ import annotations

from typing import cast
from app.modules.scraper import TMDBScraper

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.repositories import local_media as local_media_repo
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper


class LocalNsfwDirectoryIdentityTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rules = OrganizeRules(
            naming_scope="both", region_split=False, year_split=False,
            small_file_mb=0, clean_empty=False, emby_refresh=False,
            nsfw_enabled=True, nsfw_metatube_endpoint="http://nsfw-test.invalid",
        )
        self.enterContext(patch(
            "app.modules.local_media_service.OrganizeRules.from_config",
            return_value=self.rules,
        ))
        self.enterContext(patch(
            "app.modules.local_media_service.probe_local_media_profile", return_value=None,
        ))
        self.enterContext(patch(
            "app.modules.organize.Organizer._historical_root_identities",
            side_effect=AssertionError("本地不得读取光鸭历史"),
        ))

    def _source(self, name="source", *, owner="admin", target=None):
        source = self.root / name
        source.mkdir()
        target = target or self.root / "movies"
        target.mkdir(exist_ok=True)
        source_id = local_media_repo.create_local_media_source(
            name=f"{name}-{self.root.name}", qb_profile="", qb_path_prefix="", local_root=str(source),
            media_type="nsfw", owner=owner,
        )
        local_media_repo.upsert_local_library_target(source_id, "movie", str(target), owner=owner)
        return source_id, source, target

    def _service(self, provider="metatube", external_id="javbus:first", *, number="SODAH-003"):
        match = MatchResult(
            title="SODAH-003", media_type="movie", confidence=1.0, status="matched",
            provider=provider, external_id=external_id,
            metadata={"number": number, "title": "SODAH-003"},
        )
        service = LocalMediaService(scraper=cast(TMDBScraper, FakeScraper(match)))
        self.addCleanup(service.close)
        return service

    def _task(self, source_id, path, *, owner="admin"):
        task_id = local_media_repo.create_local_media_task(
            source_id, "", str(path), owner=owner, trigger="scan",
        )
        self.assertTrue(local_media_repo.claim_local_media_task(task_id, owner=owner))
        return task_id

    def _preview(self, service, source_id, path):
        inspection = service.inspect_source("admin", source_id, path)
        return service.preview("admin", inspection["inspection_id"], automatic=True)

    def _history(
        self, source_id, source, target, *, owner="admin", identity="metatube:javbus:other",
        status="completed", step_status="completed", group=None,
    ):
        path = source / f"history-{len(local_media_repo.list_local_media_tasks(owner=owner))}.mp4"
        task_id = local_media_repo.create_local_media_task(source_id, "", str(path), owner=owner, trigger="scan")
        local_media_repo.add_local_media_task_item(
            task_id, str(path), str(target), role="video", owner=owner,
            media_group=f"movie:{identity}:__root__" if group is None else group,
        )
        if step_status:
            task = local_media_repo.get_local_media_task(task_id, owner=owner)
            assert task is not None
            step = local_media_repo.add_local_media_operation_step(
                task_id, task.operation_token, 1, "rename", str(path), str(target), owner=owner,
            )
            local_media_repo.update_local_media_operation_step(step, step_status)
        local_media_repo.update_local_media_task(task_id, owner=owner, status=status)
        return task_id

    def test_independent_tasks_compare_numbers_and_reject_other_media(self):
        for index, (first_provider, provider, external_id, number, blocked) in enumerate((
            ("metatube", "metatube", "javbus:second", "SODAH-003", False),
            ("metatube", "clean_title", "SODAH-003", "SODAH-003", False),
            ("clean_title", "metatube", "JAV321:sodah00003", "SODAH-003", False),
            ("metatube", "metatube", "javbus:first", "SODAH-003", False),
            ("metatube", "metatube", "javbus:first", "SODAH-004", True),
        )):
            with self.subTest(first_provider=first_provider, provider=provider, external_id=external_id, number=number):
                source_id, source, _target = self._source(
                    f"source-{index}", target=self.root / f"movies-{index}",
                )
                batch = source / "batch:first"
                batch.mkdir()
                first = batch / "SODAH-003-1.mp4"
                second = batch / "SODAH-003-2.mp4"
                first.write_bytes(b"first-video")
                second.write_bytes(b"second-video")
                first_task = self._task(source_id, first)
                first_id = "SODAH-003" if first_provider == "clean_title" else "javbus:first"
                first_result = self._service(first_provider, first_id).execute_task("admin", first_task)
                self.assertEqual(first_result["status"], "completed")
                self.assertTrue(first_result["moved"])
                second_task = self._task(source_id, second)
                result = self._service(provider, external_id, number=number).execute_task("admin", second_task)
                self.assertEqual(result["status"], "requires_manual" if blocked else "completed")
                if blocked:
                    self.assertTrue(second.exists())
                    self.assertIn("历史整理记录", str(result["preview"]["pending_confirmations"]))
                    self.assertEqual(local_media_repo.list_local_media_task_items(second_task), [])
                else:
                    self.assertTrue(result["moved"])
                    self.assertFalse(second.exists())

    def test_history_is_scoped_by_owner_and_real_media_root(self):
        source_id, source, target = self._source()
        incoming = source / "SODAH-003.mp4"
        incoming.write_bytes(b"incoming")
        service = self._service()
        preview = self._preview(service, source_id, incoming)
        media_root = Path(preview["plans"][0]["target_path"]).parent
        self.assertEqual(media_root, target / "SODAH-003")
        self._history(source_id, source, media_root / "same.mp4", identity="metatube:javbus:first")
        self._history(source_id, source, self.root / "other-movies" / media_root.name / "other.mp4")
        self._history(source_id, source, media_root.with_name(media_root.name + "-other") / "other.mp4")
        other_id, other_source, _ = self._source("other-owner", owner="other", target=target)
        self._history(other_id, other_source, media_root / "other-owner.mp4", owner="other")
        self.assertEqual(self._preview(service, source_id, incoming)["pending_confirmations"], [])
        # 不按来源隔离：另一来源写入同一真实媒体根仍必须拦截。
        another_id, another_source, _ = self._source("another-source", target=target)
        self._history(another_id, another_source, media_root / "different.mp4")
        self.assertIn("历史整理记录", str(self._preview(service, source_id, incoming)["pending_confirmations"]))

    def test_execute_rechecks_history_created_after_preview(self):
        source_id, source, _target = self._source()
        incoming = source / "SODAH-003.mp4"
        incoming.write_bytes(b"incoming")
        task_id = self._task(source_id, incoming)
        service = self._service()
        preview = self._preview(service, source_id, incoming)
        self.assertEqual(preview["pending_confirmations"], [])
        target = Path(preview["plans"][0]["target_path"])
        self._history(source_id, source, target.with_name("already-committed.mp4"))
        result = service.execute_task("admin", task_id)
        self.assertEqual(result["status"], "requires_manual")
        self.assertIn("历史整理记录", str(result["preview"]["pending_confirmations"]))
        self.assertTrue(incoming.exists())
        self.assertFalse(target.exists())
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id), [])

    def test_only_committed_facts_bind_identity_and_missing_identity_is_not_guessed(self):
        for index, (status, step_status, group, blocked) in enumerate((
            ("completed", "", None, False),
            ("failed", "rolled_back", None, False),
            ("requires_manual", "completed", None, True),
            ("completed", "completed", "tv:metatube:javbus:first:__root__", True),
            ("completed", "completed", "", None),
        )):
            with self.subTest(status=status, step_status=step_status, group=group):
                source_id, source, _target = self._source(
                    f"facts-{index}", target=self.root / f"facts-movies-{index}",
                )
                incoming = source / "SODAH-003.mp4"
                incoming.write_bytes(b"incoming")
                service = self._service()
                preview = self._preview(service, source_id, incoming)
                target = Path(preview["plans"][0]["target_path"])
                history_task = self._history(
                    source_id, source, target.with_name("history.mp4"),
                    status=status, step_status=step_status, group=group,
                )
                if blocked is None:
                    with self.assertRaisesRegex(LocalMediaServiceError, "缺少可验证的媒体身份"):
                        self._preview(service, source_id, incoming)
                else:
                    pending = self._preview(service, source_id, incoming)["pending_confirmations"]
                    self.assertEqual(bool(pending), blocked)
                    if status == "requires_manual" and step_status == "completed":
                        archived = target.with_name("history.mp4")
                        archived.parent.mkdir(parents=True)
                        archived.write_bytes(b"already-archived")
                        get_local_media_task_result = local_media_repo.get_local_media_task(history_task)
                        assert get_local_media_task_result is not None
                        old_token = get_local_media_task_result.operation_token
                        self.assertTrue(local_media_repo.reset_local_media_task(history_task))
                        get_local_media_task_result_2 = local_media_repo.get_local_media_task(history_task)
                        assert get_local_media_task_result_2 is not None
                        self.assertNotEqual(get_local_media_task_result_2.operation_token, old_token)
                        self.assertEqual(local_media_repo.list_local_media_task_items(history_task), [])
                        with self.assertRaisesRegex(LocalMediaServiceError, "身份快照已缺失"):
                            self._preview(service, source_id, incoming)
                        self.assertTrue(archived.exists())
