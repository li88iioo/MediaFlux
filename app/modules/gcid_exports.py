"""后台生成 GCID 清单，由发起导出的 Web 会话领取临时文件。"""
from __future__ import annotations

import json
import secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO

from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger

logger = get_logger(__name__)


class ExportError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass(eq=False)
class _Export:
    source_dir_id: str
    source_name: str
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    status: str = "running"
    file_count: int = 0
    directory_count: int = 0
    error: str = ""
    artifact: BinaryIO | None = None
    expiry: threading.Timer | None = None
    filename: str = ""

    def snapshot(self) -> dict:
        return {
            "id": self.id, "status": self.status, "source_dir_id": self.source_dir_id,
            "source_name": self.source_name, "file_count": self.file_count,
            "directory_count": self.directory_count, "error": self.error,
        }

    def close(self) -> None:
        if self.expiry is not None:
            self.expiry.cancel()
        if self.artifact is not None:
            self.artifact.close()


class GCIDExports:
    def __init__(self, *, retention_seconds: float = 24 * 3600):
        self._jobs: dict[str, _Export] = {}
        self._lock = threading.Lock()
        self._retention_seconds = retention_seconds

    def current(self, owner: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(owner)
            return job.snapshot() if job is not None else None

    def start(
        self, owner: str, source_id: str, source_name: str,
        client_factory: Callable[[], GuangYaClient],
    ) -> dict:
        with self._lock:
            previous = self._jobs.get(owner)
            if previous and previous.status != "failed":
                if previous.source_dir_id == source_id:
                    return previous.snapshot()
                raise ExportError("已有导出任务，请先下载当前清单", 409)
            if sum(job.status == "running" for job in self._jobs.values()) >= 4:
                raise ExportError("同时导出的目录较多，请稍后重试", 429)
            client = client_factory()
            job = _Export(source_id, source_name)
            try:
                if not client.logged_in:
                    raise ExportError("请先登录光鸭账号", 503)
                self._jobs[owner] = job
                threading.Thread(target=self._generate, args=(owner, job, client),
                                 name="gcid-export", daemon=True).start()
            except Exception:
                if previous is None:
                    self._jobs.pop(owner, None)
                else:
                    self._jobs[owner] = previous
                close_guangya_client(client)
                raise
            if previous is not None:
                previous.close()
            return job.snapshot()

    def _generate(self, owner: str, job: _Export, client: GuangYaClient) -> None:
        def progress(directories: int, files: int) -> None:
            with self._lock:
                if self._jobs.get(owner) is not job:
                    raise InterruptedError("导出任务已停止")
                job.directory_count, job.file_count = directories, files

        artifact = None
        error = ""
        try:
            manifest = client.generate_gcid_json(job.source_dir_id, job.source_name, on_progress=progress)
            artifact = tempfile.TemporaryFile(mode="w+b")
            artifact.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            artifact.flush()
        except ValueError as exc:
            error = str(exc) or "清单生成失败，请重新生成"
        except Exception as exc:
            logger.warning("GCID 后台导出失败 type=%s", type(exc).__name__)
            error = "GCID 清单生成失败，请检查光鸭连接后重试"
        finally:
            close_guangya_client(client)

        with self._lock:
            if self._jobs.get(owner) is not job or error:
                if artifact is not None:
                    artifact.close()
                if self._jobs.get(owner) is not job:
                    return
            job.status = "failed" if error else "ready"
            job.error = error
            if not error:
                job.artifact = artifact
                job.filename = f"{job.source_name or job.source_dir_id}-gcid-{datetime.now():%Y%m%d-%H%M%S}.json"
            job.expiry = threading.Timer(self._retention_seconds, self._expire, args=(owner, job))
            job.expiry.daemon = True
            job.expiry.start()

    def download(self, owner: str, job_id: str) -> tuple[str, bytes]:
        with self._lock:
            job = self._jobs.get(owner)
            if job is None or job.id != job_id:
                raise ExportError("导出文件已清理或已过期", 404)
            if job.status != "ready" or job.artifact is None:
                raise ExportError(job.error or "清单仍在生成", 409)
            job.artifact.seek(0)
            return job.filename, job.artifact.read()

    def acknowledge(self, owner: str, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(owner)
            if job is None or job.id != job_id:
                return
            if job.status != "ready":
                raise ExportError("清单尚未就绪", 409)
            self._jobs.pop(owner).close()

    def _expire(self, owner: str, job: _Export) -> None:
        with self._lock:
            if self._jobs.get(owner) is job:
                self._jobs.pop(owner).close()

    def close(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.close()
            self._jobs.clear()
