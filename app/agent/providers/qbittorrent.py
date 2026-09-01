"""复用现有 QBittorrentClient 的 Provider transport。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app import config
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.clients.qbittorrent import QBittorrentClient, close_qbittorrent_client
from app.modules.qb_control import (
    QBControlConflict,
    QBControlSafetyUnavailable,
    assert_qb_control_allowed,
    qb_control_write_lease,
)

_PROFILE_REF = "configured:qbittorrent"
_PAUSED_STATES = frozenset(
    {
        "paused",
        "pauseddl",
        "pausedup",
        "stopped",
        "stoppeddl",
        "stoppedup",
    }
)
_QB_FAILED_STATES = frozenset({"error", "missingfiles"})
_QB_QUEUED_STATES = frozenset({"queueddl", "queuedup"})
_QB_ACTIVE_STATES = frozenset(
    {
        "allocating",
        "checkingdl",
        "checkingup",
        "checkingresumedata",
        "downloading",
        "forceddl",
        "forcedmetadl",
        "metadl",
        "moving",
        "stalleddl",
    }
)
_QB_COMPLETE_STATES = frozenset(
    {"forcedup", "stalledup", "uploading"}
)


def _torrent_state_kind(state: str, progress: float) -> str:
    normalized = str(state or "").strip().casefold()
    try:
        normalized_progress = max(0.0, min(float(progress or 0), 1.0))
    except (TypeError, ValueError):
        normalized_progress = 0.0
    if normalized in _QB_FAILED_STATES:
        return "failed"
    if normalized_progress >= 1.0 or normalized in _QB_COMPLETE_STATES:
        return "completed"
    if normalized in _PAUSED_STATES:
        return "paused"
    if normalized in _QB_QUEUED_STATES:
        return "queued"
    if normalized in _QB_ACTIVE_STATES:
        return "active"
    return "other"


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
        return [
            ProviderProfileView(
                profile_ref=_PROFILE_REF,
                provider=self.provider,
                label="qBittorrent",
                state="online" if configured else "incomplete",
            )
        ]

    def profile_revision(self, profile_ref: str) -> str:
        values = dict(self._settings())
        return self._profile_revision_from_settings(profile_ref, values)

    @staticmethod
    def _profile_revision_from_settings(
        profile_ref: str, values: Mapping[str, Any]
    ) -> str:
        if profile_ref != _PROFILE_REF:
            raise ProviderGatewayError(
                "qBittorrent profile 不存在", code="provider_not_configured"
            )
        return confirmation_context_fingerprint(
            {
                "profile_ref": _PROFILE_REF,
                "url": str(values.get("QB_URL") or "").strip().rstrip("/"),
                "username": str(values.get("QB_USERNAME") or "").strip(),
                "password": str(values.get("QB_PASSWORD") or ""),
                "api_key": str(values.get("QB_API_KEY") or "").strip(),
            },
            domain="provider-profile-revision",
        )

    def _client(self, profile_ref: str) -> QBittorrentClient:
        return self._client_from_settings(profile_ref, dict(self._settings()))

    @staticmethod
    def _client_from_settings(
        profile_ref: str, values: Mapping[str, Any]
    ) -> QBittorrentClient:
        if profile_ref != _PROFILE_REF:
            raise ProviderGatewayError(
                "qBittorrent profile 不存在", code="provider_not_configured"
            )
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
                query = " ".join(str(arguments.get("query") or "").split()).casefold()
                transfer = client.get_transfer_info() if not query else None
                if query:
                    tasks = [
                        task
                        for task in tasks
                        if query in " ".join(str(task.name or "").split()).casefold()
                    ]
                counts = {
                    "active": 0,
                    "queued": 0,
                    "paused": 0,
                    "failed": 0,
                    "completed": 0,
                    "other": 0,
                }
                for task in tasks:
                    counts[_torrent_state_kind(task.state, task.progress)] += 1
                limit = int(arguments.get("limit", 20))
                items = [
                    {
                        "__object_id": str(task.hash or "").casefold(),
                        "__object_kind": "qb_torrent",
                        "name": str(task.name or ""),
                        "state": str(task.state or ""),
                        "progress_percent": round(float(task.progress or 0) * 100, 2),
                        "size": int(task.size or 0),
                        "downloaded_bytes": int(task.downloaded or 0),
                        "download_speed": int(task.dlspeed or 0),
                        "upload_speed": int(task.upspeed or 0),
                        "eta": int(task.eta or 0),
                        "ratio": float(task.ratio or 0),
                        "category": str(task.category or ""),
                        "added_on": int(task.added_on or 0),
                    }
                    for task in tasks[:limit]
                ]
                summary = (
                    f"qBittorrent 实时队列共 {len(tasks)} 项："
                    f"进行中 {counts['active']}、排队 {counts['queued']}、"
                    f"暂停 {counts['paused']}、异常 {counts['failed']}、"
                    f"已完成 {counts['completed']}、其他 {counts['other']}"
                )
                return ProviderPayload(
                    summary=summary,
                    data={
                        "torrents": items,
                        "count": len(items),
                        "total": len(tasks),
                        "truncated": len(tasks) > len(items),
                        "state_counts": counts,
                        **(
                            {
                                "transfer": {
                                    "connection_status": str(
                                        transfer.connection_status or ""
                                    ),
                                    "download_speed": int(transfer.dl_info_speed or 0),
                                    "upload_speed": int(transfer.up_info_speed or 0),
                                }
                            }
                            if transfer is not None
                            else {}
                        ),
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
                        "progress_percent": round(float(item.progress or 0) * 100, 2),
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
        raise ProviderGatewayError(
            "qBittorrent 操作未实现", code="operation_not_allowed"
        )

    @staticmethod
    def _selected_tasks(
        client: QBittorrentClient, hashes: list[str]
    ) -> tuple[list[Any], list[str]]:
        wanted = {str(value or "").strip().casefold() for value in hashes if value}
        if not wanted or any(
            not re.fullmatch(r"[0-9a-f]{40,64}", value) for value in wanted
        ):
            raise ProviderGatewayError(
                "qBittorrent 任务引用已失效", code="confirmation_stale"
            )
        tasks = client.list_torrents()
        selected = [task for task in tasks if str(task.hash or "").casefold() in wanted]
        found = {str(task.hash or "").casefold() for task in selected}
        missing = sorted(wanted - found)
        return selected, missing

    def preview_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        target_snapshot: dict[str, Any],
    ) -> ProviderPayload:
        if operation not in {
            "qb.torrents.pause",
            "qb.torrents.resume",
            "qb.torrents.delete_task",
        }:
            raise ProviderGatewayError(
                "qBittorrent 写操作未实现", code="operation_not_allowed"
            )
        hashes = [str(value) for value in arguments.get("torrent_refs") or []]
        client: QBittorrentClient | None = None
        try:
            client = self._client(profile_ref)
            selected, missing = self._selected_tasks(client, hashes)
            if missing:
                raise ProviderGatewayError(
                    "部分 qBittorrent 任务已不存在，请重新查询",
                    code="confirmation_stale",
                )
            try:
                assert_qb_control_allowed(
                    hashes,
                    operation={
                        "qb.torrents.pause": "pause",
                        "qb.torrents.resume": "resume",
                        "qb.torrents.delete_task": "delete",
                    }[operation],
                )
            except QBControlSafetyUnavailable as exc:
                raise ProviderGatewayError(
                    str(exc), code="provider_unavailable"
                ) from exc
            except QBControlConflict as exc:
                raise ProviderGatewayError(str(exc), code="provider_conflict") from exc
            targets = [
                {
                    "name": str(task.name or ""),
                    "state": str(task.state or ""),
                    "progress": float(task.progress or 0),
                    "size": int(task.size or 0),
                }
                for task in selected
            ]
            action = {
                "qb.torrents.pause": "暂停",
                "qb.torrents.resume": "恢复",
                "qb.torrents.delete_task": "移除并保留文件",
            }[operation]
            return ProviderPayload(
                summary=f"将{action} {len(targets)} 个 qBittorrent 任务",
                data={
                    "targets": targets,
                    "target_count": len(targets),
                    "delete_files": False,
                },
                source="qbittorrent_api",
            )
        except ProviderGatewayError:
            raise
        except Exception as exc:
            raise ProviderGatewayError(
                "qBittorrent 写前检查失败", code="provider_unavailable"
            ) from exc
        finally:
            close_qbittorrent_client(client)

    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        expected_profile_revision: str,
    ) -> ProviderPayload:
        if operation not in {
            "qb.torrents.pause",
            "qb.torrents.resume",
            "qb.torrents.delete_task",
        }:
            raise ProviderGatewayError(
                "qBittorrent 写操作未实现", code="operation_not_allowed"
            )
        hashes = [str(value) for value in arguments.get("torrent_refs") or []]
        client: QBittorrentClient | None = None
        external_write_possible = False
        try:
            # revision 校验与 client 构造必须共享同一份配置快照，避免校验 A
            # 却因并发配置更新把写请求发往 B。
            settings_snapshot = dict(self._settings())
            current_revision = self._profile_revision_from_settings(
                profile_ref, settings_snapshot
            )
            if (
                not expected_profile_revision
                or current_revision != expected_profile_revision
            ):
                raise ProviderGatewayError(
                    "qBittorrent 配置已变化，请重新预检",
                    code="confirmation_stale",
                )
            try:
                with qb_control_write_lease():
                    client = self._client_from_settings(profile_ref, settings_snapshot)
                    selected, missing = self._selected_tasks(client, hashes)
                    if missing:
                        raise ProviderGatewayError(
                            "部分 qBittorrent 任务已不存在，请重新预检",
                            code="confirmation_stale",
                        )
                    assert_qb_control_allowed(
                        hashes,
                        operation={
                            "qb.torrents.pause": "pause",
                            "qb.torrents.resume": "resume",
                            "qb.torrents.delete_task": "delete",
                        }[operation],
                    )
                    joined = "|".join(hashes)
                    # 从这里开始，客户端可能已经向 qBittorrent 发出真实写请求；
                    # 超时、断连和写后核验失败都必须收束为结果未知。
                    external_write_possible = True
                    if operation == "qb.torrents.pause":
                        accepted = bool(client.pause_torrents(joined))
                        action = "暂停"
                    elif operation == "qb.torrents.resume":
                        accepted = bool(client.resume_torrents(joined))
                        action = "恢复"
                    else:
                        accepted = bool(
                            client.delete_torrents(joined, delete_files=False)
                        )
                        action = "移除"
                    if not accepted:
                        raise ProviderGatewayError(
                            "qBittorrent 未接受写操作",
                            code="provider_write_failed",
                            external_write_possible=True,
                        )
                    after, after_missing = self._selected_tasks(client, hashes)
            except QBControlSafetyUnavailable as exc:
                raise ProviderGatewayError(
                    str(exc),
                    code="provider_unavailable",
                    external_write_possible=external_write_possible,
                ) from exc
            except QBControlConflict as exc:
                raise ProviderGatewayError(
                    str(exc),
                    code="provider_conflict",
                    external_write_possible=external_write_possible,
                ) from exc
            if operation == "qb.torrents.delete_task":
                verification = (
                    "verified" if len(after_missing) == len(hashes) else "pending"
                )
            elif after_missing:
                verification = "partial"
            elif operation == "qb.torrents.pause":
                verification = (
                    "verified"
                    if all(
                        str(task.state or "").casefold() in _PAUSED_STATES
                        for task in after
                    )
                    else "pending"
                )
            else:
                verification = (
                    "verified"
                    if all(
                        str(task.state or "").casefold() not in _PAUSED_STATES
                        for task in after
                    )
                    else "pending"
                )
            return ProviderPayload(
                summary=f"qBittorrent 已接受 {len(selected)} 个任务的{action}操作",
                data={
                    "affected": len(selected),
                    "accepted": True,
                    "delete_files": False,
                    "verification": verification,
                    "observed_count": len(after),
                },
                source="qbittorrent_api",
            )
        except ProviderGatewayError:
            raise
        except Exception as exc:
            raise ProviderGatewayError(
                "qBittorrent 写操作失败",
                code="provider_write_failed",
                external_write_possible=external_write_possible,
            ) from exc
        finally:
            close_qbittorrent_client(client)
