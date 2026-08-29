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
    const categoryLabels = {
        default: '默认', movie: '电影', tv: '剧集', anime: '动漫',
        documentary: '纪录片', variety: '综艺', concert: '演唱会', kids: '儿童节目',
    };
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
            const progress = Math.max(0, Math.min(1, (now - started) / duration));
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
        return (state.overview?.servers || []).find((server) => server.server_type === provider) || null;
    }

    function localSources() {
        return Array.isArray(state.overview?.local_sources) ? state.overview.local_sources : [];
    }

    function libraryChoices() {
        return configuredServers().flatMap((server) => (server.libraries || []).map((library, index) => ({
            key: `${server.server_type}:${String(library.id || index)}`,
            provider: server.server_type,
            providerLabel: server.label || providerLabels[server.server_type] || server.server_type,
            libraryId: String(library.id || ''),
            libraryName: String(library.name || '未命名媒体库'),
            collectionType: String(library.collection_type || '').trim().toLocaleLowerCase(),
            locations: (library.locations || []).map((item) => String(item || '').trim()).filter(Boolean),
        })));
    }

    function choiceForKey(key) {
        return libraryChoices().find((choice) => choice.key === key) || null;
    }

    function inferLibraryChoice(provider, serverPath, libraryId = '') {
        const choices = libraryChoices().filter((choice) => choice.provider === provider);
        const byId = libraryId && choices.find((choice) => choice.libraryId === String(libraryId));
        if (byId) return byId;
        const exact = choices.filter((choice) => choice.locations.some(
            (location) => pathKey(location) === pathKey(serverPath),
        ));
        if (exact.length === 1) return exact[0];
        const related = choices.filter((choice) => choice.locations.some(
            (location) => pathContains(serverPath, location) || pathContains(location, serverPath),
        ));
        return related.length === 1 ? related[0] : null;
    }

    function inferLocalCategory(choice) {
        if (!choice) return 'default';
        const name = String(choice.libraryName || '').toLocaleLowerCase();
        const type = String(choice.collectionType || '').toLocaleLowerCase();
        const namedCategories = [
            ['anime', /动漫|動畫|动画|anime|animation/iu],
            ['documentary', /纪录|紀錄|documentary|documentaries|docs?/iu],
            ['variety', /综艺|綜藝|variety|reality/iu],
            ['concert', /演唱会|演唱會|音乐会|音樂會|concert|music[ _-]?videos?|live/iu],
            ['kids', /儿童|兒童|少儿|少兒|幼儿|幼兒|kids?|children/iu],
            ['movie', /电影|電影|影片|movies?|films?/iu],
            ['tv', /剧集|劇集|电视剧|電視劇|节目|節目|tv|shows?|series/iu],
        ];
        const named = namedCategories.find(([, pattern]) => pattern.test(name));
        if (named) return named[0];
        if (type === 'movies') return 'movie';
        if (type === 'tvshows') return 'tv';
        if (type === 'musicvideos') return 'concert';
        return 'default';
    }

    function availableLocalSourceIds(sourceIds = []) {
        const available = new Set(localSources().map((source) => Number(source.id)));
        return [...new Set(sourceIds.map(Number))].filter((sourceId) => available.has(sourceId));
    }

    function allLocalSourceIds() {
        return localSources().map((source) => Number(source.id)).filter((sourceId) => sourceId > 0);
    }

    function hydrateRows() {
        const rows = [];
        for (const server of state.overview?.servers || []) {
            for (const mapping of server.mappings || []) {
                const choice = inferLibraryChoice(server.server_type, mapping.server);
                rows.push({
                    kind: 'strm',
                    provider: server.server_type,
                    libraryKey: choice?.key || '',
                    libraryId: choice?.libraryId || '',
                    libraryName: choice?.libraryName || '',
                    local: String(mapping.local || ''),
                    server: String(mapping.server || ''),
                    sourceIds: [],
                    category: 'default',
                    legacy: !choice,
                });
            }
        }

        const groupedBindings = new Map();
        for (const binding of state.overview?.local_bindings || []) {
            const provider = String(binding.provider || '');
            const choice = inferLibraryChoice(provider, binding.server_path, binding.library_id);
            const row = {
                kind: 'local',
                provider,
                libraryKey: choice?.key || '',
                libraryId: choice?.libraryId || String(binding.library_id || ''),
                libraryName: choice?.libraryName || String(binding.library_name || ''),
                local: String(binding.local_path || ''),
                server: String(binding.server_path || ''),
                sourceIds: [Number(binding.source_id || 0)],
                category: String(binding.category || 'default'),
                legacy: Boolean(provider && !choice),
            };
            const signature = JSON.stringify([
                row.provider, row.libraryId, row.libraryName, row.local,
                row.server, row.category, row.libraryKey, row.legacy,
            ]);
            const existing = groupedBindings.get(signature);
            if (existing) {
                existing.sourceIds = availableLocalSourceIds([...existing.sourceIds, ...row.sourceIds]);
            } else {
                row.sourceIds = availableLocalSourceIds(row.sourceIds);
                groupedBindings.set(signature, row);
            }
        }
        rows.push(...groupedBindings.values());
        state.rows = rows;
        state.dirty = false;
    }

    function renderSummary() {
        const summary = state.overview?.summary || {};
        const servers = state.overview?.servers || [];
        const strmCount = Number(summary.path_mappings || 0);
        const localCount = Number(summary.local_bindings || 0);
        animateCount('mlServerCount', summary.configured_servers);
        animateCount('mlLibraryCount', summary.libraries);
        animateCount('mlMappingCount', summary.total_mappings ?? (strmCount + localCount));
        animateCount('mlBindingCount', localCount);

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
        if (mappingFoot) mappingFoot.textContent = `${strmCount} 条 STRM · ${localCount} 条本地`;

        const bindingFoot = $('#mlBindingSummary');
        if (bindingFoot) {
            const sourceCount = Number(summary.local_sources ?? localSources().length);
            bindingFoot.textContent = sourceCount
                ? `${sourceCount} 个本地来源已接入`
                : '尚未配置本地媒体来源';
            bindingFoot.title = bindingFoot.textContent;
        }
    }

    function rowLocked(row) {
        if (row.kind !== 'strm') return false;
        const server = serverFor(row.provider);
        return Boolean(server && managedFields().has(server.mapping_key));
    }

    function libraryOptions(row) {
        const options = ['<option value="">请选择媒体库</option>'];
        if (row.legacy && row.provider) {
            const label = `${providerLabels[row.provider] || row.provider} · ${row.libraryName || row.server || '已有映射'}`;
            options.push(`<option value="__legacy__" selected>${esc(label)}</option>`);
        }
        for (const choice of libraryChoices()) {
            const selected = choice.key === row.libraryKey ? ' selected' : '';
            options.push(`<option value="${esc(choice.key)}"${selected}>${esc(choice.providerLabel)} · ${esc(choice.libraryName)}</option>`);
        }
        return options.join('');
    }

    function mappingRow(row, index) {
        const locked = rowLocked(row);
        const isLocal = row.kind === 'local';
        return `
            <article class="ml-library-mapping-row${locked ? ' is-locked' : ''}" data-mapping-row="${index}">
                <label class="ml-mapping-field ml-library-field">
                    <span class="ml-field-label">媒体库</span>
                    <select class="form-select" data-mapping-library ${locked ? 'disabled' : ''}>${libraryOptions(row)}</select>
                </label>
                <div class="ml-mapping-field ml-mapping-source">
                    <span class="ml-field-label">来源</span>
                    <div class="ml-source-control">
                        <button class="ml-source-toggle${isLocal ? ' is-local' : ''}" type="button" data-toggle-mapping-source
                            aria-label="当前来源为${isLocal ? `本地，自动归档为${categoryLabels[row.category] || '默认分类'}` : 'STRM'}，点击切换"
                            title="${isLocal ? `自动归档为${categoryLabels[row.category] || '默认分类'}；` : ''}点击切换为${isLocal ? 'STRM' : '本地'}" ${locked ? 'disabled' : ''}>
                            <i data-lucide="${isLocal ? 'hard-drive' : 'file-symlink'}"></i><span>${isLocal ? '本地' : 'STRM'}</span>
                        </button>
                    </div>
                </div>
                <label class="ml-mapping-field ml-directory-field">
                    <span class="ml-field-label">${isLocal ? '本地归档目录' : 'STRM 目录'}</span>
                    <span class="ml-directory-control">
                        <input class="form-input ml-directory-path" data-mapping-local-path value="${esc(row.local)}" readonly
                            placeholder="${isLocal ? '点击文件夹选择本地归档目录' : '点击文件夹选择真实 STRM 目录'}" title="${esc(row.local)}">
                        <button class="jump-btn ml-directory-picker-btn" type="button" data-pick-directory
                            aria-label="选择${isLocal ? '本地归档' : 'STRM'}目录" title="选择${isLocal ? '本地归档' : 'STRM'}目录" ${locked ? 'disabled' : ''}>
                            <i data-lucide="folder-open"></i>
                        </button>
                    </span>
                </label>
                <label class="ml-mapping-field ml-server-path-field">
                    <span class="ml-field-label">服务器路径</span>
                    <input class="form-input" data-mapping-server value="${esc(row.server)}"
                        placeholder="${row.provider ? '媒体服务器实际可见路径' : '请先选择媒体库'}"
                        ${locked || !row.provider ? 'disabled' : ''}>
                </label>
                <div class="ml-mapping-actions">
                    <button class="jump-btn" type="button" data-test-mapping aria-label="测试路径映射" title="测试路径映射"
                        ${locked || !row.provider ? 'disabled' : ''}><i data-lucide="scan-line"></i></button>
                    <button class="jump-btn danger" type="button" data-remove-mapping aria-label="删除映射" title="删除映射"
                        ${locked ? 'disabled' : ''}><i data-lucide="trash-2"></i></button>
                </div>
                <div class="ml-mapping-result" data-mapping-result></div>
                ${locked ? '<div class="ml-managed-note">该 STRM 映射由部署环境管理，本页仅展示。</div>' : ''}
            </article>`;
    }

    function canAddMapping() {
        if (!state.initialized) return false;
        const hasLocal = localSources().length > 0;
        const hasWritableStrm = Boolean(state.overview?.strm?.output_root)
            && configuredServers().some((server) => !managedFields().has(server.mapping_key));
        return hasLocal || hasWritableStrm;
    }

    function updateActions() {
        const addButton = $('[data-add-library-mapping]');
        const saveButton = $('[data-save-mappings]');
        if (addButton) addButton.disabled = !canAddMapping();
        if (saveButton) saveButton.disabled = !state.initialized || !state.dirty;
    }

    function renderRows() {
        const list = $('#mlMappingList');
        if (!list) return;
        if (!state.rows.length) {
            const hasSource = Boolean(state.overview?.strm?.output_root) || localSources().length > 0;
            list.innerHTML = hasSource
                ? '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="route"></i></div><strong>还没有路径映射</strong><p>点击右上角“添加映射”，选择 STRM 或本地来源，再设置目录与媒体服务器路径。</p></div>'
                : '<div class="ml-empty-state"><div class="ml-empty-icon"><i data-lucide="folder-x"></i></div><strong>没有可用来源目录</strong><p>请先完成一次 STRM 同步，或在“本地整理”中新增媒体来源。</p></div>';
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
        if (!selectedKey) {
            row.provider = '';
            row.libraryKey = '';
            row.libraryId = '';
            row.libraryName = '';
            row.server = '';
            row.legacy = false;
        } else {
            const choice = choiceForKey(selectedKey);
            if (choice) {
                row.libraryKey = choice.key;
                row.libraryId = choice.libraryId;
                row.libraryName = choice.libraryName;
                row.provider = choice.provider;
                row.legacy = false;
            }
        }
        row.local = $('[data-mapping-local-path]', rowElement)?.value.trim() || row.local || '';
        row.server = $('[data-mapping-server]', rowElement)?.value.trim() || '';
        return row;
    }

    function resetResult(rowElement) {
        const result = $('[data-mapping-result]', rowElement);
        if (!result) return;
        result.className = 'ml-mapping-result';
        result.textContent = '';
    }

    function openMappingDirectoryPicker(rowElement) {
        const index = Number(rowElement.dataset.mappingRow);
        const row = state.rows[index];
        if (!row) return;
        if (typeof window.openGuangYaDirectoryPicker !== 'function') {
            window.showToast?.('目录选择器尚未加载，请刷新页面后重试', 'error', 4200);
            return;
        }

        const isLocal = row.kind === 'local';
        const outputRoot = String(state.overview?.strm?.output_root || '').trim();
        if (!isLocal && !outputRoot) {
            window.showToast?.('尚未配置可浏览的 STRM 输出目录', 'warning', 4200);
            return;
        }
        const rootId = isLocal ? '__roots__' : outputRoot;
        window.openGuangYaDirectoryPicker({
            modalId: isLocal ? 'mediaLibraryLocalDirModal' : 'mediaLibraryStrmDirModal',
            title: isLocal ? '选择本地归档目录' : '选择 STRM 目录',
            rootId,
            rootName: isLocal ? '容器目录' : 'STRM 输出',
            allowRoot: true,
            fetchDirectory: async (path) => {
                const requested = String(path || rootId);
                const query = new URLSearchParams({path: requested});
                const endpoint = isLocal
                    ? '/api/media-libraries/local-directories'
                    : '/api/media-libraries/strm-directories';
                const data = await api(`${endpoint}?${query}`);
                return Array.isArray(data.directories) ? data.directories : [];
            },
            onSelect: (selected) => {
                const localPath = String(selected?.id || '').trim();
                if (!localPath || localPath === '__roots__') return false;
                row.local = localPath;
                state.dirty = true;
                renderRows();
                const renderedRow = $(`[data-mapping-row="${index}"]`, $('#mlMappingList'));
                $('[data-pick-directory]', renderedRow)?.focus();
                return true;
            },
        });
    }

    function applyChoiceToRow(row, choice, {replaceServer = true} = {}) {
        if (!choice) return;
        row.provider = choice.provider;
        row.libraryKey = choice.key;
        row.libraryId = choice.libraryId;
        row.libraryName = choice.libraryName;
        row.legacy = false;
        if (row.kind === 'local') row.category = inferLocalCategory(choice);
        if (replaceServer) row.server = String(choice.locations[0] || '');
    }

    function addLibraryMapping() {
        const choices = libraryChoices();
        const writableChoices = choices.filter((choice) => {
            const server = serverFor(choice.provider);
            return server && !managedFields().has(server.mapping_key);
        });
        const hasStrm = Boolean(state.overview?.strm?.output_root) && writableChoices.length > 0;
        const source = localSources()[0];
        const choice = writableChoices[0] || choices[0] || null;
        if (!hasStrm && !source) {
            window.showToast?.('没有可添加的来源目录', 'warning', 4200);
            return;
        }
        const row = {
            kind: hasStrm ? 'strm' : 'local',
            provider: '', libraryKey: '', libraryId: '', libraryName: '',
            local: '', server: '', sourceIds: source ? allLocalSourceIds() : [],
            category: 'default', legacy: false,
        };
        if (choice) applyChoiceToRow(row, choice);
        state.rows.push(row);
        state.dirty = true;
        renderRows();
        const rows = $$('[data-mapping-row]', $('#mlMappingList'));
        $('[data-pick-directory]', rows.at(-1))?.focus();
    }

    async function testMapping(rowElement, button) {
        const row = syncRow(rowElement);
        const resultNode = $('[data-mapping-result]', rowElement);
        if (!row?.provider || !row.local || !row.server) {
            resultNode.className = 'ml-mapping-result is-error';
            resultNode.textContent = '请选择媒体库、来源目录并填写服务器路径';
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
            const selectedMatch = !row.libraryId || result.matches.some(
                (item) => String(item.id || '') === row.libraryId,
            );
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

    function showRowError(rowElements, index, message) {
        const result = $('[data-mapping-result]', rowElements[index]);
        if (!result) return false;
        result.className = 'ml-mapping-result is-error';
        result.textContent = message;
        rowElements[index].scrollIntoView({behavior: 'smooth', block: 'nearest'});
        return false;
    }

    async function saveMappings(button) {
        const rowElements = $$('[data-mapping-row]', $('#mlMappingList'));
        rowElements.forEach(syncRow);
        const seenStrm = new Set();
        const seenLocal = new Set();
        for (const [index, row] of state.rows.entries()) {
            if (rowLocked(row)) continue;
            if (row.kind === 'strm') {
                if (!row.provider || !row.local || !row.server) {
                    showRowError(rowElements, index, 'STRM 映射需要选择媒体库、STRM 目录和服务器路径');
                    return;
                }
                const key = `${row.provider}:${pathKey(row.local)}`;
                if (seenStrm.has(key)) {
                    showRowError(rowElements, index, '同一服务器不能重复映射相同 STRM 目录');
                    return;
                }
                seenStrm.add(key);
            } else {
                row.sourceIds = availableLocalSourceIds(row.sourceIds);
                if (!row.sourceIds.length || !categoryLabels[row.category] || !row.local) {
                    showRowError(rowElements, index, '本地映射需要可用的本地来源和本地归档目录');
                    return;
                }
                if (!row.provider || !row.libraryName || !row.server) {
                    showRowError(rowElements, index, '本地映射需要选择媒体库并填写服务器路径');
                    return;
                }
                for (const sourceId of row.sourceIds) {
                    const key = `${sourceId}:${row.category}`;
                    if (seenLocal.has(key)) {
                        showRowError(rowElements, index, '同一归档分类只能为每个本地来源配置一次');
                        return;
                    }
                    seenLocal.add(key);
                }
            }
        }

        const strmMappings = {};
        for (const server of state.overview?.servers || []) {
            if (managedFields().has(server.mapping_key)) continue;
            strmMappings[server.server_type] = state.rows
                .filter((row) => row.kind === 'strm' && row.provider === server.server_type)
                .map((row) => ({local: row.local, server: row.server}));
        }
        const localBindings = state.rows
            .filter((row) => row.kind === 'local')
            .flatMap((row) => availableLocalSourceIds(row.sourceIds).map((sourceId) => ({
                source_id: sourceId,
                category: row.category,
                local_path: row.local,
                provider: row.provider,
                library_id: row.libraryId,
                library_name: row.libraryName,
                server_path: row.server,
            })));

        setBusy(button, true, '保存中');
        try {
            await api('/api/media-libraries/mappings', {
                method: 'POST',
                body: JSON.stringify({strm_mappings: strmMappings, local_bindings: localBindings}),
            });
            state.dirty = false;
            window.showToast?.('媒体库与路径映射已保存', 'success');
            await loadAll({reloadRows: true});
        } catch (error) {
            window.showToast?.(error.message, 'error', 4200);
        } finally {
            setBusy(button, false, '保存映射');
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
        if (event.target.closest('[data-toggle-mapping-source]')) {
            const row = state.rows[index];
            if (!row) return;
            if (row.kind === 'strm') {
                const source = localSources()[0];
                if (!source) {
                    window.showToast?.('请先在“本地整理”中新增媒体来源', 'warning', 4200);
                    return;
                }
                row.kind = 'local';
                row.sourceIds = allLocalSourceIds();
                row.category = inferLocalCategory(choiceForKey(row.libraryKey));
            } else {
                row.kind = 'strm';
                row.sourceIds = [];
                row.category = 'default';
                if (!row.provider) applyChoiceToRow(row, libraryChoices()[0]);
            }
            row.local = '';
            row.legacy = false;
            state.dirty = true;
            renderRows();
            const renderedRow = $(`[data-mapping-row="${index}"]`, $('#mlMappingList'));
            $('[data-toggle-mapping-source]', renderedRow)?.focus();
            return;
        }
        if (event.target.closest('[data-pick-directory]')) {
            openMappingDirectoryPicker(rowElement);
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
            const selectedKey = event.target.value;
            if (!selectedKey) {
                row.provider = '';
                row.libraryKey = '';
                row.libraryId = '';
                row.libraryName = '';
                row.server = '';
                row.legacy = false;
            } else {
                const choice = choiceForKey(selectedKey);
                if (choice) {
                    const replaceServer = !row.server || oldLocations.some(
                        (location) => pathKey(location) === pathKey(row.server),
                    );
                    applyChoiceToRow(row, choice, {replaceServer});
                }
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
