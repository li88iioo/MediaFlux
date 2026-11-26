"""真实ASGI长流中的早期可见记录、正常取消与上游断流结算。"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx

from app.modules import media_proxy
from app.repositories import media_proxy as repository
from tests.support import isolated_test_database


class LivePlaybackRecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())

    async def test_stalled_web_audio_retry_releases_first_hls_contract(self):
        playlist = b"#EXTM3U\n#EXT-X-ENDLIST\n"
        first = {
            "PlaySessionId": "first-session",
            "MediaSources": [{
                "Id": "main-source",
                "Path": "/playgy/fixture-file/v1/100/film.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/film/master.m3u8?PlaySessionId=first-session&MediaSourceId=main-source",
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {"Type": "Audio", "Index": 1, "Codec": "eac3", "Language": "eng"},
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                ],
            }],
        }
        request_body = {
            "MediaSourceId": "main-source",
            "DeviceProfile": {"Name": "Jellyfin Web", "DirectPlayProfiles": [{
                "Type": "Video", "Container": "mkv", "VideoCodec": "h264", "AudioCodec": "aac",
            }]},
        }
        auth = {"X-Emby-Token": "fixture-user", "X-Emby-Client": "Jellyfin Web"}
        for stall_after_headers in (False, True):
            with self.subTest(stall_after_headers=stall_after_headers):
                retry_started, retry_closed = asyncio.Event(), asyncio.Event()
                playback_requests = []
                playlist_requests = []

                async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
                    try:
                        request_head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                        request_line, *header_lines = request_head.strip().split("\r\n")
                        _, target, _ = request_line.split()
                        headers = dict(line.lower().split(":", 1) for line in header_lines)
                        if urlsplit(target).path.endswith("/PlaybackInfo"):
                            request_bytes = await reader.readexactly(int(headers["content-length"]))
                            playback_requests.append(json.loads(request_bytes))
                            retry = len(playback_requests) > 1
                            body, content_type = json.dumps(first).encode(), "application/json"
                        else:
                            playlist_requests.append(target)
                            retry = False
                            body, content_type = playlist, "application/vnd.apple.mpegurl"
                        if not retry or stall_after_headers:
                            writer.write((
                                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                            ).encode() + (body[:1] if retry else body))
                            await writer.drain()
                        if retry:
                            retry_started.set()
                            # 保持本机上游连接打开，直到反代主动释放。
                            await reader.read()
                            retry_closed.set()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_server(upstream, "127.0.0.1", 0)
                async with server:
                    port = server.sockets[0].getsockname()[1]
                    instance_id = repository.add_media_proxy_instance(
                        name="Local Jellyfin", server_type="jellyfin", upstream_url=f"http://127.0.0.1:{port}",
                        api_key="fixture-api", listen_host="127.0.0.1", listen_port=18096 + int(stall_after_headers),
                    )
                    instance = repository.get_media_proxy_instance(instance_id)
                    assert instance is not None
                    with patch.object(media_proxy, "_WEB_AUDIO_RETRY_TIMEOUT_SECONDS", 0.2, create=True):
                        app = media_proxy.create_proxy_app(dict(instance))
                        async with (
                            app.router.lifespan_context(app),
                            httpx.AsyncClient(
                                transport=httpx.ASGITransport(app=app), base_url="http://proxy.invalid", trust_env=False,
                            ) as client,
                        ):
                            pending = asyncio.create_task(client.post(
                                "/Items/film/PlaybackInfo", headers=auth, json=request_body,
                            ))
                            try:
                                await asyncio.wait_for(retry_started.wait(), 2)
                                try:
                                    response = await asyncio.wait_for(asyncio.shield(pending), 1.5)
                                except TimeoutError:
                                    self.fail("直放优化停顿后，已取得的 HLS 播放信息仍未返回")
                                self.assertEqual(response.status_code, 200)
                                self.assertEqual(response.json()["PlaySessionId"], "first-session")
                                source = response.json()["MediaSources"][0]
                                self.assertEqual(source["Id"], "main-source")
                                self.assertEqual(source["TranscodingUrl"], first["MediaSources"][0]["TranscodingUrl"])
                                self.assertFalse(source["SupportsDirectPlay"])
                                await asyncio.wait_for(retry_closed.wait(), 2)
                                hls = await client.get(source["TranscodingUrl"], headers=auth)
                                self.assertEqual((hls.status_code, hls.content), (200, playlist))
                                self.assertEqual(playlist_requests, [source["TranscodingUrl"]])
                            finally:
                                if not pending.done():
                                    pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                    self.assertEqual(playback_requests, [request_body, {**request_body, "AudioStreamIndex": 2}])
                    records = repository.list_media_proxy_playback_records(instance_id=instance_id)["items"]
                    receipt, = [record for record in records if record["route_class"] == "playback_info"]
                    self.assertEqual((receipt["status_code"], receipt["failure_stage"]), (200, ""))

    async def test_live_response_head_cancel_and_upstream_error_keep_one_receipt(self):
        real_client = httpx.AsyncClient
        for ending in ("complete", "client_disconnect", "upstream_error"):
            with self.subTest(ending=ending):
                released, disconnected, first_body = asyncio.Event(), asyncio.Event(), asyncio.Event()

                class MediaStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b"first chunk"
                        await released.wait()
                        if ending == "upstream_error":
                            raise httpx.ReadError("synthetic upstream reset")
                        yield b"last chunk"

                def upstream(request):
                    if request.url.path.endswith("/PlaybackInfo"):
                        return httpx.Response(200, json={
                            "PlaySessionId": "fixture-session", "MediaSources": [{
                                "Id": "source", "Path": "/playgy/fake-file/etag/100/video.mp4",
                                "SupportsDirectPlay": False,
                            }],
                        })
                    return httpx.Response(206, stream=MediaStream(), headers={"Content-Type": "video/mp4"})

                def pinned(value, path=""):
                    logical = value.rstrip("/") + path
                    parsed = urlsplit(logical)
                    return media_proxy._PinnedUpstreamTarget(logical, logical, parsed.netloc, parsed.hostname, ("127.0.0.1",))

                identifier = repository.add_media_proxy_instance(
                    name=ending, server_type="jellyfin", upstream_url="http://upstream.invalid:8096",
                    api_key="fake", listen_host="127.0.0.1", listen_port=18096 + len(ending),
                )
                stored_instance = repository.get_media_proxy_instance(identifier)
                self.assertIsNotNone(stored_instance)
                instance = dict(stored_instance)
                with patch.object(media_proxy.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(upstream), **kw)), \
                        patch.object(media_proxy, "_pin_upstream_target", side_effect=pinned), \
                        patch.object(media_proxy, "_pin_signed_media_target", side_effect=pinned), \
                        patch.object(media_proxy, "_client_is_authorized", new=AsyncMock(return_value=True)), \
                        patch.object(media_proxy, "resolve_guangya_playback", return_value=media_proxy.SignedUrlCacheResult("https://cdn.invalid/fake", False)):
                    app = media_proxy.create_proxy_app(instance)
                    async with app.router.lifespan_context(app):
                        async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://proxy.invalid") as client:
                            playback = await client.post("/Items/item/PlaybackInfo", headers={"X-Emby-Token": "fake-user", "X-Emby-Client": "Jellyfin Web"}, json={})
                        url = urlsplit(playback.json()["MediaSources"][0]["DirectStreamUrl"])
                        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                                 "http_version": "1.1", "method": "GET", "scheme": "http", "path": url.path,
                                 "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
                                 "headers": [], "client": ("127.0.0.1", 12345), "server": ("proxy.invalid", 80)}
                        request_sent = False

                        async def receive():
                            nonlocal request_sent
                            if not request_sent:
                                request_sent = True
                                return {"type": "http.request", "body": b"", "more_body": False}
                            await disconnected.wait()
                            return {"type": "http.disconnect"}

                        async def send(message):
                            if message["type"] == "http.response.body" and message.get("body"):
                                first_body.set()

                        task = asyncio.create_task(app(scope, receive, send))
                        try:
                            await asyncio.wait_for(first_body.wait(), 2)
                            for _ in range(100):
                                records = await asyncio.to_thread(repository.list_media_proxy_playback_records, instance_id=identifier)
                                streams = [row for row in records["items"] if row["route_class"] == "guangya_direct"]
                                if streams:
                                    break
                                await asyncio.sleep(0.005)
                            self.assertEqual(len(streams), 1, "长流未结束时就应可读取流日志")
                            self.assertFalse(task.done())
                            head = streams[0]
                            self.assertEqual(head["status_code"], 206)
                            initial = repository.list_media_proxy_playback_sessions(instance_id=identifier)["items"][0]
                            self.assertEqual((initial["request_count"], initial["error_count"]), (2, 0))
                            await asyncio.sleep(0.06)
                            if ending == "client_disconnect":
                                disconnected.set()
                            else:
                                released.set()
                            if ending == "upstream_error":
                                with self.assertRaises(httpx.ReadError):
                                    await asyncio.wait_for(task, 2)
                            else:
                                await asyncio.wait_for(task, 2)
                            await app.state.playback_record_writer.stop()
                            final_rows = repository.list_media_proxy_playback_records(instance_id=identifier)["items"]
                            final = [row for row in final_rows if row["route_class"] == "guangya_direct"]
                            self.assertEqual(len(final_rows), 2)
                            self.assertEqual(len(final), 1)
                            self.assertEqual(final[0]["id"], head["id"])
                            self.assertEqual(final[0]["total_latency_ms"], head["total_latency_ms"])
                            failed = ending == "upstream_error"
                            self.assertEqual(final[0]["status_code"], 0 if failed else 206)
                            self.assertEqual(final[0]["failure_stage"], "upstream_stream" if failed else "")
                            final_session = repository.list_media_proxy_playback_sessions(instance_id=identifier)["items"][0]
                            self.assertEqual(final_session["request_count"], 2)
                            self.assertEqual(final_session["error_count"], int(failed))
                            self.assertEqual(final_session["success_count"], 1 if failed else 2)
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
