(() => {
    'use strict';

    const page = document.getElementById('mediaLibrariesPage');
    if (!page) return;

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
    const esc = (value) => String(value ?? '')
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    const providerLabels = {jellyfin: 'Jellyfin', emby: 'Emby'};
    const state = {
        config: {},
        overview: null,
        rows: [],
        refreshing: false,
        initialized: false,
        dirty: false,
    };

    function icons(root = document) {
        if (window.lucide) window.lucide.createIcons({nodes: [root]});
    }

    async function api(url, options = {}) {
        const response = await fetch(url, {
            headers: {'Content-Type': 'application/json', ...(options.headers || {})},
            ...options,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || '请求失败');
        return data;
    }

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    function setInitialLoading(loading) {
        page.dataset.loading = loading ? 'true' : 'false';
        page.setAttribute('aria-busy', loading ? 'true' : 'false');
        $$('#mlMappingWorkbench input, #mlMappingWorkbench select, #mlMappingWorkbench button, [data-add-library-mapping]')
            .forEach((control) => { control.disabled = loading; });
    }

    function managedFields() {
        return new Set(Array.isArray(state.config.__managed_fields) ? state.config.__managed_fields : []);
    }

    function setBusy(button, busy, label) {
        if (!button) return;
        const labelNode = $('[data-button-label]', button);
        if (button.dataset.idleLabel === undefined) {
            button.dataset.idleLabel = labelNode?.textContent || button.textContent.trim();
        }
        button.disabled = busy;
        button.classList.toggle('is-loading', busy);
        if (labelNode) labelNode.textContent = busy ? label : button.dataset.idleLabel;
    }

    function animateCount(id, value) {
        const node = document.getElementById(id);
        if (!node) return;
        const target = Math.max(0, Number(value) || 0);
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            node.textContent = String(target);
            return;
        }
        const current = Number(node.textContent.replaceAll(',', '')) || 0;
        if (current === target) {
            node.textContent = String(target);
            return;
        }
        const started = performance.now();
        const duration = 360;
        const frame = (now) => {
            const progress = Math.min(1, (now - started) / duration);
            const eased = 1 - Math.pow(1 - progress, 3);
            node.textContent = Math.round(current + ((target - current) * eased)).toLocaleString();
            if (progress < 1) window.requestAnimationFrame(frame);
        };
        window.requestAnimationFrame(frame);
    }

    function normalizePath(value) {
        const text = String(value || '').trim().replaceAll('\\', '/');
        if (text === '/' || text === '//') return text;
        return text.replace(/\/+$/, '');
    }

    function pathKey(value) {
        const normalized = normalizePath(value);
        return normalized.startsWith('//') || /^[A-Za-z]:\//.test(normalized)
            ? normalized.toLocaleLowerCase() : normalized;
    }

    function pathContains(parent, child) {
        const root = pathKey(parent);
        const candidate = pathKey(child);
        return Boolean(root && candidate && (candidate === root || candidate.startsWith(`${root}/`)));
    }

    function configuredServers() {
        return (state.overview?.servers || []).filter((server) => server.enabled && server.configured);
    }

    function serverFor(provider) {
        return configuredServers().find((server) => server.server_type === provider) || null;
    }

    function libraryChoices() {
        return configuredServers().flatMap((server) => (server.libraries || []).map((library, index) => ({
            key: `${server.server_type}:${index}`,
            provider: server.server_type,
            providerLabel: server.label || providerLabels[server.server_type] || server.server_type,
            libraryId: String(library.id || ''),
            libraryName: String(library.name || '未命名媒体库'),
            locations: (library.locations || []).map((item) => String(item || '').trim()).filter(Boolean),
        })));
    }

    function discoveredStrmDirectories() {
        return (state.overview?.strm?.directories || []).filter((item) => {
            return item?.kind !== 'root' && String(item?.local_path || '').trim();
        });
    }

    function choiceForKey(key) {
        return libraryChoices().find((choice) => choice.key === key) || null;
    }

    function inferLibraryChoice(provider, serverPath) {
        const choices = libraryChoices().filter((choice) => choice.provider === provider);
        const exact = choices.filter((choice) => choice.locations.some((location) => pathKey(location) === pathKey(serverPath)));
        if (exact.length === 1) return exact[0];
        const related = choices.filter((choice) => choice.locations.some(
            (location) => pathContains(serverPath, location) || pathContains(location, serverPath),
        ));
        return related.length === 1 ? related[0] : null;
    }

    function hydrateRows() {
        const rows = [];
        for (const server of configuredServers()) {
            for (const mapping of server.mappings || []) {
                const choice = inferLibraryChoice(server.server_type, mapping.server);
                rows.push({
                    provider: server.server_type,
                    libraryKey: choice?.key || '',
                    libraryId: choice?.libraryId || '',
                    libraryName: choice?.libraryName || '',
                    local: String(mapping.local || ''),
                    server: String(mapping.server || ''),
                    legacy: !choice,
                });
            }
        }
        state.rows = rows;
        state.dirty = false;
    }

    function renderSummary() {
        const summary = state.overview?.summary || {};
        const servers = state.overview?.servers || [];
        const directories = discoveredStrmDirectories();
        animateCount('mlServerCount', summary.configured_servers);
        animateCount('mlLibraryCount', summary.libraries);
        animateCount('mlMappingCount', summary.path_mappings);
        animateCount('mlBindingCount', directories.length);

        const serverFoot = $('#mlServerSummary');
        if (serverFoot) {
            const parts = servers
                .filter((server) => server.enabled && server.configured)
                .map((server) => `${server.label}: ${server.library_error ? '连接异常' : '在线'}`);
            serverFoot.textContent = parts.join(' · ') || '请先在设置中配置媒体服务器';
        }

        const libraryFoot = $('#mlLibrarySummary');
        if (libraryFoot) {
            const parts = configuredServers().map((server) => `${server.label} ${server.libraries?.length || 0}`);
            libraryFoot.textContent = parts.length ? parts.join(' · ') : '未发现可用媒体库';
        }

        const mappingFoot = $('#mlMappingSummary');
        if (mappingFoot) mappingFoot.textContent = `${Number(summary.path_mappings || 0)} 条规则已配置`;

        const bindingFoot = $('#mlBindingSummary');
        if (bindingFoot) {
            const outputRoot = String(state.overview?.strm?.output_root || '');
            bindingFoot.textContent = outputRoot || '尚未配置 STRM 输出目录';
            bindingFoot.title = outputRoot;
        }
    }

    function libraryOptions(row, rowIndex) {
        const options = libraryChoices().map((choice) => `
            <option value="${esc(choice.key)}" ${choice.key === row.libraryKey ? 'selected' : ''}>
                ${esc(choice.providerLabel)} · ${esc(choice.libraryName)}
            </option>`).join('');
        const legacy = row.legacy && !row.libraryKey
            ? `<option value="legacy:${rowIndex}" selected>${esc(providerLabels[row.provider] || row.provider)} · 现有映射</option>`
            : '';
        return `<option value="" ${!row.libraryKey && !row.legacy ? 'selected' : ''}>请选择媒体库</option>${legacy}${options}`;
    }

    function rowLocked(row) {
        const server = serverFor(row.provider);
        return Boolean(server?.mapping_key && managedFields().has(server.mapping_key));
    }

    function mappingRow(row, index) {
        const choice = choiceForKey(row.libraryKey);
        const locations = choice?.locations || (row.server ? [row.server] : []);
        const datalistId = `mlServerLocations-${index}`;
        const locked = rowLocked(row);
        return `<div class="ml-library-mapping-row${locked ? ' is-locked' : ''}" data-mapping-row="${index}">
            <label class="ml-mapping-field ml-library-field">
                <span class="ml-field-label">媒体库</span>
                <select class="form-select" data-mapping-library ${locked ? 'disabled' : ''}>${libraryOptions(row, index)}</select>
            </label>
            <div class="ml-mapping-source" aria-label="来源类型 STRM">
                <span class="ml-field-label">来源</span>
                <span class="ml-source-value"><i data-lucide="file-video-2"></i><span>STRM</span></span>
            </div>
            <label class="ml-mapping-field ml-strm-field">
                <span class="ml-field-label">STRM 目录</span>
                <span class="ml-strm-control">
                    <input class="form-input ml-strm-path" data-mapping-strm-path value="${esc(row.local)}" placeholder="点击文件夹选择真实 STRM 目录" readonly>
                    <button class="jump-btn ml-strm-picker-btn" type="button" data-pick-strm title="浏览 STRM 目录" aria-label="浏览 STRM 目录" ${locked ? 'disabled' : ''}><i data-lucide="folder-open"></i></button>
                </span>
            </label>
            <label class="ml-mapping-field ml-server-path-field">
                <span class="ml-field-label">服务器路径</span>
                <input class="form-input" data-mapping-server value="${esc(row.server)}" list="${datalistId}" placeholder="媒体服务器可见路径" ${locked ? 'disabled' : ''}>
                <datalist id="${datalistId}">${locations.map((location) => `<option value="${esc(location)}"></option>`).join('')}</datalist>
            </label>
            <span class="ml-mapping-actions">
                <button class="jump-btn" type="button" data-test-mapping title="测试媒体库映射" aria-label="测试媒体库映射" ${locked ? 'disabled' : ''}><i data-lucide="scan-search"></i></button>
                <button class="jump-btn danger" type="button" data-remove-mapping title="删除媒体库映射" aria-label="删除媒体库映射" ${locked ? 'disabled' : ''}><i data-lucide="trash-2"></i></button>
            </span>
            <span class="ml-mapping-result" data-mapping-result aria-live="polite"></span>
            ${locked ? '<span class="ml-managed-note">此映射由部署环境管理</span>' : ''}
        </div>`;
    }

    function updateActions() {
        const choices = libraryChoices();
        const hasStrmRoot = Boolean(String(state.overview?.strm?.output_root || '').trim());
        const writableProviders = configuredServers().filter((server) => !managedFields().has(server.mapping_key));
        const add = $('[data-add-library-mapping]');
        if (add) {
            add.disabled = !state.initialized || !choices.length || !hasStrmRoot || !writableProviders.length;
            add.title = !configuredServers().length
                ? '请先在设置中配置并启用媒体服务器'
                : !choices.length ? '当前服务器尚未返回媒体库'
                    : !hasStrmRoot ? '请先配置 STRM 本地根目录'
                        : !writableProviders.length ? '媒体库映射由部署环境管理' : '';
        }
        const save = $('[data-save-mappings]');
        if (save) {
            save.disabled = !state.initialized || !state.dirty || !writableProviders.length;
        }
    }

    function renderRows() {
        const list = $('#mlMappingList');
        if (!list) return;
        const servers = configuredServers();
        const choices = libraryChoices();
        const hasStrmRoot = Boolean(String(state.overview?.strm?.output_root || '').trim());
        const mappingErrors = servers.map((server) => server.mapping_error).filter(Boolean);

        if (mappingErrors.length) {
            list.innerHTML = `<div class="ml-empty-row is-error">当前路径映射配置无效：${esc(mappingErrors.join('；'))}</div>`;
        } else if (!servers.length) {
            list.innerHTML = '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="server-cog"></i></div><strong>尚未配置媒体服务器</strong><p>请先在设置中启用并填写 Jellyfin 或 Emby，媒体库会自动出现在这里。</p></div>';
        } else if (!choices.length) {
            list.innerHTML = '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="library"></i></div><strong>未读取到媒体库</strong><p>请检查媒体服务器连接；连接正常后，本页会直接跟随服务器中的媒体库。</p></div>';
        } else if (!hasStrmRoot) {
            list.innerHTML = '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="folder-search-2"></i></div><strong>尚未配置 STRM 根目录</strong><p>请先配置 STRM 本地根目录，再从真实输出目录中选择媒体库路径。</p></div>';
        } else if (!state.rows.length) {
            list.innerHTML = '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="folder-symlink"></i></div><strong>尚未添加媒体库映射</strong><p>点击右上角“添加媒体库”，选择媒体库、STRM 目录和服务器可见路径。</p></div>';
        } else {
            list.innerHTML = state.rows.map(mappingRow).join('');
        }
        updateActions();
        icons(list);
    }

    function applyOverview({reloadRows = true} = {}) {
        renderSummary();
        if (reloadRows) hydrateRows();
        renderRows();
    }

    async function loadAll({manual = false, reloadRows = true} = {}) {
        if (state.refreshing) return;
        if (manual && state.dirty) {
            window.showToast?.('存在尚未保存的媒体库映射，请先保存后再刷新', 'warning', 4200);
            return;
        }
        state.refreshing = true;
        const button = $('#mlRefreshBtn');
        if (manual) setBusy(button, true, '刷新中');
        const firstLoad = !state.initialized;
        try {
            const [config, overview] = await Promise.all([
                window.loadAppConfig ? window.loadAppConfig() : Promise.resolve({}),
                api('/api/media-libraries/overview'),
                state.overview ? Promise.resolve() : sleep(320),
            ]);
            state.config = config;
            state.overview = overview;
            state.initialized = true;
            if (firstLoad) setInitialLoading(false);
            applyOverview({reloadRows});
            if (manual) window.showToast?.('媒体库与路径映射已刷新', 'success');
        } catch (error) {
            window.showToast?.(error.message, 'error', 4200);
        } finally {
            if (firstLoad) setInitialLoading(false);
            state.refreshing = false;
            if (manual) setBusy(button, false, '刷新状态');
            updateActions();
        }
    }

    function syncRow(rowElement) {
        const index = Number(rowElement.dataset.mappingRow);
        const row = state.rows[index];
        if (!row) return null;
        const selectedKey = $('[data-mapping-library]', rowElement)?.value || '';
        const choice = choiceForKey(selectedKey);
        if (choice) {
            row.libraryKey = choice.key;
            row.libraryId = choice.libraryId;
            row.libraryName = choice.libraryName;
            row.provider = choice.provider;
            row.legacy = false;
        }
        row.server = $('[data-mapping-server]', rowElement)?.value.trim() || '';
        return row;
    }

    function resetResult(rowElement) {
        const result = $('[data-mapping-result]', rowElement);
        if (!result) return;
        result.className = 'ml-mapping-result';
        result.textContent = '';
    }

    function openStrmDirectoryPicker(rowElement) {
        const index = Number(rowElement.dataset.mappingRow);
        const row = state.rows[index];
        const outputRoot = String(state.overview?.strm?.output_root || '').trim();
        if (!row || !outputRoot) {
            window.showToast?.('尚未配置可浏览的 STRM 输出目录', 'warning', 4200);
            return;
        }
        if (typeof window.openGuangYaDirectoryPicker !== 'function') {
            window.showToast?.('目录选择器尚未加载，请刷新页面后重试', 'error', 4200);
            return;
        }

        window.openGuangYaDirectoryPicker({
            modalId: 'mediaLibraryStrmDirModal',
            title: '选择 STRM 目录',
            rootId: outputRoot,
            rootName: 'STRM 输出',
            allowRoot: true,
            fetchDirectory: async (path) => {
                const query = new URLSearchParams({path: String(path || outputRoot)});
                const data = await api(`/api/media-libraries/strm-directories?${query}`);
                return Array.isArray(data.directories) ? data.directories : [];
            },
            onSelect: (selected) => {
                const localPath = String(selected?.id || '').trim();
                if (!localPath) return false;
                row.local = localPath;
                state.dirty = true;
                renderRows();
                const renderedRow = $(`[data-mapping-row="${index}"]`, $('#mlMappingList'));
                $('[data-pick-strm]', renderedRow)?.focus();
                return true;
            },
        });
    }

    function addLibraryMapping() {
        const writableChoices = libraryChoices().filter((choice) => {
            const server = serverFor(choice.provider);
            return server && !managedFields().has(server.mapping_key);
        });
        const mappedLibraries = new Set(state.rows.map((row) => `${row.provider}:${row.libraryId}`));
        const choice = writableChoices.find(
            (item) => !mappedLibraries.has(`${item.provider}:${item.libraryId}`),
        ) || writableChoices[0];
        if (!choice) {
            window.showToast?.('没有可添加的媒体库', 'warning', 4200);
            return;
        }
        state.rows.push({
            provider: choice.provider,
            libraryKey: choice.key,
            libraryId: choice.libraryId,
            libraryName: choice.libraryName,
            local: '',
            server: String(choice.locations[0] || ''),
            legacy: false,
        });
        state.dirty = true;
        renderRows();
        const rows = $$('[data-mapping-row]', $('#mlMappingList'));
        $('[data-pick-strm]', rows.at(-1))?.focus();
    }

    async function testMapping(rowElement, button) {
        const row = syncRow(rowElement);
        const resultNode = $('[data-mapping-result]', rowElement);
        if (!row?.provider || !row.local || !row.server) {
            resultNode.className = 'ml-mapping-result is-error';
            resultNode.textContent = '请选择媒体库、STRM 目录并填写服务器路径';
            return;
        }
        setBusy(button, true, '');
        resultNode.className = 'ml-mapping-result';
        resultNode.textContent = '正在核对服务器媒体库…';
        try {
            const result = await api('/api/media-libraries/path-test', {
                method: 'POST',
                body: JSON.stringify({
                    provider: row.provider,
                    local_path: row.local,
                    server_path: row.server,
                    sample_path: row.local,
                }),
            });
            const selectedMatch = !row.libraryId || result.matches.some((item) => String(item.id || '') === row.libraryId);
            const names = result.matches.map((item) => item.name).filter(Boolean);
            if ((result.status === 'matched' || result.status === 'covered') && selectedMatch) {
                resultNode.className = 'ml-mapping-result is-success';
                resultNode.textContent = `映射有效：${result.mapped_path} · ${row.libraryName || names.join('、')}`;
            } else if (result.matches.length) {
                resultNode.className = 'ml-mapping-result is-warning';
                resultNode.textContent = `路径命中了 ${names.join('、')}，但不是当前选择的媒体库`;
            } else {
                resultNode.className = 'ml-mapping-result is-warning';
                resultNode.textContent = `转换后路径 ${result.mapped_path} 未命中服务器媒体库`;
            }
        } catch (error) {
            resultNode.className = 'ml-mapping-result is-error';
            resultNode.textContent = error.message;
        } finally {
            setBusy(button, false, '');
        }
    }

    async function saveMappings(button) {
        const rowElements = $$('[data-mapping-row]', $('#mlMappingList'));
        rowElements.forEach(syncRow);
        const seen = new Set();
        for (const [index, row] of state.rows.entries()) {
            const element = rowElements[index];
            const result = $('[data-mapping-result]', element);
            if (!row.provider || !row.local || !row.server) {
                result.className = 'ml-mapping-result is-error';
                result.textContent = '请选择媒体库、STRM 目录并填写服务器路径';
                return;
            }
            const key = `${row.provider}:${pathKey(row.local)}`;
            if (seen.has(key)) {
                result.className = 'ml-mapping-result is-error';
                result.textContent = '同一服务器不能重复映射相同 STRM 目录';
                return;
            }
            seen.add(key);
        }

        const managed = managedFields();
        const payload = {};
        for (const server of configuredServers()) {
            if (managed.has(server.mapping_key)) continue;
            payload[server.mapping_key] = JSON.stringify(
                state.rows
                    .filter((row) => row.provider === server.server_type)
                    .map((row) => ({local: row.local, server: row.server})),
            );
        }
        if (!Object.keys(payload).length) {
            window.showToast?.('媒体库映射由部署环境管理', 'warning', 3600);
            return;
        }

        setBusy(button, true, '保存中');
        try {
            await api('/api/config', {method: 'POST', body: JSON.stringify(payload)});
            state.dirty = false;
            window.showToast?.('媒体库路径映射已保存', 'success');
            await loadAll({reloadRows: true});
        } catch (error) {
            window.showToast?.(error.message, 'error', 4200);
        } finally {
            setBusy(button, false, '保存媒体库映射');
            updateActions();
        }
    }

    $('#mlRefreshBtn')?.addEventListener('click', () => loadAll({manual: true}));
    $('[data-add-library-mapping]')?.addEventListener('click', addLibraryMapping);
    $('[data-save-mappings]')?.addEventListener('click', (event) => saveMappings(event.currentTarget));

    $('#mlMappingWorkbench')?.addEventListener('click', (event) => {
        const rowElement = event.target.closest('[data-mapping-row]');
        if (!rowElement) return;
        const index = Number(rowElement.dataset.mappingRow);
        const pickerButton = event.target.closest('[data-pick-strm]');
        if (pickerButton) {
            openStrmDirectoryPicker(rowElement);
            return;
        }
        const testButton = event.target.closest('[data-test-mapping]');
        if (testButton) {
            testMapping(rowElement, testButton);
            return;
        }
        if (event.target.closest('[data-remove-mapping]')) {
            state.rows.splice(index, 1);
            state.dirty = true;
            renderRows();
        }
    });

    $('#mlMappingWorkbench')?.addEventListener('change', (event) => {
        const rowElement = event.target.closest('[data-mapping-row]');
        if (!rowElement) return;
        const index = Number(rowElement.dataset.mappingRow);
        const row = state.rows[index];
        if (!row) return;

        if (event.target.matches('[data-mapping-library]')) {
            const oldChoice = choiceForKey(row.libraryKey);
            const oldLocations = oldChoice?.locations || [];
            const choice = choiceForKey(event.target.value);
            if (choice) {
                const shouldReplaceServer = !row.server || oldLocations.some((location) => pathKey(location) === pathKey(row.server));
                row.provider = choice.provider;
                row.libraryKey = choice.key;
                row.libraryId = choice.libraryId;
                row.libraryName = choice.libraryName;
                row.legacy = false;
                if (shouldReplaceServer) row.server = choice.locations[0] || '';
            }
            state.dirty = true;
            renderRows();
            const renderedRow = $(`[data-mapping-row="${index}"]`, $('#mlMappingList'));
            $('[data-mapping-library]', renderedRow)?.focus();
            return;
        }

        syncRow(rowElement);
        state.dirty = true;
        resetResult(rowElement);
        updateActions();
    });

    $('#mlMappingWorkbench')?.addEventListener('input', (event) => {
        const rowElement = event.target.closest('[data-mapping-row]');
        if (!rowElement || !event.target.matches('[data-mapping-server]')) return;
        syncRow(rowElement);
        state.dirty = true;
        resetResult(rowElement);
        updateActions();
    });

    icons(page);
    setInitialLoading(true);
    loadAll();
})();
