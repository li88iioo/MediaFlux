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
    let mediaBrowse = {
        sourceId: 0, sourceName: '', rootPath: '', currentPath: '', parentPath: '', breadcrumbs: [],
    };
    let mediaBrowseRequestSerial = 0;
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
    const selectedTaskIds = new Set();
    let taskDetailRequestSerial = 0;
    const INITIAL_LOADING_DELAY_MS = 400;
    const INITIAL_LOADING_MIN_MS = 320;
    let initialLoadingTimer = null;
    let initialLoadingShownAt = 0;
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

    function armInitialLoading() {
        window.clearTimeout(initialLoadingTimer);
        initialLoadingTimer = null;
        initialLoadingShownAt = 0;
        const nodes = [...document.querySelectorAll('.lm-tab-panel.active .lm-initial-loading')];
        if (!nodes.length) return;
        initialLoadingTimer = window.setTimeout(() => {
            initialLoadingTimer = null;
            const connected = nodes.filter((node) => node.isConnected && node.closest('.lm-tab-panel.active'));
            if (!connected.length) return;
            initialLoadingShownAt = performance.now();
            connected.forEach((node) => {
                node.classList.add('is-visible');
                node.setAttribute('aria-hidden', 'false');
            });
        }, INITIAL_LOADING_DELAY_MS);
    }

    async function settleInitialLoading() {
        window.clearTimeout(initialLoadingTimer);
        initialLoadingTimer = null;
        if (!initialLoadingShownAt) return;
        const remaining = INITIAL_LOADING_MIN_MS - (performance.now() - initialLoadingShownAt);
        if (remaining > 0) {
            await new Promise((resolve) => window.setTimeout(resolve, remaining));
        }
        initialLoadingShownAt = 0;
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
        $('lmTaskList').innerHTML = `<tr class="is-empty-row"><td colspan="7" class="table-empty"><div class="lm-table-empty-wrap"><i data-lucide="alert-circle"></i><span>整理日志读取失败：${text}</span></div></td></tr>`;
        $('lmMediaItems').innerHTML = `<div class="lm-empty-state is-error"><div class="lm-empty-icon is-error"><i data-lucide="alert-circle"></i></div><strong>本地条目读取失败</strong><p>${text}</p></div>`;
        icons($('lmSourceGrid'));
        icons($('lmReviewList'));
        icons($('lmTaskList'));
        icons($('lmMediaItems'));
    }

    function renderInitialMediaItemsFailure(message) {
        const text = esc(message || '无法读取本地媒体条目，请稍后重试。');
        $('lmMediaItems').innerHTML = `<div class="lm-empty-state is-error"><div class="lm-empty-icon is-error"><i data-lucide="alert-circle"></i></div><strong>本地条目读取失败</strong><p>${text}</p></div>`;
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
        if (!force && signature === sourceSignature) {
            syncMediaSourceFilter();
            return;
        }
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
            syncMediaSourceFilter();
        }
        icons(grid);
        releaseHeight();
    }

    function taskDisplayName(task) {
        return task?.display_name || task?.content_name || '未命名';
    }

    function taskRow(task) {
        const state = states[task.status] || [task.status, ''];
        const workflowAction = task.status === 'requires_manual'
            ? `<button class="jump-btn" type="button" data-review-task="${task.id}"><i data-lucide="search-check"></i>人工确认</button>`
            : task.status === 'failed'
                ? `<button class="jump-btn" type="button" data-retry="${task.id}"><i data-lucide="rotate-ccw"></i>重试</button>`
                : '';
        const selected = selectedTaskIds.has(Number(task.id));
        const selectTitle = task.clearable ? '选择此条整理日志' : '运行中的日志暂不可清除';
        return `
            <tr data-task-row="${task.id}" class="${selected ? 'is-selected' : ''}">
                <td class="lm-task-select-cell" data-label="选择"><input class="lm-task-row-select" type="checkbox" data-task-select="${task.id}" ${selected ? 'checked' : ''} ${task.clearable ? '' : 'disabled'} aria-label="${esc(selectTitle)}" title="${esc(selectTitle)}"></td>
                <td class="lm-task-title" data-label="日志"><strong>${esc(taskDisplayName(task))}</strong><span>${esc(task.error || task.warning || '')}</span></td>
                <td data-label="来源">${esc(task.source_name || '—')}</td>
                <td data-label="触发">${esc(triggerLabels[task.trigger] || task.trigger || '—')}</td>
                <td data-label="状态"><span class="status-pill ${state[1]}">${esc(state[0])}</span></td>
                <td data-label="更新时间">${esc(task.updated_at || task.created_at || '—')}</td>
                <td data-label="操作"><div class="lm-task-actions"><button class="jump-btn" type="button" data-task-detail="${task.id}"><i data-lucide="file-search"></i>详情</button>${workflowAction}</div></td>
            </tr>`;
    }

    function filteredTasks() {
        const filter = $('lmTaskFilter').value;
        const query = $('lmTaskSearch').value.trim().toLocaleLowerCase();
        return tasks.filter((task) => {
            if (filter && task.status !== filter) return false;
            if (!query) return true;
            const stateLabel = states[task.status]?.[0] || task.status || '';
            const haystack = [
                task.display_name, task.content_name, task.source_name, task.trigger, triggerLabels[task.trigger],
                task.status, stateLabel, task.error, task.warning, task.tmdb_id,
            ].filter(Boolean).join('\n').toLocaleLowerCase();
            return haystack.includes(query);
        });
    }

    function updateTaskSelectionState(filtered = filteredTasks()) {
        const taskById = new Map(tasks.map((task) => [Number(task.id), task]));
        [...selectedTaskIds].forEach((taskId) => {
            if (!taskById.get(taskId)?.clearable) selectedTaskIds.delete(taskId);
        });
        const selectable = filtered.filter((task) => task.clearable);
        const selectedInFilter = selectable.filter((task) => selectedTaskIds.has(Number(task.id))).length;
        const selectAll = $('lmTaskSelectAll');
        $('lmTaskSelectAllLabel').hidden = false;
        selectAll.disabled = selectable.length === 0;
        selectAll.checked = selectable.length > 0 && selectedInFilter === selectable.length;
        selectAll.indeterminate = selectedInFilter > 0 && selectedInFilter < selectable.length;
        $('lmTaskSelectionCount').textContent = String(selectedTaskIds.size);
        $('lmClearTasksBtn').disabled = selectedTaskIds.size === 0;
    }

    function renderTasks(force = false, animate = false) {
        const filter = $('lmTaskFilter').value;
        const query = $('lmTaskSearch').value.trim().toLocaleLowerCase();
        const signature = JSON.stringify(tasks.map((task) => [
            task.id, task.display_name, task.content_name, task.source_name, task.trigger, task.status,
            task.updated_at, task.created_at, task.error, task.warning, task.tmdb_id, task.clearable,
        ]));
        const viewSignature = `${filter}\u0000${query}`;
        if (!force && signature === taskSignature && viewSignature === renderedFilter) return;
        taskSignature = signature;
        renderedFilter = viewSignature;

        const filtered = filteredTasks();
        const visible = filtered.slice(0, taskDisplayLimit);
        const taskList = $('lmTaskList');
        const releaseTaskHeight = animate ? lockElementHeight(taskList.closest('.lm-task-table-wrap')) : () => {};
        taskList.innerHTML = visible.length
            ? visible.map(taskRow).join('')
            : `<tr class="is-empty-row"><td colspan="7" class="table-empty"><div class="lm-table-empty-wrap"><i data-lucide="inbox"></i><span>${filter || query ? '暂无符合筛选条件的整理日志' : '暂无整理日志'}</span></div></td></tr>`;

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
                    <div><strong>${esc(taskDisplayName(task))}</strong><span>${esc(task.error || '需要重新指定 TMDB 信息')}</span></div>
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
        updateTaskSelectionState(filtered);
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

    function taskDetailField(label, value, {code = false} = {}) {
        const safeValue = value == null || value === '' ? '—' : String(value);
        return `<div class="lm-task-detail-field"><span>${esc(label)}</span>${code ? `<code>${esc(safeValue)}</code>` : `<strong>${esc(safeValue)}</strong>`}</div>`;
    }

    function renderTaskDetail(data) {
        const task = data.task || {};
        const state = states[task.status] || [task.status || '未知', ''];
        const position = task.media_type === 'tv'
            ? `第 ${task.season ?? '—'} 季 / 第 ${task.episode ?? '—'} 集`
            : '—';
        const items = Array.isArray(data.items) ? data.items : [];
        const steps = Array.isArray(data.steps) ? data.steps : [];
        $('lmTaskDetailTitle').textContent = taskDisplayName(task) || `整理日志 #${task.id || ''}`;
        $('lmTaskDetailSubtitle').textContent = `${task.source_name || '已删除来源'} · 日志 #${task.id || '—'}`;
        $('lmTaskDetailBody').innerHTML = `
            <section class="lm-task-detail-overview">
                <div class="lm-task-detail-status"><span class="status-pill ${state[1]}">${esc(state[0])}</span><span>${esc(triggerLabels[task.trigger] || task.trigger || '未知触发')}</span></div>
                <div class="lm-task-detail-grid">
                    ${taskDetailField('媒体标题', [task.title, task.year].filter(Boolean).join(' · '))}
                    ${taskDetailField('TMDB', task.tmdb_id)}
                    ${taskDetailField('媒体类型', task.media_type)}
                    ${taskDetailField('季 / 集', position)}
                    ${taskDetailField('尝试次数', task.attempts ?? 0)}
                    ${taskDetailField('创建时间', formatItemDate(task.created_at))}
                    ${taskDetailField('更新时间', formatItemDate(task.updated_at))}
                    ${taskDetailField('完成时间', formatItemDate(task.completed_at))}
                    ${taskDetailField('来源路径', task.content_path, {code: true})}
                </div>
                ${task.error ? `<div class="lm-task-detail-notice is-error"><i data-lucide="circle-x"></i><div><strong>错误</strong><span>${esc(task.error)}</span></div></div>` : ''}
                ${task.warning ? `<div class="lm-task-detail-notice"><i data-lucide="triangle-alert"></i><div><strong>提示</strong><span>${esc(task.warning)}</span></div></div>` : ''}
            </section>
            <section class="lm-task-detail-section">
                <div class="lm-task-detail-section-head"><div><strong>文件成员</strong><span>${items.length} 项</span></div><small>展示来源、目标与执行结果</small></div>
                <div class="lm-task-detail-list">${items.length ? items.map((item) => `
                    <article class="lm-task-detail-item">
                        <div><strong>${esc(item.role || item.action || '文件')}</strong><span>${esc(formatItemSize(item.size))} · ${esc(item.status || '—')}</span></div>
                        <code>${esc(item.source_path || '—')}</code>
                        ${item.target_path ? `<code class="is-target">→ ${esc(item.target_path)}</code>` : ''}
                        ${item.error ? `<p>${esc(item.error)}</p>` : ''}
                    </article>`).join('') : '<div class="lm-task-detail-empty">暂无文件成员记录</div>'}</div>
            </section>
            <section class="lm-task-detail-section">
                <div class="lm-task-detail-section-head"><div><strong>执行步骤</strong><span>${steps.length} 步</span></div><small>按实际执行顺序记录</small></div>
                <div class="lm-task-step-list">${steps.length ? steps.map((step, ordinal) => `
                    <article class="lm-task-step ${step.error ? 'is-failed' : step.status === 'completed' ? 'is-completed' : ''}">
                        <span class="lm-task-step-index">${ordinal + 1}</span>
                        <div><strong>${esc(step.action || '操作')}</strong><span>${esc(step.status || '—')} · ${esc(formatItemDate(step.finished_at || step.started_at))}</span>${step.error ? `<p>${esc(step.error)}</p>` : ''}</div>
                    </article>`).join('') : '<div class="lm-task-detail-empty">暂无执行步骤记录</div>'}</div>
            </section>
            ${task.rules_snapshot ? `<details class="lm-task-rules"><summary>查看规则快照</summary><pre>${esc(task.rules_snapshot)}</pre></details>` : ''}`;
        icons($('lmTaskDetailBody'));
    }

    function itemToken(item) {
        return encodeURIComponent(`${item.source_id}\u0000${item.path}`);
    }

    function mediaItemsUrl(sourceId = mediaBrowse.sourceId, path = mediaBrowse.currentPath) {
        if (!sourceId) return '/api/local-media/items';
        const params = new URLSearchParams({source_id: String(sourceId)});
        if (path) params.set('path', path);
        return `/api/local-media/items?${params.toString()}`;
    }

    function applyMediaItemData(data) {
        mediaItems = data?.items || [];
        mediaItemSources = data?.sources || [];
        const browse = data?.browse;
        mediaBrowse = browse ? {
            sourceId: Number(browse.source_id || 0),
            sourceName: browse.source_name || '',
            rootPath: browse.root_path || '',
            currentPath: browse.current_path || '',
            parentPath: browse.parent_path || '',
            breadcrumbs: Array.isArray(browse.breadcrumbs) ? browse.breadcrumbs : [],
        } : {
            sourceId: 0, sourceName: '', rootPath: '', currentPath: '', parentPath: '', breadcrumbs: [],
        };
        syncMediaSourceFilter();
        renderMediaBreadcrumb();
    }

    function syncMediaSourceFilter() {
        const select = $('lmItemSourceFilter');
        if (!select) return;
        select.disabled = Boolean(mediaBrowse.sourceId);
        if (mediaBrowse.sourceId) select.value = String(mediaBrowse.sourceId);
    }

    function renderMediaBreadcrumb() {
        const breadcrumb = $('lmMediaBreadcrumb');
        const home = $('lmMediaBrowseHome');
        const up = $('lmMediaBrowseUp');
        if (!breadcrumb || !home || !up) return;
        const browsing = Boolean(mediaBrowse.sourceId);
        home.disabled = !browsing;
        up.disabled = !mediaBrowse.parentPath;
        breadcrumb.innerHTML = browsing && mediaBrowse.breadcrumbs.length
            ? mediaBrowse.breadcrumbs.map((item, index) => {
                const isCurrent = index === mediaBrowse.breadcrumbs.length - 1;
                return `${index ? '<i data-lucide="chevron-right"></i>' : ''}${isCurrent
                    ? `<span aria-current="page" title="${esc(item.path)}">${esc(item.name)}</span>`
                    : `<button type="button" data-media-breadcrumb="${index}" title="${esc(item.path)}">${esc(item.name)}</button>`}`;
            }).join('')
            : '<span aria-current="page">所有已配置来源</span>';
        icons(breadcrumb);
    }

    async function loadMediaDirectory(sourceId = 0, path = '', {resetFilter = true} = {}) {
        const requestSerial = ++mediaBrowseRequestSerial;
        const list = $('lmMediaItems');
        const releaseHeight = lockElementHeight(list);
        list.classList.add('is-navigating');
        list.setAttribute('aria-busy', 'true');
        try {
            const data = await api(mediaItemsUrl(sourceId, path));
            if (requestSerial !== mediaBrowseRequestSerial) return false;
            if (resetFilter) {
                $('lmItemFilter').value = '';
                if (!sourceId) $('lmItemSourceFilter').value = '';
            }
            applyMediaItemData(data);
            renderMediaItems(true, false);
            return true;
        } catch (error) {
            if (requestSerial === mediaBrowseRequestSerial) {
                appAlert({type: 'error', title: '目录读取失败', message: error.message});
            }
            return false;
        } finally {
            if (requestSerial === mediaBrowseRequestSerial) {
                list.classList.remove('is-navigating');
                list.removeAttribute('aria-busy');
                releaseHeight();
            }
        }
    }

    function renderMediaItems(force = false, animate = false) {
        const query = ($('lmItemFilter')?.value || '').trim().toLocaleLowerCase();
        const sourceFilter = $('lmItemSourceFilter')?.value || '';
        const signature = JSON.stringify(mediaItems.map((item) => [
            item.source_id, item.name, item.path, item.kind, item.size, item.modified_at,
            item.organize_ready, item.deletable, item.relative_path, item.identity,
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
                const note = item.kind === 'directory'
                    ? `点击进入 · ${item.path}`
                    : item.organize_ready ? item.path : '未配置可写归档目标';
                return `
                    <article class="lm-media-row ${item.organize_ready ? '' : 'is-unready'} ${item.kind === 'directory' ? 'is-directory' : ''}" data-media-item="${esc(token)}" ${item.kind === 'directory' ? 'data-open-directory' : ''} tabindex="0">
                        <div class="lm-media-name">
                            <span class="lm-media-icon ${item.kind === 'video' ? 'is-video' : ''}"><i data-lucide="${item.kind === 'video' ? 'file-video-2' : 'folder'}"></i></span>
                            <span><strong title="${esc(item.name)}">${esc(item.name)}</strong><small title="${esc(note)}">${esc(note)}</small></span>
                        </div>
                        <span class="lm-media-cell" title="${esc(item.source_name)}">${esc(item.source_name)}</span>
                        <span class="lm-media-cell"><span class="lm-media-kind">${kindLabel}</span></span>
                        <span class="lm-media-cell">${esc(formatItemDate(item.modified_at))}</span>
                        <span class="lm-media-cell">${item.kind === 'video' ? esc(formatItemSize(item.size)) : '—'}</span>
                        <span class="lm-media-row-actions">${item.kind === 'directory' ? '<i class="lm-media-open-indicator" data-lucide="chevron-right"></i>' : ''}<button class="jump-btn lm-media-menu-btn" type="button" data-open-item-menu aria-label="打开 ${esc(item.name)} 操作菜单" title="更多操作"><i data-lucide="ellipsis"></i></button></span>
                    </article>`;
            }).join('')
            : `<div class="lm-empty-state">
                <div class="lm-empty-icon"><i data-lucide="${sources.length ? 'folder-open' : 'folder-plus'}"></i></div>
                <strong>${query ? '没有匹配的本地媒体条目' : mediaBrowse.sourceId ? '当前目录暂无媒体条目' : sources.length ? '暂无本地媒体条目' : '暂无媒体来源配置'}</strong>
                <p>${query ? '调整筛选关键词后重试。' : mediaBrowse.sourceId ? '可以返回上一级，或刷新后再次检查。' : sources.length ? '新下载内容会在刷新后显示。' : '先在「媒体来源」中配置本地目录和归档目标。'}</p>
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
                    if (initialLoadingTimer === null && !initialLoadingShownAt
                        && document.querySelector('.lm-tab-panel.active .lm-initial-loading')) {
                        armInitialLoading();
                    }
                    if (currentManual && !manualIndicatorVisible) {
                        manualIndicatorVisible = true;
                        setBusy($('lmRefreshBtn'), true, '刷新中');
                        document.querySelector('.lm-page')?.setAttribute('aria-busy', 'true');
                    }
                    try {
                        const shouldLoadItems = currentTab === 'manual' || currentManual;
                        const itemRequestSerial = mediaBrowseRequestSerial;
                        const [sourceData, taskData, serverData, itemData] = await Promise.all([
                            api('/api/local-media/sources'),
                            api('/api/local-media/tasks'),
                            api('/api/local-media/media-servers'),
                            shouldLoadItems ? api(mediaItemsUrl()) : Promise.resolve(null),
                        ]);
                        mediaServers = serverData.servers || [];
                        sources = sourceData.sources || [];
                        tasks = taskData.tasks || [];
                        const canApplyItems = itemData && itemRequestSerial === mediaBrowseRequestSerial;
                        if (canApplyItems) applyMediaItemData(itemData);
                        const firstLoad = !hasLoadedLocalMedia;
                        await settleInitialLoading();
                        const animate = firstLoad || currentManual;
                        renderSources(false, animate);
                        renderTasks(false, animate);
                        if (canApplyItems) renderMediaItems(false, animate);
                        hasLoadedLocalMedia = true;
                        setRefreshFailure();
                        if (currentManual && window.appAlert) {
                            appAlert({type: 'success', title: '已刷新', message: '媒体来源、整理日志和本地条目已更新。'});
                        }
                    } catch (error) {
                        await settleInitialLoading();
                        if (!hasLoadedLocalMedia) {
                            renderInitialLoadFailure(error.message);
                        } else if (currentTab === 'manual'
                            && document.querySelector('#lmMediaItems .lm-initial-loading')) {
                            renderInitialMediaItemsFailure(error.message);
                        }
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
    const taskDetailModal = window.createAppModal ? window.createAppModal($('lmTaskDetailModal')) : null;
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

    const positionControls = window.MediaScrapePosition.create({
        root: $('lmScrapeModal'),
        isSingleFile: () => inspection?.selected_kind === 'file' || Boolean(inspection?.single_video),
        elements: {
            fields: $('lmScrapeEpisodeFields'), seasonField: $('lmScrapeSeasonField'),
            episodeField: $('lmScrapeEpisodeField'), season: $('lmScrapeSeason'),
            episode: $('lmScrapeEpisode'),
        },
    });

    async function openTaskDetail(taskId, trigger = null) {
        const requestSerial = ++taskDetailRequestSerial;
        const task = tasks.find((item) => String(item.id) === String(taskId));
        $('lmTaskDetailTitle').textContent = taskDisplayName(task) || `整理日志 #${taskId}`;
        $('lmTaskDetailSubtitle').textContent = `${task?.source_name || '本地媒体'} · 正在读取完整记录`;
        $('lmTaskDetailBody').innerHTML = '<div class="lm-task-detail-loading"><i data-lucide="loader-circle"></i><span>正在读取日志详情…</span></div>';
        if (taskDetailModal) taskDetailModal.open(trigger);
        else $('lmTaskDetailModal').hidden = false;
        icons($('lmTaskDetailModal'));
        try {
            const [data] = await Promise.all([
                api(`/api/local-media/tasks/${taskId}`),
                new Promise((resolve) => window.setTimeout(resolve, 320)),
            ]);
            if (requestSerial !== taskDetailRequestSerial) return;
            renderTaskDetail(data);
        } catch (error) {
            if (requestSerial !== taskDetailRequestSerial) return;
            $('lmTaskDetailBody').innerHTML = `<div class="lm-task-detail-error"><i data-lucide="circle-alert"></i><strong>详情读取失败</strong><span>${esc(error.message)}</span></div>`;
            icons($('lmTaskDetailBody'));
        }
    }

    async function clearSelectedTasks() {
        const ids = [...selectedTaskIds];
        if (!ids.length) return;
        const ok = await appConfirm({
            title: '清除选中的整理日志',
            message: `将清除 ${ids.length} 条已选日志。运行中的任务会自动跳过；不会删除、移动或修改任何媒体文件。`,
            confirmText: '清除日志',
            danger: true,
        });
        if (!ok) return;
        const button = $('lmClearTasksBtn');
        setBusy(button, true, '清除中');
        try {
            const result = await api('/api/local-media/tasks', {
                method: 'DELETE',
                body: JSON.stringify({confirm: 'CLEAR', ids}),
            });
            selectedTaskIds.clear();
            await loadAll();
            const skipped = Number(result.skipped_busy || 0);
            appAlert({
                type: skipped ? 'warning' : 'success',
                title: skipped ? '日志已部分清除' : '整理日志已清除',
                message: `已清除 ${Number(result.deleted || 0)} 条${skipped ? `，跳过 ${skipped} 条运行中记录` : ''}。媒体文件未受影响。`,
            });
        } catch (error) {
            appAlert({type: 'error', title: '清除失败', message: error.message});
        } finally {
            setBusy(button, false, '清除选中');
            updateTaskSelectionState();
        }
    }

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
        positionControls.reset();
        positionControls.sync($('lmMediaType').value);
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
        $('lmSearchQuery').value = inspection.suggested_query || inspection.selected_name || activeMediaItem?.name || '';
        positionControls.sync($('lmMediaType').value);
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
        positionControls.sync($('lmMediaType').value);
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
                name: result.primary_video_name || result.selected_name || taskDisplayName(task) || '待确认任务',
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
            positionControls.reset({
                season: task?.season ?? result.parsed_season ?? null,
                episode: task?.episode ?? result.parsed_episode ?? null,
            });
            positionControls.sync($('lmMediaType').value);
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

    function splitPathSegments(path) {
        const raw = String(path || '').trim();
        let normalized = raw.replace(/\\/g, '/');
        let rootLabel = '';
        if (normalized.startsWith('//')) {
            const uncParts = normalized.slice(2).split('/').filter(Boolean);
            rootLabel = uncParts.length ? `\\\\${uncParts.shift()}` : '\\\\';
            normalized = uncParts.join('/');
        } else {
            const drive = normalized.match(/^([A-Za-z]:)(?:\/|$)/);
            if (drive) {
                rootLabel = drive[1];
                normalized = normalized.slice(drive[0].length);
            } else if (normalized.startsWith('/')) {
                rootLabel = '/';
                normalized = normalized.replace(/^\/+/, '');
            }
        }
        const parts = normalized.split('/').filter(Boolean);
        return rootLabel ? [rootLabel, ...parts] : parts;
    }

    function formatPathSegments(segments) {
        if (!segments.length) return '已配置媒体库';
        const [root, ...rest] = segments;
        if (root === '/') return `/${rest.join('/')}` || '/';
        if (/^[A-Za-z]:$/.test(root)) return `${root}/${rest.join('/')}`.replace(/\/$/, '');
        if (root.startsWith('\\\\')) return [root, ...rest].join('\\');
        return segments.join('/');
    }

    function normalizedComparablePath(path) {
        return String(path || '').trim().replace(/\\/g, '/').replace(/\/+$/, '').toLocaleLowerCase();
    }

    function splitPlanTargetPath(targetPath, targetName) {
        const raw = String(targetPath || '').trim();
        const parts = splitPathSegments(raw);
        let filename = String(targetName || '').trim();
        const lastPart = parts[parts.length - 1] || '';
        if (!filename) filename = lastPart || '未命名文件';
        if (lastPart && lastPart === filename) parts.pop();
        return {directories: parts, filename};
    }

    function commonPathPrefix(paths) {
        if (!paths.length) return [];
        const prefix = [...paths[0]];
        for (const parts of paths.slice(1)) {
            let length = 0;
            while (length < prefix.length && length < parts.length
                && prefix[length].toLocaleLowerCase() === parts[length].toLocaleLowerCase()) {
                length += 1;
            }
            prefix.length = length;
        }
        return prefix;
    }

    function configuredPlanRoot(plans) {
        const source = sources.find((item) => Number(item.id) === Number(activeMediaItem?.source_id));
        const roots = (source?.targets || []).map((target) => target.path).filter(Boolean);
        const targets = plans.map((item) => normalizedComparablePath(item.target_path));
        return roots
            .filter((root) => {
                const normalizedRoot = normalizedComparablePath(root);
                return normalizedRoot && targets.every((target) => target === normalizedRoot || target.startsWith(`${normalizedRoot}/`));
            })
            .sort((left, right) => normalizedComparablePath(right).length - normalizedComparablePath(left).length)[0] || '';
    }

    function buildPlanTree(plans) {
        const parsedPlans = plans.map((item) => ({item, ...splitPlanTargetPath(item.target_path, item.target_name)}));
        const configuredRoot = configuredPlanRoot(plans);
        let rootSegments = configuredRoot ? splitPathSegments(configuredRoot) : commonPathPrefix(parsedPlans.map((entry) => entry.directories));
        if (!configuredRoot && rootSegments.length > 3) rootSegments = rootSegments.slice(0, -3);
        const groups = new Map();
        parsedPlans.forEach((entry) => {
            const relative = entry.directories.slice(rootSegments.length);
            const key = relative.join('\u0000');
            if (!groups.has(key)) groups.set(key, {directories: relative, plans: []});
            groups.get(key).plans.push(entry);
        });
        return {
            rootPath: configuredRoot || formatPathSegments(rootSegments),
            groups: [...groups.values()],
        };
    }

    function planActionMeta(action) {
        if (action === 'replace') return {label: '覆盖', className: 'is-replace'};
        if (action === 'skip') return {label: '保留', className: 'is-skip'};
        return {label: '移动', className: ''};
    }

    function planRoleIcon(role) {
        if (role === 'video') return 'file-video-2';
        if (role === 'subtitle') return 'captions';
        if (role === 'image') return 'image';
        return 'file';
    }

    function renderPlanTreeNode(node) {
        return node.groups.map((group) => {
            const directoryLabel = group.directories.length ? group.directories.join(' / ') : '直接写入目标根目录';
            const files = group.plans.map(({item, filename}) => {
                const action = planActionMeta(item.action);
                return `
                    <li class="lm-plan-timeline-file ${action.className}">
                        <span class="lm-plan-timeline-node"><i data-lucide="${planRoleIcon(item.role)}"></i></span>
                        <div class="lm-plan-tree-file-body">
                            <div class="lm-plan-tree-file-head"><strong>${esc(filename)}</strong><b>${action.label}</b></div>
                            <div class="lm-plan-tree-source"><span>来源</span><code>${esc(item.source_path || '—')}</code></div>
                            ${item.note ? `<small>${esc(item.note)}</small>` : ''}
                        </div>
                    </li>`;
            }).join('');
            return `
                <li class="lm-plan-timeline-directory">
                    <span class="lm-plan-timeline-node"><i data-lucide="folder"></i></span>
                    <div><strong>${esc(directoryLabel)}</strong><small>${group.plans.length} 个文件</small></div>
                </li>${files}`;
        }).join('');
    }

    function renderPlanTree(plans, cleanup) {
        const tree = buildPlanTree(plans);
        const cleanupRows = cleanup.map((item) => `
            <div class="lm-plan-row is-delete"><b>清理</b><span>${esc(item.name)} · ${esc(item.reason)}</span></div>`).join('');
        return `
            <section class="lm-plan-tree">
                <div class="lm-plan-tree-head">
                    <span><i data-lucide="folder-tree"></i></span>
                    <div><strong>目标根目录</strong><code>${esc(tree.rootPath)}</code></div>
                </div>
                <ol class="lm-plan-timeline">${renderPlanTreeNode(tree)}</ol>
            </section>
            ${cleanupRows ? `<section class="lm-plan-cleanup"><div class="lm-plan-cleanup-head"><i data-lucide="trash-2"></i><strong>整理后清理</strong><span>${cleanup.length} 项</span></div>${cleanupRows}</section>` : ''}`;
    }

    async function preview(candidate = null) {
        if (!inspection) return false;
        const mediaType = candidate?.media_type || $('lmMediaType').value;
        positionControls.sync(mediaType);
        let positionPayload;
        try {
            positionPayload = positionControls.payload(mediaType, {singleFileRequiresDirty: false});
        } catch (error) {
            $('lmScrapeStatus').textContent = '预览参数无效';
            appAlert({type: 'warning', title: '季集参数无效', message: error.message});
            return false;
        }
        const requestSerial = ++previewRequestSerial;
        const context = Object.freeze({
            inspectionId: inspection.inspection_id,
            tmdbId: candidate?.tmdb_id || '',
            mediaType,
            season: positionPayload.season ?? null,
            episode: positionPayload.episode ?? null,
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
                    season: context.season,
                    episode: context.episode,
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
            const effectivePosition = previewResult.position_overrides || {};
            appliedPreviewContext = Object.freeze({
                ...context,
                season: effectivePosition.season ?? context.season,
                episode: effectivePosition.episode ?? context.episode,
            });
            const plans = previewResult.plans || [];
            const cleanup = previewResult.cleanup || [];
            const replacements = plans.filter((item) => item.action === 'replace').length;
            const skipped = plans.filter((item) => item.action === 'skip').length;
            const matched = previewResult.matches?.[0];
            const positionLabel = Number.isInteger(matched?.episode)
                ? ` · S${String(matched.season ?? 1).padStart(2, '0')}E${String(matched.episode).padStart(2, '0')}`
                : (Number.isInteger(appliedPreviewContext.season) ? ` · 第 ${appliedPreviewContext.season} 季` : '');
            $('lmPlanSummary').textContent = `${plans.length} 项整理 · ${replacements} 项安全覆盖 · ${cleanup.length} 项清理`;
            const primaryTargetPath = plans[0]?.target_path || '';
            $('lmScrapeTarget').textContent = primaryTargetPath ? `将整理至：${primaryTargetPath}` : '目标路径已校验';
            $('lmScrapeTarget').title = primaryTargetPath;
            $('lmScrapeStatus').textContent = '计划已就绪';
            $('lmScrapeDetail').innerHTML = `
                <div class="lm-scrape-summary">
                    <div><strong>${esc(matched?.title || activeMediaItem?.name || '已识别')}</strong><span>${esc(matched?.media_type || context.mediaType)} · TMDB ${esc(matched?.tmdb_id || context.tmdbId || '自动')}${positionLabel}</span></div>
                    <div><strong>${plans.length}</strong><span>媒体与伴随文件</span></div>
                    <div><strong>${replacements}</strong><span>同名目标安全覆盖${skipped ? ` · ${skipped} 项保留` : ''}</span></div>
                </div>
                <div class="lm-plan-list">${renderPlanTree(plans, cleanup)}</div>`;
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
                    season: confirmedContext.season,
                    episode: confirmedContext.episode,
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
        const deleteButton = menu.querySelector('[data-item-action="delete"]');
        if (deleteButton) {
            deleteButton.disabled = !item.deletable;
            deleteButton.title = item.deletable ? '' : '仅来源根目录一级条目可移入回收区';
        }
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
        delete document.documentElement.dataset.localMediaInitialTab;
        if (updateUrl) {
            history.replaceState(null, '', `#${currentTab}`);
        }
        icons();
        armInitialLoading();
        if (currentTab === 'manual' && (hasLoadedLocalMedia || refreshing)) loadAll(false);
    }

    $('lmAddSourceBtn').addEventListener('click', (event) => openSource(null, event.currentTarget));
    $('lmAddSourceMobileBtn')?.addEventListener('click', (event) => openSource(null, event.currentTarget));
    document.querySelectorAll('.lm-tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tabTarget));
    });
    $('lmSourceForm').addEventListener('submit', saveSource);
    $('lmRefreshBtn').addEventListener('click', () => loadAll(true));
    $('lmTaskFilter').addEventListener('change', () => { taskDisplayLimit = 60; renderTasks(true, true); });
    $('lmTaskSearch').addEventListener('input', () => { taskDisplayLimit = 60; renderTasks(true, false); });
    $('lmTaskSelectAll').addEventListener('change', (event) => {
        filteredTasks().filter((task) => task.clearable).forEach((task) => {
            if (event.currentTarget.checked) selectedTaskIds.add(Number(task.id));
            else selectedTaskIds.delete(Number(task.id));
        });
        renderTasks(true, false);
    });
    $('lmTaskList').addEventListener('change', (event) => {
        const checkbox = event.target.closest('[data-task-select]');
        if (!checkbox) return;
        const taskId = Number(checkbox.dataset.taskSelect);
        if (checkbox.checked) selectedTaskIds.add(taskId);
        else selectedTaskIds.delete(taskId);
        checkbox.closest('[data-task-row]')?.classList.toggle('is-selected', checkbox.checked);
        updateTaskSelectionState();
    });
    $('lmClearTasksBtn').addEventListener('click', clearSelectedTasks);
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
    $('lmMediaType').addEventListener('change', () => {
        positionControls.sync($('lmMediaType').value);
        invalidatePreview();
    });
    $('lmScrapeSeason').addEventListener('input', invalidatePreview);
    $('lmScrapeEpisode').addEventListener('input', invalidatePreview);
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

        const breadcrumbButton = event.target.closest('[data-media-breadcrumb]');
        if (breadcrumbButton) {
            const crumb = mediaBrowse.breadcrumbs[Number(breadcrumbButton.dataset.mediaBreadcrumb)];
            if (crumb) await loadMediaDirectory(mediaBrowse.sourceId, crumb.path);
            return;
        }

        const browseHome = event.target.closest('#lmMediaBrowseHome');
        if (browseHome) {
            if (!browseHome.disabled) await loadMediaDirectory();
            return;
        }

        const browseUp = event.target.closest('#lmMediaBrowseUp');
        if (browseUp) {
            if (!browseUp.disabled && mediaBrowse.parentPath) {
                await loadMediaDirectory(mediaBrowse.sourceId, mediaBrowse.parentPath);
            }
            return;
        }

        const directoryRow = event.target.closest('[data-open-directory]');
        if (directoryRow) {
            const item = mediaItemMap.get(directoryRow.dataset.mediaItem || '');
            if (item?.kind === 'directory') await loadMediaDirectory(item.source_id, item.path);
            return;
        }

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

        const taskDetail = event.target.closest('[data-task-detail]');
        if (taskDetail) {
            await openTaskDetail(taskDetail.dataset.taskDetail, taskDetail);
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

    $('lmMediaItems').addEventListener('keydown', async (event) => {
        if (!['Enter', ' '].includes(event.key) || event.target.closest('button')) return;
        const row = event.target.closest('[data-open-directory]');
        const item = mediaItemMap.get(row?.dataset.mediaItem || '');
        if (!item || item.kind !== 'directory') return;
        event.preventDefault();
        await loadMediaDirectory(item.source_id, item.path);
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
