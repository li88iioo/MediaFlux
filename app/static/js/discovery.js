// MediaFlux 媒体探索：安全 DOM 渲染、可取消请求与稳定加载状态。
(() => {
    'use strict';

    const root = document.querySelector('[data-discovery-root]');
    if (!root) return;
    const profileOnly = root.dataset.discoveryProfileHost === 'true';

    const SKELETON_DELAY_MS = 300;
    const SKELETON_MIN_VISIBLE_MS = 300;
    const RESOURCE_SKELETON_MIN_VISIBLE_MS = 300;
    const PROVIDER_LABELS = {tmdb: 'TMDB', douban: '豆瓣', bangumi: 'Bangumi', all: '综合'};
    const MEDIA_LABELS = {movie: '电影', tv: '剧集', anime: '动画', all: '全部'};
    const FALLBACK_CATEGORIES = {tmdb: 'discover', douban: 'recommend', bangumi: 'calendar'};
    const DISCOVERY_SEARCH_PATH = '/api/discovery/search';
    const INDEXER_SEARCH_PATH = '/api/indexers/search';
    const INDEXER_DOWNLOAD_PATH = '/api/indexers/download';
    const INDEXER_DOWNLOAD_BATCH_PATH = '/api/indexers/download/batch';
    const INDEXER_DOWNLOAD_RESUBMIT_PATH = '/api/indexers/download/resubmit';
    const RESOURCE_SELECTION_LIMIT = 50;
    const RESOURCE_TERMINAL_STATUSES = new Set(['expired', 'request_unknown', 'manual_review']);
    const RESOURCE_MANUAL_REVIEW_MESSAGE = '请核对下载列表/目标状态，必要时重新检索后人工处理';
    const DOWNLOAD_REQUEST_STATUS_LABELS = {
        pending: '等待提交',
        submitting: '提交中',
        submitted: '已提交',
        downloading: '下载中',
        completed: '已完成',
        failed: '失败',
        cancelled: '已取消',
        manual_review: '待核对',
        outcome_unknown: '待核对',
        resubmitted: '已重新提交',
    };
    const VIEW_CACHE_FRESH_MS = 120000;
    const VIEW_CACHE_LIMIT = 24;
    const SECTION_PRIMARY_CONCURRENCY = 3;
    const SECTION_BACKGROUND_CONCURRENCY = 2;
    const CARD_NODE_CACHE_LIMIT = 800;
    const SECTION_NODE_CACHE_LIMIT = 64;
    const RESOURCE_MATCH_REASON_LABELS = {
        title_exact: '标题精确',
        title_contains: '标题包含',
        title_similar: '标题相近',
        title_weak: '弱匹配',
        year_match: '年份一致',
        year_conflict: '年份冲突',
        download_ready: '可直接下载',
        download_resolvable: '可解析下载',
        seeded: '有做种',
        recent: '近期发布',
    };
    const DIALOG_NOTICE_SUCCESS_MS = 4000;
    const RESOURCE_SORT_OPTIONS = [
        ['published_desc', '发布时间：新到旧'],
        ['relevance_desc', '综合匹配：高到低'],
        ['episode_desc', '季集号：高到低'],
        ['seeders_desc', '做种数：多到少'],
        ['size_desc', '文件大小：大到小'],
        ['size_asc', '文件大小：小到大'],
        ['source_order', '源站原始顺序'],
    ];

    function formatBytes(bytes) {
        const num = Number(bytes);
        if (!Number.isFinite(num) || num <= 0) return '';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let idx = 0;
        let val = num;
        while (val >= 1024 && idx < units.length - 1) {
            val /= 1024;
            idx += 1;
        }
        return `${val.toFixed(val >= 100 || idx === 0 ? 0 : val >= 10 ? 1 : 2)} ${units[idx]}`;
    }

    function resourceFormattedSize(sizeBytes) {
        if (!sizeBytes) return '';
        return formatBytes(sizeBytes);
    }

    const elements = {
        tabs: document.getElementById('discovery-source-tabs'),
        filters: document.getElementById('discovery-filter-region'),
        searchForm: document.getElementById('discovery-search-form'),
        searchQuery: document.getElementById('discovery-search-query'),
        searchSubmit: document.getElementById('discovery-search-submit'),
        providerStatus: document.getElementById('discovery-provider-status'),
        sections: document.getElementById('discovery-sections'),
        grid: document.getElementById('discovery-grid'),
        stage: document.getElementById('discovery-stage'),
        live: document.getElementById('discovery-live'),
        refresh: document.getElementById('discovery-refresh'),
        loadMoreRow: document.getElementById('discovery-load-more-row'),
        loadMore: document.getElementById('discovery-load-more'),
        sentinel: document.getElementById('discovery-page-sentinel'),
        dialog: document.getElementById('discovery-detail-dialog'),
        dialogBody: document.getElementById('discovery-detail-body'),
        dialogNotice: document.getElementById('discovery-dialog-notice'),
        dialogNoticeIcon: document.getElementById('discovery-dialog-notice-icon'),
        dialogNoticeTitle: document.getElementById('discovery-dialog-notice-title'),
        dialogNoticeText: document.getElementById('discovery-dialog-notice-text'),
        dialogNoticeActions: document.getElementById('discovery-dialog-notice-actions'),
        dialogNoticeClose: document.getElementById('discovery-dialog-notice-close'),
    };

    const state = {
        mode: 'sections',
        provider: 'all',
        mediaType: 'all',
        category: '',
        searchQuery: '',
        searchProviders: ['tmdb', 'douban', 'bangumi'],
        page: 1,
        hasMore: false,
        filters: {},
        filterDefinitions: [],
        sectionsData: [],
        itemsData: [],
        controller: null,
        requestId: 0,
        detailController: null,
        detailRequestId: 0,
        loading: false,
        loadingMore: false,
        observer: null,
        appendError: null,
        statusPayload: null,
        manualPagination: typeof window.IntersectionObserver !== 'function',
        activeCard: null,
        resourceResultsEnabled: root.dataset.resourceResultsEnabled !== 'false',
        selectedResourceIds: new Set(),
        resourceResults: new Map(),
        activeResourceSiteId: '',
        resourceSort: 'published_desc',
        resourceSourceOrder: new Map(),
        resourcePage: 1,
        resourceHasMore: false,
        resourceLoadingMore: false,
        resourceSiteStatuses: [],
        resourceSearchContext: null,
        resourceSubmitState: new Map(),
        resourceBatchBusy: false,
        resourceBatchSummary: '',
        resourceSearchController: null,
        resourceSearchRequestId: 0,
        resourceSubmissionRequestId: 0,
        resourceSubmissionRequests: new Map(),
        pendingResourceNotifications: [],
        resourceNotificationFlushPromise: null,
        dialogNoticeTimer: null,
        detailExitInProgress: false,
    };
    const filterDefinitionsCache = new Map();
    const viewSnapshots = new Map();
    const tabLastView = new Map();
    const cardNodeCache = new Map();
    const sectionNodeCache = new Map();

    function node(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined && text !== null) element.textContent = String(text);
        return element;
    }

    function icon(name) {
        const element = node('i');
        element.setAttribute('data-lucide', name);
        element.setAttribute('aria-hidden', 'true');
        return element;
    }

    function renderIcons(target) {
        window.renderLucideIcons?.(target || root);
    }

    function announce(message) {
        elements.live.textContent = message;
    }

    function asArray(value) {
        if (Array.isArray(value)) return value;
        return [];
    }

    function itemKey(item) {
        return [item.provider || 'unknown', item.media_type || 'unknown', item.external_id || item.id || 'unknown'].join(':');
    }

    function sortedObject(value) {
        return Object.fromEntries(Object.entries(value || {}).sort(([left], [right]) => left.localeCompare(right)));
    }

    function activeTabKey() {
        if (state.mode === 'sections') return 'sections';
        if (state.mode === 'search') return `search:${state.searchQuery}`;
        return ['items', state.provider, state.mediaType, state.category].join(':');
    }

    function activeViewKey() {
        return JSON.stringify({
            mode: state.mode,
            provider: state.provider,
            mediaType: state.mediaType,
            category: state.category,
            searchQuery: state.searchQuery,
            searchProviders: [...state.searchProviders].sort(),
            filters: sortedObject(state.filters),
        });
    }

    function rememberSnapshot(key, snapshot) {
        if (viewSnapshots.has(key)) viewSnapshots.delete(key);
        viewSnapshots.set(key, snapshot);
        while (viewSnapshots.size > VIEW_CACHE_LIMIT) {
            viewSnapshots.delete(viewSnapshots.keys().next().value);
        }
    }

    function saveCurrentViewSnapshot() {
        const hasContent = state.mode === 'sections'
            ? state.sectionsData.length > 0
            : state.itemsData.length > 0 || state.statusPayload;
        if (!hasContent) return null;
        const key = activeViewKey();
        const snapshot = {
            mode: state.mode,
            provider: state.provider,
            mediaType: state.mediaType,
            category: state.category,
            searchQuery: state.searchQuery,
            page: state.page,
            hasMore: state.hasMore,
            filters: {...state.filters},
            filterDefinitions: state.filterDefinitions,
            sectionsData: state.sectionsData,
            itemsData: state.itemsData,
            appendError: state.appendError,
            statusPayload: state.statusPayload,
            savedAt: Date.now(),
        };
        rememberSnapshot(key, snapshot);
        tabLastView.set(activeTabKey(), key);
        return snapshot;
    }

    function restoreViewSnapshot(snapshot) {
        if (!snapshot) return false;
        state.mode = snapshot.mode;
        state.provider = snapshot.provider;
        state.mediaType = snapshot.mediaType;
        state.category = snapshot.category;
        state.searchQuery = snapshot.searchQuery;
        state.page = snapshot.page;
        state.hasMore = snapshot.hasMore;
        state.filters = {...snapshot.filters};
        state.filterDefinitions = snapshot.filterDefinitions;
        state.sectionsData = snapshot.sectionsData;
        state.itemsData = snapshot.itemsData;
        state.appendError = snapshot.appendError;
        state.statusPayload = snapshot.statusPayload;
        if (state.mode === 'sections') {
            elements.filters.replaceChildren();
            elements.filters.hidden = true;
            renderSections(state.sectionsData);
        } else {
            if (state.mode === 'items') renderFilters(state.filterDefinitions);
            else {
                elements.filters.replaceChildren();
                elements.filters.hidden = true;
            }
            renderGrid(state.itemsData, false);
        }
        updateProviderStatus(state.statusPayload || {});
        connectInfiniteScroll();
        return true;
    }

    function restoreCachedView(key) {
        const snapshot = viewSnapshots.get(key);
        if (!snapshot) return null;
        restoreViewSnapshot(snapshot);
        return snapshot;
    }

    function snapshotIsFresh(snapshot) {
        if (!snapshot || Date.now() - snapshot.savedAt >= VIEW_CACHE_FRESH_MS) return false;
        if (snapshot.mode === 'sections' && asArray(snapshot.sectionsData).some((section) => section?.loading)) return false;
        return true;
    }

    function detailIdentityFromURL(value) {
        let url;
        try {
            url = new URL(value, window.location.origin);
        } catch (_error) {
            return null;
        }
        if (url.origin !== window.location.origin || url.pathname !== '/discovery') return null;
        const params = url.searchParams;
        const provider = String(params.get('detail_provider') || '').trim().toLowerCase();
        const mediaType = String(params.get('detail_type') || '').trim().toLowerCase();
        const externalId = String(params.get('detail_id') || '').trim();
        const resourceFocus = params.get('resource_focus') === '1';
        if (!provider && !mediaType && !externalId) return null;
        const validProvider = /^(?:tmdb|douban|bangumi)$/.test(provider);
        const validMediaType = /^(?:movie|tv)$/.test(mediaType);
        const validExternalId = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(externalId);
        if (!validProvider || !validMediaType || !validExternalId) return null;
        if (provider === 'bangumi' && mediaType !== 'tv') return null;
        if ((provider === 'tmdb' || provider === 'bangumi') && !/^\d{1,20}$/.test(externalId)) return null;
        return {provider, media_type: mediaType, external_id: externalId, resource_focus: resourceFocus};
    }

    function detailIdentityFromLocation() {
        return detailIdentityFromURL(window.location.href);
    }

    function detailReturnLocation() {
        const params = new URLSearchParams(window.location.search);
        const query = String(params.get('return_query') || '').trim();
        if (query) {
            if (query.length > 120 || [...query].some((char) => /[\u0000-\u001f\u007f]/.test(char))) return '';
            const search = new URLSearchParams();
            search.set('q', query);
            return `/search?${search.toString()}`;
        }
        const returnTo = String(params.get('return_to') || '').trim();
        return ['/rss#media', '/rss#watchlist'].includes(returnTo) ? returnTo : '';
    }

    function clearDetailLocation() {
        const url = new URL(window.location.href);
        let changed = false;
        ['detail_provider', 'detail_type', 'detail_id', 'return_query', 'return_to', 'resource_focus'].forEach((key) => {
            if (url.searchParams.has(key)) {
                url.searchParams.delete(key);
                changed = true;
            }
        });
        if (!changed) return;
        window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
    }

    function leaveDetailLocation() {
        if (state.detailExitInProgress) return;
        if (profileOnly) return;
        const returnTo = detailReturnLocation();
        if (!returnTo) {
            clearDetailLocation();
            return;
        }
        state.detailExitInProgress = true;
        try {
            const referrer = new URL(document.referrer || '', window.location.origin);
            if (referrer.origin === window.location.origin && referrer.pathname === '/search') {
                window.history.back();
                return;
            }
        } catch (_error) {
            // 失效或跨源 referrer 使用显式安全返回地址。
        }
        window.location.assign(returnTo);
    }

    function syncDetailCloseCopy() {
        const returnTo = detailReturnLocation();
        let label = '关闭';
        let title = '关闭';
        if (returnTo.startsWith('/search?')) {
            label = '返回搜索结果';
            title = '关闭并返回搜索结果';
        } else if (returnTo === '/rss#media') {
            label = '返回媒体追更';
            title = '关闭并返回媒体追更';
        } else if (returnTo === '/rss#watchlist') {
            label = '返回收藏清单';
            title = '关闭并返回收藏清单';
        }
        root.querySelectorAll('[data-discovery-dialog-close]').forEach((button) => {
            button.setAttribute('aria-label', label);
            button.title = title;
        });
    }

    function providerLabel(provider) {
        return PROVIDER_LABELS[String(provider || '').toLowerCase()] || String(provider || '未知来源');
    }

    function mediaLabel(mediaType) {
        return MEDIA_LABELS[String(mediaType || '').toLowerCase()] || String(mediaType || '媒体');
    }

    function safePosterUrl(value) {
        if (!value || typeof value !== 'string') return '';
        try {
            const parsed = new URL(value, window.location.origin);
            if (parsed.origin !== window.location.origin) return '';
            return `${parsed.pathname}${parsed.search}`;
        } catch (_) {
            return '';
        }
    }

    function posterTokenFromItem(item) {
        const posterUrl = safePosterUrl(item.poster_url || '');
        const prefix = `/discovery-poster/${encodeURIComponent(item.provider || '')}/`;
        if (!posterUrl.startsWith(prefix)) return '';
        const token = posterUrl.slice(prefix.length).split('?')[0];
        return token && !token.includes('/') ? token : '';
    }

    function delay(milliseconds) {
        return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    async function keepSkeletonVisible(shownAt, requestId) {
        if (!shownAt) return requestId === state.requestId;
        const remaining = SKELETON_MIN_VISIBLE_MS - (Date.now() - shownAt);
        if (remaining > 0) await delay(remaining);
        return requestId === state.requestId;
    }

    async function responseJSON(response) {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || data.detail || `请求失败 (${response.status})`);
            error.status = response.status;
            error.payload = data;
            throw error;
        }
        return data;
    }

    async function api(path, options = {}, signal) {
        const response = await fetch(path, {...options, signal});
        return responseJSON(response);
    }

    function queryString(params) {
        const search = new URLSearchParams();
        Object.entries(params).forEach(([key, value]) => {
            if (value === undefined || value === null || value === '') return;
            search.set(key, String(value));
        });
        return search.toString();
    }

    function setBusy(isBusy, preserveContent = false) {
        state.loading = isBusy;
        root.classList.toggle('is-refreshing', isBusy && preserveContent);
        elements.refresh.classList.toggle('is-loading', isBusy);
        elements.refresh.disabled = isBusy || state.loadingMore;
        elements.searchSubmit.disabled = isBusy || state.loadingMore;
        elements.refresh.setAttribute('aria-busy', String(isBusy));
        elements.searchSubmit.setAttribute('aria-busy', String(isBusy));
        elements.sections.setAttribute('aria-busy', String(isBusy));
        elements.grid.setAttribute('aria-busy', String(isBusy));
    }

    function setLoadingMore(isLoading) {
        state.loadingMore = isLoading;
        elements.loadMore.disabled = isLoading;
        elements.refresh.disabled = isLoading || state.loading;
        elements.searchSubmit.disabled = isLoading || state.loading;
        elements.loadMore.classList.toggle('is-loading', isLoading);
        const label = elements.loadMore.querySelector('span');
        if (label) label.textContent = isLoading ? '正在调取' : (state.appendError ? '重试下一页' : '调取下一卷');
    }

    function showPaginationControl() {
        const canPaginate = state.mode !== 'sections' && state.hasMore;
        const showManual = canPaginate && (state.manualPagination || Boolean(state.appendError));
        elements.loadMoreRow.hidden = !showManual;
        elements.sentinel.hidden = !canPaginate;
        const label = elements.loadMore.querySelector('span');
        if (label && !state.loadingMore) label.textContent = state.appendError ? '重试下一页' : '调取下一卷';
    }

    function disconnectInfiniteScroll() {
        if (state.observer) state.observer.disconnect();
        state.observer = null;
    }

    function connectInfiniteScroll() {
        disconnectInfiniteScroll();
        showPaginationControl();
        if (state.mode === 'sections' || !state.hasMore || state.manualPagination || state.appendError) return;
        if (typeof window.IntersectionObserver !== 'function') {
            state.manualPagination = true;
            elements.loadMoreRow.hidden = false;
            return;
        }
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((entry) => entry.isIntersecting)) loadNextPage();
        }, {root: null, rootMargin: '600px 0px', threshold: 0});
        state.observer = observer;
        observer.observe(elements.sentinel);
    }

    function skeletonCard() {
        const card = node('article', 'discovery-card discovery-skeleton');
        const poster = node('div', 'discovery-poster discovery-skeleton-block');
        const copy = node('div', 'discovery-card-copy');
        copy.append(
            node('span', 'discovery-skeleton-line discovery-skeleton-line-short'),
            node('span', 'discovery-skeleton-line'),
            node('span', 'discovery-skeleton-line discovery-skeleton-line-meta'),
        );
        card.append(poster, copy);
        return card;
    }

    function showSkeleton(mode = state.mode) {
        const target = mode === 'sections' ? elements.sections : elements.grid;
        if (target.childElementCount) return false;
        target.dataset.globalSkeleton = 'true';
        const fragment = document.createDocumentFragment();
        if (mode === 'sections') {
            for (let shelfIndex = 0; shelfIndex < 2; shelfIndex += 1) {
                const shelf = node('section', 'discovery-shelf discovery-skeleton-shelf');
                const head = node('div', 'discovery-shelf-head');
                head.append(node('span', 'discovery-skeleton-line discovery-skeleton-heading'));
                const rail = node('div', 'discovery-rail');
                for (let cardIndex = 0; cardIndex < 6; cardIndex += 1) rail.append(skeletonCard());
                shelf.append(head, rail);
                fragment.append(shelf);
            }
        } else {
            for (let index = 0; index < 12; index += 1) fragment.append(skeletonCard());
        }
        target.replaceChildren(fragment);
        return true;
    }

    function clearSkeleton(mode = state.mode) {
        const target = mode === 'sections' ? elements.sections : elements.grid;
        if (target.dataset.globalSkeleton === 'true') {
            target.replaceChildren();
            delete target.dataset.globalSkeleton;
        }
    }

    function statusBadge(text, tone = '') {
        const badge = node('span', `discovery-data-badge${tone ? ` is-${tone}` : ''}`, text);
        return badge;
    }

    function updateProviderStatus(payload = {}) {
        const health = payload.provider_health || payload.health || payload.provider || {};
        const provider = typeof health === 'string' ? health : (health.provider || state.provider);
        const status = typeof health === 'object' ? (health.status || health.state || '') : '';
        const label = provider === 'all' ? '综合排期' : providerLabel(provider);
        let summary = `${label} · 在线`;
        let tone = 'ready';
        if (status === 'not_configured' || status === 'disabled' || health.available === false) {
            summary = `${label} · 未配置`;
            tone = 'error';
        } else if (status && !['ok', 'ready', 'healthy'].includes(status)) {
            summary = `${label} · ${health.message || status}`;
            tone = 'error';
        }
        if (payload.cached) summary += ' · 缓存数据';
        if (payload.stale) summary += ' · 数据可能过期';
        elements.providerStatus.replaceChildren(icon(tone === 'ready' ? 'radio' : 'triangle-alert'));
        elements.providerStatus.setAttribute('aria-label', summary);
        elements.providerStatus.title = summary;
        elements.providerStatus.dataset.tone = tone;
        renderIcons(elements.providerStatus);
    }

    function isTmdbConfigError(error) {
        const message = String(error?.message || error?.payload?.error || error || '');
        return message.includes('TMDB_API_KEY')
            || (error?.payload?.code === 'not_configured' && (state.provider === 'tmdb' || /tmdb/i.test(message)));
    }

    function errorPanel(error, retry) {
        const panel = node('div', 'discovery-state discovery-state-error');
        const mark = node('div', 'discovery-state-mark');
        mark.append(icon('signal-zero'));
        const copy = node('div', 'discovery-state-copy');
        copy.append(node('span', 'discovery-eyebrow', 'SOURCE INTERRUPTED'), node('h3', '', '资料源暂不可用'));
        copy.append(node('p', '', error?.message || '未能读取媒体索引，请稍后重试。'));
        if (isTmdbConfigError(error)) {
            const button = node('a', 'jump-btn discovery-retry');
            button.href = '/settings#metadata';
            button.append(icon('settings-2'), node('span', '', '去配置'));
            panel.append(mark, copy, button);
        } else {
            const button = node('button', 'jump-btn discovery-retry');
            button.type = 'button';
            button.append(icon('rotate-cw'), node('span', '', '重新连接'));
            button.addEventListener('click', retry);
            panel.append(mark, copy, button);
        }
        renderIcons(panel);
        return panel;
    }

    function emptyPanel(message = '当前条件下没有可展示的媒体条目。') {
        const panel = node('div', 'discovery-state discovery-state-empty');
        const mark = node('div', 'discovery-state-mark');
        mark.append(icon('archive-x'));
        const copy = node('div', 'discovery-state-copy');
        copy.append(node('span', 'discovery-eyebrow', 'CATALOGUE EMPTY'), node('h3', '', '资料架暂空'));
        copy.append(node('p', '', message));
        panel.append(mark, copy);
        renderIcons(panel);
        return panel;
    }

    function posterElement(item) {
        const frame = node('div', 'discovery-poster');
        const posterUrl = safePosterUrl(item.poster_url || item.poster || item.image_url || '');
        if (posterUrl) {
            const image = node('img');
            image.src = posterUrl;
            image.alt = '';
            image.width = 300;
            image.height = 450;
            image.loading = 'lazy';
            image.decoding = 'async';
            image.addEventListener('error', () => {
                image.remove();
                frame.classList.add('is-missing');
            }, {once: true});
            frame.append(image);
        } else {
            frame.classList.add('is-missing');
        }
        const source = node('span', 'discovery-source-stamp', providerLabel(item.provider));
        frame.append(source);
        if (item.rating !== undefined && item.rating !== null && item.rating !== '') {
            const rating = node('span', 'discovery-rating');
            rating.append(icon('star'), node('span', '', Number(item.rating).toFixed(1)));
            frame.append(rating);
        }
        return frame;
    }

    function cardMeta(item) {
        const parts = [];
        const year = String(item.year || '').trim();
        const releaseDate = String(item.release_date || '').trim();
        const releaseYear = releaseDate.match(/^(\d{4})(?:[-/.年]|$)/)?.[1] || '';
        if (year && year !== releaseYear) parts.push(year);
        if (item.weekday) parts.push(String(item.weekday));
        if (releaseDate) parts.push(releaseDate);
        if (!parts.length) parts.push(mediaLabel(item.media_type));
        return parts.slice(0, 2).join(' / ');
    }

    function isWatchlisted(item) {
        return Boolean(item.in_watchlist || item.watchlisted || item.state === 'watchlisted');
    }

    function watchlistButton(item, card) {
        const button = node('button', 'discovery-card-action discovery-watchlist-action');
        button.type = 'button';
        button.dataset.watchlistKey = itemKey(item);
        button.setAttribute('aria-pressed', String(isWatchlisted(item)));

        function paint(active) {
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', String(active));
            button.title = active ? '移出探索收藏' : '加入探索收藏';
            button.setAttribute('aria-label', button.title);
            button.replaceChildren(icon(active ? 'bookmark-check' : 'bookmark'));
            renderIcons(button);
        }

        paint(isWatchlisted(item));
        button.addEventListener('click', async (event) => {
            event.stopPropagation();
            if (button.disabled) return;
            const active = button.getAttribute('aria-pressed') === 'true';
            button.disabled = true;
            button.classList.add('is-pending');
            try {
                if (active) {
                    const provider = encodeURIComponent(item.provider || '');
                    const mediaType = encodeURIComponent(item.media_type || '');
                    const externalId = encodeURIComponent(item.external_id || item.id || '');
                    await api(`/api/discovery/watchlist/${provider}/${mediaType}/${externalId}`, {method: 'DELETE'});
                } else {
                    await api('/api/discovery/watchlist', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            provider: item.provider,
                            media_type: item.media_type,
                            external_id: item.external_id || item.id,
                            title: item.title || item.name || '',
                            year: item.year || '',
                            poster_token: posterTokenFromItem(item),
                        }),
                    });
                }
                item.in_watchlist = !active;
                item.state = !active ? 'watchlisted' : '';
                paint(!active);
                card.classList.toggle('is-watchlisted', !active);
                announce(!active ? `已收藏《${item.title || '未命名媒体'}》` : `已移出《${item.title || '未命名媒体'}》`);
            } catch (error) {
                announce(error.message || '收藏操作失败');
                window.appAlert?.({type: 'error', title: '收藏操作失败', message: error.message || '请稍后重试。'});
            } finally {
                button.disabled = false;
                button.classList.remove('is-pending');
            }
        });
        return button;
    }

    function createCard(item, index = 0, headingTag = 'h3') {
        const card = node('article', 'discovery-card');
        card.dataset.mediaKey = itemKey(item);
        card.style.setProperty('--discovery-order', String(Math.min(index, 10)));
        card.classList.toggle('is-watchlisted', isWatchlisted(item));

        const open = node('button', 'discovery-card-open');
        open.type = 'button';
        open.setAttribute('aria-label', `查看《${item.title || '未命名媒体'}》详情`);
        open.append(posterElement(item));

        const copy = node('div', 'discovery-card-copy');
        const sourceLine = node('div', 'discovery-card-source');
        sourceLine.append(node('span', '', cardMeta(item)), node('span', '', String(item.external_id || item.id || '—')));
        const title = node(headingTag, 'discovery-card-title', item.title || item.name || '未命名媒体');
        if (item.original_title && item.original_title !== item.title) title.title = String(item.original_title);
        const footer = node('div', 'discovery-card-footer');
        const mapping = node('span', `discovery-map-state${item.tmdb_id || item.mapped_tmdb_id ? ' is-mapped' : ''}`);
        mapping.append(icon(item.tmdb_id || item.mapped_tmdb_id ? 'link' : 'unlink'));
        mapping.append(node('span', '', item.tmdb_id || item.mapped_tmdb_id ? 'TMDB 已映射' : '待映射'));
        footer.append(mapping, watchlistButton(item, card));
        copy.append(sourceLine, title, footer);
        card.append(open, copy);
        open.addEventListener('click', () => openDetail(item, card));
        return card;
    }

    function rememberNode(cache, key, entry, limit) {
        if (cache.has(key)) cache.delete(key);
        cache.set(key, entry);
        while (cache.size > limit) cache.delete(cache.keys().next().value);
    }

    function reusableCard(item, index = 0, headingTag = 'h3', scope = 'grid') {
        const key = `${scope}:${itemKey(item)}`;
        const cached = cardNodeCache.get(key);
        if (cached?.item === item && cached.headingTag === headingTag) {
            cached.node.style.setProperty('--discovery-order', String(Math.min(index, 10)));
            rememberNode(cardNodeCache, key, cached, CARD_NODE_CACHE_LIMIT);
            return cached.node;
        }
        const card = createCard(item, index, headingTag);
        rememberNode(cardNodeCache, key, {item, headingTag, node: card}, CARD_NODE_CACHE_LIMIT);
        return card;
    }

    function sectionItems(section) {
        return asArray(section.items || section.results || section.data);
    }

    function createSectionShelf(section, sectionIndex) {
        const shelf = node('section', 'discovery-shelf');
        shelf.dataset.sectionKey = section.key || section.category || `section-${sectionIndex + 1}`;
        const head = node('header', 'discovery-shelf-head');
        const heading = node('div', 'discovery-shelf-heading');
        heading.append(
            node('span', 'discovery-shelf-index', String(sectionIndex + 1).padStart(2, '0')),
            node('h3', '', section.title || section.label || section.name || '媒体栏目'),
        );
        const meta = node('div', 'discovery-shelf-meta');
        meta.append(node('span', '', providerLabel(section.provider)));
        if (section.loading) meta.append(statusBadge('LOADING', 'cache'));
        if (section.enabled === false) meta.append(statusBadge('DISABLED', 'stale'));
        if (section.cached) meta.append(statusBadge('CACHE', 'cache'));
        if (section.stale) meta.append(statusBadge('STALE', 'stale'));
        head.append(heading, meta);
        const rail = node('div', 'discovery-rail');
        const items = sectionItems(section);
        const scope = `section:${sectionIdentity(section)}`;
        if (items.length) {
            items.forEach((item, index) => rail.append(reusableCard(item, index, 'h4', scope)));
        } else if (section.loading) {
            rail.setAttribute('aria-label', `${section.title || '媒体栏目'}正在加载`);
            for (let index = 0; index < 6; index += 1) rail.append(skeletonCard());
        } else {
            const message = section.enabled === false
                ? `${providerLabel(section.provider)} 数据源未启用，可在设置中配置后使用。`
                : (section.error || '此栏目暂时没有条目。');
            rail.append(emptyPanel(message));
        }
        shelf.append(head, rail);
        return shelf;
    }

    function reusableSectionShelf(section, sectionIndex) {
        const key = `${sectionIdentity(section)}:${sectionIndex}`;
        const cached = sectionNodeCache.get(key);
        if (cached?.section === section) {
            rememberNode(sectionNodeCache, key, cached, SECTION_NODE_CACHE_LIMIT);
            return cached.node;
        }
        const shelf = createSectionShelf(section, sectionIndex);
        rememberNode(sectionNodeCache, key, {section, node: shelf}, SECTION_NODE_CACHE_LIMIT);
        return shelf;
    }

    function renderSections(sections) {
        elements.sections.hidden = false;
        elements.grid.hidden = true;
        elements.loadMoreRow.hidden = true;
        delete elements.sections.dataset.globalSkeleton;
        const fragment = document.createDocumentFragment();
        const usable = asArray(sections).filter(Boolean);
        if (!usable.length) {
            elements.sections.replaceChildren(emptyPanel('今日排期尚未生成，或所有资料源均未配置。'));
            return;
        }
        usable.forEach((section, sectionIndex) => {
            fragment.append(reusableSectionShelf(section, sectionIndex));
        });
        elements.sections.replaceChildren(fragment);
        renderIcons(elements.sections);
    }

    function mergeUniqueItems(existingItems, incomingItems) {
        const seen = new Set(asArray(existingItems).map((item) => itemKey(item)));
        const added = [];
        asArray(incomingItems).forEach((item) => {
            const key = itemKey(item);
            if (seen.has(key)) return;
            seen.add(key);
            added.push(item);
        });
        return {items: [...asArray(existingItems), ...added], added};
    }

    function renderGrid(items, append = false) {
        elements.sections.hidden = true;
        elements.grid.hidden = false;
        delete elements.grid.dataset.globalSkeleton;
        const rows = asArray(items);
        if (!append) elements.grid.replaceChildren();
        if (!rows.length && !append) {
            elements.grid.append(emptyPanel(state.mode === 'search' ? '没有找到匹配的影视资料。' : undefined));
        } else {
            const fragment = document.createDocumentFragment();
            const offset = append ? elements.grid.querySelectorAll('.discovery-card:not(.discovery-skeleton)').length : 0;
            rows.forEach((item, index) => {
                fragment.append(reusableCard(item, offset + index, 'h3', 'grid'));
            });
            elements.grid.append(fragment);
        }
        showPaginationControl();
        renderIcons(elements.grid);
    }

    function normalizeFilterDefinitions(payload, filterState = state.filters) {
        const source = payload.filters || payload.items || payload;
        const payloadDefaults = payload && !Array.isArray(payload)
            && typeof payload.defaults === 'object' ? payload.defaults : {};
        const sourceDefaults = source && !Array.isArray(source)
            && typeof source.defaults === 'object' ? source.defaults : {};
        const defaults = {...sourceDefaults, ...payloadDefaults};
        Object.entries(defaults).forEach(([key, value]) => {
            if (value !== '' && value != null && !(key in filterState)) filterState[key] = String(value);
        });
        if (Array.isArray(source)) return source;
        if (!source || typeof source !== 'object') return [];
        return Object.entries(source).filter(([key]) => key !== 'defaults').map(([key, value]) => {
            if (Array.isArray(value)) return {key, label: key, options: value};
            return {key, ...(value || {})};
        });
    }

    function optionValue(option) {
        return typeof option === 'object' ? (option.value ?? option.id ?? option.key ?? '') : option;
    }

    function optionLabel(option) {
        return typeof option === 'object' ? (option.label ?? option.name ?? option.title ?? optionValue(option)) : option;
    }

    function renderFilters(definitions) {
        elements.filters.replaceChildren();
        const usable = asArray(definitions).filter((definition) => definition && definition.key);
        elements.filters.hidden = !usable.length;
        usable.forEach((definition) => {
            const label = node('label', 'discovery-filter-control');
            label.append(node('span', 'discovery-filter-label', definition.label || definition.name || definition.key));
            const select = node('select', 'form-select');
            select.dataset.filterKey = definition.key;
            const anyOption = node('option', '', definition.all_label || '全部');
            anyOption.value = '';
            select.append(anyOption);
            asArray(definition.options || definition.values).forEach((option) => {
                const item = node('option', '', optionLabel(option));
                item.value = String(optionValue(option));
                select.append(item);
            });
            select.value = state.filters[definition.key] || '';
            label.classList.toggle('is-active', Boolean(select.value));
            select.addEventListener('change', async () => {
                if (state.loading || state.loadingMore) {
                    select.value = state.filters[definition.key] || '';
                    label.classList.toggle('is-active', Boolean(select.value));
                    return;
                }
                saveCurrentViewSnapshot();
                const previousFilters = {...state.filters};
                const previousPage = state.page;
                const previousHasMore = state.hasMore;
                state.filters[definition.key] = select.value;
                if (!select.value) delete state.filters[definition.key];
                label.classList.toggle('is-active', Boolean(select.value));
                state.page = 1;
                state.hasMore = false;
                state.appendError = null;
                elements.loadMoreRow.hidden = true;
                const cachedSnapshot = restoreCachedView(activeViewKey());
                if (cachedSnapshot && snapshotIsFresh(cachedSnapshot)) {
                    announce('已恢复最近浏览的筛选结果。');
                    return;
                }
                const loadPromise = loadActive({preserveContent: true});
                const loadRequestId = state.requestId;
                const loaded = await loadPromise;
                if (!loaded && state.requestId === loadRequestId && !cachedSnapshot) {
                    state.filters = previousFilters;
                    state.page = previousPage;
                    state.hasMore = previousHasMore;
                    renderFilters(state.filterDefinitions);
                    elements.loadMoreRow.hidden = !state.hasMore;
                }
            });
            label.append(select);
            elements.filters.append(label);
        });
    }

    function extractSections(payload) {
        if (Array.isArray(payload)) return payload;
        return asArray(payload.sections || payload.items || payload.data);
    }

    function extractItems(payload) {
        if (Array.isArray(payload)) return payload;
        return asArray(payload.items || payload.results || payload.data);
    }

    function sectionIdentity(section) {
        return [section.provider || '', section.media_type || section.mediaType || '', section.category || section.key || ''].join(':');
    }

    function mergeFailedSections(sections, previousSections) {
        const previousByKey = new Map(asArray(previousSections).map((section) => [sectionIdentity(section), section]));
        return asArray(sections).map((section) => {
            if (!section.error) return section;
            const previous = previousByKey.get(sectionIdentity(section));
            const previousItems = sectionItems(previous || {});
            if (!previousItems.length) return section;
            return {...section, items: previousItems, stale: true};
        });
    }

    function prepareSections(definitions, previousSections, preserveContent) {
        const previousByKey = new Map(asArray(previousSections).map((section) => [sectionIdentity(section), section]));
        return asArray(definitions).map((section) => {
            const hydratable = section.enabled !== false
                && section.available !== false
                && !section.error
                && Boolean(section.provider)
                && Boolean(section.media_type || section.mediaType)
                && Boolean(section.category || section.key);
            if (!hydratable || sectionItems(section).length) return {...section, loading: false};
            const previous = preserveContent ? previousByKey.get(sectionIdentity(section)) : null;
            const previousItems = sectionItems(previous || {});
            if (previousItems.length) {
                return {...section, items: previousItems, cached: previous.cached, stale: true, loading: true};
            }
            return {...section, loading: true};
        });
    }

    function replaceSection(sections, updatedSection) {
        const identity = sectionIdentity(updatedSection);
        return asArray(sections).map((section) => (
            sectionIdentity(section) === identity ? updatedSection : section
        ));
    }

    async function hydrateSection(section, signal) {
        if (!section.loading) return section;
        const provider = section.provider;
        const mediaType = section.media_type || section.mediaType;
        const category = section.category || section.key;
        try {
            const payload = await api(`/api/discovery/items?${queryString({provider, media_type: mediaType, category, page: 1})}`, {}, signal);
            return {
                ...section,
                items: extractItems(payload),
                cached: payload.cached,
                stale: payload.stale,
                health: payload.provider_health || payload.health,
                error: '',
                loading: false,
            };
        } catch (error) {
            if (error.name === 'AbortError') throw error;
            const previousItems = sectionItems(section);
            return {
                ...section,
                items: previousItems,
                stale: previousItems.length ? true : section.stale,
                error: error.message,
                loading: false,
            };
        }
    }

    async function hydrateSectionQueue(sections, signal, {concurrency = 1, onResult = null} = {}) {
        const queue = asArray(sections).filter((section) => section?.loading);
        if (!queue.length) return [];
        const results = new Array(queue.length);
        let cursor = 0;
        const worker = async () => {
            while (cursor < queue.length) {
                if (signal.aborted) {
                    const abortError = new Error('请求已取消');
                    abortError.name = 'AbortError';
                    throw abortError;
                }
                const index = cursor;
                cursor += 1;
                const updated = await hydrateSection(queue[index], signal);
                results[index] = updated;
                if (typeof onResult === 'function') onResult(updated);
            }
        };
        const workerCount = Math.min(Math.max(1, concurrency), queue.length);
        await Promise.all(Array.from({length: workerCount}, () => worker()));
        return results;
    }

    function prioritizeBackgroundSections(sections) {
        const providerRank = {bangumi: 0, douban: 1};
        return [...asArray(sections)].sort((left, right) => {
            const leftGlobal = left.category === 'tv_global_weekly' ? 1 : 0;
            const rightGlobal = right.category === 'tv_global_weekly' ? 1 : 0;
            if (leftGlobal !== rightGlobal) return leftGlobal - rightGlobal;
            return (providerRank[left.provider] ?? 2) - (providerRank[right.provider] ?? 2);
        });
    }

    async function loadSectionMode(signal) {
        const payload = await api('/api/discovery/sections', {}, signal);
        return {payload, sections: extractSections(payload)};
    }

    async function loadFilterDefinitions(signal, filterState = state.filters) {
        const cacheKey = `${state.provider}:${state.mediaType}`;
        if (filterDefinitionsCache.has(cacheKey)) {
            return normalizeFilterDefinitions(filterDefinitionsCache.get(cacheKey), filterState);
        }
        const path = `/api/discovery/filters/${encodeURIComponent(state.provider)}/${encodeURIComponent(state.mediaType)}`;
        try {
            const payload = await api(path, {}, signal);
            filterDefinitionsCache.set(cacheKey, payload);
            return normalizeFilterDefinitions(payload, filterState);
        } catch (error) {
            if (error.name === 'AbortError') throw error;
            if (error.status === 404 || error.status === 400) {
                filterDefinitionsCache.set(cacheKey, []);
                return [];
            }
            throw error;
        }
    }

    async function loadItemsMode(signal, page = 1) {
        if (state.mode === 'search') {
            const params = {
                q: state.searchQuery,
                page,
                providers: state.searchProviders.join(','),
            };
            const payload = await api(`${DISCOVERY_SEARCH_PATH}?${queryString(params)}`, {}, signal);
            return {payload, items: extractItems(payload)};
        }
        const category = state.category || FALLBACK_CATEGORIES[state.provider] || 'popular';
        const params = {
            provider: state.provider,
            media_type: state.mediaType,
            category,
            page,
            ...state.filters,
        };
        const payload = await api(`/api/discovery/items?${queryString(params)}`, {}, signal);
        return {payload, items: extractItems(payload)};
    }

    async function loadActive({preserveContent = false, append = false} = {}) {
        const requestId = state.requestId + 1;
        state.requestId = requestId;
        state.controller?.abort();
        disconnectInfiniteScroll();
        const controller = new AbortController();
        state.controller = controller;
        const hasContent = state.mode === 'sections'
            ? state.sectionsData.length > 0
            : state.itemsData.length > 0 || Boolean(elements.grid.querySelector('.discovery-card'));
        const shouldPreserve = preserveContent && hasContent;
        let skeletonShownAt = 0;
        const skeletonTimer = window.setTimeout(() => {
            if (!shouldPreserve && requestId === state.requestId) {
                if (showSkeleton(state.mode)) skeletonShownAt = Date.now();
            }
        }, SKELETON_DELAY_MS);
        setBusy(true, shouldPreserve);
        announce(shouldPreserve ? '正在更新探索内容，现有内容将保留。' : '正在读取探索内容。');

        try {
            if (state.mode === 'sections') {
                elements.filters.replaceChildren();
                elements.filters.hidden = true;
                const {payload, sections: definitions} = await loadSectionMode(controller.signal);
                if (!(await keepSkeletonVisible(skeletonShownAt, requestId))) return false;
                const preparedSections = prepareSections(definitions, state.sectionsData, shouldPreserve);
                state.sectionsData = preparedSections;
                state.hasMore = false;
                state.appendError = null;
                state.statusPayload = payload;
                renderSections(preparedSections);
                updateProviderStatus(payload);

                const applySectionResult = (updatedSection) => {
                    if (requestId !== state.requestId || controller.signal.aborted) return;
                    state.sectionsData = replaceSection(state.sectionsData, updatedSection);
                    renderSections(state.sectionsData);
                    saveCurrentViewSnapshot();
                };
                const primarySections = preparedSections.filter((section) => section.loading && section.provider === 'tmdb');
                await hydrateSectionQueue(primarySections, controller.signal, {
                    concurrency: SECTION_PRIMARY_CONCURRENCY,
                    onResult: applySectionResult,
                });
                if (requestId !== state.requestId) return false;
                saveCurrentViewSnapshot();

                const primaryKeys = new Set(primarySections.map((section) => sectionIdentity(section)));
                const backgroundSections = prioritizeBackgroundSections(
                    preparedSections.filter((section) => section.loading && !primaryKeys.has(sectionIdentity(section))),
                );
                if (backgroundSections.length) {
                    void hydrateSectionQueue(backgroundSections, controller.signal, {
                        concurrency: SECTION_BACKGROUND_CONCURRENCY,
                        onResult: applySectionResult,
                    }).then(() => {
                        if (requestId !== state.requestId || controller.signal.aborted) return;
                        saveCurrentViewSnapshot();
                        announce(`已更新 ${state.sectionsData.length} 个放映栏目。`);
                    }).catch((error) => {
                        if (error.name !== 'AbortError' && requestId === state.requestId) {
                            announce(error.message || '部分放映栏目更新失败。');
                        }
                    });
                    announce(`首屏栏目已就绪，其余 ${backgroundSections.length} 个栏目正在后台更新。`);
                } else {
                    announce(`已更新 ${state.sectionsData.length} 个放映栏目。`);
                }
            } else {
                let definitionsPromise = Promise.resolve(null);
                const requestFilters = state.filters;
                if (!append && state.mode === 'items' && !state.filterDefinitions.length) {
                    definitionsPromise = loadFilterDefinitions(controller.signal, requestFilters);
                } else if (!append && state.mode === 'search') {
                    elements.filters.replaceChildren();
                    elements.filters.hidden = true;
                }
                const itemsPromise = loadItemsMode(controller.signal, state.page);
                const [definitions, itemResult] = await Promise.all([definitionsPromise, itemsPromise]);
                if (!(await keepSkeletonVisible(skeletonShownAt, requestId))) return false;
                if (definitions) {
                    state.filterDefinitions = definitions;
                    renderFilters(definitions);
                }
                const {payload, items} = itemResult;
                state.hasMore = Boolean(payload.has_more ?? payload.hasMore);
                state.appendError = null;
                if (append) {
                    const merged = mergeUniqueItems(state.itemsData, items);
                    state.itemsData = merged.items;
                    renderGrid(merged.added, true);
                } else {
                    state.itemsData = mergeUniqueItems([], items).items;
                    renderGrid(state.itemsData, false);
                }
                if (state.mode === 'search') {
                    state.statusPayload = {health: {provider: 'all', status: payload.partial ? 'warning' : 'ready'}};
                    updateProviderStatus(state.statusPayload);
                    announce(`已读取 ${state.itemsData.length} 条影视搜索记录。`);
                } else {
                    state.statusPayload = payload;
                    updateProviderStatus(payload);
                    announce(`已读取 ${state.itemsData.length} 条${providerLabel(state.provider)}${mediaLabel(state.mediaType)}记录。`);
                }
                saveCurrentViewSnapshot();
            }
            return true;
        } catch (error) {
            if (error.name === 'AbortError') return false;
            if (!(await keepSkeletonVisible(skeletonShownAt, requestId))) return false;
            const target = state.mode === 'sections' ? elements.sections : elements.grid;
            if (append) {
                state.appendError = error;
            } else if (!shouldPreserve) {
                delete target.dataset.globalSkeleton;
                target.replaceChildren(errorPanel(error, () => loadActive()));
            }
            elements.sections.hidden = state.mode !== 'sections';
            elements.grid.hidden = state.mode === 'sections';
            updateProviderStatus({health: {provider: state.provider, status: 'error', message: append ? '下一页中断' : '连接中断'}});
            announce(error.message || '探索内容读取失败。');
            return false;
        } finally {
            window.clearTimeout(skeletonTimer);
            if (requestId === state.requestId) {
                clearSkeleton(state.mode);
                setBusy(false, false);
                connectInfiniteScroll();
            }
        }
    }

    function detailField(label, value) {
        const row = node('div', 'discovery-detail-field');
        row.append(node('dt', '', label), node('dd', '', value || '—'));
        return row;
    }

    function mappingCandidate(candidate, item, card) {
        const row = node('div', 'discovery-map-candidate');
        const copy = node('div');
        copy.append(node('strong', '', candidate.title || candidate.name || '未命名候选'));
        const meta = [candidate.year, candidate.media_type && mediaLabel(candidate.media_type), candidate.score !== undefined ? `匹配 ${Math.round(Number(candidate.score) * (Number(candidate.score) <= 1 ? 100 : 1))}%` : ''].filter(Boolean).join(' / ');
        copy.append(node('span', '', meta));
        const confirm = node('button', 'jump-btn', '确认映射');
        confirm.type = 'button';
        confirm.addEventListener('click', async () => {
            const candidateButtons = [...elements.dialogBody.querySelectorAll('.discovery-map-candidate button')];
            const mappingRequestId = state.detailRequestId;
            const signal = state.detailController?.signal;
            candidateButtons.forEach((button) => { button.disabled = true; });
            let saved = false;
            try {
                const payload = await api('/api/discovery/map', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        provider: item.provider,
                        media_type: item.media_type,
                        external_id: item.external_id || item.id,
                        title: item.title || item.name || '',
                        year: item.year || '',
                        tmdb_id: candidate.tmdb_id || candidate.id,
                        tmdb_title: candidate.title || candidate.name || '',
                        tmdb_year: candidate.year || '',
                    }),
                }, signal);
                if (mappingRequestId !== state.detailRequestId) return;
                item.tmdb_id = payload.tmdb_id || candidate.tmdb_id || candidate.id;
                const mapState = card?.querySelector('.discovery-map-state');
                mapState?.classList.add('is-mapped');
                mapState?.replaceChildren(icon('link'), node('span', '', 'TMDB 已映射'));
                renderIcons(mapState);
                announce(`已确认《${item.title || '媒体'}》的 TMDB 映射。`);
                saved = true;
                elements.dialog.close();
            } catch (error) {
                if (error.name !== 'AbortError' && mappingRequestId === state.detailRequestId) {
                    window.appAlert?.({type: 'error', title: '映射失败', message: error.message || '无法保存映射。'});
                }
            } finally {
                if (!saved) candidateButtons.forEach((button) => { button.disabled = false; });
            }
        });
        row.append(copy, confirm);
        return row;
    }

    async function loadMappingCandidates(item, detail, signal) {
        if (item.provider === 'tmdb') {
            return {tmdb_id: String(item.external_id || item.id || ''), confirmed: true, candidates: []};
        }
        return api('/api/discovery/map', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                provider: item.provider,
                media_type: item.media_type,
                external_id: item.external_id || item.id,
                title: detail.title || item.title || item.name || '',
                year: detail.year || item.year || '',
            }),
        }, signal);
    }

    function resourceSearchPayload(item, detail = {}, page = 1) {
        const title = String(detail.title || item.title || item.name || '').trim();
        const originalTitle = String(
            detail.original_title || detail.original_name || item.original_title || item.original_name || '',
        ).trim();
        const englishTitle = String(detail.english_title || item.english_title || '').trim();
        const aliases = [...asArray(detail.aliases), ...asArray(item.aliases)]
            .map((value) => String(value || '').trim())
            .filter(Boolean)
            .filter((value, index, values) => values.findIndex((candidate) => {
                return candidate.toLocaleLowerCase() === value.toLocaleLowerCase();
            }) === index)
            .slice(0, 8);
        return {
            title,
            original_title: originalTitle,
            english_title: englishTitle,
            aliases,
            year: detail.year || item.year || null,
            media_type: detail.media_type || item.media_type || '',
            sort_mode: state.resourceSort,
            page,
        };
    }

    function resourceSearchLabel(item, detail = {}) {
        const payload = resourceSearchPayload(item, detail);
        return [payload.title, payload.year].filter(Boolean).join(' · ');
    }

    function resourceMetric(label, value, iconName = '', customClass = '') {
        if (value === undefined || value === null || value === '') return null;
        const item = node('span', `discovery-resource-metric ${customClass}`.trim());
        if (iconName) item.append(icon(iconName));
        const text = label ? `${label} ${value}` : String(value);
        item.append(node('span', '', text));
        return item;
    }

    function resourcePositionLabel(result) {
        const season = resourceKnownNumber(result?.season);
        const episode = resourceKnownNumber(result?.episode);
        const episodeEnd = resourceKnownNumber(result?.episode_end);
        if (season === null && episode === null) return '';
        if (season === 0) {
            if (episode === null) return 'Specials';
            return `SP${String(episode).padStart(2, '0')}`;
        }
        const seasonText = season === null ? '' : `S${String(season).padStart(2, '0')}`;
        if (episode === null) return seasonText;
        const episodeText = `E${String(episode).padStart(2, '0')}`;
        const rangeText = episodeEnd !== null && episodeEnd > episode
            ? `-E${String(episodeEnd).padStart(2, '0')}`
            : '';
        return `${seasonText}${episodeText}${rangeText}`;
    }

    function resourcePublishedLabel(value) {
        const timestamp = Date.parse(String(value || ''));
        if (!Number.isFinite(timestamp)) return '';
        return new Intl.DateTimeFormat('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: false,
        }).format(new Date(timestamp)).replace(/\//g, '-');
    }

    function isDownloadableResult(result) {
        return Boolean(result?.result_id) && result.download_state !== 'unavailable';
    }

    function cancelResourceSearch() {
        state.resourceSearchController?.abort();
        state.resourceSearchController = null;
        state.resourceSearchRequestId += 1;
    }

    function beginResourceSearch() {
        state.resourceSearchController?.abort();
        const controller = new AbortController();
        const resourceSearchRequestId = state.resourceSearchRequestId + 1;
        state.resourceSearchRequestId = resourceSearchRequestId;
        state.resourceSearchController = controller;
        return {controller, resourceSearchRequestId};
    }

    function resourceSearchIsCurrent(resourceSearchRequestId, detailRequestId) {
        if (resourceSearchRequestId !== state.resourceSearchRequestId) return false;
        if (detailRequestId !== state.detailRequestId) return false;
        return !state.resourceSearchController?.signal.aborted;
    }

    function clearDialogNotificationTimer() {
        if (state.dialogNoticeTimer !== null) {
            window.clearTimeout(state.dialogNoticeTimer);
            state.dialogNoticeTimer = null;
        }
    }

    function hideDialogNotification() {
        clearDialogNotificationTimer();
        elements.dialogNoticeActions.replaceChildren();
        elements.dialogNoticeActions.hidden = true;
        elements.dialogNotice.hidden = true;
    }

    function showDialogNotification(notification) {
        const type = ['success', 'warning', 'error', 'info'].includes(notification.type) ? notification.type : 'info';
        const icons = {success: 'circle-check-big', warning: 'triangle-alert', error: 'circle-x', info: 'info'};
        clearDialogNotificationTimer();
        elements.dialogNotice.className = `discovery-dialog-notice is-${type}`;
        elements.dialogNotice.setAttribute('role', type === 'error' ? 'alert' : 'status');
        elements.dialogNoticeTitle.textContent = notification.title || '下载状态';
        elements.dialogNoticeText.textContent = notification.message || '';
        elements.dialogNoticeIcon.replaceChildren(icon(icons[type]));
        const actions = asArray(notification.actions).filter((action) => action?.label);
        elements.dialogNoticeActions.replaceChildren();
        actions.forEach((action) => {
            const control = node(
                action.href ? 'a' : 'button',
                `discovery-dialog-notice-action${action.primary ? ' is-primary' : ''}`,
                action.label,
            );
            if (action.href) {
                control.href = action.href;
            } else {
                control.type = 'button';
                control.addEventListener('click', () => {
                    Promise.resolve(action.onClick?.(control)).catch(() => {});
                });
            }
            elements.dialogNoticeActions.append(control);
        });
        elements.dialogNoticeActions.hidden = actions.length === 0;
        elements.dialogNotice.hidden = false;
        renderIcons(elements.dialogNotice);
        if (notification.type === 'success' && actions.length === 0) {
            state.dialogNoticeTimer = window.setTimeout(hideDialogNotification, DIALOG_NOTICE_SUCCESS_MS);
        }
    }

    function resetResourceState() {
        cancelResourceSearch();
        hideDialogNotification();
        state.selectedResourceIds.clear();
        state.resourceResults.clear();
        state.activeResourceSiteId = '';
        state.resourceSort = 'published_desc';
        state.resourceSourceOrder.clear();
        state.resourcePage = 1;
        state.resourceHasMore = false;
        state.resourceLoadingMore = false;
        state.resourceSiteStatuses = [];
        state.resourceSearchContext = null;
        state.resourceSubmitState.clear();
        state.resourceBatchBusy = false;
        state.resourceBatchSummary = '';
    }

    function resourceSelectionLimitMessage() {
        return `最多选择 ${RESOURCE_SELECTION_LIMIT} 项`;
    }

    function dialogIsOpen() {
        return Boolean(elements.dialog.open || elements.dialog.hasAttribute('open'));
    }

    function renderResourceNotice(notification) {
        const notice = elements.dialogBody.querySelector('[data-resource-notice]');
        if (!notice) return false;
        notice.className = `discovery-resource-notice is-${notification.type || 'info'}`;
        notice.setAttribute('role', notification.type === 'error' ? 'alert' : 'status');
        notice.textContent = notification.message || '';
        notice.title = notice.textContent;
        return true;
    }

    async function flushPendingResourceNotifications() {
        if (dialogIsOpen() || !state.pendingResourceNotifications.length) return;
        if (state.resourceNotificationFlushPromise) return state.resourceNotificationFlushPromise;
        const flushPromise = (async () => {
            while (state.pendingResourceNotifications.length && !dialogIsOpen()) {
                const notification = state.pendingResourceNotifications.shift();
                try {
                    await window.appAlert?.(notification);
                } catch (_) {
                    // 全局通知失败不应阻断后续 FIFO 项。
                }
            }
        })();
        state.resourceNotificationFlushPromise = flushPromise;
        try {
            await flushPromise;
        } finally {
            if (state.resourceNotificationFlushPromise === flushPromise) {
                state.resourceNotificationFlushPromise = null;
            }
            if (state.pendingResourceNotifications.length && !dialogIsOpen()) {
                window.setTimeout(flushPendingResourceNotifications, 0);
            }
        }
    }

    function resourceSubmissionContextActive(submission) {
        return submission.detailRequestId === state.detailRequestId
            && dialogIsOpen()
            && Boolean(elements.dialogBody.querySelector('[data-discovery-resource-panel]'))
            && submission.resultIds.every((resultId) => state.resourceResults.has(resultId));
    }

    function notifyResourceCompletion(notification, submission) {
        if (resourceSubmissionContextActive(submission)) {
            renderResourceNotice(notification);
            showDialogNotification(notification);
            announce(notification.message);
            return;
        }
        state.pendingResourceNotifications.push(notification);
        if (!dialogIsOpen()) window.setTimeout(flushPendingResourceNotifications, 0);
    }

    function beginResourceSubmission(detailRequestId, resultIds, target) {
        const requestId = state.resourceSubmissionRequestId + 1;
        state.resourceSubmissionRequestId = requestId;
        const submission = {
            requestId,
            detailRequestId,
            resultIds: [...resultIds],
            target,
        };
        state.resourceSubmissionRequests.set(requestId, submission);
        return submission;
    }

    function resourceResultSubmitting(resultId) {
        if (state.resourceSubmitState.get(resultId)?.status === 'submitting') return true;
        return [...state.resourceSubmissionRequests.values()].some((submission) => {
            return submission.resultIds.includes(resultId);
        });
    }

    function uniqueResourceResults(results) {
        const unique = new Map();
        asArray(results).forEach((result) => {
            const resultId = String(result?.result_id || '');
            if (resultId && !unique.has(resultId)) unique.set(resultId, result);
        });
        return [...unique.values()];
    }

    function resourceSiteKey(result) {
        return String(result?.site_id || result?.site_name || '');
    }

    function resourceKnownNumber(value) {
        if (value === null || value === undefined || value === '') return null;
        const number = Number(value);
        return Number.isFinite(number) && number >= 0 ? number : null;
    }

    function compareKnownNumbers(left, right, direction = 'desc') {
        const a = resourceKnownNumber(left);
        const b = resourceKnownNumber(right);
        if (a === null && b === null) return 0;
        if (a === null) return 1;
        if (b === null) return -1;
        return direction === 'asc' ? a - b : b - a;
    }

    function compareResourcePublished(left, right) {
        const parse = (value) => {
            const timestamp = Date.parse(String(value || ''));
            return Number.isFinite(timestamp) ? timestamp : null;
        };
        const a = parse(left);
        const b = parse(right);
        if (a === null && b === null) return 0;
        if (a === null) return 1;
        if (b === null) return -1;
        return b - a;
    }

    function compareResourceEpisode(left, right) {
        const seasonOrder = compareKnownNumbers(left.season, right.season, 'desc');
        if (seasonOrder) return seasonOrder;
        const leftEnd = left.episode_end ?? left.episode;
        const rightEnd = right.episode_end ?? right.episode;
        const episodeEndOrder = compareKnownNumbers(leftEnd, rightEnd, 'desc');
        if (episodeEndOrder) return episodeEndOrder;
        return compareKnownNumbers(left.episode, right.episode, 'desc');
    }

    function compareResourceResults(left, right) {
        let order = 0;
        if (state.resourceSort === 'relevance_desc') order = compareKnownNumbers(left.relevance_score, right.relevance_score, 'desc');
        else if (state.resourceSort === 'published_desc') order = compareResourcePublished(left.published_at, right.published_at);
        else if (state.resourceSort === 'episode_desc') order = compareResourceEpisode(left, right);
        else if (state.resourceSort === 'seeders_desc') order = compareKnownNumbers(left.seeders, right.seeders, 'desc');
        else if (state.resourceSort === 'size_desc') order = compareKnownNumbers(left.size_bytes, right.size_bytes, 'desc');
        else if (state.resourceSort === 'size_asc') order = compareKnownNumbers(left.size_bytes, right.size_bytes, 'asc');
        if (order) return order;
        return (state.resourceSourceOrder.get(left.result_id) ?? Number.MAX_SAFE_INTEGER)
            - (state.resourceSourceOrder.get(right.result_id) ?? Number.MAX_SAFE_INTEGER);
    }

    function sortedResourceResults(results = [...state.resourceResults.values()]) {
        return [...results].sort(compareResourceResults);
    }

    function visibleResourceResults() {
        const results = [...state.resourceResults.values()];
        const filtered = state.activeResourceSiteId
            ? results.filter((result) => resourceSiteKey(result) === state.activeResourceSiteId)
            : results;
        return sortedResourceResults(filtered);
    }

    function syncResourceSiteFilterControls() {
        elements.dialogBody.querySelectorAll('[data-resource-site-filter]').forEach((button) => {
            const siteId = button.dataset.resourceSiteFilter || '';
            const active = siteId === state.activeResourceSiteId;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', String(active));
        });
    }

    function renderResourceResultsList(forceReorder = false) {
        const list = elements.dialogBody?.querySelector('[data-discovery-resource-list]');
        if (!list) return;
        const existingRows = new Map(
            [...list.querySelectorAll('[data-resource-result-id]')]
                .map((row) => [row.dataset.resourceResultId || '', row]),
        );
        let rowsChanged = forceReorder || existingRows.size !== state.resourceResults.size;
        const orderedRows = sortedResourceResults().map((result) => {
            const existing = existingRows.get(result.result_id);
            if (existing && !forceReorder) return existing;
            if (!existing) rowsChanged = true;
            return existing || resourceRow(result);
        });
        if (rowsChanged && orderedRows.length) {
            list.replaceChildren(...orderedRows);
            renderIcons(list);
        }
        const rows = [...list.querySelectorAll('[data-resource-result-id]')];
        const visibleIds = new Set(visibleResourceResults().map((result) => result.result_id));
        rows.forEach((row) => {
            row.hidden = !visibleIds.has(row.dataset.resourceResultId || '');
        });
        list.querySelector('[data-resource-filter-empty]')?.remove();
        if (!state.resourceResults.size) {
            list.replaceChildren(emptyPanel('暂未找到匹配资源，可稍后重试。'));
        } else if (!visibleIds.size) {
            const empty = emptyPanel('当前站点暂无匹配资源，请切换其他站点或查看全部。');
            empty.dataset.resourceFilterEmpty = 'true';
            list.append(empty);
        }
        syncResourceSiteFilterControls();
        syncResourceHeadTitle();
    }

    async function setResourceSort(sort) {
        const next = RESOURCE_SORT_OPTIONS.some(([value]) => value === sort) ? sort : 'published_desc';
        if (next === state.resourceSort) return;
        state.resourceSort = next;
        // 先对现有结果即时重排，再重新请求第一页。服务端会按排序模式执行
        // 每站点结果截断；仅做本地排序会永久遗漏截断前未进入当前页的候选。
        renderResourceResultsList(true);
        syncResourceControls();
        const context = state.resourceSearchContext;
        if (!context) return;
        await loadResources(context.item, context.detail, context.detailRequestId, {resort: true});
    }

    function setActiveResourceSite(siteId) {
        const normalized = String(siteId || '');
        state.activeResourceSiteId = normalized === state.activeResourceSiteId ? '' : normalized;
        state.resourceBatchSummary = '';
        renderResourceResultsList();
        syncResourceControls();
    }

    function setResourceSelected(resultId, selected) {
        if (!resultId) return false;
        if (!selected) {
            state.selectedResourceIds.delete(resultId);
            return true;
        }
        if (state.selectedResourceIds.has(resultId)) return true;
        if (state.selectedResourceIds.size >= RESOURCE_SELECTION_LIMIT) {
            const message = resourceSelectionLimitMessage();
            renderResourceNotice({type: 'warning', message});
            announce(message);
            return false;
        }
        state.selectedResourceIds.add(resultId);
        return true;
    }

    function resourceStatusLabel(submitState) {
        if (!submitState) return '';
        if (submitState.status === 'submitting') return submitState.message || '正在提交资源…';
        if (submitState.status === 'request_unknown') {
            return '提交状态未知，先核对下载列表；不要直接重复提交';
        }
        if (submitState.status === 'expired') return '资源结果已过期，请刷新或重新检索';
        if (submitState.duplicate) {
            if (submitState.can_resubmit) return '已有历史任务，可重新提交';
            return submitState.error || '任务正在处理';
        }
        if (submitState.status === 'partial') {
            const partialSuccessLabel = '部分成功：';
            const partialFailedLabel = '；失败：';
            const failureReason = String(submitState.error || '').trim();
            return `${partialSuccessLabel}${asArray(submitState.succeeded).join(' + ') || '—'}`
                + `${partialFailedLabel}${asArray(submitState.failed).join(' + ') || '—'}`
                + (failureReason ? `；${failureReason}` : '')
                + `；${RESOURCE_MANUAL_REVIEW_MESSAGE}`;
        }
        if (submitState.ok) {
            const destinations = asArray(submitState.submitted_targets).length
                ? asArray(submitState.submitted_targets)
                : asArray(submitState.succeeded);
            return destinations.length ? `已提交：${destinations.join(' + ')}` : '资源提交成功';
        }
        return `${submitState.error || '资源提交失败'}；${RESOURCE_MANUAL_REVIEW_MESSAGE}`;
    }

    function downloadRequestStatusLabel(status) {
        const normalized = String(status || '').trim().toLowerCase();
        return DOWNLOAD_REQUEST_STATUS_LABELS[normalized] || '';
    }

    async function resubmitResourceRequest(resultId, item, target, label, control) {
        if (!resultId || !item?.request_id || !target) return;
        const previous = state.resourceSubmitState.get(resultId) || item;
        control.disabled = true;
        control.textContent = '提交中';
        control.setAttribute('aria-busy', 'true');
        try {
            const payload = await api(INDEXER_DOWNLOAD_RESUBMIT_PATH, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({request_id: item.request_id, target}),
            });
            const next = resourceSubmissionState(
                {...payload, result_id: resultId},
                true,
                previous,
            );
            state.resourceSubmitState.set(resultId, next);
            syncResourceControls();
            const notification = {
                type: 'success',
                title: '已重新提交',
                message: `请求 #${payload.request_id} · ${label}`,
            };
            renderResourceNotice(notification);
            showDialogNotification(notification);
            announce(notification.message);
        } catch (error) {
            const payload = error.payload || {};
            const next = resourceSubmissionState({
                ...payload,
                result_id: resultId,
                ok: false,
                error: payload.error || error.message || '重新提交失败',
            }, true, previous);
            state.resourceSubmitState.set(resultId, next);
            syncResourceControls();
            const notification = next.duplicate
                ? resourceDuplicateNotification(resultId, next, target, label)
                : {type: 'error', title: '重新提交失败', message: next.error};
            renderResourceNotice(notification);
            showDialogNotification(notification);
            announce(notification.message);
        }
    }

    function resourceDuplicateNotification(resultId, item, target, label) {
        const existingStatus = String(item?.existing_status || '').trim().toLowerCase();
        const statusLabel = downloadRequestStatusLabel(existingStatus);
        const requestId = Number.parseInt(item?.request_id || 0, 10) || 0;
        const active = ['pending', 'submitting', 'submitted', 'downloading'].includes(existingStatus);
        const canResubmit = Boolean(item?.can_resubmit && requestId);
        const message = [requestId ? `请求 #${requestId}` : '', statusLabel]
            .filter(Boolean)
            .join(' · ') || resourceStatusLabel(item);
        const actions = requestId ? [{label: '查看任务', href: '/downloads'}] : [];
        if (canResubmit) {
            const resubmitTarget = item.resubmit_target || target;
            actions.push({
                label: '重新提交',
                primary: true,
                onClick: (control) => resubmitResourceRequest(
                    resultId,
                    item,
                    resubmitTarget,
                    label,
                    control,
                ),
            });
        }
        return {
            type: 'warning',
            title: canResubmit ? '已有历史任务'
                : active ? '任务正在处理'
                    : existingStatus === 'manual_review' || existingStatus === 'outcome_unknown'
                        ? '等待核对' : '该资源已提交',
            message,
            actions,
        };
    }

    function syncDialogHeader(item = null, detail = null, isResourceMode = false) {
        const head = elements.dialog?.querySelector('.discovery-dialog-head > div');
        if (!head) return;
        head.replaceChildren();
        if (isResourceMode && (item || detail)) {
            const mediaType = detail?.media_type || item?.media_type;
            const mediaTypeLabel = mediaLabel(mediaType);
            const eyebrowText = `CATALOGUE RECORD / 媒体档案 / ${mediaTypeLabel ? `${mediaTypeLabel}资源检索` : '资源检索'}`;
            const eyebrow = node('span', 'discovery-eyebrow', eyebrowText);
            const mainTitle = detail?.title || item?.title || '未命名媒体';
            const year = detail?.year || item?.year ? ` (${detail?.year || item?.year})` : '';
            const titleHeading = node('h2', '', '');
            titleHeading.id = 'discovery-detail-title';
            titleHeading.append(icon(mediaType === 'movie' ? 'film' : 'clapperboard'), node('span', '', ` ${mainTitle}${year}`));
            head.append(eyebrow, titleHeading);
        } else {
            const titleHeading = node('h2', '', 'CATALOGUE RECORD / 媒体档案');
            titleHeading.id = 'discovery-detail-title';
            head.append(titleHeading);
        }
        renderIcons(head);
    }

    function renderResourceSubmitStatus(resultId, rowElement = null) {
        const row = rowElement || elements.dialogBody.querySelector(`[data-resource-result-id="${resultId}"]`);
        const status = row?.querySelector('.discovery-resource-item-status');
        if (!status) return;
        const submitState = state.resourceSubmitState.get(resultId);
        const statusKey = submitState
            ? `${submitState.status || (submitState.ok ? 'success' : 'error')}:${submitState.message || ''}:${submitState.error || ''}`
            : 'idle';
        if (status.dataset.renderedStatusKey === statusKey) return;
        status.dataset.renderedStatusKey = statusKey;
        status.className = 'discovery-resource-item-status';
        status.replaceChildren();
        if (!submitState) {
            status.title = '';
            status.append(icon('sparkles'), node('span', '', '等待提交'));
            renderIcons(status);
            return;
        }
        status.title = resourceStatusLabel(submitState);
        status.classList.add(`is-${submitState.status || (submitState.ok ? 'success' : 'error')}`);
        if (submitState.status === 'submitting') status.append(icon('loader-circle'));
        else if (submitState.duplicate) status.append(icon('copy-check'));
        else if (submitState.status === 'partial') status.append(icon('circle-alert'));
        else status.append(icon(submitState.ok ? 'circle-check' : 'triangle-alert'));
        status.append(node('span', '', resourceStatusLabel(submitState)));
        renderIcons(status);
    }

    function updateResourceBulkToolbar(message) {
        const toolbar = elements.dialogBody.querySelector('.discovery-resource-bulk');
        if (!toolbar) return;
        const downloadableIds = visibleResourceResults()
            .filter(isDownloadableResult)
            .map((result) => result.result_id)
            .filter((resultId) => !resourceResultTerminal(resultId))
            .slice(0, RESOURCE_SELECTION_LIMIT);
        const selectedCount = state.selectedResourceIds.size;
        const readySelectedIds = [...state.selectedResourceIds].filter((resultId) => {
            return isDownloadableResult(state.resourceResults.get(resultId))
                && !resourceResultTerminal(resultId)
                && !resourceResultSubmitting(resultId);
        });
        const allSelected = downloadableIds.length > 0
            && downloadableIds.every((resultId) => state.selectedResourceIds.has(resultId));
        const selectAll = toolbar.querySelector('[data-resource-select-all]');
        const summary = toolbar.querySelector('[data-resource-batch-summary]');
        if (selectAll) {
            selectAll.disabled = !downloadableIds.length || state.resourceBatchBusy;
            const labelText = allSelected ? '清除选择' : '全选当前页';
            const iconName = allSelected ? 'square-x' : 'check-square';
            selectAll.replaceChildren(icon(iconName), node('span', '', labelText));
            selectAll.setAttribute('aria-label', labelText);
            selectAll.title = labelText;
            renderIcons(selectAll);
        }
        toolbar.querySelectorAll('[data-resource-batch-target]').forEach((button) => {
            const target = button.dataset.resourceBatchTarget || '';
            const eligibleIds = readySelectedIds.filter((resultId) => !resourceTargetSubmitted(resultId, target));
            button.disabled = eligibleIds.length === 0 || state.resourceBatchBusy;
            button.setAttribute('aria-busy', String(state.resourceBatchBusy));
        });
        if (message !== undefined) state.resourceBatchSummary = message;
        if (summary) {
            const summaryText = state.resourceBatchSummary || `已选 ${selectedCount} 条`;
            summary.textContent = `${summaryText}，最多选择 ${RESOURCE_SELECTION_LIMIT} 项`;
            summary.title = summary.textContent;
        }
    }

    function syncResourceControls(message) {
        elements.dialogBody.querySelectorAll('[data-resource-result-id]').forEach((row) => {
            const resultId = row.dataset.resourceResultId || '';
            const result = state.resourceResults.get(resultId);
            const submitState = state.resourceSubmitState.get(resultId);
            const rowBusy = submitState?.status === 'submitting' || resourceResultSubmitting(resultId);
            const rowTerminal = resourceResultTerminal(resultId);
            const checkbox = row.querySelector('.discovery-resource-select');
            if (checkbox) {
                checkbox.checked = state.selectedResourceIds.has(resultId);
                checkbox.disabled = !isDownloadableResult(result) || state.resourceBatchBusy || rowBusy || rowTerminal;
            }
            row.querySelectorAll('.discovery-resource-action').forEach((button) => {
                const target = button.dataset.resourceSubmitTarget || '';
                button.disabled = !isDownloadableResult(result) || state.resourceBatchBusy || rowBusy
                    || resourceTargetSubmitted(resultId, target) || rowTerminal;
            });
            row.classList.toggle('is-selected', state.selectedResourceIds.has(resultId));
            row.classList.toggle('is-submitting', Boolean(rowBusy));
            renderResourceSubmitStatus(resultId, row);
        });
        updateResourceBulkToolbar(message);
    }

    function resourceBulkToolbar() {
        const toolbar = node('div', 'discovery-resource-bulk');
        const selectionActions = node('div', 'discovery-resource-selection-actions');
        const selectAll = node('button', 'jump-btn discovery-resource-selection-action discovery-resource-select-all-btn');
        selectAll.type = 'button';
        selectAll.setAttribute('data-resource-select-all', '');
        selectAll.append(icon('check-square'), node('span', '', '全选当前页'));
        selectAll.addEventListener('click', () => {
            state.resourceBatchSummary = '';
            const downloadableIds = visibleResourceResults()
                .filter(isDownloadableResult)
                .map((result) => result.result_id)
                .filter((resultId) => !resourceResultTerminal(resultId));
            const allSelected = downloadableIds.length > 0 && downloadableIds.every((id) => state.selectedResourceIds.has(id));
            if (allSelected) {
                downloadableIds.forEach((id) => state.selectedResourceIds.delete(id));
            } else {
                const missingIds = downloadableIds.filter((resultId) => !state.selectedResourceIds.has(resultId));
                const availableSlots = Math.max(0, RESOURCE_SELECTION_LIMIT - state.selectedResourceIds.size);
                missingIds.slice(0, availableSlots).forEach((resultId) => state.selectedResourceIds.add(resultId));
                if (missingIds.length > availableSlots) {
                    const message = resourceSelectionLimitMessage();
                    renderResourceNotice({type: 'warning', message});
                    announce(message);
                }
            }
            syncResourceControls();
        });

        const summary = node('span', 'discovery-resource-batch-summary', '已选 0 条，最多选择 50 项');
        summary.setAttribute('data-resource-batch-summary', '');
        summary.setAttribute('aria-live', 'polite');

        selectionActions.append(selectAll, summary);

        const batchActions = node('div', 'discovery-resource-batch-actions');
        [
            ['qb', '推送 qB', 'cloud'],
            ['guangya', '推送 光鸭', 'send'],
            ['both', '同时推送两者', 'zap'],
        ].forEach(([target, label, iconName]) => {
            const button = node('button', `jump-btn discovery-resource-batch-action discovery-resource-batch-${target}`);
            button.type = 'button';
            button.setAttribute('data-resource-batch-target', target);
            button.disabled = state.selectedResourceIds.size === 0 || state.resourceBatchBusy;
            button.append(icon(iconName), node('span', '', label));
            button.addEventListener('click', (event) => submitResourceBatch(target, event.currentTarget));
            batchActions.append(button);
        });
        toolbar.append(selectionActions, batchActions);
        return toolbar;
    }

    function configureResourceSiteTrack(region) {
        region.tabIndex = 0;
        region.setAttribute('role', 'region');
        region.setAttribute('aria-label', '站点检索状态，可使用左右方向键横向查看');
        region.addEventListener('keydown', (event) => {
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
            event.preventDefault();
            const direction = event.key === 'ArrowLeft' ? -1 : 1;
            region.scrollBy({left: direction * 180, behavior: 'auto'});
        });
        return region;
    }

    function resourceSiteStatusFrame(region, hintText = '聚焦错误站点查看完整错误详情') {
        const frame = node('div', 'discovery-resource-sites-frame');
        frame.dataset.discoveryResourceSites = 'true';
        const details = node('div', 'discovery-resource-site-details');
        const hint = node('span', 'discovery-resource-site-details-hint', hintText);
        hint.hidden = true;
        details.append(hint);
        frame.append(region, details);
        return {frame, details, hint};
    }

    function resourceSiteDiagnostic(site) {
        const details = [];
        const query = String(site?.query || '').trim();
        const attempts = Math.max(0, Number.parseInt(site?.attempts || 0, 10) || 0);
        if (query) details.push(`查询“${query}”`);
        if (attempts) details.push(`尝试 ${attempts} 次`);
        return details.length ? `，${details.join('，')}` : '';
    }

    function resourceSiteResultCount(siteId) {
        return [...state.resourceResults.values()].filter((result) => resourceSiteKey(result) === siteId).length;
    }

    function mergeResourceSiteStatuses(statuses, siteId = '') {
        if (!siteId) return asArray(statuses).map((status) => ({...status}));
        const merged = new Map(state.resourceSiteStatuses.map((status) => [String(status.site_id || ''), {...status}]));
        asArray(statuses).forEach((status) => merged.set(String(status.site_id || ''), {...status}));
        return [...merged.values()];
    }

    function retryResourceSite(siteId) {
        const context = state.resourceSearchContext;
        if (!context || !siteId) return Promise.resolve();
        return loadResources(context.item, context.detail, context.detailRequestId, {siteId, merge: true});
    }

    function resourceSiteStatuses(statuses) {
        const region = configureResourceSiteTrack(node('div', 'discovery-resource-sites'));
        const {frame, details, hint} = resourceSiteStatusFrame(region, '选择源站筛选资源；失败站点可单独重试');
        const detailMessages = new Map();
        const syncVisibleMessage = () => {
            const activeChip = region.querySelector('.discovery-resource-site-status.has-detail:focus')
                || region.querySelector('.discovery-resource-site-status.has-detail.is-hovered')
                || region.querySelector('.discovery-resource-site-status.has-detail[aria-pressed="true"]');
            let hasVisible = false;
            detailMessages.forEach((message, chip) => {
                const visible = chip === activeChip;
                message.classList.toggle('is-visible', visible);
                if (visible) hasVisible = true;
            });
            details.classList.toggle('is-visible', hasVisible);
            hint.hidden = true;
        };
        const createFilterButton = (siteId, label, accessibleLabel, status = 'success') => {
            const button = node('button', `discovery-resource-site-status is-${status} is-filter`);
            button.type = 'button';
            button.setAttribute('data-resource-site-filter', siteId);
            button.setAttribute('aria-label', accessibleLabel);
            button.setAttribute('aria-pressed', String(siteId === state.activeResourceSiteId));
            button.classList.toggle('is-active', siteId === state.activeResourceSiteId);
            button.append(node('span', '', label));
            button.addEventListener('click', () => {
                setActiveResourceSite(siteId);
                syncVisibleMessage();
            });
            return button;
        };
        const allButton = createFilterButton('', `全部 ${state.resourceResults.size}`, `显示全部 ${state.resourceResults.size} 条资源`);
        region.append(allButton);
        const labels = {success: '检索成功', fallback: '已补位', empty: '暂无结果', error: '检索失败', disabled: '未启用'};
        asArray(statuses).forEach((site) => {
            const status = Object.prototype.hasOwnProperty.call(labels, site.status) ? site.status : 'error';
            const siteId = String(site.site_id || site.id || site.site_name || '');
            const filterSiteId = status === 'fallback' && site.fallback_site_id
                ? String(site.fallback_site_id)
                : siteId;
            const statusLabel = labels[status];
            const count = resourceSiteResultCount(filterSiteId);
            const accessibleLabel = `${site.site_name || '未知站点'}：${statusLabel}`
                + (site.message ? `，${site.message}` : '')
                + resourceSiteDiagnostic(site);
            let chip;
            if (status !== 'disabled' && filterSiteId) {
                const suffix = status === 'error' ? '失败' : status === 'empty' ? '0' : String(count);
                chip = createFilterButton(
                    filterSiteId,
                    `${site.site_name || siteId} ${suffix}`,
                    `${accessibleLabel}，点击筛选该站点资源`,
                    status,
                );
            } else {
                chip = node(
                    'span',
                    `discovery-resource-site-status is-${status}`,
                    site.site_name || siteId || '未知站点',
                );
                chip.setAttribute('aria-label', accessibleLabel);
            }
            chip.title = accessibleLabel;
            if (status === 'error' || status === 'empty' || status === 'fallback') {
                chip.classList.add('has-detail');
                const message = node('div', `discovery-resource-site-message is-${status}`);
                const messageCopy = node(
                    'span',
                    '',
                    `${site.site_name || '未知站点'}：${site.message || (status === 'empty' ? '本次检索没有匹配资源' : '站点检索失败')}${resourceSiteDiagnostic(site)}`,
                );
                message.append(messageCopy);
                if (status === 'error' && site.retryable !== false && siteId) {
                    const retry = node('button', 'jump-btn discovery-resource-site-retry', `重试 ${site.site_name || siteId}`);
                    retry.type = 'button';
                    retry.setAttribute('data-resource-site-retry', siteId);
                    retry.setAttribute('aria-label', `重新检索 ${site.site_name || siteId}`);
                    retry.addEventListener('click', (event) => {
                        event.stopPropagation();
                        retry.disabled = true;
                        retry.setAttribute('aria-busy', 'true');
                        retryResourceSite(siteId).catch(() => {});
                    });
                    message.append(retry);
                }
                detailMessages.set(chip, message);
                details.append(message);
                chip.addEventListener('focus', syncVisibleMessage);
                chip.addEventListener('blur', syncVisibleMessage);
                chip.addEventListener('mouseenter', () => {
                    chip.classList.add('is-hovered');
                    syncVisibleMessage();
                });
                chip.addEventListener('mouseleave', () => {
                    chip.classList.remove('is-hovered');
                    syncVisibleMessage();
                });
            }
            if (site.message) chip.append(node('span', 'sr-only', site.message));
            region.append(chip);
        });
        syncResourceSiteFilterControls();
        syncVisibleMessage();
        return frame;
    }


    function resourceSubmissionState(item, responseReceived = true, previous = null) {
        const submittedTargets = new Set([
            ...asArray(previous?.submitted_targets),
            ...asArray(previous?.succeeded),
            ...asArray(item?.succeeded),
        ]);
        const base = {...item, submitted_targets: [...submittedTargets]};
        if (item.code === 'result_expired' || item.error === '资源结果已过期') {
            return {...base, status: 'expired'};
        }
        if (item.duplicate) return {...base, status: 'duplicate'};
        if (item.status === 'manual_review') return {...base, status: 'manual_review'};
        const hasFailedTargets = asArray(item.failed).length > 0;
        if (item.status === 'partial' || (item.ok === true && hasFailedTargets)) {
            return {...base, status: 'partial'};
        }
        if (item.ok) return {...base, status: 'success'};
        return {...base, status: responseReceived ? 'error' : 'request_unknown'};
    }

    function resourceSubmittedTargets(resultId) {
        const submitState = state.resourceSubmitState.get(resultId);
        return new Set([
            ...asArray(submitState?.submitted_targets),
            ...asArray(submitState?.succeeded),
        ]);
    }

    function resourceTargetSubmitted(resultId, target) {
        const submitted = resourceSubmittedTargets(resultId);
        if (target === 'both') return submitted.has('qb') && submitted.has('guangya');
        return submitted.has(target);
    }

    function isCompleteResourceSuccess(item) {
        return Boolean(item?.ok)
            && !item.duplicate
            && item.status !== 'partial'
            && asArray(item.failed).length === 0;
    }

    function isTerminalResourceSelection(item) {
        if (RESOURCE_TERMINAL_STATUSES.has(item?.status)) return true;
        const submitted = new Set([
            ...asArray(item?.submitted_targets),
            ...asArray(item?.succeeded),
        ]);
        return submitted.has('qb') && submitted.has('guangya');
    }

    function resourceResultTerminal(resultId) {
        return isTerminalResourceSelection(state.resourceSubmitState.get(resultId));
    }

    function normalizeResourceBatchItems(resultIds, items, responseReceived = true) {
        const requested = new Set(resultIds);
        const returned = new Map();
        asArray(items).forEach((item) => {
            const resultId = String(item?.result_id || '');
            if (!requested.has(resultId) || returned.has(resultId)) return;
            returned.set(resultId, resourceSubmissionState(
                item, responseReceived, state.resourceSubmitState.get(resultId),
            ));
        });
        return resultIds.map((resultId) => {
            return returned.get(resultId) || resourceSubmissionState({
                result_id: resultId,
                ok: false,
                duplicate: false,
                failed: [],
                error: '服务端未返回该资源的提交结果',
            }, false, state.resourceSubmitState.get(resultId));
        });
    }

    function summarizeResourceBatchItems(items) {
        const summary = {
            total: items.length,
            succeeded: 0,
            partial: 0,
            review_required: 0,
            failed: 0,
            duplicate: 0,
        };
        items.forEach((item) => {
            if (item.duplicate) {
                summary.duplicate += 1;
            } else if (isCompleteResourceSuccess(item)) {
                summary.succeeded += 1;
            } else if (item.status === 'partial') {
                summary.partial += 1;
            } else if (item.status === 'manual_review') {
                summary.review_required += 1;
            } else {
                summary.failed += 1;
            }
        });
        return summary;
    }

    async function submitResourceBatch(target, trigger = null) {
        if (state.resourceBatchBusy) return;
        const selectedResultIds = [...state.selectedResourceIds].filter((resultId) => {
            return isDownloadableResult(state.resourceResults.get(resultId))
                && !resourceTargetSubmitted(resultId, target);
        });
        const busyResultIds = selectedResultIds.filter((resultId) => {
            return resourceResultSubmitting(resultId);
        });
        const resultIds = selectedResultIds.filter((resultId) => {
            return !resourceResultSubmitting(resultId);
        }).slice(0, RESOURCE_SELECTION_LIMIT);
        if (busyResultIds.length) {
            const message = `已跳过 ${busyResultIds.length} 个正在提交的资源`;
            renderResourceNotice({type: 'warning', message});
            announce(message);
        }
        if (!resultIds.length) {
            syncResourceControls();
            return;
        }
        const targetLabel = target === 'qb' ? 'qBittorrent' : target === 'guangya' ? '光鸭' : 'qBittorrent 与光鸭';
        const confirmed = await window.appConfirm?.({
            trigger,
            kicker: '资源下载',
            title: `提交 ${resultIds.length} 条资源？`,
            message: target === 'both'
                ? '将同时提交到 qBittorrent 与光鸭。'
                : `目标：${targetLabel}`,
            confirmText: `提交到${target === 'both' ? '两者' : target === 'qb' ? ' qB' : '光鸭'}`,
            icon: 'send',
        });
        if (!confirmed) return;
        const submission = beginResourceSubmission(state.detailRequestId, resultIds, target);
        state.resourceBatchBusy = true;
        resultIds.forEach((resultId) => {
            state.resourceSubmitState.set(resultId, {
                result_id: resultId,
                status: 'submitting',
                message: `正在批量提交到 ${target === 'qb' ? 'qB' : target === 'guangya' ? '光鸭' : 'qB + 光鸭'}…`,
            });
        });
        syncResourceControls(`正在提交 0 / ${resultIds.length}`);
        try {
            const payload = await api(INDEXER_DOWNLOAD_BATCH_PATH, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({result_ids: resultIds, target}),
            });
            const items = normalizeResourceBatchItems(resultIds, payload.items, true);
            const summary = summarizeResourceBatchItems(items);
            const countsMessage = `成功 ${summary.succeeded}，部分 ${summary.partial}，待核对 ${summary.review_required}，失败 ${summary.failed}，重复 ${summary.duplicate}`;
            const requiresManualReview = summary.partial > 0
                || summary.review_required > 0
                || summary.failed > 0;
            const message = requiresManualReview
                ? `${countsMessage}；${RESOURCE_MANUAL_REVIEW_MESSAGE}`
                : countsMessage;
            const alertType = summary.partial > 0 || summary.review_required > 0 ? 'warning'
                : summary.failed > 0 ? (summary.succeeded ? 'warning' : 'error')
                    : summary.duplicate > 0 ? 'warning' : 'success';
            if (resourceSubmissionContextActive(submission)) {
                items.forEach((item) => {
                    state.resourceSubmitState.set(item.result_id, item);
                    if (isTerminalResourceSelection(item)) state.selectedResourceIds.delete(item.result_id);
                    else state.selectedResourceIds.add(item.result_id);
                });
                state.resourceBatchBusy = false;
                syncResourceControls(message);
            }
            notifyResourceCompletion({
                type: alertType,
                title: summary.partial > 0 ? '批量提交部分完成'
                    : summary.review_required > 0 ? '批量提交需要核对' : '批量提交完成',
                message,
            }, submission);
        } catch (error) {
            const items = normalizeResourceBatchItems(resultIds, resultIds.map((resultId) => {
                return {
                    result_id: resultId,
                    ok: false,
                    duplicate: false,
                    failed: [],
                    error: error.message || '批量提交请求失败',
                };
            }), false);
            const message = `提交状态未知，共 ${resultIds.length} 项；先核对下载列表，不要直接重复提交`;
            if (resourceSubmissionContextActive(submission)) {
                items.forEach((item) => {
                    state.resourceSubmitState.set(item.result_id, item);
                    state.selectedResourceIds.add(item.result_id);
                });
                state.resourceBatchBusy = false;
                syncResourceControls(message);
            }
            notifyResourceCompletion({type: 'error', title: '批量提交失败', message}, submission);
        } finally {
            state.resourceSubmissionRequests.delete(submission.requestId);
            if (resourceSubmissionContextActive(submission) && state.resourceBatchBusy) {
                state.resourceBatchBusy = false;
            }
            if (dialogIsOpen()) syncResourceControls();
        }
    }

    function resourceActionButton(result, label, target, iconName) {
        const button = node('button', 'jump-btn discovery-resource-action');
        button.type = 'button';
        button.dataset.resourceSubmitTarget = target;
        button.append(icon(iconName), node('span', '', label));
        const unavailable = !isDownloadableResult(result);
        button.disabled = unavailable;
        if (unavailable) button.title = '当前资源暂不可下载';
        button.addEventListener('click', async () => {
            if (button.disabled || !result.result_id) return;
            const wasSelected = state.selectedResourceIds.has(result.result_id);
            const submission = beginResourceSubmission(
                state.detailRequestId,
                [result.result_id],
                target,
            );
            state.resourceBatchSummary = '';
            button.disabled = true;
            button.classList.add('is-busy');
            button.setAttribute('aria-busy', 'true');
            state.resourceSubmitState.set(result.result_id, {
                result_id: result.result_id,
                status: 'submitting',
                message: `正在提交到${label}…`,
            });
            syncResourceControls();
            try {
                const payload = await api(INDEXER_DOWNLOAD_PATH, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({result_id: result.result_id, target}),
                });
                const item = resourceSubmissionState(
                    {...payload, result_id: result.result_id},
                    true,
                    state.resourceSubmitState.get(result.result_id),
                );
                const complete = isCompleteResourceSuccess(item);
                const terminalSelection = isTerminalResourceSelection(item);
                const message = complete ? `已发送到${label}。` : resourceStatusLabel(item);
                const notification = item.duplicate
                    ? resourceDuplicateNotification(result.result_id, item, target, label)
                    : {
                        type: complete ? 'success' : (item.status === 'partial' ? 'warning' : 'error'),
                        title: complete ? '已提交' : (item.status === 'partial' ? '部分完成' : '提交失败'),
                        message,
                    };
                if (resourceSubmissionContextActive(submission)) {
                    state.resourceSubmitState.set(result.result_id, item);
                    if (terminalSelection) state.selectedResourceIds.delete(result.result_id);
                    else if (wasSelected) state.selectedResourceIds.add(result.result_id);
                    else state.selectedResourceIds.delete(result.result_id);
                    button.disabled = false;
                    button.classList.remove('is-busy');
                    button.setAttribute('aria-busy', 'false');
                    syncResourceControls();
                }
                notifyResourceCompletion(notification, submission);
            } catch (error) {
                const payload = error.payload || {};
                const responseReceived = Boolean(
                    payload.status || payload.duplicate || payload.request_id
                    || payload.succeeded || payload.failed || payload.code === 'result_expired'
                );
                const item = resourceSubmissionState({
                    ...payload,
                    result_id: result.result_id,
                    ok: Boolean(payload.ok),
                    error: payload.error || error.message || '资源下载提交失败。',
                }, responseReceived, state.resourceSubmitState.get(result.result_id));
                const message = resourceStatusLabel(item);
                if (resourceSubmissionContextActive(submission)) {
                    state.resourceSubmitState.set(result.result_id, item);
                    if (isTerminalResourceSelection(item)) state.selectedResourceIds.delete(result.result_id);
                    else if (wasSelected) state.selectedResourceIds.add(result.result_id);
                    else state.selectedResourceIds.delete(result.result_id);
                    button.disabled = false;
                    button.classList.remove('is-busy');
                    button.setAttribute('aria-busy', 'false');
                    syncResourceControls();
                }
                const notification = item.duplicate
                    ? resourceDuplicateNotification(result.result_id, item, target, label)
                    : {
                        type: item.status === 'request_unknown' || item.status === 'expired' ? 'warning' : 'error',
                        title: item.status === 'expired' ? '资源已过期' : item.status === 'request_unknown' ? '状态未知' : '提交失败',
                        message,
                    };
                notifyResourceCompletion(notification, submission);
            } finally {
                state.resourceSubmissionRequests.delete(submission.requestId);
                if (dialogIsOpen()) syncResourceControls();
            }
        });
        return button;
    }


    function resourceMatchSummary(result) {
        const reasons = asArray(result.match_reasons)
            .map((reason) => RESOURCE_MATCH_REASON_LABELS[reason] || '')
            .filter(Boolean);
        return reasons.slice(0, 3).join(' · ');
    }

    function resourceRow(result) {
        const row = node('article', 'discovery-resource-row');
        row.dataset.resourceResultId = result.result_id || '';
        row.dataset.resourceSiteId = resourceSiteKey(result);
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'discovery-resource-select';
        checkbox.disabled = !isDownloadableResult(result);
        checkbox.setAttribute('aria-label', `选择资源 ${result.title || '未命名资源'}`);
        checkbox.addEventListener('change', () => {
            if (!result.result_id) return;
            state.resourceBatchSummary = '';
            setResourceSelected(result.result_id, checkbox.checked);
            syncResourceControls();
        });
        const copy = node('div', 'discovery-resource-copy');
        const badges = node('div', 'discovery-resource-badges');
        badges.append(
            node('span', 'discovery-resource-site', (result.site_name || result.site_id || '未知站点').toUpperCase()),
        );
        const position = resourcePositionLabel(result);
        if (position) {
            badges.append(node('span', 'discovery-resource-category', position));
        }
        if (Number.isFinite(Number(result.relevance_score))) {
            badges.append(node('span', 'discovery-resource-score', `${Number(result.relevance_score)} MATCH`));
        }
        if (Number(result.cluster_size || 1) > 1) {
            badges.append(node('span', 'discovery-resource-cluster', `${Number(result.cluster_size)} 个相近来源`));
        }
        const titleRow = node('div', 'discovery-resource-title-row');
        titleRow.append(node('h4', '', result.title || '未命名资源'));
        copy.append(badges, titleRow);
        const matchSummary = resourceMatchSummary(result);
        if (matchSummary) copy.append(node('p', 'discovery-resource-match', matchSummary));
        const metrics = node('div', 'discovery-resource-metrics');
        const sizeText = resourceFormattedSize(result.size_bytes) || result.size_text || (result.size_bytes ? `${result.size_bytes} B` : '');
        [
            resourceMetric('', sizeText, 'folder', 'discovery-metric-size'),
            resourceMetric('', result.seeders, 'arrow-up', 'discovery-metric-seeders'),
            resourceMetric('', result.leechers, 'arrow-down', 'discovery-metric-leechers'),
            resourceMetric('', result.downloads, 'check', 'discovery-metric-downloads'),
            resourceMetric('', resourcePublishedLabel(result.published_at), 'clock-3', 'discovery-metric-published'),
        ].filter(Boolean).forEach((metric) => metrics.append(metric));
        if (metrics.childElementCount) copy.append(metrics);
        const itemStatus = node('div', 'discovery-resource-item-status');
        itemStatus.dataset.renderedStatusKey = 'idle';
        itemStatus.setAttribute('role', 'status');
        itemStatus.setAttribute('aria-live', 'polite');
        itemStatus.append(icon('sparkles'), node('span', '', '等待提交'));
        const actions = node('div', 'discovery-resource-actions');
        const actionHead = node('div', 'discovery-resource-action-head');
        actionHead.append(node('span', 'discovery-resource-action-label', '发送至'), itemStatus);
        const actionButtons = node('div', 'discovery-resource-action-buttons');
        if (result.source_url) {
            const sourceLink = node('a', 'discovery-resource-source-link');
            sourceLink.href = result.source_url;
            sourceLink.target = '_blank';
            sourceLink.rel = 'noopener noreferrer';
            sourceLink.title = '核验源站';
            sourceLink.setAttribute('aria-label', '核验源站');
            sourceLink.append(icon('link'));
            actionButtons.append(sourceLink);
        }
        actionButtons.append(
            resourceActionButton(result, 'qBittorrent', 'qb', 'download'),
            resourceActionButton(result, '光鸭', 'guangya', 'cloud-download'),
        );
        actions.append(actionHead, actionButtons);
        row.append(checkbox, copy, actions);
        return row;
    }

    function resourceLoadingRows() {
        const fragment = document.createDocumentFragment();
        for (let index = 0; index < 4; index += 1) {
            const row = node('div', 'discovery-resource-row is-skeleton');
            row.setAttribute('aria-hidden', 'true');
            row.append(
                node('span', 'discovery-resource-skeleton-check'),
                node('span', 'discovery-resource-skeleton-copy'),
                node('span', 'discovery-resource-skeleton-actions'),
            );
            fragment.append(row);
        }
        return fragment;
    }

    function resourceInfoBanner() {
        const banner = node('div', 'discovery-resource-info-banner');
        banner.hidden = true;
        return banner;
    }

    function resourceMediaHeader(item, detail, mappingPayload = {}) {
        const banner = resourceInfoBanner();
        banner.classList.add('discovery-resource-media-header');
        banner.hidden = true;
        return banner;
    }

    function compactDetailContext(item, detail, mappingPayload = {}) {
        return resourceInfoBanner();
    }

    function activeResourceSiteName() {
        if (!state.activeResourceSiteId) return '全部';
        const site = state.resourceSiteStatuses.find((status) => {
            const siteId = String(status.site_id || status.id || status.site_name || '');
            const fallbackId = String(status.fallback_site_id || '');
            return siteId === state.activeResourceSiteId || fallbackId === state.activeResourceSiteId;
        });
        return site?.site_name || site?.name || state.activeResourceSiteId;
    }

    function syncResourceHeadTitle() {
        const headTitle = elements.dialogBody?.querySelector('.discovery-resource-head h3');
        if (!headTitle) return;
        const siteName = activeResourceSiteName();
        headTitle.replaceChildren('索引结果', node('span', 'discovery-resource-head-hint', `（${siteName}）`));
    }

    function resourcePanel(item, detail, mappingPayload = {}) {
        const panel = node('section', 'discovery-resource-panel');
        panel.dataset.discoveryResourcePanel = 'true';
        const mediaHeader = resourceMediaHeader(item, detail, mappingPayload);
        const siteTrack = configureResourceSiteTrack(node('div', 'discovery-resource-sites'));
        siteTrack.append(node('span', 'discovery-resource-site-status is-pending', '等待站点状态'));
        const {frame: sites} = resourceSiteStatusFrame(siteTrack, '选择源站筛选资源；失败站点可单独重试');
        const head = node('header', 'discovery-resource-head');
        const copy = node('div', 'discovery-resource-head-copy');
        const headTitle = node('h3', '', '索引结果');
        headTitle.append(node('span', 'discovery-resource-head-hint', '（全部）'));
        copy.append(headTitle);
        const tools = node('div', 'discovery-resource-head-tools');
        const sortLabel = node('label', 'discovery-resource-sort-label');
        sortLabel.append(node('span', '', '排序: '));
        const sort = document.createElement('select');
        sort.className = 'form-select discovery-resource-sort';
        sort.setAttribute('data-resource-sort', '');
        sort.setAttribute('aria-label', '资源排序');
        RESOURCE_SORT_OPTIONS.forEach(([value, label]) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            sort.append(option);
        });
        sort.value = state.resourceSort;
        sort.addEventListener('change', () => {
            void setResourceSort(sort.value);
        });
        sortLabel.append(sort);
        tools.append(sortLabel);
        head.append(copy, tools);
        const list = node('div', 'discovery-resource-list');
        list.dataset.discoveryResourceList = 'true';
        list.append(resourceLoadingRows());
        const notice = node('div', 'discovery-resource-notice', '等待资源操作');
        notice.setAttribute('data-resource-notice', '');
        notice.setAttribute('role', 'status');
        notice.setAttribute('aria-live', 'polite');
        notice.hidden = true;
        const pagination = node('div', 'discovery-resource-pagination');
        pagination.hidden = true;
        pagination.setAttribute('data-resource-pagination', '');
        const loadMore = node('button', 'jump-btn discovery-resource-load-more', '加载更多资源');
        loadMore.type = 'button';
        loadMore.addEventListener('click', async () => {
            if (state.resourceLoadingMore || !state.resourceHasMore) return;
            const context = state.resourceSearchContext;
            if (!context) return;
            const siteIds = state.resourceSiteStatuses
                .filter((status) => status.has_more === true && status.pagination_supported !== false)
                .map((status) => String(status.site_id || ''))
                .filter(Boolean);
            if (!siteIds.length) return;
            await loadResources(context.item, context.detail, context.detailRequestId, {
                append: true, page: state.resourcePage + 1, siteIds,
            });
        });
        pagination.append(loadMore);
        panel.append(mediaHeader, sites, head, list, pagination, resourceBulkToolbar(), notice);
        return panel;
    }

    function focusResourceWorkbench(searchSucceeded = true) {
        const panel = elements.dialogBody.querySelector('[data-discovery-resource-panel]');
        if (!panel) return;
        panel.tabIndex = -1;
        window.requestAnimationFrame(() => {
            const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
            panel.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'start'});
            panel.focus({preventScroll: true});
            announce(searchSucceeded
                ? '资源搜索完成，已定位到资源结果'
                : '已定位到资源搜索区域，请查看错误提示');
        });
    }

    async function loadResources(item, detail, detailRequestId, options = {}) {
        const panel = elements.dialogBody.querySelector('[data-discovery-resource-panel]');
        const list = panel?.querySelector('[data-discovery-resource-list]');
        const sites = elements.dialogBody.querySelector('[data-discovery-resource-sites]');
        if (!panel || !list || !sites) return false;
        const siteId = String(options.siteId || '');
        const merge = Boolean(options.merge && siteId);
        const append = Boolean(options.append);
        const resort = Boolean(options.resort);
        const requestedPage = Math.max(1, Number.parseInt(options.page || 1, 10) || 1);
        const siteIds = asArray(options.siteIds).map((value) => String(value || '')).filter(Boolean);
        const resourceSkeletonShownAt = !append && !merge && !resort ? Date.now() : 0;
        if (resourceSkeletonShownAt) list.replaceChildren(resourceLoadingRows());
        const searchPayload = resourceSearchPayload(item, detail, requestedPage);
        if (siteId) searchPayload.sites = [siteId];
        else if (siteIds.length) searchPayload.sites = siteIds;
        if (!searchPayload.title) {
            list.replaceChildren(emptyPanel('缺少可用于资源检索的标题。'));
            return false;
        }
        state.resourceSearchContext = {item, detail, detailRequestId};
        const pagination = panel.querySelector('[data-resource-pagination]');
        const loadMoreButton = pagination?.querySelector('.discovery-resource-load-more');
        if (append) {
            state.resourceLoadingMore = true;
            if (loadMoreButton) {
                loadMoreButton.disabled = true;
                loadMoreButton.textContent = '正在加载更多…';
            }
        }
        const {controller, resourceSearchRequestId} = beginResourceSearch();
        try {
            const payload = await api(
                INDEXER_SEARCH_PATH,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(searchPayload),
                },
                controller.signal,
            );
            if (!resourceSearchIsCurrent(resourceSearchRequestId, detailRequestId)) return false;
            const remainingSkeletonTime = RESOURCE_SKELETON_MIN_VISIBLE_MS - (Date.now() - resourceSkeletonShownAt);
            if (resourceSkeletonShownAt && remainingSkeletonTime > 0) await delay(remainingSkeletonTime);
            if (!resourceSearchIsCurrent(resourceSearchRequestId, detailRequestId)) return false;
            const results = uniqueResourceResults(extractItems(payload));
            if (merge) {
                [...state.resourceResults.entries()].forEach(([resultId, result]) => {
                    if (resourceSiteKey(result) !== siteId) return;
                    state.resourceResults.delete(resultId);
                    state.resourceSourceOrder.delete(resultId);
                    state.selectedResourceIds.delete(resultId);
                    state.resourceSubmitState.delete(resultId);
                });
            } else if (!append) {
                state.resourceResults.clear();
                state.resourceSourceOrder.clear();
                if (!resort) state.activeResourceSiteId = '';
                state.resourceSubmitState.clear();
                state.selectedResourceIds.clear();
            }
            state.resourceBatchSummary = '';
            const nextSourceIndex = state.resourceSourceOrder.size
                ? Math.max(...state.resourceSourceOrder.values()) + 1
                : 0;
            results.forEach((result, index) => {
                if (!result.result_id) return;
                state.resourceResults.set(result.result_id, result);
                state.resourceSourceOrder.set(result.result_id, nextSourceIndex + index);
            });
            state.resourceSiteStatuses = mergeResourceSiteStatuses(
                payload.site_statuses, merge ? siteId : append ? '__append__' : '',
            );
            state.resourcePage = append ? requestedPage : 1;
            state.resourceHasMore = Boolean(payload.has_more);
            sites.replaceWith(resourceSiteStatuses(state.resourceSiteStatuses));
            const nextPagination = panel.querySelector('[data-resource-pagination]');
            if (nextPagination) nextPagination.hidden = !state.resourceHasMore;
            list.querySelector('[data-resource-filter-empty]')?.remove();
            renderResourceResultsList(true);
            syncResourceControls();
            renderIcons(elements.dialogBody);
            return true;
        } catch (error) {
            if (error.name === 'AbortError') return false;
            const remainingSkeletonTime = RESOURCE_SKELETON_MIN_VISIBLE_MS - (Date.now() - resourceSkeletonShownAt);
            if (resourceSkeletonShownAt && remainingSkeletonTime > 0) await delay(remainingSkeletonTime);
            if (!resourceSearchIsCurrent(resourceSearchRequestId, detailRequestId)) return false;
            if (append) {
                renderResourceNotice({type: 'error', message: '更多资源加载失败，现有结果已保留'});
                return false;
            }
            if (resort) {
                renderResourceNotice({type: 'error', message: '排序刷新失败，现有结果已保留'});
                return false;
            }
            if (merge) {
                const current = state.resourceSiteStatuses.find((status) => String(status.site_id || '') === siteId) || {};
                state.resourceSiteStatuses = mergeResourceSiteStatuses([{
                    ...current,
                    site_id: siteId,
                    site_name: current.site_name || siteId,
                    status: 'error',
                    count: resourceSiteResultCount(siteId),
                    message: error.message || '站点检索失败，请稍后重试',
                    code: error.payload?.code || 'unavailable',
                    retryable: true,
                }], siteId);
                sites.replaceWith(resourceSiteStatuses(state.resourceSiteStatuses));
                renderResourceNotice({type: 'error', message: `${current.site_name || siteId} 检索失败，其他源站结果已保留`});
                renderIcons(elements.dialogBody);
                return false;
            }
            state.resourceResults.clear();
            state.resourceSourceOrder.clear();
            state.activeResourceSiteId = '';
            state.resourceSiteStatuses = [];
            sites.replaceWith(resourceSiteStatuses([]));
            const failure = node('div', 'discovery-resource-state is-error');
            failure.append(icon('triangle-alert'), node('span', '', error.message || '资源索引暂不可用'));
            const retry = node('button', 'jump-btn discovery-resource-retry', '重试资源检索');
            retry.type = 'button';
            retry.addEventListener('click', () => loadResources(item, detail, detailRequestId));
            failure.append(retry);
            list.replaceChildren(failure);
            renderIcons(panel);
            return false;
        } finally {
            if (append) {
                state.resourceLoadingMore = false;
                const currentPagination = panel.querySelector('[data-resource-pagination]');
                const currentLoadMore = currentPagination?.querySelector('.discovery-resource-load-more');
                if (currentLoadMore) {
                    currentLoadMore.disabled = false;
                    currentLoadMore.textContent = '加载更多资源';
                }
            }
            if (resourceSearchRequestId === state.resourceSearchRequestId) {
                state.resourceSearchController = null;
            }
        }
    }


    function detailMappingPanel(payload, item, detail, card, mappingPayload = {}) {
        const mapping = mappingPayload.mapping || mappingPayload || payload.mapping || detail.mapping || {};
        if (mapping.tmdb_id || item.tmdb_id || item.mapped_tmdb_id) {
            const mapped = node('section', 'discovery-mapping-panel');
            mapped.append(node('span', 'discovery-eyebrow', 'EXTERNAL LINK'), node('h3', '', `TMDB #${mapping.tmdb_id || item.tmdb_id || item.mapped_tmdb_id}`));
            mapped.append(node('p', '', mapping.confirmed ? '此映射已人工确认。' : '此条目已关联 TMDB，可用于后续资源检索。'));
            return mapped;
        }
        const candidates = asArray(mapping.candidates || payload.candidates || detail.candidates);
        const panel = node('section', 'discovery-mapping-panel');
        panel.append(node('span', 'discovery-eyebrow', 'TMDB MAPPING'), node('h3', '', candidates.length ? '候选映射待确认' : '尚未建立 TMDB 映射'));
        panel.append(node('p', '', candidates.length ? '低置信候选不会自动保存，请核对标题与年份后确认。' : '当前没有可用候选，稍后可再次检查。'));
        if (candidates.length) {
            const list = node('div', 'discovery-map-candidates');
            candidates.forEach((candidate) => list.append(mappingCandidate(candidate, item, card)));
            panel.append(list);
        }
        return panel;
    }

    function renderDetail(payload, item, card, mappingPayload = {}) {
        const detail = payload.detail || payload.item || payload;
        elements.dialogBody.replaceChildren();
        const mappingPanel = detailMappingPanel(payload, item, detail, card, mappingPayload);
        if (state.resourceResultsEnabled) {
            const panel = resourcePanel(item, detail, mappingPayload);
            syncDialogHeader(item, detail, true);
            elements.dialogBody.replaceChildren(panel);
            renderIcons(elements.dialogBody);
            return;
        }

        syncDialogHeader(item, detail, false);
        const layout = node('div', 'discovery-detail-layout');
        const poster = posterElement({...item, ...detail});
        const copy = node('div', 'discovery-detail-copy');
        copy.append(node('span', 'discovery-detail-source', `${providerLabel(item.provider)} / ${mediaLabel(item.media_type)}`));
        copy.append(node('h3', '', detail.title || item.title || '未命名媒体'));
        if (detail.original_title || item.original_title) copy.append(node('p', 'discovery-detail-original', detail.original_title || item.original_title));
        copy.append(node('p', 'discovery-detail-overview', detail.overview || detail.summary || item.overview || '暂无剧情简介。'));
        const fields = node('dl', 'discovery-detail-fields');
        fields.append(
            detailField('年份', detail.year || item.year),
            detailField('评分', detail.rating || item.rating),
            detailField('上映', detail.release_date || item.release_date),
            detailField('来源 ID', item.external_id || item.id),
        );
        copy.append(fields);
        layout.append(poster, copy);
        elements.dialogBody.append(layout, mappingPanel);
        renderIcons(elements.dialogBody);
    }


    async function openDetail(item, card) {
        state.detailExitInProgress = false;
        syncDetailCloseCopy();
        state.detailController?.abort();
        resetResourceState();
        syncDialogHeader(null, null, false);
        const controller = new AbortController();
        const detailRequestId = state.detailRequestId + 1;
        state.detailRequestId = detailRequestId;
        state.detailController = controller;
        state.activeCard = card;
        elements.dialogBody.replaceChildren();
        const loading = node('div', 'discovery-dialog-loading');
        loading.classList.toggle('is-resource-workbench', state.resourceResultsEnabled);
        loading.append(icon('loader-circle'), node('span', '', '正在调取媒体档案'));
        elements.dialogBody.append(loading);
        renderIcons(elements.dialogBody);
        document.body.classList.add('discovery-modal-open');
        if (typeof elements.dialog.showModal === 'function' && !elements.dialog.open) elements.dialog.showModal();
        else elements.dialog.setAttribute('open', '');
        try {
            const provider = encodeURIComponent(item.provider || '');
            const mediaType = encodeURIComponent(item.media_type || '');
            const externalId = encodeURIComponent(item.external_id || item.id || '');
            const payload = await api(`/api/discovery/detail/${provider}/${mediaType}/${externalId}`, {}, controller.signal);
            if (detailRequestId !== state.detailRequestId) return;
            const detail = payload.detail || payload.item || payload;
            const resolvedItem = {
                ...item,
                ...detail,
                provider: item.provider || detail.provider || '',
                media_type: item.media_type || detail.media_type || '',
                external_id: item.external_id || item.id || detail.external_id || detail.id || '',
            };
            let mapping = {};
            if (!(detail.tmdb_id || resolvedItem.tmdb_id || resolvedItem.mapped_tmdb_id)) {
                try {
                    mapping = await loadMappingCandidates(resolvedItem, detail, controller.signal);
                } catch (mappingError) {
                    if (mappingError.name === 'AbortError') return;
                    mapping = {candidates: [], error: mappingError.message};
                }
            }
            if (detailRequestId === state.detailRequestId) {
                renderDetail(payload, resolvedItem, card, mapping);
                if (state.resourceResultsEnabled) {
                    const resourceSearchSucceeded = await loadResources(
                        resolvedItem, detail, detailRequestId,
                    );
                    if (item.resource_focus && detailRequestId === state.detailRequestId) {
                        focusResourceWorkbench(Boolean(resourceSearchSucceeded));
                    }
                }
            }
        } catch (error) {
            if (error.name === 'AbortError' || detailRequestId !== state.detailRequestId) return;
            elements.dialogBody.replaceChildren(errorPanel(error, () => openDetail(item, card)));
        }
    }

    function cancelActiveDiscoveryLoad() {
        state.controller?.abort();
        state.controller = null;
        state.requestId += 1;
        disconnectInfiniteScroll();
        setBusy(false, false);
        if (state.loadingMore) setLoadingMore(false);
    }

    function activateTab(button) {
        if (button.getAttribute('aria-selected') === 'true' && state.mode !== 'search') return;
        saveCurrentViewSnapshot();
        cancelActiveDiscoveryLoad();
        elements.tabs.querySelectorAll('[role="tab"]').forEach((tab) => {
            const active = tab === button;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        state.mode = button.dataset.mode || 'items';
        state.provider = button.dataset.provider || 'all';
        state.mediaType = button.dataset.mediaType || 'all';
        state.category = button.dataset.category || FALLBACK_CATEGORIES[state.provider] || '';
        state.searchQuery = '';
        elements.stage.setAttribute('aria-labelledby', button.id);
        elements.stage.setAttribute('aria-label', '探索内容');
        root.classList.remove('is-search-mode');
        elements.sections.hidden = state.mode !== 'sections';
        elements.grid.hidden = state.mode === 'sections';
        elements.loadMoreRow.hidden = true;
        state.detailController?.abort();

        const cachedKey = tabLastView.get(activeTabKey());
        const cachedSnapshot = cachedKey ? restoreCachedView(cachedKey) : null;
        if (cachedSnapshot) {
            if (snapshotIsFresh(cachedSnapshot)) {
                announce('已恢复最近浏览内容。');
                return;
            }
            void loadActive({preserveContent: true});
            return;
        }

        state.page = 1;
        state.hasMore = false;
        state.appendError = null;
        state.filters = {};
        state.filterDefinitions = [];
        state.statusPayload = null;
        if (state.mode === 'sections') state.sectionsData = [];
        else state.itemsData = [];
        elements.filters.replaceChildren();
        elements.filters.hidden = true;
        const activeTarget = state.mode === 'sections' ? elements.sections : elements.grid;
        delete activeTarget.dataset.globalSkeleton;
        activeTarget.replaceChildren();
        void loadActive({preserveContent: false});
    }

    elements.tabs.addEventListener('click', (event) => {
        const button = event.target.closest('[role="tab"]');
        if (button) activateTab(button);
    });

    elements.tabs.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const tabs = [...elements.tabs.querySelectorAll('[role="tab"]')];
        const current = tabs.indexOf(document.activeElement);
        let next = current;
        if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
        if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabs.length - 1;
        event.preventDefault();
        tabs[next].focus();
    });

    elements.searchForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (state.loading || state.loadingMore) return;
        const query = elements.searchQuery.value.trim();
        if (!query) {
            elements.searchQuery.setCustomValidity('请输入影视名称');
            elements.searchQuery.reportValidity();
            return;
        }
        elements.searchQuery.setCustomValidity('');
        saveCurrentViewSnapshot();
        cancelActiveDiscoveryLoad();
        elements.tabs.querySelectorAll('[role="tab"]').forEach((tab) => {
            tab.classList.remove('is-active');
            tab.setAttribute('aria-selected', 'false');
        });
        state.mode = 'search';
        state.provider = 'all';
        state.mediaType = 'all';
        state.category = '';
        state.searchQuery = query;
        state.page = 1;
        state.hasMore = false;
        state.appendError = null;
        state.filters = {};
        state.filterDefinitions = [];
        elements.filters.replaceChildren();
        elements.filters.hidden = true;
        root.classList.add('is-search-mode');
        elements.stage.removeAttribute('aria-labelledby');
        elements.stage.setAttribute('aria-label', `“${query}”的搜索结果`);
        elements.sections.hidden = true;
        elements.grid.hidden = false;
        elements.loadMoreRow.hidden = true;
        const cachedSnapshot = restoreCachedView(activeViewKey());
        if (cachedSnapshot && snapshotIsFresh(cachedSnapshot)) {
            announce(`已恢复“${query}”的最近搜索结果。`);
            return;
        }
        if (!cachedSnapshot) {
            state.itemsData = [];
            state.statusPayload = null;
            delete elements.grid.dataset.globalSkeleton;
            elements.grid.replaceChildren();
        }
        await loadActive({preserveContent: Boolean(cachedSnapshot)});
    });

    elements.searchQuery.addEventListener('input', () => {
        elements.searchQuery.setCustomValidity('');
    });

    async function refreshActive() {
        if (state.loading || state.loadingMore) return false;
        saveCurrentViewSnapshot();
        const previousPage = state.page;
        const previousHasMore = state.hasMore;
        const previousAppendError = state.appendError;
        state.page = 1;
        state.appendError = null;
        const loadPromise = loadActive({preserveContent: true});
        const refreshRequestId = state.requestId;
        const loaded = await loadPromise;
        if (!loaded && state.requestId === refreshRequestId) {
            state.page = previousPage;
            state.hasMore = previousHasMore;
            state.appendError = previousAppendError;
            connectInfiniteScroll();
        }
        return loaded;
    }

    async function loadNextPage() {
        if (state.loadingMore || state.loading || !state.hasMore) return false;
        disconnectInfiniteScroll();
        const previousPage = state.page;
        state.page += 1;
        state.appendError = null;
        showPaginationControl();
        setLoadingMore(true);
        try {
            const loadPromise = loadActive({preserveContent: true, append: true});
            const loadRequestId = state.requestId;
            const loaded = await loadPromise;
            if (!loaded && state.requestId === loadRequestId) {
                state.page = previousPage;
                if (!state.appendError) state.appendError = new Error('下一页读取失败');
            }
            return loaded;
        } finally {
            setLoadingMore(false);
            connectInfiniteScroll();
        }
    }

    elements.refresh.addEventListener('click', refreshActive);
    elements.loadMore.addEventListener('click', loadNextPage);

    function restoreDetailFocus() {
        const target = state.activeCard?.querySelector?.('.discovery-card-open') || state.activeCard || elements.searchQuery;
        state.activeCard = null;
        window.requestAnimationFrame(() => target?.focus?.({preventScroll: true}));
    }

    function closeDetailDialog() {
        document.body.classList.remove('discovery-modal-open');
        state.detailController?.abort();
        state.detailRequestId += 1;
        resetResourceState();
        syncDialogHeader(null, null, false);
        if (typeof elements.dialog.close === 'function' && elements.dialog.open) {
            elements.dialog.close();
        } else {
            elements.dialog.removeAttribute('open');
            leaveDetailLocation();
            restoreDetailFocus();
        }
        window.setTimeout(flushPendingResourceNotifications, 0);
    }

    root.querySelectorAll('[data-discovery-dialog-close]').forEach((button) => {
        button.addEventListener('click', closeDetailDialog);
    });
    elements.dialogNoticeClose.addEventListener('click', hideDialogNotification);
    elements.dialog.addEventListener('click', (event) => {
        if (event.target === elements.dialog) closeDetailDialog();
    });
    elements.dialog.addEventListener('close', () => {
        document.body.classList.remove('discovery-modal-open');
        state.detailController?.abort();
        state.detailRequestId += 1;
        resetResourceState();
        syncDialogHeader(null, null, false);
        leaveDetailLocation();
        restoreDetailFocus();
        window.setTimeout(flushPendingResourceNotifications, 0);
    });

    if (profileOnly) {
        document.addEventListener('click', (event) => {
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            const link = event.target.closest?.('a[data-media-profile-link]');
            if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
            const identity = detailIdentityFromURL(link.href);
            if (!identity) return;
            event.preventDefault();
            void openDetail(identity, link);
        });
    }

    const initialDetail = detailIdentityFromLocation();
    if (!profileOnly) loadActive();
    if (initialDetail) openDetail(initialDetail, null);
})();
