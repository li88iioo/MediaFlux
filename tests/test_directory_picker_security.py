"""目录选择器虚拟根拦截与交互防护单元测试。"""
from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
LOCAL_MEDIA_JS = ROOT / "app" / "static" / "js" / "local-media.js"


class DirectoryPickerSecurityCodeContractTests(unittest.TestCase):
    def test_app_js_has_virtual_root_guard_contracts(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("function isVirtualRootId(id)", js)
        self.assertIn("raw === 'undefined' || raw === 'null'", js)
        self.assertIn("const isVirtual = isVirtualRootId(current.id);", js)
        self.assertIn(
            "const isDisallowedRoot = !allowRoot && String(current.id) === String(options.rootId || '0');",
            js,
        )
        self.assertIn("selectCurrent.disabled = disabled;", js)
        self.assertIn("虚拟根入口不可选，请进入具体驱动器或目录", js)
        self.assertIn("当前根目录不可选，请选择子目录", js)
        self.assertIn("if (isVirtualRootId(value.id)) return;", js)
        self.assertIn("directory.file_id ?? directory.id ?? directory.path", js)
        self.assertNotIn(
            "function updateSelectionControls() {\n            if (!multiple) return;",
            js,
        )

    def test_local_media_js_has_virtual_root_and_allow_root_contracts(self):
        js = LOCAL_MEDIA_JS.read_text(encoding="utf-8")
        self.assertIn("const isRootsMode = effectiveRootId === '__roots__';", js)
        self.assertIn("allowRoot: !isRootsMode && Boolean(sourceId || networkRoot),", js)
        self.assertIn("lower === 'undefined' || lower === 'null'", js)
        self.assertIn("input.value = safeId;", js)
        self.assertIn("input.dispatchEvent(new Event('input', {bubbles: true}));", js)
        self.assertIn("input.dispatchEvent(new Event('change', {bubbles: true}));", js)


class DirectoryPickerSecurityNodeHarnessTests(unittest.TestCase):
    @staticmethod
    def _run_node_script(script: str):
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Node.js script failed with code {result.returncode}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return json.loads(result.stdout.strip()) if result.stdout.strip() else {}

    def test_directory_picker_node_simulation(self):
        harness = textwrap.dedent(
            r"""
            const fs = require('fs');
            const path = require('path');

            // Minimal DOM Mocking
            class MockElement {
                constructor(tagName = 'div') {
                    this.tagName = tagName.toUpperCase();
                    this.children = [];
                    this.childNodes = this.children;
                    this.classList = {
                        _classes: new Set(),
                        add(c) { this._classes.add(c); },
                        remove(c) { this._classes.delete(c); },
                        toggle(c, force) {
                            if (force === undefined) {
                                if (this._classes.has(c)) this._classes.delete(c);
                                else this._classes.add(c);
                            } else if (force) this._classes.add(c);
                            else this._classes.delete(c);
                        },
                        contains(c) { return this._classes.has(c); }
                    };
                    this.dataset = {};
                    this.attributes = {};
                    this.style = {};
                    this._listeners = {};
                    this.hidden = false;
                    this.disabled = false;
                    this.textContent = '';
                    this.title = '';
                    this.value = '';
                    this.scrollWidth = 0;
                    this.scrollLeft = 0;
                }
                appendChild(child) {
                    this.children.push(child);
                    child.parentElement = this;
                    return child;
                }
                append(...children) {
                    for (const c of children) this.appendChild(c);
                }
                replaceChildren(...children) {
                    this.children = [];
                    for (const c of children) this.appendChild(c);
                }
                remove() {
                    if (this.parentElement) {
                        const idx = this.parentElement.children.indexOf(this);
                        if (idx !== -1) this.parentElement.children.splice(idx, 1);
                    }
                }
                setAttribute(k, v) { this.attributes[k] = String(v); }
                getAttribute(k) { return this.attributes[k] || null; }
                addEventListener(evt, fn) {
                    if (!this._listeners[evt]) this._listeners[evt] = [];
                    this._listeners[evt].push(fn);
                }
                dispatchEvent(event) {
                    const handlers = this._listeners[event.type] || [];
                    for (const h of handlers) h(event);
                    return true;
                }
                querySelector(selector) {
                    return this._query(selector);
                }
                querySelectorAll(selector) {
                    const results = [];
                    this._queryAll(selector, results);
                    return results;
                }
                _query(selector) {
                    const all = [];
                    this._queryAll(selector, all);
                    return all[0] || null;
                }
                _queryAll(selector, list) {
                    const match = (el) => {
                        if (selector.startsWith('[data-dir-title]') && el.dataset.dirTitle !== undefined) return true;
                        if (selector.startsWith('[data-dir-up]') && el.dataset.dirUp !== undefined) return true;
                        if (selector.startsWith('[data-dir-breadcrumb]') && el.dataset.dirBreadcrumb !== undefined) return true;
                        if (selector.startsWith('[data-dir-list]') && el.dataset.dirList !== undefined) return true;
                        if (selector.startsWith('[data-dir-current]') && el.dataset.dirCurrent !== undefined) return true;
                        if (selector.startsWith('[data-dir-selection-count]') && el.dataset.dirSelectionCount !== undefined) return true;
                        if (selector.startsWith('[data-dir-select-current]') && el.dataset.dirSelectCurrent !== undefined) return true;
                        if (selector.startsWith('[data-dir-confirm]') && el.dataset.dirConfirm !== undefined) return true;
                        if (selector.startsWith('[data-dir-close]') && el.dataset.dirClose !== undefined) return true;
                        if (selector === '.settings-dir-dialog') return el.classList.contains('settings-dir-dialog');
                        return false;
                    };
                    for (const child of this.children) {
                        if (match(child)) list.push(child);
                        child._queryAll(selector, list);
                    }
                }
                set innerHTML(html) {
                    const dialog = new MockElement('div');
                    dialog.classList.add('settings-dir-dialog');

                    const title = new MockElement('strong');
                    title.dataset.dirTitle = '';
                    const close = new MockElement('button');
                    close.dataset.dirClose = '';
                    const up = new MockElement('button');
                    up.dataset.dirUp = '';
                    const breadcrumb = new MockElement('nav');
                    breadcrumb.dataset.dirBreadcrumb = '';
                    const list = new MockElement('div');
                    list.dataset.dirList = '';
                    const current = new MockElement('span');
                    current.dataset.dirCurrent = '';
                    const count = new MockElement('span');
                    count.dataset.dirSelectionCount = '';
                    const selectCurrent = new MockElement('button');
                    selectCurrent.dataset.dirSelectCurrent = '';
                    const confirm = new MockElement('button');
                    confirm.dataset.dirConfirm = '';

                    dialog.append(title, close, up, breadcrumb, list, current, count, selectCurrent, confirm);
                    this.replaceChildren(dialog);
                }
            }

            class MockEvent {
                constructor(type, init = {}) {
                    this.type = type;
                    this.bubbles = init.bubbles || false;
                }
            }

            global.Event = MockEvent;
            global.CustomEvent = MockEvent;
            global.HTMLElement = MockElement;
            global.HTMLInputElement = MockElement;
            global.Headers = class { constructor() {} set() {} };
            global.MutationObserver = class { observe() {} disconnect() {} };
            global.document = {
                documentElement: new MockElement('html'),
                getElementById(id) { return null; },
                createElement(tag) { return new MockElement(tag); },
                querySelector(sel) { return null; },
                querySelectorAll(sel) { return []; },
                removeEventListener() {},
                addEventListener() {},
                dispatchEvent() { return true; },
                body: new MockElement('body'),
            };
            global.window = global;
            global.addEventListener = () => {};
            global.removeEventListener = () => {};
            global.innerWidth = 1200;
            global.innerHeight = 800;
            global.localStorage = { getItem: () => null, setItem: () => null };
            global.matchMedia = () => ({ matches: false, addEventListener() {} });
            global.lucide = { createIcons() {} };

            // Load app.js
            const appJsCode = fs.readFileSync(path.join(__dirname, 'app/static/js/app.js'), 'utf-8');
            eval(appJsCode);

            const testResults = {};

            // TEST 1: Initial state at __roots__ should have selectCurrent disabled
            let selectedValue = null;
            const picker1 = window.openGuangYaDirectoryPicker({
                rootId: '__roots__',
                rootName: '本机目录',
                allowRoot: false,
                fetchDirectory: async (id) => {
                    if (id === '__roots__') {
                        return [
                            { file_id: 'C:\\', name: 'C: (系统盘)', is_dir: true },
                            { file_id: 'D:\\', name: 'D: (数据盘)', is_dir: true },
                        ];
                    }
                    return [];
                },
                onSelect: (val) => { selectedValue = val; }
            });

            const btnSelectCurrent = picker1.modal.querySelector('[data-dir-select-current]');
            testResults.test1_initial_disabled = btnSelectCurrent.disabled;
            testResults.test1_initial_title = btnSelectCurrent.title;

            // Trigger select current button click at __roots__
            const clickHandlers = btnSelectCurrent._listeners['click'] || [];
            clickHandlers.forEach(fn => fn());
            testResults.test1_click_roots_blocked = (selectedValue === null);

            // TEST 2: Navigate to C:\ and verify button enabled
            picker1.navigator.enter({ id: 'C:\\', name: 'C: (系统盘)' }).then(() => {
                testResults.test2_drive_disabled = btnSelectCurrent.disabled;
                testResults.test2_drive_title = btnSelectCurrent.title;

                // Click select current now
                clickHandlers.forEach(fn => fn());
                testResults.test2_selected_value = selectedValue;

                // Output results as JSON
                console.log(JSON.stringify(testResults));
            });
            """
        )
        results = self._run_node_script(harness)
        self.assertTrue(results.get("test1_initial_disabled"))
        self.assertIn("虚拟根入口不可选", results.get("test1_initial_title", ""))
        self.assertTrue(results.get("test1_click_roots_blocked"))
        self.assertFalse(results.get("test2_drive_disabled"))
        self.assertEqual(results.get("test2_drive_title"), "")
        self.assertEqual(results.get("test2_selected_value", {}).get("id"), "C:\\")

    def test_local_media_on_select_rejects_virtual_roots(self):
        harness = textwrap.dedent(
            r"""
            const fs = require('fs');
            const path = require('path');

            class MockElement {
                constructor(tagName = 'div') {
                    this.tagName = tagName.toUpperCase();
                    this.value = '';
                    this._events = [];
                    this._listeners = {};
                }
                addEventListener(evt, fn) {
                    if (!this._listeners[evt]) this._listeners[evt] = [];
                    this._listeners[evt].push(fn);
                }
                dispatchEvent(event) {
                    this._events.push(event.type);
                    const handlers = this._listeners[event.type] || [];
                    for (const h of handlers) h(event);
                    return true;
                }
            }
            class MockEvent {
                constructor(type, init = {}) {
                    this.type = type;
                    this.bubbles = init.bubbles || false;
                }
            }
            global.Event = MockEvent;

            let capturedOptions = null;
            global.window = {
                openGuangYaDirectoryPicker(options) {
                    capturedOptions = options;
                }
            };
            function windowsUncRoot(val) {
                return val && val.startsWith('\\\\') ? val : '';
            }
            global.$ = () => null;

            function openLocalDirectoryPicker(input, {sourceId = 0, rootId = '__roots__', rootName = '本机目录'} = {}) {
                if (!window.openGuangYaDirectoryPicker) return;
                const currentValue = String(input?.value || '').trim();
                const networkRoot = windowsUncRoot(currentValue);
                const smbUser = '';
                const smbPass = '';
                const effectiveRootId = sourceId ? rootId : '__roots__';
                const effectiveRootName = sourceId ? rootName : '本机目录';
                const isRootsMode = effectiveRootId === '__roots__';

                window.openGuangYaDirectoryPicker({
                    modalId: 'localMediaDirModal',
                    title: networkRoot ? '选择 SMB 网络目录' : '选择本地目录',
                    rootId: effectiveRootId,
                    rootName: effectiveRootName,
                    allowRoot: !isRootsMode && Boolean(sourceId || networkRoot),
                    fetchDirectory: async (path) => [],
                    onSelect: (directory) => {
                        if (!directory || !directory.id || directory.id === '__roots__' || directory.id === '0') return false;
                        input.value = directory.id;
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        input.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    },
                });
            }

            const input = new MockElement('input');
            openLocalDirectoryPicker(input);

            const allowRootInitial = capturedOptions.allowRoot;
            const res1 = capturedOptions.onSelect({ id: '__roots__', name: '本机目录' });
            const val1 = input.value;
            const events1 = [...input._events];

            const res2 = capturedOptions.onSelect({ id: 'D:\\Media', name: 'Media' });
            const val2 = input.value;
            const events2 = [...input._events];

            console.log(JSON.stringify({
                allowRootInitial,
                res1,
                val1,
                events1,
                res2,
                val2,
                events2,
            }));
            """
        )
        results = self._run_node_script(harness)
        self.assertFalse(results.get("allowRootInitial"))
        self.assertFalse(results.get("res1"))
        self.assertEqual(results.get("val1"), "")
        self.assertEqual(results.get("events1"), [])
        self.assertTrue(results.get("res2"))
        self.assertEqual(results.get("val2"), "D:\\Media")
        self.assertEqual(results.get("events2"), ["input", "change"])


if __name__ == "__main__":
    unittest.main()
