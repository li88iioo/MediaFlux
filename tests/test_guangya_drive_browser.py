"""通过真实光鸭页面和共享目录选择器验证云盘浏览与 Writer 回执。"""

from __future__ import annotations

import mimetypes
import unittest
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

from .test_agent_kernel_browser import _chromium_executable, sync_playwright

if sync_playwright is not None:
    from playwright.sync_api import expect


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/static"
ORIGIN = "http://offline.invalid"
API = "/api/guangya"
SEARCH = API + "/search"
FILE_CHANGE = API + "/file-change"
WRITER_STATUS = API + "/organize/status"
TASK_ID = "fixture-writer-task"
KEYWORD = "星海 + 特辑"


class DriveItem(TypedDict):
    file_id: str
    name: str
    parent_id: str
    is_dir: bool


def drive_item(file_id: str, name: str, parent_id: str, *, directory: bool = False) -> DriveItem:
    return {"file_id": file_id, "name": name, "parent_id": parent_id, "is_dir": directory}


MATCHED_FILE = drive_item("match-file", KEYWORD + ".mkv", "video-source")
MATCHED_FOLDER = drive_item("match-folder", KEYWORD + "花絮", "extras-source", directory=True)
MATCHED_TRAILER = drive_item("trailer", KEYWORD + "预告.mkv", "trailers")


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class GuangyaDriveBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
        )
        cls.addClassCleanup(cls.browser.close)
        env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
        cls.html = env.get_template("guangya.html").render(
            active="guangya", app_version="offline-test", discovery_enabled=False, agent_enabled=False,
            csrf_token=lambda: "offline-csrf",
            static_url=lambda name: "/static/" + name,
            url_for=lambda name: "/" + name.split(".")[-1],
        )

    def setUp(self):
        self.directories = {
            "0": [drive_item("inbox", "浏览目录", "0", directory=True),
                  drive_item("archive", "归档盘", "0", directory=True)],
            "inbox": [drive_item("alpha", "A.txt", "inbox"), drive_item("omega", "Z.txt", "inbox")],
            "archive": [drive_item("destination", "归档目标", "archive", directory=True)],
            "destination": [],
        }
        self.search_pages = {"1": [MATCHED_FILE, MATCHED_FOLDER], "2": [MATCHED_TRAILER]}
        self.search_queries = []
        self.writes = []
        self.writer_finished = False
        self.unexpected, self.errors = [], []

    def route_request(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = unquote(url.path)
        query = parse_qs(url.query, keep_blank_values=True)
        if url.netloc == "offline.invalid":
            if request.method == "GET":
                if path == "/guangya":
                    route.fulfill(content_type="text/html", body=self.html)
                    return
                if path.startswith("/static/"):
                    asset = (STATIC / path.removeprefix("/static/")).resolve()
                    if asset.is_relative_to(STATIC) and asset.is_file():
                        route.fulfill(
                            body=asset.read_bytes(),
                            content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream",
                        )
                        return
                if path == API + "/capabilities":
                    route.fulfill(json={"sdk_available": True})
                    return
                if path == API + "/dirs" and query.get("parent_id", [""])[0] in self.directories:
                    route.fulfill(json=self.directories[query["parent_id"][0]])
                    return
                if path == SEARCH:
                    page = query.setdefault("page", ["1"])[0]
                    self.search_queries.append(query)
                    if page in self.search_pages:
                        route.fulfill(json={"items": self.search_pages[page], "has_more": page == "1"})
                        return
                if path == WRITER_STATUS and query == {"task_ids": [TASK_ID]} and self.writes:
                    route.fulfill(json={"tasks": [{
                        "id": TASK_ID, "status": "completed" if self.writer_finished else "queued",
                        "queue_position": 1, "message": "", "error": "",
                        "result": {"ok": True} if self.writer_finished else None,
                    }]})
                    return
            if request.method == "POST":
                if path == API + "/token/validate":
                    route.fulfill(json={"has_access_token": True, "valid": True, "validation_status": "valid"})
                    return
                if path == FILE_CHANGE:
                    self.writes.append(request.post_data_json)
                    route.fulfill(status=202, json={"ok": True, "task_id": TASK_ID, "queued": True})
                    return
        self.unexpected.append(request.method + " " + request.url)
        route.abort()

    def open_inbox(self, width=1280):
        self.mobile = width < 600
        context = self.browser.new_context(
            viewport={"width": width, "height": 844 if self.mobile else 900},
            is_mobile=self.mobile, has_touch=self.mobile,
            service_workers="block", reduced_motion="reduce",
        )
        self.addCleanup(context.close)
        self.addCleanup(self.assertEqual, self.unexpected, [], "所有请求都必须在离线测试夹具内")
        self.addCleanup(self.assertEqual, self.errors, [], "页面脚本不能报错")
        context.route("**/*", self.route_request)
        context.route_web_socket("**/*", lambda ws: ws.close())
        self.page = context.new_page()
        self.page.set_default_timeout(4000)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN + "/guangya")
        self.activate(self.drive_list.get_by_title("打开 浏览目录", exact=True))
        expect(self.drive_list.get_by_text("A.txt", exact=True)).to_be_visible()

    @property
    def drive_list(self):
        return self.page.locator("#gyDirList")

    @property
    def search_input(self):
        return self.page.locator("#gyDriveSearch")

    def activate(self, locator):
        if self.mobile:
            locator.tap()
        else:
            locator.click()

    def search_drive(self):
        self.search_input.fill(KEYWORD)
        if self.mobile:
            form = self.page.locator("form", has=self.search_input)
            self.activate(form.get_by_role("button", name="搜索", exact=True))
        else:
            self.search_input.press("Enter")
        for item in (MATCHED_FILE, MATCHED_FOLDER):
            expect(self.drive_list.get_by_text(item["name"], exact=True)).to_be_visible()
        expect(self.drive_list.get_by_text("A.txt", exact=True)).to_have_count(0)
        expect(self.page.locator("#gyBreadcrumb")).to_contain_text(KEYWORD)
        self.assertEqual(self.search_queries[-1], {"keyword": [KEYWORD], "page": ["1"]})

    def assert_precedes(self, first, next_id):
        self.assertTrue(first.evaluate(
            "(el, id) => Boolean(el.compareDocumentPosition(document.getElementById(id))"
            " & Node.DOCUMENT_POSITION_FOLLOWING)", next_id,
        ))

    def assert_fits_phone(self, *controls):
        viewport = self.page.viewport_size
        assert viewport is not None
        width = viewport["width"]
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
        for control in controls:
            expect(control).to_be_visible()
            rect = control.bounding_box()
            assert rect is not None
            self.assertGreaterEqual(rect["x"], -1)
            self.assertLessEqual(rect["x"] + rect["width"], width + 1)

    def submit_writer(self, button, expected_body):
        with self.page.expect_response(f"**{FILE_CHANGE}") as submission:
            with self.page.expect_response(f"**{WRITER_STATUS}?*") as queued:
                self.activate(button)
        self.assertEqual(submission.value.status, 202)
        self.assertEqual(self.writes, [expected_body])
        self.assertEqual(queued.value.json()["tasks"][0]["status"], "queued")

    def complete_writer(self):
        with self.page.expect_response(f"**{WRITER_STATUS}?*") as completed:
            self.writer_finished = True
        self.assertEqual(completed.value.json()["tasks"][0]["status"], "completed")

    def test_enter_searches_entire_drive_and_pagination_can_return_to_sorted_origin(self):
        self.open_inbox()
        sort = self.page.locator("#gySortSelect")
        form = self.page.locator("form", has=self.search_input)
        expect(form).to_be_visible()
        self.assert_precedes(form, "gySortSelect")
        search_rect = form.bounding_box()
        sort_rect = sort.bounding_box()
        assert search_rect is not None and sort_rect is not None
        self.assertLessEqual(search_rect["x"] + search_rect["width"], sort_rect["x"])
        sort.select_option("name_desc")
        names = self.drive_list.locator(".gy-dir-name")
        expect(names).to_have_text(["Z.txt", "A.txt"])
        pager = self.page.get_by_role("navigation", name="搜索结果分页")
        previous = pager.get_by_role("button", name="上一页")
        following = pager.get_by_role("button", name="下一页")

        for return_to_inbox in ("clear", "up"):
            with self.subTest(return_to_inbox=return_to_inbox):
                self.search_drive()
                expect(pager).to_contain_text("第 1 页")
                expect(previous).to_be_disabled()
                following.click()
                expect(pager).to_contain_text("第 2 页")
                expect(names).to_have_text([MATCHED_TRAILER["name"]])
                expect(following).to_be_disabled()
                if return_to_inbox == "clear":
                    self.search_input.fill("")
                else:
                    previous.click()
                    expect(pager).to_contain_text("第 1 页")
                    expect(names).to_have_text([MATCHED_FOLDER["name"], MATCHED_FILE["name"]])
                    self.page.locator("#gyUpBtn").click()
                expect(names).to_have_text(["Z.txt", "A.txt"])
                expect(pager).to_be_hidden()
                expect(self.page.locator("#gyBreadcrumb .dir-breadcrumb-current")).to_have_text("浏览目录")
                expect(self.search_input).to_have_value("")
                expect(sort).to_have_value("name_desc")
        first_page = {"keyword": [KEYWORD], "page": ["1"]}
        second_page = {"keyword": [KEYWORD], "page": ["2"]}
        self.assertEqual(self.search_queries, [first_page, second_page, first_page, second_page, first_page])
        self.assertEqual(self.writes, [])

    def select_search_result_destination(self, item, operation, target_id, path=()):
        action = self.drive_list.locator(f'[data-media-id="{item["file_id"]}"]').get_by_role(
            "button", name="打开项目操作",
        )
        self.activate(action)
        menu = self.page.get_by_role("menu", name="目录与文件操作")
        for key in ("copy", "move"):
            expect(menu.locator(f'[data-browser-action="{key}"]')).to_be_visible()
        self.activate(menu.locator(f'[data-browser-action="{operation}"]'))

        picker = self.page.locator(".settings-dir-modal").get_by_role("dialog")
        for name in path:
            self.activate(picker.get_by_title(f"进入 {name}", exact=True))
        expect(picker.locator("[data-dir-current]")).to_have_text("当前：" + (path[-1] if path else "根目录"))
        choose = picker.get_by_role("button", name="选择当前目录", exact=True)
        if self.mobile:
            self.assert_fits_phone(picker, choose)
        self.assertEqual(self.writes, [])
        self.submit_writer(choose, {
            "operation": operation, "file_id": item["file_id"],
            "expected_parent_id": item["parent_id"], "expected_name": item["name"],
            "target_dir_id": target_id,
        })
        expect(picker).to_have_count(0)

    def test_copy_global_search_file_to_root_uses_its_source_parent_and_writer_result(self):
        self.open_inbox()
        self.search_drive()
        self.select_search_result_destination(MATCHED_FILE, "copy", "0")
        matching_files = self.drive_list.get_by_text(MATCHED_FILE["name"], exact=True)
        expect(matching_files).to_have_count(1)
        self.search_pages["1"].append(drive_item("copied-file", MATCHED_FILE["name"], "0"))
        self.complete_writer()
        expect(matching_files).to_have_count(2)
        expect(self.page.locator("#appToastContainer").get_by_text("复制完成", exact=True)).to_be_visible()

    def test_move_global_search_directory_on_phone_updates_results_after_writer(self):
        self.open_inbox(width=390)
        self.assert_fits_phone(self.search_input, self.page.locator("#gySortSelect"))
        self.search_drive()
        pager = self.page.get_by_role("navigation", name="搜索结果分页")
        self.assert_fits_phone(pager, pager.get_by_role("button", name="下一页"))
        self.select_search_result_destination(MATCHED_FOLDER, "move", "destination", ("归档盘", "归档目标"))
        moved = self.drive_list.get_by_text(MATCHED_FOLDER["name"], exact=True)
        expect(moved).to_be_visible()
        self.search_pages["1"] = [MATCHED_FILE, {**MATCHED_FOLDER, "parent_id": "destination"}]
        with self.page.expect_response(f"**{SEARCH}?*"):
            self.complete_writer()
        expect(moved).to_be_visible()
        expect(self.drive_list.get_by_text(MATCHED_FILE["name"], exact=True)).to_be_visible()
        expect(self.page.locator("#gyBreadcrumb")).to_contain_text(KEYWORD)
        expect(self.page.locator("#appToastContainer").get_by_text("移动完成", exact=True)).to_be_visible()
        self.assert_fits_phone(self.search_input, self.page.locator("#gySortSelect"))

    def test_create_folder_inline_before_up_on_narrow_phone(self):
        self.open_inbox(width=320)
        new_folder = self.page.locator("#gyNewFolderBtn")
        up = self.page.locator("#gyUpBtn")
        sort = self.page.locator("#gySortSelect")
        name = self.page.locator("#gyFolderName")
        form = self.page.locator("form", has=name)
        create = form.get_by_role("button", name="创建", exact=True)
        cancel = form.get_by_role("button", name="取消", exact=True)
        self.assert_precedes(new_folder, "gyUpBtn")
        self.assert_precedes(up, "gyFolderName")
        self.assert_fits_phone(self.search_input, sort, new_folder, up)
        expect(form).to_be_hidden()

        self.activate(new_folder)
        name.fill("未提交草稿")
        self.assert_fits_phone(form, name, create, cancel)
        self.activate(cancel)
        expect(form).to_be_hidden()
        self.assertEqual(self.writes, [])

        self.activate(new_folder)
        expect(name).to_have_value("")
        name.fill("新建归档")
        self.submit_writer(create, {"operation": "mkdir", "target_dir_id": "inbox", "name": "新建归档"})
        created = self.drive_list.get_by_text("新建归档", exact=True)
        expect(created).to_have_count(0)
        self.directories["inbox"].append(drive_item("created", "新建归档", "inbox", directory=True))
        self.complete_writer()
        expect(form).to_be_hidden()
        expect(created).to_be_visible()
        expect(self.page.locator("#gyBreadcrumb .dir-breadcrumb-current")).to_have_text("浏览目录")
        self.assert_fits_phone(new_folder, up)
