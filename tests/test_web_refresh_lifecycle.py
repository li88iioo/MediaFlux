from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extract(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index:source.index(end, start_index)]


def _run_node(script: str) -> None:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)


class LocalMediaRefreshLifecycleTests(unittest.TestCase):
    def test_partial_refresh_keeps_old_items_and_item_button_reports_failure(self):
        source = (ROOT / "app/static/js/local-media.js").read_text(encoding="utf-8")
        load_all = _extract(
            source,
            "    function loadAll(manual = false) {",
            "\n    function openLocalDirectoryPicker",
        )
        refresh_items = _extract(
            source,
            "    async function refreshMediaItems() {",
            "\n    let currentTab = 'tasks';",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            let sources = [{{id: 'old-source'}}];
            let tasks = [{{id: 'old-task'}}];
            let mediaItems = [{{id: 'old-item'}}];
            let hasLoadedLocalMedia = true;
            const loadedResources = {{sources: true, tasks: true, items: true}};
            let refreshing = false;
            let refreshQueued = false;
            let queuedManualRefresh = false;
            let refreshPromise = Promise.resolve();
            let initialLoadingTimer = null;
            let initialLoadingShownAt = 0;
            let currentTab = 'manual';
            let mediaBrowseRequestSerial = 0;
            const alerts = [];
            const renders = {{sources: 0, tasks: 0, items: 0}};
            const elements = {{
              lmRefreshBtn: {{}}, lmRefreshItemsBtn: {{}}, lmSourceGrid: {{}},
              lmReviewList: {{}}, lmTaskList: {{}}, lmMediaItems: {{}},
            }};
            const $ = (id) => elements[id];
            const document = {{
              querySelector: () => null,
              createElement: () => ({{textContent: '', innerHTML: ''}}),
            }};
            const window = {{appAlert: true}};
            const appAlert = (value) => alerts.push(value);
            const setBusy = () => {{}};
            const armInitialLoading = () => {{}};
            const settleInitialLoading = async () => {{}};
            const mediaItemsUrl = () => '/api/local-media/items';
            const applyMediaItemData = (data) => {{mediaItems = data.items || [];}};
            const renderSources = () => {{renders.sources += 1;}};
            const renderTasks = () => {{renders.tasks += 1;}};
            const renderMediaItems = () => {{renders.items += 1;}};
            const renderInitialResourceFailure = () => {{}};
            const setRefreshFailure = () => {{}};
            const api = async (path) => {{
              if (path.endsWith('/sources')) return {{sources: [{{id: 'new-source'}}]}};
              if (path.endsWith('/tasks')) return {{tasks: [{{id: 'new-task'}}]}};
              throw new Error('items unavailable');
            }};
            {load_all}
            {refresh_items}
            (async () => {{
              const outcome = await loadAll(false);
              assert.equal(outcome.ok, false);
              assert.equal(outcome.partial, true);
              assert.equal(outcome.resources.sources.applied, true);
              assert.equal(outcome.resources.tasks.applied, true);
              assert.equal(outcome.resources.items.ok, false);
              assert.deepEqual(mediaItems, [{{id: 'old-item'}}]);
              assert.equal(renders.sources, 1);
              assert.equal(renders.tasks, 1);
              assert.equal(renders.items, 0);

              await refreshMediaItems();
              assert.equal(alerts.at(-1).type, 'error');
              assert.equal(alerts.at(-1).title, '条目刷新失败');
              assert.deepEqual(mediaItems, [{{id: 'old-item'}}]);
            }})().catch((error) => {{console.error(error); process.exit(1);}});
            """
        )
        _run_node(script)


class OrganizeStatusPollingLifecycleTests(unittest.TestCase):
    def test_status_failures_preserve_last_success_and_retry_with_timeout(self):
        source = (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8")
        status_block = _extract(
            source,
            "    function renderScheduleStatus(schedule){",
            "\n\n\n    document.getElementById('saveOrganizeConfigBtn')",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const nodes = new Map();
            const node = (id) => {{
              if (!nodes.has(id)) nodes.set(id, {{
                id, textContent: '', className: '', hidden: false, disabled: false,
                dataset: {{}}, innerHTML: '',
                querySelector: () => ({{classList: {{toggle: () => {{}}}}}}),
              }});
              return nodes.get(id);
            }};
            let visibilityHandler = null;
            const document = {{
              hidden: false,
              getElementById: node,
              addEventListener: (name, handler) => {{if (name === 'visibilitychange') visibilityHandler = handler;}},
            }};
            const timers = [];
            const cleared = [];
            const window = {{
              setTimeout: (fn, delay) => {{timers.push({{fn, delay}}); return timers.length;}},
              clearTimeout: (id) => cleared.push(id),
            }};
            const renderLucideIcons = () => {{}};
            const isRules = false;
            let pollTimer = null;
            let lastStatusRenderKey = '';
            let lastStatusDetailText = '';
            let lastScheduleLastText = '';
            const STATUS_ACTIVE_POLL_MS = 2000;
            const STATUS_IDLE_POLL_MS = 30000;
            const STATUS_RETRY_POLL_MS = 5000;
            let statusRequestSerial = 0;
            let organizeActionBusy = false;
            let organizeStatusRunning = false;
            let fetchImpl;
            const fetch = (...args) => fetchImpl(...args);
            {status_block}
            (async () => {{
              fetchImpl = async () => ({{
                ok: true, status: 200,
                json: async () => ({{
                  status: 'running', message: '整理中', current_source: '下载',
                  stats: {{moved: 1, skipped: 0, failed: 0}}, schedule: {{}},
                }}),
              }});
              assert.equal((await loadStatus()).ok, true);
              const tag = node('organizeStateTag');
              const run = node('runOrganizeBtn');
              const stop = node('stopOrganizeBtn');
              const stableTag = tag.textContent;
              assert.equal(run.disabled, true);
              assert.equal(stop.disabled, false);

              fetchImpl = async () => ({{ok: false, status: 503, json: async () => ({{}})}});
              assert.equal((await loadStatus()).ok, false);
              assert.equal(tag.textContent, stableTag);
              assert.equal(run.disabled, true);
              assert.equal(stop.disabled, false);
              assert.match(node('organizeStatusDetail').textContent, /同步失败，重试中/);

              fetchImpl = async () => ({{ok: true, status: 200, json: async () => {{throw new Error('bad json');}}}});
              assert.equal((await loadStatus()).ok, false);
              assert.equal(tag.textContent, stableTag);
              assert.equal(run.disabled, true);
              assert.equal(stop.disabled, false);

              fetchImpl = async () => ({{ok: false, status: 502, json: async () => ({{}})}});
              await pollStatus();
              assert.equal(timers.at(-1).delay, STATUS_RETRY_POLL_MS);
              assert.equal(timers.some((timer) => timer.delay === STATUS_RETRY_POLL_MS), true);

              document.hidden = true;
              visibilityHandler();
              assert.equal(pollTimer, null);
              document.hidden = false;
              visibilityHandler();
              assert.equal(timers.at(-1).delay, 0);
            }})().catch((error) => {{console.error(error); process.exit(1);}});
            """
        )
        _run_node(script)

    def test_status_polling_uses_no_interval_loop(self):
        source = (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(loadStatus", source)
        self.assertIn("Promise.allSettled", (ROOT / "app/static/js/local-media.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
