(function () {
    const root = document.querySelector('[data-subscription-center]');
    if (!root) return;

    const state = {
        activeTab: 'media',
        media: [],
        watchlist: [],
        runs: [],
        mediaRequestId: 0,
        watchlistRequestId: 0,
        runsRequestId: 0,
        statsRequestId: 0,
        candidateRequestId: 0,
        mediaController: null,
        watchlistController: null,
        runsController: null,
        statsController: null,
        candidateController: null,
        editingId: null,
        watchlistContext: null,
        candidateSubscriptionId: null,
        loaded: {media: false, watchlist: false, runs: false},
    };

    const modalReturnFocus = new WeakMap();
    const focusableSelector = [
        'a[href]',
        'button:not([disabled])',
        'input:not([disabled]):not([type="hidden"])',
        'select:not([disabled])',
        'textarea:not([disabled])',
        '[tabindex]:not([tabindex="-1"])',
    ].join(',');

    const elements = {
        tabs: [...root.querySelectorAll('[data-subscription-tab]')],
        panels: [...root.querySelectorAll('[data-subscription-panel]')],
        mediaList: document.getElementById('mediaSubscriptionList'),
        watchlist: document.getElementById('subscriptionWatchlist'),
        runs: document.getElementById('subscriptionRunList'),
        mediaStatus: document.getElementById('mediaSubscriptionStatus'),
        mediaRefresh: document.getElementById('refreshMediaSubscriptions'),
        watchlistRefresh: document.getElementById('refreshSubscriptionWatchlist'),
        runsRefresh: document.getElementById('refreshSubscriptionRuns'),
        createMedia: document.getElementById('createMediaSubscription'),
        mediaModal: document.getElementById('mediaSubModal'),
        mediaForm: document.getElementById('mediaSubForm'),
        mediaModalTitle: document.getElementById('mediaSubModalTitle'),
        mediaModalHint: document.getElementById('mediaSubModalHint'),
        mediaFormStatus: document.getElementById('mediaSubFormStatus'),
        mapping: document.getElementById('mediaMappingCandidates'),
        mappingList: document.getElementById('mediaMappingList'),
        mappingCount: document.getElementById('mediaMappingCount'),
        mediaSave: document.getElementById('mediaSubSaveBtn'),
        candidateModal: document.getElementById('mediaCandidateModal'),
        candidateTitle: document.getElementById('mediaCandidateTitle'),
        candidateHint: document.getElementById('mediaCandidateHint'),
        candidateList: document.getElementById('mediaCandidateList'),
        candidateStatus: document.getElementById('mediaCandidateStatus'),
    };

    const fields = {
        id: document.getElementById('ms_subscription_id'),
        provider: document.getElementById('ms_provider'),
        externalId: document.getElementById('ms_external_id'),
        tmdbId: document.getElementById('ms_tmdb_id'),
        mediaType: document.getElementById('ms_media_type'),
        monitorMode: document.getElementById('ms_monitor_mode'),
        seasons: document.getElementById('ms_seasons'),
        action: document.getElementById('ms_action'),
        target: document.getElementById('ms_download_target'),
        sites: document.getElementById('ms_sites'),
        interval: document.getElementById('ms_interval'),
        specials: document.getElementById('ms_include_specials'),
        enabled: document.getElementById('ms_enabled'),
        seasonsRow: document.getElementById('msSeasonsRow'),
        monitorRow: document.getElementById('msMonitorRow'),
        specialsRow: document.getElementById('msSpecialsRow'),
    };

    function renderIcons(target) {
        window.renderLucideIcons?.(target || root);
    }

    function lockElementHeight(element) {
        if (!element) return () => {};
        const height = element.getBoundingClientRect().height;
        if (height > 0) element.style.minHeight = `${Math.ceil(height)}px`;
        return () => window.requestAnimationFrame(() => window.requestAnimationFrame(() => { element.style.minHeight = ''; }));
    }

    function node(tag, className, text) {
        const item = document.createElement(tag);
        if (className) item.className = className;
        if (text !== undefined && text !== null) item.textContent = String(text);
        return item;
    }

    function icon(name) {
        const item = document.createElement('i');
        item.setAttribute('data-lucide', name);
        item.setAttribute('aria-hidden', 'true');
        return item;
    }

    async function apiJSON(path, options = {}, signal) {
        const headers = new Headers(options.headers || {});
        if (options.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
        const response = await fetch(path, {...options, headers, signal});
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = {}; }
        if (!response.ok || payload?.error) {
            const error = new Error(payload?.error || `请求失败（HTTP ${response.status}）`);
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function buttonBusy(button, busy, label = '') {
        if (!button) return;
        if (busy) {
            if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
            if (!button.dataset.idleWidth) button.dataset.idleWidth = String(Math.ceil(button.getBoundingClientRect().width));
            if (button.dataset.idleWidth) button.style.width = `${button.dataset.idleWidth}px`;
            button.disabled = true;
            button.classList.add('is-loading');
            const text = label || button.querySelector('span')?.textContent || '处理中';
            button.replaceChildren(icon('loader-circle'), node('span', '', text));
            renderIcons(button);
            return;
        }
        button.disabled = false;
        button.classList.remove('is-loading');
        if (button.dataset.idleHtml) button.innerHTML = button.dataset.idleHtml;
        button.style.width = '';
        delete button.dataset.idleWidth;
        renderIcons(button);
    }

    function formatDate(value) {
        const raw = String(value || '').trim();
        if (!raw) return '尚未运行';
        const normalized = raw.includes('T') ? raw : raw.replace(' ', 'T');
        const date = new Date(normalized);
        if (!Number.isFinite(date.getTime())) return raw;
        return new Intl.DateTimeFormat('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: false,
        }).format(date).replace(/\//g, '-');
    }

    function formatSize(value) {
        const bytes = Number(value || 0);
        if (!Number.isFinite(bytes) || bytes <= 0) return '';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let current = bytes;
        let index = 0;
        while (current >= 1024 && index < units.length - 1) { current /= 1024; index += 1; }
        return `${current >= 10 || index === 0 ? current.toFixed(0) : current.toFixed(1)} ${units[index]}`;
    }

    function mediaDetailURL(item, resourceFocus = false, returnTo = '') {
        const params = new URLSearchParams();
        params.set('detail_provider', String(item.provider || 'tmdb'));
        params.set('detail_type', String(item.media_type || 'tv'));
        params.set('detail_id', String(item.external_id || item.tmdb_id || ''));
        if (resourceFocus) params.set('resource_focus', '1');
        if (returnTo) params.set('return_to', returnTo);
        return `/discovery?${params.toString()}`;
    }

    function mediaSubscriptionLink(item) {
        return mediaDetailURL(item, false, '/rss#media');
    }

    function statusMeta(status, enabled = true) {
        if (!enabled || status === 'paused') return {label: '已暂停', className: 'is-paused', icon: 'pause'};
        return ({
            new: {label: '待首次检查', className: 'is-new', icon: 'sparkles'},
            checking: {label: '检查中', className: 'is-checking', icon: 'loader-circle'},
            satisfied: {label: '已满足', className: 'is-satisfied', icon: 'circle-check-big'},
            missing: {label: '发现缺失', className: 'is-missing', icon: 'circle-alert'},
            inconclusive: {label: '待核验', className: 'is-inconclusive', icon: 'shield-alert'},
            error: {label: '检查异常', className: 'is-error', icon: 'circle-x'},
        })[status] || {label: status || '未知', className: 'is-new', icon: 'circle-help'};
    }

    function mediaStatusMeta(item) {
        const primary = String(item?.workflow?.primary || '');
        const workflowMeta = {
            submitted: {label: '已推送', className: 'is-submitted', icon: 'send'},
            downloading: {label: '下载中', className: 'is-downloading', icon: 'download'},
            processing: {label: '待整理入库', className: 'is-processing', icon: 'package-open'},
            manual_review: {label: '需要核验', className: 'is-inconclusive', icon: 'shield-alert'},
            delivery_failed: {label: '下载失败', className: 'is-error', icon: 'circle-x'},
            candidate_waiting_auto: {label: '候选未达门槛', className: 'is-candidate', icon: 'list-filter'},
            candidate_waiting_confirm: {label: '候选待确认', className: 'is-candidate', icon: 'list-filter'},
            candidate_available: {label: '已找到候选', className: 'is-candidate', icon: 'list-filter'},
            missing_no_candidate: {label: '等待资源', className: 'is-missing', icon: 'circle-alert'},
        };
        return workflowMeta[primary] || statusMeta(primary || item?.status, item?.enabled);
    }

    function mediaWorkflowSummary(item, result) {
        const workflow = item.workflow && typeof item.workflow === 'object' ? item.workflow : {};
        const primary = String(workflow.primary || '');
        const available = Number(workflow.available_candidate_count || 0);
        const active = Number(workflow.active_count || 0);
        const missing = Math.max(Number(item.missing_count || 0), missingLabels(item).length, active);
        const unit = item.media_type === 'movie' ? '部' : '集';
        const target = downloadTargetLabel(item.download_target);
        if (primary === 'submitted') return `缺失 ${missing || active} ${unit} · 已推送至 ${target}，等待下载`;
        if (primary === 'downloading') return `缺失 ${missing || active} ${unit} · ${target} 正在下载，完成后将自动整理入库`;
        if (primary === 'processing') return `缺失 ${missing || active} ${unit} · 下载已完成，正在整理并等待媒体库入库`;
        if (primary === 'manual_review') return `有 ${Number(workflow.manual_review_count || active)} 个任务需要人工核验，请查看资源候选或下载任务`;
        if (primary === 'delivery_failed') return `缺失 ${missing} ${unit} · 最近一次下载失败，可打开候选重新提交`;
        if (primary === 'candidate_waiting_auto') return `缺失 ${missing} ${unit} · 已找到 ${available} 个候选，尚未满足自动下载条件`;
        if (primary === 'candidate_waiting_confirm') return `缺失 ${missing} ${unit} · ${available} 个候选等待你确认后推送`;
        if (primary === 'candidate_available') return `缺失 ${missing} ${unit} · 已找到 ${available} 个资源候选`;
        if (primary === 'missing_no_candidate') return `缺失 ${missing} ${unit} · 等待资源站返回可用资源`;
        return result.summary || item.last_error || (item.enabled ? '等待下一次媒体库巡检' : '订阅已暂停');
    }

    function actionLabel(action) {
        return ({notify: '仅通知', confirm: '人工确认', auto: '高置信自动下载'})[action] || action || '人工确认';
    }

    function downloadTargetLabel(target) {
        return ({qb: 'qBittorrent', both: '光鸭 + qB', guangya: '光鸭云盘'})[target] || '光鸭云盘';
    }

    function monitorLabel(item) {
        if (item.media_type === 'movie') return '上映后检查';
        if (item.monitor_mode === 'future') return '仅追后续新播';
        if (item.monitor_mode === 'selected') {
            const seasons = Array.isArray(item.seasons) ? item.seasons : [];
            return seasons.length ? `指定 ${seasons.map((value) => `S${String(value).padStart(2, '0')}`).join('、')}` : '指定季度';
        }
        return '全部已播缺集';
    }

    function missingLabels(item) {
        const missing = Array.isArray(item.missing) ? item.missing : [];
        return missing.map((entry) => entry.label || (entry.season !== undefined && entry.episode !== undefined
            ? `S${String(entry.season).padStart(2, '0')}E${String(entry.episode).padStart(2, '0')}` : '电影'));
    }

    function emptyState(iconName, title, hint) {
        const empty = node('div', 'subscription-empty');
        const mark = node('span', 'subscription-empty-icon');
        mark.append(icon(iconName));
        empty.append(mark, node('strong', '', title), node('span', '', hint));
        return empty;
    }

    function errorState(message, retry) {
        const empty = emptyState('triangle-alert', '读取失败', message || '请稍后重试');
        empty.classList.add('is-error');
        if (retry) {
            const button = node('button', 'jump-btn', '重新读取');
            button.type = 'button';
            button.prepend(icon('refresh-cw'));
            button.addEventListener('click', retry);
            empty.append(button);
        }
        renderIcons(empty);
        return empty;
    }

    function skeletonRows(count = 3, kind = 'media') {
        const stack = node('div', `subscription-skeleton-stack is-${kind}`);
        stack.setAttribute('aria-hidden', 'true');
        for (let index = 0; index < count; index += 1) {
            const row = node('div', `subscription-skeleton is-${kind}`);
            row.setAttribute('aria-hidden', 'true');
            row.append(node('span'), node('span'), node('span'));
            stack.append(row);
        }
        return stack;
    }

    function showSkeleton(container, count, kind) {
        container.dataset.skeletonShownAt = String(Date.now());
        container.replaceChildren(skeletonRows(count, kind));
    }

    async function settleSkeleton(container, minimumMs = 320) {
        const shownAt = Number(container?.dataset?.skeletonShownAt || 0);
        if (!shownAt) return;
        const wait = Math.max(0, minimumMs - (Date.now() - shownAt));
        if (wait) await new Promise((resolve) => window.setTimeout(resolve, wait));
        delete container.dataset.skeletonShownAt;
    }

    function renderMediaList(rows) {
        const list = elements.mediaList;
        list.replaceChildren();
        if (!rows.length) {
            list.append(emptyState('radar', '暂无媒体订阅', '可从收藏清单创建追更，或使用 TMDB ID 新建订阅。'));
            renderIcons(list);
            return;
        }
        rows.forEach((item) => {
            const status = mediaStatusMeta(item);
            const result = item.result && typeof item.result === 'object' ? item.result : {};
            const workflow = item.workflow && typeof item.workflow === 'object' ? item.workflow : {};
            const missing = missingLabels(item);
            const candidateCount = Number(
                item.candidate_count ?? workflow.candidate_count ?? result.candidate_count ?? 0
            ) || 0;
            const card = node('article', 'media-subscription-card');
            card.dataset.subscriptionId = String(item.id);

            const art = node('div', 'media-subscription-art');
            art.append(icon(item.media_type === 'movie' ? 'film' : 'tv'));
            art.setAttribute('aria-hidden', 'true');

            const body = node('div', 'media-subscription-body');
            const heading = node('div', 'media-subscription-heading');
            const titleWrap = node('div');
            const title = node('a', '', item.title || '未命名媒体');
            title.href = mediaSubscriptionLink(item);
            titleWrap.append(title);
            const meta = node('div', 'media-subscription-meta');
            [item.year, `TMDB ${item.tmdb_id}`, monitorLabel(item)].filter(Boolean).forEach((value) => meta.append(node('span', '', value)));
            titleWrap.append(meta);
            const badge = node('span', `subscription-status ${status.className}`);
            badge.append(icon(status.icon), node('span', '', status.label));
            heading.append(titleWrap, badge);

            const summary = node('p', 'media-subscription-summary', mediaWorkflowSummary(item, result));
            const details = node('div', 'media-subscription-details');
            [
                ['crosshair', actionLabel(item.action)],
                ['cloud-download', downloadTargetLabel(item.download_target)],
                ['history', `最近 ${formatDate(item.last_checked_at)}`],
                ['calendar-clock', item.enabled ? `下次 ${formatDate(item.next_check_at)}` : '调度已暂停'],
            ].forEach(([iconName, value]) => {
                const detail = node('span');
                detail.append(icon(iconName), node('span', '', value));
                details.append(detail);
            });
            body.append(heading, summary, details);

            if (missing.length) {
                const missingRow = node('div', 'media-subscription-missing');
                const missingTitle = node('strong', '', `缺失 ${missing.length} ${item.media_type === 'movie' ? '部' : '集'}`);
                missingTitle.prepend(icon('circle-alert'));
                missingRow.append(missingTitle);
                missing.slice(0, 3).forEach((label) => missingRow.append(node('span', '', label)));
                if (missing.length > 3) missingRow.append(node('span', 'is-more', `+${missing.length - 3}`));
                body.append(missingRow);
            }

            const actions = node('div', 'media-subscription-actions');
            const check = node('button', 'media-subscription-check');
            check.type = 'button'; check.dataset.mediaAction = 'check'; check.title = '立即检查媒体库'; check.setAttribute('aria-label', '立即检查媒体库');
            check.append(icon('scan-search'), node('span', '', '检查'));
            check.disabled = !item.enabled || item.status === 'checking';
            actions.append(check);
            if (candidateCount > 0) {
                const candidates = node('button', 'icon-action media-subscription-candidate-action');
                candidates.type = 'button'; candidates.dataset.mediaAction = 'candidates'; candidates.title = `查看 ${candidateCount} 个资源候选`; candidates.setAttribute('aria-label', `查看 ${candidateCount} 个资源候选`);
                candidates.append(icon('list-filter'), node('span', 'media-subscription-candidate-count', candidateCount > 99 ? '99+' : candidateCount));
                actions.append(candidates);
            }
            const edit = node('button', 'icon-action');
            edit.type = 'button'; edit.dataset.mediaAction = 'edit'; edit.title = '编辑媒体订阅'; edit.setAttribute('aria-label', '编辑媒体订阅'); edit.append(icon('pencil'));
            const toggle = node('button', 'icon-action');
            toggle.type = 'button'; toggle.dataset.mediaAction = 'toggle'; toggle.title = item.enabled ? '暂停媒体订阅' : '启用媒体订阅'; toggle.setAttribute('aria-label', toggle.title); toggle.append(icon(item.enabled ? 'pause' : 'play'));
            const remove = node('button', 'icon-action danger');
            remove.type = 'button'; remove.dataset.mediaAction = 'delete'; remove.title = '删除媒体订阅'; remove.setAttribute('aria-label', '删除媒体订阅'); remove.append(icon('trash-2'));
            actions.append(edit, toggle, remove);

            card.append(art, body, actions);
            list.append(card);
        });
        renderIcons(list);
    }

    async function loadStats() {
        state.statsController?.abort();
        const controller = new AbortController();
        state.statsController = controller;
        const requestId = state.statsRequestId + 1;
        state.statsRequestId = requestId;
        try {
            const stats = await apiJSON('/api/subscriptions/stats', {}, controller.signal);
            if (requestId !== state.statsRequestId) return;
            const values = {
                subscriptionStatTotal: stats.total,
                subscriptionStatMediaActive: stats.media_active,
                subscriptionStatMissing: stats.media_missing,
                subscriptionStatRssPending: stats.rss_pending,
                subscriptionMediaBadge: stats.media_total,
                subscriptionRssBadge: stats.rss_total,
            };
            Object.entries(values).forEach(([id, value]) => {
                const element = document.getElementById(id);
                if (!element) return;
                const numeric = Number(value) || 0;
                if (element.classList.contains('subscription-metric-value') && window.MFAnim) {
                    window.MFAnim.countUp(element, numeric, {duration: 0.6});
                } else element.textContent = String(numeric);
            });
        } catch (error) {
            if (error.name !== 'AbortError') console.warn('订阅中心统计读取失败', error);
        } finally {
            if (state.statsController === controller) state.statsController = null;
        }
    }

    async function loadMedia({preserve = true} = {}) {
        state.mediaController?.abort();
        const controller = new AbortController();
        state.mediaController = controller;
        const requestId = state.mediaRequestId + 1;
        state.mediaRequestId = requestId;
        const frame = elements.mediaList.closest('.subscription-list-frame');
        frame?.setAttribute('aria-busy', 'true');
        buttonBusy(elements.mediaRefresh, true, '刷新');
        const hadContent = elements.mediaList.childElementCount > 0;
        let skeletonTimer = null;
        if (!hadContent || !preserve) {
            skeletonTimer = window.setTimeout(() => {
                if (requestId !== state.mediaRequestId || elements.mediaList.childElementCount) return;
                showSkeleton(elements.mediaList, 2, 'media');
            }, 320);
        }
        const status = elements.mediaStatus.value;
        try {
            const query = status ? `?status=${encodeURIComponent(status)}` : '';
            const rows = await apiJSON(`/api/subscriptions/media${query}`, {}, controller.signal);
            if (requestId !== state.mediaRequestId) return;
            await settleSkeleton(elements.mediaList);
            if (requestId !== state.mediaRequestId) return;
            state.media = Array.isArray(rows) ? rows : [];
            state.loaded.media = true;
            const releaseHeight = preserve && hadContent ? lockElementHeight(elements.mediaList) : () => {};
            renderMediaList(state.media);
            releaseHeight();
            loadStats();
        } catch (error) {
            if (error.name === 'AbortError' || requestId !== state.mediaRequestId) return;
            if (!hadContent || !preserve) {
                await settleSkeleton(elements.mediaList);
                if (requestId !== state.mediaRequestId) return;
                elements.mediaList.replaceChildren(errorState(error.message, () => loadMedia({preserve: false})));
            } else appAlert?.({type: 'error', title: '媒体订阅读取失败', message: error.message});
        } finally {
            if (skeletonTimer) window.clearTimeout(skeletonTimer);
            if (requestId === state.mediaRequestId) {
                frame?.setAttribute('aria-busy', 'false');
                buttonBusy(elements.mediaRefresh, false);
                state.mediaController = null;
            }
        }
    }

    function watchlistLink(item, resourceFocus = false) {
        return mediaDetailURL(item, resourceFocus, '/rss#watchlist');
    }

    function renderWatchlist(rows) {
        elements.watchlist.replaceChildren();
        document.getElementById('subscriptionWatchlistBadge').textContent = String(rows.length);
        if (!rows.length) {
            elements.watchlist.append(emptyState('bookmark', '收藏清单为空', '在探索页收藏媒体后，可在这里转为追更订阅。'));
            renderIcons(elements.watchlist);
            return;
        }
        rows.forEach((item) => {
            const card = node('article', 'subscription-watch-card');
            card.dataset.provider = item.provider || '';
            card.dataset.externalId = item.external_id || '';
            card.dataset.mediaType = item.media_type || '';
            const poster = node('a', 'subscription-watch-poster');
            poster.href = watchlistLink(item);
            if (item.poster_url) {
                const image = document.createElement('img');
                image.src = item.poster_url; image.alt = `${item.title || '媒体'} 海报`; image.loading = 'lazy'; image.width = 180; image.height = 270;
                image.addEventListener('error', () => { image.remove(); poster.append(icon(item.media_type === 'movie' ? 'film' : 'tv')); renderIcons(poster); }, {once: true});
                poster.append(image);
            } else poster.append(icon(item.media_type === 'movie' ? 'film' : 'tv'));
            const body = node('div', 'subscription-watch-body');
            const title = node('a', '', item.title || '未命名媒体'); title.href = watchlistLink(item);
            const meta = node('div', 'subscription-watch-meta');
            [item.year, item.provider?.toUpperCase(), item.media_type === 'movie' ? '电影' : '剧集'].filter(Boolean).forEach((value) => meta.append(node('span', '', value)));
            body.append(title, meta);
            if (item.subscription) {
                const status = mediaStatusMeta(item.subscription);
                const badge = node('span', `subscription-status ${status.className}`);
                badge.append(icon(status.icon), node('span', '', `已订阅 · ${status.label}`));
                body.append(badge);
            }
            const actions = node('div', 'subscription-watch-actions');
            const primary = node(
                'button',
                'rss-btn is-accent subscription-watch-primary',
                item.subscription ? '管理订阅' : '创建订阅',
            );
            primary.type = 'button';
            primary.dataset.watchAction = item.subscription ? 'manage' : 'subscribe';
            primary.prepend(icon(item.subscription ? 'settings-2' : 'bell-plus'));
            actions.append(primary);
            [
                ['panel-top-open', '媒体档案', watchlistLink(item)],
                ['search', '搜索资源', watchlistLink(item, true)],
                ['download', '打开资源工作台', watchlistLink(item, true)],
            ].forEach(([iconName, label, href]) => {
                const action = node('a', 'icon-action subscription-watch-icon');
                action.href = href;
                action.title = label;
                action.setAttribute('aria-label', label);
                action.append(icon(iconName));
                actions.append(action);
            });
            const remove = node('button', 'icon-action danger subscription-watch-icon');
            remove.type = 'button';
            remove.dataset.watchAction = 'remove';
            remove.title = '取消收藏';
            remove.setAttribute('aria-label', '取消收藏');
            remove.append(icon('bookmark-x'));
            actions.append(remove);
            card.append(poster, body, actions);
            elements.watchlist.append(card);
        });
        renderIcons(elements.watchlist);
    }

    async function loadWatchlist({preserve = true} = {}) {
        state.watchlistController?.abort();
        const controller = new AbortController();
        state.watchlistController = controller;
        const requestId = state.watchlistRequestId + 1;
        state.watchlistRequestId = requestId;
        const frame = elements.watchlist.closest('.subscription-list-frame');
        frame?.setAttribute('aria-busy', 'true');
        buttonBusy(elements.watchlistRefresh, true, '刷新');
        const hadContent = elements.watchlist.childElementCount > 0;
        let timer = null;
        if (!hadContent || !preserve) timer = window.setTimeout(() => {
            if (requestId === state.watchlistRequestId && !elements.watchlist.childElementCount) showSkeleton(elements.watchlist, 4, 'watchlist');
        }, 320);
        try {
            const rows = await apiJSON('/api/subscriptions/watchlist', {}, controller.signal);
            if (requestId !== state.watchlistRequestId) return;
            await settleSkeleton(elements.watchlist);
            if (requestId !== state.watchlistRequestId) return;
            state.watchlist = Array.isArray(rows) ? rows : [];
            state.loaded.watchlist = true;
            const releaseHeight = preserve && hadContent ? lockElementHeight(elements.watchlist) : () => {};
            renderWatchlist(state.watchlist);
            releaseHeight();
        } catch (error) {
            if (error.name === 'AbortError' || requestId !== state.watchlistRequestId) return;
            if (!hadContent || !preserve) {
                await settleSkeleton(elements.watchlist);
                if (requestId !== state.watchlistRequestId) return;
                elements.watchlist.replaceChildren(errorState(error.message, () => loadWatchlist({preserve: false})));
            } else appAlert?.({type: 'error', title: '收藏清单读取失败', message: error.message});
        } finally {
            if (timer) window.clearTimeout(timer);
            if (requestId === state.watchlistRequestId) {
                frame?.setAttribute('aria-busy', 'false');
                buttonBusy(elements.watchlistRefresh, false);
                state.watchlistController = null;
            }
        }
    }

    function runStatus(status) {
        return ({completed: ['已完成', 'is-satisfied', 'circle-check-big'], failed: ['失败', 'is-error', 'circle-x'], running: ['运行中', 'is-checking', 'loader-circle']})[status]
            || [status || '未知', 'is-new', 'circle-help'];
    }

    function renderRuns(rows) {
        elements.runs.replaceChildren();
        if (!rows.length) {
            elements.runs.append(emptyState('history', '暂无巡检记录', '媒体订阅完成首次检查后会在这里留下结果。'));
            renderIcons(elements.runs);
            return;
        }
        rows.forEach((item) => {
            const [label, className, iconName] = runStatus(item.status);
            const payload = item.payload && typeof item.payload === 'object' ? item.payload : {};
            const row = node('article', 'subscription-run-row');
            const mark = node('span', `subscription-run-mark ${className}`); mark.append(icon(iconName));
            const body = node('div', 'subscription-run-body');
            const head = node('div', 'subscription-run-head');
            const title = node('strong', '', item.title || `媒体订阅 #${item.subscription_id}`);
            const badge = node('span', `subscription-status ${className}`); badge.append(node('span', '', label));
            head.append(title, badge);
            const summary = node('p', '', item.error || item.summary || payload.summary || '巡检已完成');
            const meta = node('div', 'subscription-run-meta');
            [item.trigger_type === 'manual' ? '手动检查' : '定时检查', formatDate(item.started_at), item.media_type === 'movie' ? '电影' : '剧集', `TMDB ${item.tmdb_id || '-'}`].forEach((value) => meta.append(node('span', '', value)));
            body.append(head, summary, meta);
            const counts = node('div', 'subscription-run-counts');
            [['缺失', payload.missing_count], ['候选', payload.candidate_count], ['自动提交', payload.auto_submitted]].forEach(([name, value]) => {
                if (value === undefined || value === null) return;
                const cell = node('span'); cell.append(node('small', '', name), node('b', '', String(value))); counts.append(cell);
            });
            row.append(mark, body, counts);
            elements.runs.append(row);
        });
        renderIcons(elements.runs);
    }

    async function loadRuns({preserve = true} = {}) {
        state.runsController?.abort();
        const controller = new AbortController();
        state.runsController = controller;
        const requestId = state.runsRequestId + 1;
        state.runsRequestId = requestId;
        const frame = elements.runs.closest('.subscription-list-frame');
        frame?.setAttribute('aria-busy', 'true');
        buttonBusy(elements.runsRefresh, true, '刷新');
        const hadContent = elements.runs.childElementCount > 0;
        let timer = null;
        if (!hadContent || !preserve) timer = window.setTimeout(() => {
            if (requestId === state.runsRequestId && !elements.runs.childElementCount) showSkeleton(elements.runs, 4, 'runs');
        }, 320);
        try {
            const rows = await apiJSON('/api/subscriptions/runs?limit=100', {}, controller.signal);
            if (requestId !== state.runsRequestId) return;
            await settleSkeleton(elements.runs);
            if (requestId !== state.runsRequestId) return;
            state.runs = Array.isArray(rows) ? rows : [];
            state.loaded.runs = true;
            const releaseHeight = preserve && hadContent ? lockElementHeight(elements.runs) : () => {};
            renderRuns(state.runs);
            releaseHeight();
        } catch (error) {
            if (error.name === 'AbortError' || requestId !== state.runsRequestId) return;
            if (!hadContent || !preserve) {
                await settleSkeleton(elements.runs);
                if (requestId !== state.runsRequestId) return;
                elements.runs.replaceChildren(errorState(error.message, () => loadRuns({preserve: false})));
            } else appAlert?.({type: 'error', title: '运行记录读取失败', message: error.message});
        } finally {
            if (timer) window.clearTimeout(timer);
            if (requestId === state.runsRequestId) {
                frame?.setAttribute('aria-busy', 'false');
                buttonBusy(elements.runsRefresh, false);
                state.runsController = null;
            }
        }
    }

    function activateTab(name, {focus = false} = {}) {
        const normalized = ['media', 'rss', 'watchlist', 'runs'].includes(name) ? name : 'media';
        const nextPanel = elements.panels.find((panel) => panel.dataset.subscriptionPanel === normalized);
        state.activeTab = normalized;
        elements.tabs.forEach((tab) => {
            const active = tab.dataset.subscriptionTab === normalized;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            if (active && focus) tab.focus({preventScroll: true});
        });
        elements.panels.forEach((panel) => {
            const active = panel === nextPanel;
            panel.hidden = !active;
            panel.classList.toggle('is-active', active);
        });
        const url = new URL(window.location.href);
        url.hash = normalized === 'media' ? '' : normalized;
        window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
        if (normalized === 'media' && !state.loaded.media) return loadMedia({preserve: false});
        if (normalized === 'rss') return window.ensureRssPanelLoaded?.() || Promise.resolve();
        if (normalized === 'watchlist' && !state.loaded.watchlist) return loadWatchlist({preserve: false});
        if (normalized === 'runs' && !state.loaded.runs) return loadRuns({preserve: false});
        return Promise.resolve();
    }

    function syncMediaForm() {
        const movie = fields.mediaType.value === 'movie';
        fields.monitorRow.hidden = movie;
        fields.seasonsRow.hidden = movie || fields.monitorMode.value !== 'selected';
        fields.specialsRow.hidden = movie;
        fields.seasons.required = !movie && fields.monitorMode.value === 'selected';
        if (movie) { fields.monitorMode.value = 'missing'; fields.seasons.value = ''; fields.specials.checked = false; }
    }

    function openModal(modal, trigger) {
        if (!modal) return;
        modalReturnFocus.set(modal, trigger instanceof HTMLElement ? trigger : document.activeElement);
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('subscription-modal-open');
        window.requestAnimationFrame(() => modal.querySelector('input:not([type="hidden"]),select,button')?.focus?.({preventScroll: true}));
        renderIcons(modal);
    }

    function closeModal(modal) {
        if (!modal) return;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        const hasVisibleModal = [...document.querySelectorAll('.rss-sub-modal')].some((item) => item.style.display === 'flex' && item.getAttribute('aria-hidden') !== 'true');
        if (!hasVisibleModal) document.body.classList.remove('subscription-modal-open');
        const returnFocus = modalReturnFocus.get(modal);
        modalReturnFocus.delete(modal);
        returnFocus?.focus?.({preventScroll: true});
    }

    function visibleSubscriptionModal() {
        const visible = [...document.querySelectorAll('.rss-sub-modal')].filter((modal) => modal.style.display === 'flex' && modal.getAttribute('aria-hidden') !== 'true');
        return visible.at(-1) || null;
    }

    function trapModalFocus(event, modal) {
        if (event.key !== 'Tab' || !modal) return;
        const focusable = [...modal.querySelectorAll(focusableSelector)].filter((item) => item.offsetParent !== null);
        if (!focusable.length) { event.preventDefault(); modal.focus?.(); return; }
        const first = focusable[0];
        const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }

    function closeCandidateModal() {
        state.candidateController?.abort();
        state.candidateController = null;
        state.candidateRequestId += 1;
        state.candidateSubscriptionId = null;
        closeModal(elements.candidateModal);
    }

    window.openSubscriptionModal = openModal;
    window.closeSubscriptionModal = closeModal;

    const supportedMediaIntervals = new Set(['4320', '10080']);

    function formatLegacyMediaInterval(minutes) {
        const value = Number(minutes);
        if (value >= 1440 && value % 1440 === 0) return `每 ${value / 1440} 天`;
        if (value >= 60 && value % 60 === 0) return `每 ${value / 60} 小时`;
        return `每 ${value} 分钟`;
    }

    function clearLegacyMediaInterval() {
        fields.interval.querySelector('[data-legacy-interval]')?.remove();
        delete fields.interval.dataset.legacyInterval;
    }

    function setMediaInterval(value, {preserveLegacy = false} = {}) {
        clearLegacyMediaInterval();
        const normalized = String(Number(value) || 4320);
        if (preserveLegacy && !supportedMediaIntervals.has(normalized)) {
            const option = new Option(`当前旧设置：${formatLegacyMediaInterval(normalized)}`, normalized, true, true);
            option.disabled = true;
            option.dataset.legacyInterval = 'true';
            fields.interval.prepend(option);
            fields.interval.dataset.legacyInterval = normalized;
            return;
        }
        fields.interval.value = supportedMediaIntervals.has(normalized) ? normalized : '4320';
    }

    function resetMediaForm() {
        state.editingId = null;
        state.watchlistContext = null;
        fields.id.value = '';
        fields.provider.value = 'tmdb';
        fields.externalId.value = '';
        fields.tmdbId.value = '';
        fields.tmdbId.disabled = false;
        fields.mediaType.value = 'tv';
        fields.mediaType.disabled = false;
        fields.monitorMode.value = 'missing';
        fields.seasons.value = '';
        fields.action.value = 'confirm';
        fields.target.value = 'guangya';
        fields.sites.value = '';
        setMediaInterval(4320);
        fields.specials.checked = false;
        fields.enabled.checked = true;
        elements.mediaFormStatus.textContent = '';
        elements.mapping.hidden = true;
        elements.mappingList.replaceChildren();
        elements.mappingCount.textContent = '';
        elements.mediaModalTitle.textContent = '新建媒体订阅';
        elements.mediaModalHint.textContent = '使用 TMDB ID 建立可靠的媒体身份。';
        syncMediaForm();
    }

    function openCreateMedia(trigger = elements.createMedia) {
        resetMediaForm();
        openModal(elements.mediaModal, trigger);
    }

    function openEditMedia(item, trigger) {
        resetMediaForm();
        state.editingId = Number(item.id);
        fields.id.value = String(item.id);
        fields.provider.value = item.provider || 'tmdb';
        fields.externalId.value = item.external_id || item.tmdb_id || '';
        fields.tmdbId.value = item.tmdb_id || '';
        fields.tmdbId.disabled = true;
        fields.mediaType.value = item.media_type || 'tv';
        fields.mediaType.disabled = true;
        fields.monitorMode.value = item.monitor_mode || 'missing';
        fields.seasons.value = Array.isArray(item.seasons) ? item.seasons.join(', ') : '';
        fields.action.value = item.action || 'confirm';
        fields.target.value = item.download_target || 'guangya';
        fields.sites.value = Array.isArray(item.sites) ? item.sites.join(', ') : '';
        setMediaInterval(item.check_interval_minutes || 4320, {preserveLegacy: true});
        fields.specials.checked = Boolean(item.include_specials);
        fields.enabled.checked = Boolean(item.enabled);
        elements.mediaModalTitle.textContent = `编辑 · ${item.title || '媒体订阅'}`;
        elements.mediaModalHint.textContent = `TMDB ${item.tmdb_id} · 保存后将重新排定检查。`;
        syncMediaForm();
        openModal(elements.mediaModal, trigger);
    }

    async function openWatchlistSubscription(item, trigger) {
        if (item.subscription) {
            if (elements.mediaStatus && elements.mediaStatus.value) {
                elements.mediaStatus.value = '';
            }
            await activateTab('media');
            await loadMedia({preserve: true});
            const card = root.querySelector(`[data-subscription-id="${item.subscription.id}"]`);
            await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
            if (card) {
                card.tabIndex = -1;
                const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
                card.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'center'});
                card.focus({preventScroll: true});
                card.classList.add('is-focus-target');
                window.setTimeout(() => card.classList.remove('is-focus-target'), 1400);
            } else {
                appAlert?.({type: 'warning', title: '订阅未找到', message: '订阅列表可能已发生变化，请刷新收藏清单后重试。'});
            }
            return;
        }
        resetMediaForm();
        state.watchlistContext = item;
        fields.provider.value = item.provider || 'tmdb';
        fields.externalId.value = item.external_id || '';
        fields.mediaType.value = item.media_type || 'tv';
        fields.mediaType.disabled = true;
        if (item.provider === 'tmdb') fields.tmdbId.value = item.external_id || '';
        elements.mediaModalTitle.textContent = `创建追更 · ${item.title || '收藏媒体'}`;
        elements.mediaModalHint.textContent = [item.year, item.provider?.toUpperCase()].filter(Boolean).join(' · ') || '将收藏项映射到 TMDB 后建立订阅。';
        syncMediaForm();
        openModal(elements.mediaModal, trigger);
    }

    function renderMappingCandidates(candidates) {
        const rows = Array.isArray(candidates) ? candidates.filter((item) => /^\d{1,10}$/.test(String(item.tmdb_id || item.id || ''))) : [];
        elements.mappingList.replaceChildren();
        const visibleRows = rows.slice(0, 8);
        elements.mappingCount.textContent = rows.length > visibleRows.length
            ? `显示 ${visibleRows.length} / ${rows.length} 项`
            : `${rows.length} 项`;
        elements.mapping.hidden = !rows.length;
        visibleRows.forEach((item) => {
            const button = node('button', 'subscription-mapping-option');
            button.type = 'button';
            button.dataset.tmdbId = String(item.tmdb_id || item.id || '');
            const copy = node('span');
            copy.append(node('strong', '', item.title || item.name || '未命名媒体'));
            copy.append(node('small', '', [item.year || '年份未知', `TMDB ${button.dataset.tmdbId}`].join(' · ')));
            const score = Number(item.score || 0);
            if (score > 0) button.append(copy, node('b', '', `${Math.round(score * 100)}%`));
            else button.append(copy, icon('chevron-right'));
            button.addEventListener('click', () => {
                fields.tmdbId.value = button.dataset.tmdbId;
                elements.mappingList.querySelectorAll('.subscription-mapping-option').forEach((row) => row.classList.toggle('is-selected', row === button));
                elements.mediaFormStatus.textContent = `已选择 TMDB ${button.dataset.tmdbId}，请确认后保存`;
                fields.tmdbId.focus({preventScroll: true});
            });
            elements.mappingList.append(button);
        });
        renderIcons(elements.mapping);
    }

    function parseIntegerList(value) {
        const raw = String(value || '').trim();
        if (!raw) return [];
        const result = [];
        raw.split(/[，,\s]+/).filter(Boolean).forEach((part) => {
            if (!/^\d{1,3}$/.test(part)) throw new Error('季度必须是 0 到 100 的整数');
            const number = Number(part);
            if (number < 0 || number > 100) throw new Error('季度必须是 0 到 100 的整数');
            if (!result.includes(number)) result.push(number);
        });
        return result.sort((a, b) => a - b);
    }

    function parseSites(value) {
        return [...new Set(String(value || '').split(/[，,\s]+/).map((item) => item.trim().toLowerCase()).filter(Boolean))];
    }

    async function saveMediaSubscription(event) {
        event.preventDefault();
        elements.mediaFormStatus.textContent = '';
        let seasons;
        try { seasons = parseIntegerList(fields.seasons.value); } catch (error) { elements.mediaFormStatus.textContent = error.message; fields.seasons.focus(); return; }
        const tmdbId = fields.tmdbId.value.trim();
        const canMapWatchlist = Boolean(state.watchlistContext && state.watchlistContext.provider !== 'tmdb' && !tmdbId);
        if (!canMapWatchlist && (!/^\d{1,10}$/.test(tmdbId) || Number(tmdbId) <= 0)) {
            elements.mediaFormStatus.textContent = '请输入有效的 TMDB ID';
            fields.tmdbId.focus();
            return;
        }
        if (fields.mediaType.value === 'tv' && fields.monitorMode.value === 'selected' && !seasons.length) {
            elements.mediaFormStatus.textContent = '指定季度模式至少填写一个季度';
            fields.seasons.focus();
            return;
        }
        const payload = {
            tmdb_id: tmdbId,
            media_type: fields.mediaType.value,
            monitor_mode: fields.mediaType.value === 'movie' ? 'missing' : fields.monitorMode.value,
            seasons: fields.mediaType.value === 'movie' ? [] : seasons,
            include_specials: fields.mediaType.value === 'tv' && fields.specials.checked,
            action: fields.action.value,
            download_target: fields.target.value,
            sites: parseSites(fields.sites.value),
            check_interval_minutes: Number(fields.interval.value || 4320),
            enabled: fields.enabled.checked,
        };
        let path = '/api/subscriptions/media';
        let method = 'POST';
        if (state.editingId) {
            path = `/api/subscriptions/media/${state.editingId}`;
            method = 'PUT';
            delete payload.tmdb_id;
            delete payload.media_type;
            if (fields.interval.dataset.legacyInterval === fields.interval.value) {
                delete payload.check_interval_minutes;
            }
        } else if (state.watchlistContext) {
            path = '/api/subscriptions/media/from-watchlist';
            payload.provider = state.watchlistContext.provider;
            payload.external_id = state.watchlistContext.external_id;
        } else {
            payload.provider = 'tmdb';
            payload.external_id = tmdbId;
        }
        buttonBusy(elements.mediaSave, true, '保存中');
        try {
            const result = await apiJSON(path, {method, body: JSON.stringify(payload)});
            closeModal(elements.mediaModal);
            await Promise.all([loadMedia({preserve: true}), loadStats(), loadWatchlist({preserve: true})]);
            appAlert?.({type: 'success', title: result.created ? '媒体订阅已创建' : '媒体订阅已更新', message: '系统将按检查周期核对媒体库，并避免重复下载。'});
        } catch (error) {
            const candidates = Array.isArray(error.payload?.candidates) ? error.payload.candidates : [];
            if (error.payload?.code === 'mapping_required' && candidates.length) {
                renderMappingCandidates(candidates);
                elements.mediaFormStatus.textContent = '请选择下方正确的 TMDB 媒体，再次保存即可';
                const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
                elements.mapping.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest'});
            } else {
                elements.mediaFormStatus.textContent = error.message;
            }
        } finally {
            buttonBusy(elements.mediaSave, false);
        }
    }

    async function checkMedia(item, trigger) {
        buttonBusy(trigger, true, '检查中');
        try {
            const result = await apiJSON(`/api/subscriptions/media/${item.id}/check`, {method: 'POST'});
            const missing = Number(result.missing_count || 0);
            const candidates = Number(result.candidate_count || 0);
            await Promise.all([loadMedia({preserve: true}), loadRuns({preserve: true}), loadStats()]);
            appAlert?.({
                type: result.status === 'error' ? 'error' : result.status === 'inconclusive' ? 'warning' : 'success',
                title: '媒体巡检完成',
                message: `${result.summary || '检查完成'}\n缺失 ${missing} 项 · 候选 ${candidates} 项`,
            });
        } catch (error) {
            appAlert?.({type: error.payload?.code === 'busy' ? 'warning' : 'error', title: '媒体巡检未完成', message: error.message});
        } finally {
            buttonBusy(trigger, false);
        }
    }

    async function toggleMedia(item, trigger) {
        buttonBusy(trigger, true, item.enabled ? '暂停中' : '启用中');
        try {
            await apiJSON(`/api/subscriptions/media/${item.id}`, {method: 'PUT', body: JSON.stringify({enabled: !item.enabled})});
            await Promise.all([loadMedia({preserve: true}), loadStats()]);
        } catch (error) {
            appAlert?.({type: 'error', title: item.enabled ? '暂停失败' : '启用失败', message: error.message});
        } finally { buttonBusy(trigger, false); }
    }

    async function deleteMedia(item) {
        const confirmed = await appConfirm?.({title: '删除媒体订阅', message: `删除“${item.title || '该媒体'}”的追更配置和候选记录？已提交的下载任务与媒体文件不会被删除。`, confirmText: '删除订阅', danger: true});
        if (!confirmed) return;
        try {
            await apiJSON(`/api/subscriptions/media/${item.id}`, {method: 'DELETE'});
            await Promise.all([loadMedia({preserve: true}), loadWatchlist({preserve: true}), loadStats()]);
        } catch (error) { appAlert?.({type: 'error', title: '删除失败', message: error.message}); }
    }

    function candidateLabel(item) {
        if (item.season !== null && item.season !== undefined && item.episode !== null && item.episode !== undefined) {
            return `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}`;
        }
        return '电影';
    }

    function candidateDeliveryView(item) {
        const delivery = item.delivery || {};
        const requestStatus = String(delivery.request_status || '');
        const admissionStatus = String(delivery.status || '');
        const postFailed = ['failed', 'requires_manual'].includes(String(delivery.organize_status || ''))
            || ['failed', 'requires_manual'].includes(String(delivery.local_import_status || ''))
            || ['failed'].includes(String(delivery.strm_status || ''));
        if (postFailed) return {label: '处理失败', tone: 'error'};
        if (requestStatus === 'manual_review') return {label: '需要核验', tone: 'warning'};
        if (requestStatus === 'failed' || admissionStatus === 'failed') return {label: '下载失败', tone: 'error'};
        if (admissionStatus === 'completed') return {label: '已入库', tone: 'success'};
        if (requestStatus === 'completed' || admissionStatus === 'processing') return {label: '等待入库', tone: 'processing'};
        if (requestStatus === 'downloading' || admissionStatus === 'downloading') return {label: '下载中', tone: 'downloading'};
        if (item.status === 'submitted' || admissionStatus) return {label: '已提交', tone: 'submitted'};
        return null;
    }

    function renderCandidates(subscription, rows) {
        elements.candidateList.replaceChildren();
        if (!rows.length) {
            elements.candidateList.append(emptyState('search-x', '暂无有效候选', '请先执行一次媒体巡检，或等待资源站返回新资源。'));
            renderIcons(elements.candidateList);
            return;
        }
        rows.forEach((item) => {
            const row = node('article', 'subscription-candidate-row');
            const body = node('div', 'subscription-candidate-body');
            const top = node('div', 'subscription-candidate-top');
            top.append(node('span', 'subscription-position', candidateLabel(item)), node('span', 'subscription-candidate-site', item.site_name || item.site_id || '未知站点'));
            const deliveryView = candidateDeliveryView(item);
            if (deliveryView) top.append(node('span', `subscription-candidate-delivery is-${deliveryView.tone}`, deliveryView.label));
            const title = node('strong', '', item.title || '未命名资源');
            const meta = node('div', 'subscription-candidate-meta');
            [item.size_text || formatSize(item.size_bytes), item.seeders !== null && item.seeders !== undefined ? `种子 ${item.seeders}` : '', item.published_at ? `发布 ${formatDate(item.published_at)}` : '', item.relevance_score !== null && item.relevance_score !== undefined ? `匹配 ${item.relevance_score}%` : ''].filter(Boolean).forEach((value) => meta.append(node('span', '', value)));
            body.append(top, title, meta);
            if (Array.isArray(item.match_reasons) && item.match_reasons.length) body.append(node('p', 'subscription-candidate-reason', item.match_reasons.join(' · ')));
            if (item.delivery?.error) body.append(node('p', 'subscription-candidate-error', item.delivery.error));
            const actions = node('div', 'subscription-candidate-actions');
            if (item.status === 'available') {
                const target = subscription.download_target || 'guangya';
                const label = target === 'both' ? '同时发送到 qB + 光鸭' : target === 'qb' ? '发送到 qB' : '发送到光鸭';
                const button = node('button', 'rss-btn is-accent', label);
                button.type = 'button'; button.dataset.candidateId = String(item.id); button.dataset.target = target; button.prepend(icon(target === 'qb' ? 'download' : target === 'both' ? 'send' : 'cloud-download')); actions.append(button);
            } else {
                const locked = node('span', 'subscription-candidate-locked', deliveryView?.label || '已提交');
                locked.prepend(icon(deliveryView?.tone === 'error' ? 'triangle-alert' : 'activity'));
                actions.append(locked);
            }
            row.append(body, actions);
            elements.candidateList.append(row);
        });
        renderIcons(elements.candidateList);
    }

    async function openCandidates(item, trigger) {
        state.candidateController?.abort();
        const controller = new AbortController();
        const requestId = state.candidateRequestId + 1;
        state.candidateRequestId = requestId;
        state.candidateController = controller;
        const subscriptionId = Number(item.id);
        state.candidateSubscriptionId = subscriptionId;
        elements.candidateTitle.textContent = `资源候选 · ${item.title || '媒体订阅'}`;
        elements.candidateHint.textContent = `候选按匹配度排序；已提交的季集会由下载准入去重。`;
        elements.candidateStatus.textContent = '';
        showSkeleton(elements.candidateList, 3, 'candidate');
        openModal(elements.candidateModal, trigger);
        try {
            const rows = await apiJSON(`/api/subscriptions/media/${subscriptionId}/candidates`, {}, controller.signal);
            if (requestId !== state.candidateRequestId || state.candidateSubscriptionId !== subscriptionId) return;
            await settleSkeleton(elements.candidateList);
            if (requestId !== state.candidateRequestId || state.candidateSubscriptionId !== subscriptionId) return;
            renderCandidates(item, Array.isArray(rows) ? rows : []);
        } catch (error) {
            if (error.name === 'AbortError' || requestId !== state.candidateRequestId || state.candidateSubscriptionId !== subscriptionId) return;
            await settleSkeleton(elements.candidateList);
            if (requestId !== state.candidateRequestId || state.candidateSubscriptionId !== subscriptionId) return;
            elements.candidateList.replaceChildren(errorState(error.message, () => openCandidates(item, trigger)));
        } finally {
            if (state.candidateController === controller) state.candidateController = null;
        }
    }

    async function downloadCandidate(candidateId, target, trigger) {
        const subscriptionId = state.candidateSubscriptionId;
        if (!subscriptionId) return;
        const targetLabel = target === 'qb' ? 'qBittorrent' : target === 'both' ? 'qBittorrent + 光鸭云盘' : '光鸭云盘';
        buttonBusy(trigger, true, '提交中');
        elements.candidateStatus.textContent = '';
        try {
            const result = await apiJSON(`/api/subscriptions/media/${subscriptionId}/download`, {method: 'POST', body: JSON.stringify({candidate_id: candidateId, target})});
            const duplicate = Boolean(result.duplicate);
            const partial = result.status === 'partial';
            const succeeded = Array.isArray(result.succeeded) ? result.succeeded.join('、') : '';
            const failed = Array.isArray(result.failed) ? result.failed.join('、') : '';
            const message = duplicate ? '该季集已有下载或入库任务，未重复提交。' : partial ? `已提交：${succeeded || '部分目标'}；失败：${failed || '部分目标'}。` : `任务已提交到${targetLabel}。`;
            const contextActive = state.candidateSubscriptionId === subscriptionId;
            if (contextActive) elements.candidateStatus.textContent = message;
            void window.appAlert?.({
                type: duplicate ? 'info' : partial ? 'warning' : 'success',
                title: duplicate ? '未重复提交' : partial ? '部分目标提交失败' : `已发送到${targetLabel}`,
                message,
            });
            const subscription = contextActive ? state.media.find((item) => Number(item.id) === subscriptionId) : null;
            if (subscription && contextActive) {
                const rows = await apiJSON(`/api/subscriptions/media/${subscription.id}/candidates`);
                if (state.candidateSubscriptionId === subscriptionId) renderCandidates(subscription, Array.isArray(rows) ? rows : []);
            }
            void loadStats();
        } catch (error) {
            const duplicate = Boolean(error.payload?.duplicate);
            const message = duplicate ? '该季集已有下载或入库任务，未重复提交。' : (error.message || `发送到${targetLabel}失败`);
            if (state.candidateSubscriptionId === subscriptionId) elements.candidateStatus.textContent = message;
            void window.appAlert?.({
                type: duplicate ? 'info' : 'error',
                title: duplicate ? '未重复提交' : `发送到${targetLabel}失败`,
                message,
            });
        } finally { buttonBusy(trigger, false); }
    }

    async function removeWatchlist(item) {
        const confirmed = await appConfirm?.({title: '取消收藏', message: `从收藏清单移除“${item.title || '该媒体'}”？已创建的媒体订阅不会被删除。`, confirmText: '取消收藏', danger: true});
        if (!confirmed) return;
        try {
            const provider = encodeURIComponent(item.provider || '');
            const mediaType = encodeURIComponent(item.media_type || '');
            const externalId = encodeURIComponent(item.external_id || '');
            await apiJSON(`/api/discovery/watchlist/${provider}/${mediaType}/${externalId}`, {method: 'DELETE'});
            await loadWatchlist({preserve: true});
        } catch (error) { appAlert?.({type: 'error', title: '取消收藏失败', message: error.message}); }
    }

    elements.tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => activateTab(tab.dataset.subscriptionTab));
        tab.addEventListener('keydown', (event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            let next = index;
            if (event.key === 'ArrowLeft') next = (index - 1 + elements.tabs.length) % elements.tabs.length;
            if (event.key === 'ArrowRight') next = (index + 1) % elements.tabs.length;
            if (event.key === 'Home') next = 0;
            if (event.key === 'End') next = elements.tabs.length - 1;
            activateTab(elements.tabs[next].dataset.subscriptionTab, {focus: true});
        });
    });

    elements.mediaList.addEventListener('click', (event) => {
        const button = event.target.closest('[data-media-action]');
        const card = event.target.closest('[data-subscription-id]');
        if (!button || !card) return;
        const item = state.media.find((row) => Number(row.id) === Number(card.dataset.subscriptionId));
        if (!item) return;
        const action = button.dataset.mediaAction;
        if (action === 'check') checkMedia(item, button);
        if (action === 'candidates') openCandidates(item, button);
        if (action === 'edit') openEditMedia(item, button);
        if (action === 'toggle') toggleMedia(item, button);
        if (action === 'delete') deleteMedia(item);
    });

    elements.watchlist.addEventListener('click', (event) => {
        const button = event.target.closest('[data-watch-action]');
        const card = event.target.closest('[data-provider][data-external-id][data-media-type]');
        if (!button || !card) return;
        const item = state.watchlist.find((row) => row.provider === card.dataset.provider && row.external_id === card.dataset.externalId && row.media_type === card.dataset.mediaType);
        if (!item) return;
        if (button.dataset.watchAction === 'subscribe' || button.dataset.watchAction === 'manage') openWatchlistSubscription(item, button);
        if (button.dataset.watchAction === 'remove') removeWatchlist(item);
    });

    elements.candidateList.addEventListener('click', (event) => {
        const button = event.target.closest('[data-candidate-id]');
        if (!button) return;
        downloadCandidate(Number(button.dataset.candidateId), button.dataset.target || '', button);
    });

    elements.mediaStatus.addEventListener('change', () => loadMedia({preserve: true}));
    elements.mediaRefresh.addEventListener('click', () => loadMedia({preserve: true}));
    elements.watchlistRefresh.addEventListener('click', () => loadWatchlist({preserve: true}));
    elements.runsRefresh.addEventListener('click', () => loadRuns({preserve: true}));
    elements.createMedia.addEventListener('click', () => openCreateMedia(elements.createMedia));
    elements.mediaForm.addEventListener('submit', saveMediaSubscription);
    fields.mediaType.addEventListener('change', syncMediaForm);
    fields.monitorMode.addEventListener('change', syncMediaForm);
    document.querySelectorAll('[data-media-sub-close]').forEach((button) => button.addEventListener('click', () => closeModal(elements.mediaModal)));
    document.querySelectorAll('[data-media-candidate-close]').forEach((button) => button.addEventListener('click', closeCandidateModal));
    elements.mediaModal?.addEventListener('click', (event) => { if (event.target === elements.mediaModal) closeModal(elements.mediaModal); });
    elements.candidateModal?.addEventListener('click', (event) => { if (event.target === elements.candidateModal) closeCandidateModal(); });
    document.addEventListener('keydown', (event) => {
        const modal = visibleSubscriptionModal();
        if (!modal) return;
        if (event.key === 'Tab') { trapModalFocus(event, modal); return; }
        if (event.key !== 'Escape') return;
        event.preventDefault();
        if (modal === elements.candidateModal) closeCandidateModal();
        else if (modal.id === 'subModal' && typeof window.closeSubForm === 'function') window.closeSubForm();
        else closeModal(modal);
    });

    document.addEventListener('mediaflux:rss-stats-updated', () => { void loadStats(); });

    const initialTab = window.location.hash.replace('#', '');
    activateTab(['rss', 'watchlist', 'runs'].includes(initialTab) ? initialTab : 'media');
    delete document.documentElement.dataset.subscriptionInitialTab;
    loadStats();
})();
