"""播放诊断写入器并发关闭/重启后仍能交付后续记录。"""
import asyncio
import threading
import unittest

from app import database as db
from app.repositories import media_proxy as media_proxy_repo
from app.modules.media_proxy_recorder import PlaybackRecordWriter
from tests.support import isolated_test_database


class RecorderLifecycleAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stop_does_not_leave_a_stop_marker_for_next_start(self):
        records = []
        writer = PlaybackRecordWriter(write_record=lambda payload: records.append(dict(payload)))
        await writer.start()
        try:
            self.assertEqual(await asyncio.gather(writer.stop(), writer.stop()), [True, True])
            self.assertEqual(writer.metrics()['pending'], 0)
            await writer.start()
            self.assertTrue(writer.enqueue({'name': 'after-restart', 'status_code': 206}))
            self.assertTrue(await writer.stop())
            self.assertEqual([row['name'] for row in records], ['after-restart'])
        finally:
            await writer.stop()
            writer._drop_queued_for_shutdown()

    async def test_start_waits_until_inflight_stop_finishes_then_accepts_new_records(self):
        entered, release = threading.Event(), threading.Event()
        records = []

        def write(payload):
            if not records:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('synthetic write barrier')
            records.append(dict(payload))

        writer = PlaybackRecordWriter(write_record=write, drain_timeout_seconds=2)
        await writer.start()
        self.assertTrue(writer.enqueue({'name': 'first', 'status_code': 206}))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        stopping = asyncio.create_task(writer.stop())
        await asyncio.sleep(0)
        starting = asyncio.create_task(writer.start())
        try:
            await asyncio.sleep(0)
            self.assertFalse(starting.done())
            self.assertFalse(writer.metrics()['accepting'])
            release.set()
            await asyncio.wait_for(asyncio.gather(stopping, starting), 3)
            self.assertTrue(writer.enqueue({'name': 'second', 'status_code': 206}))
            self.assertTrue(await writer.stop())
            self.assertEqual([row['name'] for row in records], ['first', 'second'])
        finally:
            release.set()
            await asyncio.gather(stopping, starting, return_exceptions=True)
            await writer.stop()
            writer._drop_queued_for_shutdown()

    async def test_repeated_concurrent_shutdown_keeps_all_accepted_records(self):
        records = []
        writer = PlaybackRecordWriter(write_record=lambda payload: records.append(dict(payload)))
        try:
            for index in range(4):
                await writer.start()
                self.assertTrue(writer.enqueue({'index': index, 'status_code': 206}))
                self.assertTrue(all(await asyncio.gather(writer.stop(), writer.stop(), writer.stop())))
                self.assertEqual(writer.metrics()['pending'], 0)
            self.assertEqual([row['index'] for row in records], list(range(4)))
            self.assertEqual(writer.metrics()['written'], 4)
        finally:
            await writer.stop()
            writer._drop_queued_for_shutdown()


    async def test_concurrent_shutdown_restart_persists_records_and_session_history(self):
        with isolated_test_database():
            instance_id = media_proxy_repo.add_media_proxy_instance(
                name='synthetic-recorder', server_type='jellyfin', upstream_url='http://synthetic.invalid',
                api_key='synthetic', listen_host='127.0.0.1', listen_port=18888, enabled=0,
            )
            writer = PlaybackRecordWriter()
            try:
                for index in range(2):
                    await writer.start()
                    self.assertTrue(writer.enqueue({
                        'instance_id': instance_id, 'route_class': 'stream', 'method': 'GET',
                        'status_code': 206, 'source': 'upstream', 'playback_session_key': 'synthetic-session',
                        'media_name': f'Episode {index}',
                    }))
                    self.assertEqual(await asyncio.gather(writer.stop(), writer.stop()), [True, True])
                    self.assertEqual(writer.metrics()['pending'], 0)
            finally:
                await writer.stop()
                writer._drop_queued_for_shutdown()
            db.init_db()
            records = media_proxy_repo.list_media_proxy_playback_records(instance_id=instance_id)
            sessions = media_proxy_repo.list_media_proxy_playback_sessions(instance_id=instance_id)
            self.assertEqual(records['total'], 2)
            self.assertEqual(sessions['total'], 1)
            self.assertEqual(sessions['items'][0]['request_count'], 2)
            self.assertEqual(sessions['items'][0]['success_count'], 2)
