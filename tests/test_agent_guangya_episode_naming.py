"""共享真实ID季集/字幕算法的行为回归；不保存OBS/OBJ或Agent私有计划。"""
from __future__ import annotations
from typing import cast
from app.clients.guangya import GuangYaClient
import unittest
import re
from copy import deepcopy
from app.clients.guangya import GuangYaFile
from app.modules import guangya_workspace
from app.modules.guangya_episode_naming import GuangYaEpisodeNamingError, compile_episode_naming_operations, summarize_episode_files
from tests.support import PagedDirectoryTestMixin


def observed_plan(arguments):
    normalized = deepcopy(arguments)
    for group in normalized["groups"]:
        group.setdefault("target_episode_start", 1)
        group.setdefault("include_extras", group.get("source_season") == 0 or group.get("target_season") == 0)
    return normalized

def _episode_number(name: str) -> int:
    match = re.search(r"E(\d+)", name)
    assert match is not None, name
    return int(match[1])


class EpisodeNamingClient(PagedDirectoryTestMixin):
    def __init__(self, *, target_directories: bool = True, episodes_per_group: int = 100):
        self.logged_in = True
        self.credential_generation = 44
        self.closed = False
        self.directories: dict[str, list[GuangYaFile]] = {
            "0": [GuangYaFile("show", "狐妖小红娘", True, parent_id="0", etag="show")],
            "show": [
                GuangYaFile("release-a", "发布组A", True, parent_id="show", etag="a"),
                GuangYaFile("release-b", "发布组B", True, parent_id="show", etag="b"),
            ],
            "release-a": [],
            "release-b": [],
        }
        for episode in range(1, episodes_per_group + 1):
            self.directories["release-a"].append(
                GuangYaFile(
                    f"a-{episode}",
                    f"[A] Fox Spirit Matchmaker.S01E{episode:03d}.1080p.mkv",
                    False,
                    parent_id="release-a",
                    size=1000 + episode,
                    etag=f"a-{episode}",
                    extension="mkv",
                )
            )
            self.directories["release-b"].append(
                GuangYaFile(
                    f"b-{episode}",
                    f"[B] Fox Spirit Matchmaker.S02E{episode:03d}.1080p.mp4",
                    False,
                    parent_id="release-b",
                    size=2000 + episode,
                    etag=f"b-{episode}",
                    extension="mp4",
                )
            )
        if target_directories:
            self.directories["show"].extend(
                [
                    GuangYaFile("season-1", "Season 01", True, parent_id="show", etag="s1"),
                    GuangYaFile("season-2", "Season 02", True, parent_id="show", etag="s2"),
                ]
            )
            self.directories["season-1"] = [
                GuangYaFile(
                    "season-1-note",
                    "README.txt",
                    False,
                    parent_id="season-1",
                    size=1,
                    etag="note1",
                    extension="txt",
                )
            ]
            self.directories["season-2"] = [
                GuangYaFile(
                    "season-2-note",
                    "README.txt",
                    False,
                    parent_id="season-2",
                    size=1,
                    etag="note2",
                    extension="txt",
                )
            ]

    def list_dir(self, parent_id="0"):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def close(self):
        self.closed = True
        return True

