"""原生 recognition SDK 与公共服务，真实隔离 SQLite 和解析器。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from app import database as db, sdk
from app.modules import recognition_service, recognition_knowledge as knowledge
from app.modules.recognition import formats
from app.modules.scraper import _parse_release_core, extract_recognition_context, parse_release_position
from tests.support import IsolatedDatabaseTestCase
from tests.test_release_formats import PARENT, filename, teaching


class RecognitionTeachingServiceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()

    def test_preview_is_complete_and_read_only_without_agent_projection(self):
        value = teaching()
        value["filenames"] = [filename(i) for i in range(15, 115)]
        with patch("app.agent.kernel.projection.DefaultProjector.project", side_effect=AssertionError("原生读取不应投影截断")):
            result = sdk.recognition.preview_release_format(**value)
        self.assertEqual(result["summary"]["total"], 100)
        self.assertEqual(len(result["rows"]), 100)
        self.assertEqual(result["rows"][-1]["after"]["episode"], 114)
        self.assertEqual(len(result["examples"]), 2)
        self.assertTrue(result["can_save"])
        self.assertNotIn("preview_token", json.dumps(result))
        self.assertEqual(formats.list_rules(), [])
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)

    def test_sample_only_preview_keeps_raw_summary(self):
        value = teaching()
        value["filenames"] = []
        result = sdk.recognition.preview_release_format(**value)
        self.assertEqual(result["summary"]["total"], 0)
        self.assertEqual(len(result["examples"]), 2)
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["can_save"])

    def test_prepare_freezes_original_web_preview_and_does_not_write(self):
        with patch.object(formats, "preview", wraps=formats.preview) as preview:
            prepared = recognition_service.prepare_release_format(teaching())
        self.assertEqual(preview.call_count, 1)
        self.assertTrue(prepared["snapshot"]["preview_token"])
        self.assertNotIn("preview_token", prepared["preview"])
        self.assertEqual(prepared["snapshot"]["request"], formats.normalize_request(teaching()))
        self.assertEqual(formats.list_rules(), [])

    def test_save_reuses_web_transaction_and_disabled_duplicate_contract(self):
        value = teaching()
        prepared = recognition_service.prepare_release_format(value)
        first = recognition_service.save_release_format(value, expected_snapshot=prepared["snapshot"])
        self.assertTrue(first["data"]["created"])
        self.assertTrue(first["data"]["enabled"])
        self.assertEqual(_parse_release_core(filename(15), PARENT).context.episode, 15)
        item = formats.list_rules()[0]
        formats.change(item["id"], {"revision": item["revision"], "disabled": True})
        second = recognition_service.save_release_format(value, expected_snapshot=prepared["snapshot"])
        self.assertEqual(second["data"]["rule_id"], item["id"])
        self.assertEqual(second["data"]["rule_type"], "release_format")
        self.assertTrue(second["data"]["duplicate"])
        self.assertFalse(second["data"]["enabled"])
        self.assertIn("已停用", second["summary"])
        self.assertEqual(len(formats.list_rules()), 1)
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)

    def test_save_rejects_tampered_expired_and_changed_registry_snapshots(self):
        value = teaching()
        prepared = recognition_service.prepare_release_format(value)
        changed = deepcopy(value)
        changed["filenames"].append(filename(16))
        with self.assertRaises(formats.FormatConflict):
            recognition_service.save_release_format(changed, expected_snapshot=prepared["snapshot"])
        with patch("itsdangerous.timed.time.time", return_value=time.time() + 901), self.assertRaises(formats.FormatConflict):
            recognition_service.save_release_format(value, expected_snapshot=prepared["snapshot"])
        other = deepcopy(value)
        other["draft"]["parent_path"] = "/Anime/Other"
        other_prepared = recognition_service.prepare_release_format(other)
        recognition_service.save_release_format(other, expected_snapshot=other_prepared["snapshot"])
        with self.assertRaises(formats.FormatConflict):
            recognition_service.save_release_format(value, expected_snapshot=prepared["snapshot"])
        item = formats.list_rules()[0]
        formats.change(item["id"], {"revision": item["revision"]}, delete=True)
        with self.assertRaises(formats.FormatConflict):
            recognition_service.save_release_format(value, expected_snapshot=prepared["snapshot"])
        self.assertEqual(formats.list_rules(), [])

    def test_teaching_requires_explicit_distinct_samples_and_cannot_inject_confirmation(self):
        value = teaching()
        bad = [{**value, **extra} for extra in ({"confirmed": True}, {"preview_token": "private"}, {"unknown": 1})]
        bad += [{**value, "examples": value["examples"][:1]}, {**value, "examples": [value["examples"][0]] * 2}]
        for invalid in bad:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recognition_service.teaching_arguments(invalid)
        mismatch = deepcopy(value)
        mismatch["examples"][0]["episode"] = 999
        self.assertFalse(sdk.recognition.preview_release_format(**mismatch)["can_save"])
        with self.assertRaises(ValueError):
            recognition_service.prepare_release_format(mismatch)
        with self.assertRaises(ValueError):
            sdk.recognition.save_release_format(**value)
        self.assertEqual(formats.list_rules(), [])


SAMPLES = [
    "[GM-Team][国漫][沧元图 第3季][The Demon Hunter Ⅲ][2026][25][GB][4K HEVC 10Bit].mp4",
    "[GM-Team][国漫][大主宰 第2季][The Great Ruler Ⅱ][2026][38][HEVC][GB][4K].mp4",
]


class RecognitionInspectionTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 与 app.main 的正常启动一致；不把既有种子初始化算作 READ handler。
        knowledge.ensure_seed_knowledge()

    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()
        knowledge.invalidate_active_cache()

    def test_gm_team_batch_calls_real_parsers_and_returns_raw_release_positions(self):
        with (
            patch("app.modules.recognition_service.extract_recognition_context",
                  wraps=extract_recognition_context) as context_parser,
            patch("app.modules.recognition_service.parse_release_position",
                  wraps=parse_release_position) as position_parser,
        ):
            result = sdk.recognition.inspect_filenames(SAMPLES)
        self.assertEqual(result["total"], 2)
        self.assertEqual(context_parser.call_count, 2)
        self.assertEqual(position_parser.call_count, 2)
        for index, (row, title, season, episode) in enumerate(zip(
            result["rows"], ["沧元图", "大主宰"], [3, 2], [25, 38],
        )):
            self.assertEqual(row["filename"], SAMPLES[index])
            self.assertEqual(row["normalized_title"], title)
            context = extract_recognition_context(SAMPLES[index])
            for field in ("normalized_title", "filename_title", "folder_title", "title_variants", "cleaned_components"):
                self.assertEqual(row[field], getattr(context, field), field)
            self.assertEqual(row["filename_year"], "2026")
            self.assertEqual(row["release_position"], {
                "season": season, "episode": episode, "episode_end": None,
            })
            self.assertEqual(row["context_position"], {"season": season, "episode": episode})
            self.assertEqual(row["unresolved_fields"], [])
            fields = row["cleaned_components"]
            self.assertIn("GM-Team", fields["candidate_release_groups"])
            self.assertIn("国漫", fields["media_kinds"])
            self.assertIn("GB", fields["language_tags"])
            self.assertIn("HEVC", " ".join(fields["noise_tokens"]))
            self.assertIn("4K", " ".join(fields["noise_tokens"]))
        self.assertFalse(result["tmdb_verified"])
        self.assertIn("不是已核验作品", " ".join(result["effects"]))
        self.assertIn("不等于 TMDB", " ".join(result["effects"]))
        self.assertEqual(result["evidence"][0]["source"], "builtin_filename_parser")

    def test_long_fields_and_markup_are_returned_exactly_as_the_real_parser(self):
        samples = [
            ("[Long-Group" + "组" * 330 + "][" + "星海航行" * 90 + "].S03E25.mkv",
             "剧集/" + "银河物语" * 90 + "/Season 02"),
            ("Demo.<特别版>.S03E25.mkv", ""),
        ]
        for index, (name, parent) in enumerate(samples):
            context = extract_recognition_context(name, parent)
            with self.subTest(sample=index):
                if index == 0:
                    for field in ("normalized_title", "filename_title", "folder_title"):
                        self.assertGreater(len(getattr(context, field)), 300, field)
                    self.assertTrue(any(len(title) > 300 for title in context.title_variants))
                row = sdk.recognition.inspect_filenames([name], parent_path=parent)["rows"][0]
                row = json.loads(json.dumps(row, ensure_ascii=False))
                for field in ("normalized_title", "filename_title", "folder_title", "title_variants", "cleaned_components"):
                    self.assertEqual(row[field], getattr(context, field), field)
                self.assertEqual(row["release_position"], parse_release_position(name))

    def test_noise_unrecognized_and_partial_positions_stay_unresolved(self):
        result = sdk.recognition.inspect_filenames([
            "[1080p][HEVC][GB].mkv", "???", "E03.mkv", "电影.2026.2160p.HEVC.mkv",
        ])
        noise, unknown, episode, movie = result["rows"]
        self.assertEqual(noise["normalized_title"], "")
        self.assertEqual(noise["unresolved_fields"], ["title", "season", "episode"])
        self.assertIn("[1080p]", noise["cleaned_components"]["release_prefixes"])
        self.assertEqual(unknown["unresolved_fields"], ["title", "season", "episode"])
        self.assertEqual(episode["context_position"], {"season": None, "episode": 3})
        self.assertEqual(episode["unresolved_fields"], ["title", "season"])
        self.assertEqual(movie["release_position"], {
            "season": None, "episode": None, "episode_end": None,
        })
        self.assertEqual(result["total"], 4)

    def test_parent_text_supplies_context_but_does_not_rewrite_filename_evidence(self):
        for parent in ("剧集/示例作品/Season 02", "/does-not-exist/剧集/示例作品/Season 02",
                       r"Z:\剧集\示例作品\Season 02"):
            with self.subTest(parent=parent):
                result = sdk.recognition.inspect_filenames(["E03.mkv"], parent_path=parent)
                row = result["rows"][0]
                self.assertEqual(row["normalized_title"], "示例作品")
                self.assertEqual(row["folder_title"], "示例作品")
                self.assertEqual(row["release_position"]["season"], None)
                self.assertEqual(row["context_position"], {"season": 2, "episode": 3})
                self.assertTrue(result["parent_context_supplied"])
                self.assertNotIn(parent, json.dumps(result, ensure_ascii=False))
        row = sdk.recognition.inspect_filenames(["Demo.S03E25.mkv"], parent_path="剧集/示例作品/Season 02")["rows"][0]
        self.assertEqual(row["context_position"]["season"], 3)

    def test_special_zero_range_and_duplicates_preserve_core_contract(self):
        names = ["Demo.S00E00.mkv", "Demo.S02E03-E05.1080p.mkv", SAMPLES[0], SAMPLES[0]]
        rows = sdk.recognition.inspect_filenames(names)["rows"]
        self.assertEqual(rows[0]["release_position"], {"season": 0, "episode": 0, "episode_end": None})
        self.assertEqual(rows[0]["unresolved_fields"], [])
        self.assertEqual(rows[1]["release_position"], {"season": 2, "episode": 3, "episode_end": 5})
        self.assertEqual([row["filename"] for row in rows], names)
        self.assertEqual([row["index"] for row in rows], [1, 2, 3, 4])

    def test_arguments_reject_teaching_write_fields_paths_and_invalid_inputs(self):
        bad = [
            None, [], {}, {"filenames": SAMPLES[0]}, {"filenames": []},
            {"filenames": [True]}, {"filenames": [""]}, {"filenames": ["   "]},
            {"filenames": ["bad\nname.mkv"]}, {"filenames": ["bad\x00name.mkv"]},
            {"filenames": ["/tmp/episode.mkv"]}, {"filenames": [r"C:\episode.mkv"]},
            {"filenames": ["a" * 1025]}, {"filenames": ["E03.mkv"] * 101},
            {"filenames": SAMPLES, "parent_path": True},
            {"filenames": SAMPLES, "parent_path": "a" * 4097},
            {"filenames": SAMPLES, "parent_path": "secret\x7f"},
            {"filenames": ["Show.token=fixture-private-value.S02E03.mkv"]},
            {"filenames": ["E03.mkv"], "parent_path": "access_token=fixture-private-value"},
            {"filenames": SAMPLES, "draft": {}},
            {"filenames": SAMPLES, "save": True},
            {"filenames": SAMPLES, "tmdb_id": 123},
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                recognition_service.inspect_filenames_arguments(cast(dict[str, Any], args))
        args = {"filenames": SAMPLES, "parent_path": ""}
        self.assertEqual(recognition_service.inspect_filenames_arguments(args), args)
        self.assertEqual(args["filenames"], SAMPLES)

    def test_handler_does_not_write_database_or_access_sample_files(self):
        mutations = []
        original_get_conn = db.get_conn
        forbidden = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
        }

        def authorize(action, *details):
            if action in forbidden:
                mutations.append((action, details))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        @contextmanager
        def readonly_conn():
            with original_get_conn() as conn:
                conn.set_authorizer(authorize)
                yield conn

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "示例作品" / "Season 02"
            parent.mkdir(parents=True)
            sample = parent / "E03.mkv"
            sample.write_bytes(b"unchanged synthetic sample")
            before_files = {str(p.relative_to(parent)): p.read_bytes() for p in parent.iterdir()}
            with original_get_conn() as conn:
                before_db = "\n".join(conn.iterdump())
            with (
                patch("app.database.get_conn", readonly_conn),
                patch("app.modules.recognition_knowledge.get_conn", readonly_conn),
                patch.object(Path, "open", side_effect=AssertionError("must not read samples")),
                patch.object(Path, "iterdir", side_effect=AssertionError("must not browse samples")),
                patch("socket.create_connection", side_effect=AssertionError("no external lookup")),
            ):
                sdk.recognition.inspect_filenames([*SAMPLES, "E03.mkv"], parent_path=str(parent))
            with original_get_conn() as conn:
                after_db = "\n".join(conn.iterdump())
            after_files = {str(p.relative_to(parent)): p.read_bytes() for p in parent.iterdir()}
        self.assertEqual(mutations, [])
        self.assertEqual(before_db, after_db)
        self.assertEqual(before_files, after_files)
        self.assertEqual(formats.list_rules(), [])


    def test_sdk_help_has_one_native_contract_and_complete_hundred_row_result(self):
        from app.agent.kernel.bootstrap import build_agent_kernel_runtime
        from app.agent.kernel.capabilities import ToolEffect
        from tests.support import initialized_web_runtime
        from tests.test_agent_kernel_core import ScriptedModel

        names = {"recognition.inspect_filenames", "recognition.preview_release_format", "recognition.save_release_format"}
        with initialized_web_runtime():
            kernel = build_agent_kernel_runtime(model=ScriptedModel([])).session
            self.assertIs(kernel.catalog.get("recognition.save_release_format").effect, ToolEffect.WRITE)
            model_names = {spec.name for spec in kernel.visible_model_tools({})}
            self.assertEqual(model_names, {"agent.run_code"})
            self.assertFalse(names & model_names)
        methods = {item["name"]: item for item in sdk.describe_methods()}
        self.assertTrue(names <= methods.keys())
        self.assertIn("recognition", sdk.describe_namespaces())
        self.assertEqual(methods["recognition.preview_release_format"]["signature"], methods["recognition.save_release_format"]["signature"])
        description = methods["recognition.preview_release_format"]["doc"]
        for instruction in ("整理起点目录名/起点下的相对父目录", "显示别名", "保留已有规则原值", "不要要求用户写模板", "缺少信息先询问"):
            self.assertIn(instruction, description)
        self.assertEqual(names & sdk.WRITE_METHODS, {"recognition.save_release_format"})
        names = [f"Show.S02E{i:02d}.mkv" for i in range(1, 101)]
        with patch("app.agent.kernel.projection.DefaultProjector.project", side_effect=AssertionError("原生读取不应投影截断")):
            result = sdk.recognition.inspect_filenames(names)
        self.assertEqual(len(result["rows"]), 100)
        self.assertEqual([row["filename"] for row in result["rows"]], names)
        self.assertEqual(result["rows"][-1]["release_position"]["episode"], 100)
