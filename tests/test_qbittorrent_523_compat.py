"""qBittorrent v5.2.3 Web API 与 BitTorrent v2 兼容契约。"""
from __future__ import annotations

import base64
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult
from app.modules.download_dispatcher import (
    _submit_qb,
    normalize_download_url,
    parse_torrent_metadata,
    request_key,
    request_keys,
    torrent_download_input,
    torrent_identity,
)
from app.modules.download_tracker import DownloadTracker


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if not isinstance(self._payload, (dict, list)):
            raise ValueError("not json")
        return self._payload


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, dict):
        return b"d" + b"".join(
            _bencode(key) + _bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


class QBittorrent523IdentityTests(unittest.TestCase):
    def test_connection_test_uses_api_key_without_password_login(self):
        client = QBittorrentClient("http://qb.invalid", api_key="token")
        client._session.post = Mock()
        client._session.get = Mock(side_effect=[
            _Response(200, "5.2.3"),
            _Response(200, "2.11.4"),
        ])

        result = client.test_connection()

        self.assertEqual(result, {
            "app": "5.2.3",
            "webapi": "2.11.4",
            "auth_mode": "api_key",
        })
        client._session.post.assert_not_called()
        for call in client._session.get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer token")

    def test_connection_test_logs_in_before_password_version_checks(self):
        client = QBittorrentClient(
            "http://qb.invalid", username="admin", password="secret"
        )
        client._session.post = Mock(return_value=_Response(200, "Ok."))
        client._session.get = Mock(side_effect=[
            _Response(200, "5.2.3"),
            _Response(200, "2.11.4"),
        ])

        result = client.test_connection()

        self.assertEqual(result["auth_mode"], "password")
        client._session.post.assert_called_once()
        self.assertEqual(client._session.get.call_count, 2)

    def test_add_response_exposes_actual_qb_torrent_ids(self):
        task_id = "a" * 40
        client = QBittorrentClient("http://qb.invalid", api_key="token")
        client._session.post = Mock(return_value=_Response(200, {
            "success_count": 1,
            "failure_count": 0,
            "pending_count": 0,
            "added_torrent_ids": [task_id.upper(), task_id],
        }))

        result = client.add_torrent_detailed(torrents=b"torrent")

        self.assertEqual(result, TorrentAddResult(True, task_ids=(task_id,)))

    def test_submit_prefers_qb_returned_task_id_over_precomputed_identity(self):
        returned_id = "b" * 40
        row = {
            "kind": "magnet",
            "source_value": f"magnet:?xt=urn:btih:{'a' * 40}",
            "torrent_data": None,
        }
        values = {
            "QB_URL": "http://qb.invalid",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "token",
            "TG_QB_CATEGORY": "",
            "TG_QB_SAVE_PATH": "",
        }
        with patch(
            "app.modules.download_dispatcher.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.modules.download_dispatcher.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(True, task_ids=(returned_id,)),
        ):
            result = _submit_qb(row)

        self.assertEqual(result["task_id"], returned_id)
        self.assertEqual(result["task_ids"], [returned_id])

    def test_pure_v2_torrent_uses_qb_sha256_torrent_id_and_valid_btmh(self):
        info = {
            b"file tree": {},
            b"meta version": 2,
            b"name": b"pure-v2",
            b"piece length": 16384,
        }
        raw_info = _bencode(info)
        torrent = _bencode({b"info": info})
        full_v2_hash = hashlib.sha256(raw_info).hexdigest()

        name, torrent_id = parse_torrent_metadata(torrent)
        item = torrent_download_input("pure-v2.torrent", torrent)

        self.assertEqual(name, "pure-v2")
        self.assertEqual(torrent_id, full_v2_hash[:40])
        self.assertIn(f"xt=urn:btmh:1220{full_v2_hash}", item.source_value)
        self.assertEqual(
            request_key(item),
            request_key(normalize_download_url(item.source_value)),
        )
        self.assertEqual(torrent_identity({
            "kind": "torrent", "torrent_data": torrent, "source_value": item.source_value,
        }), full_v2_hash[:40])

    def test_hybrid_torrent_keeps_v1_identity(self):
        info = {
            b"file tree": {},
            b"meta version": 2,
            b"name": b"hybrid",
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
        raw_info = _bencode(info)
        torrent = _bencode({b"info": info})
        expected_v1 = hashlib.sha1(raw_info).hexdigest()

        _name, torrent_id = parse_torrent_metadata(torrent)
        item = torrent_download_input("hybrid.torrent", torrent)

        self.assertEqual(torrent_id, expected_v1)
        self.assertIn(f"xt=urn:btih:{expected_v1}", item.source_value)

        expected_v2 = hashlib.sha256(raw_info).hexdigest()
        btmh_item = normalize_download_url(
            f"magnet:?xt=urn:btmh:1220{expected_v2}"
        )
        self.assertTrue(set(request_keys(item)) & set(request_keys(btmh_item)))

    def test_btmh_and_base32_btih_are_normalized_to_qb_torrent_ids(self):
        v2_hash = "12" * 32
        btmh = f"magnet:?xt=urn:btmh:1220{v2_hash}"
        self.assertEqual(normalize_download_url(btmh).kind, "magnet")
        self.assertEqual(torrent_identity({
            "kind": "magnet", "source_value": btmh, "torrent_data": None,
        }), v2_hash[:40])

        v1_bytes = bytes.fromhex("34" * 20)
        base32_hash = base64.b32encode(v1_bytes).decode("ascii")
        btih = f"magnet:?xt=urn:btih:{base32_hash}"
        self.assertEqual(torrent_identity({
            "kind": "magnet", "source_value": btih, "torrent_data": None,
        }), v1_bytes.hex())


class QBittorrent523CompletionTests(unittest.TestCase):
    @staticmethod
    def _row() -> dict:
        return {
            "id": 1,
            "title": "Demo",
            "qb_status": "downloading",
            "gy_status": "",
            "qb_task_id": "a" * 40,
            "qb_task_missing_since": "",
            "organize_started": 0,
        }

    def test_transitional_full_progress_states_do_not_start_local_import(self):
        for state in ("checkingDL", "checkingUP", "checkingResumeData", "moving"):
            with self.subTest(state=state):
                tracker = DownloadTracker()
                task = SimpleNamespace(
                    hash="a" * 40,
                    name="Demo",
                    progress=1.0,
                    state=state,
                    content_path="/downloads/Demo",
                )
                with patch.object(tracker, "_update_backend_log"), patch.object(
                    tracker, "_start_local_import"
                ) as start_import, patch.object(
                    tracker, "_notify_completion"
                ), patch(
                    "app.modules.download_tracker.db.update_download_request_and_sync_media_admission"
                ) as update:
                    tracker._update_request(
                        self._row(), [task], [], qb_available=True, gy_available=False,
                    )

                self.assertEqual(update.call_args.kwargs["qb_status"], "downloading")
                start_import.assert_not_called()

    def test_completed_upload_state_starts_local_import(self):
        tracker = DownloadTracker()
        task = SimpleNamespace(
            hash="a" * 40,
            name="Demo",
            progress=1.0,
            state="stalledUP",
            content_path="/downloads/Demo",
        )
        with patch.object(tracker, "_update_backend_log"), patch.object(
            tracker, "_start_local_import"
        ) as start_import, patch.object(
            tracker, "_notify_completion"
        ), patch(
            "app.modules.download_tracker.db.update_download_request_and_sync_media_admission"
        ) as update:
            tracker._update_request(
                self._row(), [task], [], qb_available=True, gy_available=False,
            )

        self.assertEqual(update.call_args.kwargs["qb_status"], "completed")
        start_import.assert_called_once_with(self._row(), task)


if __name__ == "__main__":
    unittest.main()
