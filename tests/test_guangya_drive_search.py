"""光鸭全盘搜索的上游协议与浏览 API 回归。"""
from __future__ import annotations

from unittest.mock import ANY, Mock, call

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import sdk
from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaSearchHit
from app.modules.organize import OrganizeRules
from app.routes import guangya_api
from tests.support import authenticated_session


@pytest.fixture
def search_transport(tmp_path, monkeypatch):
    transport = Mock(spec=["request", "fs_files"])
    monkeypatch.setattr(GuangYaClient, "raw", property(lambda _: transport))
    client = GuangYaClient(tmp_path / "guangya_token.json")
    try:
        yield client, transport
    finally:
        client.close()


def test_provider_search_uses_native_whole_drive_paging_and_parent_names(search_transport):
    client, transport = search_transport
    keyword = "样片 & 集锦"
    transport.request.side_effect = [
        httpx.Response(200, json={"msg": "success", "data": {"keyword": keyword, "total": 3, "list": [
            {"fileId": "clip", "fileName": "样片.MKV", "resType": 1, "fileSize": 1024,
             "parentId": "nested", "parentName": "剪辑", "gcid": "etag-clip",
             "ctime": 123, "utime": 456, "ext": "mkv", "mineType": "video/x-matroska"},
            {"fileId": "duplicate-name", "fileName": "样片.MKV", "resType": 1,
             "parentId": "other", "parentName": "备份"},
        ]}}),
        httpx.Response(200, json={"msg": "success", "data": {"keyword": keyword, "list": [
            {"fileId": "folder", "fileName": "样片.MKV", "resType": 2},
        ]}}),
        httpx.Response(200, json={"msg": "success", "data": {"keyword": keyword}}),
    ]

    hits, has_more = client.search_files(keyword, page_size=2)
    assert hits == [
        GuangYaSearchHit(
            GuangYaFile("clip", "样片.MKV", False, 1024, "etag-clip", "nested",
                        created_at=123, updated_at=456, mime_type="video/x-matroska", extension="mkv"),
            "剪辑",
        ),
        GuangYaSearchHit(GuangYaFile("duplicate-name", "样片.MKV", False, parent_id="other"), "备份"),
    ]
    assert has_more is True
    # 第二页省略 total，即使条目数少于 page_size，也允许继续查询。
    assert client.search_files(keyword, page=1, page_size=2) == (
        [GuangYaSearchHit(GuangYaFile("folder", "样片.MKV", True), "")], True,
    )
    assert client.search_files(keyword, page=2, page_size=2) == ([], False)
    assert transport.request.call_args_list == [
        call("https://api.guangyapan.com/userres/v1/file/search_files", method="POST",
             json={"name": keyword, "page": page, "pageSize": 2}, timeout=ANY)
        for page in range(3)
    ]


def test_provider_search_stops_when_total_is_reached(search_transport):
    client, transport = search_transport
    transport.request.return_value = httpx.Response(200, json={"msg": "success", "data": {
        "keyword": "样片", "total": 1, "list": [{"fileId": "clip", "fileName": "样片.MKV"}],
    }})

    hits, has_more = client.search_files("样片")

    assert [hit.file.file_id for hit in hits] == ["clip"]
    assert has_more is False
    assert transport.request.call_count == 1


@pytest.mark.parametrize("payload", [
    {"code": 403, "msg": "private-provider-detail", "data": {"keyword": "样片"}},
    {"data": {"keyword": "样片"}},
    {"msg": "success", "data": {"list": [], "total": 1}},
    {"msg": "success", "data": {"list": [{"fileName": "样片.MKV"}]}},
])
def test_provider_search_never_reports_read_failure_as_empty_results(search_transport, payload):
    client, transport = search_transport
    transport.request.return_value = httpx.Response(200, json=payload)

    with pytest.raises(RuntimeError) as caught:
        client.search_files("样片")

    assert "private-provider-detail" not in str(caught.value)


@pytest.fixture
def search_api(monkeypatch):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="guangya-drive-search-test")
    app.include_router(guangya_api.router)

    @app.post("/__test/login")
    def login(request: Request):
        request.session.update(authenticated_session())

    provider = Mock(spec=GuangYaClient)
    provider.logged_in = True
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr(guangya_api, "GuangYaClient", provider_factory)
    monkeypatch.setattr(OrganizeRules, "from_config", lambda: OrganizeRules(video_exts="mkv"))
    with TestClient(app) as client:
        yield client, provider, provider_factory


def test_search_api_requires_login_before_opening_cloud_client(search_api):
    client, _, provider_factory = search_api

    response = client.get("/api/guangya/search", params={"keyword": "样片"})

    assert response.status_code == 401
    provider_factory.assert_not_called()


