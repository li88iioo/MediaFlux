"""文件名只读解析与发布格式教学 Agent 动作的真实隔离数据库契约。"""
from __future__ import annotations

import json
import time
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

from app import database as db
from app.agent.domain_catalog import build_tool_specs
from app.agent.errors import AgentToolError
from app.agent.kernel.capabilities import CapabilityRetriever, ToolCatalog, ToolEffect
from app.agent.kernel.ports.existing_actions import adapt_tool_spec
from app.agent.kernel.projection import DefaultProjector
from app.agent.models import RiskLevel
from app.agent.public_safety import public_tool_label
from app.agent.release_format_actions import (
    inspect_filenames,
    inspect_filenames_arguments,
    prepare_release_format,
    preview_release_format,
    save_release_format_confirmed,
    teaching_arguments,
)
from app.modules import recognition_knowledge as knowledge
from app.modules.recognition import formats
from app.modules.scraper import _parse_release_core, extract_recognition_context, parse_release_position
from tests.support import IsolatedDatabaseTestCase

TEMPLATE = "[Example-Team][{title}][track{episode}r{version}][{resolution}].mkv"
PARENT = "/Anime/Teaching"


def filename(episode: int, title: str = "星海航行") -> str:
    return f"[Example-Team][{title}][track{episode:03d}r2][1080p].mkv"


def teaching(parent: str = PARENT) -> dict:
    return {
        "draft": {
            "name": "轨道式发布编号",
            "template": TEMPLATE,
            "scope": "directory",
            "parent_path": parent,
        },
        "examples": [
            {"filename": filename(13), "title": "星海航行", "episode": 13},
            {"filename": filename(14), "title": "星海航行", "episode": 14},
        ],
        "filenames": [filename(15), "unrelated.mkv"],
    }


class ReleaseFormatAgentActionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()

    def test_teaching_tools_explain_runtime_directory_context_not_source_labels(self):
        from types import SimpleNamespace
        from app.agent.domain_catalog.configuration_management import register_specs

        specs = []
        register_specs(SimpleNamespace(register=specs.append))
        tools = [spec for spec in specs if spec.name in {"recognition.preview_release_format", "recognition.save_release_format"}]
        self.assertEqual(len(tools), 2)
        for tool in tools:
            description = tool.parameters["properties"]["draft"]["properties"]["parent_path"]["description"]
            self.assertIn("整理起点目录名/起点下的相对父目录", description)
            self.assertIn("显示别名", description)
            self.assertIn("保留已有规则原值", description)
            self.assertNotIn("本地完整路径、光鸭相对目录名", description)

    @staticmethod
    def rule_count() -> int:
        with db.get_conn() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM recognition_format_rules").fetchone()[0]
            )

    def test_read_is_safe_and_does_not_write_or_create_confirmation(self) -> None:
        result = preview_release_format(teaching())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.data["review_required"], 1)
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(len(result.data["resources"]), 2)
        self.assertEqual(self.rule_count(), 0)
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)
        public = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("preview_token", public)
        self.assertNotIn(TEMPLATE, public)
        self.assertNotIn(PARENT, public)
        for resource in result.data["resources"]:
            self.assertIn("before", resource)
            self.assertIn("after", resource)
            self.assertIn("filename", resource)
        effects = "".join(result.data["effects"])
        self.assertIn("特别篇/受保护 0 个", effects)
        self.assertIn("格式冲突 0 个", effects)
        self.assertIn("原有识别流程", effects)
        self.assertIn("不移动文件", effects)
        self.assertIn("不绑定 TMDB", effects)
        self.assertIn("不偏移季集编号", effects)
        self.assertNotIn("preview_token", json.dumps(result.model_data, ensure_ascii=False))

    def test_sample_only_preview_counts_samples_and_keeps_raw_model_summary(self) -> None:
        value = teaching()
        value["filenames"] = []
        result = preview_release_format(value)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(result.data["count"], 2)
        self.assertEqual(result.data["sample_count"], 2)
        self.assertEqual(result.data["batch_total"], 0)
        self.assertEqual(result.data["review_required"], 0)
        self.assertEqual(result.data["summary"]["total"], 2)
        self.assertEqual(result.model_data["summary"]["total"], 0)
        self.assertIn("已核对 2 个标注样本", result.summary)

    def test_confirmation_keeps_dotted_names_and_long_title_episode_visible(self) -> None:
        for title in ("星海航行", "星海航行" * 40):
            with self.subTest(long_title=len(title) > 100):
                value = teaching()
                value["draft"]["template"] = "Example.Team.{title}.track{episode}r{version}.{resolution}.mkv"
                value["examples"] = [
                    {"filename": f"Example.Team.{title}.track{episode:03d}r2.1080p.mkv",
                     "title": title, "episode": episode} for episode in (13, 14)
                ]
                value["filenames"] = []
                result, _ = prepare_release_format(value)
                text = result.data["resources"][0]["title"]
                self.assertIn("Example.Team.", text)
                self.assertIn("第13集", text)
                self.assertIn(title, text)

    def test_prepare_reuses_preview_token_and_stays_read_only(self) -> None:
        captured: list[dict] = []
        original = formats.preview

        def preview(value: dict) -> dict:
            result = original(value)
            captured.append(result)
            return result

        with patch.object(formats, "preview", side_effect=preview):
            result, token = prepare_release_format(teaching())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "confirmation_required")
        self.assertEqual(token, captured[0]["preview_token"])
        self.assertEqual(self.rule_count(), 0)

    def test_confirm_writes_one_rule_and_core_uses_it(self) -> None:
        value = teaching()
        preview, token = prepare_release_format(value)
        saved = save_release_format_confirmed(value, token)
        self.assertTrue(saved.ok)
        self.assertTrue(saved.data["created"])
        self.assertTrue(saved.data["enabled"])
        self.assertEqual(self.rule_count(), 1)
        self.assertEqual(_parse_release_core(filename(15), PARENT).context.episode, 15)
        self.assertNotIn("preview_token", json.dumps(preview.to_model_dict(), ensure_ascii=False))

    def test_repeated_confirmation_returns_same_id_without_second_insert(self) -> None:
        value = teaching()
        _, token = prepare_release_format(value)
        first = save_release_format_confirmed(value, token)
        second = save_release_format_confirmed(value, token)
        self.assertTrue(first.data["created"])
        self.assertTrue(second.data["duplicate"])
        self.assertFalse(second.data["created"])
        self.assertTrue(second.data["enabled"])
        with db.get_conn() as conn:
            rows = conn.execute("SELECT id FROM recognition_format_rules").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(first.model_data["draft"], second.model_data["draft"])

    def test_disabled_duplicate_is_not_reported_as_enabled_or_reenabled(self) -> None:
        value = teaching()
        _, token = prepare_release_format(value)
        first = save_release_format_confirmed(value, token)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,revision FROM recognition_format_rules"
            ).fetchone()
        formats.change(row[0], {"revision": row[1], "disabled": True})
        repeated = save_release_format_confirmed(value, token)
        self.assertTrue(repeated.data["duplicate"])
        self.assertFalse(repeated.data["enabled"])
        self.assertIn("已停用", repeated.summary)
        self.assertNotIn("已启用", repeated.summary)
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)
        self.assertEqual(first.data["enabled"], True)

    def test_saved_rule_receipt_exposes_stable_typed_identity(self) -> None:
        value = teaching()
        _, token = prepare_release_format(value)
        first = save_release_format_confirmed(value, token)
        second = save_release_format_confirmed(value, token)
        item = formats.list_rules()[0]
        for result in (first, second):
            self.assertEqual(result.data["rule_type"], "release_format")
            self.assertEqual(result.data["rule_id"], item["id"])
            self.assertEqual(result.model_data["rule_id"], item["id"])

    def test_cancel_without_confirmation_has_no_write(self) -> None:
        _, _token = prepare_release_format(teaching())
        self.assertEqual(self.rule_count(), 0)
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)

    def test_prepare_rejects_unreviewable_preview_without_writing(self) -> None:
        value = teaching()
        value["examples"][0]["episode"] = 999
        preview = preview_release_format(value)
        self.assertTrue(preview.ok)
        self.assertTrue(preview.data["review_required"])
        with self.assertRaises(AgentToolError) as raised:
            prepare_release_format(value)
        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertEqual(self.rule_count(), 0)

    def test_unknown_fields_and_insufficient_samples_are_rejected(self) -> None:
        value = teaching()
        for extra in ({"confirmed": True}, {"preview_token": "private"}, {"unknown": 1}):
            invalid = {**deepcopy(value), **extra}
            with self.subTest(extra=extra), self.assertRaises(AgentToolError):
                teaching_arguments(invalid)
        insufficient = deepcopy(value)
        insufficient["examples"] = insufficient["examples"][:1]
        with self.assertRaises(AgentToolError):
            teaching_arguments(insufficient)

    def test_tampered_expired_changed_and_deleted_confirmations_are_public_stale_errors(self) -> None:
        value = teaching()
        _, token = prepare_release_format(value)
        changed = deepcopy(value)
        changed["filenames"].append(filename(16))
        with self.assertRaises(AgentToolError) as tampered:
            save_release_format_confirmed(changed, token)
        self.assertEqual(tampered.exception.code, "confirmation_stale")
        with patch("itsdangerous.timed.time.time", return_value=time.time() + 901), self.assertRaises(AgentToolError) as expired:
            save_release_format_confirmed(value, token)
        self.assertEqual(expired.exception.code, "confirmation_stale")

        other = teaching("/Anime/Other")
        _, other_token = prepare_release_format(other)
        save_release_format_confirmed(other, other_token)
        with self.assertRaises(AgentToolError) as changed_registry:
            save_release_format_confirmed(value, token)
        self.assertEqual(changed_registry.exception.code, "confirmation_stale")

        candidate = teaching("/Anime/Candidate")
        _, candidate_token = prepare_release_format(candidate)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,revision FROM recognition_format_rules WHERE parent_path=?",
                ("/Anime/Other",),
            ).fetchone()
        formats.change(row[0], {"revision": row[1]}, delete=True)
        with self.assertRaises(AgentToolError) as deleted:
            save_release_format_confirmed(candidate, candidate_token)
        self.assertEqual(deleted.exception.code, "confirmation_stale")
        self.assertEqual(self.rule_count(), 0)


