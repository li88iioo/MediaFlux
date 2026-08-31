"""复用现有 QBittorrentClient 的 Provider transport。"""
from __future__ import annotations

from typing import Any

from app import config
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.clients.qbittorrent import QBittorrentClient, close_qbittorrent_client

_PROFILE_REF = "configured:qbittorrent"


class QBittorrentProviderTransport:
    provider = "qbittorrent"

    @staticmethod
    def _settings() -> dict[str, str]:
        return config.get_many(("QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY"))

    def profiles(self) -> list[ProviderProfileView]:
        values = self._settings()
        configured = bool(
            str(values.get("QB_URL") or "").strip()
            and (
                str(values.get("QB_API_KEY") or "").strip()
                or (
                    str(values.get("QB_USERNAME") or "").strip()
                    and str(values.get("QB_PASSWORD") or "")
                )
            )
        )
        return [ProviderProfileView(
            profile_ref=_PROFILE_REF,
            provider=self.provider,
            label="qBittorrent",
            state="online" if configured else "incomplete",
        )]

    def _client(self, profile_ref: str) -> QBittorrentClient:
        if profile_ref != _PROFILE_REF:
            raise ProviderGatewayError(
                "qBittorrent profile 不存在", code="provider_not_configured"
            )
        values = self._settings()
        url = str(values.get("QB_URL") or "").strip()
        username = str(values.get("QB_USERNAME") or "").strip()
        password = str(values.get("QB_PASSWORD") or "")
        api_key = str(values.get("QB_API_KEY") or "").strip()
        if not url or (not api_key and not (username and password)):
            raise ProviderGatewayError(
                "qBittorrent 尚未配置", code="provider_not_configured"
            )
        return QBittorrentClient(
            url=url,
            username=username,
            password=password,
            api_key=api_key,
            timeout=10,
        )

    def execute_read(
        self, profile_ref: str, operation: str, arguments: dict[str, Any]
    ) -> ProviderPayload:
        client: QBittorrentClient | None = None
        try:
            client = self._client(profile_ref)
            if operation == "qb.app.version":
                if not client.api_key and not client.login():
                    raise ProviderGatewayError(
                        "qBittorrent 鉴权失败", code="provider_auth_failed"
                    )
                versions = client.get_version()
                return ProviderPayload(
                    summary="qBittorrent 版本信息已读取",
                    data={"version": versions},
                    source="qbittorrent_api",
                )
            if operation == "qb.transfer.info":
                info = client.get_transfer_info()
                return ProviderPayload(
                    summary="qBittorrent 传输状态已读取",
                    data={
                        "transfer": {
                            "connection_status": str(info.connection_status or ""),
                            "download_speed": int(info.dl_info_speed or 0),
                            "upload_speed": int(info.up_info_speed or 0),
                            "downloaded_bytes_total": int(info.dl_info_data or 0),
                            "uploaded_bytes_total": int(info.up_info_data or 0),
                            "download_rate_limit": int(info.dl_rate_limit or 0),
                            "upload_rate_limit": int(info.up_rate_limit or 0),
                            "dht_nodes": int(info.dht_nodes or 0),
                        }
                    },
                    source="qbittorrent_api",
                )
            if operation == "qb.torrents.info":
                tasks = client.list_torrents(str(arguments.get("category") or ""))
                limit = int(arguments.get("limit", 20))
                items = [
                    {
                        "__object_id": str(task.hash or "").casefold(),
                        "__object_kind": "qb_torrent",
                        "name": str(task.name or ""),
                        "state": str(task.state or ""),
                        "progress": float(task.progress or 0),
                        "size": int(task.size or 0),
                        "downloaded": int(task.downloaded or 0),
                        "download_speed": int(task.dlspeed or 0),
                        "upload_speed": int(task.upspeed or 0),
                        "eta": int(task.eta or 0),
                        "ratio": float(task.ratio or 0),
                        "category": str(task.category or ""),
                        "added_on": int(task.added_on or 0),
                    }
                    for task in tasks[:limit]
                ]
                return ProviderPayload(
                    summary=f"qBittorrent 返回 {len(items)} 个下载任务",
                    data={
                        "torrents": items,
                        "count": len(items),
                        "total": len(tasks),
                        "truncated": len(tasks) > len(items),
                    },
                    source="qbittorrent_api",
                )
            if operation == "qb.torrents.files":
                files = client.get_torrent_files(str(arguments["torrent_ref"]))
                limit = int(arguments.get("limit", 50))
                items = [
                    {
                        "index": int(item.index),
                        "name": str(item.name or ""),
                        "size": int(item.size or 0),
                        "progress": float(item.progress or 0),
                    }
                    for item in files[:limit]
                ]
                return ProviderPayload(
                    summary=f"qBittorrent 返回 {len(items)} 个任务文件",
                    data={
                        "files": items,
                        "count": len(items),
                        "total": len(files),
                        "truncated": len(files) > len(items),
                    },
                    source="qbittorrent_api",
                )
        except ProviderGatewayError:
            raise
        except Exception as exc:
            raise ProviderGatewayError(
                "qBittorrent 当前不可用", code="provider_unavailable"
            ) from exc
        finally:
            close_qbittorrent_client(client)
        raise ProviderGatewayError("qBittorrent 操作未实现", code="operation_not_allowed")
