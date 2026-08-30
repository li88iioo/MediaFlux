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
            "    function loadAll(manual = false, {includeItems = false} = {}) {",
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
            let queuedItemRefresh = false;
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
              const outcome = await loadAll(false, {{includeItems: true}});
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


class StrmStatusPollingLifecycleTests(unittest.TestCase):
    def test_status_polling_is_single_flight_and_preserves_last_good_state(self):
        source = (ROOT / "app/static/js/guangya-strm.js").read_text(encoding="utf-8")
        status_block = _extract(
            source,
            "    function clearStatusPoll(){",
            "\n    function getDiagnosticValue",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const progress = {{textContent: '最近状态'}};
            let visibilityHandler = null;
            const document = {{
              hidden: false,
              getElementById: (id) => id === 'strmProgressStage' ? progress : {{textContent: ''}},
              addEventListener: (name, handler) => {{if (name === 'visibilitychange') visibilityHandler = handler;}},
            }};
            const timers = [];
            const window = {{
              setTimeout: (fn, delay) => {{timers.push({{fn, delay}}); return timers.length;}},
              clearTimeout: () => {{}},
            }};
            const STRM_STATUS_POLL_MS = 2500;
            const STRM_STATUS_RETRY_MS = 5000;
            let pollTimer = null;
            let statusShouldPoll = true;
            let statusRequestSerial = 0;
            let statusAbortController = null;
            let lastStatusProgressText = '最近状态';
            let activeStrmTab = 'schedule';
            let strmConfigReady = true;
            const setTag = () => {{throw new Error('background failure must preserve the stable tag');}};
            const renderStatus = () => {{statusShouldPoll = true; lastStatusProgressText = '最近状态'; progress.textContent = '最近状态';}};
            let fetchImpl;
            const fetch = (...args) => fetchImpl(...args);
            {status_block}
            (async () => {{
              const pending = [];
              fetchImpl = (_url, options) => new Promise((resolve, reject) => {{
                const error = new Error('aborted');
                error.name = 'AbortError';
                options.signal.addEventListener('abort', () => reject(error), {{once: true}});
                pending.push({{resolve, signal: options.signal}});
              }});
              const first = loadStatus({{background: true}});
              const second = loadStatus({{background: true}});
              assert.equal(pending[0].signal.aborted, true);
              pending[1].resolve({{ok: true, status: 200, json: async () => ({{running: true}})}});
              assert.equal((await second).ok, true);
              assert.equal((await first).stale, true);
              assert.equal(timers.at(-1).delay, STRM_STATUS_POLL_MS);

              fetchImpl = async () => ({{ok: false, status: 503, json: async () => ({{}})}});
              await pollStatus();
              assert.match(progress.textContent, /状态同步失败，重试中/);
              assert.equal(timers.at(-1).delay, STRM_STATUS_RETRY_MS);

              document.hidden = true;
              visibilityHandler();
              assert.equal(pollTimer, null);
            }})().catch((error) => {{console.error(error); process.exit(1);}});
            """
        )
        _run_node(script)


class SettingsDraftLifecycleTests(unittest.TestCase):
    def test_draft_gate_aborts_and_invalidates_stale_results(self):
        source = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
        gate_block = _extract(
            source,
            "    const isAbortError=",
            "\n    const INDEXER_SITE_ORDER",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            {gate_block}
            let draft = {{url: 'https://old.example'}};
            let invalidations = 0;
            const handlers = {{}};
            const field = {{addEventListener: (name, handler) => {{handlers[name] = handler;}}}};
            const gate = createDraftRequestGate(() => draft, () => {{invalidations += 1;}});
            bindDraftInvalidation([field], gate);
            const oldTicket = gate.begin();
            draft = {{url: 'https://new.example'}};
            handlers.input();
            assert.equal(oldTicket.signal.aborted, true);
            assert.equal(gate.isCurrent(oldTicket), false);
            assert.equal(invalidations, 1);
            const currentTicket = gate.begin();
            assert.equal(gate.isCurrent(currentTicket), true);
            assert.equal(gate.finish(currentTicket), true);
            """
        )
        _run_node(script)

        self.assertIn("agentModelRequestGate=createDraftRequestGate", source)
        self.assertIn("qbRequestGate=createDraftRequestGate", source)
        self.assertIn("telegramRequestGate=createDraftRequestGate", source)
        self.assertIn("tmdbRequestGate=createDraftRequestGate", source)
        self.assertGreaterEqual(source.count("signal:ticket.signal"), 5)


    def test_config_save_preserves_edits_made_while_request_is_in_flight(self):
        app_source = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        settings_source = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
        save_block = _extract(
            app_source,
            "    window.collectConfigFields = function (root) {",
            "\n\n    window.setupTabGroup",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            global.window = global;
            function syncSecretControls() {{}}
            {save_block}
            const normal = {{
              type: 'text', value: 'sent-value',
              dataset: {{key: 'NORMAL', configInitialValue: 'old-value'}},
            }};
            const secret = {{
              type: 'password', value: 'secret-a', placeholder: '',
              dataset: {{key: 'SECRET', secretField: 'true', secretState: 'draft'}},
            }};
            const fields = [normal, secret];
            const root = {{querySelectorAll: selector => selector === '[data-key]' ? fields : []}};
            let sentPayload = null;
            let resolveRequest;
            global.fetch = (_url, options) => {{
              sentPayload = JSON.parse(options.body);
              return new Promise(resolve => {{resolveRequest = resolve;}});
            }};
            (async () => {{
              const pending = window.saveAppConfig(root, {{toast: false}});
              assert.deepEqual(sentPayload, {{NORMAL: 'sent-value', SECRET: 'secret-a'}});
              normal.value = 'newer-value';
              secret.value = 'secret-b';
              secret.dataset.secretState = 'draft';
              resolveRequest({{ok: true, json: async () => ({{}})}});
              const first = await pending;
              assert.equal(first.__hasPendingConfigChanges, true);
              assert.equal(normal.dataset.configInitialValue, 'sent-value');
              assert.equal(normal.value, 'newer-value');
              assert.equal(secret.value, 'secret-b');
              assert.equal(secret.dataset.secretState, 'draft');
              assert.deepEqual(window.collectConfigFields(root), {{NORMAL: 'newer-value', SECRET: 'secret-b'}});

              global.fetch = async (_url, options) => ({{ok: true, json: async () => ({{saved: JSON.parse(options.body)}})}});
              const second = await window.saveAppConfig(root, {{toast: false}});
              assert.equal(second.__hasPendingConfigChanges, false);
              assert.equal(normal.dataset.configInitialValue, 'newer-value');
              assert.equal(secret.value, '');
              assert.equal(secret.dataset.secretState, 'saved');
            }})().catch(error => {{console.error(error); process.exit(1);}});
            """
        )
        _run_node(script)
        self.assertIn("上一版已保存，仍有未保存更改", settings_source)

    def test_every_config_save_surface_reports_in_flight_edits(self):
        paths = (
            ROOT / "app/templates/dashboard.html",
            ROOT / "app/templates/guangya_offline.html",
            ROOT / "app/static/js/downloads.js",
            ROOT / "app/static/js/guangya-strm.js",
            ROOT / "app/static/js/organize.js",
            ROOT / "app/static/js/settings.js",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                source = path.read_text(encoding="utf-8")
                self.assertIn("saveAppConfig(", source)
                self.assertIn("__hasPendingConfigChanges", source)
                self.assertIn("上一版已保存，仍有未保存更改", source)

        dashboard = paths[0].read_text(encoding="utf-8")
        pending_branch = dashboard.index("if(hasPendingChanges)")
        reload_call = dashboard.index("window.location.reload()", pending_branch)
        self.assertIn("return;", dashboard[pending_branch:reload_call])

class AppModalFocusLifecycleTests(unittest.TestCase):
    def test_nested_modals_only_close_top_and_restore_focus_layer_by_layer(self):
        source = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        modal_block = _extract(
            source,
            "    const modalStack = [];",
            "\n\n    const confirmModal",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const frames = [];
            const keydownHandlers = [];
            const classes = new Set();
            const classList = {{
              toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
              contains: (name) => classes.has(name),
            }};
            const body = {{classList, appendChild: () => {{}}}};
            const document = {{
              body,
              activeElement: null,
              addEventListener: (name, handler) => {{
                if (name === 'keydown') keydownHandlers.push(handler);
              }},
            }};
            const makeFocusable = (name) => ({{
              name,
              hidden: false,
              isConnected: true,
              getAttribute: () => null,
              closest: () => null,
              focus: () => {{document.activeElement = elements[name];}},
            }});
            const elements = {{outer: null, parentFirst: null, parentLast: null, childFirst: null, childLast: null}};
            Object.keys(elements).forEach((name) => {{elements[name] = makeFocusable(name);}});
            function makeModal(first, last) {{
              const dialog = {{
                querySelectorAll: () => [first, last],
                querySelector: () => first,
                contains: (element) => element === first || element === last,
                hasAttribute: () => true,
                setAttribute: () => {{}},
                focus: () => {{document.activeElement = dialog;}},
              }};
              const modal = {{
                hidden: true,
                parentElement: body,
                querySelector: () => dialog,
                querySelectorAll: () => [],
                addEventListener: () => {{}},
              }};
              return {{modal, dialog}};
            }}
            const requestAnimationFrame = (callback) => {{frames.push(callback); return frames.length;}};
            const window = {{}};
            {modal_block}
            const parent = makeModal(elements.parentFirst, elements.parentLast);
            const child = makeModal(elements.childFirst, elements.childLast);
            // 生命周期创建顺序与实际页面一致：全局确认框通常早于页面弹窗创建，
            // 但页面弹窗会先打开，确认框随后叠在其上方。
            const childLifecycle = window.createAppModal(child.modal);
            const parentLifecycle = window.createAppModal(parent.modal);
            const dispatchKey = (key, shiftKey = false) => {{
              let prevented = 0;
              let immediateStopped = false;
              const event = {{
                key,
                shiftKey,
                preventDefault: () => {{prevented += 1;}},
                stopImmediatePropagation: () => {{immediateStopped = true;}},
                stopPropagation: () => {{}},
              }};
              for (const handler of keydownHandlers) {{
                handler(event);
                if (immediateStopped) break;
              }}
              return prevented;
            }};

            document.activeElement = elements.outer;
            parentLifecycle.open(elements.outer, {{initialFocus: elements.parentFirst}});
            frames.shift()();
            childLifecycle.open(elements.parentFirst, {{initialFocus: elements.childFirst}});
            frames.shift()();
            assert.equal(document.activeElement, elements.childFirst);
            assert.equal(body.classList.contains('modal-open'), true);

            document.activeElement = elements.childLast;
            assert.equal(dispatchKey('Tab'), 1);
            assert.equal(document.activeElement, elements.childFirst);

            assert.equal(dispatchKey('Escape'), 1);
            assert.equal(child.modal.hidden, true);
            assert.equal(parent.modal.hidden, false);
            assert.equal(document.activeElement, elements.parentFirst);
            assert.equal(body.classList.contains('modal-open'), true);

            assert.equal(dispatchKey('Escape'), 1);
            assert.equal(parent.modal.hidden, true);
            assert.equal(document.activeElement, elements.outer);
            assert.equal(body.classList.contains('modal-open'), false);

            parentLifecycle.open(elements.outer, {{initialFocus: elements.parentFirst}});
            parentLifecycle.close();
            frames.shift()();
            assert.equal(document.activeElement, elements.outer);
            """
        )
        _run_node(script)


