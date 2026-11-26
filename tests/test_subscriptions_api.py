"""统一订阅中心 API 与页面契约测试。"""
from __future__ import annotations

from tests.support import seed_media_subscription_candidates

from typing import cast
from starlette.requests import Request

import asyncio
import json
import re
import sqlite3
import socket
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient

from app import database as db
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.repositories import discovery as discovery_repo
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class SubscriptionReceiptBrowserTests(IsolatedDatabaseTestCase):
    """真实页面、请求助手与 API 合同；外部媒体/下载后端由固定回执隔离。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright

        if sync_playwright is None:
            raise unittest.SkipTest("未安装 Playwright")
        playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = playwright.chromium.launch(
            executable_path=executable, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        from tests.test_config_empty_values_api import login

        with db.get_conn() as conn:
            for table in ("media_download_admissions", "media_subscription_candidates",
                          "media_subscription_runs", "media_subscriptions"):
                conn.execute(f"DELETE FROM {table}")
        self.subscription_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034", media_type="movie",
            title="回执测试", download_target="both",
        )
        real_connect = socket.socket.connect

        def offline_connect(sock, address):
            if address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise OSError("offline subscription test: outbound connection blocked")
            return real_connect(sock, address)

        self.enterContext(patch.object(socket.socket, "connect", offline_connect))
        self.client = self.enterContext(TestClient(create_app(start_background=False)))
        login(self.client)
        self.errors = []
        self.requests = []
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, reduced_motion="reduce", service_workers="block",
        )
        self.addCleanup(context.close)
        context.route("**/*", self._proxy)
        self.page = context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def _proxy(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != "app.test":
            self.errors.append(f"unexpected external request: {request.url}")
            route.abort()
            return
        self.requests.append((request.method, url.path))
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() in {"content-type", "x-csrf-token", "accept"}}
        response = self.client.request(
            request.method, url.path + (f"?{url.query}" if url.query else ""),
            content=request.post_data_buffer, headers=headers, follow_redirects=False,
        )
        route.fulfill(status=response.status_code, body=response.content, headers={
            "content-type": response.headers.get("content-type", "application/octet-stream"),
        })

    def _open(self, tab="media"):
        url = "http://app.test/rss" + (f"#{tab}" if tab != "media" else "")
        if self.page.url == url:
            self.page.reload()
        else:
            self.page.goto(url)
        self.page.wait_for_selector("#mediaSubscriptionList .media-subscription-card" if tab == "media"
                                    else "#subscriptionRunList .subscription-run-row")
        self.page.evaluate("""() => {
            window.receipts = [];
            const alert = window.appAlert;
            window.appAlert = value => { window.receipts.push(value); return alert(value); };
        }""")

    def test_search_links_open_the_matching_media_and_rss_filters(self):
        from playwright.sync_api import expect
        from app.repositories import rss as rss_repo

        feed_id = rss_repo.add_rss_subscription("回执测试订阅源", "https://example.invalid/feed")
        self.addCleanup(rss_repo.delete_rss_subscription, feed_id)
        rss_repo.add_rss_entry_with_media(feed_id, "选定来源的条目", "search-link-guid")
        media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="86035", tmdb_id="86035", media_type="movie", title="另一个订阅",
        )
        page = self.page
        search_url = "http://app.test/search?" + urlencode({"q": "回执测试"})
        cards = page.locator(".global-subscription-card")

        with patch.dict("os.environ", {"DISCOVERY_ENABLED": "0"}):
            page.goto(search_url)
            expect(cards).to_have_count(2)
            cards.filter(has_text="媒体追更").click()
            expect(page.locator("#subscriptionTabMedia")).to_have_attribute("aria-selected", "true")
            matches = page.locator("#mediaSubscriptionList .media-subscription-card")
            expect(matches).to_have_count(1)
            expect(matches).to_have_attribute("data-subscription-id", str(self.subscription_id))
            expect(page.locator("#mediaSubscriptionStatus")).to_have_value("match")
            page.locator("#mediaSubscriptionStatus").select_option("")
            expect(matches).to_have_count(2)

            page.goto(search_url)
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/rss/entries") as entries:
                cards.filter(has_text="RSS 订阅").click()
            self.assertEqual(parse_qs(urlsplit(entries.value.url).query)["subscription_id"], [str(feed_id)])
            expect(page.locator("#subscriptionTabRss")).to_have_attribute("aria-selected", "true")
            expect(page.locator("#filterSub")).to_have_value(str(feed_id))
            expect(page.locator("#entryList")).to_contain_text("选定来源的条目")

    def test_subscription_form_can_replace_legacy_sites_without_changing_legacy_interval(self):
        from playwright.sync_api import expect

        media_subscriptions_repo.update_media_subscription_config(
            self.subscription_id, sites_json='["1lou","mikan"]', check_interval_minutes=60,
        )
        with patch("app.routes.subscriptions_api._wake_scheduler"):
            self._open()
            self.page.get_by_role("button", name="编辑媒体订阅").click()
            sites = self.page.get_by_label("限定资源站")
            expect(sites).to_have_value("1lou, mikan")
            expect(self.page.get_by_label("检查间隔")).to_have_value("60")
            sites.fill("nyaa")
            self.page.get_by_role("button", name="保存订阅").click()
            expect(self.page.locator("#appMessageTitle")).to_have_text("媒体订阅已更新")
        stored = self.client.get(f"/api/subscriptions/media/{self.subscription_id}").json()
        self.assertEqual((stored["sites"], stored["check_interval_minutes"]), (["nyaa"], 60))

    def test_check_receipt_uses_nested_result_and_error_semantics(self):
        from app.modules.media_subscriptions import MediaSubscriptionError, MediaSubscriptionService
        from playwright.sync_api import expect

        self._open()
        for status, summary, missing_count, candidate_count, tone in (
            ("missing", "发现资源", 1, 1, "success"),
            ("inconclusive", "媒体服务器不可用", 0, 0, "warning"),
            ("satisfied", "媒体库完整", 0, 0, "success"),
            ("error", "索引器不可用", 1, 0, "error"),
        ):
            result = {"status": status, "summary": summary, "missing_count": missing_count,
                      "candidate_count": candidate_count}
            with self.subTest(status=status), patch.object(
                MediaSubscriptionService, "check_subscription",
                AsyncMock(return_value={"subscription": {"id": self.subscription_id}, "result": result}),
            ):
                self.page.evaluate("window.receipts = []")
                self.page.locator('[data-media-action="check"]').click()
                self.page.wait_for_function("window.receipts.length === 1")
                receipt = self.page.evaluate("window.receipts[0]")
                self.assertEqual(receipt["type"], tone)
                self.assertIn(summary, receipt["message"])
                self.assertIn(f'缺失 {missing_count} 项 · 候选 {candidate_count} 项', receipt["message"])
                expect(self.page.locator("#appMessageModal")).to_have_class(re.compile(f"is-{tone}"))
                expect(self.page.locator("#appMessageText")).to_contain_text(summary)
                self.page.locator("#appMessageClose").click()
                expect(self.page.locator('[data-media-action="check"]')).to_be_enabled()
        with patch.object(MediaSubscriptionService, "check_subscription", AsyncMock(
            side_effect=MediaSubscriptionError("检查被拒绝", code="busy", status_code=409),
        )):
            self.page.evaluate("window.receipts = []")
            self.page.locator('[data-media-action="check"]').click()
            self.page.wait_for_function("window.receipts.length === 1")
            self.assertEqual(self.page.evaluate("window.receipts[0]"), {
                "type": "warning", "title": "媒体巡检未完成", "message": "检查被拒绝",
            })
            self.page.locator("#appMessageClose").click()

    def test_download_receipts_keep_accepted_targets_and_refresh_candidates(self):
        from app.modules.media_subscriptions import MediaSubscriptionError, MediaSubscriptionService
        from playwright.sync_api import expect

        candidate_id = seed_media_subscription_candidates(
            self.subscription_id, "tmdb:86034:movie", season=None, episode=None,
            candidates=[{"result_id": "receipt-candidate", "title": "回执候选", "download_state": "ready"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        cases = (
            ({"ok": True, "status": "submitted", "request_id": 1}, "success", "已发送", ()),
            ({"ok": True, "status": "partial", "request_id": 2, "succeeded": ["qb"],
              "failed": ["guangya"], "error": "光鸭提交失败"}, "warning", "部分目标提交失败",
             ("已提交：qBittorrent", "失败：光鸭云盘")),
            ({"ok": True, "status": "partial", "request_id": 3, "succeeded": ["guangya"],
              "failed": ["qb"], "error": "qB提交失败"}, "warning", "部分目标提交失败",
             ("已提交：光鸭云盘", "失败：qBittorrent")),
            ({"ok": False, "status": "manual_review", "request_id": 4,
              "error": "下载提交结果未知，勿直接重复提交"}, "warning", "提交结果待核验", ("勿直接重复提交",)),
            ({"ok": False, "duplicate": True, "status": "duplicate", "error": "已在下载"},
             "info", "未重复提交", ("未重复提交",)),
            (MediaSubscriptionError("所有目标提交失败", status_code=502, code="download_failed"),
             "error", "发送到", ("所有目标提交失败",)),
        )
        self._open()
        for receipt, tone, title, fragments in cases:
            with self.subTest(tone=tone, title=title):
                media_subscriptions_repo.update_media_subscription_candidate(candidate_id, status="available")
                self.page.evaluate("window.receipts = []")
                self.page.locator('[data-media-action="candidates"]').click()
                button = self.page.locator(f'[data-candidate-id="{candidate_id}"]')
                expect(button).to_be_visible()
                before = len(self.requests)

                async def submit(*_args):
                    if isinstance(receipt, Exception):
                        raise receipt
                    if not receipt.get("duplicate"):
                        media_subscriptions_repo.update_media_subscription_candidate(candidate_id, status="submitted")
                    return receipt

                with patch.object(MediaSubscriptionService, "download_candidate", side_effect=submit):
                    button.click()
                    self.page.wait_for_function("window.receipts.length === 1")
                    expect(self.page.locator("#appMessageModal")).to_have_class(re.compile(f"is-{tone}"))
                    expect(self.page.locator("#appMessageTitle")).to_contain_text(title)
                    for fragment in fragments:
                        expect(self.page.locator("#appMessageText")).to_contain_text(fragment)
                    self.page.locator("#appMessageClose").click()
                    if isinstance(receipt, Exception) or receipt.get("duplicate"):
                        expect(button).to_be_enabled()
                    else:
                        expect(button).to_have_count(0)
                        expect(self.page.locator(".subscription-candidate-locked")).to_have_count(1)
                sent = self.requests[before:]
                self.assertIn(("GET", f"/api/subscriptions/media/{self.subscription_id}/candidates"), sent)
                self.assertIn(("GET", "/api/subscriptions/stats"), sent)
                self.page.locator('[data-media-candidate-close]').click()

    def test_candidate_terminal_states_and_existing_download_center_recovery(self):
        from app.repositories import download_requests as downloads_repo
        from playwright.sync_api import expect

        candidate_id = seed_media_subscription_candidates(
            self.subscription_id, "tmdb:86034:movie", season=None, episode=None,
            candidates=[{"result_id": "delivery-candidate", "title": "状态候选", "download_state": "ready"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission_id = media_subscriptions_repo.claim_media_download_admission(
            media_key="tmdb:86034:movie", tmdb_id="86034", media_type="movie",
            subscription_id=self.subscription_id, candidate_id=candidate_id,
            season=None, episode=None, subscription_revision=1,
        )
        assert admission_id is not None
        self.assertTrue(media_subscriptions_repo.begin_media_download_dispatch(
            admission_id, subscription_id=self.subscription_id, subscription_revision=1,
        ))
        request_id, _ = downloads_repo.create_download_request(
            "delivery-projection", "magnet", title="状态候选", admission_id=admission_id,
        )
        downloads_repo.add_download_log("qb", title="状态候选", request_id=request_id, status="success")
        media_subscriptions_repo.update_media_subscription_candidate(candidate_id, status="submitted", request_id=request_id)
        with db.get_conn() as conn:
            conn.execute("UPDATE media_subscriptions SET status='missing',missing_count=1 WHERE id=?", (self.subscription_id,))
        cases = (
            ("cancelled", "cancelled", "", "", "已停止跟踪", "", 1280),
            ("failed", "failed", "", "", "下载失败", "issues", 390),
            ("submitted", "outcome_unknown", "", "", "需要核验", "issues", 1280),
            ("downloading", "cancelled", "downloading", "", "下载中", "", 1280),
            ("completed", "completed", "", "failed", "处理失败", "logs", 1280),
        )
        for status, qb_status, gy_status, organize_status, label, request_view, width in cases:
            with self.subTest(status=status, label=label):
                downloads_repo.update_download_request(
                    request_id, status=status, targets="both" if gy_status else "qb",
                    qb_status=qb_status, gy_status=gy_status, organize_status=organize_status, error="",
                )
                media_subscriptions_repo.update_media_download_admission(admission_id, status="submitted")
                media_subscriptions_repo.sync_media_download_admission_for_request(request_id)
                self.page.set_viewport_size({"width": width, "height": 900})
                self._open()
                if status == "failed":
                    expect(self.page.locator(".media-subscription-summary")).to_contain_text("请在下载中心处理对应请求")
                self.page.locator('[data-media-action="candidates"]').click()
                expect(self.page.locator(".subscription-candidate-delivery")).to_have_text(label)
                expect(self.page.locator("[data-candidate-id]")).to_have_count(0)
                link = self.page.locator('.subscription-candidate-actions a')
                expect(link).to_have_count(int(bool(request_view)))
                if request_view:
                    expect(link).to_be_visible()
                    expect(link).to_have_attribute("href", f"/downloads?view={request_view}")
                    expect(link).to_contain_text(f"请求 #{request_id}")
                    if request_view == "logs":
                        logs = self.client.get("/api/downloads/logs").json()["items"]
                        record = next(item for item in logs if item["request_id"] == request_id)
                        self.assertEqual(record["postprocess_issues"][0]["key"], "organize")
                        issues = self.client.get("/api/downloads/issues").json()["items"]
                        self.assertFalse(any(item["id"] == request_id for item in issues))
                if status == "cancelled":
                    expect(self.page.locator(".subscription-candidate-reason")).to_contain_text("不会自动重启")
                    expect(self.page.locator(".subscription-candidate-reason")).to_contain_text("手动选择新资源")
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
                self.page.locator('[data-media-candidate-close]').click()

    def test_run_history_uses_shared_chinese_statuses_and_preserves_legacy(self):
        from app.modules.media_subscriptions import MediaSubscriptionService
        from playwright.sync_api import expect

        states = {"satisfied": "已满足", "missing": "发现缺失", "inconclusive": "待核验",
                  "error": "检查异常", "completed": "已完成", "failed": "失败",
                  "running": "运行中", "cancelled": "已取消"}
        # completed/error 是历史或展示合同输入，当前数据库只接受有限巡检终态。
        with patch.object(MediaSubscriptionService, "list_runs", return_value=[
            {"subscription_id": self.subscription_id, "status": status, "summary": status}
            for status in states
        ]):
            self._open("runs")
        for status, label in states.items():
            row = self.page.locator(".subscription-run-row").filter(
                has=self.page.locator(".subscription-run-body > p", has_text=re.compile(f"^{status}$")),
            )
            expect(row.locator(".subscription-status")).to_have_text(label)
            expect(row.locator('[data-lucide="circle-help"]')).to_have_count(0)


class SubscriptionAPITests(IsolatedDatabaseTestCase):
    def test_delete_after_check_claim_returns_not_found_and_preserves_cancelled_run(self):
        from concurrent.futures import ThreadPoolExecutor

        headers = self.login()
        sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="471", tmdb_id="471", media_type="movie", title="Removed",
        )
        endpoint = f"/api/subscriptions/media/{sid}"
        claimed, resume = threading.Event(), threading.Event()
        claim = media_subscriptions_repo.claim_media_subscription_check_run

        def wait_after_claim(*args):
            run_id = claim(*args)
            claimed.set()
            self.assertTrue(resume.wait(5))
            return run_id

        checker = TestClient(self.client.app, cookies=self.client.cookies, raise_server_exceptions=False)
        self.addCleanup(checker.close)
        with patch.object(media_subscriptions_repo, "claim_media_subscription_check_run", side_effect=wait_after_claim):
            with ThreadPoolExecutor(max_workers=1) as pool:
                checking = pool.submit(checker.post, endpoint + "/check", headers=headers)
                try:
                    self.assertTrue(claimed.wait(5))
                    self.assertEqual(self.client.delete(endpoint, headers=headers).status_code, 200)
                finally:
                    resume.set()
                response = checking.result(timeout=5)
        runs = self.client.get("/api/subscriptions/runs", params={"subscription_id": sid}).json()
        self.assertEqual([run["status"] for run in runs], ["cancelled"])
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("媒体订阅不存在", response.text)

    def test_pause_during_indexer_resolution_prevents_candidate_submission(self):
        from app.indexers.registry import IndexerRegistry
        from app.indexers.result_store import IndexerResultStore
        from app.indexers.service import IndexerService
        from app.modules import download_dispatcher, media_subscriptions
        from tests.test_indexer_service import FakeAdapter, item

        headers = self.login()
        subscription_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="471", tmdb_id="471", media_type="movie",
            title="Paused Signal", year="2020", sites=["nyaa"], action="confirm", download_target="qb",
        )
        endpoint = f"/api/subscriptions/media/{subscription_id}"
        result = item("nyaa", "Paused Signal 2020 1080p", magnet="magnet:?xt=urn:btih:" + "a" * 40)
        adapter = FakeAdapter("nyaa", [result])
        indexer = IndexerService(
            registry=IndexerRegistry({"nyaa": adapter}), result_store=IndexerResultStore(),
        )
        self.addCleanup(lambda: asyncio.run(indexer.aclose()))
        candidate_id, = seed_media_subscription_candidates(
            subscription_id, "tmdb:471:movie", season=None, episode=None,
            candidates=[{**result.to_public_dict(), "result_id": indexer.result_store.put(result)}],
            expires_at="2099-01-01 00:00:00",
        )
        with db.get_conn() as conn:
            request_count = conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]

        do_resolve = adapter.resolve

        async def resolve_after_pause(stored_result):
            paused = await asyncio.to_thread(
                self.client.put, endpoint, json={"enabled": False}, headers=headers,
            )
            self.assertEqual(paused.status_code, 200, paused.text)
            self.assertFalse(paused.json()["enabled"])
            return await do_resolve(stored_result)

        with (
            patch.object(adapter, "resolve", side_effect=resolve_after_pause) as resolve,
            patch.object(media_subscriptions, "get_indexer_service", return_value=indexer),
            patch.object(media_subscriptions, "_tmdb_detail", return_value={"release_date": "2020-01-01"}),
            patch.object(media_subscriptions, "inspect_media_identity_sources", return_value=[{"status": "not_found"}]),
            patch.object(download_dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "offline-qb"}) as submit,
            patch("app.modules.media_subscription_notifications.drain_media_subscription_notifications"),
            patch("app.routes.subscriptions_api._wake_scheduler"),
        ):
            download = self.client.post(
                f"{endpoint}/download", json={"candidate_id": candidate_id, "target": "qb"}, headers=headers,
            )
            self.assertEqual(len(adapter.resolve_calls), 1)
            self.assertEqual(
                (download.status_code, download.json().get("code"), submit.call_count),
                (409, "cancelled", 0), download.text,
            )
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], request_count)
                admission = conn.execute(
                    "SELECT status,request_id FROM media_download_admissions WHERE candidate_id=?", (candidate_id,),
                ).fetchone()
            self.assertEqual(tuple(admission), ("cancelled", None))

            resolve.side_effect = do_resolve
            resumed = self.client.put(endpoint, json={"enabled": True}, headers=headers)
            self.assertEqual(resumed.status_code, 200, resumed.text)
            checked = self.client.post(f"{endpoint}/check", headers=headers)
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["result"]["candidate_count"], 1)
            [candidate] = self.client.get(f"{endpoint}/candidates").json()
            self.assertNotEqual(candidate["id"], candidate_id)
            retried = self.client.post(
                f"{endpoint}/download", json={"candidate_id": candidate["id"], "target": "qb"}, headers=headers,
            )
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["succeeded"], ["qb"])
            submit.assert_called_once()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], request_count + 1)

    def test_search_outage_keeps_unexpired_candidate_for_manual_download(self):
        from datetime import datetime, timedelta, timezone

        from app.indexers.errors import IndexerUnavailable
        from app.indexers.registry import IndexerRegistry
        from app.indexers.result_store import IndexerResultStore
        from app.indexers.service import IndexerService
        from app.modules import download_dispatcher, media_subscriptions
        from tests.support import isolated_test_database
        from tests.test_indexer_service import FakeAdapter, HASH, item

        self.enterContext(isolated_test_database())
        headers = self.login()
        subscription_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="472", tmdb_id="472", media_type="movie",
            title="Harbor Signal", year="2020", sites=["nyaa", "btbtla"], action="confirm", download_target="qb",
        )
        endpoint = f"/api/subscriptions/media/{subscription_id}"
        resource = item("nyaa", "Harbor Signal 2020 1080p", magnet=f"magnet:?xt=urn:btih:{HASH}")
        adapter = FakeAdapter("nyaa", [resource])
        peer = FakeAdapter("btbtla", [
            item("btbtla", "Harbor Signal 2020 2160p", magnet="magnet:?xt=urn:btih:" + "b" * 40),
        ])
        clock = datetime.now(timezone.utc)
        indexer = IndexerService(
            registry=IndexerRegistry({"nyaa": adapter, "btbtla": peer}),
            result_store=IndexerResultStore(clock=lambda: clock),
            clock=lambda: clock, cache_ttl_seconds=5,
        )
        self.addCleanup(lambda: asyncio.run(indexer.aclose()))

        def recheck(expected_count):
            nonlocal clock
            clock += timedelta(seconds=6)
            response = self.client.post(f"{endpoint}/check", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["candidate_count"], expected_count)
            listed = self.client.get(f"{endpoint}/candidates")
            self.assertEqual(listed.status_code, 200, listed.text)
            return {row["site_id"]: row for row in listed.json()}

        def download(candidate):
            return self.client.post(
                f"{endpoint}/download", json={"candidate_id": candidate["id"], "target": "qb"}, headers=headers,
            )

        with (
            patch.object(media_subscriptions, "get_indexer_service", return_value=indexer),
            patch.object(media_subscriptions, "_tmdb_detail", return_value={"release_date": "2020-01-01"}),
            patch.object(media_subscriptions, "inspect_media_identity_sources", return_value=[{"status": "not_found"}]),
            patch("app.modules.media_subscription_notifications.drain_media_subscription_notifications"),
            patch.object(download_dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "offline-qb"}) as submit,
        ):
            selected = recheck(2)
            adapter.error = peer.error = IndexerUnavailable()
            offline = recheck(0)

            peer.error = None
            peer.items.clear()
            recovered = recheck(0)
            rejected = download(selected["btbtla"])
            self.assertEqual((rejected.status_code, rejected.json()["code"]), (409, "unavailable"))
            submit.assert_not_called()

            candidate = selected["nyaa"]
            self.assertEqual(indexer.result_store.get(candidate["result_id"]).magnet, resource.magnet)
            accepted = download(candidate)
            self.assertEqual((accepted.status_code, submit.call_count), (200, 1), accepted.text)
            self.assertEqual(accepted.json()["succeeded"], ["qb"])
            self.assertEqual(offline, selected)
            self.assertEqual(recovered, {"nyaa": candidate})
            [submitted] = self.client.get(f"{endpoint}/candidates").json()
            self.assertEqual(
                (submitted["id"], submitted["status"], submitted["request_id"]),
                (candidate["id"], "submitted", accepted.json()["request_id"]),
            )
            repeated = download(candidate)
            self.assertEqual((repeated.status_code, repeated.json()["code"], submit.call_count), (409, "unavailable", 1))

    def test_saved_site_selection_recovers_without_expanding_after_sites_become_unavailable(self):
        from app.indexers.registry import IndexerRegistry
        from app.indexers.result_store import IndexerResultStore
        from app.indexers.service import IndexerService
        from app.modules import media_subscriptions
        from tests.test_indexer_service import FakeAdapter, item

        headers = self.login()
        selected = FakeAdapter("nyaa", [
            item("nyaa", "Offline Signal 2020 1080p", magnet="magnet:?xt=urn:btih:" + "a" * 40),
            item("nyaa", "Archive Signal S01E01 1080p", magnet="magnet:?xt=urn:btih:" + "b" * 40),
        ])
        disabled, unselected = FakeAdapter("mikan"), FakeAdapter("btbtla")
        indexer = IndexerService(
            registry=IndexerRegistry({adapter.site_id: adapter for adapter in (selected, disabled, unselected)}),
            result_store=IndexerResultStore(), enabled_site_ids=("nyaa", "btbtla"),
        )
        old_sites = ["1lou", "mikan", "NYAA"]
        with (
            patch.object(media_subscriptions, "get_indexer_service", return_value=indexer),
            patch.object(media_subscriptions, "_tmdb_detail", return_value={"release_date": "2020-01-01"}),
            patch.object(media_subscriptions, "inspect_media_identity_sources", return_value=[{"status": "not_found"}]),
            patch.object(media_subscriptions, "inspect_series_episode_sources", return_value=[{"status": "not_found"}]),
            patch.object(media_subscriptions.MediaSubscriptionService, "_expected_tv", return_value=(
                [media_subscriptions._ExpectedMedia("tmdb:42:tv:S01E001", 1, 1)], 0, 0,
            )),
            patch("app.modules.media_subscription_notifications.drain_media_subscription_notifications"),
            patch("app.routes.subscriptions_api._wake_scheduler"),
        ):
            for media_type, tmdb_id, title in (("movie", "41", "Offline Signal"), ("tv", "42", "Archive Signal")):
                with self.subTest(media_type=media_type):
                    subscription_id = media_subscriptions_repo.add_media_subscription(
                        provider="tmdb", external_id=tmdb_id, tmdb_id=tmdb_id, media_type=media_type,
                        title=title, year="2020", sites=old_sites,
                    )
                    endpoint = f"/api/subscriptions/media/{subscription_id}"
                    checked = self.client.post(f"{endpoint}/check", headers=headers)
                    self.assertEqual(checked.status_code, 200, checked.text)
                    self.assertGreater(checked.json()["result"]["candidate_count"], 0, checked.text)
                    self.assertEqual(self.client.get(endpoint).json()["sites"], old_sites)
                    [candidate] = self.client.get(f"{endpoint}/candidates").json()
                    self.assertEqual(candidate["site_id"], "nyaa")

                    no_sites = self.client.put(endpoint, headers=headers, json={"sites": ["1lou", "mikan"]})
                    self.assertEqual(no_sites.status_code, 200, no_sites.text)
                    call_count = selected.calls
                    conflict = self.client.post(f"{endpoint}/check", headers=headers)
                    self.assertEqual(conflict.status_code, 409, conflict.text)
                    self.assertEqual(conflict.json()["code"], "sites_unavailable")
                    self.assertIn("编辑订阅", conflict.json()["error"])
                    self.assertEqual(selected.calls, call_count)
                    self.assertEqual(self.client.get(endpoint).json()["sites"], ["1lou", "mikan"])
                    self.assertEqual(self.client.get(
                        "/api/subscriptions/runs", params={"subscription_id": subscription_id},
                    ).json()[0]["status"], "failed")

                    recovered = self.client.put(endpoint, headers=headers, json={"sites": ["nyaa"]})
                    self.assertEqual(recovered.status_code, 200, recovered.text)
                    retry = self.client.post(f"{endpoint}/check", headers=headers)
                    self.assertEqual(retry.status_code, 200, retry.text)
                    self.assertGreater(retry.json()["result"]["candidate_count"], 0)
        self.assertGreater(selected.calls, 0)
        self.assertEqual((disabled.calls, unselected.calls), (0, 0))

    def test_watchlist_remains_manageable_when_discovery_page_is_disabled(self):
        from app.discovery.service import DiscoveryService
        from app.routes.discovery_image import encode_poster_token

        path = "/api/subscriptions/watchlist/tmdb/tv/9917"
        with patch.dict("os.environ", {"DISCOVERY_ENABLED": "0"}), patch(
            "app.routes.subscriptions_api.get_discovery_service", return_value=DiscoveryService
        ):
            self.assertEqual(self.client.delete(path).status_code, 401)
            headers = self.login()
            discovery_repo.add_media_watchlist("tmdb", "9917", "tv", "收藏作品", "2026", "poster.jpg")
            subscription_id = media_subscriptions_repo.add_media_subscription(
                provider="tmdb", external_id="9917", tmdb_id="9917", media_type="tv", title="收藏作品"
            )
            self.assertEqual(self.client.get("/api/discovery/watchlist").status_code, 404)
            self.assertEqual(self.client.delete(path).status_code, 403)
            token = encode_poster_token("tmdb", "poster.jpg")
            upstream = Mock(status_code=200, headers={"Content-Type": "image/jpeg"})
            upstream.iter_content.return_value = iter([b"\xff\xd8\xff\x00"])
            with patch("app.routes.discovery_image._get_poster_session") as session:
                session.return_value.get.return_value = upstream
                poster = self.client.get(f"/api/subscriptions/poster/tmdb/{token}")
            self.assertEqual(poster.status_code, 200)
            self.assertEqual(poster.content, b"\xff\xd8\xff\x00")
            removed = self.client.delete(path, headers=headers)
            self.assertEqual(removed.json(), {"success": True, "removed": True})
            self.assertIsNone(discovery_repo.get_media_watchlist("tmdb", "9917", "tv"))
            self.assertEqual(self.client.get(f"/api/subscriptions/media/{subscription_id}").status_code, 200)
            self.assertEqual(self.client.delete(path, headers=headers).json(), {"success": True, "removed": False})
            self.assertEqual(self.client.delete("/api/subscriptions/watchlist/evil/tv/1", headers=headers).status_code, 400)

    def test_complete_lists_use_stable_cursors_and_page_identity_subscription_lookup(self):
        from app.discovery.service import DiscoveryService

        self.login()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_watchlist")
        with patch("app.routes.subscriptions_api.get_discovery_service", return_value=DiscoveryService), patch(
            "app.routes.discovery_api.get_discovery_service", return_value=DiscoveryService
        ), patch.object(db, "now", return_value="2026-10-21 10:00:00"):
            empty = self.client.get("/api/subscriptions/media").json()
            self.assertEqual(empty, {"items": [], "total": 0, "has_more": False, "next": None})
            self.assertEqual(self.client.get("/api/subscriptions/watchlist").json(), empty)
            for number in range(1, 202):
                media_subscriptions_repo.add_media_subscription(provider="tmdb", external_id=str(number), tmdb_id=str(number), media_type="tv", title=f"Test {number}")
                if number == 200:
                    boundary = self.client.get("/api/subscriptions/media").json()
                    self.assertEqual((len(boundary["items"]), boundary["has_more"], boundary["next"]), (200, False, None))
            for number in [*range(2, 502), 1]:
                discovery_repo.add_media_watchlist("tmdb", str(number), "tv", f"Test {number}", "2026", "")
            media = self.client.get("/api/subscriptions/media?limit=200").json()
            self.assertEqual((len(media["items"]), media["total"], media["has_more"]), (200, 201, True))
            # 页间新增以及删除已读项不能让 offset 漂移而漏掉旧订阅。
            media_subscriptions_repo.add_media_subscription(provider="tmdb", external_id="202", tmdb_id="202", media_type="tv", title="New")
            with db.get_conn() as conn:
                conn.execute("UPDATE media_subscriptions SET deleted_at=? WHERE id=?", (db.now(), media["items"][0]["id"]))
            last = self.client.get("/api/subscriptions/media", params=media["next"]).json()
            self.assertEqual([item["tmdb_id"] for item in last["items"]], ["1"])
            self.assertFalse(last["has_more"])
            self.assertEqual(self.client.get("/api/subscriptions/media", params={"before_id": 1}).status_code, 400)

            watch = self.client.get("/api/subscriptions/watchlist?limit=200").json()
            self.assertEqual((len(watch["items"]), watch["total"], watch["has_more"]), (200, 501, True))
            oldest = next(item for item in watch["items"] if item["external_id"] == "1")
            self.assertEqual(oldest["subscription"]["tmdb_id"], "1")
            seen = [item["external_id"] for item in watch["items"]]
            discovery_repo.add_media_watchlist("tmdb", "9999", "tv", "New", "2026", "")
            discovery_repo.delete_media_watchlist("tmdb", watch["items"][1]["external_id"], "tv")
            while watch["has_more"]:
                watch = self.client.get("/api/subscriptions/watchlist", params=watch["next"]).json()
                seen.extend(item["external_id"] for item in watch["items"])
            self.assertEqual(len(seen), 501)
            self.assertEqual(len(set(seen)), 501)
            self.assertNotIn("9999", seen)
            with patch.dict("os.environ", {"DISCOVERY_ENABLED": "1"}):
                discovery = self.client.get("/api/discovery/watchlist?limit=1").json()
            self.assertEqual(len(discovery["items"]), 1)
            self.assertEqual(discovery["total"], 501)
            self.assertTrue(discovery["has_more"])
            with db.get_conn() as conn:
                conn.execute("UPDATE media_subscriptions SET status='paused',enabled=0 WHERE tmdb_id IN ('197','199')")
            filtered = self.client.get("/api/subscriptions/media", params={"status": "paused", "enabled": "0", "limit": 1}).json()
            self.assertEqual((filtered["total"], filtered["items"][0]["tmdb_id"]), (2, "199"))
            filtered_last = self.client.get("/api/subscriptions/media", params=filtered["next"]).json()
            self.assertEqual([item["tmdb_id"] for item in filtered_last["items"]], ["197"])
            self.assertFalse(filtered_last["has_more"])

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
        self.client = TestClient(create_app(start_background=False))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> dict[str, str]:
        token = self._csrf(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return {"X-CSRF-Token": self._csrf(self.client.get("/rss").text)}

    def test_api_requires_login_and_csrf_for_mutations(self) -> None:
        self.assertEqual(self.client.get("/api/subscriptions/media").status_code, 401)
        self.assertEqual(self.client.get("/api/subscriptions/stats").status_code, 401)
        self.assertEqual(self.client.get("/api/subscriptions/runs").status_code, 401)
        self.assertEqual(self.client.delete("/api/subscriptions/runs").status_code, 401)
        self.assertEqual(
            self.client.post("/api/subscriptions/media", json={"tmdb_id": "1"}).status_code,
            401,
        )
        self.login()
        self.assertEqual(
            self.client.post("/api/subscriptions/media", json={"tmdb_id": "1"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete("/api/subscriptions/media/1").status_code,
            403,
        )
        self.assertEqual(self.client.delete("/api/subscriptions/runs").status_code, 403)
        self.assertEqual(self.client.delete(
            "/api/subscriptions/runs", headers={"X-CSRF-Token": "invalid"},
        ).status_code, 403)

    def test_clear_runs_deletes_all_finished_history_and_is_idempotent(self) -> None:
        headers = self.login()
        subscription_ids = [media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id=str(number), tmdb_id=str(number),
            media_type="movie", title=f"清除测试 {number}",
        ) for number in (1, 2, 3)]
        statuses = ("satisfied", "missing", "inconclusive", "failed", "cancelled")
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_subscription_runs(subscription_id,status,started_at,finished_at) "
                "VALUES(?,?,?,?)",
                [(subscription_ids[number % 3], statuses[number % len(statuses)], db.now(), None)
                 for number in range(305)],
            )
        self.assertTrue(media_subscriptions_repo.delete_media_subscription(subscription_ids[2]))
        running_ids = [media_subscriptions_repo.claim_media_subscription_check_run(sid)
                       for sid in subscription_ids[:2]]
        self.assertNotIn(None, running_ids)
        with db.get_conn() as conn:
            subscriptions = conn.execute("SELECT * FROM media_subscriptions ORDER BY id").fetchall()
            running = conn.execute(
                "SELECT * FROM media_subscription_runs WHERE status='running' ORDER BY id"
            ).fetchall()
        listed = self.client.get("/api/subscriptions/runs")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 100)
        self.assertEqual(listed.json()[0]["id"], running_ids[-1])
        self.assertEqual(listed.json()[0]["payload"], {})
        self.assertEqual(len(self.client.get(
            "/api/subscriptions/runs", params={"subscription_id": subscription_ids[0], "limit": 300},
        ).json()), 103)

        response = self.client.delete("/api/subscriptions/runs", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"success": True, "deleted": 305, "retained": 2})
        self.assertEqual(self.client.delete("/api/subscriptions/runs", headers=headers).json(),
                         {"success": True, "deleted": 0, "retained": 2})
        self.assertEqual({item["id"] for item in self.client.get("/api/subscriptions/runs").json()},
                         set(running_ids))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT * FROM media_subscriptions ORDER BY id").fetchall(),
                             subscriptions)
            self.assertEqual(conn.execute("SELECT * FROM media_subscription_runs ORDER BY id").fetchall(),
                             running)

    def test_clear_runs_preserves_live_check_downloads_and_notifications(self) -> None:
        from app.modules.media_subscription_notifications import drain_media_subscription_notifications
        from app.notifier import TelegramSendResult
        from app.repositories import download_requests as downloads_repo
        from app.repositories import media_experience as experience_repo

        headers = self.login()
        sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="movie", title="清除通知测试",
        )
        rule = experience_repo.get_notification_rule(sid)
        assert rule is not None
        self.assertIsNotNone(experience_repo.set_notification_rule(
            sid, expected_rule_revision=rule["revision"],
            expected_subscription_revision=rule["subscription_revision"], updates={"enabled": True},
        ))
        revision = rule["subscription_revision"]

        def finalize(run_id: int) -> None:
            self.assertTrue(media_subscriptions_repo.finalize_media_subscription_check(
                sid, run_id, status="missing", run_status="missing", summary="发现缺失",
                payload={"status": "missing", "candidate_count": 1}, interval_minutes=60,
                expected_count=1, local_count=0, missing_count=1, missing_json="[]", result_json="{}",
                subscription_revision=revision,
            ))

        previous_run = media_subscriptions_repo.claim_media_subscription_check_run(sid)
        assert previous_run is not None
        candidate_id = seed_media_subscription_candidates(
            sid, "tmdb:1:movie", season=None, episode=None,
            candidates=[{"result_id": "clear-runs", "title": "候选"}], expires_at="2099-01-01 00:00:00",
        )[0]
        admission_id = media_subscriptions_repo.claim_media_download_admission(
            media_key="tmdb:1:movie", tmdb_id="1", media_type="movie", subscription_id=sid,
            candidate_id=candidate_id, season=None, episode=None, subscription_revision=revision,
            require_active_check=True, check_run_id=previous_run,
        )
        assert admission_id is not None and admission_id > 0
        self.assertTrue(media_subscriptions_repo.begin_media_download_dispatch(
            admission_id, subscription_id=sid, subscription_revision=revision,
            require_active_check=True, check_run_id=previous_run,
        ))
        request_id, created = downloads_repo.create_download_request(
            "clear-runs-download", "magnet", title="候选", admission_id=admission_id,
        )
        self.assertTrue(created)
        media_subscriptions_repo.update_media_subscription_candidate(
            candidate_id, status="submitted", request_id=request_id,
        )
        downloads_repo.add_download_log("qb", title="候选", request_id=request_id)
        finalize(previous_run)
        live_run = media_subscriptions_repo.claim_media_subscription_check_run(sid)
        assert live_run is not None
        tables = ("media_subscriptions", "media_subscription_candidates", "media_download_admissions",
                  "download_requests", "download_log", "media_subscription_notification_rules")
        with db.get_conn() as conn:
            before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                      for table in tables}
        notification = dict(experience_repo.list_notification_outbox()[0])

        response = self.client.delete("/api/subscriptions/runs", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"success": True, "deleted": 1, "retained": 1})
        with db.get_conn() as conn:
            for table in tables:
                self.assertEqual(conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(),
                                 before[table], table)
        self.assertEqual(dict(experience_repo.list_notification_outbox()[0]),
                         {**notification, "run_id": None})
        self.assertTrue(media_subscriptions_repo.media_subscription_check_is_active(sid, revision, run_id=live_run))
        self.assertIsNone(media_subscriptions_repo.claim_media_subscription_check_run(sid))
        finalize(live_run)
        self.assertEqual(self.client.get(f"/api/subscriptions/media/{sid}").json()["status"], "missing")
        self.assertEqual(self.client.get("/api/subscriptions/runs").json()[0]["status"], "missing")
        self.assertEqual(self.client.delete("/api/subscriptions/runs", headers=headers).json(),
                         {"success": True, "deleted": 1, "retained": 0})
        self.assertEqual(self.client.delete("/api/subscriptions/runs", headers=headers).json(),
                         {"success": True, "deleted": 0, "retained": 0})
        self.assertEqual(self.client.get("/api/subscriptions/runs").json(), [])
        notifications = experience_repo.list_notification_outbox()
        self.assertEqual({row["event_key"] for row in notifications},
                         {f"media-subscription-run:{run}:missing" for run in (previous_run, live_run)})
        self.assertTrue(all(row["run_id"] is None and row["status"] == "pending" for row in notifications))
        # 本用例验证即时投递，独立于同进程其它用例留下的后台关停事件。
        with patch("app.modules.telegram_notification_center._dispatch_stop", threading.Event()), patch(
            "app.modules.telegram_notification_center.notification_target_chat_id", return_value="100",
        ), patch(
            "app.modules.telegram_notification_center.send_event_result",
            return_value=TelegramSendResult(ok=True, message_id=51),
        ) as send:
            self.assertTrue(drain_media_subscription_notifications())
            self.assertTrue(drain_media_subscription_notifications())
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all(row["status"] == "sent" for row in experience_repo.list_notification_outbox()))

    def test_clear_runs_rolls_back_on_delete_failure(self) -> None:
        headers = self.login()
        sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="movie", title="清除回滚测试",
        )
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_subscription_runs(subscription_id,status,started_at) VALUES(?,?,?)",
                [(sid, status, db.now()) for status in ("satisfied", "failed")],
            )
            before = conn.execute("SELECT * FROM media_subscription_runs ORDER BY id").fetchall()
            conn.execute(
                "CREATE TRIGGER reject_history_clear BEFORE DELETE ON media_subscription_runs "
                "WHEN OLD.status='failed' BEGIN SELECT RAISE(FAIL, 'clear failed'); END"
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "clear failed"):
                self.client.delete("/api/subscriptions/runs", headers=headers)
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT * FROM media_subscription_runs ORDER BY id").fetchall(),
                                 before)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER reject_history_clear")

    def test_media_form_defaults_match_creation_and_leave_saved_values_unchanged(self) -> None:
        headers = self.login()
        saved_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="40", tmdb_id="40", media_type="tv",
            title="历史订阅", action="notify", download_target="qb",
            monitor_mode="selected", seasons=[2], check_interval_minutes=60,
            enabled=False, include_specials=True,
        )
        saved_row = media_subscriptions_repo.get_media_subscription(saved_id)
        assert saved_row is not None
        saved = dict(saved_row)
        response = self.client.get("/api/subscriptions/stats")
        self.assertEqual(response.status_code, 200)
        defaults = response.json()["form_defaults"]
        self.assertEqual(defaults, {
            "provider": "tmdb", "media_type": "tv", "enabled": True,
            "monitor_mode": "missing", "seasons": [], "include_specials": False,
            "action": "confirm", "download_target": "guangya", "sites": [],
            "check_interval_minutes": 4320,
        })
        self.assertEqual(response.json()["media_total"], 1)
        with patch("app.modules.media_subscriptions._tmdb_detail", return_value={
            "name": "新订阅", "first_air_date": "2026-01-01",
        }), patch("app.routes.subscriptions_api._wake_scheduler"):
            for identity, options in (("41", {}), ("42", defaults)):
                with self.subTest(options=options):
                    created = self.client.post(
                        "/api/subscriptions/media", headers=headers,
                        json={**options, "tmdb_id": identity},
                    )
                    self.assertEqual(created.status_code, 201, created.text)
                    subscription = created.json()["subscription"]
                    self.assertEqual({key: subscription[key] for key in defaults}, defaults)
        saved_row = media_subscriptions_repo.get_media_subscription(saved_id)
        assert saved_row is not None
        self.assertEqual(dict(saved_row), saved)

    def test_thin_routes_forward_crud_check_and_candidate_scope(self) -> None:
        headers = self.login()
        service = Mock()
        service.create_subscription = AsyncMock(return_value={
            "created": True,
            "subscription": {"id": 7, "tmdb_id": "86034", "media_type": "tv"},
        })
        service.update_subscription.return_value = {"id": 7, "enabled": False}
        service.check_subscription = AsyncMock(return_value={"result": {"status": "satisfied"}})
        service.delete_subscription.return_value = True
        with patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            created = self.client.post(
                "/api/subscriptions/media",
                headers=headers,
                json={"tmdb_id": "86034", "media_type": "tv"},
            )
            updated = self.client.put(
                "/api/subscriptions/media/7", headers=headers, json={"enabled": False}
            )
            checked = self.client.post("/api/subscriptions/media/7/check", headers=headers)
            removed = self.client.delete("/api/subscriptions/media/7", headers=headers)

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json(), {"result": {"status": "satisfied"}})
        self.assertEqual(removed.status_code, 200, removed.text)
        service.create_subscription.assert_awaited_once_with({"tmdb_id": "86034", "media_type": "tv"})
        service.update_subscription.assert_called_once_with(7, {"enabled": False})
        service.check_subscription.assert_awaited_once_with(7, trigger="manual")
        service.delete_subscription.assert_called_once_with(7)

        subscription_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034", media_type="tv",
            title="平凡职业造就世界最强",
        )
        other_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="127532", tmdb_id="127532", media_type="tv",
            title="我独自升级",
        )
        candidate_id = seed_media_subscription_candidates(
            other_id,
            "tmdb:127532:tv:S01E001",
            season=1,
            episode=1,
            candidates=[{"result_id": "other", "title": "other"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        scoped_service = Mock()
        scoped_service.download_candidate = AsyncMock()
        with patch(
            "app.routes.subscriptions_api.get_media_subscription_service",
            return_value=scoped_service,
        ):
            response = self.client.post(
                f"/api/subscriptions/media/{subscription_id}/download",
                headers=headers,
                json={"candidate_id": candidate_id, "target": "guangya"},
            )
        self.assertEqual(response.status_code, 404, response.text)
        scoped_service.download_candidate.assert_not_awaited()

    def test_watchlist_uses_one_batched_external_mapping_lookup(self) -> None:
        headers = self.login()
        rows = [{
            "provider": "bangumi",
            "external_id": str(index),
            "media_type": "tv",
            "title": f"动画 {index}",
            "year": "2026",
            "poster_key": "",
            "created_at": "2026-08-30 12:00:00",
        } for index in range(500)]
        discovery = Mock()
        discovery.list_watchlist.return_value = {"items": rows, "total": len(rows), "has_more": False, "next": None}
        service = Mock()
        service.subscriptions_by_identity.return_value = {}
        with patch(
            "app.routes.subscriptions_api.get_discovery_service",
            return_value=discovery,
        ), patch(
            "app.routes.subscriptions_api.get_media_subscription_service",
            return_value=service,
        ), patch(
            "app.repositories.discovery.list_media_external_ids",
            return_value={},
        ) as batch_lookup:
            response = self.client.get("/api/subscriptions/watchlist", headers=headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 500)
        batch_lookup.assert_called_once()
        self.assertEqual(len(batch_lookup.call_args.args[0]), 500)

    def test_watchlist_subscription_requires_confirmed_tmdb_mapping(self) -> None:
        headers = self.login()
        watchlist = [{
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "title": "测试动画",
            "year": "2026",
            "poster_key": "",
            "created_at": "2026-08-09 12:00:00",
        }]
        discovery = Mock()
        discovery.map_to_tmdb_async = AsyncMock(return_value={
            "confirmed": False,
            "candidates": [{"tmdb_id": "999", "title": "测试动画"}],
        })
        service = Mock()
        with patch("app.routes.subscriptions_api.get_discovery_service", return_value=discovery), patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch(
            "app.repositories.discovery.get_media_watchlist",
            return_value=watchlist[0],
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            pending = self.client.post(
                "/api/subscriptions/media/from-watchlist",
                headers=headers,
                json={
                    "provider": "bangumi",
                    "external_id": "12345",
                    "media_type": "tv",
                    "monitor_mode": "selected",
                    "seasons": [2],
                },
            )
        self.assertEqual(pending.status_code, 409, pending.text)
        self.assertEqual(pending.json()["code"], "mapping_required")
        self.assertFalse(service.create_subscription.called)

        discovery.map_to_tmdb_async.return_value = {"confirmed": True, "tmdb_id": "999"}
        service.create_subscription = AsyncMock(return_value={
            "created": True,
            "subscription": {"id": 9, "tmdb_id": "999", "media_type": "tv"},
        })
        with patch("app.routes.subscriptions_api.get_discovery_service", return_value=discovery), patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch(
            "app.repositories.discovery.get_media_watchlist",
            return_value=watchlist[0],
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            created = self.client.post(
                "/api/subscriptions/media/from-watchlist",
                headers=headers,
                json={
                    "provider": "bangumi",
                    "external_id": "12345",
                    "media_type": "tv",
                    "tmdb_id": "999",
                    "monitor_mode": "selected",
                    "seasons": [2],
                    "action": "auto",
                    "download_target": "both",
                },
            )
        self.assertEqual(created.status_code, 201, created.text)
        service.create_subscription.assert_awaited_once_with({
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "tmdb_id": "999",
            "monitor_mode": "selected",
            "seasons": [2],
            "action": "auto",
            "download_target": "both",
        }, identity_confirmed=True)

    def test_subscription_center_page_exposes_unified_navigation_and_stable_hooks(self) -> None:
        self.login()
        response = self.client.get("/rss")
        self.assertEqual(response.status_code, 200, response.text)
        html = response.text
        self.assertIn("订阅 - MediaFlux", html)
        self.assertIn('data-subscription-center', html)
        self.assertIn('data-subscription-tab="media"', html)
        self.assertIn('data-subscription-tab="rss"', html)
        self.assertIn('data-subscription-tab="watchlist"', html)
        self.assertIn('data-subscription-tab="runs"', html)
        self.assertIn('id="mediaSubscriptionList"', html)
        self.assertIn('id="subscriptionWatchlist"', html)
        self.assertIn('id="subscriptionRunList"', html)
        self.assertIn('/static/js/subscriptions.js', html)
        self.assertIn('<option value="4320" selected>每 3 天</option>', html)
        self.assertIn('<option value="10080">每 7 天</option>', html)
        self.assertNotIn('<option value="60" selected>每 1 小时</option>', html)
        self.assertIn('subscriptionInitialTab', html)
        self.assertIn('MEDIA FOLLOW</span><span class="subscription-heading-divider"', html)
        self.assertIn('RSS SOURCES</span><span class="subscription-heading-divider"', html)
        self.assertIn('WATCHLIST</span><span class="subscription-heading-divider"', html)
        self.assertIn('AUDIT LOG</span><span class="subscription-heading-divider"', html)
        self.assertIn('class="btn btn-primary btn-sm subscription-rss-create"', html)
        self.assertIn('class="subscription-metric-value" id="subscriptionStatTotal"', html)
        self.assertRegex(html, r'href="/rss"[^>]*>.*?<span>订阅</span>')

        root = Path(__file__).resolve().parents[1]
        styles = (root / 'app/static/css/main.css').read_text(encoding='utf-8')
        scripts = (root / 'app/static/js/subscriptions.js').read_text(encoding='utf-8')
        self.assertIn('html[data-subscription-initial-tab="rss"] #subscriptionPanelRss', styles)
        self.assertIn('html[data-subscription-initial-tab="watchlist"] #subscriptionPanelWatchlist', styles)
        self.assertIn('html[data-subscription-initial-tab="runs"] #subscriptionPanelRuns', styles)
        self.assertRegex(
            styles,
            r'\.subscription-panel-actions \.subscription-create\s*\{[^}]*width:\s*auto',
        )
        self.assertRegex(
            styles,
            r'\.subscription-section-toolbar \.subscription-rss-create\s*\{[^}]*width:\s*auto',
        )
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr))', styles)
        self.assertIn('grid-template-columns: 76px minmax(0, 1fr)', styles)
        self.assertIn('.subscription-watch-icon { width: 32px; height: 32px;', styles)
        self.assertNotIn('repeat(auto-fill, minmax(360px, 440px))', styles)
        self.assertIn('grid-template-columns: 40px minmax(0, 1fr) auto', styles)
        self.assertIn('.subscription-media-list { align-content: start; grid-auto-rows: max-content; }', styles)
        self.assertIn('.media-subscription-card {', styles)
        self.assertIn('min-height: 112px;', styles)
        self.assertIn('.subscription-skeleton.is-media {', styles)
        self.assertIn('.subscription-skeleton.is-media > span:nth-child(3) { grid-column: 2;', styles)
        self.assertIn('padding: 8px 8px 4px 0;', styles)
        self.assertIn('.media-subscription-details {', styles)
        self.assertIn('.media-subscription-candidate-count {', styles)
        self.assertNotIn('media-subscription-facts', scripts)
        self.assertIn("void window.appAlert?.({", scripts)
        self.assertIn("title: duplicate ? '未重复提交' : `发送到${targetLabel}失败`", scripts)
        self.assertIn('const contextActive = state.candidateSubscriptionId === subscriptionId', scripts)
        self.assertIn("const target = subscription.download_target || 'guangya'", scripts)
        self.assertIn("target === 'both' ? '同时发送到 qB + 光鸭'", scripts)
        self.assertNotIn("[['guangya', '光鸭'], ['qb', 'qB']]", scripts)
        self.assertIn('candidateDeliveryView(item)', scripts)
        self.assertIn('mediaWorkflowSummary(item, result)', scripts)
        self.assertIn('item.candidate_count ?? workflow.candidate_count', scripts)
        self.assertIn('已推送至 ${target}，等待下载', scripts)
        self.assertIn('尚未满足自动下载条件', scripts)
        self.assertIn("'icon-action subscription-watch-icon'", scripts)
        self.assertIn("return mediaDetailURL(item, false, '/rss#media')", scripts)
        self.assertIn('title.href = mediaSubscriptionLink(item)', scripts)
        self.assertIn("return mediaDetailURL(item, resourceFocus, '/rss#watchlist')", scripts)
        self.assertIn("params.set('return_to', returnTo)", scripts)
        self.assertIn('delete document.documentElement.dataset.subscriptionInitialTab', scripts)
        self.assertIn("setMediaInterval(defaults.check_interval_minutes);", scripts)
        self.assertNotIn("setMediaInterval(4320)", scripts)
        self.assertIn("当前旧设置：${formatLegacyMediaInterval(normalized)}", scripts)
        self.assertIn("fields.interval.dataset.legacyInterval === fields.interval.value", scripts)
        self.assertIn("delete payload.check_interval_minutes;", scripts)
        self.assertIn("document.addEventListener('mediaflux:rss-stats-updated'", scripts)
        self.assertNotIn('window.updateStats =', scripts)
        self.assertIn(
            "document.dispatchEvent(new CustomEvent('mediaflux:rss-stats-updated'))",
            html,
        )


class SubscriptionMotionContractTests(unittest.TestCase):
    def test_subscription_cards_stay_static_and_numbers_count_up(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / "app/templates/rss.html").read_text(encoding="utf-8")
        script = (root / "app/static/js/subscriptions.js").read_text(encoding="utf-8")
        styles = (root / "app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn("window.MFAnim.countUp", script)
        self.assertNotIn("window.MFAnim.staggerIn", script)
        self.assertNotIn("window.MFAnim.crossfade", script)
        self.assertIn("function lockElementHeight(element)", script)
        self.assertIn("requestAnimationFrame(() => window.requestAnimationFrame", script)
        self.assertIn("preserve && hadContent ? lockElementHeight", script)
        self.assertIn("delete document.documentElement.dataset.subscriptionInitialTab", script)
        self.assertIn("static_url('js/subscriptions.js')", template)

        self.assertIn(".subscription-tab,", styles)
        self.assertIn(".media-subscription-card,", styles)
        self.assertIn(".rss-entry-card { transition: none; }", styles)


class SubscriptionMappingAsyncContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _watchlist():
        return [{
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "title": "测试动画",
            "year": "2026",
        }]

    @staticmethod
    def _payload():
        return {
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
        }

    async def test_watchlist_mapping_does_not_block_event_loop(self):
        from app.routes import subscriptions_api

        entered = threading.Event()
        release = threading.Event()
        discovery = Mock()

        def slow_map(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=0.5)
            return {"confirmed": False, "tmdb_id": "", "candidates": []}

        async def slow_map_async(*args, **kwargs):
            return await asyncio.to_thread(slow_map, *args, **kwargs)

        discovery.map_to_tmdb_async = AsyncMock(side_effect=slow_map_async)
        with (
            patch.object(subscriptions_api, "require_api_login"),
            patch.object(
                subscriptions_api, "get_discovery_service", return_value=discovery
            ),
            patch("app.repositories.discovery.get_media_watchlist",
                return_value=self._watchlist()[0],
            ),
        ):
            task = asyncio.create_task(
                subscriptions_api.create_from_watchlist(
                    cast(Request, SimpleNamespace()), self._payload()
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "同步 TMDB 映射阻塞了事件循环")
            release.set()
            response = await task

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(bytes(response.body))["code"], "mapping_required")
        discovery.map_to_tmdb_async.assert_awaited_once_with(
            "bangumi", "12345", "tv", "测试动画", "2026",
            confirmed_tmdb_id="",
        )

    async def test_cancelling_mapping_does_not_create_subscription(self):
        from app.routes import subscriptions_api

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        discovery = Mock()

        def slow_map(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1)
            finished.set()
            return {"confirmed": True, "tmdb_id": "999"}

        async def slow_map_async(*args, **kwargs):
            return await asyncio.to_thread(slow_map, *args, **kwargs)

        discovery.map_to_tmdb_async = AsyncMock(side_effect=slow_map_async)
        service = Mock()
        service.create_subscription = AsyncMock()
        with (
            patch.object(subscriptions_api, "require_api_login"),
            patch.object(
                subscriptions_api, "get_discovery_service", return_value=discovery
            ),
            patch("app.repositories.discovery.get_media_watchlist",
                return_value=self._watchlist()[0],
            ),
            patch.object(
                subscriptions_api,
                "get_media_subscription_service",
                return_value=service,
            ),
            patch.object(subscriptions_api, "_wake_scheduler") as wake_scheduler,
        ):
            task = asyncio.create_task(
                subscriptions_api.create_from_watchlist(
                    cast(Request, SimpleNamespace()), self._payload()
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            service.create_subscription.assert_not_awaited()
            wake_scheduler.assert_not_called()
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
