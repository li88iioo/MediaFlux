"""空目录清理反馈的纯函数、按钮生命周期与安全纯文本契约（不访问 HTTP/DB）。"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app/static/js/organize.js"


def _section(source: str, start: str, end: str) -> str:
    offset = source.index(start)
    return source[offset:source.index(end, offset)]


@unittest.skipUnless(shutil.which("node"), "Node.js 不可用")
class OrganizeEmptyCleanupUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SCRIPT.read_text(encoding="utf-8")
        self.feedback_source = _section(
            self.source, "    function emptyCleanupText(", "    async function cleanEmpty(",
        )
        self.action_source = _section(
            self.source, "    async function cleanEmpty(", "    function renderScheduleStatus(",
        )
        self.busy_source = _section(
            self.source, "    function setOrganizeActionBusy(", "    async function run(",
        )

    def _node(self, script: str):
        result = subprocess.run(
            ["node", "-e", self.feedback_source + "\n" + script],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=20,
        )
        return json.loads(result.stdout)

    def _feedback(self, *responses):
        return self._node(
            "console.log(JSON.stringify("
            + json.dumps(responses, ensure_ascii=False)
            + ".map(emptyCleanupFeedback)));"
        )

    def _actions(self, *cases):
        # 仅提取目标函数，所有请求、确认与状态刷新均由内存 fixture 接管。
        harness = textwrap.dedent(
            """
            async function runCase(config) {
                let organizeActionBusy = config.initialBusy || false;
                let organizeStatusRunning = config.running || false;
                const organizeStoppableTaskId = organizeStatusRunning ? 'active-task' : '';
                const isRules = false;
                const sources = config.noSources ? [] : [{id: 'fixture-source', name: '测试来源'}];
                const buttons = Object.fromEntries(
                    ['runOrganizeBtn', 'stopOrganizeBtn', 'cleanEmptyBtn'].map(id => [id, {disabled: false}])
                );
                const document = {getElementById: id => buttons[id]};
                const states = [];
                const alerts = [];
                const requests = [];
                let confirmations = 0;
                let refreshes = 0;
                const snapshot = phase => states.push({phase, busy: organizeActionBusy,
                    disabled: Object.fromEntries(Object.entries(buttons).map(([id, el]) => [id, el.disabled]))});
                const appConfirm = async () => { confirmations += 1; return config.confirm !== false; };
                const appAlert = async options => {
                    alerts.push(options);
                    snapshot('alert');
                    if (config.reenter) await cleanEmpty();
                };
                const fetch = async (url, options) => {
                    requests.push({url, method: options.method, body: JSON.parse(options.body)});
                    snapshot('fetch');
                    if (config.networkError) throw new Error(config.networkError);
                    if (config.throwNull) throw null;
                    return {
                        ok: config.httpOk !== false,
                        json: async () => {
                            if (config.invalidJson) throw new Error('bad JSON');
                            return config.response;
                        },
                    };
                };
                const loadStatus = async () => { refreshes += 1; snapshot('refresh'); };
            """
        )
        harness += self.busy_source + "\n" + self.action_source
        harness += textwrap.dedent(
            """
                setOrganizeActionBusy(organizeActionBusy);
                snapshot('initial');
                await cleanEmpty();
                snapshot('done');
                return {states, alerts, requests, confirmations, refreshes};
            }
            (async () => {
                const results = [];
                for (const config of CASES) results.push(await runCase(config));
                console.log(JSON.stringify(results));
            })().catch(error => { console.error(error); process.exitCode = 1; });
            """
        ).replace("CASES", json.dumps(cases, ensure_ascii=False))
        return self._node(harness)

    def test_legacy_success_and_zero_remain_compatible(self):
        success, zero, old_without_ok = self._feedback(
            {"ok": True, "cleaned": 3},
            {"ok": True, "cleaned": 0},
            {"cleaned": 1},
        )
        self.assertEqual(success, {
            "type": "success", "title": "空目录清理完成", "message": "已清理 3 个空目录。",
        })
        self.assertEqual(zero["type"], "info")
        self.assertIn("未发现可清理空目录", zero["message"])
        self.assertEqual(old_without_ok["type"], "success")

    def test_protected_roots_active_downloads_and_nonempty_are_explained_not_failed(self):
        zero, positive, reason_only = self._feedback(
            {"ok": True, "cleaned": 0, "scanned": 6, "candidates": 0,
             "protected": 2, "not_empty": 3, "reasons": ["活动下载目录已保留"]},
            {"ok": True, "cleaned": 1, "protected": 2, "not_empty": 3},
            {"ok": True, "cleaned": 0, "reasons": ["来源根目录永久保留"]},
        )
        for feedback in (zero, positive, reason_only):
            self.assertIn(feedback["type"], ("info", "success"))
            self.assertNotIn("失败", feedback["message"])
            self.assertNotIn("未发现可清理空目录", feedback["message"])
        self.assertEqual(zero["type"], "info")
        for text in ("受保护目录", "2", "非空目录", "3", "来源根目录", "活动下载"):
            self.assertIn(text, zero["message"])
        self.assertIn("来源根目录永久保留", reason_only["message"])

    def test_true_no_candidates_and_unexplained_candidates_are_distinct(self):
        empty, unresolved = self._feedback(
            {"ok": True, "cleaned": 0, "scanned": 4, "candidates": 0,
             "protected": 0, "not_empty": 0, "reasons": []},
            {"ok": True, "cleaned": 0, "scanned": 4, "candidates": 2},
        )
        self.assertEqual(empty["type"], "info")
        self.assertIn("未发现可清理空目录", empty["message"])
        self.assertEqual(unresolved["type"], "warning")
        self.assertIn("候选", unresolved["message"])
        self.assertIn("原因", unresolved["message"])
        self.assertNotIn("未发现可清理空目录", unresolved["message"])

    def test_failures_partial_unsupported_and_unavailable_never_show_green(self):
        reasons = {
            "scan_failures": "扫描失败", "delete_failures": "删除失败",
            "unsupported": "不支持", "unavailable": "不可用",
        }
        for key, label in reasons.items():
            for cleaned in (0, 2):
                with self.subTest(key=key, cleaned=cleaned):
                    feedback, = self._feedback({"ok": True, "cleaned": cleaned, key: 1})
                    self.assertIn(feedback["type"], ("warning", "error"))
                    self.assertIn(label, feedback["message"])
                    self.assertNotIn("未发现可清理空目录", feedback["message"])
        partial, failed, mixed = self._feedback(
            {"ok": True, "partial": True, "cleaned": 0},
            {"ok": False, "cleaned": 0, "error": "来源暂不可用"},
            {"ok": False, "cleaned": 2, "scan_failures": "4", "delete_failures": "2"},
        )
        self.assertEqual(partial["type"], "warning")
        self.assertIn("未完成", partial["message"])
        self.assertNotIn("0 项未完成", partial["message"])
        self.assertEqual(failed["type"], "error")
        self.assertIn("来源暂不可用", failed["message"])
        self.assertEqual(mixed["type"], "warning")
        self.assertIn("扫描失败 4", mixed["message"])
        self.assertIn("删除失败 2", mixed["message"])
        self.assertNotIn("42 项", mixed["message"])

    def test_failure_diagnostics_already_explain_uncleaned_candidates(self):
        responses: list[dict] = [
            {"ok": True, "cleaned": 0, "candidates": 2, key: 1}
            for key in ("scan_failures", "delete_failures", "unsupported", "unavailable")
        ]
        responses.append({"ok": False, "cleaned": 0, "candidates": 2, "error": "安全删除能力不可用"})
        for feedback in self._feedback(*responses):
            self.assertIn(feedback["type"], ("warning", "error"))
            self.assertNotIn("未返回未清理原因", feedback["message"])
            self.assertNotIn("未发现可清理空目录", feedback["message"])

    def test_flags_accept_case_and_whitespace_without_truthy_false(self):
        success, partial, failed, numeric = self._feedback(
            {"ok": " TRUE ", "partial": " FaLsE ", "cleaned": " 2 ", "scanned": "4"},
            {"ok": "TrUe", "partial": "tRuE", "cleaned": 0},
            {"ok": "FALSE", "partial": "false", "cleaned": 0},
            {"ok": 1, "partial": "0", "cleaned": "02", "protected": "0"},
        )
        self.assertEqual(success["type"], "success")
        self.assertEqual(partial["type"], "warning")
        self.assertEqual(failed["type"], "error")
        self.assertEqual(numeric["type"], "success")
        self.assertIn("已清理 2 个", success["message"])
        self.assertIn("已清理 2 个", numeric["message"])
        for key in ("ok", "partial"):
            for invalid in (None, [], {}, "yes", "<b>true</b>", 2):
                with self.subTest(key=key, invalid=invalid):
                    feedback, = self._feedback({"cleaned": 1, key: invalid})
                    self.assertNotEqual(feedback["type"], "success")
                    self.assertIn("格式异常", feedback["message"])

    def test_missing_or_nonobject_response_never_fabricates_success(self):
        responses: list[object] = [None, [], "ok", True, 1, {}, {"ok": True}, {"ok": True, "scanned": 2}]
        for response, feedback in zip(responses, self._feedback(*responses)):
            with self.subTest(response=response):
                self.assertNotEqual(feedback["type"], "success")
                self.assertNotIn("未发现可清理空目录", feedback["message"])
                self.assertNotIn("已清理 0 个", feedback["message"])
                self.assertIn("清理数量", feedback["message"])

    def test_counts_reject_coercion_negative_fractional_and_unsafe_values(self):
        invalid_values = [
            None, True, False, [], {}, -1, 1.5, float("nan"), float("inf"),
            "", " ", "-2", "1.5", "NaN", "Infinity", "0x10", "1e3", "3个",
            9007199254740992, "9007199254740992", "<img src=x onerror=alert(1)>",
        ]
        keys = ("cleaned", "scanned", "candidates", "protected", "not_empty",
                "scan_failures", "delete_failures", "unsupported", "unavailable")
        for key in keys:
            responses: list[dict] = [{"ok": True, "cleaned": 1, key: value} for value in invalid_values]
            for value, feedback in zip(invalid_values, self._feedback(*responses)):
                with self.subTest(key=key, value=value):
                    self.assertNotEqual(feedback["type"], "success")
                    self.assertIn("格式异常", feedback["message"])
                    for unwanted in ("NaN", "Infinity", "[object Object]", "-1", "<img"):
                        self.assertNotIn(unwanted, feedback["message"])
                    if key == "cleaned":
                        self.assertIn("清理数量", feedback["message"])
                        self.assertNotIn("已清理 0 个", feedback["message"])

    def test_reasons_are_bounded_deduplicated_text_and_do_not_dump_structures(self):
        injection = '<img src=x onerror="globalThis.pwned=true">'
        feedback, malformed, invalid_item = self._feedback(
            {"ok": True, "cleaned": 0, "protected": 1,
             "reasons": ["  Retained  ", "retained", "\n" + injection + "\t",
                         "长" * 400, "不应显示的第4条", "不应显示的第5条"],
             "sources": [{"private_path": "/do-not-expose", "secret": "fixture-only"}]},
            {"ok": True, "cleaned": 0, "reasons": {"private": "do-not-dump"}},
            {"ok": True, "cleaned": 0, "reasons": [None, {"private": "do-not-dump"}, 5]},
        )
        self.assertIn(injection, feedback["message"])
        self.assertEqual(feedback["message"].lower().count("retained"), 1)
        self.assertNotIn("长" * 81, feedback["message"])
        self.assertNotIn("第4条", feedback["message"])
        self.assertNotIn("第5条", feedback["message"])
        self.assertIn("更多原因", feedback["message"])
        self.assertLess(len(feedback["message"]), 700)
        for result in (feedback, malformed, invalid_item):
            for private in ("[object Object]", "do-not-expose", "fixture-only", "do-not-dump"):
                self.assertNotIn(private, result["message"])
        for result in (malformed, invalid_item):
            self.assertEqual(result["type"], "warning")

    def test_combined_diagnostics_keep_a_mobile_sized_text_budget(self):
        feedback, = self._feedback({
            "ok": False, "partial": True, "cleaned": 0,
            **{key: 9007199254740991 for key in (
                "scanned", "candidates", "protected", "not_empty", "scan_failures",
                "delete_failures", "unsupported", "unavailable",
            )},
            "error": "异常" * 200,
            "reasons": ["保护" * 200, "非空" * 200, "能力" * 200, "更多诊断"],
        })
        self.assertEqual(feedback["type"], "error")
        self.assertLess(len(feedback["message"]), 650)
        for label in ("受保护目录", "非空目录", "扫描失败", "删除失败", "不支持", "不可用"):
            self.assertIn(label, feedback["message"])
        self.assertIn("更多原因", feedback["message"])

    def test_action_preserves_busy_through_alert_and_restores_on_every_result(self):
        cases = [
            {"response": {"ok": True, "cleaned": 2}},
            {"response": {"ok": True, "cleaned": 0}},
            {"response": {"ok": True, "cleaned": 0, "protected": 2}},
            {"response": {"ok": True, "cleaned": 0, "unsupported": 1}},
            {"response": {"ok": True, "cleaned": 0, "partial": True}},
            {"response": {"ok": False, "cleaned": 0, "delete_failures": 1}},
            {"response": None}, {"response": []}, {"invalidJson": True},
            {"httpOk": False, "response": {"error": "拒绝清理"}},
            {"httpOk": False, "response": {"error": {"private": "do-not-dump"}}},
            {"httpOk": False, "response": None},
            {"networkError": "连接失败"}, {"throwNull": True},
            {"response": {"ok": True, "cleaned": 2}, "reenter": True},
            {"response": {"ok": True, "cleaned": 0}, "running": True},
        ]
        for config, result in zip(cases, self._actions(*cases)):
            with self.subTest(config=config):
                self.assertEqual(result["confirmations"], 1)
                self.assertEqual(result["refreshes"], 1)
                self.assertEqual(len(result["alerts"]), 1)
                self.assertEqual(result["requests"], [{
                    "url": "/api/guangya/organize/clean-empty", "method": "POST",
                    "body": {"source_dirs": [{"id": "fixture-source", "name": "测试来源"}]},
                }])
                for state in result["states"]:
                    if state["phase"] in ("fetch", "alert"):
                        self.assertTrue(state["busy"])
                        self.assertTrue(all(state["disabled"].values()))
                final = result["states"][-1]
                self.assertFalse(final["busy"])
                self.assertEqual(final["disabled"], result["states"][0]["disabled"])
                self.assertFalse(result["states"][-2]["busy"])
                message = result["alerts"][0]["message"]
                assert isinstance(message, str)
                self.assertNotIn("do-not-dump", message)
                if config.get("httpOk") is False or config.get("networkError") or config.get("throwNull"):
                    self.assertEqual(result["alerts"][0]["type"], "error")

    def test_guards_cancel_and_busy_do_not_submit_or_refresh(self):
        no_sources, cancelled, busy = self._actions(
            {"noSources": True}, {"confirm": False}, {"initialBusy": True},
        )
        for result in (no_sources, cancelled, busy):
            self.assertEqual(result["requests"], [])
            self.assertEqual(result["refreshes"], 0)
            self.assertEqual(result["states"][-1]["disabled"], result["states"][0]["disabled"])
        self.assertEqual(no_sources["alerts"][0]["title"], "未选择源目录")
        self.assertEqual(no_sources["confirmations"], 0)
        self.assertEqual(cancelled["alerts"], [])
        self.assertEqual(cancelled["confirmations"], 1)
        self.assertEqual(busy["alerts"], [])
        self.assertTrue(busy["states"][-1]["busy"])

    def test_http_and_network_messages_remain_bounded_plain_text(self):
        injection = '<img src=x onerror="globalThis.pwned=true">'
        for config, result in zip(
            ("http", "network"),
            self._actions(
                {"httpOk": False, "response": {"error": injection + "长" * 1000}},
                {"networkError": injection + "长" * 1000},
            ),
        ):
            with self.subTest(config=config):
                alert = result["alerts"][0]
                self.assertEqual(alert["type"], "error")
                self.assertIn(injection, alert["message"])
                self.assertLessEqual(len(alert["message"]), 201)
                self.assertEqual(set(alert), {"type", "title", "message"})

    def test_feedback_uses_existing_text_alert_and_no_layout_or_busy_mutations(self):
        self.assertIn("await appAlert(emptyCleanupFeedback(data))", self.action_source)
        self.assertIn("finally{setOrganizeActionBusy(false);await loadStatus();}", self.action_source)
        for fragment in (self.feedback_source, self.action_source):
            for forbidden in ("innerHTML", "insertAdjacentHTML", "classList", ".style", "createElement", "textContent="):
                self.assertNotIn(forbidden, fragment)
        app_source = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        self.assertIn("messageText.textContent = options.message || '';", app_source)


if __name__ == "__main__":
    unittest.main()
