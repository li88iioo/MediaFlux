"""qBittorrent 客户端（基于 Web API，公开标准）。

认证：用户名/密码登录获取 cookie，或使用 API Key（Bearer 头）。
下载目录：支持多条分类目录。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Optional

import requests

from app.logger import get_logger

logger = get_logger(__name__)


def close_qbittorrent_client(client: object | None) -> None:
    """尽力释放短生命周期 qB Client，不让清理异常覆盖业务结果。"""
    if client is None:
        return
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        logger.warning("关闭 qBittorrent HTTP Client 失败 type=%s", type(exc).__name__)


_QB_COMPLETE_STATES = frozenset({
    "uploading",
    "stalledup",
    "queuedup",
    "forcedup",
    "stoppedup",
    "pausedup",
})


def is_qb_torrent_complete(state: str, progress: float) -> bool:
    """仅在 qB 已进入安全做本地入库的上传/停止上传状态时判定完成。"""
    try:
        normalized_progress = max(0.0, min(float(progress or 0), 1.0))
    except (TypeError, ValueError):
        normalized_progress = 0.0
    return (
        normalized_progress >= 1.0
        and str(state or "").strip().casefold() in _QB_COMPLETE_STATES
    )


class QBConnectionTestError(RuntimeError):
    """qBittorrent 只读连通性测试的稳定失败分类。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code or "connection")


