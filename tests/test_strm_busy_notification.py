from __future__ import annotations

import threading
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.bot import handlers
from app.bot.progress import TelegramProgress
from app.modules.scheduler import STRMScheduler
from app.modules.strm_notifications import sync_notice_lines
from app.notifier import TelegramSendResult, render_event
from tests.support import isolated_test_database

_BUSY_DETAIL = "已有 STRM 同步正在运行，本次未启动，请等待结果。"


@pytest.fixture
def sync_runtime():
    with isolated_test_database(), ExitStack() as stack:
        task_lock = threading.Lock()
        assert task_lock.acquire(blocking=False)
        bot = Mock(spec=["edit_message_text", "edit_message_reply_markup", "send_message", "delete_message"])
        bot.edit_message_text.return_value = True
        scheduler = STRMScheduler()
        stack.enter_context(patch.object(handlers, "_bot", bot))
        stack.enter_context(patch.object(handlers, "_task_lock", task_lock))
        stack.enter_context(patch.object(handlers, "_sync_running", True))
        stack.enter_context(patch("app.modules.scheduler.get_scheduler", return_value=scheduler))
        send_event = stack.enter_context(patch.object(
            handlers, "send_event_result", return_value=TelegramSendResult(ok=True),
        ))
        try:
            yield SimpleNamespace(
                bot=bot, scheduler=scheduler, task_lock=task_lock, send_event=send_event,
            )
        finally:
            if task_lock.locked():
                task_lock.release()


def _progress_handle(runtime, with_progress):
    if not with_progress:
        return None
    return TelegramProgress(
        runtime.bot,
        SimpleNamespace(types=SimpleNamespace()),
        "123",
        "STRM 同步",
        source_message=SimpleNamespace(chat=SimpleNamespace(id="123"), message_id=10),
        mode="edit",
        message_id=20,
    )


def _terminal_text(runtime, progress):
    assert not runtime.task_lock.locked()
    assert handlers._sync_running is False
    runtime.bot.send_message.assert_not_called()
    runtime.bot.delete_message.assert_not_called()
    if progress is None:
        runtime.bot.edit_message_text.assert_not_called()
        runtime.send_event.assert_called_once()
        assert runtime.send_event.call_args.kwargs == {"chat_id": "123"}
        return render_event(runtime.send_event.call_args.args[0])

    runtime.send_event.assert_not_called()
    assert progress.finished_event.is_set()
    edits = runtime.bot.edit_message_text.call_args_list
    assert len(edits) == 2
    confirmation, terminal = edits
    assert confirmation.args[1:] == ("123", 10)
    assert confirmation.kwargs["reply_markup"] is None
    assert terminal.args[1:] == ("123", 20)
    # 真实句柄只能结束一次，不因 busy 终态额外发消息或重复交付。
    assert progress.finish("不应重复交付") is False
    assert runtime.bot.edit_message_text.call_count == 2
    return terminal.args[0]


@pytest.mark.parametrize("with_progress", [False, True], ids=["without-progress", "with-progress"])
def test_real_busy_finishes_once_without_start_queue_or_retry(sync_runtime, with_progress):
    runtime = sync_runtime
    scheduler = runtime.scheduler
    progress = _progress_handle(runtime, with_progress)
    assert scheduler._run_lock.acquire(blocking=False)
    try:
        with patch.object(scheduler, "run_blocking", wraps=scheduler.run_blocking) as run, \
                patch.object(scheduler, "_execute_locked") as execute, \
                patch.object(scheduler, "trigger") as trigger, \
                patch.object(scheduler, "_queue_organize_trigger") as queue, \
                patch.object(scheduler, "_schedule_persisted_change_queue") as persisted_queue:
            handlers._do_sync("123", progress)

        run.assert_called_once()
        assert run.call_args.args == ("telegram",)
        assert run.call_args.kwargs["sync_mode"] == "full"
        assert callable(run.call_args.kwargs["on_progress"])
        execute.assert_not_called()
        trigger.assert_not_called()
        queue.assert_not_called()
        persisted_queue.assert_not_called()
        assert scheduler._pending_organize_options is None
        assert scheduler._pending_thread is None
        assert scheduler._worker is None
        # 只释放本次 Telegram 互斥锁，不能释放正在执行的 STRM 锁。
        assert scheduler._run_lock.acquire(blocking=False) is False
        text = _terminal_text(runtime, progress)
        assert "STRM 同步未启动" in text
        assert _BUSY_DETAIL in text
        assert "STRM 同步失败" not in text
        assert "错误原因" not in text
        assert "❌" not in text
        if progress is None:
            event = runtime.send_event.call_args.args[0]
            assert event.title == "STRM 同步未启动"
            assert event.lines == (_BUSY_DETAIL,)
            assert not event.fields
        else:
            confirmation = runtime.bot.edit_message_text.call_args_list[0].args[0]
            assert confirmation == f"<b>STRM 同步未启动</b>\n{_BUSY_DETAIL}"
    finally:
        scheduler._run_lock.release()


