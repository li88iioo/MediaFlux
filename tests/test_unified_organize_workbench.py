"""统一整理入口与光鸭/本地只读日志时间线契约。"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class UnifiedOrganizeWorkbenchTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_task_items")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM organize_log")
        self.client = TestClient(create_app(start_background=False))
        login = self.client.get("/login")
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login.text).group(1)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def tearDown(self) -> None:
        self.client.close()

    def _seed_timeline(self) -> tuple[int, int]:
        guangya_id = db.add_organize_log(
            "guangya", "1/Anime", "动漫/Anime/Season 01/Anime.S01E01.mkv",
            "cloud-file", "skipped", "100",
            original_parent_id="1", original_name="Anime.E01.mkv",
            current_parent_id="1", current_name="Anime.E01.mkv",
            media_type="tv", title="Anime", error="目标已存在",
        )
        source_id = db.create_local_media_source(
            name="qB 动漫", qb_profile="", qb_path_prefix="",
            local_root="/downloads/anime", owner="admin",
        )
        local_id = db.create_local_media_task(
            source_id, "", "/downloads/anime/Local.Show.S01E01.mkv",
            owner="admin", trigger="manual",
        )
        db.add_local_media_task_item(
            local_id, "/downloads/anime/Local.Show.S01E01.mkv",
            "/media/anime/Local Show/Season 01/Local.Show.S01E01.mkv",
            role="video", owner="admin",
        )
        db.update_local_media_task(
            local_id, owner="admin", status="failed", title="Local Show",
            media_type="tv", tmdb_id="200", error="目标目录不可写",
        )
        return guangya_id, local_id

    def test_metatube_panel_defaults_collapsed_and_tracks_switch_state(self):
        rules = self.client.get("/organize-rules")
        self.assertEqual(rules.status_code, 200)
        html = rules.text + Path("app/static/js/organize.js").read_text(encoding="utf-8")
        self.assertIn('id="organizeConfigForm"', html)
        self.assertIn('onsubmit="return false;" novalidate', html)
        self.assertIn('id="r_nsfw_token"', html)
        self.assertIn('type="password"', html)
        self.assertIn('id="nsfwProviderPanel"', html)
        self.assertIn('class="nsfw-provider-panel is-disabled is-collapsed"', html)
        self.assertIn('id="saveOrganizeConfigBtn" disabled aria-busy="true"', html)
        self.assertIn('id="nsfwProviderDisclosure" aria-expanded="false"', html)
        self.assertIn('id="nsfwProviderBody" hidden', html)
        self.assertIn("function setNsfwPanelExpanded", html)
        self.assertIn("syncNsfwPanel({collapseWhenDisabled:true", html)
        self.assertIn('id="nsfwSourceIds"', html)
        self.assertIn('id="nsfwSourceOptions"', html)
        self.assertIn("已启用 · ${nsfwSourceIds.length} 源", html)
        self.assertIn("已启用 · 待分配", html)
        self.assertIn("未选中的普通来源不会调用 MetaTube", html)
        self.assertIn("focusEndpoint:bool('r_nsfw')", html)
        self.assertIn("function finishConfigLoad(success)", html)
        self.assertIn("if(!configReady)return", html)
        self.assertIn("const serial=++statusRequestSerial", html)
        self.assertIn("if(serial!==statusRequestSerial)return {ok:true,stale:true}", html)
        self.assertIn("renderStatus(data)", html)
        self.assertIn("if(organizeActionBusy)return", html)

    def test_execute_page_server_renders_saved_directories_before_config_recheck(self):
        from unittest.mock import patch

        values = {
            "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"11","name":"电视剧"},{"id":"22","name":"动漫"}]',
            "GY_ORGANIZE_TARGET_DIR": "99",
            "GY_ORGANIZE_TARGET_DIR_NAME": "整理",
        }
        with patch("app.routes.pages.config.get", side_effect=lambda key, default="": values.get(key, default)):
            organize = self.client.get("/organize")

        self.assertEqual(organize.status_code, 200)
        self.assertIn('class="organize-workspace is-config-loading"', organize.text)
        self.assertIn('aria-busy="true"', organize.text)
        self.assertIn('(支持多选 · 已选 2 项)', organize.text)
        self.assertIn('电视剧 (ID: 11)', organize.text)
        self.assertIn('动漫 (ID: 22)', organize.text)
        self.assertIn('整理 (ID: 99)', organize.text)
        self.assertNotIn('(支持多选 · 已选 0 项)', organize.text)
        self.assertNotIn('organize-source-skeleton-row', organize.text)
        self.assertIn('id="addOrganizeSourceBtn" disabled', organize.text)
        self.assertIn('id="pickOrganizeTargetBtn" disabled', organize.text)
        self.assertIn('id="previewBtn" disabled', organize.text)
        self.assertIn('id="runOrganizeBtn" disabled', organize.text)

        script = Path("app/static/js/organize.js").read_text(encoding="utf-8")
        self.assertIn("workspace.classList.remove('is-config-loading')", script)
        self.assertIn("workspace.setAttribute('aria-busy','false')", script)
        self.assertNotIn("MIN_EXECUTE_CONFIG_SKELETON_MS", script)

    def test_metatube_enabled_state_is_server_rendered_without_expand_shift(self):
        from unittest.mock import patch

        def enabled_for_nsfw(key, default=False):
            return True if key == "GY_ORGANIZE_NSFW_ENABLED" else default

        with patch("app.routes.pages.config.get_bool", side_effect=enabled_for_nsfw):
            rules = self.client.get("/organize-rules")
        self.assertEqual(rules.status_code, 200)
        self.assertIn('class="nsfw-provider-panel" id="nsfwProviderPanel"', rules.text)
        self.assertIn('id="nsfwProviderDisclosure" aria-expanded="true"', rules.text)
        self.assertNotIn('id="nsfwProviderBody" hidden', rules.text)
        self.assertIn('aria-label="启用 MetaTube 番号识别" checked', rules.text)

    def test_navigation_separates_cloud_execution_shared_rules_and_local_media(self):
        organize = self.client.get("/organize")
        rules = self.client.get("/organize-rules")
        local = self.client.get("/local-media")
        self.assertEqual(organize.status_code, 200)
        self.assertEqual(rules.status_code, 200)
        self.assertEqual(local.status_code, 200)
        self.assertNotIn('class="organize-source-switch"', organize.text + local.text)
        self.assertIn('id="organizeSourceDirs"', organize.text)
        self.assertIn('data-extension-value="mkv"', rules.text)
        self.assertIn('data-extension-value="nfo"', rules.text)
        self.assertNotIn('data-key="GY_ORGANIZE_REGION_SPLIT"', organize.text)
        self.assertIn('data-key="GY_ORGANIZE_REGION_SPLIT"', rules.text)
        self.assertNotIn('data-key="MEDIA_MOVIE_TEMPLATE"', rules.text)
        self.assertNotIn('data-key="GY_ORGANIZE_RENAME"', rules.text)
        self.assertNotIn('data-key="GY_ORGANIZE_MEDIAINFO"', rules.text)
        self.assertNotIn('data-key="GY_ORGANIZE_MEDIA_PROBE_ENABLED"', rules.text)
        self.assertIn('data-key="ORGANIZE_DOUBAN_HINTS_ENABLED"', rules.text)
        self.assertIn('data-key="ORGANIZE_BANGUMI_HINTS_ENABLED"', rules.text)
        self.assertIn("普通 TMDB 失败后使用豆瓣标题与年份重试", rules.text)
        self.assertNotIn('id="runOrganizeBtn"', rules.text)
        self.assertIn('class="organize-page organize-rules-page persistent-savebar-page compact-workspace-page"', rules.text)
        self.assertIn('<h1 class="page-title">整理规则</h1>', rules.text)
        self.assertNotIn('id="organizeRulesStep"', rules.text)
        self.assertNotIn('organize-rules-workspace-heading', rules.text)
        self.assertNotIn('id="organizeRulesTitle"', rules.text)
        self.assertLess(rules.text.index('<h1 class="page-title">整理规则</h1>'), rules.text.index('id="organizeWorkspace"'))
        self.assertIn('data-lucide="scan-search"', rules.text)
        self.assertIn('data-lucide="layers-3"', rules.text)
        self.assertIn('data-lucide="send"', rules.text)
        self.assertIn('organize-rules-nav-card', rules.text)
        self.assertLess(rules.text.index('organize-rules-nav-card'), rules.text.index('class="card card-pad organize-config-card"'))
        self.assertIn('data-panel-step="STEP 02 / MATCHING &amp; NAMING"', rules.text)
        self.assertIn('data-panel-step="STEP 03 / ARCHIVE POLICY"', rules.text)
        self.assertIn('data-panel-step="STEP 04 / DELIVERY CHAIN"', rules.text)
        self.assertNotIn('<h3>统一整理规则</h3>', rules.text)
        self.assertNotIn('class="organize-card-header"', rules.text)
        self.assertIn('class="organize-card-header"', organize.text)
        self.assertIn('id="tmdbRegexRulesModal"', rules.text)
        self.assertNotIn('id="tmdbRegexRulesModal"', organize.text)
        base = Path("app/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("<span>光鸭整理</span>", base)
        self.assertIn("<span>整理规则</span>", base)
        self.assertIn("<span>本地整理</span>", base)

    def test_timeline_keeps_colliding_ids_isolated_and_filters_sources(self):
        guangya_id, local_id = self._seed_timeline()
        rows = db.list_organize_timeline(owner="admin", limit=20)
        self.assertEqual({row["origin"] for row in rows}, {"guangya", "local"})
        self.assertEqual(db.count_organize_timeline(owner="admin"), 2)
        self.assertEqual(db.count_organize_timeline(owner="admin", origin="guangya"), 1)
        self.assertEqual(db.count_organize_timeline(owner="admin", origin="local"), 1)
        self.assertEqual(db.count_organize_timeline(owner="admin", status="failed"), 1)
        self.assertEqual(db.count_organize_timeline(owner="admin", status="skipped"), 1)

        response = self.client.get("/api/logs/organize/timeline")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        items = payload["items"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual({item["record_key"] for item in items}, {
            f"guangya:{guangya_id}", f"local:{local_id}",
        })
        local_item = next(item for item in items if item["origin"] == "local")
        cloud_item = next(item for item in items if item["origin"] == "guangya")
        self.assertFalse(local_item["actions"]["detail"])
        self.assertFalse(local_item["actions"]["batch"])
        self.assertIn("/media/anime/Local Show/Season 01", local_item["new_path"])
        self.assertTrue(cloud_item["actions"]["detail"])
        self.assertTrue(cloud_item["actions"]["batch"])

        filtered = self.client.get(
            "/api/logs/organize/timeline",
            params={"origin": "local", "status": "failed", "q": "qB 动漫"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["record_key"], f"local:{local_id}")

    def test_guangya_manual_record_is_not_counted_as_skipped(self):
        log_id = db.add_organize_log(
            "guangya", "下载/待确认电影", "", "manual-file", "manual", "",
            original_parent_id="parent", original_name="Pending.Movie.mkv",
            current_parent_id="parent", current_name="Pending.Movie.mkv",
            media_type="movie", title="待确认电影", error="需要人工确认",
            legacy_incomplete=False,
        )

        counts = db.count_organize_timeline_by_status(owner="admin")
        self.assertEqual(counts["manual"], 1)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual(counts["failed"], 0)
        response = self.client.get(
            "/api/logs/organize/timeline", params={"status": "manual"},
        )
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["record_key"], f"guangya:{log_id}")
        self.assertEqual(item["status"], "manual")
        self.assertTrue(item["actions"]["detail"])
        self.assertFalse(item["actions"]["batch"])

    def test_legacy_manual_skip_is_collapsed_after_later_success(self):
        pending_id = db.add_organize_log(
            "guangya", "下载/旧待确认电影", "", "legacy-manual", "skipped", "",
            original_parent_id="parent", original_name="Legacy.Movie.mkv",
            current_parent_id="parent", current_name="Legacy.Movie.mkv",
            media_type="movie", title="旧待确认电影", error="TMDB 匹配结果需人工确认",
            legacy_incomplete=False,
        )
        success_id = db.add_organize_log(
            "guangya", "下载/旧待确认电影",
            "电影/旧待确认电影 (2026) {tmdb-9}/Legacy.Movie.mkv",
            "legacy-manual", "success", "9",
            original_parent_id="parent", original_name="Legacy.Movie.mkv",
            current_parent_id="target", current_name="Legacy.Movie.mkv",
            target_parent_id="target", media_type="movie", title="旧待确认电影",
            legacy_incomplete=False,
        )

        rows = db.list_organize_timeline(owner="admin", origin="guangya", limit=20)
        self.assertNotIn(pending_id, [int(row["id"]) for row in rows])
        self.assertEqual([int(row["id"]) for row in rows], [success_id])
        counts = db.count_organize_timeline_by_status(owner="admin")
        self.assertEqual(counts["success"], 1)
        self.assertEqual(counts["manual"], 0)
        self.assertEqual(counts["skipped"], 0)

    def test_timeline_rejects_unknown_filters_and_page_uses_read_only_endpoint(self):
        self.assertEqual(
            self.client.get("/api/logs/organize/timeline", params={"origin": "other"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/logs/organize/timeline", params={"status": "deleted"}).status_code,
            400,
        )
        page = self.client.get("/logs")
        source = page.text + Path("app/static/js/logs.js").read_text(encoding="utf-8")
        self.assertIn('id="orgOrigin"', source)
        self.assertIn("/api/logs/organize/timeline", source)
        self.assertIn("已选 ${count} 条光鸭记录", source)
        self.assertIn("function _attr(v)", source)
        self.assertIn("requestSerial!==organizeRequestSerial", source)
        self.assertNotIn('title="${_esc(r.source_label)}"', source)


if __name__ == "__main__":
    import unittest
    unittest.main()