class GuangYaEpisodeNamingTests(unittest.TestCase):
    @staticmethod
    def _arguments() -> dict:
            return observed_plan(
                {
                    "title": "狐妖小红娘",
                    "target_root": "/狐妖小红娘",
                    "groups": [
                        {
                            "source_path": "/狐妖小红娘/发布组A",
                            "source_season": 1,
                            "source_episode_start": 1,
                            "source_episode_end": 100,
                            "target_season": 1,
                            "expected_count": 100,
                        },
                        {
                            "source_path": "/狐妖小红娘/发布组B",
                            "source_season": 2,
                            "source_episode_start": 1,
                            "source_episode_end": 100,
                            "target_season": 2,
                            "expected_count": 100,
                        },
                    ],
                    "trigger_strm": False,
                }
            )

    @staticmethod
    def _plain_suffix_observation(*names: str) -> dict:
            return {
                "plan_id": "plain-suffix-audit",
                "scan_complete": True,
                "items": [
                    {
                        "file_id": f"plain-{index}",
                        "name": name,
                        "is_dir": False,
                        "media_kind": "video",
                        "parent_path": "/Fox Spirit Matchmaker",
                        "size": 1000 + index, "parent_id": "plain-parent", "etag": "v1", "updated_at": 1,
                    }
                    for index, name in enumerate(names, start=1)
                ],
            }

    def test_plain_suffix_mapping_excludes_specials_without_source_season(self):
            observation = self._plain_suffix_observation(
                "Fox Spirit Matchmaker 014.mkv",
                "Fox Spirit Matchmaker Special 014.mkv",
            )
            group = {
                "source_path": "/Fox Spirit Matchmaker",
                "source_episode_start": 14,
                "source_episode_end": 14,
                "target_season": 1,
                "expected_count": 1,
            }
            compiled = compile_episode_naming_operations(
                observation,
                title="Fox Spirit Matchmaker",
                target_root="/Fox Spirit Matchmaker",
                groups=[group],
            )

            self.assertEqual(compiled["selected_files"], 1)
            self.assertEqual(compiled["groups"][0]["matched"], 1)
            relocate = [item for item in compiled["operations"] if item["op"] == "relocate"]
            self.assertEqual([item["file_id"] for item in relocate], ["plain-1"])

            mismatch = dict(group, expected_count=2)
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "预期 2 集，实际匹配 1 集"):
                compile_episode_naming_operations(
                    observation,
                    title="Fox Spirit Matchmaker",
                    target_root="/Fox Spirit Matchmaker",
                    groups=[mismatch],
                )

    def test_more_than_two_hundred_media_files_requires_complete_season_split(self):
        client = EpisodeNamingClient(episodes_per_group=101)
        payload = guangya_workspace.list_files(cast(GuangYaClient, client), recursive=True, max_items=500, max_depth=2, parent_id='show')
        groups = self._arguments()['groups']
        for group in groups:
            group['source_episode_end'] = 101
            group['expected_count'] = 101
        with self.assertRaisesRegex(GuangYaEpisodeNamingError, '202 个媒体文件'):
            compile_episode_naming_operations(payload, title='狐妖小红娘', target_root='/狐妖小红娘', groups=groups)

    def test_compiler_uses_exact_observed_release_path(self):
        client = EpisodeNamingClient(episodes_per_group=3)
        payload = guangya_workspace.list_files(cast(GuangYaClient, client), recursive=True, max_items=100, max_depth=2, parent_id='show')
        compiled = compile_episode_naming_operations(payload, title='狐妖小红娘', target_root='/狐妖小红娘', groups=[{'source_path': '/狐妖小红娘/发布组A', 'source_season': 1, 'source_episode_start': 1, 'source_episode_end': 3, 'target_season': 1, 'expected_count': 3}])
        self.assertEqual(compiled['effective_total'], 3)
        self.assertEqual(compiled['groups'][0]['matched'], 3)

    @staticmethod
    def _b1_observation(*names: str, parent: str = "/Series/Release") -> dict:
            return {
                "plan_id": "b1-observation", "scan_complete": True,
                "items": [
                    {"file_id": f"B1-{i}", "name": name, "is_dir": False,
                     "media_kind": "video", "parent_path": parent, "size": 1000, "parent_id": "fixture-parent", "etag": "v1", "updated_at": 1}
                    for i, name in enumerate(names, 1)
                ],
            }

    @staticmethod
    def _b1_mapping(**changes) -> dict:
            return {
                "source_path": "/Series/Release",
                "source_episode_start": 1, "source_episode_end": 3,
                "target_season": 1, "target_episode_start": 1, "expected_count": 3,
                **changes,
            }

    @staticmethod
    def _mixed_observation(*files: dict) -> dict:
            return {"plan_id": "mixed-media", "scan_complete": True, "items": list(files)}

    @staticmethod
    def _media(handle: str, name: str, kind: str, parent: str = "/Series/Release") -> dict:
            return {
                "file_id": handle,
                "name": name,
                "is_dir": False,
                "media_kind": kind,
                "parent_path": parent,
                "size": 1000, "parent_id": "fixture-parent", "etag": "v1", "updated_at": 1,
            }

    def test_inspect_reports_subtitle_inventory_and_unmatched_reason_without_exposing_handles(self):
            observation = self._mixed_observation(
                self._media("V1", "Series S01E01.mkv", "video"),
                self._media("S1", "Series S01E01.zh.srt", "subtitle"),
                self._media("S2", "Unmatched.en.srt", "subtitle"),
            )

            data = summarize_episode_files(observation, target_root="/Series")

            self.assertEqual((data["video_count"], data["subtitle_count"]), (1, 2))
            self.assertEqual(data["matched_subtitle_count"], 1)
            self.assertEqual(data["unmatched_subtitle_count"], 1)
            self.assertEqual(data["subtitle_skips"], [{
                "name": "Unmatched.en.srt", "reason": "字幕未唯一匹配任何视频",
            }])
            self.assertNotIn("file_id", data["subtitle_skips"][0])

    def test_compiler_reports_unmatched_subtitle_as_not_included(self):
            observation = self._mixed_observation(
                self._media("V1", "Series S01E01.mkv", "video"),
                self._media("S1", "Loose.en.srt", "subtitle"),
            )
            compiled = compile_episode_naming_operations(
                observation, title="Series", target_root="/Series",
                groups=[self._b1_mapping(source_episode_end=1, expected_count=1)],
            )

            self.assertEqual((compiled["video_count"], compiled["subtitle_count"]), (1, 0))
            self.assertEqual(compiled["selected_files"], 1)
            self.assertEqual(compiled["unmatched_subtitle_count"], 1)
            self.assertEqual(compiled["subtitle_skips"], [{
                "name": "Loose.en.srt", "reason": "字幕未唯一匹配任何视频",
            }])
            self.assertNotIn("file_id", compiled["subtitle_skips"][0])

    def test_compiler_rejects_media_observation_without_real_handle(self):
            observation = self._mixed_observation(
                {**self._media("V1", "Series S01E01.mkv", "video"), "file_id": ""},
            )
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "缺少真实file_id"):
                compile_episode_naming_operations(
                    observation, title="Series", target_root="/Series",
                    groups=[self._b1_mapping(source_episode_end=1, expected_count=1)],
                )

    def test_compiler_includes_all_languages_with_shared_normalized_suffixes(self):
            observation = self._mixed_observation(
                self._media("V1", "Series S01E01.mkv", "video"),
                self._media("S1", "Series S01E01.chs.default.forced.srt", "subtitle"),
                self._media("S2", "Series S01E01.en.forced.default.ass", "subtitle"),
            )
            compiled = compile_episode_naming_operations(
                observation, title="Series", target_root="/Series",
                groups=[self._b1_mapping(source_episode_end=1, expected_count=1)],
            )

            self.assertEqual((compiled["video_count"], compiled["subtitle_count"]), (1, 2))
            self.assertEqual(compiled["selected_files"], 3)
            self.assertEqual(compiled["unmatched_subtitle_count"], 0)
            subtitle_ops = [item for item in compiled["operations"] if item.get("file_id") in {"S1", "S2"}]
            self.assertEqual(
                {item["file_id"]: item["new_name"] for item in subtitle_ops},
                {
                    "S1": "Series - S01E01.zh-Hans.forced.default.srt",
                    "S2": "Series - S01E01.en.forced.default.ass",
                },
            )
            self.assertTrue(all(item["op"] == "relocate" for item in subtitle_ops))

    def test_subtitle_pairing_never_crosses_parent_directories(self):
            observation = self._mixed_observation(
                self._media("VA", "Series S01E01.mkv", "video", "/Series/A"),
                self._media("VB", "Series S01E01.mkv", "video", "/Series/B"),
                self._media("SB", "Series S01E01.en.srt", "subtitle", "/Series/B"),
            )
            compiled = compile_episode_naming_operations(
                observation, title="Series", target_root="/Series",
                groups=[{
                    "source_path": "/Series/A", "source_episode_start": 1,
                    "source_episode_end": 1, "target_season": 1, "expected_count": 1,
                }],
            )

            self.assertEqual(compiled["video_count"], 1)
            self.assertEqual(compiled["subtitle_count"], 0)
            self.assertNotIn("SB", str(compiled["operations"]))

    def test_compiler_rejects_subtitle_ambiguity_for_selected_duplicate_stems(self):
            observation = self._mixed_observation(
                self._media("V1", "Series S01E01.mkv", "video"),
                self._media("V2", "Series S01E01.mp4", "video"),
                self._media("S1", "Series S01E01.en.srt", "subtitle"),
            )
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "所选视频存在字幕歧义.*多个视频具有相同 stem"):
                compile_episode_naming_operations(
                    observation, title="Series", target_root="/Series",
                    groups=[self._b1_mapping(source_episode_end=1, expected_count=2)],
                )

    def test_compiler_rejects_duplicate_normalized_subtitle_targets(self):
            observation = self._mixed_observation(
                self._media("V1", "Series S01E01.mkv", "video"),
                self._media("S1", "Series S01E01.chs.srt", "subtitle"),
                self._media("S2", "Series S01E01.zh-Hans.srt", "subtitle"),
            )
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "多个字幕归一化后目标名称重复"):
                compile_episode_naming_operations(
                    observation, title="Series", target_root="/Series",
                    groups=[self._b1_mapping(source_episode_end=1, expected_count=1)],
                )

    def test_selected_media_cap_counts_companions_at_two_hundred_and_two_hundred_one(self):
            def observation(video_total: int, subtitle_total: int) -> dict:
                return self._mixed_observation(*[
                    *[
                        self._media(f"V{i}", f"Series S01E{i:03d}.mkv", "video")
                        for i in range(1, video_total + 1)
                    ],
                    *[
                        self._media(f"S{i}", f"Series S01E{i:03d}.zh.srt", "subtitle")
                        for i in range(1, subtitle_total + 1)
                    ],
                ])

            at_limit = compile_episode_naming_operations(
                observation(100, 100), title="Series", target_root="/Series",
                groups=[self._b1_mapping(source_episode_end=100, expected_count=100)],
            )
            self.assertEqual((at_limit["video_count"], at_limit["subtitle_count"]), (100, 100))
            self.assertEqual(at_limit["selected_files"], 200)
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "201 个媒体文件"):
                compile_episode_naming_operations(
                    observation(101, 100), title="Series", target_root="/Series",
                    groups=[self._b1_mapping(source_episode_end=101, expected_count=101)],
                )

    def test_e00_explicitly_maps_to_season_zero_without_renaming_specials_directory(self):
            files = []
            for episode in range(53):
                stem = (
                    "[LAB] Acceptance.Show.S01E00.1080p"
                    if episode == 0
                    else f"[LAB] Acceptance.Show.S01E{episode:02d}.1080p"
                )
                files.append(self._media(f"V{episode}", f"{stem}.mkv", "video"))
                files.append(self._media(f"S{episode}", f"{stem}.zh.srt", "subtitle"))
            observation = self._mixed_observation(*files)
            compiled = compile_episode_naming_operations(
                observation, title="Acceptance Show", target_root="/Series",
                groups=[
                    {
                        "source_path": "/Series/Release", "source_season": 1,
                        "source_episode_start": 1, "source_episode_end": 52,
                        "target_season": 1, "target_episode_start": 1,
                        "expected_count": 52,
                    },
                    {
                        "source_path": "/Series/Release", "source_season": 1,
                        "source_episode_start": 0, "source_episode_end": 0,
                        "target_season": 0, "target_episode_start": 1,
                        "expected_count": 1,
                        "include_extras": True,
                    },
                ],
            )

            self.assertEqual((compiled["video_count"], compiled["subtitle_count"]), (53, 53))
            self.assertEqual(compiled["selected_files"], 106)
            special_video = next(
                operation for operation in compiled["operations"]
                if operation.get("file_id") == "V0"
            )
            self.assertEqual(special_video["target_path"], "/Series/Season 00")
            self.assertEqual(special_video["new_name"], "Acceptance Show - S00E01.mkv")
            special_subtitle = next(item for item in compiled["operations"] if item.get("file_id") == "S0")
            self.assertEqual(special_subtitle["target_path"], "/Series/Season 00")
            self.assertEqual(special_subtitle["new_name"], "Acceptance Show - S00E01.zh.srt")
            self.assertEqual(compiled["included_extra_count"], 1)

    def test_target_collision_between_mapping_groups_is_rejected(self):
            observation = self._mixed_observation(
                self._media("VA", "Series S01E01.mkv", "video", "/Series/A"),
                self._media("VB", "Series S01E01.mkv", "video", "/Series/B"),
            )
            group = {
                "source_episode_start": 1, "source_episode_end": 1,
                "target_season": 1, "expected_count": 1,
            }
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "多个篇章映射会生成同一目标"):
                compile_episode_naming_operations(
                    observation, title="Series", target_root="/Series",
                    groups=[dict(group, source_path="/Series/A"), dict(group, source_path="/Series/B")],
                )

    def test_mixed_inspect_separates_all_exception_types_after_regular_samples(self):
            regular = [f"Series S01E{i:02d}.mkv" for i in range(1, 7)]
            observation = self._b1_observation(
                *regular, "Series S01E14 - Movie.mkv", "Series S01E15 - AD.mkv",
                "Series S01E16 - 特典.mkv", "Series NCOP01.mkv",
                "zzz unknown.mkv", "Series S01E20-E21.mkv",
            )
            data = summarize_episode_files(observation, target_root="/Series")
            group = data["groups"][0]
            self.assertEqual((data["video_count"], data["regular_count"], data["extra_count"], data["unknown_count"]), (12, 6, 4, 2))
            self.assertEqual(group["positions"], [{"source_season": 1, "episodes": "1-6", "count": 6}])
            self.assertEqual(group["small_video_count"], 12)
            self.assertEqual({r["reason"] for r in group["extras"]}, {"movie", "advertisement", "extra", "special_media"})
            self.assertEqual({r["reason"] for r in group["unknown"]}, {"unparsed_episode", "multi_episode_file"})
            self.assertEqual({name for row in group["extras"] for name in row["samples"]}, {observation["items"][i]["name"] for i in range(6, 10)})
            self.assertEqual(len(group["samples"]), 3)
            self.assertTrue(group["samples_truncated"])
            self.assertEqual(data["mapping_status"], "needs_verification")
            self.assertNotIn("target_episode_start", str(data))

    def test_compact_exception_summary_keeps_full_counts_not_all_names(self):
            observation = self._b1_observation(
                *(f"Series S01E{i:03d}.mkv" for i in range(1, 101)),
                *(f"Series S03E{i:03d} - Movie.mkv" for i in range(1, 101)),
                "Series S03E101 - AD.mkv", "zzz unknown.mkv",
            )
            data = summarize_episode_files(observation, target_root="/Series")
            group = data["groups"][0]
            self.assertEqual(data["video_count"], 202)
            movies = next(row for row in group["extras"] if row["reason"] == "movie")
            self.assertEqual(movies["video_count"], 100)
            self.assertEqual(movies["positions"][0]["episodes"], "1-100")
            self.assertTrue(movies["samples_truncated"])
            self.assertEqual(len(movies["samples"]), 3)
            ads = next(row for row in group["extras"] if row["reason"] == "advertisement")
            self.assertEqual(ads["samples"], ["Series S03E101 - AD.mkv"])
            self.assertLess(len(str(data)), 4000)

    def test_shared_special_path_rules_and_explicit_extra_directories(self):
            for directory in ("Extras", "Bonus", "Movie", "AD", "特典", "Extras/disc1"):
                with self.subTest(directory=directory):
                    observation = self._b1_observation("Series S01E01.mkv", parent=f"/Series/{directory}")
                    data = summarize_episode_files(observation, target_root="/Series")
                    self.assertEqual(data["extra_count"], 1)
                    self.assertEqual(data["regular_count"], 0)
                    group = self._b1_mapping(source_path=f"/Series/{directory}", expected_count=1)
                    with self.assertRaisesRegex(GuangYaEpisodeNamingError, "非正片默认排除"):
                        compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
            # 作品根外的目录不影响分类，词内部的 Movie/AD 也不触发排除。
            observation = self._b1_observation("MovieMaker Adventure S01E01.mkv", parent="/Extras/Series/Release")
            data = summarize_episode_files(observation, target_root="/Extras/Series")
            self.assertEqual(data["regular_count"], 1)

    def test_plan_excludes_movie_ad_and_unknown_before_matching_count(self):
            observation = self._b1_observation(
                "Series S01E01.mkv", "Series S01E02.mkv", "Series S01E03.mkv",
                "Series S01E02 - Movie.mkv", "Series S01E03 - AD.mkv", "Series Unknown.mkv",
            )
            compiled = compile_episode_naming_operations(
                observation, title="Series", target_root="/Series", groups=[self._b1_mapping()],
            )
            self.assertEqual(compiled["selected_files"], 3)
            self.assertEqual(compiled["included_extra_count"], 0)
            self.assertEqual(compiled["groups"][0]["excluded_extra_count"], 2)
            self.assertEqual(compiled["groups"][0]["unknown_count"], 1)
            moves = [op for op in compiled["operations"] if op["op"] == "relocate"]
            self.assertEqual([op["file_id"] for op in moves], ["B1-1", "B1-2", "B1-3"])
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "预期 5 集，实际匹配 3 集"):
                compile_episode_naming_operations(
                    observation, title="Series", target_root="/Series", groups=[self._b1_mapping(expected_count=5)],
                )

    def test_explicit_false_excludes_extras_even_for_season_zero(self):
            for name in ("Series S01E01 - Movie.mkv", "Series S01E01 - AD.mkv", "Series S01E01 - 特典.mkv", "Series NCOP01.mkv", "Series S00E01.mkv"):
                with self.subTest(name=name):
                    observation = self._b1_observation(name)
                    group = self._b1_mapping(target_season=0, source_episode_end=1, expected_count=1, include_extras=False)
                    with self.assertRaisesRegex(GuangYaEpisodeNamingError, "非正片默认排除"):
                        compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
                    group["include_extras"] = True
                    compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
                    self.assertEqual(compiled["included_extra_count"], 1)
                    self.assertEqual(compiled["groups"][0]["included_extra_count"], 1)
                    self.assertIn(" - S00E01", compiled["operations"][-1]["new_name"])

    def test_include_extras_does_not_invent_unknown_or_multi_episode_positions(self):
            for name in ("unknown.mkv", "Series S01E04-E05.mkv", "Series S01E04E05.mkv"):
                with self.subTest(name=name), self.assertRaisesRegex(GuangYaEpisodeNamingError, "无法识别集号"):
                    compile_episode_naming_operations(
                        self._b1_observation(name), title="Series", target_root="/Series",
                        groups=[self._b1_mapping(include_extras=True, source_episode_end=9, expected_count=1)],
                    )

    def test_omitted_target_episode_start_keeps_legacy_default(self):
            observation = self._b1_observation("Series S03E05.mkv", "Series S03E07.mkv")
            group = self._b1_mapping(source_episode_start=5, source_episode_end=7, expected_count=2)
            del group["target_episode_start"]
            arguments = observed_plan({
                "title": "Series", "target_root": "/Series", "groups": [group],
            })
            self.assertEqual(arguments["groups"][0]["target_episode_start"], 1)
            self.assertFalse(arguments["groups"][0]["include_extras"])
            compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
            self.assertEqual([_episode_number(item["new_name"]) for item in compiled["operations"] if item.get("file_id")], [1, 3])

    def test_explicit_offsets_preserve_gaps_not_observed_file_count(self):
            observation = self._b1_observation("Series S03E02.mkv", "Series S03E05.mp4")
            for target_start in (7, 42, 119):
                with self.subTest(target_start=target_start):
                    group = self._b1_mapping(
                        source_episode_start=2, source_episode_end=5, source_season=3,
                        target_episode_start=target_start, expected_count=2,
                    )
                    args = observed_plan({"title": "Series", "target_root": "/Series", "groups": [group]})
                    compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=args["groups"])
                    moves = [op for op in compiled["operations"] if op.get("file_id")]
                    self.assertEqual([_episode_number(op["new_name"]) for op in moves], [target_start, target_start + 3])
                    self.assertFalse(args["groups"][0]["include_extras"])

    def test_correct_files_are_noop_and_partial_correct_files_not_rewritten(self):
            observation = self._b1_observation(
                "Series - S01E01.mkv", "Series - S01E02.mp4", "Series - S01E03.mkv",
                parent="/Series/Season 01",
            )
            group = self._b1_mapping(source_path="/Series/Season 01")
            with self.assertRaisesRegex(GuangYaEpisodeNamingError, "无需变更"):
                compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
            observation["items"][-1]["name"] = "Series S01E03.mkv"
            compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[group])
            self.assertEqual(compiled["skipped_noop"], 2)
            self.assertEqual([{k:v for k,v in op.items() if k != "expected"} for op in compiled["operations"]], [{"op": "rename", "file_id": "B1-3", "new_name": "Series - S01E03.mkv"}])
            self.assertEqual(compiled["operations"][0]["expected"]["name"], "Series S01E03.mkv")

    def test_unselected_video_companion_is_reported_without_being_moved(self):
            observation = self._b1_observation("Show.S01E01.mkv", "Show.S01E02.mkv")
            observation["items"].append({
                "file_id": "SUB-2", "name": "Show.S01E02.zh.srt", "is_dir": False,
                "media_kind": "subtitle", "parent_path": observation["items"][0]["parent_path"],
            })
            group = self._b1_mapping(source_episode_end=1, expected_count=1)
            compiled = compile_episode_naming_operations(
                observation, title="Series", target_root="/Series", groups=[group],
            )
            self.assertEqual(compiled["subtitle_count"], 0)
            self.assertEqual(compiled["unmatched_subtitle_count"], 0)
            self.assertEqual(compiled["unselected_subtitle_count"], 1)
            self.assertEqual(compiled["subtitle_skips"][0]["name"], "Show.S01E02.zh.srt")
            self.assertIn("未被本次", compiled["subtitle_skips"][0]["reason"])
            self.assertNotIn("SUB-2", str(compiled["operations"]))

    def test_legacy_s0_mapping_inherits_extra_selection_unless_explicit_false(self):
            observation = self._b1_observation("Series S00E01.mkv")
            for seasons in ({"source_season": 0}, {"target_season": 0}, {"source_season": 0, "target_season": 0}):
                for explicit_false in (False, True):
                    with self.subTest(seasons=seasons, explicit_false=explicit_false):
                        group = self._b1_mapping(source_episode_end=1, expected_count=1, **seasons)
                        if explicit_false:
                            group["include_extras"] = False
                        normalized = observed_plan({
                            "title": "Series", "target_root": "/Series", "groups": [group],
                        })["groups"][0]
                        self.assertIs(normalized["include_extras"], not explicit_false)
                        for candidate in (group, normalized):
                            if explicit_false:
                                with self.assertRaisesRegex(GuangYaEpisodeNamingError, "非正片默认排除"):
                                    compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[candidate])
                            else:
                                compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[candidate])
                                self.assertEqual(compiled["selected_files"], 1)
                                self.assertEqual(compiled["included_extra_count"], 1)

    def test_regular_mapping_still_excludes_movie_when_extra_flag_omitted(self):
            observation = self._b1_observation("Series S01E01.mkv", "Series S01E02 - Movie.mkv")
            group = self._b1_mapping(source_episode_end=2, expected_count=1)
            normalized = observed_plan({
                "title": "Series", "target_root": "/Series", "groups": [group],
            })["groups"][0]
            self.assertIs(normalized["include_extras"], False)
            for candidate in (group, normalized):
                compiled = compile_episode_naming_operations(observation, title="Series", target_root="/Series", groups=[candidate])
                self.assertEqual(compiled["selected_files"], 1)
                self.assertEqual(compiled["included_extra_count"], 0)
                self.assertEqual(compiled["groups"][0]["excluded_extra_count"], 1)
