(() => {
    const $ = (id) => document.getElementById(id);
    const esc = (value) => {
        const node = document.createElement('div');
        node.textContent = value == null ? '' : String(value);
        return node.innerHTML;
    };
    const api = async (path, options = {}) => {
        const response = await fetch(path, {
            headers: {'Content-Type': 'application/json'},
            ...options,
        });
        let data = {};
        try { data = await response.json(); } catch {}
        if (!response.ok) throw new Error(data.error || '请求失败');
        return data;
    };

    const categories = [
        ['default', '默认'], ['movie', '电影'], ['tv', '剧集'], ['anime', '动漫'],
        ['documentary', '纪录片'], ['variety', '综艺'], ['concert', '演唱会'], ['kids', '儿童'],
    ];
    const categoryLabels = Object.fromEntries(categories);
    const windowsPackage = document.querySelector('.lm-page')?.dataset.windowsPackage === '1';
    const states = {
        waiting_stable: ['等待稳定', 'paused'], recognizing: ['识别中', 'running'],
        requires_manual: ['待确认', 'paused'], planned: ['已规划', 'running'],
        moving: ['移动中', 'running'], verifying: ['校验中', 'running'],
        refreshing: ['刷新中', 'running'], completed: ['已完成', 'done'],
        failed: ['失败', 'failed'], rolling_back: ['回滚中', 'failed'],
    };
    const triggerLabels = {
        manual: '手动整理', qb_completed: 'qB 完成', scan: '目录扫描', retry: '重新入队',
    };

    let sources = [];
    let tasks = [];
    let mediaItems = [];
    let mediaItemSources = [];
    let inspection = null;
    let selectedCandidate = null;
    let lastPreview = null;
    let appliedPreviewContext = null;
    let previewRequestSerial = 0;
    let searchRequestSerial = 0;
    let inspectionRequestSerial = 0;
    let hasLoadedLocalMedia = false;
    let refreshing = false;
    let refreshQueued = false;
    let queuedManualRefresh = false;
    let refreshPromise = Promise.resolve();
    let sourceSignature = '';
    let taskSignature = '';
    let renderedFilter = '';
    let itemSignature = '';
    let renderedItemFilter = '';
    let activeMediaItem = null;
    let activeContextItem = null;
    let activeContextTrigger = null;
    const mediaItemMap = new Map();
    let mediaServers = [];
    let taskDisplayLimit = 60;
    let pollTimer = null;
    const libraryCache = new Map();
    const activeTaskStates = new Set(['waiting_stable', 'recognizing', 'planned', 'moving', 'verifying', 'refreshing', 'rolling_back']);

    function icons(root = document) {
        if (window.renderLucideIcons) window.renderLucideIcons(root);
        else if (window.lucide) window.lucide.createIcons({attrs: {'stroke-width': 1.8, 'aria-hidden': 'true'}, nameAttr: 'data-lucide', root});
    }

    function setBusy(button, busy, label) {
        if (!button) return;
        button.disabled = busy;
        const text = button.querySelector('[data-button-label]') || button.lastChild;
        if (text) text.textContent = label;
    }

    function lockElementHeight(element) {
        if (!element) return () => {};
        const height = element.getBoundingClientRect().height;
        if (height > 0) element.style.minHeight = `${Math.ceil(height)}px`;
        return () => window.requestAnimationFrame(() => window.requestAnimationFrame(() => { element.style.minHeight = ''; }));
    }

    function animateLocalCount(id, value, animate = true) {
        const target = $(id);
        if (!target) return;
        if (animate && window.MFAnim) window.MFAnim.countUp(target, value, {duration: 0.6});
        else target.textContent = String(value);
    }

    function pollingDelay() {
        return tasks.some((task) => activeTaskStates.has(task.status)) ? 10000 : 45000;
    }

    function invalidatePreview() {
        previewRequestSerial += 1;
        selectedCandidate = null;
        lastPreview = null;
        appliedPreviewContext = null;
        if ($('lmExecuteBtn')) $('lmExecuteBtn').disabled = true;
    }

    function setPreviewControlsBusy(busy) {
        document.querySelectorAll('[data-candidate]').forEach((button) => { button.disabled = busy; });
        setBusy($('lmAutoPreviewBtn'), busy, busy ? '生成计划中' : '自动匹配');
        if (busy && $('lmExecuteBtn')) $('lmExecuteBtn').disabled = true;
    }

    function setInspectionControlsBusy(busy, activeButton = null) {
        document.querySelectorAll('[data-review-task], [data-open-item-menu]').forEach((button) => {
            if (button !== activeButton) button.disabled = busy;
        });
    }

    function renderInitialLoadFailure(message) {
        const text = esc(message || '无法读取本地媒体数据，请稍后重试。');
        $('lmSourceGrid').innerHTML = `<div class="lm-empty-state is-error"><div class="lm-empty-icon is-error"><i data-lucide="alert-circle"></i></div><strong>来源读取失败</strong><p>${text}</p></div>`;
        $('lmReviewList').innerHTML = `<div class="lm-empty-state is-error"><div class="lm-empty-icon is-error"><i data-lucide="alert-circle"></i></div><strong>待确认任务读取失败</strong><p>${text}</p></div>`;
        $('lmTaskList').innerHTML = `<tr class="is-empty-row"><td colspan="6" class="table-empty"><div class="lm-table-empty-wrap"><i data-lucide="alert-circle"></i><span>任务读取失败：${text}</span></div></td></tr>`;
        $('lmMediaItems').innerHTML = `<div class="lm-empty-state is-error"><div class="lm-empty-icon is-error"><i data-lucide="alert-circle"></i></div><strong>本地条目读取失败</strong><p>${text}</p></div>`;
        icons($('lmSourceGrid'));
        icons($('lmReviewList'));
        icons($('lmTaskList'));
        icons($('lmMediaItems'));
    }

    function setRefreshFailure(error = null) {
        const button = $('lmRefreshBtn');
        const message = error?.message || '';
        document.querySelector('.lm-page')?.toggleAttribute('data-refresh-error', Boolean(message));
        if (button) button.title = message ? `最近一次自动刷新失败：${message}` : '刷新本地媒体数据';
    }

    function schedulePoll() {
        window.clearTimeout(pollTimer);
        pollTimer = null;
        if (document.hidden) return;
        pollTimer = window.setTimeout(async () => {
            await loadAll(false);
            schedulePoll();
        }, pollingDelay());
    }

    function sourceCard(source) {
        const targetItems = source.targets || [];
        const automatic = source.enabled || source.scan_enabled;
        const triggerText = [source.enabled ? 'qB 完成' : '', source.scan_enabled ? '定时扫描' : ''].filter(Boolean).join(' + ') || '仅手动';
        const targetPaths = targetItems.map((target) => `${categoryLabels[target.category] || target.category}: ${target.path}`).join('\n');
        const chips = targetItems.length
            ? targetItems.slice(0, 4).map((target) => `<span title="${esc(target.path)}">${esc(categoryLabels[target.category] || target.category)}</span>`).join('')
                + (targetItems.length > 4 ? `<span>+${targetItems.length - 4}</span>` : '')
            : '<span>未配置目标</span>';
        return `
            <article class="lm-source-card ${automatic ? '' : 'is-disabled'}">
                <div class="lm-source-top">
                    <span class="lm-source-icon"><i data-lucide="folder-cog"></i></span>
                    <div class="lm-source-identity">
                        <strong title="${esc(source.name)}">${esc(source.name)}</strong>
                        <small>${esc(triggerText)} · ${targetItems.length} 个归档目标</small>
                    </div>
                    <span class="status-pill ${automatic ? 'done' : ''}">${automatic ? '自动' : '手动'}</span>
                </div>
                <div class="lm-source-route">
                    <div><b>qB 路径</b><span title="${esc(source.qb_path_prefix)}">${esc(source.qb_path_prefix || '与本地路径相同')}</span></div>
                    <div><b>本机路径</b><span title="${esc(source.local_root)}">${esc(source.local_root)}</span></div>
                </div>
                <div class="lm-source-targets" title="${esc(targetPaths)}">${chips}</div>
                <div class="lm-source-actions">
                    <button class="jump-btn" type="button" data-edit-source="${source.id}"><i data-lucide="settings-2"></i>编辑</button>
                    <button class="jump-btn danger" type="button" data-delete-source="${source.id}"><i data-lucide="trash-2"></i>删除</button>
                </div>
            </article>`;
    }

    function renderSources(force = false, animate = false) {
        const signature = JSON.stringify(sources);
        if (!force && signature === sourceSignature) return;
        sourceSignature = signature;
        const grid = $('lmSourceGrid');
        const releaseHeight = animate ? lockElementHeight(grid) : () => {};
        grid.innerHTML = sources.length
            ? sources.map(sourceCard).join('')
            : `<div class="lm-empty-state">
                <div class="lm-empty-icon"><i data-lucide="folder-plus"></i></div>
                <strong>暂无媒体来源配置</strong>
                <p>配置 qBittorrent 下载路径或本地媒体目录映射，即可开启自动整理与刮削。</p>
            </div>`;
        animateLocalCount('lmSourceCount', sources.length, animate);
        const sourcesBadge = $('lmSourcesTabBadge');
        if (sourcesBadge) sourcesBadge.textContent = sources.length;

        const select = $('lmItemSourceFilter');
        const previous = select?.value || '';
        if (select) {
            select.innerHTML = '<option value="">全部来源</option>' + sources
                .map((source) => `<option value="${source.id}">${esc(source.name)}</option>`)
                .join('');
            if (sources.some((source) => String(source.id) === previous)) select.value = previous;
        }
        icons(grid);
        releaseHeight();
    }

    function taskRow(task) {
        const state = states[task.status] || [task.status, ''];
        const retry = task.status === 'requires_manual'
            ? `<button class="jump-btn" type="button" data-review-task="${task.id}"><i data-lucide="search-check"></i>人工确认</button>`
            : task.status === 'failed'
                ? `<button class="jump-btn" type="button" data-retry="${task.id}"><i data-lucide="rotate-ccw"></i>重试</button>`
                : '<span class="text-muted">—</span>';
        return `
            <tr>
                <td class="lm-task-title" data-label="任务"><strong>${esc(task.content_name || '未命名')}</strong><span>${esc(task.error || task.warning || '')}</span></td>
                <td data-label="来源">${esc(task.source_name || '—')}</td>
                <td data-label="触发">${esc(triggerLabels[task.trigger] || task.trigger || '—')}</td>
                <td data-label="状态"><span class="status-pill ${state[1]}">${esc(state[0])}</span></td>
                <td data-label="更新时间">${esc(task.updated_at || task.created_at || '—')}</td>
                <td data-label="操作">${retry}</td>
            </tr>`;
    }

    function renderTasks(force = false, animate = false) {
        const filter = $('lmTaskFilter').value;
        const signature = JSON.stringify(tasks.map((task) => [
            task.id, task.content_name, task.source_name, task.trigger, task.status,
            task.updated_at, task.created_at, task.error, task.warning,
        ]));
        if (!force && signature === taskSignature && filter === renderedFilter) return;
        taskSignature = signature;
        renderedFilter = filter;

        const filtered = filter ? tasks.filter((task) => task.status === filter) : tasks;
        const visible = filtered.slice(0, taskDisplayLimit);
        const taskList = $('lmTaskList');
        const releaseTaskHeight = animate ? lockElementHeight(taskList.closest('.lm-task-table-wrap')) : () => {};
        taskList.innerHTML = visible.length
            ? visible.map(taskRow).join('')
            : `<tr class="is-empty-row"><td colspan="6" class="table-empty"><div class="lm-table-empty-wrap"><i data-lucide="inbox"></i><span>${filter ? '暂无符合筛选条件的状态任务' : '暂无整理任务'}</span></div></td></tr>`;

        const remaining = Math.max(0, filtered.length - visible.length);
        $('lmTaskMore').hidden = remaining === 0;
        $('lmLoadMoreTasksBtn').querySelector('[data-button-label]').textContent = remaining
            ? `再显示 ${Math.min(60, remaining)} 条 · 剩余 ${remaining} 条`
            : '显示更多';

        const review = tasks.filter((task) => task.status === 'requires_manual');
        const reviewList = $('lmReviewList');
        const releaseReviewHeight = animate ? lockElementHeight(reviewList) : () => {};
        reviewList.innerHTML = review.length
            ? review.slice(0, 30).map((task) => `
                <article class="lm-review-item">
                    <div><strong>${esc(task.content_name || '未命名')}</strong><span>${esc(task.error || '需要重新指定 TMDB 信息')}</span></div>
                    <button class="jump-btn" type="button" data-review-task="${task.id}"><i data-lucide="search-check"></i>人工确认</button>
                </article>`).join('')
            : `<div class="lm-empty-state">
                <div class="lm-empty-icon is-success"><i data-lucide="check-circle-2"></i></div>
                <strong>暂无待确认任务</strong>
                <p>当前所有媒体任务识别置信度正常，无命名冲突或需人工复核的条目。</p>
            </div>`;

        animateLocalCount('lmReviewCount', review.length, animate);
        $('lmReviewBadge').textContent = review.length;
        const tasksBadge = $('lmTasksTabBadge');
        if (tasksBadge) tasksBadge.textContent = tasks.length;
        const reviewTabBtn = $('lmTabReviewBtn');
        if (reviewTabBtn) reviewTabBtn.classList.toggle('has-issues', review.length > 0);
        animateLocalCount('lmPendingCount', tasks.filter((task) => !['completed', 'failed'].includes(task.status)).length, animate);
        icons(taskList);
        icons(reviewList);
        releaseTaskHeight();
        releaseReviewHeight();
    }

    function formatItemSize(value) {
        const size = Number(value);
        if (!Number.isFinite(size) || size < 0) return '—';
        if (size < 1024) return `${size} B`;
        const units = ['KB', 'MB', 'GB', 'TB'];
        let current = size / 1024;
        let index = 0;
        while (current >= 1024 && index < units.length - 1) {
            current /= 1024;
            index += 1;
        }
        return `${current >= 10 ? current.toFixed(0) : current.toFixed(1)} ${units[index]}`;
    }

    function formatItemDate(value) {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '—';
        return date.toLocaleString('zh-CN', {hour12: false});
    }

    function itemToken(item) {
        return encodeURIComponent(`${item.source_id}\u0000${item.path}`);
    }

    function renderMediaItems(force = false, animate = false) {
        const query = ($('lmItemFilter')?.value || '').trim().toLocaleLowerCase();
        const sourceFilter = $('lmItemSourceFilter')?.value || '';
        const signature = JSON.stringify(mediaItems.map((item) => [
            item.source_id, item.name, item.path, item.kind, item.size, item.modified_at,
            item.organize_ready, item.identity,
        ]));
        const filterSignature = `${query}\u0000${sourceFilter}`;
        if (!force && signature === itemSignature && filterSignature === renderedItemFilter) return;
        itemSignature = signature;
        renderedItemFilter = filterSignature;
        mediaItemMap.clear();
        mediaItems.forEach((item) => mediaItemMap.set(itemToken(item), item));
        const visible = mediaItems.filter((item) => {
            if (sourceFilter && String(item.source_id) !== sourceFilter) return false;
            if (!query) return true;
            return `${item.name} ${item.source_name} ${item.path}`.toLocaleLowerCase().includes(query);
        });
        const list = $('lmMediaItems');
        const releaseHeight = animate ? lockElementHeight(list) : () => {};
        list.innerHTML = visible.length
            ? visible.map((item) => {
                const token = itemToken(item);
                const kindLabel = item.kind === 'directory' ? '目录' : '视频';
                const note = item.organize_ready ? item.path : '未配置可写归档目标，仅可移入回收区';
                return `
                    <article class="lm-media-row ${item.organize_ready ? '' : 'is-unready'}" data-media-item="${esc(token)}" tabindex="0">
                        <div class="lm-media-name">
                            <span class="lm-media-icon ${item.kind === 'video' ? 'is-video' : ''}"><i data-lucide="${item.kind === 'video' ? 'file-video-2' : 'folder'}"></i></span>
                            <span><strong title="${esc(item.name)}">${esc(item.name)}</strong><small title="${esc(note)}">${esc(note)}</small></span>
                        </div>
                        <span class="lm-media-cell" title="${esc(item.source_name)}">${esc(item.source_name)}</span>
                        <span class="lm-media-cell"><span class="lm-media-kind">${kindLabel}</span></span>
                        <span class="lm-media-cell">${esc(formatItemDate(item.modified_at))}</span>
                        <span class="lm-media-cell">${item.kind === 'video' ? esc(formatItemSize(item.size)) : '—'}</span>
                        <button class="jump-btn lm-media-menu-btn" type="button" data-open-item-menu aria-label="打开 ${esc(item.name)} 操作菜单" title="更多操作"><i data-lucide="ellipsis"></i></button>
                    </article>`;
            }).join('')
            : `<div class="lm-empty-state">
                <div class="lm-empty-icon"><i data-lucide="${sources.length ? 'search-x' : 'folder-plus'}"></i></div>
                <strong>${sources.length ? '没有匹配的本地媒体条目' : '暂无媒体来源配置'}</strong>
                <p>${sources.length ? '调整名称或来源筛选；新下载内容会在刷新后显示。' : '先在「媒体来源」中配置本地目录和归档目标。'}</p>
            </div>`;
        const failures = mediaItemSources.filter((source) => source.error);
        const errorBox = $('lmMediaSourceErrors');
        errorBox.hidden = failures.length === 0;
        errorBox.textContent = failures.length
            ? failures.map((source) => `${source.name}：${source.error}`).join('；')
            : '';
        animateLocalCount('lmManualState', mediaItems.length, animate);
        icons(list);
        releaseHeight();
    }

    function loadAll(manual = false) {
        refreshQueued = true;
        queuedManualRefresh = queuedManualRefresh || manual;
        if (refreshing) return refreshPromise;

        refreshing = true;
        let manualIndicatorVisible = false;
        refreshPromise = (async () => {
            try {
                while (refreshQueued) {
                    const currentManual = queuedManualRefresh;
                    refreshQueued = false;
                    queuedManualRefresh = false;
                    if (currentManual && !manualIndicatorVisible) {
                        manualIndicatorVisible = true;
                        setBusy($('lmRefreshBtn'), true, '刷新中');
                        document.querySelector('.lm-page')?.setAttribute('aria-busy', 'true');
                    }
                    try {
                        const shouldLoadItems = currentTab === 'manual' || currentManual;
                        const [sourceData, taskData, serverData, itemData] = await Promise.all([
                            api('/api/local-media/sources'),
                            api('/api/local-media/tasks'),
                            api('/api/local-media/media-servers'),
                            shouldLoadItems ? api('/api/local-media/items') : Promise.resolve(null),
                        ]);
                        mediaServers = serverData.servers || [];
                        sources = sourceData.sources || [];
                        tasks = taskData.tasks || [];
                        if (itemData) {
                            mediaItems = itemData.items || [];
                            mediaItemSources = itemData.sources || [];
                        }
                        const animate = !hasLoadedLocalMedia || currentManual;
                        renderSources(false, animate);
                        renderTasks(false, animate);
                        if (itemData) renderMediaItems(false, animate);
                        hasLoadedLocalMedia = true;
                        setRefreshFailure();
                        if (currentManual && window.appAlert) {
                            appAlert({type: 'success', title: '已刷新', message: '媒体来源、整理任务和本地条目已更新。'});
                        }
                    } catch (error) {
                        if (!hasLoadedLocalMedia) renderInitialLoadFailure(error.message);
                        setRefreshFailure(error);
                        if (currentManual && window.appAlert) {
                            appAlert({type: 'error', title: '读取失败', message: error.message});
                        }
                    }
                }
            } finally {
                refreshing = false;
                if (manualIndicatorVisible) {
                    setBusy($('lmRefreshBtn'), false, '刷新');
                    document.querySelector('.lm-page')?.removeAttribute('aria-busy');
                }
            }
        })();
        return refreshPromise;
    }

    function providerOptions(selected = '') {
        const known = mediaServers.some((item) => item.provider === selected);
        const unavailable = selected && !known
            ? `<option value="${esc(selected)}" selected>${esc(selected)}（当前不可用）</option>` : '';
        return `<option value="">自动按路径刷新</option>${mediaServers.map((item) =>
            `<option value="${esc(item.provider)}" ${item.provider === selected ? 'selected' : ''}>${esc(item.label)}</option>`
        ).join('')}${unavailable}`;
    }

    async function librariesFor(provider) {
        if (!provider) return [];
        if (!libraryCache.has(provider)) {
            libraryCache.set(provider, api(`/api/local-media/media-servers/${encodeURIComponent(provider)}/libraries`)
                .then((data) => data.libraries || []).catch((error) => { libraryCache.delete(provider); throw error; }));
        }
        return libraryCache.get(provider);
    }

    async function populateLibrarySelect(row, selected = {}) {
        const provider = row.querySelector('[data-target-provider]').value;
        const select = row.querySelector('[data-target-library]');
        const requestVersion = Number(row.dataset.libraryRequest || 0) + 1;
        row.dataset.libraryRequest = String(requestVersion);
        select.disabled = !provider;
        select.innerHTML = `<option value="">${provider ? '正在读取媒体库…' : '未绑定媒体库'}</option>`;
        if (!provider) return;
        try {
            const libraries = await librariesFor(provider);
            if (row.dataset.libraryRequest !== String(requestVersion)
                || row.querySelector('[data-target-provider]').value !== provider) return;
            const selectedId = selected.id || '';
            const selectedName = selected.name || '';
            select.innerHTML = '<option value="">请选择媒体库</option>' + libraries.map((item) => {
                const isSelected = item.id === selectedId || (!selectedId && item.name === selectedName);
                return `<option value="${esc(item.id)}" data-name="${esc(item.name)}" ${isSelected ? 'selected' : ''}>${esc(item.name)}</option>`;
            }).join('');
            const matched = libraries.find((item) => item.id === selectedId || (!selectedId && item.name === selectedName));
            if (matched) {
                row.dataset.libraryId = matched.id;
                row.dataset.libraryName = matched.name;
            } else if (selectedId || selectedName) {
                const fallbackId = selectedId || '';
                select.insertAdjacentHTML('beforeend', `<option value="${esc(fallbackId)}" data-name="${esc(selectedName)}" selected>${esc(selectedName || selectedId)}（当前不可用）</option>`);
            }
        } catch (error) {
            if (row.dataset.libraryRequest !== String(requestVersion)
                || row.querySelector('[data-target-provider]').value !== provider) return;
            const selectedId = selected.id || '';
            const selectedName = selected.name || '';
            select.innerHTML = selectedId || selectedName
                ? `<option value="${esc(selectedId)}" data-name="${esc(selectedName)}" selected>${esc(selectedName || selectedId)}（读取失败）</option>`
                : '<option value="">媒体库读取失败</option>';
        }
    }

    function targetRows(values = {}) {
        $('lmTargetRows').innerHTML = categories.map(([key, label]) => {
            const target = values[key] || {};
            return `<div class="lm-target-row" data-target-row="${key}" data-library-id="${esc(target.library_id || '')}" data-library-name="${esc(target.library_name || '')}">
                <span>${label}</span>
                <div class="lm-target-fields">
                    <span class="lm-path-control">
                        <input class="form-input" data-target-path="${key}" value="${esc(target.path || '')}" placeholder="未配置">
                        <button class="jump-btn lm-picker-btn" type="button" data-pick-target="${key}" aria-label="选择${label}目录" title="选择${label}目录"><i data-lucide="folder-open"></i></button>
                    </span>
                    <span class="lm-target-binding">
                        <select class="form-select" data-target-provider>${providerOptions(target.provider || '')}</select>
                        <select class="form-select" data-target-library disabled><option value="">未绑定媒体库</option></select>
                    </span>
                </div>
            </div>`;
        }).join('');
        document.querySelectorAll('[data-target-row]').forEach((row) => {
            const target = values[row.dataset.targetRow] || {};
            populateLibrarySelect(row, {id: target.library_id || '', name: target.library_name || ''});
        });
        icons($('lmTargetRows'));
    }

    function windowsUncRoot(value) {
        if (!windowsPackage) return '';
        const normalized = String(value || '').trim();
        const match = normalized.match(/^(\\\\[^\\]+\\[^\\]+)/);
        return match ? match[1] : '';
    }

    function updateSmbAuthGuidance() {
        const localRootInput = $('lmLocalRoot');
        const smbUser = $('lmSmbUser');
        const smbPass = $('lmSmbPass');
        const smbAuthSection = $('lmSmbAuthSection');
        const hintEl = document.querySelector('.lm-network-hint span');
        if (!localRootInput || !smbAuthSection) return;

        const pathVal = localRootInput.value.trim();
        const isUnc = /^(\\\\[^\\]+|[\/]{2}[^\/]+)/.test(pathVal);
        const isMappedDrive = /^[a-zA-Z]:/.test(pathVal);
        const hasCredentials = Boolean((smbUser?.value || '').trim() || (smbPass?.value || '').trim() || (smbPass?.placeholder || '').includes('已保存'));

        if ((isUnc || isMappedDrive) && !hasCredentials) {
            smbAuthSection.classList.add('is-highlighted');
            if (hintEl) {
                hintEl.innerHTML = isUnc
                    ? '检测到 UNC 网络共享路径。若 NAS 未开启匿名访问，请在下方填写<strong>「NAS 访问账号」</strong>与<strong>「NAS 访问密码」</strong>。'
                    : '若当前盘符为网络映射驱动器且需要身份验证，请在下方填写 NAS 访问账号和密码以便自动挂载。';
            }
        } else {
            smbAuthSection.classList.remove('is-highlighted');
            if (hintEl) {
                hintEl.innerHTML = 'SMB 网络共享请直接填写 <code>\\\\NAS\\共享名</code>。若 NAS 设置了账号密码，可在下方配置访问凭据自动挂载。';
            }
        }
    }

    function openLocalDirectoryPicker(input, {sourceId = 0, rootId = '__roots__', rootName = '本机目录'} = {}) {
        if (!window.openGuangYaDirectoryPicker) return;
        const currentValue = String(input?.value || '').trim();
        const networkRoot = windowsUncRoot(currentValue);
        const smbUser = $('lmSmbUser')?.value.trim() || '';
        const smbPass = $('lmSmbPass')?.value.trim() || '';

        // rootId 基准入口始终支持从 __roots__ 导航。
        // 若指定了具体 sourceId 作用域，则锁定在该来源的 rootId；否则始终基于 __roots__ 导航。
        const effectiveRootId = sourceId ? rootId : '__roots__';
        const effectiveRootName = sourceId ? rootName : '本机目录';
        const isRootsMode = effectiveRootId === '__roots__';

        window.openGuangYaDirectoryPicker({
            modalId: 'localMediaDirModal',
            title: networkRoot ? '选择 SMB 网络目录' : '选择本地目录',
            rootId: effectiveRootId,
            rootName: effectiveRootName,
            allowRoot: !isRootsMode && Boolean(sourceId || networkRoot),
            fetchDirectory: async (path) => {
                const isRootsPath = !path || path === '__roots__';
                const query = new URLSearchParams({path});
                if (networkRoot && !isRootsPath) {
                    query.set('network_root', networkRoot);
                }
                if (sourceId) {
                    query.set('source_id', String(sourceId));
                }
                if (smbUser) query.set('smb_user', smbUser);
                if (smbPass) query.set('smb_pass', smbPass);

                try {
                    const data = await api(`/api/local-media/directories?${query.toString()}`);
                    return (data.directories || []).map((item) => {
                        const itemId = String(item.id ?? item.path ?? item.file_id ?? '');
                        return {
                            id: itemId,
                            file_id: itemId,
                            name: item.name || itemId,
                            path: item.path || itemId,
                            is_dir: true,
                        };
                    });
                } catch (error) {
                    // 若非 __roots__ 且无 sourceId 强作用域限制时加载失败（例如 1326 认证失败），安全降级回退到 __roots__ 并展示所有可用盘符
                    if (!isRootsPath && !sourceId) {
                        try {
                            const fallbackData = await api('/api/local-media/directories?path=__roots__');
                            return (fallbackData.directories || []).map((item) => {
                                const itemId = String(item.id ?? item.path ?? item.file_id ?? '');
                                return {
                                    id: itemId,
                                    file_id: itemId,
                                    name: item.name || itemId,
                                    path: item.path || itemId,
                                    is_dir: true,
                                };
                            });
                        } catch (fallbackErr) {
                            throw error;
                        }
                    }
                    throw error;
                }
            },
            onSelect: (directory) => {
                const rawId = directory?.id ?? directory?.file_id ?? directory?.path ?? '';
                const safeId = String(rawId).trim();
                const lower = safeId.toLowerCase();
                if (!safeId || lower === '__roots__' || lower === '0' || lower === 'undefined' || lower === 'null' || lower === '[object object]') {
                    return false;
                }
                input.value = safeId;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            },
        });
    }

    const sourceModal = window.createAppModal ? window.createAppModal($('lmSourceModal')) : null;
    let scrapeReturnFocus = null;
    const scrapeModal = {
        open(trigger = null) {
            scrapeReturnFocus = trigger || document.activeElement;
            $('lmScrapeModal').hidden = false;
            document.body.classList.add('lm-scrape-open');
            window.requestAnimationFrame(() => $('lmScrapeCloseBtn')?.focus());
        },
        close() {
            $('lmScrapeModal').hidden = true;
            document.body.classList.remove('lm-scrape-open');
            scrapeReturnFocus?.focus?.();
            scrapeReturnFocus = null;
        },
    };

    function openSource(source = null, trigger = null) {
        $('lmSourceDialogTitle').textContent = source ? '编辑媒体来源' : '新增媒体来源';
        $('lmSourceId').value = source?.id || '';
        $('lmSourceMode').value = source?.mode || 'move';
        $('lmSourceName').value = source?.name || '';
        $('lmQbPrefix').value = source?.qb_path_prefix || '';
        $('lmLocalRoot').value = source?.local_root || '';
        if ($('lmSmbUser')) $('lmSmbUser').value = source?.smb_user || '';
        if ($('lmSmbPass')) {
            $('lmSmbPass').value = '';
            $('lmSmbPass').placeholder = source?.has_smb_pass
                ? '已保存密码（保持为空则不修改）'
                : 'NAS 共享密码';
        }
        $('lmStableSeconds').value = source?.stable_seconds ?? 300;
        $('lmScanMinutes').value = source?.scan_interval_minutes ?? 10;
        $('lmSourceEnabled').checked = source?.enabled ?? true;
        $('lmScanEnabled').checked = source?.scan_enabled ?? false;
        $('lmSourceMediaType').value = source?.media_type || 'auto';
        targetRows(Object.fromEntries((source?.targets || []).map((target) => [target.category, target])));
        updateSmbAuthGuidance();
        if (sourceModal) sourceModal.open(trigger);
        else $('lmSourceModal').hidden = false;
        icons($('lmSourceModal'));
    }

    function closeSource() {
        if (sourceModal) sourceModal.close();
        else $('lmSourceModal').hidden = true;
    }

    async function saveSource(event) {
        event.preventDefault();
        const button = $('lmSaveSourceBtn');
        if (button.disabled) return;
        const id = $('lmSourceId').value;
        const targets = [...document.querySelectorAll('[data-target-row]')].map((row) => {
            const path = row.querySelector('[data-target-path]').value.trim();
            const provider = row.querySelector('[data-target-provider]').value;
            const select = row.querySelector('[data-target-library]');
            const selectedOption = select.selectedOptions[0];
            const libraryId = select.value || row.dataset.libraryId || '';
            const libraryName = selectedOption?.dataset.name || row.dataset.libraryName || '';
            return {
                category: row.dataset.targetRow, path, provider,
                library_id: libraryId, library_name: libraryName,
            };
        }).filter((target) => target.path);
        const payload = {
            name: $('lmSourceName').value.trim(),
            qb_profile: 'configured:qb',
            qb_path_prefix: $('lmQbPrefix').value.trim(),
            local_root: $('lmLocalRoot').value.trim(),
            smb_user: $('lmSmbUser')?.value.trim() || '',
            smb_pass: $('lmSmbPass')?.value.trim() || '',
            enabled: $('lmSourceEnabled').checked,
            stable_seconds: Number($('lmStableSeconds').value) || 0,
            scan_enabled: $('lmScanEnabled').checked,
            scan_interval_minutes: Number($('lmScanMinutes').value) || 10,
            media_type: $('lmSourceMediaType').value,
            mode: $('lmSourceMode').value || 'move',
            targets,
        };
        setBusy(button, true, '保存中');
        try {
            await api(`/api/local-media/sources${id ? `/${id}` : ''}`, {
                method: id ? 'PUT' : 'POST',
                body: JSON.stringify(payload),
            });
            closeSource();
            await loadAll();
        } catch (error) {
            appAlert({type: 'error', title: '保存失败', message: error.message});
        } finally {
            setBusy(button, false, '保存来源');
        }
    }

    function resetScrapeWorkspace() {
        searchRequestSerial += 1;
        invalidatePreview();
        inspection = null;
        $('lmCandidateCount').textContent = '0 项';
        $('lmCandidates').innerHTML = '<div class="lm-scrape-placeholder"><i data-lucide="scan-search"></i><strong>等待搜索</strong><span>检查完成后会在这里展示 TMDB 候选。</span></div>';
        $('lmScrapeDetail').innerHTML = '<div class="lm-scrape-placeholder"><i data-lucide="clapperboard"></i><strong>等待媒体识别</strong><span>选择候选或使用自动匹配后，将显示完整归档方案。</span></div>';
        $('lmScrapeStatus').textContent = '尚未选择';
        $('lmScrapeInspectionSummary').textContent = '等待检查';
        $('lmPlanSummary').textContent = '确认前不会写入媒体库';
        $('lmScrapeTarget').textContent = '目标路径将在识别后显示';
        icons($('lmScrapeModal'));
    }

    function applyInspection(result) {
        inspection = result;
        selectedCandidate = null;
        lastPreview = null;
        appliedPreviewContext = null;
        $('lmSearchQuery').value = inspection.selected_name || activeMediaItem?.name || '';
        $('lmScrapeInspectionSummary').textContent = `${inspection.video_count} 个视频 · ${inspection.file_count} 个扫描文件`;
        $('lmScrapeStatus').textContent = '等待匹配';
        $('lmCandidates').innerHTML = '<div class="lm-scrape-placeholder"><i data-lucide="scan-search"></i><strong>检查完成</strong><span>可以搜索 TMDB 候选，或直接使用自动匹配。</span></div>';
        $('lmScrapeDetail').innerHTML = '<div class="lm-scrape-placeholder"><i data-lucide="route"></i><strong>等待生成归档方案</strong><span>确认前不会移动、覆盖或清理任何文件。</span></div>';
        icons($('lmScrapeModal'));
    }

    async function openScrapeForItem(item, {automatic = false, trigger = null} = {}) {
        if (!item?.organize_ready) {
            return appAlert({type: 'warning', title: '尚未配置归档目标', message: '请先在媒体来源中为该目录配置至少一个媒体库目标。'});
        }
        activeMediaItem = item;
        resetScrapeWorkspace();
        const source = sources.find((entry) => Number(entry.id) === Number(item.source_id));
        $('lmMediaType').value = source?.media_type === 'tv' ? 'tv' : 'movie';
        $('lmScrapeTitle').textContent = automatic ? `自动匹配 · ${item.name}` : `搜索刮削 · ${item.name}`;
        $('lmScrapeItemPath').textContent = `${item.source_name} / ${item.path}`;
        if (scrapeModal) scrapeModal.open(trigger);
        else $('lmScrapeModal').hidden = false;
        icons($('lmScrapeModal'));
        const requestSerial = ++inspectionRequestSerial;
        setInspectionControlsBusy(true, trigger);
        $('lmScrapeStatus').textContent = '正在检查';
        try {
            const result = await api('/api/local-media/inspect', {
                method: 'POST',
                body: JSON.stringify({source_id: item.source_id, path: item.path}),
            });
            if (requestSerial !== inspectionRequestSerial) return;
            applyInspection(result);
            if (automatic) {
                const planned = await preview();
                if (!planned) await search();
            } else {
                await search();
            }
        } catch (error) {
            if (requestSerial !== inspectionRequestSerial) return;
            $('lmScrapeStatus').textContent = '检查失败';
            $('lmScrapeDetail').innerHTML = `<div class="lm-scrape-placeholder"><i data-lucide="circle-alert"></i><strong>无法检查此条目</strong><span>${esc(error.message)}</span></div>`;
            icons($('lmScrapeDetail'));
            appAlert({type: 'error', title: '检查失败', message: error.message});
        } finally {
            if (requestSerial === inspectionRequestSerial) setInspectionControlsBusy(false);
        }
    }

    function closeScrape() {
        inspectionRequestSerial += 1;
        searchRequestSerial += 1;
        invalidatePreview();
        scrapeModal.close();
        activeMediaItem = null;
        inspection = null;
    }

    $('lmScrapeModal').addEventListener('click', (event) => {
        if (event.target === $('lmScrapeModal')) closeScrape();
    });

    async function beginTaskReview(taskId, button) {
        const task = tasks.find((item) => String(item.id) === String(taskId));
        const requestSerial = ++inspectionRequestSerial;
        setInspectionControlsBusy(true, button);
        setBusy(button, true, '读取中');
        try {
            const result = await api(`/api/local-media/tasks/${taskId}/inspect`, {method: 'POST', body: '{}'});
            if (requestSerial !== inspectionRequestSerial) return;
            activeMediaItem = {
                source_id: result.source_id,
                source_name: task?.source_name || '本地媒体',
                name: result.selected_name || task?.content_name || '待确认任务',
                path: task?.content_name || result.selected_name || '',
                organize_ready: true,
            };
            resetScrapeWorkspace();
            $('lmScrapeTitle').textContent = `人工确认 · ${activeMediaItem.name}`;
            $('lmScrapeItemPath').textContent = `${activeMediaItem.source_name} / 待确认任务 #${taskId}`;
            const source = sources.find((entry) => Number(entry.id) === Number(result.source_id));
            $('lmMediaType').value = task?.media_type || (source?.media_type === 'tv' ? 'tv' : 'movie');
            if (scrapeModal) scrapeModal.open(button);
            else $('lmScrapeModal').hidden = false;
            applyInspection(result);
            await search();
        } catch (error) {
            if (requestSerial === inspectionRequestSerial) appAlert({type: 'error', title: '无法进入人工确认', message: error.message});
        } finally {
            if (requestSerial === inspectionRequestSerial) {
                setBusy(button, false, '人工确认');
                setInspectionControlsBusy(false);
            }
        }
    }

    async function search() {
        if (!inspection) return false;
        const query = $('lmSearchQuery').value.trim();
        if (!query) {
            appAlert({type: 'warning', title: '请输入搜索名称', message: '搜索名称不能为空。'});
            return false;
        }
        const requestSerial = ++searchRequestSerial;
        const inspectionId = inspection.inspection_id;
        setBusy($('lmSearchBtn'), true, '搜索中');
        $('lmScrapeStatus').textContent = '正在搜索';
        try {
            const data = await api('/api/local-media/search', {
                method: 'POST',
                body: JSON.stringify({query, media_type: $('lmMediaType').value}),
            });
            if (requestSerial !== searchRequestSerial || inspection?.inspection_id !== inspectionId) return false;
            const candidates = data.candidates || [];
            $('lmCandidates').innerHTML = candidates.length
                ? candidates.map((candidate, index) => `
                    <button type="button" class="lm-candidate" data-candidate="${index}">
                        <span><strong>${esc(candidate.title)}${candidate.year ? ` (${esc(candidate.year)})` : ''}</strong><span>TMDB ${esc(candidate.tmdb_id)} · ${(Number(candidate.score) || 0).toFixed(0)}% 匹配</span></span>
                        <i data-lucide="chevron-right"></i>
                    </button>`).join('')
                : '<div class="lm-scrape-placeholder"><i data-lucide="search-x"></i><strong>没有搜索到候选</strong><span>可以调整名称或媒体类型后重试。</span></div>';
            $('lmCandidates')._items = candidates;
            $('lmCandidateCount').textContent = `${candidates.length} 项`;
            $('lmScrapeStatus').textContent = candidates.length ? '请选择候选' : '暂无候选';
            icons($('lmCandidates'));
            return candidates.length > 0;
        } catch (error) {
            if (requestSerial === searchRequestSerial && inspection?.inspection_id === inspectionId) {
                $('lmScrapeStatus').textContent = '搜索失败';
                appAlert({type: 'error', title: '搜索失败', message: error.message});
            }
            return false;
        } finally {
            if (requestSerial === searchRequestSerial) setBusy($('lmSearchBtn'), false, '搜索');
        }
    }

    async function preview(candidate = null) {
        if (!inspection) return false;
        const requestSerial = ++previewRequestSerial;
        const context = Object.freeze({
            inspectionId: inspection.inspection_id,
            tmdbId: candidate?.tmdb_id || '',
            mediaType: candidate?.media_type || $('lmMediaType').value,
        });
        selectedCandidate = null;
        lastPreview = null;
        appliedPreviewContext = null;
        $('lmExecuteBtn').disabled = true;
        $('lmScrapeStatus').textContent = '正在生成计划';
        setPreviewControlsBusy(true);
        try {
            const previewResult = await api('/api/local-media/preview', {
                method: 'POST',
                body: JSON.stringify({
                    inspection_id: context.inspectionId,
                    tmdb_id: context.tmdbId,
                    media_type: context.mediaType,
                }),
            });
            if (requestSerial !== previewRequestSerial || inspection?.inspection_id !== context.inspectionId) return false;
            if (previewResult.status !== 'planned') {
                $('lmScrapeStatus').textContent = '需要人工确认';
                $('lmScrapeDetail').innerHTML = `<div class="lm-scrape-placeholder"><i data-lucide="circle-alert"></i><strong>自动识别需要确认</strong><span>${esc(previewResult.reason || '请选择一个 TMDB 候选。')}</span></div>`;
                icons($('lmScrapeDetail'));
                return false;
            }
            selectedCandidate = candidate;
            lastPreview = previewResult;
            appliedPreviewContext = context;
            const plans = previewResult.plans || [];
            const cleanup = previewResult.cleanup || [];
            const replacements = plans.filter((item) => item.action === 'replace').length;
            const skipped = plans.filter((item) => item.action === 'skip').length;
            const matched = previewResult.matches?.[0];
            $('lmPlanSummary').textContent = `${plans.length} 项整理 · ${replacements} 项安全覆盖 · ${cleanup.length} 项清理`;
            $('lmScrapeTarget').textContent = plans[0]?.target_path ? `将整理至：${plans[0].target_path}` : '目标路径已校验';
            $('lmScrapeStatus').textContent = '计划已就绪';
            $('lmScrapeDetail').innerHTML = `
                <div class="lm-scrape-summary">
                    <div><strong>${esc(matched?.title || activeMediaItem?.name || '已识别')}</strong><span>${esc(matched?.media_type || context.mediaType)} · TMDB ${esc(matched?.tmdb_id || context.tmdbId || '自动')}</span></div>
                    <div><strong>${plans.length}</strong><span>媒体与伴随文件</span></div>
                    <div><strong>${replacements}</strong><span>同名目标安全覆盖${skipped ? ` · ${skipped} 项保留` : ''}</span></div>
                </div>
                <div class="lm-plan-list">${plans.map((item) => {
                    const actionLabel = item.action === 'replace' ? '覆盖' : item.action === 'skip' ? '保留' : '移动';
                    const actionClass = item.action === 'replace' ? 'is-replace' : '';
                    return `<div class="lm-plan-row ${actionClass}"><b>${actionLabel}</b><span title="${esc(item.target_path)}">${esc(item.source_path)} → ${esc(item.target_name)}${item.note ? ` · ${esc(item.note)}` : ''}</span></div>`;
                }).join('')}${cleanup.map((item) => `<div class="lm-plan-row is-delete"><b>清理</b><span>${esc(item.name)} · ${esc(item.reason)}</span></div>`).join('')}</div>`;
            $('lmExecuteBtn').disabled = false;
            document.querySelectorAll('[data-candidate]').forEach((button) => button.classList.remove('is-selected'));
            if (candidate) {
                const candidates = $('lmCandidates')._items || [];
                const index = candidates.indexOf(candidate);
                document.querySelector(`[data-candidate="${index}"]`)?.classList.add('is-selected');
            }
            icons($('lmScrapeDetail'));
            return true;
        } catch (error) {
            if (requestSerial === previewRequestSerial && inspection?.inspection_id === context.inspectionId) {
                $('lmScrapeStatus').textContent = '预览失败';
                appAlert({type: 'error', title: '预览失败', message: error.message});
            }
            return false;
        } finally {
            if (requestSerial === previewRequestSerial) {
                setPreviewControlsBusy(false);
                $('lmExecuteBtn').disabled = !appliedPreviewContext;
            }
        }
    }

    async function execute() {
        if (!inspection || !lastPreview || !appliedPreviewContext) return;
        const confirmedContext = appliedPreviewContext;
        const confirmedPreview = lastPreview;
        if (confirmedContext.inspectionId !== inspection.inspection_id) return;
        const replacements = (confirmedPreview.plans || []).filter((item) => item.action === 'replace').length;
        const ok = await appConfirm({
            title: '执行本地媒体整理',
            message: `将整理 ${confirmedPreview.plans?.length || 0} 个媒体或伴随文件${replacements ? `，其中 ${replacements} 个同名目标会安全覆盖` : ''}，并清理 ${confirmedPreview.cleanup?.length || 0} 个明确垃圾文件。`,
            confirmText: '开始整理',
        });
        if (!ok) return;
        if (appliedPreviewContext !== confirmedContext || lastPreview !== confirmedPreview) {
            return appAlert({type: 'warning', title: '计划已变化', message: '候选或执行计划已更新，请重新确认。'});
        }
        setBusy($('lmExecuteBtn'), true, '整理中');
        try {
            const result = await api('/api/local-media/execute', {
                method: 'POST',
                body: JSON.stringify({
                    inspection_id: confirmedContext.inspectionId,
                    tmdb_id: confirmedContext.tmdbId,
                    media_type: confirmedContext.mediaType,
                    rules_snapshot: confirmedPreview.rules_snapshot || '',
                }),
            });
            appAlert({
                type: result.status === 'completed' ? 'success' : 'warning',
                title: result.status === 'completed' ? '整理完成' : '任务已进入待确认',
                message: result.status === 'completed'
                    ? `已移动 ${result.moved?.length || 0} 项，清理 ${result.deleted_junk?.length || 0} 项。`
                    : (result.preview?.reason || '请在待确认任务中继续处理。'),
            });
            if (result.status === 'completed') closeScrape();
            await loadAll(false);
        } catch (error) {
            appAlert({type: 'error', title: '整理失败', message: error.message});
        } finally {
            setBusy($('lmExecuteBtn'), false, '确认并开始整理');
            $('lmExecuteBtn').disabled = !appliedPreviewContext;
        }
    }

    function closeItemContextMenu() {
        $('lmItemContextMenu').hidden = true;
        document.querySelector('.lm-media-row.is-context')?.classList.remove('is-context');
        activeContextItem = null;
        activeContextTrigger = null;
    }

    function openItemContextMenu(item, x, y, row) {
        closeItemContextMenu();
        activeContextItem = item;
        activeContextTrigger = row?.querySelector('[data-open-item-menu]') || row;
        row?.classList.add('is-context');
        const menu = $('lmItemContextMenu');
        menu.querySelectorAll('[data-item-action="search"], [data-item-action="auto"]').forEach((button) => {
            button.disabled = !item.organize_ready;
            button.title = item.organize_ready ? '' : '请先为来源配置归档目标';
        });
        menu.hidden = false;
        const rect = menu.getBoundingClientRect();
        const left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
        const top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
        menu.querySelector('button:not(:disabled)')?.focus({preventScroll: true});
    }

    async function deleteMediaItem(item) {
        const ok = await appConfirm({
            title: '删除本地媒体条目',
            message: `“${item.name}”将移入该来源内的 MediaFlux 回收区，不会立即永久删除。正在下载或刚发生变化的条目会被安全拒绝。`,
            confirmText: '移入回收区',
            danger: true,
        });
        if (!ok) return;
        try {
            await api('/api/local-media/items/delete', {
                method: 'POST',
                body: JSON.stringify({source_id: item.source_id, path: item.path, identity: item.identity}),
            });
            appAlert({type: 'success', title: '已移入回收区', message: `${item.name} 已从本地媒体条目中移除。`});
            await loadAll(false);
        } catch (error) {
            appAlert({type: 'error', title: '删除失败', message: error.message});
        }
    }

    async function refreshMediaItems() {
        const button = $('lmRefreshItemsBtn');
        setBusy(button, true, '刷新中');
        try {
            await loadAll(false);
            appAlert({type: 'success', title: '条目已刷新', message: `当前共 ${mediaItems.length} 个本地媒体条目。`});
        } finally {
            setBusy(button, false, '刷新条目');
        }
    }

    let currentTab = 'tasks';

    function switchTab(tabName, updateUrl = true) {
        currentTab = ['tasks', 'sources', 'review', 'manual'].includes(tabName) ? tabName : 'tasks';
        document.querySelectorAll('.lm-tab-btn').forEach((btn) => {
            const active = btn.dataset.tabTarget === currentTab;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', String(active));
            btn.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('.lm-tab-panel').forEach((panel) => {
            const active = panel.dataset.tabPanel === currentTab;
            panel.classList.toggle('active', active);
            panel.hidden = !active;
        });
        document.querySelectorAll('[data-tab-action]').forEach((action) => {
            action.hidden = action.dataset.tabAction !== currentTab;
        });
        if (updateUrl) {
            history.replaceState(null, '', `#${currentTab}`);
        }
        icons();
        if (currentTab === 'manual' && hasLoadedLocalMedia) loadAll(false);
    }

    $('lmAddSourceBtn').addEventListener('click', (event) => openSource(null, event.currentTarget));
    $('lmAddSourceMobileBtn')?.addEventListener('click', (event) => openSource(null, event.currentTarget));
    document.querySelectorAll('.lm-tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tabTarget));
    });
    $('lmSourceForm').addEventListener('submit', saveSource);
    $('lmRefreshBtn').addEventListener('click', () => loadAll(true));
    $('lmTaskFilter').addEventListener('change', () => { taskDisplayLimit = 60; renderTasks(true, true); });
    $('lmLoadMoreTasksBtn').addEventListener('click', () => { taskDisplayLimit += 60; renderTasks(true, true); });
    $('lmPickLocalRootBtn').addEventListener('click', () => openLocalDirectoryPicker($('lmLocalRoot')));
    $('lmLocalRoot')?.addEventListener('input', updateSmbAuthGuidance);
    $('lmLocalRoot')?.addEventListener('change', updateSmbAuthGuidance);
    $('lmSmbUser')?.addEventListener('input', updateSmbAuthGuidance);
    $('lmSmbPass')?.addEventListener('input', updateSmbAuthGuidance);
    $('lmItemFilter').addEventListener('input', () => renderMediaItems(true, false));
    $('lmItemSourceFilter').addEventListener('change', () => renderMediaItems(true, false));
    $('lmRefreshItemsBtn').addEventListener('click', refreshMediaItems);
    $('lmSearchBtn').addEventListener('click', search);
    $('lmAutoPreviewBtn').addEventListener('click', () => preview());
    $('lmSearchQuery').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); search(); }
    });
    $('lmScrapeCloseBtn').addEventListener('click', closeScrape);
    $('lmScrapeCancelBtn').addEventListener('click', closeScrape);
    $('lmExecuteBtn').addEventListener('click', execute);

    $('lmTargetRows').addEventListener('change', async (event) => {
        const row = event.target.closest('[data-target-row]');
        if (!row) return;
        if (event.target.matches('[data-target-provider]')) {
            row.dataset.libraryId = '';
            row.dataset.libraryName = '';
            await populateLibrarySelect(row);
            return;
        }
        if (event.target.matches('[data-target-library]')) {
            const option = event.target.selectedOptions[0];
            row.dataset.libraryId = event.target.value;
            row.dataset.libraryName = option?.dataset.name || '';
        }
    });

    document.addEventListener('click', async (event) => {
        const itemMenuButton = event.target.closest('[data-open-item-menu]');
        if (itemMenuButton) {
            const row = itemMenuButton.closest('[data-media-item]');
            const item = mediaItemMap.get(row?.dataset.mediaItem || '');
            if (item) {
                const rect = itemMenuButton.getBoundingClientRect();
                openItemContextMenu(item, rect.right - 226, rect.bottom + 5, row);
            }
            return;
        }
        if (!event.target.closest('#lmItemContextMenu')) closeItemContextMenu();

        const edit = event.target.closest('[data-edit-source]');
        if (edit) {
            openSource(sources.find((source) => source.id === Number(edit.dataset.editSource)), edit);
            return;
        }

        const remove = event.target.closest('[data-delete-source]');
        if (remove) {
            if (remove.disabled) return;
            const ok = await appConfirm({
                title: '删除媒体来源',
                message: '仅删除来源配置，不删除磁盘中的任何文件。',
                confirmText: '删除来源',
                danger: true,
            });
            if (!ok) return;
            setBusy(remove, true, '删除中');
            try {
                await api(`/api/local-media/sources/${remove.dataset.deleteSource}`, {method: 'DELETE'});
                await loadAll();
            } catch (error) {
                appAlert({type: 'error', title: '删除失败', message: error.message});
            } finally {
                setBusy(remove, false, '删除');
            }
            return;
        }

        const pickTarget = event.target.closest('[data-pick-target]');
        if (pickTarget) {
            const row = pickTarget.closest('[data-target-row]');
            openLocalDirectoryPicker(row.querySelector('[data-target-path]'));
            return;
        }

        const candidateButton = event.target.closest('[data-candidate]');
        if (candidateButton) {
            if (candidateButton.disabled) return;
            document.querySelectorAll('[data-candidate]').forEach((button) => button.classList.remove('is-selected'));
            candidateButton.classList.add('is-selected');
            await preview($('lmCandidates')._items[Number(candidateButton.dataset.candidate)]);
            return;
        }

        const reviewTask = event.target.closest('[data-review-task]');
        if (reviewTask) {
            if (reviewTask.disabled) return;
            await beginTaskReview(reviewTask.dataset.reviewTask, reviewTask);
            return;
        }

        const retry = event.target.closest('[data-retry]');
        if (retry) {
            if (retry.disabled) return;
            setBusy(retry, true, '入队中');
            try {
                await api(`/api/local-media/tasks/${retry.dataset.retry}/retry`, {method: 'POST', body: '{}'});
                await loadAll();
            } catch (error) {
                appAlert({type: 'error', title: '重试失败', message: error.message});
            } finally {
                setBusy(retry, false, '重试');
            }
        }
    });

    $('lmMediaItems').addEventListener('contextmenu', (event) => {
        const row = event.target.closest('[data-media-item]');
        const item = mediaItemMap.get(row?.dataset.mediaItem || '');
        if (!item) return;
        event.preventDefault();
        openItemContextMenu(item, event.clientX, event.clientY, row);
    });

    $('lmItemContextMenu').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-item-action]');
        if (!button || button.disabled || !activeContextItem) return;
        const item = activeContextItem;
        const trigger = activeContextTrigger;
        const action = button.dataset.itemAction;
        closeItemContextMenu();
        if (action === 'search') await openScrapeForItem(item, {trigger});
        else if (action === 'auto') await openScrapeForItem(item, {automatic: true, trigger});
        else if (action === 'delete') await deleteMediaItem(item);
    });

    window.addEventListener('resize', closeItemContextMenu);
    window.addEventListener('scroll', closeItemContextMenu, true);
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        if (!$('lmItemContextMenu').hidden) closeItemContextMenu();
        else if (!$('lmScrapeModal').hidden) closeScrape();
    });

    document.addEventListener('visibilitychange', async () => {
        window.clearTimeout(pollTimer);
        pollTimer = null;
        if (!document.hidden) {
            await loadAll(false);
            schedulePoll();
        }
    });

    function resolveTargetTab() {
        const hashTab = (window.location.hash || '').replace('#', '').trim();
        const searchTab = new URLSearchParams(window.location.search).get('tab') || new URLSearchParams(window.location.search).get('view');
        const target = hashTab || searchTab;
        return ['tasks', 'sources', 'review', 'manual'].includes(target) ? target : 'tasks';
    }

    window.addEventListener('hashchange', () => {
        const target = resolveTargetTab();
        if (target !== currentTab) {
            switchTab(target, false);
        }
    });

    switchTab(resolveTargetTab(), false);

    loadAll().finally(schedulePoll);
    icons();
})();