def test_search_api_matches_directory_items_and_reports_next_page(search_api):
    client, provider, _ = search_api
    files = [
        GuangYaFile("clip", "样片.MKV", False, 1024, "etag-clip", "nested",
                    created_at=123, updated_at=456, mime_type="video/x-matroska"),
        GuangYaFile("folder", "样片.MKV", True, parent_id="nested"),
    ]
    provider.list_dir.return_value = files
    provider.search_files.return_value = ([GuangYaSearchHit(f, "剪辑") for f in files], True)
    client.post("/__test/login")
    directory = client.get("/api/guangya/dirs", params={"parent_id": "nested"})

    response = client.get("/api/guangya/search", params={
        "keyword": "  样片 & 集锦  ", "page": 2, "page_size": 10,
    })

    assert directory.status_code == response.status_code == 200
    assert response.json() == {
        "items": [{**item, "parent_name": "剪辑"} for item in directory.json()],
        "page": 2,
        "page_size": 10,
        "has_more": True,
        "next_page": 3,
    }
    assert [item["is_video"] for item in response.json()["items"]] == [True, False]
    provider.search_files.assert_called_once_with("样片 & 集锦", page=1, page_size=10)
    provider.list_dir.assert_called_once_with("nested")
    provider.file_info.assert_not_called()
    assert provider.close.call_count == 2


def test_directory_api_keeps_first_page_total_when_middle_page_omits_it(
    search_api, search_transport, monkeypatch,
):
    web, _, provider_factory = search_api
    client, transport = search_transport
    monkeypatch.setattr(GuangYaClient, "logged_in", property(lambda _: True))
    provider_factory.return_value = client
    transport.fs_files.side_effect = [
        {"data": {"total": 3, "list": [{"fileId": "1", "fileName": "第一项"}]}},
        {"data": {"list": [{"fileId": "2", "fileName": "第二项"}]}},
        {"data": {"list": [{"fileId": "3", "fileName": "第三项"}]}},
    ]
    web.post("/__test/login")

    response = web.get("/api/guangya/dirs", params={"parent_id": "nested"})

    assert response.status_code == 200, response.text
    assert [(row["file_id"], row["parent_id"]) for row in response.json()] == [
        (file_id, "nested") for file_id in ("1", "2", "3")
    ]
    assert [request.kwargs["page"] for request in transport.fs_files.call_args_list] == [0, 1, 2]


def test_search_api_returns_empty_page_with_no_continuation(search_api):
    client, provider, _ = search_api
    provider.search_files.return_value = ([], False)
    client.post("/__test/login")

    response = client.get("/api/guangya/search", params={"keyword": "没有命中"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [], "page": 1, "page_size": 20, "has_more": False, "next_page": None,
    }
    provider.search_files.assert_called_once_with("没有命中", page=0, page_size=20)
    provider.close.assert_called_once()


def test_agent_finds_same_name_files_then_resolves_only_the_selected_path(search_api, monkeypatch):
    _, provider, factory = search_api
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", factory)
    first = GuangYaFile("copy-a", "样片.mkv", False, parent_id="staging")
    second = GuangYaFile("copy-b", "样片.mkv", False, parent_id="season")
    provider.search_files.side_effect = [
        ([GuangYaSearchHit(first, "离线")], True), ([GuangYaSearchHit(second, "第一季")], False),
    ]
    provider.file_info.side_effect = {
        "copy-b": second, "season": GuangYaFile("season", "第一季", True, parent_id="show"),
        "show": GuangYaFile("show", "剧集", True, parent_id="0"),
    }.get
    page = sdk.cloud.search_files("样片", page_size=1)
    other = sdk.cloud.search_files("样片", page=page["next_page"], page_size=1)
    assert [p["items"][0]["parent_name"] for p in (page, other)] == ["离线", "第一季"]
    assert other["next_page"] is None
    provider.file_info.assert_not_called()
    selected = sdk.cloud.info(other["items"][0]["file_id"], include_path=True)["item"]
    assert selected["path"] == "/剧集/第一季/样片.mkv"
    assert provider.file_info.call_args_list == [call("copy-b"), call("season"), call("show")]
    with pytest.raises(RuntimeError, match="文件或目录不存在"):
        sdk.cloud.info("already-removed", include_path=True)
    assert provider.search_files.call_args_list == [
        call("样片", page=i, page_size=1) for i in (0, 1)
    ]
    provider.iter_dir.assert_not_called()
    provider.list_dir.assert_not_called()


@pytest.mark.parametrize(("params", "status"), [
    ({}, 400),
    ({"keyword": " \t "}, 400),
    ({"keyword": "样片", "page": 0}, 422),
    ({"keyword": "样片", "page_size": 101}, 422),
])
def test_search_api_rejects_invalid_input_without_provider_io(search_api, params, status):
    client, _, provider_factory = search_api
    client.post("/__test/login")

    response = client.get("/api/guangya/search", params=params)

    assert response.status_code == status
    provider_factory.assert_not_called()


def test_search_api_rejects_unconnected_cloud_account(search_api):
    client, provider, _ = search_api
    client.post("/__test/login")
    provider.logged_in = False

    response = client.get("/api/guangya/search", params={"keyword": "样片"})

    assert response.status_code == 503
    provider.search_files.assert_not_called()
    provider.close.assert_called_once()


def test_search_api_hides_provider_failure_and_closes_client(search_api, caplog):
    client, provider, _ = search_api
    client.post("/__test/login")
    provider.search_files.side_effect = RuntimeError("private-provider-detail")

    response = client.get("/api/guangya/search", params={"keyword": "样片"})

    assert response.status_code == 502
    assert response.json().get("error")
    assert "private-provider-detail" not in response.text
    assert "private-provider-detail" not in caplog.text
    provider.close.assert_called_once()