@pytest.mark.parametrize("with_progress", [False, True], ids=["without-progress", "with-progress"])
def test_real_start_failure_remains_failure(sync_runtime, with_progress):
    runtime = sync_runtime
    progress = _progress_handle(runtime, with_progress)
    runtime.scheduler._stop_event.set()
    with patch.object(runtime.scheduler, "run_blocking", wraps=runtime.scheduler.run_blocking) as run:
        handlers._do_sync("123", progress)

    run.assert_called_once()
    assert runtime.scheduler._run_lock.acquire(blocking=False)
    runtime.scheduler._run_lock.release()
    text = _terminal_text(runtime, progress)
    assert "❌ STRM 同步失败" in text
    assert "STRM 调度器正在停止" in text
    assert _BUSY_DETAIL not in text


@pytest.mark.parametrize("with_progress", [False, True], ids=["without-progress", "with-progress"])
@pytest.mark.parametrize("error", ["STRM 同步任务正在运行：执行失败", None], ids=["not-exact-busy", "missing-error"])
def test_other_errors_are_not_misclassified_as_busy(sync_runtime, with_progress, error):
    runtime = sync_runtime
    progress = _progress_handle(runtime, with_progress)
    result = {"ok": False}
    if error is not None:
        result["error"] = error
    with patch.object(runtime.scheduler, "run_blocking", return_value=result) as run:
        handlers._do_sync("123", progress)

    run.assert_called_once()
    text = _terminal_text(runtime, progress)
    assert "❌ STRM 同步失败" in text
    assert (error or "未知错误") in text
    assert _BUSY_DETAIL not in text
    if progress is not None:
        confirmation = runtime.bot.edit_message_text.call_args_list[0].args[0]
        detail = error or "请检查配置后重新发起。"
        assert confirmation == f"<b>STRM 同步未启动</b>\n{detail}"


@pytest.mark.parametrize("with_progress", [False, True], ids=["without-progress", "with-progress"])
@pytest.mark.parametrize("error", ["执行异常", "STRM 同步任务正在运行"], ids=["runtime-error", "busy-text-exception"])
def test_exceptions_remain_failures_even_when_text_matches_busy(sync_runtime, with_progress, error):
    runtime = sync_runtime
    progress = _progress_handle(runtime, with_progress)
    with patch.object(runtime.scheduler, "run_blocking", side_effect=RuntimeError(error)) as run, \
            patch.object(handlers.logger, "error") as log_error:
        handlers._do_sync("123", progress)

    run.assert_called_once()
    log_error.assert_called_once()
    assert str(log_error.call_args.args[1]) == error
    text = _terminal_text(runtime, progress)
    assert "❌ STRM 同步失败" in text
    assert error in text
    assert _BUSY_DETAIL not in text
    if progress is not None:
        confirmation = runtime.bot.edit_message_text.call_args_list[0].args[0]
        assert "STRM 同步失败" in confirmation
        assert "执行过程中发生异常" in confirmation


def test_summary_uses_shared_cleanup_notice_without_rewriting():
    stats = {"clean_skipped": True}
    notice = ("共享清理保护说明", "共享排队说明")
    with patch.object(handlers, "sync_notice_lines", return_value=notice) as shared_notice:
        event = handlers._strm_summary_event({"partial": True}, stats, 0)
    shared_notice.assert_called_once_with(stats)
    assert event.lines == notice


@pytest.mark.parametrize("queued", [False, True], ids=["not-queued", "queued"])
def test_cleanup_summary_describes_round_guard_and_only_observed_queue(queued):
    stats = {"clean_skipped": True}
    if queued:
        stats["organize_followup_queued"] = True
    event = handlers._strm_summary_event({"partial": True}, stats, 0)
    assert event.lines == sync_notice_lines(stats)
    lines: list[str] = []
    for line in event.lines:
        assert isinstance(line, str)
        lines.append(line)
    text = "\n".join(lines)
    assert "暂停本轮删除型清理" in text
    assert "来源退役" in text
    assert "空目录" in text
    assert "对应来源" not in text
    assert ("已排队" in text) is queued


def test_complete_summary_has_no_cleanup_guard_or_queue_promise():
    stats = {"organize_followup_queued": True}
    event = handlers._strm_summary_event({}, stats, 0)
    assert event.lines == sync_notice_lines(stats) == ()
