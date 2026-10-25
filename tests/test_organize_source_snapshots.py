"""确认来源快照必须跨越重新扫描、规划与最终写入。"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from typing import cast
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.clients.tmdb import TMDBClient
from app.modules import organize_confirmations as confirmations
from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.organize import Organizer, OrganizeRules
from app.modules.scraper import TMDBScraper
from tests.support import IsolatedDatabaseTestCase
from tests.test_guangya_directory_scrape import (
    _DirectoryScrapeTMDB, _MutableTreeClient, _dir, _file,
)


def _change(client: _MutableTreeClient, file_id: str, kind: str) -> None:
    """直接修改假云盘，不把并发方操作混入 Organizer 写调用。"""
    old = client.infos[file_id]
    client.tree[old.parent_id] = [
        item for item in client.tree[old.parent_id] if item.file_id != file_id
    ]
    if kind == "missing":
        del client.infos[file_id]
        return
    changes = {
        "name": {"name": "Alien.1979.1080p." + old.name.rsplit(".", 1)[-1]},
        "parent": {"parent_id": "elsewhere"},
        "size": {"size": old.size + 1},
        "etag": {"etag": old.etag + "-changed"},
        "replacement": {"file_id": file_id + "-replacement"},
        "extension": {"name": old.name.rsplit(".", 1)[0] + ".txt"},
        "sample": {"name": "Sample.mkv"},
        "small": {"size": 1},
        "type": {"is_dir": True},
    }
    current = replace(old)
    for field, value in changes[kind].items():
        setattr(current, field, value)
    client.infos.pop(file_id)
    client.infos[current.file_id] = current
    client.tree.setdefault(current.parent_id, []).append(current)


class ConfirmedSourceSnapshotTests(IsolatedDatabaseTestCase):
    def _fixture(self):
        video = _file("video", "Iron.Man.2008.1080p.mkv", "source")
        subtitle = _file("subtitle", "Iron.Man.2008.1080p.zh.srt", "source", size=100)
        tree = {"source": [video, subtitle], "archive": [], "elsewhere": []}
        infos = {
            "source": _dir("source", "钢铁侠"),
            "archive": _dir("archive", "媒体库"),
            "elsewhere": _dir("elsewhere", "其他目录"),
            "video": video, "subtitle": subtitle,
        }
        client = _MutableTreeClient(tree, infos)
        scraper = _DirectoryScrapeTMDB()
        scraper.confirm = Mock()
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False, notify_enabled=False,
        )
        service = DirectoryScrapeService(
            client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, scraper),
            store=DirectoryScrapeStore(), rules_loader=lambda: rules,
        )
        self.addCleanup(service.close)
        inspection = service.inspect("owner", "source")
        preview = service.preview("owner", inspection["inspection_id"], "1726", "movie")
        record = service.store.get_preview("owner", preview["preview_id"])
        return client, scraper, rules, service, record, preview["preview_id"]

    def _group_fixture(self, *, nested: bool, directory_snapshots: bool):
        root = replace(_dir("source", "待整理"), size=8192)
        if nested:
            a = replace(_dir("a", "Iron Man (2008)", "source"), size=4096)
            b = replace(_dir("b", "Alien (1979)", "source"), size=4096)
            inner = replace(_dir("inner", "Disc", "a"), size=4096)
            tree = {"source": [a, b], "a": [inner], "inner": [], "b": [], "archive": []}
            parents = ("inner", "b")
        else:
            tree = {"source": [], "archive": []}
            parents = ("source", "source")
        for file_id, name, parent in zip(
            ("v1", "v2"), ("Iron.Man.2008.1080p", "Alien.1979.1080p"), parents,
        ):
            tree[parent].extend([
                _file(file_id, name + ".mkv", parent),
                _file(file_id + "-sub", name + ".zh.srt", parent, size=100),
            ])
        infos = {item.file_id: item for entries in tree.values() for item in entries}
        infos.update({"source": root, "archive": _dir("archive", "媒体库")})
        client = _MutableTreeClient(tree, infos)
        snapshots = {
            file_id: replace(item, size=0) if item.is_dir else replace(item)
            for file_id, item in infos.items()
            if file_id != "archive" and (directory_snapshots or not item.is_dir)
        }
        members = frozenset(set(infos) - {"archive", "source"})
        fake = _DirectoryScrapeTMDB()
        scraper = TMDBScraper(client=cast(TMDBClient, fake))
        self.addCleanup(scraper.close)

        def match(filename, _parent="", **_kwargs):
            return replace(fake.auto_result, tmdb_id="348", title="异形", year="1979") if "Alien" in filename else replace(fake.auto_result)

        def detail(tmdb_id, media_type, **_kwargs):
            result = fake.get_detail(tmdb_id, media_type)
            if tmdb_id == "348":
                result.update(title="异形", original_title="Alien", release_date="1979-05-25")
            return result

        self.enterContext(patch.object(scraper, "match", side_effect=match))
        self.enterContext(patch.object(scraper, "get_detail", side_effect=detail))
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=100, region_split=False,
            year_split=False, clean_empty=False, link_strm=False, notify_enabled=False,
            media_info_enabled=False, media_probe_enabled=False,
        )
        return client, scraper, rules, snapshots, members

    def test_real_group_pipeline_scopes_snapshots_and_reuses_source_reads(self):
        original = Organizer._scan_source
        for nested in (False, True):
            for directory_snapshots in (False, True):
                for workers in (1, 2):
                    with self.subTest(nested=nested, directories=directory_snapshots, workers=workers):
                        client, scraper, rules, snapshots, members = self._group_fixture(
                            nested=nested, directory_snapshots=directory_snapshots,
                        )
                        contexts = []

                        def scan(instance, context, *args, **kwargs):
                            if kwargs.get("restriction") is not None:
                                contexts.append(context)
                            return original(instance, context, *args, **kwargs)

                        with patch.object(Organizer, "_scan_source", scan), patch.object(
                            client, "list_dir", wraps=client.list_dir,
                        ) as listing:
                            plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=scraper).organize(
                                "source", rules, dry_run=False, post_actions=False,
                                source_snapshots=snapshots, source_member_ids=members,
                                group_pipeline=True, planning_workers=workers, media_probe_cache_only=True,
                            )
                        self.assertEqual(stats["source_groups_total"], 2)
                        self.assertEqual(stats["moved"], 2)
                        self.assertEqual(stats["metadata_moved"], 2)
                        self.assertEqual(stats["failed"], 0)
                        self.assertEqual({plan.file_id for plan in plans}, {"v1", "v2"})
                        self.assertEqual(len(contexts), 2)
                        if workers == 2:
                            self.assertEqual(stats["planning_workers"], 2)
                        for context in contexts:
                            selected = set(context.source_snapshots or {}) & {"v1", "v2"}
                            self.assertEqual(len(selected), 1)
                            selected_id = selected.pop()
                            self.assertIn(selected_id + "-sub", context.source_snapshots or {})
                            self.assertNotIn(("v2" if selected_id == "v1" else "v1"), context.source_member_ids or ())
                        reads = Counter(call.args[0] for call in listing.call_args_list)
                        self.assertEqual(reads["source"], 2)  # 原有枚举 + 一次完整扫描；各组只读缓存。
                        if nested:
                            self.assertEqual({key: reads[key] for key in ("a", "inner", "b")}, {"a": 1, "inner": 1, "b": 1})
                        for file_id in ("v1", "v2"):
                            self.assertNotIn(client.infos[file_id].parent_id, {"source", "a", "inner", "b"})
                            self.assertEqual(client.infos[file_id].parent_id, client.infos[file_id + "-sub"].parent_id)

    def test_missing_group_or_video_fails_before_any_group_write(self):
        for nested, file_id, kind in ((False, "v2", "missing"), (False, "v2", "replacement"), (True, "v2", "missing"), (True, "b", "missing")):
            with self.subTest(nested=nested, file_id=file_id, kind=kind):
                client, scraper, rules, snapshots, members = self._group_fixture(nested=nested, directory_snapshots=False)
                _change(client, file_id, kind)
                with patch.object(client, "move", wraps=client.move) as move, patch.object(
                    client, "create_dir", wraps=client.create_dir,
                ) as mkdir, self.assertRaises(DirectoryScrapeConflictError):
                    Organizer(client=cast(GuangYaClient, client), scraper=scraper).organize(
                        "source", rules, dry_run=False, post_actions=False,
                        source_snapshots=snapshots, source_member_ids=members,
                        group_pipeline=True, planning_workers=2, media_probe_cache_only=True,
                    )
                move.assert_not_called()
                mkdir.assert_not_called()

    def test_directory_nonzero_size_is_not_an_invented_file_constraint(self):
        client, _scraper, _rules, service, _record, preview_id = self._fixture()
        client.infos["source"].size = 8192
        result = service.execute_preview("owner", preview_id)
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["metadata_moved"], 1)
        self.assertEqual(result["stats"]["failed"], 0)

    def test_unknown_etag_keeps_existing_snapshot_compatibility(self):
        client, scraper, rules, _service, _record, _preview_id = self._fixture()
        snapshots = {file_id: replace(client.infos[file_id], etag="") for file_id in ("video", "subtitle")}
        _change(client, "video", "etag")
        _plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, scraper)).organize(
            "source", rules, dry_run=False, post_actions=False, source_snapshots=snapshots,
            source_member_ids=frozenset(snapshots), group_pipeline=False, media_probe_cache_only=True,
        )
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["metadata_moved"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_released_preview_retry_still_uses_original_snapshot(self):
        client, _scraper, _rules, service, _record, preview_id = self._fixture()
        original = replace(client.infos["video"])
        organize = Organizer.organize
        changed = False

        def change_before_scan(instance, *args, **kwargs):
            nonlocal changed
            if kwargs.get("dry_run") is False and not changed:
                changed = True
                _change(client, "video", "etag")
            return organize(instance, *args, **kwargs)

        with patch.object(Organizer, "organize", change_before_scan), self.assertRaises(DirectoryScrapeConflictError):
            service.execute_preview("owner", preview_id)
        with self.assertRaises(DirectoryScrapeConflictError):
            service.execute_preview("owner", preview_id)
        client.infos["video"] = original
        client.tree["source"] = [original if item.file_id == "video" else item for item in client.tree["source"]]
        result = service.execute_preview("owner", preview_id)
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["failed"], 0)

    def test_renamed_sample_and_new_small_file_are_not_silently_filtered_out(self):
        for kind in ("small", "sample"):
            with self.subTest(kind=kind):
                client, scraper, rules, snapshots, members = self._group_fixture(nested=False, directory_snapshots=False)
                _change(client, "v2", kind)
                with patch.object(client, "move", wraps=client.move) as move, self.assertRaises(DirectoryScrapeConflictError):
                    Organizer(client=cast(GuangYaClient, client), scraper=scraper).organize(
                        "source", rules, dry_run=False, post_actions=False,
                        source_snapshots=snapshots, source_member_ids=members,
                        group_pipeline=True, media_probe_cache_only=True,
                    )
                move.assert_not_called()

    def test_unfrozen_automatic_scans_keep_live_attributes_and_group_pipeline(self):
        client, scraper, rules, _snapshots, members = self._group_fixture(nested=True, directory_snapshots=False)
        _change(client, "v2", "size")
        _change(client, "v2", "etag")
        plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=scraper).organize(
            "source", rules, dry_run=False, post_actions=False, automatic=True,
            source_member_ids=members, group_pipeline=True, planning_workers=2, media_probe_cache_only=True,
        )
        self.assertEqual(stats["source_groups_total"], 2)
        self.assertEqual(stats["moved"], 2)
        self.assertEqual(stats["failed"], 0)
        plan = next(plan for plan in plans if plan.file_id == "v2")
        self.assertEqual(plan.size, client.infos["v2"].size)
        self.assertEqual(plan.etag, "etag-v2-changed")

    def test_final_writer_rechecks_originals_after_target_creation(self):
        for file_id in ("video", "subtitle"):
            for kind in ("name", "parent", "size", "etag", "missing", "replacement"):
                with self.subTest(file_id=file_id, kind=kind):
                    client, scraper, _rules, service, _record, preview_id = self._fixture()
                    video_before = replace(client.infos["video"])
                    original = client.create_dir
                    changed = False

                    def create(*args, **kwargs):
                        nonlocal changed
                        result = original(*args, **kwargs)
                        if not changed:
                            changed = True
                            _change(client, file_id, kind)
                        return result

                    with patch.object(client, "create_dir", side_effect=create):
                        result = service.execute_preview("owner", preview_id)
                    self.assertTrue(changed)
                    self.assertEqual(result["stats"]["moved"], 0)
                    self.assertEqual(result["stats"]["failed"], 1)
                    if file_id == "subtitle":
                        self.assertEqual(client.infos["video"], video_before)
                    scraper.confirm.assert_not_called()

    def test_live_object_mutation_during_planning_does_not_mutate_frozen_baseline(self):
        client, scraper, rules, _service, _record, _preview_id = self._fixture()
        # 故意将 client 的同一可变对象交给入口，入口必须保留独立的原确认版本。
        snapshots = {file_id: client.infos[file_id] for file_id in ("video", "subtitle")}
        expected = replace(snapshots["video"])
        original = Organizer._build_plans

        def plan(instance, *args, **kwargs):
            client.infos["video"].name = "Alien.1979.1080p.mkv"
            client.infos["video"].etag = "concurrent-version"
            return original(instance, *args, **kwargs)

        organizer = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, scraper))
        with patch.object(Organizer, "_build_plans", plan), patch.object(
            organizer, "_verify_remote_snapshot", wraps=organizer._verify_remote_snapshot,
        ) as verify, patch.object(client, "move", wraps=client.move) as move:
            _plans, stats = organizer.organize(
                "source", rules, dry_run=False, post_actions=False, group_pipeline=False,
                source_snapshots=snapshots, source_member_ids=frozenset(snapshots), media_probe_cache_only=True,
            )
        self.assertEqual(stats["moved"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(verify.call_args_list[0].args[0], expected)
        move.assert_not_called()

    def test_web_rejects_changed_video_and_companion_before_final_scan(self):
        original = Organizer.organize
        for file_id in ("video", "subtitle"):
            for kind in ("name", "parent", "size", "etag", "missing", "replacement", "extension", "type"):
                with self.subTest(file_id=file_id, kind=kind):
                    client, scraper, _rules, service, _record, preview_id = self._fixture()
                    changed = False

                    def organize(instance, *args, **kwargs):
                        nonlocal changed
                        if kwargs.get("dry_run") is False and not changed:
                            changed = True
                            _change(client, file_id, kind)
                        return original(instance, *args, **kwargs)

                    with patch.object(Organizer, "organize", organize), patch.object(
                        client, "move", wraps=client.move,
                    ) as move, patch.object(client, "create_dir", wraps=client.create_dir) as mkdir:
                        with self.assertRaises(DirectoryScrapeConflictError):
                            service.execute_preview("owner", preview_id)
                    self.assertTrue(changed)
                    move.assert_not_called()
                    mkdir.assert_not_called()
                    scraper.confirm.assert_not_called()

    def _claim_confirmation(self, record, rules):
        group = {
            "source_dir_id": "source", "source_parent_id": "source",
            "source_name": "下载", "directory": "钢铁侠", "reason": "需要人工确认",
            "files": [vars(item) for item in record.inspection.videos],
            "companions": [vars(item) for item in record.inspection.companions],
            "candidates": [{"tmdb_id": "1726", "media_type": "movie", "title": "钢铁侠", "year": "2008"}],
        }
        actions = confirmations.create_confirmation_actions(group, rules, notification_suppressed=True)
        token = actions[0].callback_data.split(":")[1]
        self.assertIsNotNone(db.claim_organize_confirmation(token, chat_id="", selected_index=0))
        row = db.claim_queued_organize_confirmation(token)
        assert row is not None
        payload = json.loads(row["payload_json"])
        return token, payload

    def test_tg_rejects_source_changes_during_resolution_and_final_scan(self):
        original = Organizer.organize
        for stage in ("resolution", "final_scan"):
            for file_id in ("video", "subtitle"):
                for kind in ("name", "parent", "size", "etag", "missing", "replacement", "extension", "type"):
                    with self.subTest(stage=stage, file_id=file_id, kind=kind):
                        client, scraper, rules, _service, record, _preview_id = self._fixture()
                        token, payload = self._claim_confirmation(record, rules)
                        changed = False

                        def resolve(*_args):
                            nonlocal changed
                            if stage == "resolution":
                                changed = True
                                _change(client, file_id, kind)
                            return scraper, record.match, record.detail, "tmdb"

                        def organize(instance, *args, **kwargs):
                            nonlocal changed
                            if stage == "final_scan" and kwargs.get("dry_run") is False:
                                changed = True
                                _change(client, file_id, kind)
                            return original(instance, *args, **kwargs)

                        with patch.object(confirmations, "GuangYaClient", return_value=client), patch.object(
                            OrganizeRules, "from_config", return_value=rules,
                        ), patch.object(confirmations, "_resolve_guangya_confirmation_candidate", side_effect=resolve), patch.object(
                            Organizer, "organize", organize,
                        ), patch.object(client, "move", wraps=client.move) as move:
                            with self.assertRaises(DirectoryScrapeConflictError):
                                confirmations._execute_confirmation(
                                    token, payload, payload["candidates"][0], selected_index=0, chat_id="",
                                )
                        self.assertTrue(changed)
                        move.assert_not_called()
                        saved = db.get_organize_confirmation(token)
                        assert saved is not None
                        self.assertEqual(saved["status"], "failed")
                        scraper.confirm.assert_not_called()

    def test_tg_unchanged_snapshot_keeps_real_execution_and_human_learning(self):
        client, scraper, rules, _service, record, _preview_id = self._fixture()
        token, payload = self._claim_confirmation(record, rules)
        with patch.object(confirmations, "GuangYaClient", return_value=client), patch.object(
            OrganizeRules, "from_config", return_value=rules,
        ), patch.object(confirmations, "_resolve_guangya_confirmation_candidate", return_value=(scraper, record.match, record.detail, "tmdb")):
            result = confirmations._execute_confirmation(
                token, payload, payload["candidates"][0], selected_index=0, chat_id="",
            )
        stats = result["stats"]
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["metadata_moved"], 1)
        self.assertEqual(stats["failed"], 0)
        saved = db.get_organize_confirmation(token)
        assert saved is not None
        self.assertEqual(saved["status"], "completed")
        scraper.confirm.assert_called_once()
