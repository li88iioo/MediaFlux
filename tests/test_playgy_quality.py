"""同一签名 STRM 的云端画质合同：真实路由、客户端与隔离 Provider。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients import guangya as guangya_module
from app.clients.guangya import GuangYaClient
from app.modules.playgy_signing import sign_playgy
from app.modules import media_proxy
from app.modules.strm import build_play_url
from app.routes import proxy


_FILE_ID = "quality-file"
_ETAG = "quality-etag"
_SIZE = 8_765_432_100
_FILENAME = "裁切影片.mp4"
_SOURCE_GCID = "source-gcid"
_MP4_GCID = "same-size-h264"
_AV1_GCID = "same-size-av1"
_HLS_GCID = "cropped-hls"
_FORMAT_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"
_API = "https://api.guangyapan.com/"
_DETAIL = _API + "userres/v1/file/get_file_detail"
_ORIGINAL_URL = _API + "nd.bizuserres.s/v1/get_res_download_url"
_VOD_URL = _API + "userres/v1/file/get_vod_download_url"
_ACCESS_TOKEN = "quality-test-access-token-never-real"
_REFRESH_TOKEN = "quality-test-refresh-token-never-real"
_PRIVATE_DETAIL = "private-provider-diagnostic"
_VARIANT_FIELDS = {
    "id", "original", "label", "width", "height", "video_codec",
    "audio_codec", "bitrate", "duration", "transport", "mime_type", "container",
}


def _resource(gcid, width=3840, height=1608, *, video_type="mp4", codec="h264"):
    return {"gcid": gcid, "info": {
        "resolution": {"width": width, "height": height},
        "duration": 7200, "bitRate": 6_000_000,
        "videoCodec": codec, "audioCodec": "aac", "videoType": video_type,
        "resolutionName": f"{width}×{height}", "needVipType": 0,
    }}


class _Provider:
    """只允许详情与取流三个只读接口；意外转码请求也会被记录并拒绝。"""

    token = _ACCESS_TOKEN
    refresh_token_value = _REFRESH_TOKEN
    token_expires_at = None
    download_url_user_agent_bound = False

    def __init__(self):
        source = _resource(_SOURCE_GCID, video_type=_FORMAT_FAMILY, codec="hevc")
        source["info"]["mimeType"] = "video/mp4"
        self.detail: dict[str, Any] = {"code": 0, "msg": "success", "data": {
            "fileInfo": {"fileId": _FILE_ID, "gcid": _SOURCE_GCID,
                         "fileName": _FILENAME, "fileSize": _SIZE},
            "videoResource": [
                source, _resource(_MP4_GCID), _resource(_AV1_GCID, codec="av1"),
                _resource(_HLS_GCID, 1920, 804, video_type="mpegts"),
            ],
        }}
        self.original_url = "https://storage.invalid/original.mp4?signature=original-test"
        self.original_response: dict[str, Any] = {"code": 0, "msg": "success", "data": {
            "signedURL": self.original_url, "urlDuration": 21600,
        }}
        self.vod_responses: dict[str, dict[str, Any]] = {
            gcid: {"code": 0, "msg": "success", "data": {
                "signedURL": f"https://storage.invalid/{gcid}.{extension}?signature=vod-test",
                "urlDuration": 600,
            }}
            for gcid, extension in ((_MP4_GCID, "mp4"), (_AV1_GCID, "mp4"), (_HLS_GCID, "m3u8"))
        }
        self.calls = []
        self.on_request = None

    def request(self, url, method="GET", **kwargs):
        call = {"url": url, "method": method, "json": deepcopy(kwargs.get("json")),
                "timeout": kwargs.get("timeout")}
        self.calls.append(call)
        if self.on_request is not None:
            self.on_request(call)
        if method != "POST":
            raise AssertionError(f"画质读取必须使用 POST: {method}")
        if url == _DETAIL:
            payload = self.detail
        elif url == _ORIGINAL_URL:
            payload = self.original_response
        elif url == _VOD_URL:
            payload = self.vod_responses[kwargs["json"]["gcid"]]
        else:
            raise AssertionError(f"禁止请求转码或其它接口: {url}")
        return httpx.Response(200, json=deepcopy(payload), request=httpx.Request(method, url))


class PlayGyQualityTests(unittest.TestCase):
    def setUp(self):
        self.provider = _Provider()
        self.client = object.__new__(GuangYaClient)
        self.client._raw = self.provider
        self.client._last_persisted_token = _ACCESS_TOKEN
        self.clock = [1000.0]
        self.enterContext(patch.object(self.client, "_invalidate_if_stale", return_value=False))
        self.enterContext(patch.dict(guangya_module._READ_CONGESTION, {}, clear=True))
        self.enterContext(patch.object(guangya_module, "monotonic", side_effect=lambda: self.clock[0]))
        self.enterContext(patch.object(guangya_module, "sleep", side_effect=self._advance))
        self.factory = self.enterContext(patch.object(proxy, "GuangYaClient", return_value=self.client))
        self.enterContext(patch.object(proxy, "close_guangya_client"))
        self.enterContext(patch.object(proxy, "_playgy_signed_urls", proxy.SignedUrlCache(max_entries=16)))
        self.enterContext(patch("app.modules.playgy_signing.get_web_secret", return_value="quality-tests-isolated-signing-key"))
        app = FastAPI()
        app.include_router(proxy.router)
        self.http = self.enterContext(TestClient(app, follow_redirects=False))
        self.strm_url = build_play_url("http://testserver", _FILE_ID, _ETAG, _SIZE, _FILENAME)

    def _advance(self, seconds):
        self.clock[0] += seconds

    def _url(self, *, base=None, **query):
        return (base or self.strm_url) + "&" + urlencode(query)

    def _calls(self):
        return [call["url"] for call in self.provider.calls]

    def _variants(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue({"protocol", "variants"}.issubset(payload))
        self.assertEqual(payload["protocol"], "mediaflux.playback.v1")
        for variant in payload["variants"]:
            self.assertTrue(_VARIANT_FIELDS.issubset(variant))
            self.assertIsInstance(variant["original"], bool)
            self.assertIsInstance(variant["label"], str)
            self.assertTrue(variant["label"])
        variants = {variant["id"]: variant for variant in payload["variants"]}
        self.assertEqual(len(variants), len(payload["variants"]))
        return variants

    def _resolved(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue({"protocol", "variant", "url", "expires_in"}.issubset(payload))
        self.assertEqual(payload["protocol"], "mediaflux.playback.v1")
        self.assertTrue(_VARIANT_FIELDS.issubset(payload["variant"]))
        return payload

    def _error(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        self.assertIsInstance(response.json().get("error"), str)
        public = response.text + str(dict(response.headers))
        for private in (_ACCESS_TOKEN, _REFRESH_TOKEN, _PRIVATE_DETAIL,
                        "Traceback", "AttributeError", "RuntimeError", "FileNotFoundError"):
            self.assertNotIn(private, public)

    def test_proxy_source_keeps_quality_entry_and_resolves_original_and_cloud_variant(self):
        for binding in (None, {"id": 1, "source_type": "guangya", "guangya_file_id": _FILE_ID,
                               "media_source_id": "cloud"}):
            with self.subTest(binding=bool(binding)):
                # 同时覆盖含斜线 etag 的 b64 路径，不重构/丢失原签名参数。
                strm_url = (build_play_url("http://testserver", _FILE_ID, "et/ag", _SIZE, _FILENAME)
                            if binding else self.strm_url)
                payload = {"MediaSources": [
                    {"Id": "cloud", "Protocol": "Http", "Path": strm_url},
                    {"Id": "local", "Protocol": "File", "Path": "/media/local.mp4"},
                ]}
                local = deepcopy(payload["MediaSources"][1])
                with patch.object(media_proxy.database, "get_media_proxy_binding",
                                  side_effect=lambda _instance, _item, source: binding if source == "cloud" else None):
                    result, changed = media_proxy.rewrite_playback_info(
                        payload, 7, "quality-item",
                        dynamic_mappings=media_proxy.DynamicGuangYaMappings(),
                    )
                self.assertTrue(changed)
                source = result["MediaSources"][0]
                self.assertEqual(result["MediaSources"][1], local)
                self.assertEqual(source["Path"], "/Videos/quality-item/stream?MediaSourceId=cloud")
                self.assertEqual(source["Protocol"], "File")
                entry = source["MediaFluxPlayback"]
                self.assertEqual(entry, {"protocol": "mediaflux.playback.v1", "source_url": strm_url})
                self._variants(self.http.get(self._url(base=entry["source_url"], view="variants")))
                for quality in ("original", _HLS_GCID):
                    resolved = self._resolved(self.http.get(self._url(
                        base=entry["source_url"], view="resolve", quality=quality,
                    )))
                    self.assertEqual(resolved["variant"]["id"], quality)
                self.assertEqual(self.http.get(entry["source_url"]).status_code, 302)

    def test_proxy_quality_entry_is_only_for_the_bound_file_and_signed_http_source(self):
        cases = [
            (self.strm_url, "another-file"),
            (self.strm_url.split("?")[0], _FILE_ID),
            (self.strm_url.replace("http://testserver", "file://testserver"), _FILE_ID),
            (self.strm_url.replace("http://testserver", "http://user:password@testserver"), _FILE_ID),
            ("/local/movie.mp4", _FILE_ID),
        ]
        for path, file_id in cases:
            with self.subTest(path=path, file_id=file_id):
                payload = {"MediaSources": [{"Id": "cloud", "Path": path,
                           "MediaFluxPlayback": {"protocol": "stale", "source_url": self.strm_url}}]}
                binding = {"id": 1, "source_type": "guangya", "guangya_file_id": file_id,
                           "media_source_id": "cloud"}
                with patch.object(media_proxy.database, "get_media_proxy_binding", return_value=binding):
                    result, changed = media_proxy.rewrite_playback_info(payload, 7, "quality-item")
                self.assertTrue(changed)
                self.assertNotIn("MediaFluxPlayback", result["MediaSources"][0])

    def test_variants_preserve_resource_identity_crop_and_real_format_family(self):
        for mime_from_provider in (True, False):
            with self.subTest(mime_from_provider=mime_from_provider):
                self.provider.calls.clear()
                file = self.provider.detail["data"]["fileInfo"]
                file["fileName"] = "影片.bin" if mime_from_provider else _FILENAME
                if not mime_from_provider:
                    self.provider.detail["data"]["videoResource"][0]["info"].pop("mimeType")
                variants = self._variants(self.http.get(self._url(view="variants")))

                self.assertEqual(set(variants), {"original", _MP4_GCID, _AV1_GCID, _HLS_GCID})
                self.assertEqual(sum(row["original"] for row in variants.values()), 1)
                for identity in ("original", _MP4_GCID, _AV1_GCID):
                    self.assertEqual((variants[identity]["width"], variants[identity]["height"]), (3840, 1608))
                original = variants["original"]
                self.assertEqual(original["container"], _FORMAT_FAMILY)
                self.assertEqual((original["transport"], original["mime_type"]), ("file", "video/mp4"))
                cropped = variants[_HLS_GCID]
                self.assertEqual((cropped["width"], cropped["height"]), (1920, 804))
                self.assertEqual((cropped["video_codec"], cropped["audio_codec"]), ("h264", "aac"))
                self.assertEqual((cropped["bitrate"], cropped["duration"]), (6_000_000, 7200))
                self.assertEqual((cropped["transport"], cropped["mime_type"], cropped["container"]),
                                 ("hls", "application/x-mpegURL", "mpegts"))
                self.assertEqual(variants[_AV1_GCID]["video_codec"], "av1")
                self.assertEqual(self._calls(), [_DETAIL])
                self.assertEqual(self.provider.calls[0]["json"], {"fileId": _FILE_ID})

    def test_only_original_keeps_unknown_metadata_and_unknown_expiry_null(self):
        self.provider.detail["data"]["videoResource"] = []
        self.provider.original_response["data"].pop("urlDuration")

        variants = self._variants(self.http.get(self._url(view="variants")))
        resolved = self._resolved(self.http.get(self._url(view="resolve", quality="original")))

        self.assertEqual(set(variants), {"original"})
        original = variants["original"]
        self.assertIs(original["original"], True)
        for field in ("width", "height", "video_codec", "audio_codec", "bitrate", "duration"):
            self.assertIsNone(original[field], field)
        self.assertEqual((original["transport"], original["mime_type"], original["container"]),
                         ("file", "video/mp4", "mp4"))
        self.assertEqual(resolved["variant"], original)
        self.assertIsNone(resolved["expires_in"])
        self.assertEqual(self._calls(), [_DETAIL, _DETAIL, _ORIGINAL_URL])

    def test_cloud_resources_need_real_dimensions_and_their_own_known_video_type(self):
        no_dimensions = _resource("not-generated")
        no_dimensions["info"].pop("resolution")
        no_type = _resource("missing-cloud-format")
        no_type["info"].pop("videoType")
        self.provider.detail["data"]["videoResource"] = [
            _resource(_HLS_GCID, 1920, 804, video_type="mpegts"), no_dimensions,
            _resource("zero-height", height=0), _resource("unknown-format", video_type="unknown"), no_type,
        ]

        variants = self._variants(self.http.get(self._url(view="variants")))

        self.assertEqual(set(variants), {"original", _HLS_GCID})
        self.assertEqual(self._calls(), [_DETAIL])

    def test_resolve_uses_each_resource_url_and_hls_never_uses_original_size(self):
        variants = self._variants(self.http.get(self._url(view="variants")))
        urls = []
        for quality in ("original", _MP4_GCID, _AV1_GCID, _HLS_GCID):
            with self.subTest(quality=quality):
                response = self.http.get(self._url(view="resolve", quality=quality),
                                         headers={"Range": "bytes=0-0"})
                resolved = self._resolved(response)
                original = quality == "original"
                data = (self.provider.original_response if original else self.provider.vod_responses[quality])["data"]
                self.assertEqual(resolved["variant"], variants[quality])
                self.assertEqual(resolved["url"], data["signedURL"])
                self.assertEqual(resolved["expires_in"], 21600 if original else 600)
                self.assertEqual(self.provider.calls[-1]["url"], _ORIGINAL_URL if original else _VOD_URL)
                self.assertEqual(self.provider.calls[-1]["json"],
                                 {"fileId": _FILE_ID} if original else {"fileId": _FILE_ID, "gcid": quality})
                self.assertNotEqual(response.headers.get("content-length"), str(_SIZE))
                self.assertNotIn("content-range", response.headers)
                self.assertNotIn("location", response.headers)
                urls.append(resolved["url"])
        self.assertEqual(len(set(urls)), 4)
        self.assertEqual(self._calls(), [_DETAIL] + [_DETAIL, _ORIGINAL_URL] + [_DETAIL, _VOD_URL] * 3)

    def test_foreign_and_removed_qualities_are_rejected_after_fresh_detail_without_vod(self):
        self.assertIn(_HLS_GCID, self._variants(self.http.get(self._url(view="variants"))))
        resources = self.provider.detail["data"]["videoResource"]
        self.provider.detail["data"]["videoResource"] = [row for row in resources if row["gcid"] != _HLS_GCID]

        for quality in ("another-files-gcid", _HLS_GCID):
            with self.subTest(quality=quality):
                self._error(self.http.get(self._url(view="resolve", quality=quality)), 404)
        self.assertEqual(self._calls(), [_DETAIL, _DETAIL, _DETAIL])

    def test_unsigned_and_tampered_identity_are_rejected_before_client_or_network(self):
        parsed = urlsplit(self.strm_url)
        signed = parse_qs(parsed.query)
        cases = [
            (parsed.path, {}, {"view": "variants"}),
            (parsed.path.replace(_FILE_ID, "foreign-file"), signed, {"view": "resolve", "quality": _HLS_GCID}),
            (parsed.path.replace(_ETAG, "tampered-etag"), signed, {"view": "variants"}),
            (parsed.path.replace(str(_SIZE), "1"), signed, {"view": "resolve", "quality": "original"}),
            (parsed.path, {**signed, "v": ["2"]}, {"view": "variants"}),
            (parsed.path, {**signed, "sig": ["0" * 64]}, {"view": "resolve", "quality": _HLS_GCID}),
            (parsed.path, {**signed, "sig": ["鸭" * 64]}, {"view": "variants"}),
            (parsed.path, {}, {"view": "unknown", "quality": "original"}),
        ]
        for path, credentials, view in cases:
            with self.subTest(path=path, view=view):
                query = {**credentials, **{key: [value] for key, value in view.items()}}
                response = self.http.get(urlunsplit(("", "", path, urlencode(query, doseq=True), "")))
                self._error(response, 403)
        self.factory.assert_not_called()
        self.assertEqual(self.provider.calls, [])

    def test_views_validate_query_and_head_before_network(self):
        for query in (
            {"view": "unknown"}, {"view": "resolve"}, {"view": "resolve", "quality": ""},
            {"view": "variants", "quality": "original"}, {"quality": "original"},
        ):
            with self.subTest(query=query):
                self._error(self.http.get(self._url(**query)), 400)
        for query in ({"view": "variants"}, {"view": "resolve", "quality": _HLS_GCID}):
            with self.subTest(head=query):
                response = self.http.head(self._url(**query), headers={"Range": "bytes=0-0"})
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers.get("allow"), "GET")
                self.assertEqual(response.content, b"")
                self.assertNotIn("content-range", response.headers)
        self.factory.assert_not_called()
        self.assertEqual(self.provider.calls, [])

    def test_logged_out_json_views_return_503_without_provider_requests(self):
        self.client._raw = None
        for query in ({"view": "variants"}, {"view": "resolve", "quality": "original"}):
            with self.subTest(query=query):
                self._error(self.http.get(self._url(**query)), 503)
        self.assertEqual(self.provider.calls, [])

    def test_b64_signed_strm_keeps_original_signature_redirect_and_local_head_range(self):
        file_id, etag = "目录/文件-id", "etag/片段"
        self.provider.detail["data"]["fileInfo"]["fileId"] = file_id
        base = build_play_url("http://testserver", file_id, etag, _SIZE, _FILENAME)
        query = parse_qs(urlsplit(base).query)
        self.assertEqual(query, {"v": ["1"], "sig": [sign_playgy(file_id, etag, _SIZE)], "enc": ["b64"]})

        resolved = self._resolved(self.http.get(self._url(base=base, view="resolve", quality=_HLS_GCID)))
        redirect = self.http.get(base)
        head = self.http.head(base, headers={"Range": "bytes=0-0"})

        self.assertEqual(resolved["variant"]["id"], _HLS_GCID)
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect.headers["location"], self.provider.original_url)
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.headers["content-length"], "1")
        self.assertEqual(head.headers["content-range"], f"bytes 0-0/{_SIZE}")
        self.assertNotIn("location", head.headers)
        self.assertEqual(head.content, b"")
        self.assertEqual(self._calls(), [_DETAIL, _VOD_URL, _ORIGINAL_URL])
        self.assertTrue(all(call["json"]["fileId"] == file_id for call in self.provider.calls))

    def test_provider_failures_are_502_without_diagnostics_credentials_or_empty_success(self):
        valid = deepcopy(self.provider.detail)
        missing_file = deepcopy(valid)
        missing_file["data"].pop("fileInfo")
        wrong_file = deepcopy(valid)
        wrong_file["data"]["fileInfo"]["fileId"] = "unexpected-file"
        invalid_resources = deepcopy(valid)
        invalid_resources["data"]["videoResource"] = {"private": _PRIVATE_DETAIL}
        business_error = {"code": 42, "msg": f"{_PRIVATE_DETAIL} Authorization: Bearer {_ACCESS_TOKEN}",
                          "data": {"refresh_token": _REFRESH_TOKEN}}
        for payload in (missing_file, wrong_file, invalid_resources, business_error):
            with self.subTest(payload=payload):
                self.provider.detail = payload
                self.provider.calls.clear()
                self._error(self.http.get(self._url(view="variants")), 502)
                self.assertEqual(self._calls(), [_DETAIL])
        self.provider.detail = valid
        self.provider.calls.clear()
        self.provider.vod_responses[_HLS_GCID] = {"code": 0, "msg": "success", "data": {"urlDuration": 600}}
        self._error(self.http.get(self._url(view="resolve", quality=_HLS_GCID)), 502)
        self.assertEqual(self._calls(), [_DETAIL, _VOD_URL])

    def test_client_resolve_keeps_one_read_deadline_and_reduces_transport_budget(self):
        self.provider.on_request = lambda call: self._advance(1.25 if call["url"] == _DETAIL else 0)
        with patch.object(self.client, "_call_read", wraps=self.client._call_read) as reads:
            resolved = self.client.playback_info(_FILE_ID, quality=_HLS_GCID, timeout=2.0)

        self.assertTrue({"variant", "url", "expires_in"}.issubset(resolved))
        self.assertNotIn("protocol", resolved)
        self.assertEqual(resolved["variant"]["id"], _HLS_GCID)
        self.assertEqual(self._calls(), [_DETAIL, _VOD_URL])
        self.assertEqual([call.kwargs.get("deadline") for call in reads.call_args_list], [1002.0, 1002.0])
        self.assertEqual([call["timeout"] for call in self.provider.calls], [2.0, 0.75])

    def test_total_budget_exhaustion_and_transport_timeout_return_504_without_secret(self):
        for detail_exhausts_budget in (True, False):
            with self.subTest(detail_exhausts_budget=detail_exhausts_budget):
                self.clock[0] = 1000.0
                self.provider.calls.clear()

                def consume_budget(call):
                    if call["url"] == _DETAIL:
                        self._advance(call["timeout"] if detail_exhausts_budget else 3.0)
                    else:
                        self._advance(call["timeout"])
                        raise httpx.ReadTimeout(f"{_PRIVATE_DETAIL}: {_ACCESS_TOKEN}")

                self.provider.on_request = consume_budget
                self._error(self.http.get(self._url(view="resolve", quality=_HLS_GCID)), 504)
                self.assertEqual(self._calls(), [_DETAIL] if detail_exhausts_budget else [_DETAIL, _VOD_URL])
                self.assertEqual([call["timeout"] for call in self.provider.calls],
                                 [8.0] if detail_exhausts_budget else [8.0, 5.0])


if __name__ == "__main__":
    unittest.main()
