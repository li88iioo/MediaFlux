from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extract(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


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
        self.assertIn(
            "Promise.allSettled",
            (ROOT / "app/static/js/local-media.js").read_text(encoding="utf-8"),
        )

    def test_config_save_serializes_requests_and_coalesces_while_busy(self):
        source = (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8")
        save_block = _extract(
            source,
            "    function finishConfigLoad(success){",
            "\n    async function preview(){",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            let saveCalls = 0;
            let activeSaves = 0;
            let maxActiveSaves = 0;
            let statusLoads = 0;
            const saveResolvers = [];
            const button = {{
              disabled: false,
              attributes: {{}},
              setAttribute(name, value) {{this.attributes[name] = String(value);}},
            }};
            const state = {{textContent: '', className: ''}};
            const document = {{
              getElementById: (id) => id === 'saveOrganizeConfigBtn' ? button : state,
            }};
            const workspace = {{}};
            const sourceInput = null;
            const isRules = false;
            const extensionEditors = {{video: null, metadata: null}};
            const configFieldLocks = [];
            let configReady = true;
            let configSaveBusy = false;
            let configSaveQueued = false;
            let configSavePromise = null;
            const saveAppConfig = async () => {{
              saveCalls += 1;
              activeSaves += 1;
              maxActiveSaves = Math.max(maxActiveSaves, activeSaves);
              return new Promise((resolve) => saveResolvers.push(() => {{activeSaves -= 1; resolve({{}});}}));
            }};
            const loadStatus = async () => {{statusLoads += 1;}};
            {save_block}
            (async () => {{
              const first = saveConfig();
              const duplicate = saveConfig();
              assert.equal(duplicate, first);
              assert.equal(saveCalls, 1);
              assert.equal(maxActiveSaves, 1);
              assert.equal(button.disabled, true);
              assert.equal(button.attributes['aria-busy'], 'true');
              saveResolvers.shift()();
              await new Promise(setImmediate);
              assert.equal(saveCalls, 2);
              assert.equal(activeSaves, 1);
              assert.equal(maxActiveSaves, 1);
              assert.equal(button.disabled, true);
              saveResolvers.shift()();
              await Promise.all([first, duplicate]);
              assert.equal(statusLoads, 2);
              assert.equal(button.disabled, false);
              assert.equal(button.attributes['aria-busy'], 'false');
            }})().catch((error) => {{console.error(error); process.exit(1);}});
            """
        )
        _run_node(script)


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
        settings_source = (ROOT / "app/static/js/settings.js").read_text(
            encoding="utf-8"
        )
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


class FrontendAsyncRequestLifecycleTests(unittest.TestCase):
    def test_media_recent_ignores_old_body_after_newer_results_apply(self):
        source = (ROOT / "app/templates/media_recent.html").read_text(encoding="utf-8")
        block = _extract(
            source,
            "    const form=document.querySelector('[data-media-recent-filter]');",
            "\n    form?.addEventListener('submit'",
        )
        script = (
            textwrap.dedent(
                """
            const assert = require('node:assert/strict');
            const makeClassList = () => ({add() {}, remove() {}, toggle() {}, contains() {return false;}});
            let currentResults = {
              name: 'initial', style: {}, classList: makeClassList(),
              getBoundingClientRect: () => ({height: 120}),
              replaceWith(next) {currentResults = next;},
            };
            const overview = {innerHTML: 'initial'};
            const filterForm = {
              classList: makeClassList(), setAttribute() {},
              closest: () => ({classList: makeClassList()}), querySelector: () => null,
            };
            const documents = {};
            for (const name of ['A', 'B']) {
              const results = {name, style: {}, classList: makeClassList()};
              documents[name] = {
                querySelector(selector) {
                  if (selector === '[data-media-recent-results]') return results;
                  if (selector === '.media-recent-overview-copy small') return {innerHTML: name};
                  return null;
                },
              };
            }
            const document = {
              querySelector(selector) {
                if (selector === '[data-media-recent-filter]') return filterForm;
                if (selector === '[data-media-recent-results]') return currentResults;
                if (selector === '.media-recent-overview-copy small') return overview;
                return null;
              },
            };
            class DOMParser {parseFromString(text) {return documents[text];}}
            const pendingBodies = new Map();
            const fetch = async (url) => ({
              ok: true, status: 200,
              text: () => new Promise(resolve => pendingBodies.set(url, resolve)),
            });
            const historyUrls = [];
            const history = {replaceState: (_state, _title, url) => historyUrls.push(String(url))};
            const location = {assign: () => {throw new Error('must not hard navigate');}};
            const renderLucideIcons = () => {};
            const requestAnimationFrame = callback => {callback(); return 1;};
            const matchMedia = () => ({matches: false});
            """
            )
            + block
            + textwrap.dedent(
                """
            (async () => {
              const first = updateResults('/first');
              await new Promise(setImmediate);
              const second = updateResults('/second');
              await new Promise(setImmediate);
              pendingBodies.get('/second')('B');
              await second;
              assert.equal(currentResults.name, 'B');
              assert.equal(overview.innerHTML, 'B');
              assert.deepEqual(historyUrls, ['/second']);

              pendingBodies.get('/first')('A');
              await first;
              assert.equal(currentResults.name, 'B');
              assert.equal(overview.innerHTML, 'B');
              assert.deepEqual(historyUrls, ['/second']);
            })().catch(error => {console.error(error); process.exit(1);});
            """
            )
        )
        _run_node(script)

    def test_torrent_policy_ignores_closed_modal_load_and_clears_busy_state(self):
        source = (ROOT / "app/static/js/downloads.js").read_text(encoding="utf-8")
        block = _extract(
            source,
            "const torrentCachePolicyForm=document.getElementById('torrentCachePolicyForm');",
            "\nlet overviewTimer=null;",
        )
        script = (
            textwrap.dedent(
                """
            const assert = require('node:assert/strict');
            const handlers = {};
            const attributes = {};
            const form = {};
            const button = {addEventListener(name, handler) {handlers['open:' + name] = handler;}};
            const save = {
              disabled: false,
              setAttribute(name, value) {attributes[name] = String(value);},
              removeAttribute(name) {delete attributes[name];},
              addEventListener(name, handler) {handlers['save:' + name] = handler;},
            };
            const retention = {
              value: '', readOnly: false, dataset: {},
              addEventListener(name, handler) {handlers['retention:' + name] = handler;},
              focus() {},
            };
            const state = {textContent: '', className: ''};
            const modalElement = {};
            const elements = {
              torrentCachePolicyForm: form,
              torrentCachePolicyBtn: button,
              torrentCachePolicySave: save,
              torrentCacheRetentionDays: retention,
              torrentCachePolicyState: state,
              torrentCachePolicyModal: modalElement,
            };
            const document = {getElementById: id => elements[id]};
            let modalOptions = null;
            let closes = 0;
            const loads = [];
            const window = {
              createAppModal(_element, options) {
                modalOptions = options;
                return {open() {}, close() {closes += 1;}};
              },
              loadAppConfig({signal} = {}) {
                return new Promise(resolve => loads.push({resolve, signal}));
              },
              fillConfigFields(_root, config) {retention.value = config.retention;},
              saveAppConfig: async () => ({}),
            };
            """
            )
            + block
            + textwrap.dedent(
                """
            (async () => {
              const first = openTorrentCachePolicy();
              await new Promise(setImmediate);
              assert.equal(attributes['aria-busy'], 'true');
              modalOptions.onRequestClose();
              assert.equal(attributes['aria-busy'], 'false');
              assert.equal(closes, 1);

              const second = openTorrentCachePolicy();
              await new Promise(setImmediate);
              loads[1].resolve({retention: '30'});
              await second;
              assert.equal(retention.value, '30');
              retention.value = '45';
              loads[0].resolve({retention: '7'});
              await first;
              assert.equal(retention.value, '45');
              assert.equal(save.disabled, false);
              assert.equal(attributes['aria-busy'], 'false');
            })().catch(error => {console.error(error); process.exit(1);});
            """
            )
        )
        _run_node(script)

    def test_directory_external_hints_ignore_changed_query(self):
        source = (ROOT / "app/static/js/guangya-directory-scrape.js").read_text(
            encoding="utf-8"
        )
        invalidate_block = _extract(
            source,
            "    function invalidateExternalHints() {",
            "\n\n    function openModal",
        )
        load_block = _extract(
            source,
            "    async function loadExternalHints() {",
            "\n\n    function addChip",
        )
        script = (
            textwrap.dedent(
                """
            const assert = require('node:assert/strict');
            const externalHints = {hidden: true, replaceChildren() {this.cleared = (this.cleared || 0) + 1;}};
            const elements = {
              query: {value: 'Old title'}, type: {value: 'auto'},
              externalBtn: {disabled: false}, externalHints,
            };
            const state = {
              inspection: {inspection_id: 'inspection-1'}, requestVersion: 4,
              externalController: null, pendingExternalKey: '',
            };
            const setButtonContent = () => {};
            const isNsfwOnly = () => false;
            const alerts = [];
            const window = {appAlert: value => alerts.push(value)};
            let resolveRequest;
            const api = () => new Promise(resolve => {resolveRequest = resolve;});
            let rendered = 0;
            const renderExternalHints = () => {rendered += 1; externalHints.hidden = false;};
            """
            )
            + invalidate_block
            + load_block
            + textwrap.dedent(
                """
            (async () => {
              const pending = loadExternalHints();
              await new Promise(setImmediate);
              elements.query.value = 'New title';
              state.requestVersion += 1;
              invalidateExternalHints();
              resolveRequest({hints: [{title: 'Old result'}]});
              await pending;
              assert.equal(rendered, 0);
              assert.equal(externalHints.hidden, true);
              assert.equal(elements.externalBtn.disabled, false);
              assert.equal(alerts.length, 0);
            })().catch(error => {console.error(error); process.exit(1);});
            """
            )
        )
        _run_node(script)

    def test_share_preview_is_invalidated_when_url_changes_mid_inspection(self):
        source = (ROOT / "app/templates/_share_transfer_scripts.html").read_text(
            encoding="utf-8"
        )
        block = _extract(source, "    let previewId='';", "\n    function renderTarget")
        script = (
            textwrap.dedent(
                """
            const assert = require('node:assert/strict');
            const elements = {
              shareUrl: {value: 'https://guangyapan.com/s/old'},
              shareStateTag: {innerHTML: '', className: ''},
              shareSelectionCount: {textContent: ''},
              shareCheckAll: {checked: false, indeterminate: false},
              restoreShareBtn: {disabled: false},
              shareTransferSummary: {textContent: ''},
              shareTargetDirName: {value: '目标目录'},
              shareFileToolbar: {hidden: true},
              shareFileList: {
                innerHTML: '', children: [],
                replaceChildren() {this.innerHTML = ''; this.children = [];},
                querySelectorAll() {return [];},
              },
              shareMessage: {textContent: ''},
              inspectShareBtn: {disabled: false, innerHTML: ''},
            };
            const document = {
              getElementById: id => elements[id],
              querySelectorAll: () => [],
            };
            const window = {renderLucideIcons() {}};
            let resolveInspect;
            const fetch = () => new Promise(resolve => {resolveInspect = resolve;});
            """
            )
            + block
            + textwrap.dedent(
                """
            (async () => {
              const pending = inspectShare();
              await new Promise(setImmediate);
              elements.shareUrl.value = 'https://guangyapan.com/s/new';
              invalidateSharePreview('分享链接已变化，请重新解析');
              resolveInspect({
                ok: true,
                json: async () => ({preview_id: 'old-preview', share_id: 'old', count: 1, files: [{id: '1'}]}),
              });
              await pending;
              assert.equal(previewId, '');
              assert.equal(previewUrl, '');
              assert.deepEqual(files, []);
              assert.equal(elements.shareFileToolbar.hidden, true);
              assert.equal(elements.restoreShareBtn.disabled, true);
              assert.equal(elements.inspectShareBtn.disabled, false);
              assert.match(elements.inspectShareBtn.innerHTML, /解析分享/);
              assert.match(elements.shareMessage.textContent, /重新解析/);
            })().catch(error => {console.error(error); process.exit(1);});
            """
            )
        )
        _run_node(script)

    def test_dashboard_config_requests_are_bound_to_modal_and_current_draft(self):
        source = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
        block = _extract(
            source,
            "    const modalEl=document.getElementById('mediaConfigModal');",
            "\n})();",
        )
        script = (
            textwrap.dedent(
                """
            const assert = require('node:assert/strict');
            function control(dataset = {}) {
              return {
                dataset, value: '', disabled: false, readOnly: false,
                tagName: 'INPUT', type: 'text', isConnected: true,
                attributes: {}, handlers: {},
                setAttribute(name, value) {this.attributes[name] = String(value);},
                addEventListener(name, handler) {this.handlers[name] = handler;},
              };
            }
            const urlField = control({key: 'JELLYFIN_URL'});
            const tokenField = control({key: 'JELLYFIN_API_KEY'});
            const state = {textContent: '', className: '', classList: {contains(name) {return state.className.split(/\\s+/).includes(name);}}};
            const detected = {textContent: '尚未识别', dataset: {}};
            const testButton = control({testMedia: 'jellyfin'});
            const saveButton = control({saveMedia: 'jellyfin'});
            const panel = {
              querySelector(selector) {
                if (selector === '[data-save-state]') return state;
                if (selector === '[data-media-detected]') return detected;
                if (selector === '[data-save-media]') return saveButton;
                if (selector.includes('JELLYFIN_URL')) return urlField;
                if (selector.includes('JELLYFIN_API_KEY')) return tokenField;
                return null;
              },
            };
            urlField.closest = tokenField.closest = selector => selector === '[data-tab-panel]' ? panel : null;
            const modalHandlers = {};
            const modalRoot = {
              hidden: true, attributes: {},
              setAttribute(name, value) {this.attributes[name] = String(value);},
              addEventListener(name, handler) {modalHandlers[name] = handler;},
              querySelectorAll(selector) {
                if (selector === '[data-tab-panel]') return [panel];
                if (selector === '[data-key]') return [urlField, tokenField];
                if (selector === '[data-test-media],[data-save-media]') return [testButton, saveButton];
                if (selector === '[data-media-detected]') return [detected];
                if (selector === '[data-test-media]') return [testButton];
                if (selector === '[data-save-media]') return [saveButton];
                return [];
              },
              querySelector(selector) {
                if (selector === '[data-save-state]') return state;
                if (selector === '[data-tab-panel="jellyfin"]') return panel;
                return null;
              },
            };
            const openButton = control({openMediaConfig: 'jellyfin'});
            const document = {
              activeElement: openButton,
              getElementById: id => id === 'mediaConfigModal' ? modalRoot : null,
              querySelectorAll: selector => selector === '[data-open-media-config]' ? [openButton] : [],
            };
            const tabsFixture = {activate() {}};
            const setupTabGroup = () => tabsFixture;
            let modalOptions = null;
            const createAppModal = (_element, options) => {
              modalOptions = options;
              return {open() {modalRoot.hidden = false;}, close() {modalRoot.hidden = true;}};
            };
            const configLoads = [];
            const loadAppConfig = ({signal} = {}) => new Promise(resolve => configLoads.push({resolve, signal}));
            const fillConfigFields = (_root, config) => {
              urlField.value = config.JELLYFIN_URL || '';
              tokenField.value = config.JELLYFIN_API_KEY || '';
            };
            let pendingFields = false;
            const collectConfigFields = () => pendingFields ? {JELLYFIN_URL: urlField.value} : {};
            const saveAppConfig = async () => ({});
            let resolveTest;
            const fetch = () => new Promise(resolve => {resolveTest = resolve;});
            let nextTimer = 0;
            const timers = new Map();
            const cancelledTimers = [];
            let reloads = 0;
            const window = {
              setTimeout(callback, delay) {const id = ++nextTimer; timers.set(id, {callback, delay}); return id;},
              clearTimeout(id) {cancelledTimers.push(id); timers.delete(id);},
              location: {reload() {reloads += 1;}},
            };
            """
            )
            + block
            + textwrap.dedent(
                """
            (async () => {
              const firstLoad = openConfig('jellyfin', openButton);
              await new Promise(setImmediate);
              closeConfig();
              const secondLoad = openConfig('jellyfin', openButton);
              await new Promise(setImmediate);
              configLoads[1].resolve({JELLYFIN_URL: 'https://new', JELLYFIN_API_KEY: 'new-token'});
              await secondLoad;
              assert.equal(urlField.value, 'https://new');
              urlField.value = 'https://draft';
              configLoads[0].resolve({JELLYFIN_URL: 'https://old', JELLYFIN_API_KEY: 'old-token'});
              await firstLoad;
              assert.equal(urlField.value, 'https://draft');

              urlField.value = 'https://tested';
              tokenField.value = 'token-a';
              const testRequest = testButton.handlers.click();
              await new Promise(setImmediate);
              urlField.value = 'https://changed';
              modalHandlers.input({target: urlField});
              resolveTest({ok: true, json: async () => ({server_name: 'Old', product: 'Jellyfin', version: '1', latency_ms: 2})});
              await testRequest;
              assert.equal(state.textContent, '配置已变化，请重新测试');
              assert.equal(detected.textContent, '尚未识别');
              assert.equal(testButton.disabled, false);

              pendingFields = false;
              const saveRequest = saveButton.handlers.click();
              await saveRequest;
              assert.equal(timers.size, 1);
              pendingFields = true;
              modalHandlers.input({target: urlField});
              assert.equal(timers.size, 0);
              assert.equal(cancelledTimers.length, 1);
              assert.equal(reloads, 0);
              assert.match(state.textContent, /存在新的未保存更改/);
              assert.equal(saveButton.disabled, false);
              assert.equal(saveButton.attributes['aria-busy'], 'false');
              assert.ok(modalOptions.onRequestClose);
            })().catch(error => {console.error(error); process.exit(1);});
            """
            )
        )
        _run_node(script)


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
              disabled: false,
              isConnected: true,
              getAttribute: () => null,
              matches: (selector) => selector === ':disabled' && elements[name].disabled,
              closest: () => null,
              focus: () => {{if (!elements[name].disabled) document.activeElement = elements[name];}},
            }});
            const elements = {{outer: null, parentFirst: null, parentLast: null, childFirst: null, childLast: null}};
            Object.keys(elements).forEach((name) => {{elements[name] = makeFocusable(name);}});
            function makeModal(first, last) {{
              const dialog = {{
                querySelectorAll: () => [first, last].filter((element) => !element.disabled),
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

            parentLifecycle.open(elements.outer, {{initialFocus: elements.parentFirst}});
            frames.shift()();
            childLifecycle.open(elements.parentFirst, {{initialFocus: elements.childFirst}});
            frames.shift()();
            elements.parentFirst.disabled = true;
            childLifecycle.close();
            assert.equal(document.activeElement, elements.parentLast);
            elements.parentFirst.disabled = false;
            parentLifecycle.close();
            """
        )
        _run_node(script)


class AgentVisualViewportLifecycleTests(unittest.TestCase):
    def test_visual_viewport_height_is_applied_without_layout_delay(self):
        source = (ROOT / "app/static/js/agent.js").read_text(encoding="utf-8")
        viewport_block = _extract(
            source,
            "    function syncViewportHeight() {",
            "\n\n    composer?.addEventListener",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const styleValues = {{}};
            const window = {{visualViewport: {{height: 700}}, innerHeight: 800}};
            const document = {{
              documentElement: {{style: {{setProperty: (name, value) => {{styleValues[name] = value;}}}}}},
            }};
            {viewport_block}
            syncViewportHeight();
            assert.equal(styleValues['--agent-viewport-height'], '700px');
            window.visualViewport.height = 643.6;
            syncViewportHeight();
            assert.equal(styleValues['--agent-viewport-height'], '644px');
            """
        )
        _run_node(script)
        self.assertIn(
            "window.visualViewport?.addEventListener('resize', syncViewportHeight",
            source,
        )
        self.assertIn("window.addEventListener('resize', syncViewportHeight", source)


class AgentTranscriptLifecycleTests(unittest.TestCase):
    def test_transcript_pruning_keeps_the_latest_bounded_history(self):
        source = (ROOT / "app/static/js/agent.js").read_text(encoding="utf-8")
        prune_block = _extract(
            source,
            "    function pruneTranscript() {",
            "\n\n    function appendMessage",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('node:assert/strict');
            const MAX_TRANSCRIPT_ITEMS = 120;
            const transcript = {{children: []}};
            Object.defineProperty(transcript, 'firstElementChild', {{
              get: () => transcript.children[0] || null,
            }});
            const message = (id) => {{
              const item = {{
                id,
                remove: () => {{transcript.children.splice(transcript.children.indexOf(item), 1);}},
              }};
              return item;
            }};
            {prune_block}
            transcript.children = Array.from({{length: 122}}, (_, index) => message(index));
            pruneTranscript();
            assert.equal(transcript.children.length, 120);
            assert.equal(transcript.children[0].id, 2);
            assert.equal(transcript.children.at(-1).id, 121);
            """
        )
        _run_node(script)


if __name__ == "__main__":
    unittest.main()
