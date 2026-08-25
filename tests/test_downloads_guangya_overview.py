from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.clients.guangya import GuangYaClient
from app.config import web_credentials
from app.main import create_app
from app.routes import downloads_api


class GuangYaDownloadOverviewTests(InitializedWebTestCase):
    def setUp(self):
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf_token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _authenticated_headers(self) -> dict[str, str]:
        login_page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf_token(login_page),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        settings = self.client.get("/settings")
        return {"X-CSRF-Token": self._csrf_token(settings)}

    def test_completed_task_without_progress_reports_full_size(self):
        task = GuangYaClient._to_offline_task({
            "taskId": "task-1",
            "fileName": "Demo.Show.S01",
            "status": 1,
            "totalSize": 4096,
        })

        self.assertEqual(task["progress"], 1.0)
        self.assertEqual(task["downloaded"], 4096)
        self.assertEqual(task["status_label"], "已完成")
        self.assertEqual(task["status_kind"], "done")

    def test_status_two_is_reported_as_completed(self):
        task = GuangYaClient._to_offline_task({
            "taskId": "task-2",
            "fileName": "Completed.By.Cloud",
            "status": 2,
            "progress": 100,
            "totalSize": 8192,
        })

        self.assertEqual(task["status_label"], "已完成")
        self.assertEqual(task["status_kind"], "done")
        self.assertEqual(task["progress"], 1.0)
        self.assertEqual(task["downloaded"], 8192)

    def test_offline_task_list_requests_all_statuses_and_paginates(self):
        first = [{"taskId": f"task-{index}", "status": 0} for index in range(50)]
        second = [{"taskId": "task-50", "status": 2}]
        raw = Mock()
        raw.cloud_task_list.side_effect = [
            {"data": {"list": first}},
            {"data": {"list": second}},
        ]
        client = object.__new__(GuangYaClient)
        client._call_read = Mock(side_effect=lambda _name, callback: callback())

        with patch.object(GuangYaClient, "raw", new_callable=PropertyMock, return_value=raw):
            tasks = client.list_offline_tasks()

        self.assertEqual(len(tasks), 51)
        self.assertEqual(tasks[-1]["status_kind"], "done")
        self.assertEqual(raw.cloud_task_list.call_count, 2)
        self.assertEqual(
            raw.cloud_task_list.call_args_list[0].kwargs,
            {"page": 0, "page_size": 50, "status": [0, 1, 2, 3, 4]},
        )
        self.assertEqual(raw.cloud_task_list.call_args_list[1].kwargs["page"], 1)

    def test_offline_task_list_stops_on_repeated_full_page(self):
        page = [{"taskId": f"task-{index}", "status": 0} for index in range(50)]
        raw = Mock()
        raw.cloud_task_list.side_effect = [page, page]
        client = object.__new__(GuangYaClient)
        client._call_read = Mock(side_effect=lambda _name, callback: callback())

        with patch.object(GuangYaClient, "raw", new_callable=PropertyMock, return_value=raw):
            tasks = client.list_offline_tasks()

        self.assertEqual(len(tasks), 50)
        self.assertEqual(raw.cloud_task_list.call_count, 2)

    def test_overview_does_not_query_or_return_guangya_live_tasks(self):
        headers = self._authenticated_headers()
        with patch(
            "app.routes.downloads_api.config.get",
            side_effect=lambda key, default="": "" if key == "QB_URL" else default,
        ), patch.object(downloads_api, "GuangYaClient", create=True) as guangya_client:
            response = self.client.get("/api/downloads/overview", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("guangya", response.json())
        guangya_client.assert_not_called()

    def test_download_page_only_keeps_guangya_in_logs(self):
        template = (Path("app/templates/downloads.html").read_text(encoding="utf-8") + Path("app/static/js/downloads.js").read_text(encoding="utf-8"))
        stylesheet = Path("app/static/css/main.css").read_text(encoding="utf-8")

        self.assertNotIn("光鸭离线", template)
        self.assertNotIn('id="gyList"', template)
        self.assertNotIn("function renderGy", template)
        self.assertNotIn("d.guangya", template)
        self.assertIn('<option value="guangya">光鸭</option>', template)
        self.assertEqual(template.count('<section class="card card-pad download-panel">'), 1)
        self.assertIn(".download-layout { display: grid; grid-template-columns: 1fr;", stylesheet)
        self.assertIn(".download-summary { display: grid; grid-template-columns: repeat(3", stylesheet)

    def test_settings_page_has_no_redundant_media_server_action(self):
        template = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))

        self.assertNotIn("媒体服务器配置", template)


if __name__ == "__main__":
    unittest.main()
