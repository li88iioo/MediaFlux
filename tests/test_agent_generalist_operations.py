"""播放运行时原有实例重建行为。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class MediaProxyRuntimeRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_instance_rebuilds_only_selected_runtime(self) -> None:
        from app.modules import media_proxy

        row = {
            "id": 7,
            "enabled": 1,
            "listen_host": "127.0.0.1",
            "listen_port": 18097,
            "upstream_url": "http://127.0.0.1:8096",
        }
        previous = MagicMock()
        replacement = MagicMock()
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[7] = previous
        with (
            patch("app.repositories.media_proxy.get_media_proxy_instance", return_value=row
            ),
            patch("app.repositories.media_proxy.update_media_proxy_instance"),
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop,
            patch.object(
                manager, "_start_runtime", new=AsyncMock(return_value=replacement)
            ) as start,
        ):
            result = await manager.restart_instance(7)
        self.assertEqual(result, {"restarted": True, "reason": ""})
        stop.assert_awaited_once_with(previous)
        start.assert_awaited_once_with(row)
        self.assertIs(manager._runtimes[7], replacement)