class QBTorrentExportError(RuntimeError):
    """qBittorrent 5.x 种子导出的稳定失败分类。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code or "export_failed")


@dataclass(frozen=True)
class TorrentAddResult:
    ok: bool
    failure_code: str = ""
    retryable: bool = False
    task_ids: tuple[str, ...] = ()


@dataclass
class TorrentTask:
    hash: str
    name: str
    progress: float
    state: str
    save_path: str
    content_path: str
    size: int
    downloaded: int
    dlspeed: int
    upspeed: int
    eta: int
    ratio: float
    category: str
    added_on: int


@dataclass
class TorrentFile:
    index: int
    name: str
    size: int
    progress: float


@dataclass
class TransferInfo:
    connection_status: str = "disconnected"
    dl_info_speed: int = 0
    dl_info_data: int = 0
    up_info_speed: int = 0
    up_info_data: int = 0
    dl_rate_limit: int = 0
    up_rate_limit: int = 0
    dht_nodes: int = 0


class QBittorrentClient:
    def __init__(self, url: str, username: str = "", password: str = "",
                 api_key: str = "", timeout: int = 10):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._close_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """幂等释放当前客户端拥有的 requests 连接池。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._session.close()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            # qBittorrent 5.x API Key 用 Bearer 认证（实测 X-API-Key 返回 403）
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def login(self) -> bool:
        """用户名密码登录。使用 API Key 时可跳过。"""
        if self.api_key:
            return True
        try:
            resp = self._session.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
            ok = resp.text.strip() == "Ok."
            if ok:
                logger.info("qBittorrent 登录成功")
            else:
                logger.error("qBittorrent 登录失败: 鉴权未通过")
            return ok
        except requests.RequestException as exc:
            logger.error("qBittorrent 登录异常: type=%s", type(exc).__name__)
            return False

    def _get(self, path: str, params: Optional[dict] = None):
        resp = self._session.get(
            f"{self.url}/api/v2{path}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp

    def _post(self, path: str, data: Optional[dict] = None):
        if not self.api_key:
            self.login()
        resp = self._session.post(
            f"{self.url}/api/v2{path}",
            headers=self._headers(),
            data=data or {},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp

    def add_torrent_detailed(self, urls: str = "", save_path: str = "",
                             category: str = "",
                             torrents: Optional[bytes] = None) -> TorrentAddResult:
        """添加下载任务并返回稳定、无敏感信息的失败分类。"""
        if not self.api_key:
            login_result = self._login_for_add()
            if not login_result.ok:
                return login_result
        data = {"urls": urls}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        files = {
            "torrents": ("upload.torrent", torrents, "application/x-bittorrent")
        } if torrents else None
        try:
            resp = self._session.post(
                f"{self.url}/api/v2/torrents/add",
                headers=self._headers(),
                data=data,
                files=files,
                timeout=self.timeout,
            )
        except requests.ConnectTimeout:
            logger.warning("qB 添加任务失败: 建立连接超时")
            return TorrentAddResult(False, "qb_unavailable", True)
        except requests.ConnectionError:
            logger.warning("qB 添加任务结果未知: 连接中断")
            return TorrentAddResult(False, "qb_outcome_unknown", False)
        except (requests.ReadTimeout, requests.Timeout):
            logger.warning("qB 添加任务结果未知: 请求超时")
            return TorrentAddResult(False, "qb_outcome_unknown", False)
        except requests.RequestException:
            logger.warning("qB 添加任务结果未知: 请求异常")
            return TorrentAddResult(False, "qb_outcome_unknown", False)

        status = int(resp.status_code or 0)
        if status in (401, 403):
            logger.warning("qB 添加任务失败: 鉴权失败")
            return TorrentAddResult(False, "qb_auth_failed", False)
        if status == 429:
            logger.warning("qB 添加任务失败: 请求受限")
            return TorrentAddResult(False, "qb_rate_limited", True)
        if status >= 500:
            logger.warning("qB 添加任务结果未知: 服务端异常 status=%s", status)
            return TorrentAddResult(False, "qb_outcome_unknown", False)
        if status not in (200, 202):
            logger.warning("qB 添加任务失败: 请求被拒绝 status=%s", status)
            return TorrentAddResult(False, "qb_rejected", False)
        if not self._parse_add_result(resp):
            logger.warning("qB 添加任务未确认成功: status=%s", status)
            return TorrentAddResult(False, "qb_rejected", False)
        logger.info("qB 添加任务成功")
        return TorrentAddResult(True, task_ids=self._added_torrent_ids(resp))

    def _login_for_add(self) -> TorrentAddResult:
        """为添加任务登录，并保留可安全重试所需的最小分类。"""
        try:
            resp = self._session.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except (requests.ConnectTimeout, requests.ConnectionError):
            logger.warning("qB 登录失败: 下载器不可用")
            return TorrentAddResult(False, "qb_unavailable", True)
        except (requests.ReadTimeout, requests.Timeout):
            # 登录请求本身不创建下载任务；超时可安全重试。
            logger.warning("qB 登录失败: 下载器响应超时")
            return TorrentAddResult(False, "qb_unavailable", True)
        except requests.RequestException:
            logger.warning("qB 登录结果未知: 请求异常")
            return TorrentAddResult(False, "qb_outcome_unknown", False)

        status = int(resp.status_code or 0)
        if status == 429:
            return TorrentAddResult(False, "qb_rate_limited", True)
        if status >= 500:
            return TorrentAddResult(False, "qb_server_error", True)
        if status in (401, 403) or (resp.text or "").strip() != "Ok.":
            logger.warning("qB 登录失败: 鉴权失败")
            return TorrentAddResult(False, "qb_auth_failed", False)
        return TorrentAddResult(True)

    @staticmethod
    def _parse_add_result(resp) -> bool:
        """解析 /torrents/add 响应，兼容 qB 4.x/5.x。"""
        if resp.status_code not in (200, 202):
            return False
        text = (resp.text or "").strip()
        low = text.lower()
        # qB 4.x：纯文本 "Ok." / "Ok" / "true"
        if low in ("ok.", "ok", "true", ""):
            return True
        # qB 5.x：JSON
        try:
            data = resp.json()
        except (ValueError, AttributeError):
            # 非 JSON 但 2xx，保守视为成功
            return True
        if not isinstance(data, dict):
            return True
        if data.get("failure_count", 0) > 0:
            return False
        return bool(
            data.get("success_count", 0)
            or data.get("pending_count", 0)
            or data.get("added_torrent_ids")
        )

    @staticmethod
    def _added_torrent_ids(resp) -> tuple[str, ...]:
        """读取 qB 5.x 返回的实际 TorrentID；旧版文本响应返回空集合。"""
        try:
            data = resp.json()
        except (ValueError, AttributeError):
            return ()
        if not isinstance(data, dict) or not isinstance(data.get("added_torrent_ids"), list):
            return ()
        task_ids: list[str] = []
        for value in data["added_torrent_ids"]:
            normalized = str(value or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", normalized) and normalized not in task_ids:
                task_ids.append(normalized)
        return tuple(task_ids)

    def list_torrents(self, category: str = "") -> list[TorrentTask]:
        if not self.api_key:
            self.login()
        params = {}
        if category:
            params["category"] = category
        resp = self._get("/torrents/info", params=params).json()
        return [
            TorrentTask(
                hash=t["hash"],
                name=t["name"],
                progress=t.get("progress", 0),
                state=t.get("state", ""),
                save_path=t.get("save_path", ""),
                content_path=t.get("content_path", ""),
                size=t.get("size", 0),
                downloaded=t.get("downloaded", 0),
                dlspeed=t.get("dlspeed", 0),
                upspeed=t.get("upspeed", 0),
                eta=t.get("eta", 0),
                ratio=t.get("ratio", 0),
                category=t.get("category", ""),
                added_on=t.get("added_on", 0),
            )
            for t in resp
        ]

    def get_torrent_files(self, torrent_hash: str) -> list[TorrentFile]:
        """读取 torrent 文件清单，供完成后的本地入库精确定位。"""
        if not self.api_key:
            self.login()
        data = self._get("/torrents/files", params={"hash": torrent_hash}).json()
        return [
            TorrentFile(
                index=int(item.get("index", index)),
                name=str(item.get("name") or ""),
                size=int(item.get("size") or 0),
                progress=float(item.get("progress") or 0),
            )
            for index, item in enumerate(data if isinstance(data, list) else [])
        ]

    def export_torrent(self, torrent_hash: str) -> bytes:
        """通过 qB 5.x Web API 导出现有任务的原始 ``.torrent`` 数据。"""
        normalized_hash = str(torrent_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", normalized_hash):
            raise QBTorrentExportError("invalid_hash")
        if not self.api_key and not self.login():
            raise QBTorrentExportError("authentication")
        headers = self._headers()
        headers["Accept"] = "application/x-bittorrent, application/octet-stream;q=0.9, */*;q=0.1"
        try:
            response = self._session.post(
                f"{self.url}/api/v2/torrents/export",
                headers=headers,
                data={"hash": normalized_hash},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise QBTorrentExportError("timeout") from exc
        except requests.RequestException as exc:
            raise QBTorrentExportError("connection") from exc

        status = int(response.status_code or 0)
        if status in {401, 403}:
            raise QBTorrentExportError("authentication")
        if status == 404:
            raise QBTorrentExportError("not_found")
        if status == 409:
            raise QBTorrentExportError("unavailable")
        if status == 429:
            raise QBTorrentExportError("rate_limited")
        if status >= 500:
            raise QBTorrentExportError("server_error")
        if status < 200 or status >= 300:
            raise QBTorrentExportError("invalid_response")
        payload = bytes(response.content or b"")
        if not payload:
            raise QBTorrentExportError("empty_response")
        if len(payload) > 10 * 1024 * 1024:
            raise QBTorrentExportError("too_large")
        return payload

    def get_transfer_info(self) -> TransferInfo:
        if not self.api_key:
            self.login()
        data = self._get("/transfer/info").json()
        return TransferInfo(
            connection_status=data.get("connection_status", "disconnected"),
            dl_info_speed=data.get("dl_info_speed", 0),
            dl_info_data=data.get("dl_info_data", 0),
            up_info_speed=data.get("up_info_speed", 0),
            up_info_data=data.get("up_info_data", 0),
            dl_rate_limit=data.get("dl_rate_limit", 0),
            up_rate_limit=data.get("up_rate_limit", 0),
            dht_nodes=data.get("dht_nodes", 0),
        )

    @staticmethod
    def _connection_test_response(response: requests.Response) -> str:
        status = int(response.status_code or 0)
        if status in {401, 403}:
            raise QBConnectionTestError("authentication")
        if status == 404:
            raise QBConnectionTestError("not_qb_api")
        if status == 429:
            raise QBConnectionTestError("rate_limited")
        if status >= 500:
            raise QBConnectionTestError("server_error")
        if 300 <= status < 400:
            raise QBConnectionTestError("redirect")
        if status < 200 or status >= 300:
            raise QBConnectionTestError("invalid_response")
        value = response.text.strip()
        if not value or len(value) > 100:
            raise QBConnectionTestError("invalid_response")
        return value

    def test_connection(self) -> dict[str, str]:
        """验证网络、认证和 qB Web API，仅执行只读版本请求。"""
        try:
            auth_mode = "api_key" if self.api_key else "password"
            if not self.api_key:
                login_response = self._session.post(
                    f"{self.url}/api/v2/auth/login",
                    data={"username": self.username, "password": self.password},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if int(login_response.status_code or 0) in {401, 403}:
                    raise QBConnectionTestError("authentication")
                if int(login_response.status_code or 0) == 404:
                    raise QBConnectionTestError("not_qb_api")
                if int(login_response.status_code or 0) == 429:
                    raise QBConnectionTestError("rate_limited")
                if int(login_response.status_code or 0) >= 500:
                    raise QBConnectionTestError("server_error")
                if login_response.text.strip() != "Ok.":
                    raise QBConnectionTestError("authentication")
            versions: dict[str, str] = {}
            for key, path in (("app", "/app/version"), ("webapi", "/app/webapiVersion")):
                response = self._session.get(
                    f"{self.url}/api/v2{path}",
                    headers=self._headers(),
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                versions[key] = self._connection_test_response(response)
            versions["auth_mode"] = auth_mode
            return versions
        except QBConnectionTestError:
            raise
        except requests.Timeout as exc:
            raise QBConnectionTestError("timeout") from exc
        except requests.ConnectionError as exc:
            raise QBConnectionTestError("connection") from exc
        except requests.RequestException as exc:
            raise QBConnectionTestError("connection") from exc

    def get_version(self) -> dict[str, str]:
        return {
            "app": self._get("/app/version").text.strip(),
            "webapi": self._get("/app/webapiVersion").text.strip(),
        }

    def pause_torrents(self, hashes: str) -> bool:
        """暂停/停止任务，优先 qB 5.x stop，回退 4.x pause。"""
        return self._post_with_fallback("/torrents/stop", "/torrents/pause", hashes)

    def resume_torrents(self, hashes: str) -> bool:
        """恢复/开始任务，优先 qB 5.x start，回退 4.x resume。"""
        return self._post_with_fallback("/torrents/start", "/torrents/resume", hashes)

    def delete_torrents(self, hashes: str, delete_files: bool = False) -> bool:
        self._post("/torrents/delete", {
            "hashes": hashes,
            "deleteFiles": "true" if delete_files else "false",
        })
        return True

    def _post_with_fallback(self, primary: str, legacy: str, hashes: str) -> bool:
        try:
            self._post(primary, {"hashes": hashes})
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status not in (404, 405):
                raise
            self._post(legacy, {"hashes": hashes})
        return True

    def get_completion(self, torrent_hash: str) -> bool:
        """查询任务是否完成。"""
        if not self.api_key:
            self.login()
        resp = self._get("/torrents/info", params={"hashes": torrent_hash}).json()
        if not resp:
            return False
        return is_qb_torrent_complete(
            str(resp[0].get("state") or ""),
            resp[0].get("progress", 0),
        )
