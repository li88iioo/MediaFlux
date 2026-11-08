"""会话工作区：正常 Python/Shell，业务调用直达 SDK，隔离由操作系统执行。"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
import subprocess
from functools import lru_cache
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agent.errors import ToolPipelineError
from app.agent.public_safety import public_tool_label, sanitize_public_text
from app.modules.process_lock import CrossProcessLock
from app.runtime_paths import get_runtime_paths
from app.sdk import METHODS, WRITE_METHODS, call_owner, describe_methods, describe_namespaces
from app.sensitive_data import redact_sensitive_text

from .capabilities import KernelToolSpec, ToolEffect
from .projection import ToolOutcome, _json_safe
from .state_types import SessionBusyError, _PublicationUnprotected
from .task_state import identity, observation_patch, read_observations
from .tool_calls import PipelineResult

if TYPE_CHECKING:
    from .effects import EffectExecutor
    from .pipeline import ToolPipeline
    from .tool_calls import ToolCallContext

RUN_CODE_TOOL = "agent.run_code"
_TASK_METHODS = frozenset({"task", "start_task", "revise_task", "checkpoint_task", "cancel_task"})
_CHILD = Path(__file__).with_name("sandbox_child.py")
_CLIENT = Path(__file__).with_name("workspace_client.py")
_SKILLS = Path(__file__).parent.parent / "skills"
_OUTPUT_CHARS = 16_000
_REQUEST_BYTES = 4 * 1024 * 1024
_REQUEST_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")


def _sdk_activity(identity, method, arguments, *, status="running", result=None, error=""):
    if method.startswith(("session.", "skills.")):
        return None
    parts = [str(arguments[key]) for key in ("query", "title", "keyword")
             if isinstance(arguments.get(key), str) and arguments[key].strip()][:1]
    if error:
        parts.append(error)
    elif isinstance(result, dict):
        summary = result.get("summary") or result.get("message")
        if isinstance(summary, str):
            parts.append(summary)
        else:
            items = result.get("items")
            if isinstance(items, list):
                parts.append(f"本次返回 {len(items)} 项")
        if result.get("partial") is True:
            parts.append("结果不完整")
        if result.get("has_more") is True:
            parts.append("还有后续页")
    return {"id": identity, "kind": "tool", "method": method,
            "label": public_tool_label(method), "status": status,
            "summary": sanitize_public_text("；".join(parts), limit=300)}


@lru_cache(maxsize=1)
def _workspace_probe() -> str:
    """固定无业务命令验证实际系统权限，不把已安装依赖误当可执行；进程内只探测一次。"""
    try:
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(_CHILD), "--check"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"无法启动代码工作区隔离：{exc}"
    if result.returncode:
        guidance = "请检查系统用户命名空间及沙箱权限。"
        if os.environ.get("MEDIAFLUX_CONTAINER") == "1":
            guidance = (
                "Docker 部署请核对 MediaFlux 服务的 security_opt 包含 "
                "seccomp=unconfined 和 apparmor=unconfined，然后重新创建容器；"
                "仅重启旧容器不会更新权限。若已配置仍失败，请检查宿主机用户命名空间支持。"
            )
        return "代码工作区隔离不可用。" + guidance + "原始错误：" + result.stderr.strip()[:600]
    return ""


def workspace_unavailable_reason() -> str:
    if sys.platform != "linux":
        return "代码工作区当前需要 Linux 系统隔离支持"
    if shutil.which("bwrap") is None:
        return "缺少 bubblewrap；请安装系统包 bubblewrap 和 libseccomp2"
    return _workspace_probe()


def sandbox_supported() -> bool:
    return not workspace_unavailable_reason()


def _adopt_legacy_work(directory: int) -> None:
    """仅升级旧 root 工作区时接回历史工作文件，不跟随软链接或接管外部硬链接。"""
    with os.scandir(directory) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                nested = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    _adopt_legacy_work(nested)
                finally:
                    os.close(nested)
            elif info.st_uid != os.geteuid():
                if info.st_nlink > 1:
                    raise ValueError("cannot adopt a hard-linked workspace file")
                os.chown(entry.name, os.geteuid(), os.getegid(), dir_fd=directory, follow_symlinks=False)
    os.fchown(directory, os.geteuid(), os.getegid())


def _prepare_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    # 只做旧文件数据迁移，不保留旧执行后端；work/output 原路径和内容均保持。
    if (root / ".version").exists():
        directory = os.open(root / "work", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _adopt_legacy_work(directory)
        finally:
            os.close(directory)
        for entry in root.iterdir():
            if entry.name in {"work", "output"}:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for directory in ("work", "output", "run", "opt/mf"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    # 协议文件不回放；上一轮 worker 已全部退出，只有宿主可以更新运行文件。
    for entry in (root / "run").iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    content = {"mf.py": _CLIENT.read_text(),
               "mf-methods.json": json.dumps(describe_methods(), ensure_ascii=False),
               "cli": '#!/bin/sh\nexec /usr/local/bin/python -m mf "$@"\n'}
    for name, value in content.items():
        path = root / "opt/mf" / name
        path.unlink(missing_ok=True)
        path.write_text(value)
        path.chmod(0o555 if name == "cli" else 0o444)


class _SDKRequests:
    def __init__(self, context: ToolCallContext, root: Path, effects: EffectExecutor):
        from app.agent.kernel.ports.resource_presentation import ResourcePresentation

        self.context = context
        self.root = root
        self.calls: list[dict[str, Any]] = []
        self.completed_reads: list[tuple[str, dict[str, Any]]] = []
        self.patches = []
        self.pending: dict[str, asyncio.Task] = {}
        self.accepted: set[str] = set()
        self.effects = effects
        from .session_service import SessionService
        self.session_service = SessionService(effects, context)
        self.presentation = ResourcePresentation(context, effects, self.session_service.snapshots)
        self.proposal: PipelineResult | None = None
        self.approval_ready = asyncio.Event()
        self.effect_lock = asyncio.Lock()
        self.task_budget_admitted = False
        self.uid = None
        for name, mode in (("mf-requests", 0o700), ("mf-responses", 0o755)):
            directory = root / "run" / name
            directory.mkdir(mode=mode)
            directory.chmod(mode)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.requests_fd = os.open(root / "run/mf-requests", flags)
        self.responses_fd = os.open(root / "run/mf-responses", flags)

    def read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=self.requests_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != self.uid or info.st_size > _REQUEST_BYTES):
                raise ValueError("无效的 SDK 请求文件，须为不超过 4 MiB 的独立普通文件")
            data = stream.read(_REQUEST_BYTES + 1)
        if len(data) > _REQUEST_BYTES:
            raise ValueError("SDK 请求超过 4 MiB")
        return json.loads(data)

    def write(self, name, data):
        temporary = "." + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.responses_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), 0o444)
            os.replace(temporary, name, src_dir_fd=self.responses_fd, dst_dir_fd=self.responses_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.responses_fd)
            except FileNotFoundError:
                pass

    def check_publication(self, conn):
        """内部经验落库与原会话发布权在同一事务内校验。"""
        from .persistence import SQLiteKernelStore

        self.context.cancellation.raise_if_cancelled()
        store = self.effects.state_store
        if not isinstance(store, SQLiteKernelStore):
            raise RuntimeError("长期技能保存需要持久会话仓储")
        store.require_current_in_transaction(self.context.lease, conn)

    def task_observations(self):
        pending, self.patches = self.patches, []
        return replace(pending[0], payload={"observations": {
            key: fact for patch in pending for key, fact in patch.payload["observations"].items()
        }}) if pending else None

    async def session_call(self, method, arguments):
        if method in _TASK_METHODS:
            patch = self.task_observations()
            if patch is not None:
                if self.context.task_control is None:
                    raise ToolPipelineError("当前执行未绑定持久任务运行时", code="task_runtime_unavailable")
                task = await self.context.task_control(patch)
                self.context = replace(self.context, current_task=task)
                self.session_service.context = self.context
        result = await getattr(self.session_service, method)(**arguments)
        self.context = self.session_service.context
        if method in {"start_task", "revise_task", "cancel_task"}:
            self.task_budget_admitted = False
        return result

    async def dispatch(self, name):
        method = ""
        arguments = {}
        activity_id = f"{self.context.call_id}:{name.removesuffix('.json')}"
        current_task = self.context.current_task
        observation: dict[str, Any] = {}
        checkpoint: dict[str, Any] | None = None
        stopped = threading.Event()
        loop = asyncio.get_running_loop()

        def session_call(method, arguments):
            return asyncio.run_coroutine_threadsafe(self.session_call(method, arguments), loop).result()

        def cancelled():
            return stopped.is_set() or self.context.cancellation.cancelled

        def remember(result):
            nonlocal checkpoint
            if cancelled():
                return
            snapshot = {**observation, "completed_at": datetime.now(timezone.utc).isoformat(),
                        "partial": True, "result": result}
            if not cancelled():
                checkpoint = snapshot

        try:
            request = self.read(name)
            method = request["method"]
            function = METHODS.get(method)
            if function is None:
                raise ValueError(f"未知 SDK 方法：{method}；可运行 mf --help 查看接口")
            arguments = request["arguments"]
            if not isinstance(arguments, dict):
                raise TypeError("arguments 必须是对象")
            bound = inspect.signature(function).bind(**arguments)
            bound.apply_defaults()
            self.context.cancellation.raise_if_cancelled()
            current_task = self.context.current_task
            if not method.startswith("session.") and method not in {"skills.save", "skills.restore"} and self.context.task_budget is not None:
                async with self.effect_lock:
                    if not self.task_budget_admitted:
                        await self.context.task_budget()
                        self.task_budget_admitted = True
            if self.context.read_only and method in WRITE_METHODS | {"skills.save", "skills.restore"}:
                raise ToolPipelineError("工作区行动接力仅允许只读 SDK。", code="read_scope_required")
            activity = _sdk_activity(activity_id, method, arguments)
            if activity:
                await self.context.report_progress({"activity": activity})
            if method in WRITE_METHODS:
                async with self.effect_lock:
                    if self.proposal is not None:
                        raise ToolPipelineError("已有操作等待确认；本次调用未执行", code="approval_pending")
                    prepared = await self.effects.prepare(method,
                        {key: value for key, value in bound.arguments.items() if value is not Ellipsis},
                        context=self.context)
                    if prepared.effect_plan is not None and prepared.tool.metadata.get("confirmation_required") is False:
                        # 免确认写仍复用冻结、一次领取和持久回执；不终止脚本或生成用户卡片。
                        try:
                            prepared = await self.effects.execute_confirmed(prepared.effect_plan.plan_id, context=self.context)
                        finally:
                            await self.effects.state_store.commit(self.context.lease, updates=(_PublicationUnprotected("", None),))
                        prepared = replace(prepared, effect_plan=None)
                    if prepared.effect_plan is not None:
                        self.proposal = prepared
                        self.calls.append({"method": method, "ok": True, "status": "approval_required"})
                        await self.context.report_progress({"activity": _sdk_activity(
                            activity_id, method, arguments, status="approval_required")})
                        self.approval_ready.set()
                        return
                    result = dict(prepared.outcome.public_content)
            else:
                operation_id = await self.presentation.begin(method)
                snapshot = await self.presentation.snapshot() if method == "resources.present" and bound.arguments.get("result_ids") else None
                def invoke():
                    # 技能保存/恢复是内部写入，也不能当作只读观察归档。
                    if method not in {"skills.save", "skills.restore"}:
                        observation.update(method=method,
                                           arguments={key: value for key, value in bound.arguments.items() if value is not Ellipsis},
                                           called_at=datetime.now(timezone.utc).isoformat())
                    try:
                        return function(**bound.arguments)
                    finally:
                        if observation:
                            observation["completed_at"] = datetime.now(timezone.utc).isoformat()
                with call_owner(self.context.owner, publication_check=self.check_publication,
                                cancelled=cancelled, read_checkpoint=remember, delivery_route=self.context.delivery_route,
                                resource_candidates=snapshot, session_call=session_call):
                    result = await asyncio.to_thread(invoke)
                self.context.cancellation.raise_if_cancelled()
                await self.presentation.observed(method, bound.arguments, result, operation_id=operation_id, snapshot=snapshot)
            self.context.cancellation.raise_if_cancelled()
            status = str(result.get("status") or ("partial" if result.get("partial") else "success"))
            ok = result.get("ok") is not False
            activity = _sdk_activity(activity_id, method, arguments,
                status="failed" if not ok else "partial" if result.get("partial") or status == "partial" else "success",
                result=result)
            if activity:
                await self.context.report_progress({"activity": activity})
            encoded = json.dumps({**observation, "result": result}, ensure_ascii=False, allow_nan=False).encode()
            call = {"method": method, "ok": ok, "status": status}
            if observation:
                call.update({key: result[key] for key in ("cached", "partial", "truncated", "has_more") if key in result})
            self.calls.append(call)
            observations = {} if method.startswith("session.") or identity(current_task) != identity(self.context.current_task) else read_observations(
                current_task, method, bound.arguments,
                {**result, "ok": ok, "status": status, "summary": result.get("summary") or method},
            )
            patch = observation_patch(
                current_task, owner=self.context.owner, session_id=self.context.session_id,
                tool=method, call_id=f"{self.context.call_id}:{len(self.calls)}", observations=observations,
            )
            if patch is not None:
                self.patches.append(patch)
        except asyncio.CancelledError:
            stopped.set()
            if checkpoint is not None:
                # 已完成的批内读取仍归档到同一工作区；在途/取消后结果不补发。
                self.write(name, json.dumps(checkpoint, ensure_ascii=False, allow_nan=False).encode())
                call = {"method": method, "ok": checkpoint["result"].get("ok") is not False, "status": "partial", "called_at": observation["called_at"]}
                self.calls.append(call)
                self.completed_reads.append((name, call))
            raise
        except Exception as exc:
            if observation:
                patch = observation_patch(current_task, owner=self.context.owner, session_id=self.context.session_id,
                    tool=method, call_id=f"{self.context.call_id}:{len(self.calls) + 1}",
                    observations={} if method.startswith("session.") or identity(current_task) != identity(self.context.current_task) else read_observations(current_task, method, observation["arguments"], None))
                if patch is not None:
                    self.patches.append(patch)
            message = redact_sensitive_text(getattr(exc, "public_message", None) or str(exc))
            code = str(getattr(exc, "code", None) or type(exc).__name__)
            call = {"method": str(method), "ok": False, "status": code}
            self.calls.append(call)
            encoded = json.dumps({**observation, "error": {"code": code, "message": message}}, ensure_ascii=False).encode()
            if method in METHODS:
                activity = _sdk_activity(activity_id, method, arguments if isinstance(arguments, dict) else {},
                                         status="failed", error=message)
                if activity:
                    await self.context.report_progress({"activity": activity})
        if observation:
            call["called_at"] = observation["called_at"]
        self.write(name, encoded)
        if observation:
            self.completed_reads.append((name, call))

    async def poll(self):
        while True:
            for name, task in tuple(self.pending.items()):
                if task.done():
                    del self.pending[name]
                    task.result()  # 响应写入等基础设施错误交给父端立即停止执行。
            with os.scandir(self.requests_fd) as entries:
                for entry in entries:
                    if len(self.pending) >= 4:
                        break
                    name = entry.name
                    if not _REQUEST_NAME.fullmatch(name) or name in self.accepted:
                        continue
                    try:
                        os.stat(name, dir_fd=self.responses_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        # 完成只释放并发槽；请求ID在本次执行内不重放，也不与客户端抢删文件。
                        self.accepted.add(name)
                        self.pending[name] = asyncio.create_task(self.dispatch(name))
            await asyncio.sleep(0.020)

    async def close(self):
        pending = tuple(self.pending.values())
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*pending, return_exceptions=True)
        error = next((result for result in results if isinstance(result, OSError)), None)
        try:
            for name, call in self.completed_reads:
                target = self.root / "output" / (uuid.uuid4().hex + ".sdk.json")
                try:
                    # worker 已退出；搬移同一普通文件，客户端的单链接校验保持不变。
                    os.replace(name, target, src_dir_fd=self.responses_fd)
                    call["response_path"] = "/output/" + target.name
                except OSError as exc:
                    error = error or exc
        finally:
            os.close(self.requests_fd)
            os.close(self.responses_fd)
        return error


def _output_preview(path: Path) -> tuple[str, bool]:
    """完整输出保留在文件中；有界读取首尾，避免日志末尾的结论或异常被隐藏。"""
    with path.open(encoding="utf-8", errors="replace") as output:
        head = output.read(_OUTPUT_CHARS + 1)
        if len(head) <= _OUTPUT_CHARS:
            return head, False
        size = os.fstat(output.fileno()).st_size
        gap = f"\n[中间输出已省略；完整文件 {size} bytes]\n"
        tail_chars = (_OUTPUT_CHARS - len(gap)) // 2
        # UTF-8 每字符最多4字节，多读一个字符后按字符截尾，避免切断多字节字符。
        output.seek(max(0, size - (tail_chars + 1) * 4))
        tail = output.read()[-tail_chars:]
        return head[:_OUTPUT_CHARS - len(gap) - len(tail)] + gap + tail, True


async def _execute(root: Path, arguments: dict, context: ToolCallContext, effects: EffectExecutor) -> tuple[dict, _SDKRequests]:
    await asyncio.to_thread(_prepare_workspace, root)
    context.cancellation.raise_if_cancelled()
    language = arguments.get("language", "python")
    script = root / "run" / ("code.py" if language == "python" else "code.sh")
    script.write_text(arguments["code"])
    script.chmod(0o444)
    command = (["/usr/local/bin/python", "-P", "-B", "-u", "-c",
                "import mf, runpy; runpy.run_path('/run/code.py', init_globals={'mf': mf}, run_name='__main__')"]
               if language == "python" else ["/bin/sh", "/run/code.sh"])
    rpc = _SDKRequests(context, root, effects)
    run_id = uuid.uuid4().hex
    paths = {stream: root / "output" / f"{run_id}.{stream}" for stream in ("stdout", "stderr")}
    timeout = arguments.get("timeout_seconds", 120)
    process = None
    io_error = None
    finished = stopped = poller = approval = None
    try:
        try:
            with paths["stdout"].open("wb") as stdout, paths["stderr"].open("wb") as stderr:
                paths["stdout"].chmod(0o644)
                paths["stderr"].chmod(0o644)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-S", "-B", str(_CHILD),
                    "--root", str(root), "--timeout", str(timeout), "--", *command,
                    stdin=asyncio.subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    env={"MF_WORKSPACE_PARENT_PID": str(os.getpid())}, start_new_session=True,
                )
                rpc.uid = os.geteuid()
                poller = asyncio.create_task(rpc.poll())
                finished = asyncio.create_task(process.wait())
                stopped = asyncio.create_task(context.cancellation.wait())
                approval = asyncio.create_task(rpc.approval_ready.wait())
                await asyncio.wait((finished, stopped, poller, approval), return_when=asyncio.FIRST_COMPLETED)
                context.cancellation.raise_if_cancelled()
                if poller.done():
                    poller.result()
        except OSError as exc:
            io_error = exc
        finally:
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            archive_error = await rpc.close()
            io_error = io_error or archive_error
            for task in (finished, stopped, approval):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (finished, stopped, approval) if t is not None), return_exceptions=True)
            for name in ("mf-requests", "mf-responses"):
                shutil.rmtree(root / "run" / name)
        context.cancellation.raise_if_cancelled()
        data: dict[str, Any] = {"exit_code": process.returncode if process else None, "timed_out": bool(process and process.returncode == 124),
                "working_directory": "/work", "sdk_calls": rpc.calls}
        for stream, path in paths.items():
            preview, truncated, output_path = "", False, None
            try:
                preview, truncated = _output_preview(path)
                output_path = "/output/" + path.name
            except OSError as exc:
                io_error = io_error or exc
            data[stream] = preview
            data[f"{stream}_truncated"] = truncated
            data[f"{stream}_path"] = output_path
        if io_error is not None:
            data["error"] = {"code": "workspace_io_error", "message": redact_sensitive_text(str(io_error))}
        return data, rpc
    except BaseException:
        if rpc.proposal is not None:
            assert rpc.proposal.effect_plan is not None
            await effects.discard_unpublished_effect(plan=rpc.proposal.effect_plan, context=context)
        raise


def _workspace_location(owner: str, session_id: str) -> tuple[Path, CrossProcessLock]:
    scope = hashlib.sha256(json.dumps([owner, session_id]).encode()).hexdigest()
    paths = get_runtime_paths()
    return (paths.runtime_dir / "agent-workspaces" / scope,
            CrossProcessLock("agent-workspace-" + scope, directory=paths.data_dir))


def workspace_output_available(owner: str, session_id: str, path: str) -> bool:
    """模型请求和调用准入共用同一会话工作区的输出文件检查。"""
    workspace, _ = _workspace_location(owner, session_id)
    source = workspace / path.lstrip("/")
    return source.is_file() and not source.is_symlink()


def clear_workspace(*, owner: str, session_id: str) -> None:
    """取消后等当前命令退出再清理；使用与执行相同的锁，不删除仍在使用的工作区。"""
    root, lock = _workspace_location(owner, session_id)
    deadline = time.monotonic() + 5
    while not lock.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise SessionBusyError("工作区命令仍在停止，请稍后重试")
        time.sleep(0.02)
    try:
        if root.exists():
            shutil.rmtree(root)
    finally:
        lock.release()


async def run_code(pipeline: ToolPipeline, arguments: dict[str, Any], context: ToolCallContext) -> PipelineResult:
    unavailable = workspace_unavailable_reason()
    if unavailable:
        raise ToolPipelineError(unavailable, code="workspace_unavailable")
    root, lock = _workspace_location(context.owner, context.session_id)
    if not lock.acquire(blocking=False):
        raise ToolPipelineError("同一会话的工作区仍有命令执行中", code="workspace_busy")
    try:
        data, rpc = await _execute(root, arguments, context, pipeline)
    finally:
        lock.release()
    # 与终端相同，exit_code/stderr 是下一步观察；捕获过的错误不变成永久任务失败。
    proposal = rpc.proposal
    if data.get("error") and proposal is not None:
        assert proposal.effect_plan is not None
        await pipeline.discard_unpublished_effect(plan=proposal.effect_plan, context=context)
        proposal = None
    content = {"ok": not data.get("error"), "status": "failed" if data.get("error") else "approval_required" if proposal else "success",
               "summary": data["error"]["message"] if data.get("error") else "脚本已在确认处结束，确认后执行预览中的操作；后续命令可读取原工作文件。" if proposal else f"命令已退出（退出码 {data['exit_code']}）",
               "data": data}
    if proposal is not None:
        assert proposal.effect_plan is not None
        content["approval"] = proposal.effect_plan.public_dict()
    if rpc.presentation.changed:
        content["candidate_view"] = {key: value for key, value in rpc.presentation.view.items() if key != "generation"} if rpc.presentation.view else None
    patch = rpc.task_observations()
    # 路径属于隔离工作区，是后续命令的真实输入；不能套用云盘 DTO 的 opaque-ref 隐藏规则。
    safe = _json_safe(content)
    # 卡片经公开事件交付；模型已有SDK展示回执，不重复读取UI列表/按钮/选择凭证。
    model = {key: value for key, value in safe.items() if key != "candidate_view"}
    if "candidate_view" in safe:
        model["candidate_view"] = {"ref": safe["candidate_view"]["ref"]} if safe["candidate_view"] else None
    outcome = ToolOutcome(model_content=json.dumps(model, ensure_ascii=False), public_content=safe)
    return PipelineResult(tool=pipeline.catalog.get(RUN_CODE_TOOL), arguments=arguments,
                          outcome=outcome, task_patch=patch,
                          effect_plan=proposal.effect_plan if proposal else None)


def run_code_tool(pipeline: ToolPipeline) -> KernelToolSpec:
    async def read(arguments, context):
        return await run_code(pipeline, arguments, context)

    namespaces = "\n".join(
        f"mf.{name}: {description}"
        for name, description in describe_namespaces().items()
    )
    skills = "\n".join(
        f"/skills/{path.parent.name}/SKILL.md：{next(line for line in path.read_text().splitlines() if line.strip()).lstrip('# ')}"
        for path in sorted(_SKILLS.glob("*/SKILL.md"))
    )
    return KernelToolSpec(
        name=RUN_CODE_TOOL, domain="agent", effect=ToolEffect.READ,
        description=("执行普通 Python 或 Shell。Python 已提供 mf 业务 SDK；每次调用启动新进程，其他变量与 import 不跨调用保留；同一会话的 /work 文件持久保留。"
                     "可 import、读写文件、运行子进程。"
                     "业务接口用 import mf；可用的 /skills 文档见下方目录。"
                     "mf <模块> --help 查看方法用途；调用陌生方法前用 help(mf.<模块>.<方法>) 读取真实签名、参数和返回字段，不猜接口。"
                     "业务数据由 SDK 提供，完整结果可保存 JSON 再分页/关联；工作区无主机凭据和正式媒体访问。"
                     "返回 stdout/stderr/exit_code；非零退出码需检查错误并修正。长输出仅展示头尾并标记中间省略，全文读取返回的文件。"
                     "sdk_calls 的 response_path 是 JSON 归档：成功响应在 result，失败在 error（code/message）；"
                     "json.load(open(response_path))[\"result\"] 与 SDK 返回值相同。called_at 是 SDK 调用时间，不是数据源刷新时间。"
                     "任务资料保存到 /work，已验证的长期经验用 mf.skills 保存；远端写入遵循确认授权。"
                     f"\nSDK 范围：\n{namespaces}\n已安装流程文档：\n{skills}"),
        input_schema={"type": "object", "required": ["code"], "additionalProperties": False, "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 64_000},
            "language": {"type": "string", "enum": ["python", "shell"], "default": "python"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
        }}, read=read,
        examples=("读取全部收藏并核对每部缺集，保存结果继续查资源", "在工作区汇总、过滤、去重并保存调查资料"),
        metadata={"source_kind": "agent_workspace", "freshness": "derived", "parallel_read": False},
    )
