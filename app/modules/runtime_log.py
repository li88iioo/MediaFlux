"""固定应用日志文件的安全、有界 tail 读取。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
from threading import Lock
from typing import Iterable

from app.logger import (
    LOG_DIR,
    clear_runtime_log,
    normalize_telebot_polling_error,
    redact_sensitive_text,
)

APP_LOG = Path(LOG_DIR) / "app.log"
_MAX_STREAM_READ_BYTES = 512 * 1024
_MAX_STREAM_EVENTS = 4096
_CURSOR_CHECKPOINT_BYTES = 128
_CURSOR_HEAD_CHECKPOINT_BYTES = 128
_DISCARDING_CHECKPOINT_PREFIX = "discard:"
_TRUNCATED_LINE_NOTICE = "--- 单条日志超过显示上限，内容已截断 ---"
_MAX_SUPPRESSION_LOOKBACK_BYTES = 64 * 1024
_LOG_HEADER_RE = re.compile(
    r"^(?P<prefix>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| "
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\| (?P<logger>[^|]+?) \| )"
    r"(?P<message>.*)$"
)
_GENERATION_LOCK = Lock()
_generation = 0


@dataclass(frozen=True)
class RuntimeLogEvent:
    line: str
    offset: int
    checkpoint: str = ""


@dataclass(frozen=True)
class RuntimeLogChunk:
    events: tuple[RuntimeLogEvent, ...]
    offset: int
    stream_id: str
    reset_reason: str = ""
    reset_offset: int = 0
    checkpoint: str = ""
    reset_checkpoint: str = ""
    generation: int = 0


def log_generation() -> int:
    """返回当前进程内日志代次；每次成功清空后单调递增。"""
    with _GENERATION_LOCK:
        return _generation


def clear_logs() -> tuple[int, int, str, str]:
    """截断持久化日志并返回不会跳过清空后写入的零游标基线。"""
    global _generation
    with _GENERATION_LOCK:
        clear_runtime_log(APP_LOG)
        _generation += 1
        generation = _generation
        try:
            stream_id = _stream_id_from_stat(APP_LOG.stat())
        except OSError:
            stream_id = ""
    # handler 释放截断锁后即可出现清空后的新写入，因此不能把随后采样到的
    # 文件大小作为基线；固定从 0 读取才能保证这些新日志不会被永久跳过。
    return generation, 0, stream_id, ""


def _fold_runtime_lines(lines: Iterable[str], *, suppressed: bool = False) -> tuple[list[str], bool]:
    """折叠历史 TeleBot traceback，并规范化可保留的轮询摘要。"""
    output: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        match = _LOG_HEADER_RE.match(line)
        if match:
            logger_name = match.group("logger").strip()
            message = match.group("message")
            suppressed = logger_name.casefold() == "telebot" and message.startswith("Exception traceback:")
            if suppressed:
                continue
            normalized = normalize_telebot_polling_error(message) if logger_name.casefold() == "telebot" else None
            line = f"{match.group('prefix')}{normalized}" if normalized is not None else line
            output.append(redact_sensitive_text(line))
            continue
        if not suppressed:
            output.append(redact_sensitive_text(line))
    return output, suppressed


def _suppressed_before_offset(handle, offset: int) -> bool | None:
    """有界向前定位最近结构化日志头；找不到时返回未知。"""
    position = max(0, int(offset))
    floor = max(0, position - _MAX_SUPPRESSION_LOOKBACK_BYTES)
    collected = b""
    while position > floor:
        chunk_size = min(8192, position - floor)
        position -= chunk_size
        handle.seek(position)
        collected = handle.read(chunk_size) + collected
        text = collected.decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            match = _LOG_HEADER_RE.match(line)
            if not match:
                continue
            return (
                match.group("logger").strip().casefold() == "telebot"
                and match.group("message").startswith("Exception traceback:")
            )
    return None


def _stream_id_from_stat(stat_result: os.stat_result) -> str:
    """返回不随追加写入变化、但能识别常规文件替换的流身份。"""
    device = int(getattr(stat_result, "st_dev", 0) or 0)
    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    if inode:
        return f"{device:x}:{inode:x}"
    # 部分 Windows 文件系统不提供 file index；creation time 仅作为降级身份。
    created_ns = int(getattr(stat_result, "st_ctime_ns", 0) or 0)
    return f"{device:x}:0:{created_ns:x}"

def _checkpoint_digest(offset: int, tail: bytes, head: bytes = b"") -> str:
    if int(offset) <= 0:
        return ""
    digest = hashlib.sha256()
    digest.update(str(int(offset)).encode("ascii"))
    digest.update(b":head:")
    digest.update(head[:_CURSOR_HEAD_CHECKPOINT_BYTES])
    digest.update(b":tail:")
    digest.update(tail[-_CURSOR_CHECKPOINT_BYTES:])
    return digest.hexdigest()


def _head_sample(handle, offset: int) -> bytes:
    handle.seek(0)
    return handle.read(min(max(0, int(offset)), _CURSOR_HEAD_CHECKPOINT_BYTES))


def _tail_before_offset(handle, offset: int) -> bytes:
    cursor = max(0, int(offset))
    start = max(0, cursor - _CURSOR_CHECKPOINT_BYTES)
    handle.seek(start)
    return handle.read(cursor - start)


def _checkpoint_at(handle, offset: int) -> str:
    cursor = max(0, int(offset))
    return _checkpoint_digest(
        cursor, _tail_before_offset(handle, cursor), _head_sample(handle, cursor)
    )


def _decode_cursor_checkpoint(value: str) -> tuple[str, bool]:
    checkpoint = str(value or "").strip().lower()
    if checkpoint.startswith(_DISCARDING_CHECKPOINT_PREFIX):
        return checkpoint[len(_DISCARDING_CHECKPOINT_PREFIX):], True
    return checkpoint, False


def _discarding_checkpoint(checkpoint: str) -> str:
    return f"{_DISCARDING_CHECKPOINT_PREFIX}{checkpoint}"


def _advance_tail(tail: bytearray, payload: bytes) -> None:
    tail.extend(payload)
    if len(tail) > _CURSOR_CHECKPOINT_BYTES:
        del tail[:-_CURSOR_CHECKPOINT_BYTES]


def _truncated_line_chunk(
    handle, offset: int, stream_id: str, generation: int,
) -> RuntimeLogChunk:
    checkpoint = _discarding_checkpoint(_checkpoint_at(handle, offset))
    event = RuntimeLogEvent(_TRUNCATED_LINE_NOTICE, offset, checkpoint)
    return RuntimeLogChunk(
        (event,), offset, stream_id, "line_truncated", offset, checkpoint, checkpoint,
        generation,
    )


def _continue_discarding_line_chunk(
    handle, offset: int, stream_id: str, generation: int,
) -> RuntimeLogChunk:
    checkpoint = _discarding_checkpoint(_checkpoint_at(handle, offset))
    return RuntimeLogChunk(
        (), offset, stream_id, checkpoint=checkpoint, generation=generation,
    )

def log_identity() -> str:
    try:
        return _stream_id_from_stat(APP_LOG.stat())
    except OSError:
        return ""


def read_last_lines(limit: int = 200) -> list[str]:
    count = max(1, min(int(limit or 200), 1000))
    if not APP_LOG.exists():
        return []
    result: deque[str] = deque(maxlen=count)
    suppressed = False
    with APP_LOG.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            visible, suppressed = _fold_runtime_lines((raw_line,), suppressed=suppressed)
            result.extend(visible)
    return list(result)


def log_snapshot() -> tuple[int, int]:
    try:
        stat = APP_LOG.stat()
    except OSError:
        return 0, 0
    return int(stat.st_mtime_ns), int(stat.st_size)


def read_stream_chunk(
    offset: int,
    *,
    expected_stream_id: str = "",
    expected_checkpoint: str = "",
    expected_generation: int | None = None,
    max_bytes: int = _MAX_STREAM_READ_BYTES,
    max_events: int = _MAX_STREAM_EVENTS,
) -> RuntimeLogChunk:
    """读取完整日志行，并用代次、流身份、检查点和事件数限制 tail。"""
    generation = log_generation()
    requested = max(0, int(offset or 0))
    if expected_generation is not None and int(expected_generation) != generation:
        requested = 0
        expected_stream_id = ""
        expected_checkpoint = ""
        generation_reset = True
    else:
        generation_reset = False
    expected = str(expected_stream_id or "").strip()
    expected_cursor_checkpoint, discarding_truncated_line = _decode_cursor_checkpoint(
        expected_checkpoint
    )
    limit = max(4096, min(int(max_bytes or _MAX_STREAM_READ_BYTES), 4 * 1024 * 1024))
    event_limit = max(1, min(int(max_events or _MAX_STREAM_EVENTS), 8192))

    try:
        handle = APP_LOG.open("rb")
    except OSError:
        reason = "cleared" if generation_reset else (
            "rotated" if requested or expected or expected_cursor_checkpoint else ""
        )
        return RuntimeLogChunk((), 0, "", reason, 0, generation=generation)

    with handle:
        stat = os.fstat(handle.fileno())
        size = int(stat.st_size)
        stream_id = _stream_id_from_stat(stat)
        identity_changed = bool(expected and expected != stream_id)
        truncated = size < requested
        checkpoint_changed = False
        if (
            expected_cursor_checkpoint
            and requested > 0
            and not identity_changed
            and not truncated
        ):
            actual_checkpoint = _checkpoint_at(handle, requested)
            checkpoint_changed = not hmac.compare_digest(
                expected_cursor_checkpoint, actual_checkpoint
            )

        rotated = identity_changed or truncated or checkpoint_changed
        if rotated or generation_reset:
            discarding_truncated_line = False
        base = 0 if rotated or generation_reset else requested
        start = requested if discarding_truncated_line else max(base, size - limit)
        if generation_reset:
            reset_reason = "cleared"
        elif rotated:
            reset_reason = "rotated"
        elif start != requested:
            reset_reason = "tail_rebase"
        else:
            reset_reason = ""

        remaining_limit = limit
        if start > 0:
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                handle.seek(start)
                fragment = handle.readline(remaining_limit)
                start += len(fragment)
                remaining_limit -= len(fragment)
                if not fragment.endswith(b"\n"):
                    # 首次识别时只提示一次；后续轮询通过 checkpoint 中的状态
                    # 有界丢弃同一物理行，直到遇到换行后再恢复正常交付。
                    if discarding_truncated_line:
                        return _continue_discarding_line_chunk(
                            handle, start, stream_id, generation
                        )
                    return _truncated_line_chunk(handle, start, stream_id, generation)
                if discarding_truncated_line:
                    discarding_truncated_line = False
                else:
                    reset_reason = reset_reason or "tail_rebase"

        if remaining_limit <= 0:
            checkpoint = _checkpoint_at(handle, start)
            return RuntimeLogChunk(
                (), start, stream_id, reset_reason, start, checkpoint, checkpoint,
                generation,
            )

        handle.seek(start)
        payload = handle.read(remaining_limit)
        if not payload:
            checkpoint = _checkpoint_at(handle, start)
            return RuntimeLogChunk(
                (), start, stream_id, reset_reason, start, checkpoint, checkpoint,
                generation,
            )

        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            if len(payload) >= limit or start + len(payload) < size:
                advanced = min(size, start + len(payload))
                return _truncated_line_chunk(handle, advanced, stream_id, generation)
            checkpoint = _checkpoint_at(handle, start)
            return RuntimeLogChunk(
                (), start, stream_id, reset_reason, start, checkpoint, checkpoint,
                generation,
            )

        complete = payload[:last_newline + 1]
        if complete.count(b"\n") > event_limit:
            boundary = len(complete) - 1
            for _ in range(event_limit):
                boundary = complete.rfind(b"\n", 0, boundary)
                if boundary < 0:
                    break
            if boundary >= 0:
                drop = boundary + 1
                start += drop
                complete = complete[drop:]
                reset_reason = reset_reason or "tail_rebase"

        suppressed_state = False if start == 0 else _suppressed_before_offset(handle, start)
        # 有界回看仍找不到结构化日志头时，宁可隐藏到下一个日志头，也不能把
        # 超长 TeleBot traceback 的续行作为普通日志泄露给浏览器。
        suppressed = True if suppressed_state is None else suppressed_state
        prefix_tail = _tail_before_offset(handle, start)
        file_head_sample = _head_sample(handle, start + len(complete))
        reset_checkpoint = _checkpoint_digest(
            start, prefix_tail, file_head_sample[:min(start, _CURSOR_HEAD_CHECKPOINT_BYTES)]
        )

    cursor = start
    rolling_tail = bytearray(prefix_tail)
    events: list[RuntimeLogEvent] = []
    for physical_line in io.BytesIO(complete):
        cursor += len(physical_line)
        _advance_tail(rolling_tail, physical_line)
        checkpoint = _checkpoint_digest(
            cursor,
            bytes(rolling_tail),
            file_head_sample[:min(cursor, _CURSOR_HEAD_CHECKPOINT_BYTES)],
        )
        decoded = physical_line.decode("utf-8", errors="replace")
        visible, suppressed = _fold_runtime_lines((decoded,), suppressed=suppressed)
        events.extend(
            RuntimeLogEvent(line=line, offset=cursor, checkpoint=checkpoint)
            for line in visible
        )
    checkpoint = _checkpoint_digest(
        cursor,
        bytes(rolling_tail),
        file_head_sample[:min(cursor, _CURSOR_HEAD_CHECKPOINT_BYTES)],
    )
    return RuntimeLogChunk(
        tuple(events), cursor, stream_id, reset_reason, start,
        checkpoint, reset_checkpoint, generation,
    )


def read_from_offset(
    offset: int,
    *,
    expected_stream_id: str = "",
    max_bytes: int = _MAX_STREAM_READ_BYTES,
) -> tuple[list[str], int, bool]:
    """兼容旧调用方：返回可见行、下一游标和是否发生重定位。"""
    chunk = read_stream_chunk(
        offset,
        expected_stream_id=expected_stream_id,
        max_bytes=max_bytes,
    )
    return [event.line for event in chunk.events], chunk.offset, bool(chunk.reset_reason)