class AgentVisualViewportLifecycleTests(unittest.TestCase):
    def test_bfcache_rebind_is_idempotent_and_cancels_pending_frame(self):
        source = (ROOT / "app/static/js/agent.js").read_text(encoding="utf-8")
        viewport_block = _extract(
            source,
            "    function cancelVisualViewportFrame() {",
            "\n\n    syncConversationLayout();",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            let visualViewportFrame = 0;
            let boundVisualViewport = null;
            let nextFrame = 0;
            const frames = new Map();
            const cancelled = [];
            const windowHandlers = {{}};
            const viewportHandlers = {{resize: new Set(), scroll: new Set()}};
            const viewport = {{
              height: 700,
              addEventListener: (name, handler) => viewportHandlers[name].add(handler),
              removeEventListener: (name, handler) => viewportHandlers[name].delete(handler),
            }};
            const window = {{
              visualViewport: viewport,
              innerHeight: 800,
              requestAnimationFrame: callback => {{const id = ++nextFrame; frames.set(id, callback); return id;}},
              cancelAnimationFrame: id => {{cancelled.push(id); frames.delete(id);}},
              addEventListener: (name, handler) => {{windowHandlers[name] = handler;}},
            }};
            const styleValues = {{}};
            const document = {{
              activeElement: null,
              documentElement: {{style: {{setProperty: (name, value) => {{styleValues[name] = value;}}}}}},
            }};
            const page = {{classList: {{toggle: () => {{}}}}}};
            const promptInput = {{}};
            const scrollToLatest = () => {{}};
            {viewport_block}
            assert.equal(viewportHandlers.resize.size, 1);
            assert.equal(viewportHandlers.scroll.size, 1);
            assert.ok(visualViewportFrame > 0);
            const pendingFrame = visualViewportFrame;
            windowHandlers.pagehide();
            assert.equal(viewportHandlers.resize.size, 0);
            assert.equal(viewportHandlers.scroll.size, 0);
            assert.equal(visualViewportFrame, 0);
            assert.deepEqual(cancelled, [pendingFrame]);
            windowHandlers.pageshow({{persisted: true}});
            windowHandlers.pageshow({{persisted: true}});
            assert.equal(viewportHandlers.resize.size, 1);
            assert.equal(viewportHandlers.scroll.size, 1);
            const frame = frames.get(visualViewportFrame);
            frame();
            assert.equal(styleValues['--agent-viewport-height'], '700px');
            """
        )
        _run_node(script)


class AgentTranscriptLifecycleTests(unittest.TestCase):
    def test_transcript_pruning_keeps_latest_and_active_message(self):
        source = (ROOT / "app/static/js/agent.js").read_text(encoding="utf-8")
        prune_block = _extract(
            source,
            "    function transcriptMessageMustRemain",
            "\n    function appendUserMessage",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const MAX_TRANSCRIPT_MESSAGES = 120;
            const confirmationTimers = new Map();
            let activeQuery = null;
            const transcript = {{children: []}};
            const clearConfirmationTimer = () => {{}};
            const message = (id) => {{
              const item = {{
                id,
                classList: {{contains: (name) => name === 'agent-message'}},
                querySelector: () => null,
                querySelectorAll: () => [],
                remove: () => {{transcript.children.splice(transcript.children.indexOf(item), 1);}},
              }};
              return item;
            }};
            {prune_block}
            transcript.children = Array.from({{length: 122}}, (_, index) => message(index));
            pruneTranscript({{preserve: [transcript.children.at(-1)]}});
            assert.equal(transcript.children.length, 120);
            assert.equal(transcript.children[0].id, 2);
            assert.equal(transcript.children.at(-1).id, 121);

            transcript.children = Array.from({{length: 121}}, (_, index) => message(index));
            activeQuery = {{pending: transcript.children[0]}};
            pruneTranscript({{preserve: [transcript.children.at(-1)]}});
            assert.equal(transcript.children.length, 120);
            assert.equal(transcript.children[0].id, 0);
            assert.equal(transcript.children.some((item) => item.id === 1), false);
            """
        )
        _run_node(script)


if __name__ == "__main__":
    unittest.main()
