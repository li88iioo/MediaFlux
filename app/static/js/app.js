// MediaFlux 公共前端交互
(function () {
    const THEME_STORAGE_KEY = 'mediaflux.theme.mode';
    const THEME_MODES = ['auto', 'light', 'dark'];
    const THEME_META = {
        auto: {label: '自动', icon: 'monitor-cog', next: '浅色'},
        light: {label: '浅色', icon: 'sun', next: '深色'},
        dark: {label: '深色', icon: 'moon', next: '自动'},
    };
    const systemTheme = window.matchMedia?.('(prefers-color-scheme: dark)');

    const globalSearchURL = document.querySelector('meta[name="global-search-url"]')?.content || '/search';

    document.addEventListener('keydown', (event) => {
        if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
        const input = document.querySelector('#dashboardSearchInput, #globalSearchInput');
        event.preventDefault();
        if (input && !input.disabled) {
            input.focus();
            input.select?.();
            return;
        }
        window.location.assign(globalSearchURL);
    });


    function resolveTheme(mode) {
        return mode === 'auto' ? (systemTheme?.matches ? 'dark' : 'light') : mode;
    }

    function storedThemeMode() {
        try {
            const mode = localStorage.getItem(THEME_STORAGE_KEY);
            return THEME_MODES.includes(mode) ? mode : 'auto';
        } catch (_) {
            return document.documentElement.dataset.themeMode || 'auto';
        }
    }

    function renderIcons(root) {
        if (!window.lucide) return;
        window.lucide.createIcons({
            attrs: {'stroke-width': 1.8, 'aria-hidden': 'true'},
            nameAttr: 'data-lucide',
            root: root || document,
        });
    }

    let secretInputSequence = 0;

    function paintSecretInput(input, button, visible) {
        input.type = visible ? 'text' : 'password';
        button.setAttribute('aria-pressed', String(visible));
        button.setAttribute('aria-label', visible ? '隐藏敏感内容' : '显示本次输入');
        button.title = button.getAttribute('aria-label');
        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', visible ? 'eye-off' : 'eye');
        icon.setAttribute('aria-hidden', 'true');
        button.replaceChildren(icon);
        renderIcons(button);
    }

    function syncSecretControls(input) {
        const shell = input.closest('.secret-input-shell');
        if (!shell) return;
        const toggle = shell.querySelector('.secret-input-toggle');
        const clear = shell.querySelector('.secret-input-clear');
        const state = input.dataset.secretState || 'empty';
        const hasDraft = Boolean(input.value);
        if (toggle) {
            toggle.disabled = !hasDraft;
            toggle.title = hasDraft
                ? (input.type === 'text' ? '隐藏敏感内容' : '显示本次输入')
                : (state === 'saved' ? '已保存的密钥不会从服务端回传' : '请先输入敏感内容');
            toggle.setAttribute('aria-label', toggle.title);
        }
        if (clear) {
            clear.hidden = state !== 'saved';
            clear.disabled = input.dataset.managedByEnvironment === 'true';
        }
        shell.classList.toggle('has-saved-secret', state === 'saved' && !hasDraft);
    }

    function enhanceSecretInput(input) {
        if (!(input instanceof HTMLInputElement)) return;
        if (input.type !== 'password' || input.getAttribute('data-secret-toggle') === 'off') return;
        if (input.getAttribute('data-secret-enhanced') === 'true') return;
        input.setAttribute('data-secret-enhanced', 'true');
        input.dataset.secretField = 'true';
        input.dataset.secretState = 'empty';
        input.classList.add('has-secret-toggle');
        if (!input.id) {
            secretInputSequence += 1;
            input.id = `secret-input-${secretInputSequence}`;
        }

        let shell = input.parentElement?.classList.contains('secret-input-shell')
            ? input.parentElement
            : null;
        if (!shell) {
            shell = document.createElement('span');
            shell.className = 'secret-input-shell';
            input.before(shell);
            shell.append(input);
        }

        if (input.dataset.secretClearable !== 'false') {
            const clear = document.createElement('button');
            clear.type = 'button';
            clear.className = 'secret-input-clear';
            clear.hidden = true;
            clear.setAttribute('aria-label', '清除已保存凭据');
            clear.title = '清除已保存凭据';
            clear.innerHTML = '<i data-lucide="x"></i>';
            clear.addEventListener('click', () => {
                input.value = '';
                input.type = 'password';
                input.dataset.secretState = 'clear';
                input.placeholder = '保存后清除';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                syncSecretControls(input);
                input.focus({preventScroll: true});
            });
            shell.append(clear);
        }

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'secret-input-toggle';
        button.setAttribute('aria-controls', input.id);
        button.setAttribute('aria-pressed', 'false');
        button.addEventListener('click', () => {
            if (!input.value) return;
            paintSecretInput(input, button, input.type === 'password');
            input.focus({preventScroll: true});
        });
        input.addEventListener('input', () => {
            if (input.value) input.dataset.secretState = 'draft';
            else if (input.dataset.secretState === 'draft') input.dataset.secretState = 'empty';
            syncSecretControls(input);
        });
        shell.append(button);
        paintSecretInput(input, button, false);
        syncSecretControls(input);
    }

    function enhanceSecretInputs(root = document) {
        const selector = 'input[type="password"]';
        if (root instanceof HTMLInputElement && root.matches(selector)) {
            enhanceSecretInput(root);
        }
        root.querySelectorAll?.(selector).forEach(enhanceSecretInput);
    }

    function updateThemeControls(mode) {
        const meta = THEME_META[mode];
        document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
            button.dataset.themeMode = mode;
            button.setAttribute('aria-label', `主题模式：${meta.label}`);
            button.title = `当前${meta.label}，点击切换${meta.next}`;
            button.innerHTML = `<i data-lucide="${meta.icon}"></i><span class="sr-only" data-theme-label aria-live="polite">主题模式：${meta.label}</span>`;
            renderIcons(button);
        });
    }

    function applyThemeMode(mode, persist = false) {
        const safeMode = THEME_MODES.includes(mode) ? mode : 'auto';
        const theme = resolveTheme(safeMode);
        document.documentElement.dataset.themeMode = safeMode;
        document.documentElement.dataset.theme = theme;
        document.documentElement.style.colorScheme = theme;
        if (persist) {
            try {
                localStorage.setItem(THEME_STORAGE_KEY, safeMode);
            } catch (_) {}
        }
        updateThemeControls(safeMode);
        document.dispatchEvent(new CustomEvent('mediaflux:themechange', {
            detail: {mode: safeMode, theme},
        }));
    }

    window.renderLucideIcons = renderIcons;
    window.mediaFluxTheme = {
        getMode: () => document.documentElement.dataset.themeMode || 'auto',
        getTheme: () => document.documentElement.dataset.theme || resolveTheme('auto'),
        setMode: (mode) => applyThemeMode(mode, true),
    };

    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        button.addEventListener('click', () => {
            const mode = window.mediaFluxTheme.getMode();
            const index = Math.max(0, THEME_MODES.indexOf(mode));
            applyThemeMode(THEME_MODES[(index + 1) % THEME_MODES.length], true);
        });
    });
    const handleSystemThemeChange = () => {
        if (window.mediaFluxTheme.getMode() === 'auto') applyThemeMode('auto');
    };
    if (systemTheme?.addEventListener) {
        systemTheme.addEventListener('change', handleSystemThemeChange);
    } else {
        systemTheme?.addListener?.(handleSystemThemeChange);
    }
    applyThemeMode(storedThemeMode());
    enhanceSecretInputs();
    renderIcons();

    const iconObserver = new MutationObserver((mutations) => {
        const iconRoots = new Set();
        mutations.forEach((mutation) => [...mutation.addedNodes].forEach((node) => {
            if (node.nodeType !== 1) return;
            enhanceSecretInputs(node);
            if (node.matches?.('i[data-lucide]')) iconRoots.add(node.parentElement || node);
            else if (node.querySelector?.('i[data-lucide]')) iconRoots.add(node);
        }));
        iconRoots.forEach((root) => {
            if (root?.isConnected) renderIcons(root);
        });
    });
    iconObserver.observe(document.body, {childList: true, subtree: true});

    const nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        const options = {...(init || {})};
        const method = String(options.method || 'GET').toUpperCase();
        const url = typeof input === 'string' ? input : input.url;
        const sameOrigin = new URL(url, window.location.href).origin === window.location.origin;
        if (sameOrigin && !['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes(method)) {
            const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
            options.headers = new Headers(options.headers || {});
            options.headers.set('X-CSRF-Token', token);
        }
        return nativeFetch(input, options);
    };

    window.createGuangYaDirectoryNavigator = function (options) {
        let currentId = String(options.rootId || '0');
        let currentPath = [];
        let requestVersion = 0;

        function snapshot() {
            return {
                id: currentId,
                path: currentPath.map((node) => ({...node})),
                isRoot: currentPath.length === 0,
            };
        }

        async function load(targetId, targetPath) {
            const version = ++requestVersion;
            const nextId = String(targetId || options.rootId || '0');
            const nextPath = (targetPath || []).map((node) => ({
                id: String(node.id),
                name: String(node.name || ''),
            }));
            options.onLoading?.({id: nextId, path: nextPath});
            try {
                const items = await options.fetchDirectory(nextId);
                if (version !== requestVersion) return false;
                if (!Array.isArray(items)) throw new Error('目录数据格式无效');
                currentId = nextId;
                currentPath = nextPath;
                const state = snapshot();
                options.onPathChange?.(state);
                options.onLoaded?.(items, state);
                return true;
            } catch (error) {
                if (version !== requestVersion) return false;
                options.onError?.(error, snapshot());
                return false;
            }
        }

        return {
            load,
            root: () => load(String(options.rootId || '0'), []),
            reload: () => load(currentId, currentPath),
            enter: (node) => load(String(node.id), [...currentPath, node]),
            up: () => currentPath.length
                ? load(
                    currentPath.length > 1 ? currentPath[currentPath.length - 2].id : String(options.rootId || '0'),
                    currentPath.slice(0, -1),
                )
                : Promise.resolve(false),
            goTo: (index) => index < 0
                ? load(String(options.rootId || '0'), [])
                : load(currentPath[index].id, currentPath.slice(0, index + 1)),
            state: snapshot,
            cancel: () => { requestVersion += 1; },
        };
    };

    window.renderDirectoryBreadcrumb = function (container, path, onNavigate) {
        if (!container) return;
        container.replaceChildren();
        const nodes = [{id: '0', name: '根目录'}, ...(path || [])];
        nodes.forEach((node, index) => {
            if (index > 0) {
                const separator = document.createElement('span');
                separator.className = 'dir-breadcrumb-separator';
                separator.setAttribute('aria-hidden', 'true');
                separator.textContent = '/';
                container.appendChild(separator);
            }
            const isCurrent = index === nodes.length - 1;
            if (isCurrent) {
                const current = document.createElement('span');
                current.className = 'dir-breadcrumb-current';
                current.textContent = node.name;
                current.title = node.name;
                container.appendChild(current);
                return;
            }
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'dir-breadcrumb-link';
            button.textContent = node.name;
            button.title = `返回 ${node.name}`;
            button.addEventListener('click', () => onNavigate(index - 1));
            container.appendChild(button);
        });
        container.scrollLeft = container.scrollWidth;
    };

    window.openGuangYaDirectoryPicker = function (options) {
        function isVirtualRootId(id) {
            const raw = String(id || '').trim().toLowerCase();
            return !raw || raw === '__roots__' || raw === '0' || raw === 'undefined' || raw === 'null' || raw === '[object object]';
        }
        const modalId = options.modalId || 'guangyaDirModal';
        const multiple = options.multiple === true;
        const allowRoot = options.allowRoot !== false;
        const selected = new Map(
            (Array.isArray(options.selected) ? options.selected : [])
                .filter((item) => item && item.id && !isVirtualRootId(item.id))
                .map((item) => [String(item.id), {
                    id: String(item.id),
                    name: String(item.name || ''),
                    path: Array.isArray(item.path) ? item.path.map((part) => ({...part})) : [],
                }]),
        );
        document.getElementById(modalId)?.remove();

        const modal = document.createElement('div');
        modal.id = modalId;
        modal.className = 'settings-dir-modal';
        modal.innerHTML = `<div class="card card-pad settings-dir-dialog" role="dialog" aria-modal="true">
            <div class="settings-dir-head">
                <strong data-dir-title></strong>
                <button type="button" class="icon-btn" data-dir-close title="关闭" aria-label="关闭"><i data-lucide="x"></i></button>
            </div>
            <div class="dir-browser-toolbar">
                <button type="button" class="jump-btn dir-browser-up" data-dir-up disabled title="返回上一级"><i data-lucide="arrow-up"></i>上一级</button>
                <nav class="dir-breadcrumb" data-dir-breadcrumb aria-label="当前目录路径"></nav>
            </div>
            <div class="settings-dir-list" data-dir-list></div>
            <div class="settings-dir-footer">
                <span class="text-muted" data-dir-current></span>
                <div class="settings-dir-footer-actions">
                    <span class="settings-dir-selection-count" data-dir-selection-count hidden></span>
                    <button type="button" class="jump-btn settings-dir-current" data-dir-select-current>选择当前目录</button>
                    <button type="button" class="btn btn-primary settings-dir-confirm" data-dir-confirm hidden>确认选择</button>
                </div>
            </div>
        </div>`;

        const title = modal.querySelector('[data-dir-title]');
        const upButton = modal.querySelector('[data-dir-up]');
        const breadcrumb = modal.querySelector('[data-dir-breadcrumb]');
        const list = modal.querySelector('[data-dir-list]');
        const currentLabel = modal.querySelector('[data-dir-current]');
        const selectionCount = modal.querySelector('[data-dir-selection-count]');
        const selectCurrent = modal.querySelector('[data-dir-select-current]');
        const confirm = modal.querySelector('[data-dir-confirm]');
        title.textContent = options.title || (multiple ? '选择多个目录' : '选择目录');
        selectionCount.hidden = !multiple;
        confirm.hidden = !multiple;

        let navigator;
        let lastItems = [];
        let lastState = {path: [], isRoot: true};
        function close() {
            navigator?.cancel();
            modal.remove();
            document.removeEventListener('keydown', onKeydown);
        }
        function onKeydown(event) {
            if (event.key === 'Escape') close();
        }
        function normalizedNode(node) {
            const rawId = node ? (node.id ?? node.file_id ?? node.path ?? '') : '';
            const safeId = rawId !== undefined && rawId !== null ? String(rawId).trim() : '';
            return {
                id: safeId,
                name: String(node?.name || safeId || ''),
                path: (node?.path || []).map((part) => ({...part})),
            };
        }
        function currentNode() {
            const state = navigator.state();
            if (state.path.length) {
                const last = state.path[state.path.length - 1];
                return {...last, path: state.path};
            }
            const fallbackId = options.rootId !== undefined && options.rootId !== null ? String(options.rootId) : '0';
            return {
                id: fallbackId,
                name: options.rootName || '根目录',
                path: [],
            };
        }
        function updateSelectionControls() {
            const current = currentNode();
            const isVirtual = isVirtualRootId(current.id);
            const isDisallowedRoot = !allowRoot && String(current.id) === String(options.rootId || '0');
            const disabled = isVirtual || isDisallowedRoot;
            selectCurrent.disabled = disabled;
            if (isVirtual) {
                selectCurrent.title = '虚拟根入口不可选，请进入具体驱动器或目录';
            } else if (isDisallowedRoot) {
                selectCurrent.title = '当前根目录不可选，请选择子目录';
            } else {
                selectCurrent.title = '';
            }

            if (!multiple) {
                selectCurrent.textContent = '选择当前目录';
                selectCurrent.classList.remove('is-selected');
                return;
            }

            selectionCount.textContent = `已选择 ${selected.size} 个`;
            confirm.disabled = selected.size === 0;
            const selectedCurrent = selected.has(String(current.id));
            selectCurrent.textContent = selectedCurrent ? '取消当前目录' : '选择当前目录';
            selectCurrent.classList.toggle('is-selected', selectedCurrent);
        }
        function commitSelection(value) {
            if (Array.isArray(value)) {
                const filtered = value.filter((item) => item && !isVirtualRootId(item.id));
                if (filtered.length === 0 && value.length > 0) return;
                const accepted = options.onSelect?.(filtered);
                if (accepted !== false) close();
                return;
            }
            if (!value || isVirtualRootId(value.id)) return;
            const accepted = options.onSelect?.(value);
            if (accepted !== false) close();
        }
        function selectNode(node) {
            const value = normalizedNode(node);
            if (isVirtualRootId(value.id)) return;
            if (!allowRoot && value.id === String(options.rootId || '0')) return;
            if (!multiple) {
                commitSelection(value);
                return;
            }
            if (selected.has(value.id)) selected.delete(value.id);
            else selected.set(value.id, value);
            renderItems(lastItems, lastState);
            updateSelectionControls();
        }
        function renderItems(items, state) {
            lastItems = items;
            lastState = state;
            list.replaceChildren();
            const dirs = items.filter((item) => item && item.is_dir);
            if (!dirs.length) {
                const empty = document.createElement('div');
                empty.className = 'empty-state';
                const text = document.createElement('p');
                const current = currentNode();
                if (isVirtualRootId(current.id)) {
                    text.textContent = '当前没有可选择的驱动器或目录';
                } else {
                    text.textContent = allowRoot || state.path.length ? '无子目录，可选择当前目录' : '当前没有可选择的子目录';
                }
                empty.appendChild(text);
                list.appendChild(empty);
                updateSelectionControls();
                return;
            }
            dirs.forEach((directory) => {
                const rawId = directory ? (directory.file_id ?? directory.id ?? directory.path ?? '') : '';
                const dirId = rawId !== undefined && rawId !== null ? String(rawId).trim() : '';
                const dirName = String(directory?.name || dirId || '未命名目录');
                const value = {
                    id: dirId,
                    name: dirName,
                    path: [...state.path, {
                        id: dirId,
                        name: dirName,
                    }],
                };
                const checked = selected.has(value.id);
                const row = document.createElement('div');
                row.className = 'settings-dir-item';
                row.classList.toggle('is-selected', multiple && checked);
                const open = document.createElement('button');
                open.type = 'button';
                open.className = 'settings-dir-open';
                open.textContent = value.name;
                open.title = `进入 ${open.textContent}`;
                open.addEventListener('click', () => navigator.enter({id: value.id, name: value.name}));
                const choose = document.createElement('button');
                choose.type = 'button';
                choose.className = 'jump-btn settings-dir-select';
                choose.classList.toggle('is-selected', multiple && checked);
                choose.textContent = multiple ? (checked ? '已选择' : '选择') : '选择';
                choose.setAttribute('aria-pressed', multiple ? String(checked) : 'false');
                choose.addEventListener('click', () => selectNode(value));
                row.append(open, choose);
                list.appendChild(row);
            });
            updateSelectionControls();
        }

        navigator = window.createGuangYaDirectoryNavigator({
            rootId: options.rootId || '0',
            fetchDirectory: options.fetchDirectory || (async (id) => {
                const response = await fetch(`/api/guangya/dirs?parent_id=${encodeURIComponent(id)}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '目录加载失败');
                return data;
            }),
            onLoading: () => {
                list.replaceChildren();
                const loading = document.createElement('div');
                loading.className = 'empty-state';
                const text = document.createElement('p');
                text.textContent = '加载中...';
                loading.appendChild(text);
                list.appendChild(loading);
            },
            onPathChange: (state) => {
                window.renderDirectoryBreadcrumb(breadcrumb, state.path, (index) => navigator.goTo(index));
                upButton.disabled = state.isRoot;
                currentLabel.textContent = `当前：${state.path.length ? state.path[state.path.length - 1].name : (options.rootName || '根目录')}`;
                updateSelectionControls();
            },
            onLoaded: renderItems,
            onError: (error) => {
                list.replaceChildren();
                const empty = document.createElement('div');
                empty.className = 'empty-state';
                const text = document.createElement('p');
                text.textContent = error.message || '目录加载失败';
                empty.appendChild(text);
                list.appendChild(empty);
            },
        });

        modal.querySelector('[data-dir-close]').addEventListener('click', close);
        modal.addEventListener('click', (event) => { if (event.target === modal) close(); });
        upButton.addEventListener('click', () => navigator.up());
        selectCurrent.addEventListener('click', () => selectNode(currentNode()));
        confirm.addEventListener('click', () => commitSelection(Array.from(selected.values())));
        document.addEventListener('keydown', onKeydown);
        document.body.appendChild(modal);
        window.renderLucideIcons?.(modal);
        updateSelectionControls();
        navigator.root();
        return {modal, navigator, close};
    };

    window.loadAppConfig = async function () {
        const response = await fetch('/api/config');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '配置加载失败');
        return data;
    };

    window.fillConfigFields = function (root, config) {
        const managedFields = new Set(
            Array.isArray(config.__managed_fields) ? config.__managed_fields : []
        );
        root.querySelectorAll('[data-key]').forEach((field) => {
            const key = field.dataset.key;
            const value = config[key] || '';
            const managed = managedFields.has(key);
            if (field.dataset.initiallyDisabled === undefined) {
                field.dataset.initiallyDisabled = field.disabled ? 'true' : 'false';
                field.dataset.initialTitle = field.getAttribute('title') || '';
            }
            field.dataset.managedByEnvironment = managed ? 'true' : 'false';
            field.disabled = managed || field.dataset.initiallyDisabled === 'true';
            const managedContainer = field.closest(
                '.agent-field, .agent-settings-option, .agent-settings-master-toggle, .form-row'
            );
            managedContainer?.classList.toggle('is-managed', managed);
            field.title = managed
                ? '此配置由部署环境管理，请修改环境变量后重启服务'
                : field.dataset.initialTitle;
            field.closest('.field-control')?.querySelectorAll(
                '.secret-input-toggle'
            ).forEach((button) => { button.disabled = managed; });
            if (field.type === 'checkbox') {
                field.checked = ['1', 'true', 'yes'].includes(String(value).toLowerCase());
            } else if (field.dataset.secretField === 'true' && String(value) === '********') {
                field.value = '';
                field.type = 'password';
                field.dataset.secretState = 'saved';
                field.placeholder = '已保存；输入新值以替换';
                syncSecretControls(field);
            } else {
                field.value = value;
                if (field.dataset.secretField === 'true') {
                    field.dataset.secretState = value ? 'draft' : 'empty';
                    syncSecretControls(field);
                }
            }
        });
        root.querySelectorAll('[data-key]').forEach((field) => {
            field.dataset.configInitialValue = field.type === 'checkbox'
                ? (field.checked ? '1' : '0')
                : field.value;
        });
        root.querySelectorAll('[data-managed-note]').forEach((note) => {
            const keys = String(note.dataset.managedKeys || '')
                .split(',')
                .map((key) => key.trim())
                .filter(Boolean);
            const managed = keys.filter((key) => managedFields.has(key));
            note.hidden = managed.length === 0;
            note.dataset.tone = managed.length ? 'managed' : 'default';
            note.textContent = managed.length
                ? `部署环境已锁定 ${managed.length} 个字段`
                : '';
        });
    };

    window.collectConfigFields = function (root) {
        const payload = {};
        const clearSecrets = [];
        root.querySelectorAll('[data-key]').forEach((field) => {
            if (field.dataset.managedByEnvironment === 'true') return;
            if (field.dataset.secretField === 'true') {
                const state = field.dataset.secretState || 'empty';
                if (state === 'saved' && !field.value) return;
                if (state === 'clear') {
                    clearSecrets.push(field.dataset.key);
                    return;
                }
                if (!field.value) return;
            }
            const value = field.type === 'checkbox'
                ? (field.checked ? '1' : '0')
                : field.value;
            if (
                field.dataset.secretField !== 'true'
                && field.dataset.configInitialValue !== undefined
                && value === field.dataset.configInitialValue
            ) return;
            payload[field.dataset.key] = value;
        });
        if (clearSecrets.length) payload.__clear_secrets = clearSecrets;
        return payload;
    };

    window.showToast = function (message, type = 'success', duration = 2800) {
        if (!message) return null;
        let container = document.getElementById('appToastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'appToastContainer';
            container.className = 'app-toast-container';
            container.setAttribute('aria-live', 'polite');
            container.setAttribute('aria-atomic', 'true');
            document.body.appendChild(container);
        }
        const toast = document.createElement('div');
        toast.className = `app-toast is-${type}`;
        const iconName = type === 'success' ? 'circle-check-big' : (type === 'error' ? 'circle-x' : (type === 'warning' ? 'triangle-alert' : (type === 'loading' ? 'loader-2' : 'info')));
        const icon = document.createElement('span');
        icon.className = 'app-toast-icon';
        const iconNode = document.createElement('i');
        iconNode.dataset.lucide = iconName;
        icon.appendChild(iconNode);
        const messageNode = document.createElement('span');
        messageNode.className = 'app-toast-message';
        messageNode.textContent = String(message);
        toast.append(icon, messageNode);
        container.appendChild(toast);
        renderIcons(toast);
        if (window.MFAnim) {
            window.MFAnim.popIn(toast, { y: 12, duration: 0.22 });
        }

        if (navigator.vibrate) {
            try {
                if (type === 'error') navigator.vibrate([30, 40, 30]);
                else if (type === 'success') navigator.vibrate([15, 30]);
            } catch (_) {}
        }

        let isClosed = false;
        function dismiss() {
            if (isClosed) return;
            isClosed = true;
            if (window.MFAnim) {
                window.MFAnim.popOut(toast, { y: -8, duration: 0.18, onComplete: () => toast.remove() });
            } else {
                toast.classList.add('is-leaving');
                setTimeout(() => toast.remove(), 220);
            }
        }
        toast.addEventListener('click', dismiss);
        if (duration > 0) {
            setTimeout(dismiss, duration);
        }
        return { dismiss };
    };

    window.saveAppConfig = async function (root, options = {}) {
        try {
            const response = await fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(window.collectConfigFields(root)),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '配置保存失败');
            root.querySelectorAll('[data-secret-field="true"]').forEach((field) => {
                const state = field.dataset.secretState || 'empty';
                if (state === 'draft' && field.value) {
                    field.value = '';
                    field.type = 'password';
                    field.dataset.secretState = 'saved';
                    field.placeholder = '已保存；输入新值以替换';
                } else if (state === 'clear') {
                    field.value = '';
                    field.type = 'password';
                    field.dataset.secretState = 'empty';
                    field.placeholder = '未配置';
                }
                syncSecretControls(field);
            });
            root.querySelectorAll('[data-key]').forEach((field) => {
                field.dataset.configInitialValue = field.type === 'checkbox'
                    ? (field.checked ? '1' : '0')
                    : field.value;
            });
            if (options.toast !== false) {
                const warnings = Array.isArray(data.warnings)
                    ? data.warnings.filter(Boolean)
                    : [];
                if (warnings.length) {
                    window.showToast(warnings.join('；'), 'warning', 5200);
                } else {
                    window.showToast(options.toastMessage || '配置已成功保存', 'success');
                }
            }
            return data;
        } catch (error) {
            if (options.toast !== false) {
                window.showToast(error.message || '配置保存失败', 'error');
            }
            throw error;
        }
    };

    window.setupTabGroup = function (root, options) {
        if (!root) return null;
        const buttons = [...root.querySelectorAll('[data-tab-target]')];
        const panels = [...root.querySelectorAll('[data-tab-panel]')];
        function activate(target) {
            buttons.forEach((button) => {
                const active = button.dataset.tabTarget === target;
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
                button.tabIndex = active ? 0 : -1;
            });
            panels.forEach((panel) => {
                const active = panel.dataset.tabPanel === target;
                panel.hidden = !active;
                panel.classList.toggle('active', active);
            });
            options?.onChange?.(target);
        }
        buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.tabTarget)));
        const initial = options?.initial || buttons.find((button) => button.classList.contains('active'))?.dataset.tabTarget || buttons[0]?.dataset.tabTarget;
        if (initial) activate(initial);
        return {activate};
    };

    window.createAppModal = function (modal) {
        if (!modal) return null;
        // 弹窗必须脱离带 transform/animation 的页面内容容器，否则 fixed 会相对
        // 容器而非浏览器视口定位，长页面中会出现在当前可视区域下方。
        if (modal.parentElement !== document.body) document.body.appendChild(modal);
        const dialog = modal.querySelector('[role="dialog"]');
        const focusableSelector = [
            'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
            'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
        ].join(',');
        let returnFocus = null;
        let focusGeneration = 0;
        function focusableElements() {
            if (!dialog) return [];
            return [...dialog.querySelectorAll(focusableSelector)].filter((element) => (
                !element.hidden
                && element.getAttribute('aria-hidden') !== 'true'
                && !element.closest?.('[hidden], [aria-hidden="true"]')
            ));
        }
        function resolveInitialFocus(initialFocus) {
            if (typeof initialFocus === 'string') return dialog?.querySelector(initialFocus);
            return initialFocus || focusableElements()[0] || dialog;
        }
        function close({restoreFocus = true} = {}) {
            focusGeneration += 1;
            modal.hidden = true;
            document.body.classList.remove('modal-open');
            const target = returnFocus;
            returnFocus = null;
            if (restoreFocus && target?.isConnected !== false) target?.focus?.({preventScroll: true});
        }
        function open(trigger, {initialFocus = null} = {}) {
            const generation = ++focusGeneration;
            returnFocus = trigger || document.activeElement;
            modal.hidden = false;
            document.body.classList.add('modal-open');
            requestAnimationFrame(() => {
                if (generation !== focusGeneration || modal.hidden) return;
                resolveInitialFocus(initialFocus)?.focus?.({preventScroll: true});
            });
        }
        modal.querySelectorAll('[data-modal-close]').forEach((button) => button.addEventListener('click', () => close()));
        modal.addEventListener('click', (event) => { if (event.target === modal) close(); });
        document.addEventListener('keydown', (event) => {
            if (modal.hidden) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close();
                return;
            }
            if (event.key !== 'Tab' || !dialog) return;
            const focusable = focusableElements();
            if (!focusable.length) {
                event.preventDefault();
                if (!dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');
                dialog.focus({preventScroll: true});
                return;
            }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const active = document.activeElement;
            if (event.shiftKey && (active === first || !dialog.contains(active))) {
                event.preventDefault();
                last.focus({preventScroll: true});
            } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
                event.preventDefault();
                first.focus({preventScroll: true});
            }
        });
        return {open, close};
    };

    const confirmModal = document.getElementById('appConfirmModal');
    if (confirmModal) {
        const confirmDialog = confirmModal.querySelector('[role="alertdialog"]');
        const confirmTitle = document.getElementById('appConfirmTitle');
        const confirmMessage = document.getElementById('appConfirmMessage');
        const confirmKicker = document.getElementById('appConfirmKicker');
        const confirmVerify = document.getElementById('appConfirmVerify');
        const confirmVerifyLabel = document.getElementById('appConfirmVerifyLabel');
        const confirmVerifyInput = document.getElementById('appConfirmVerifyInput');
        const confirmCancel = document.getElementById('appConfirmCancel');
        const confirmSubmit = document.getElementById('appConfirmSubmit');
        const confirmIcon = confirmModal.querySelector('[data-confirm-icon]');
        let activeConfirm = null;
        let confirmReturnFocus = null;

        function confirmFocusable() {
            return [...confirmDialog.querySelectorAll('button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')];
        }
        function syncConfirmSubmit() {
            if (!activeConfirm) return;
            const verifyText = activeConfirm.verifyText || '';
            confirmSubmit.disabled = activeConfirm.busy || (verifyText && confirmVerifyInput.value !== verifyText);
        }
        function finishConfirm(value) {
            if (!activeConfirm) return;
            const resolve = activeConfirm.resolve;
            activeConfirm = null;
            confirmModal.hidden = true;
            document.body.classList.remove('modal-open');
            confirmReturnFocus?.focus?.();
            resolve(value);
        }
        async function submitConfirm() {
            if (!activeConfirm || confirmSubmit.disabled) return;
            if (!activeConfirm.onConfirm) {
                finishConfirm(true);
                return;
            }
            activeConfirm.busy = true;
            confirmCancel.disabled = true;
            confirmSubmit.disabled = true;
            const originalText = confirmSubmit.textContent;
            confirmSubmit.textContent = activeConfirm.loadingText || '处理中...';
            try {
                const result = await activeConfirm.onConfirm();
                finishConfirm(result === undefined ? true : result);
            } catch (error) {
                activeConfirm.busy = false;
                confirmCancel.disabled = false;
                confirmSubmit.textContent = originalText;
                confirmMessage.textContent = error?.message || '操作失败，请重试';
                confirmModal.classList.add('has-error');
                syncConfirmSubmit();
            }
        }
        confirmCancel.addEventListener('click', () => finishConfirm(false));
        confirmSubmit.addEventListener('click', submitConfirm);
        confirmVerifyInput.addEventListener('input', syncConfirmSubmit);
        confirmModal.addEventListener('click', (event) => {
            if (event.target === confirmModal && activeConfirm?.dismissible !== false && !activeConfirm.busy) finishConfirm(false);
        });
        document.addEventListener('keydown', (event) => {
            if (confirmModal.hidden || !activeConfirm) return;
            if (event.key === 'Escape' && activeConfirm.dismissible !== false && !activeConfirm.busy) {
                event.preventDefault();
                finishConfirm(false);
                return;
            }
            if (event.key === 'Enter' && !event.shiftKey && document.activeElement === confirmVerifyInput) {
                event.preventDefault();
                submitConfirm();
                return;
            }
            if (event.key !== 'Tab') return;
            const focusable = confirmFocusable();
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });

        window.appConfirm = function (options = {}) {
            if (activeConfirm) finishConfirm(false);
            const danger = options.danger === true;
            const verifyText = String(options.verifyText || '');
            confirmReturnFocus = options.trigger || document.activeElement;
            confirmModal.classList.remove('is-danger', 'has-error');
            confirmModal.classList.toggle('is-danger', danger);
            confirmKicker.textContent = danger ? 'DANGEROUS ACTION' : (options.kicker || 'CONFIRM ACTION');
            confirmTitle.textContent = options.title || '确认操作';
            confirmMessage.textContent = options.message || '确认继续执行此操作？';
            confirmCancel.textContent = options.cancelText || '取消';
            confirmSubmit.textContent = options.confirmText || '确认';
            confirmSubmit.classList.toggle('btn-danger', danger);
            confirmIcon.innerHTML = `<i data-lucide="${danger ? 'triangle-alert' : (options.icon || 'circle-help')}"></i>`;
            confirmVerify.hidden = !verifyText;
            confirmVerifyLabel.textContent = options.verifyLabel || (verifyText ? `输入 ${verifyText} 继续` : '');
            confirmVerifyInput.value = '';
            confirmVerifyInput.placeholder = verifyText;
            confirmCancel.disabled = false;
            confirmModal.hidden = false;
            document.body.classList.add('modal-open');
            renderIcons(confirmModal);
            return new Promise((resolve) => {
                activeConfirm = {
                    resolve,
                    danger,
                    verifyText,
                    onConfirm: options.onConfirm,
                    loadingText: options.loadingText,
                    dismissible: options.dismissible,
                    busy: false,
                };
                syncConfirmSubmit();
                requestAnimationFrame(() => (verifyText ? confirmVerifyInput : confirmCancel).focus());
            });
        };
    } else {
        window.appConfirm = async function () { return false; };
    }

    const messageModal = document.getElementById('appMessageModal');
    if (messageModal) {
        const messageDialog = messageModal.querySelector('[role="alertdialog"]');
        const messageTitle = document.getElementById('appMessageTitle');
        const messageText = document.getElementById('appMessageText');
        const messageKicker = document.getElementById('appMessageKicker');
        const messageClose = document.getElementById('appMessageClose');
        const messageIcon = messageModal.querySelector('[data-message-icon]');
        let activeMessage = null;
        let messageReturnFocus = null;

        const messageTypes = {
            success: {title: '操作完成', kicker: 'SUCCESS', icon: 'circle-check-big'},
            info: {title: '提示', kicker: 'INFORMATION', icon: 'info'},
            warning: {title: '请注意', kicker: 'ATTENTION', icon: 'triangle-alert'},
            error: {title: '操作失败', kicker: 'ERROR', icon: 'circle-x'},
        };
        function finishMessage() {
            if (!activeMessage) return;
            const resolve = activeMessage.resolve;
            activeMessage = null;
            messageModal.hidden = true;
            if (confirmModal?.hidden !== false) document.body.classList.remove('modal-open');
            messageReturnFocus?.focus?.();
            resolve(true);
        }
        messageClose.addEventListener('click', finishMessage);
        messageModal.addEventListener('click', (event) => {
            if (event.target === messageModal && activeMessage?.dismissible !== false) finishMessage();
        });
        document.addEventListener('keydown', (event) => {
            if (messageModal.hidden || !activeMessage) return;
            if ((event.key === 'Escape' && activeMessage.dismissible !== false) || event.key === 'Enter') {
                event.preventDefault();
                finishMessage();
                return;
            }
            if (event.key !== 'Tab') return;
            event.preventDefault();
            messageClose.focus();
        });

        window.appAlert = function (options = {}) {
            if (typeof options === 'string') options = {message: options};
            if (activeMessage) finishMessage();
            const type = ['success', 'info', 'warning', 'error'].includes(options.type)
                ? options.type
                : 'info';
            const meta = messageTypes[type];
            messageReturnFocus = options.trigger || document.activeElement;
            messageModal.classList.remove('is-success', 'is-info', 'is-warning', 'is-error');
            messageModal.classList.add(`is-${type}`);
            messageKicker.textContent = options.kicker || meta.kicker;
            messageTitle.textContent = options.title || meta.title;
            messageText.textContent = options.message || '';
            messageClose.textContent = options.closeText || '知道了';
            messageIcon.innerHTML = `<i data-lucide="${options.icon || meta.icon}"></i>`;
            messageModal.hidden = false;
            document.body.classList.add('modal-open');
            renderIcons(messageModal);
            return new Promise((resolve) => {
                activeMessage = {resolve, dismissible: options.dismissible};
                requestAnimationFrame(() => messageClose.focus());
            });
        };
    } else {
        window.appAlert = async function () { return false; };
    }

    const navClusters = [...document.querySelectorAll('[data-nav-cluster]')];
    function sidebarIsCollapsed() {
        return window.innerWidth > 900 && document.documentElement.dataset.sidebar === 'collapsed';
    }
    function closeGuangyaFlyout() {
        navClusters.forEach((cluster) => {
            const flyout = cluster.querySelector('.nav-cluster-flyout');
            const button = cluster.querySelector('.nav-cluster-toggle');
            if (flyout) flyout.hidden = true;
            if (button) button.setAttribute('aria-expanded', 'false');
        });
    }
    function closeExpandedNavClusters() {
        navClusters.forEach((cluster) => {
            cluster.classList.remove('open');
            cluster.querySelector('.nav-cluster-toggle')?.setAttribute('aria-expanded', 'false');
        });
    }
    function navClusterStorageKey(cluster) {
        const clusterName = cluster?.dataset.navCluster;
        return clusterName ? `mediaflux.nav.${clusterName}.open` : '';
    }
    function readNavClusterPreference(cluster) {
        const key = navClusterStorageKey(cluster);
        if (!key) return null;
        try {
            return localStorage.getItem(key);
        } catch (_) {
            return null;
        }
    }
    function saveNavClusterPreference(cluster, open) {
        const key = navClusterStorageKey(cluster);
        if (!key) return;
        try {
            localStorage.setItem(key, open ? '1' : '0');
        } catch (_) {}
    }
    navClusters.forEach((cluster) => {
        const button = cluster.querySelector('.nav-cluster-toggle');
        const flyout = cluster.querySelector('.nav-cluster-flyout');
        if (!button) return;
        const savedPreference = readNavClusterPreference(cluster);
        const initiallyOpen = savedPreference === null
            ? cluster.dataset.navDefaultOpen === 'true' || cluster.classList.contains('active') || cluster.classList.contains('open')
            : savedPreference === '1';
        if (cluster.classList.contains('open') !== initiallyOpen) {
            cluster.classList.toggle('open', initiallyOpen);
        }
        button.setAttribute('aria-expanded', initiallyOpen ? 'true' : 'false');
        button.addEventListener('click', () => {
            if (sidebarIsCollapsed() && flyout) {
                const open = flyout.hidden;
                closeGuangyaFlyout();
                if (open) {
                    const rect = button.getBoundingClientRect();
                    flyout.hidden = false;
                    flyout.style.top = `${Math.max(12, Math.min(rect.top, window.innerHeight - flyout.offsetHeight - 12))}px`;
                }
                button.setAttribute('aria-expanded', open ? 'true' : 'false');
                return;
            }
            closeGuangyaFlyout();
            const open = !cluster.classList.contains('open');
            navClusters.forEach((other) => {
                if (other === cluster) return;
                other.classList.remove('open');
                other.querySelector('.nav-cluster-toggle')?.setAttribute('aria-expanded', 'false');
            });
            cluster.classList.toggle('open', open);
            button.setAttribute('aria-expanded', open ? 'true' : 'false');
            saveNavClusterPreference(cluster, open);
        });
    });
    delete document.documentElement.dataset.navGuangya;

    const collapse = document.getElementById('collapseSidebar');
    const collapsedKey = 'mediaflux.sidebar.collapsed';
    function applySidebarCollapsed(collapsed) {
        if (window.innerWidth <= 900) collapsed = false;
        document.documentElement.dataset.sidebar = collapsed ? 'collapsed' : 'expanded';
        closeGuangyaFlyout();
        if (!collapsed) {
            navClusters.forEach((cluster) => {
                cluster.querySelector('.nav-cluster-toggle')?.setAttribute(
                    'aria-expanded', cluster.classList.contains('open') ? 'true' : 'false'
                );
            });
        }
        if (!collapse) return;
        collapse.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        collapse.setAttribute('aria-label', collapsed ? '展开侧栏' : '收缩侧栏');
        collapse.title = collapsed ? '展开侧栏' : '收缩侧栏';
        collapse.innerHTML = `<i data-lucide="${collapsed ? 'panel-left-open' : 'panel-left-close'}"></i>`;
        renderIcons(collapse);
    }
    if (collapse) {
        applySidebarCollapsed(localStorage.getItem(collapsedKey) === '1');
        collapse.addEventListener('click', () => {
            const collapsed = document.documentElement.dataset.sidebar !== 'collapsed';
            localStorage.setItem(collapsedKey, collapsed ? '1' : '0');
            applySidebarCollapsed(collapsed);
        });
    }

    const toggle = document.getElementById('toggleSidebar');
    const sidebar = document.getElementById('sidebar');
    function closeMobileSidebar() {
        if (!sidebar) return;
        sidebar.classList.remove('open');
        document.body.classList.remove('sidebar-open');
        toggle?.setAttribute('aria-expanded', 'false');
    }
    if (toggle && sidebar) {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.addEventListener('click', () => {
            const open = sidebar.classList.toggle('open');
            document.body.classList.toggle('sidebar-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
    }

    document.addEventListener('click', (event) => {
        if (window.innerWidth > 900) {
            navClusters.forEach((cluster) => {
                const flyout = cluster.querySelector('.nav-cluster-flyout');
                if (flyout && !flyout.hidden && !cluster.contains(event.target)) closeGuangyaFlyout();
            });
            return;
        }
        if (sidebar && sidebar.classList.contains('open')
            && !sidebar.contains(event.target) && !toggle.contains(event.target)) {
            closeMobileSidebar();
        }
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeGuangyaFlyout();
            if (!sidebarIsCollapsed()) closeExpandedNavClusters();
            if (window.innerWidth <= 900) closeMobileSidebar();
        }
    });
    window.addEventListener('resize', () => {
        if (window.innerWidth > 900) {
            closeMobileSidebar();
            applySidebarCollapsed(localStorage.getItem(collapsedKey) === '1');
        } else {
            closeGuangyaFlyout();
            applySidebarCollapsed(false);
        }
    });
})();
