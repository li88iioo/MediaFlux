"""Telegram 资源站搜索会话与专用异步执行器。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app import config
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import build_indexer_service
from app.logger import get_logger
from app.indexers.downloads import download_indexer_result_public

logger = get_logger(__name__)

_SITE_ERROR_MESSAGES = {
    "timeout": "响应超时",
    "unavailable": "暂不可用",
    "rate_limited": "请求过于频繁",
    "invalid_response": "返回数据异常",
    "response_too_large": "响应内容异常",
    "security_error": "安全校验失败",
}


class TelegramResourceSearchError(RuntimeError):
    """Telegram 资源搜索会话不可用。"""


@dataclass(frozen=True, slots=True)
class _Session:
    session_id: str
    chat_id: str
    user_id: str
    query: str
    items: tuple[dict[str, Any], ...]
    sites: tuple[dict[str, Any], ...]
    created_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Action:
    action_id: str
    session_id: str
    chat_id: str
    user_id: str
    kind: str
    value: Any
    expires_at: float


class TelegramResourceSearchStore:
    """有界、限时、绑定 Telegram 身份的搜索会话。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 900,
        max_sessions: int = 64,
        max_actions: int = 2048,
        clock: Callable[[], float] | None = None,
    ):
        if ttl_seconds <= 0 or max_sessions <= 0 or max_actions <= 0:
            raise ValueError("store limits must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.max_sessions = int(max_sessions)
        self.max_actions = int(max_actions)
        self._clock = clock or time.monotonic
        self._sessions: dict[str, _Session] = {}
        self._actions: dict[str, _Action] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        *,
        chat_id: str,
        user_id: str,
        query: str,
        items: list[dict[str, Any]],
        sites: list[dict[str, Any]],
    ) -> str:
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda item: item.created_at)
                self._delete_session(oldest.session_id)
            session_id = self._token()
            self._sessions[session_id] = _Session(
                session_id=session_id,
                chat_id=str(chat_id),
                user_id=str(user_id),
                query=str(query),
                items=tuple(dict(item) for item in items),
                sites=tuple(dict(site) for site in sites),
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            return session_id

    def snapshot(self, session_id: str, chat_id: str, user_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(str(session_id))
            if session is None:
                raise TelegramResourceSearchError("搜索结果已过期，请重新搜索")
            self._validate_owner(session.chat_id, session.user_id, chat_id, user_id)
            return {
                "session_id": session.session_id,
                "query": session.query,
                "items": [dict(item) for item in session.items],
                "sites": [dict(site) for site in session.sites],
            }

    def create_action(
        self,
        session_id: str,
        chat_id: str,
        user_id: str,
        kind: str,
        value: Any = None,
    ) -> str:
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(str(session_id))
            if session is None:
                raise TelegramResourceSearchError("搜索结果已过期，请重新搜索")
            self._validate_owner(session.chat_id, session.user_id, chat_id, user_id)
            while len(self._actions) >= self.max_actions:
                oldest_id = min(
                    self._actions, key=lambda key: self._actions[key].expires_at
                )
                self._actions.pop(oldest_id, None)
            action_id = self._token()
            self._actions[action_id] = _Action(
                action_id=action_id,
                session_id=session.session_id,
                chat_id=session.chat_id,
                user_id=session.user_id,
                kind=str(kind),
                value=value,
                expires_at=session.expires_at,
            )
            return action_id

    def resolve_action(self, action_id: str, chat_id: str, user_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            action = self._actions.get(str(action_id))
            if action is None:
                raise TelegramResourceSearchError("操作已过期，请重新搜索")
            self._validate_owner(action.chat_id, action.user_id, chat_id, user_id)
            if action.kind == "download":
                self._actions.pop(str(action_id), None)
            return {
                "session_id": action.session_id,
                "kind": action.kind,
                "value": action.value,
            }

    @staticmethod
    def _validate_owner(
        expected_chat: str, expected_user: str, chat_id: str, user_id: str
    ) -> None:
        if str(chat_id) != expected_chat or str(user_id) != expected_user:
            raise TelegramResourceSearchError("该操作不属于当前会话")

    @staticmethod
    def _token() -> str:
        return secrets.token_urlsafe(9).rstrip("=")

    def _delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        stale = [
            action_id
            for action_id, action in self._actions.items()
            if action.session_id == session_id
        ]
        for action_id in stale:
            self._actions.pop(action_id, None)

    def _prune(self, now: float) -> None:
        expired_sessions = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired_sessions:
            self._delete_session(session_id)
        expired_actions = [
            action_id
            for action_id, action in self._actions.items()
            if action.expires_at <= now
        ]
        for action_id in expired_actions:
            self._actions.pop(action_id, None)


class TelegramIndexerWorker:
    """让 TG 搜索与下载解析始终运行在同一个事件循环和专用 service 上。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._service = None
        self._startup_error: BaseException | None = None
        self._stopping = False

    def search(self, query: str) -> dict[str, Any]:
        if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
            raise TelegramResourceSearchError("资源站搜索当前已关闭")
        request = IndexerMediaSearchRequest.create(title=query)

        async def run(service):
            result = await service.search_media(request, None)
            return _search_snapshot(service, result)

        return self._call(run, timeout=130.0)

    def download(
        self,
        result_id: str,
        target: str,
        *,
        chat_id: str = "",
        user_id: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        async def run(service):
            return await download_indexer_result_public(
                service,
                result_id,
                target,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
            )

        return self._call(run, timeout=130.0)

    def _call(self, factory, *, timeout: float):
        self._ensure_started()
        if self._startup_error is not None:
            raise TelegramResourceSearchError("资源站服务启动失败") from self._startup_error
        with self._lock:
            if self._stopping:
                raise TelegramResourceSearchError("资源站服务正在关闭，请稍后重试")
            loop = self._loop
            service = self._service
        if loop is None or service is None:
            raise TelegramResourceSearchError("资源站服务尚未就绪")
        coroutine = factory(service)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TelegramResourceSearchError("资源站请求超时，请稍后重试") from exc

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stopping:
                raise TelegramResourceSearchError("资源站服务正在关闭，请稍后重试")
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="tg-indexer-worker",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise TelegramResourceSearchError("资源站服务启动超时")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            service = build_indexer_service()
            with self._lock:
                self._loop = loop
                self._service = service
        except BaseException as exc:
            with self._lock:
                self._startup_error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        while True:
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            try:
                loop.run_until_complete(service.aclose())
            except Exception as exc:
                # IndexerService 的 close 是可重试的；保持同一 owner loop 和
                # service 存活，下一次 stop 再次触发关闭，不能创建第二套客户端池。
                logger.warning(
                    "关闭 Telegram 资源站运行时失败 type=%s", type(exc).__name__
                )
                continue
            break
        loop.close()
        with self._lock:
            if self._loop is loop:
                self._loop = None
            if self._service is service:
                self._service = None

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            loop = self._loop
            thread = self._thread
            if thread is None:
                self._stopping = False
                return True
            self._stopping = True
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._stopping = False
                return True
        return False


def _search_snapshot(service, result) -> dict[str, Any]:
    items = [item.to_public_dict() for item in result.items]
    counts: dict[str, int] = {}
    for item in items:
        site_id = str(item.get("site_id") or "")
        counts[site_id] = counts.get(site_id, 0) + 1
    errors = {str(error.site_id): str(error.code or "unavailable") for error in result.errors}
    attempted = set(result.sites_attempted)
    succeeded = set(result.sites_succeeded)
    sites = []
    for site_id in service.registry.ids():
        if site_id not in attempted and site_id not in counts:
            continue
        adapter = service.registry.get(site_id)
        code = errors.get(site_id, "")
        if code:
            status = "error"
            message = _SITE_ERROR_MESSAGES.get(code, "检索失败")
        elif site_id in succeeded:
            status = "success" if counts.get(site_id, 0) else "empty"
            message = ""
        else:
            status = "error"
            message = "未返回有效状态"
        sites.append(
            {
                "site_id": site_id,
                "site_name": adapter.site_name,
                "status": status,
                "count": counts.get(site_id, 0),
                "message": message,
            }
        )
    return {
        "query": result.query,
        "items": items,
        "sites": sites,
        "partial": bool(result.partial),
    }


_result_ttl = max(60, min(config.get_int("INDEXER_RESULT_TTL_SECONDS", 600), 3600))
_store = TelegramResourceSearchStore(ttl_seconds=max(30, _result_ttl - 5))
_worker = TelegramIndexerWorker()


def get_telegram_resource_search_store() -> TelegramResourceSearchStore:
    return _store


def get_telegram_indexer_worker() -> TelegramIndexerWorker:
    return _worker


def shutdown_telegram_indexer_worker(timeout: float = 5.0) -> bool:
    return _worker.stop(timeout=timeout)
