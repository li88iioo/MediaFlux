"""季集研究写边界回归：真实 executor + 隔离 DB/假云盘，禁止未授权写入。"""
from __future__ import annotations

import copy
import json
import time
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from app import database as db
from app.modules import episode_research_service as service
from app.modules import organize_confirmations as confirmations
from tests import test_episode_research_confirmation as fixtures
from tests.support import IsolatedDatabaseTestCase
from tests.test_guangya_directory_scrape import _file


class EpisodeResearchWriteSafetyTests(IsolatedDatabaseTestCase):
    @contextmanager
    def _build(self):
        """只借用既有 fixture 的公开辅助方法，不继承并重复运行旧测试。"""
        case = fixtures.EpisodeResearchConfirmationTests()
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()

    @staticmethod
    def _add_subtitle(case, *, video_index=1):
        video = case.payload["files"][video_index]
        subtitle = _file(
            "source-subtitle", video["name"].rsplit(".", 1)[0] + ".srt", "source", size=64,
        )
        case.cloud.infos[subtitle.file_id] = subtitle
        case.cloud.tree["source"].append(subtitle)
        case.payload["companions"].append({
            "file_id": subtitle.file_id, "name": subtitle.name,
            "parent_id": subtitle.parent_id, "size": subtitle.size, "etag": subtitle.etag,
            "video_file_id": video["file_id"],
        })
        return subtitle

    @staticmethod
    def _change_content(case, file_id):
        original = case.cloud.infos[file_id]
        changed = _file(
            file_id, original.name, original.parent_id,
            size=original.size + 123, etag="post-freeze-replacement-content",
        )
        case.cloud.infos[file_id] = changed
        case.cloud.tree[original.parent_id] = [
            changed if item.file_id == file_id else item
            for item in case.cloud.tree[original.parent_id]
        ]

    def _assert_all_frozen_files_stay_in_source(self, case):
        for item in (*case.payload["files"], *case.payload["companions"]):
            with self.subTest(file_id=item["file_id"]):
                self.assertEqual(case.cloud.infos[item["file_id"]].parent_id, item["parent_id"])
                self.assertEqual(case.cloud.infos[item["file_id"]].name, item["name"])

    def test_source_change_during_receipt_revalidation_blocks_all_media_writes(self):
        for explicit, kind in ((False, "video"), (False, "companion"), (True, "video"), (True, "companion")):
            with self.subTest(explicit_approval=explicit, kind=kind), self._build() as case:
                subtitle = self._add_subtitle(case)
                file_id = case.payload["files"][0]["file_id"] if kind == "video" else subtitle.file_id
                if explicit:
                    case.payload["review_only"] = True
                self.assertEqual(case.create(case.decision()), "approved")
                if explicit:
                    case.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
                    confirmations.start_confirmation("episode-case", 0, chat_id="owner-chat", actor="agent",
                        explicit_approval=True, dispatch=False)
                original = service.revalidate_episode_research_receipt
                reached = []

                def revalidate_then_change(
                    *args, original=original, file_id=file_id, reached=reached, **kwargs,
                ):
                    proposal = original(*args, **kwargs)
                    self._change_content(case, file_id)
                    reached.append(True)
                    return proposal

                with (
                    patch.object(service, "revalidate_episode_research_receipt", side_effect=revalidate_then_change),
                    patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                    patch.object(case.cloud, "rename", wraps=case.cloud.rename) as rename,
                    patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
                ):
                    with self.assertRaises((ValueError, confirmations.DirectoryScrapeConflictError)):
                        case.execute()
                    move.assert_not_called()
                    rename.assert_not_called()
                    delete.assert_not_called()
                self.assertEqual(reached, [True])
                self._assert_all_frozen_files_stay_in_source(case)

    def test_late_orphan_subtitle_is_rejected_before_conflicting_media_write(self):
        with self._build() as case:
            subtitle = self._add_subtitle(case)
            self.assertEqual(case.create(case.decision()), "approved")
            first_id = case.payload["files"][0]["file_id"]
            second_id = case.payload["files"][1]["file_id"]
            original_rename = case.cloud.rename
            original_list = case.cloud.list_dir
            injections = []
            reads = []

            def list_dir(parent_id):
                reads.append(parent_id)
                return original_list(parent_id)

            def rename_then_inject(file_id, new_name):
                result = original_rename(file_id, new_name)
                if file_id == first_id and not injections:
                    target = case.cloud.infos[first_id].parent_id
                    name = new_name.replace("S00E01", "S00E02").rsplit(".", 1)[0] + ".srt"
                    orphan = _file("external-orphan", name, target, size=128)
                    case.cloud.infos[orphan.file_id] = orphan
                    case.cloud.tree[target].append(orphan)
                    injections.append((orphan, len(reads)))
                return result

            with (
                patch.object(case.cloud, "rename", side_effect=rename_then_inject) as rename,
                patch.object(case.cloud, "list_dir", side_effect=list_dir),
                patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
            ):
                result = case.execute()
                moved_ids = {file_id for call in move.call_args_list for file_id in call.args[0]}
                renamed_ids = {call.args[0] for call in rename.call_args_list}
                self.assertNotIn(second_id, moved_ids)
                self.assertNotIn(subtitle.file_id, moved_ids)
                self.assertNotIn(second_id, renamed_ids)
                self.assertNotIn(subtitle.file_id, renamed_ids)
                delete.assert_not_called()
            self.assertEqual(len(injections), 1)
            orphan, cursor = injections[0]
            self.assertGreater(reads[cursor:].count(orphan.parent_id), 0)
            self.assertEqual(case.cloud.infos[orphan.file_id], orphan)
            self.assertEqual(
                [item.file_id for item in case.cloud.tree[orphan.parent_id] if item.name == orphan.name],
                [orphan.file_id],
            )
            self.assertEqual(case.cloud.infos[second_id].parent_id, "source")
            self.assertEqual(case.cloud.infos[subtitle.file_id].parent_id, "source")
            self.assertEqual(result["stats"]["moved"], 1)
            self.assertGreater(result["stats"].get("failed", 0), 0)
            get_organize_confirmation_result = db.get_organize_confirmation("episode-case")
            assert get_organize_confirmation_result is not None
            self.assertIn(get_organize_confirmation_result["status"], {"completed", "failed"})

    def test_subtitle_arriving_after_own_video_rename_blocks_companion_and_rolls_back_video(self):
        with self._build() as case:
            subtitle = self._add_subtitle(case)
            first_id = case.payload["files"][0]["file_id"]
            second = case.payload["files"][1]
            second_id = second["file_id"]
            self.assertEqual(case.create(case.decision()), "approved")
            original_rename = case.cloud.rename
            injections = []

            def rename_then_inject_same_episode_subtitle(file_id, new_name):
                result = original_rename(file_id, new_name)
                if file_id == second_id and not injections:
                    current = case.cloud.infos[file_id]
                    self.assertNotEqual(current.parent_id, "source")
                    self.assertIn("S00E02", current.name)
                    orphan = _file(
                        "external-late-same-episode-subtitle",
                        new_name.rsplit(".", 1)[0] + ".srt", current.parent_id, size=128,
                    )
                    case.cloud.infos[orphan.file_id] = orphan
                    case.cloud.tree[orphan.parent_id].append(orphan)
                    injections.append(orphan)
                return result

            with (
                patch.object(case.cloud, "rename", side_effect=rename_then_inject_same_episode_subtitle) as rename,
                patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
            ):
                result = case.execute()
                move_ids = {file_id for call in move.call_args_list for file_id in call.args[0]}
                rename_ids = {call.args[0] for call in rename.call_args_list}
                self.assertIn(second_id, move_ids)  # 证明不是在视频写前的旧窗口注入。
                self.assertNotIn(subtitle.file_id, move_ids)
                self.assertNotIn(subtitle.file_id, rename_ids)
                self.assertTrue(any(
                    second_id in call.args[0] and call.args[1] == "source"
                    for call in move.call_args_list
                ), "第二集视频应由既有回滚路径移回源目录")
                delete.assert_not_called()
            self.assertEqual(len(injections), 1)
            orphan = injections[0]
            self.assertEqual(case.cloud.infos[orphan.file_id], orphan)
            self.assertEqual(
                [item.file_id for item in case.cloud.tree[orphan.parent_id] if item.name == orphan.name],
                [orphan.file_id],
            )
            restored = case.cloud.infos[second_id]
            self.assertEqual((restored.parent_id, restored.name, restored.size, restored.etag), (
                second["parent_id"], second["name"], second["size"], second["etag"],
            ))
            self.assertEqual(case.cloud.infos[subtitle.file_id], subtitle)
            self.assertNotEqual(case.cloud.infos[first_id].parent_id, "source")
            self.assertIn("S00E01", case.cloud.infos[first_id].name)
            for item in case.payload["files"][2:]:
                self.assertEqual(case.cloud.infos[item["file_id"]].parent_id, "source")
            self.assertEqual(result["stats"]["moved"], 1)
            self.assertGreater(result["stats"].get("failed", 0), 0)
            row = db.get_organize_confirmation("episode-case")
            assert row is not None
            self.assertIn(row["status"], {"completed", "failed"})
            recorded = json.loads(row["result_json"])
            self.assertEqual(recorded["moved"], 1)
            self.assertGreater(recorded.get("failed", 0), 0)

    def test_frozen_source_change_between_preview_and_execution_is_not_rebased(self):
        for kind in ("video", "bound_companion"):
            with self.subTest(kind=kind), self._build() as case:
                subtitle = self._add_subtitle(case, video_index=0)
                file_id = case.payload["files"][0]["file_id"] if kind == "video" else subtitle.file_id
                self.assertEqual(case.create(case.decision()), "approved")
                original_organize = confirmations.Organizer.organize
                reached = []

                def organize_with_changed_source(
                    organizer, *args, original_organize=original_organize,
                    file_id=file_id, reached=reached, **kwargs,
                ):
                    if kwargs.get("dry_run") is False and not reached:
                        self._change_content(case, file_id)
                        reached.append(True)
                    return original_organize(organizer, *args, **kwargs)

                with (
                    patch.object(confirmations.Organizer, "organize", new=organize_with_changed_source),
                    patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                    patch.object(case.cloud, "rename", wraps=case.cloud.rename) as rename,
                    patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
                ):
                    try:
                        result = case.execute()
                    except (ValueError, confirmations.DirectoryScrapeConflictError):
                        result = None
                    move.assert_not_called()
                    rename.assert_not_called()
                    delete.assert_not_called()
                self.assertEqual(reached, [True])
                self._assert_all_frozen_files_stay_in_source(case)
                if result is not None:
                    self.assertEqual(result["stats"].get("moved", 0), 0)
                    self.assertGreater(result["stats"].get("failed", 0), 0)

    def test_valid_research_moves_all_files_and_allows_unrelated_target_subtitle(self):
        with self._build() as case:
            subtitle = self._add_subtitle(case)
            self.assertEqual(case.create(case.decision()), "approved")
            first_id = case.payload["files"][0]["file_id"]
            original_rename = case.cloud.rename
            outsiders = []

            def rename_then_add_unrelated(file_id, new_name):
                result = original_rename(file_id, new_name)
                if file_id == first_id and not outsiders:
                    parent = case.cloud.infos[first_id].parent_id
                    name = new_name.replace("S00E01", "S00E99").rsplit(".", 1)[0] + ".srt"
                    other = _file("unrelated-existing-subtitle", name, parent, size=128)
                    case.cloud.infos[other.file_id] = other
                    case.cloud.tree[parent].append(other)
                    outsiders.append(other)
                return result

            with patch.object(case.cloud, "rename", side_effect=rename_then_add_unrelated):
                result = case.execute()
            self.assertEqual(result["stats"]["moved"], len(case.payload["files"]))
            self.assertEqual(result["stats"].get("failed", 0), 0)
            for episode, item in enumerate(case.payload["files"], 1):
                current = case.cloud.infos[item["file_id"]]
                self.assertIn(f"S00E{episode:02d}", current.name)
                self.assertEqual(case.cloud.infos[current.parent_id].name, "Specials")
            second = case.cloud.infos[case.payload["files"][1]["file_id"]]
            actual_subtitle = case.cloud.infos[subtitle.file_id]
            self.assertEqual(actual_subtitle.parent_id, second.parent_id)
            self.assertEqual(actual_subtitle.name, second.name.rsplit(".", 1)[0] + ".srt")
            self.assertEqual(len(outsiders), 1)
            self.assertEqual(case.cloud.infos[outsiders[0].file_id], outsiders[0])
            self.assertEqual(case.cloud.deleted, [])
            get_organize_confirmation_result_2 = db.get_organize_confirmation("episode-case")
            assert get_organize_confirmation_result_2 is not None
            self.assertEqual(get_organize_confirmation_result_2["status"], "completed")

    def test_human_atomic_claim_during_revalidation_prevents_agent_ownership(self):
        with self._build() as case:
            decision = case.decision()
            original = service.revalidate_episode_research_receipt
            claims = []

            def revalidate_then_human_claim(*args, **kwargs):
                proposal = original(*args, **kwargs)
                if not claims:
                    claims.append(confirmations.start_confirmation(
                        "episode-case", 0, chat_id="owner-chat", actor="human",
                    ))
                return proposal

            with (
                patch.object(service, "revalidate_episode_research_receipt", side_effect=revalidate_then_human_claim),
                patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                patch.object(case.cloud, "rename", wraps=case.cloud.rename) as rename,
            ):
                self.assertEqual(case.create(decision), "ownership_lost")
                move.assert_not_called()
                rename.assert_not_called()
            self.assertEqual(len(claims), 1)
            row = db.get_organize_confirmation("episode-case")
            assert row is not None
            self.assertEqual((row["confirmation_actor"], row["status"]), ("human", "queued"))
            self.assertIsNone(confirmations._episode_research_for_execution(
                "episode-case", case.payload, 0, "human",
            ))
            self._assert_all_frozen_files_stay_in_source(case)

    def test_tampered_receipt_does_not_queue_or_fall_back_to_ordinary_numbering(self):
        with self._build() as case:
            decision = case.decision()
            receipt = copy.deepcopy(decision.episode_research_receipt)
            assert receipt is not None
            receipt["candidate_index"] = 999
            with patch.object(confirmations, "Organizer") as organizer:
                result = case.create(replace(decision, episode_research_receipt=receipt))
                organizer.assert_not_called()
            self.assertNotEqual(result, "approved")
            row = db.get_organize_confirmation("episode-case")
            assert row is not None
            self.assertEqual((row["status"], row["confirmation_actor"]), ("pending", ""))
            self._assert_all_frozen_files_stay_in_source(case)

    def test_expired_queued_receipt_never_enters_organizer(self):
        with self._build() as case:
            self.assertEqual(case.create(case.decision()), "approved")
            later = time.time() + 86400
            with (
                patch.object(time, "time", return_value=later),
                patch.object(confirmations, "Organizer") as organizer,
                self.assertRaises((ValueError, confirmations.DirectoryScrapeConflictError)),
            ):
                case.execute()
            organizer.assert_not_called()
            self._assert_all_frozen_files_stay_in_source(case)
            get_organize_confirmation_result_3 = db.get_organize_confirmation("episode-case")
            assert get_organize_confirmation_result_3 is not None
            self.assertEqual(get_organize_confirmation_result_3["status"], "failed")

    def test_switch_or_effective_rules_change_after_revalidation_blocks_write(self):
        for explicit, fault in ((False, "disabled"), (False, "rules_changed"),
                                (True, "disabled"), (True, "rules_changed"), (True, "agent_disabled")):
            with self.subTest(explicit_approval=explicit, fault=fault), self._build() as case:
                if explicit:
                    case.payload["review_only"] = True
                self.assertEqual(case.create(case.decision()), "approved")
                if explicit:
                    case.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
                    confirmations.start_confirmation("episode-case", 0, chat_id="owner-chat", actor="agent",
                        explicit_approval=True, dispatch=False)
                original = service.revalidate_episode_research_receipt
                reached = []

                def revalidate_then_revoke(
                    *args, original=original, fault=fault, reached=reached, **kwargs,
                ):
                    proposal = original(*args, **kwargs)
                    if fault == "disabled":
                        case.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "0"
                    elif fault == "agent_disabled":
                        case.values["AGENT_ENABLED"] = "0"
                    else:
                        case.enterContext(patch.object(
                            type(case.rules), "from_config",
                            return_value=replace(case.rules, target_dir_id="different-archive"),
                        ))
                    reached.append(True)
                    return proposal

                with (
                    patch.object(service, "revalidate_episode_research_receipt", side_effect=revalidate_then_revoke),
                    patch.object(case.cloud, "move", wraps=case.cloud.move) as move,
                    patch.object(case.cloud, "rename", wraps=case.cloud.rename) as rename,
                    patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
                ):
                    with self.assertRaises((ValueError, confirmations.DirectoryScrapeConflictError)):
                        case.execute()
                    move.assert_not_called()
                    rename.assert_not_called()
                    delete.assert_not_called()
                self.assertEqual(reached, [True])
                self._assert_all_frozen_files_stay_in_source(case)

    def test_switch_off_mid_batch_stops_new_writes_and_exposes_partial_failure(self):
        for explicit in (False, True):
            with self.subTest(explicit_approval=explicit), self._build() as case:
                if explicit:
                    case.payload["review_only"] = True
                self.assertEqual(case.create(case.decision()), "approved")
                if explicit:
                    case.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
                    confirmations.start_confirmation("episode-case", 0, chat_id="owner-chat", actor="agent",
                        explicit_approval=True, dispatch=False)
                original_move = case.cloud.move
                first_id = case.payload["files"][0]["file_id"]

                def move_then_disable(file_ids, parent_id):
                    result = original_move(file_ids, parent_id)
                    case.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "0"
                    return result

                with (
                    patch.object(case.cloud, "move", side_effect=move_then_disable) as move,
                    patch.object(case.cloud, "rename", wraps=case.cloud.rename) as rename,
                    patch.object(case.cloud, "delete", wraps=case.cloud.delete) as delete,
                ):
                    result = case.execute()
                    self.assertEqual([call.args[0] for call in move.call_args_list], [[first_id]])
                    self.assertTrue(all(call.args[0] == first_id for call in rename.call_args_list))
                    delete.assert_not_called()
                self.assertEqual(result["stats"]["moved"], 1)
                self.assertGreater(result["stats"].get("failed", 0), 0)
                for item in case.payload["files"][1:]:
                    self.assertEqual(case.cloud.infos[item["file_id"]].parent_id, "source")
                row = db.get_organize_confirmation("episode-case")
                # completed 表示这次 job 已结束，并不要求把旧部分失败语义改为 failed。
                assert row is not None
                self.assertIn(row["status"], {"completed", "failed"})
                recorded = json.loads(row["result_json"])
                self.assertEqual(recorded["moved"], 1)
                self.assertGreater(recorded.get("failed", 0), 0)

    def test_existing_guard_without_target_context_hook_remains_compatible(self):
        with self._build() as case:
            subtitle = self._add_subtitle(case)
            self.assertEqual(case.create(case.decision()), "approved")
            stages = []

            class LegacyGuard:
                media_write_attempted = False

                def __call__(self, plan, stage, *, target_files=()):
                    stages.append(stage)
                    if stage == "commit":
                        self.media_write_attempted = True

            guard = LegacyGuard()
            self.assertFalse(hasattr(guard, "bind_target_context"))
            self.assertFalse(hasattr(guard, "before_companion_write"))
            with patch.object(confirmations, "_AgentEpisodeWriteBoundary", return_value=guard):
                result = case.execute()
            self.assertIn("commit", stages)
            self.assertEqual(result["stats"]["moved"], 4)
            self.assertEqual(result["stats"].get("failed", 0), 0)
            second = case.cloud.infos[case.payload["files"][1]["file_id"]]
            self.assertEqual(case.cloud.infos[subtitle.file_id].parent_id, second.parent_id)
            self.assertEqual(
                case.cloud.infos[subtitle.file_id].name, second.name.rsplit(".", 1)[0] + ".srt",
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