TOOL = "recognition.inspect_filenames"
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
            patch("app.agent.release_format_actions.extract_recognition_context",
                  wraps=extract_recognition_context) as context_parser,
            patch("app.agent.release_format_actions.parse_release_position",
                  wraps=parse_release_position) as position_parser,
        ):
            result = inspect_filenames({"filenames": SAMPLES})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(context_parser.call_count, 2)
        self.assertEqual(position_parser.call_count, 2)
        for index, (row, title, english, season, episode) in enumerate(zip(
            result.data["rows"], ["沧元图", "大主宰"],
            ["The Demon Hunter III", "The Great Ruler II"], [3, 2], [25, 38],
        )):
            self.assertEqual(row["filename"], SAMPLES[index])
            self.assertEqual(row["normalized_title"], title)
            self.assertIn(english, row["title_variants"])
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
        self.assertFalse(result.data["tmdb_verified"])
        self.assertIn("不是已核验作品", " ".join(result.data["effects"]))
        self.assertIn("不等于 TMDB", " ".join(result.data["effects"]))
        self.assertEqual(result.evidence[0].source, "builtin_filename_parser")

    def test_noise_unrecognized_and_partial_positions_stay_unresolved(self):
        result = inspect_filenames({"filenames": [
            "[1080p][HEVC][GB].mkv", "???", "E03.mkv", "电影.2026.2160p.HEVC.mkv",
        ]})
        noise, unknown, episode, movie = result.data["rows"]
        self.assertEqual(noise["normalized_title"], "")
        self.assertEqual(noise["unresolved_fields"], ["title", "season", "episode"])
        self.assertIn("[1080p]", noise["cleaned_components"]["release_prefixes"])
        self.assertEqual(unknown["unresolved_fields"], ["title", "season", "episode"])
        self.assertEqual(episode["context_position"], {"season": None, "episode": 3})
        self.assertEqual(episode["unresolved_fields"], ["title", "season"])
        self.assertEqual(movie["release_position"], {
            "season": None, "episode": None, "episode_end": None,
        })
        self.assertEqual(result.data["total"], 4)

    def test_parent_text_supplies_context_but_does_not_rewrite_filename_evidence(self):
        for parent in ("剧集/示例作品/Season 02", "/does-not-exist/剧集/示例作品/Season 02",
                       r"Z:\剧集\示例作品\Season 02"):
            with self.subTest(parent=parent):
                result = inspect_filenames({"filenames": ["E03.mkv"], "parent_path": parent})
                row = result.data["rows"][0]
                self.assertEqual(row["normalized_title"], "示例作品")
                self.assertEqual(row["folder_title"], "示例作品")
                self.assertEqual(row["release_position"]["season"], None)
                self.assertEqual(row["context_position"], {"season": 2, "episode": 3})
                self.assertTrue(result.data["parent_context_supplied"])
                self.assertNotIn(parent, json.dumps(result.to_dict(), ensure_ascii=False))
        row = inspect_filenames({"filenames": ["Demo.S03E25.mkv"],
                                 "parent_path": "剧集/示例作品/Season 02"}).data["rows"][0]
        self.assertEqual(row["context_position"]["season"], 3)

    def test_special_zero_range_and_duplicates_preserve_core_contract(self):
        names = ["Demo.S00E00.mkv", "Demo.S02E03-E05.1080p.mkv", SAMPLES[0], SAMPLES[0]]
        rows = inspect_filenames({"filenames": names}).data["rows"]
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
            {"filenames": SAMPLES, "draft": {}},
            {"filenames": SAMPLES, "save": True},
            {"filenames": SAMPLES, "tmdb_id": 123},
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(AgentToolError):
                inspect_filenames_arguments(args)
        args = {"filenames": SAMPLES, "parent_path": ""}
        self.assertEqual(inspect_filenames_arguments(args), args)
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
                result = inspect_filenames({"filenames": [*SAMPLES, "E03.mkv"], "parent_path": str(parent)})
            with original_get_conn() as conn:
                after_db = "\n".join(conn.iterdump())
            after_files = {str(p.relative_to(parent)): p.read_bytes() for p in parent.iterdir()}
        self.assertTrue(result.ok)
        self.assertEqual(mutations, [])
        self.assertEqual(before_db, after_db)
        self.assertEqual(before_files, after_files)
        self.assertEqual(formats.list_rules(), [])
        self.assertEqual(result.references, [])
        self.assertEqual(result.effect_metadata, {})

    def test_registered_read_tool_is_retrievable_and_projects_evidence_without_confirmation(self):
        specs = build_tool_specs()
        matches = [spec for spec in specs if spec.name == TOOL]
        self.assertEqual(len(matches), 1)
        spec = matches[0]
        self.assertEqual(spec.risk, RiskLevel.READ)
        self.assertFalse(spec.requires_confirmation)
        self.assertIsNone(spec.context_confirmation_preparer)
        self.assertIsNone(spec.context_confirmed_handler)
        self.assertEqual(spec.parameters["required"], ["filenames"])
        self.assertEqual(public_tool_label(TOOL), "文件名样本只读解析")
        catalog = ToolCatalog(adapt_tool_spec(item) for item in specs)
        self.assertEqual(catalog.get(TOOL).effect, ToolEffect.READ)
        for message in (
            "我有两个真实发布格式样本，想知道系统会怎样识别它们的作品、季和集。只告诉我识别结果和依据，不要保存规则、不要移动或改名：\n" + "\n".join(SAMPLES),
            "请逐个把原文件名、识别到的中文和英文标题、季号、集号，以及被清洗掉的发布/技术字段列出来；仍然只做说明，不保存规则、不移动、不改名。",
            "这些文件名清洗后是什么？只读解析，不用教规则。",
        ):
            with self.subTest(message=message):
                selected = CapabilityRetriever().retrieve(message, catalog)
                self.assertIn(TOOL, selected.names)
        result = spec.handler(spec.validator({"filenames": SAMPLES}))
        outcome = DefaultProjector().project(result)
        model = json.loads(outcome.model_message())
        self.assertNotIn("truncated", model)
        self.assertEqual(model["data"]["rows"][0]["release_position"]["episode"], 25)
        self.assertEqual(model["data"]["rows"][1]["context_position"]["season"], 2)
        self.assertIsNone(outcome.effect_plan)
