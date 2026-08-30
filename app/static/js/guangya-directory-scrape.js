(function () {
    const modal = document.getElementById('gyScrapeModal');
    const menu = document.getElementById('gyDirectoryContextMenu');
    if (!modal || !menu) return;
    document.body.appendChild(menu);
    document.body.appendChild(modal);

    const elements = {
        close: document.getElementById('gyScrapeCloseBtn'),
        cancel: document.getElementById('gyScrapeCancelBtn'),
        run: document.getElementById('gyScrapeRunBtn'),
        search: document.getElementById('gyScrapeSearchBtn'),
        externalBtn: document.getElementById('gyScrapeExternalBtn'),
        externalHints: document.getElementById('gyScrapeExternalHints'),
        cleanBtn: document.getElementById('gyScrapeCleanBtn'),
        query: document.getElementById('gyScrapeQuery'),
        type: document.getElementById('gyScrapeType'),
        episodeFields: document.getElementById('gyScrapeEpisodeFields'),
        numberingField: document.getElementById('gyScrapeNumberingField'),
        numbering: document.getElementById('gyScrapeNumbering'),
        seasonField: document.getElementById('gyScrapeSeasonField'),
        episodeField: document.getElementById('gyScrapeEpisodeField'),
        season: document.getElementById('gyScrapeSeason'),
        episode: document.getElementById('gyScrapeEpisode'),
        directory: document.getElementById('gyScrapeDirectory'),
        inspectionBar: document.getElementById('gyScrapeInspection'),
        inspectionSummary: document.getElementById('gyScrapeInspectionSummary'),
        inspectionHint: document.getElementById('gyScrapeInspectionHint'),
        candidates: document.getElementById('gyScrapeCandidates'),
        candidateCount: document.getElementById('gyScrapeCandidateCount'),
        detail: document.getElementById('gyScrapeDetail'),
        status: document.getElementById('gyScrapeStatus'),
        archiveTarget: document.getElementById('gyScrapeArchiveTarget'),
        planSummary: document.getElementById('gyScrapePlanSummary'),
    };
    const state = {
        activeItem: null,
        activeRow: null,
        activeAction: null,
        parentId: '',
        inspection: null,
        selectedCandidate: null,
        preview: null,
        menuReturnFocus: null,
        menuOpenedAt: 0,
        requestVersion: 0,
        searchController: null,
        previewController: null,
        externalController: null,
        pendingSearchKey: '',
        pendingPreviewKey: '',
        pendingExternalKey: '',
    };
    const modalLifecycle = window.createAppModal(modal, {
        onRequestClose: closeModal,
    });
    const positionControls = window.MediaScrapePosition.create({
        root: modal,
        isSingleFile: () => isSingleFileScope(),
        elements: {
            fields: elements.episodeFields, seasonField: elements.seasonField,
            episodeField: elements.episodeField, season: elements.season,
            episode: elements.episode, numbering: elements.numbering,
        },
    });

    function node(tag, className = '', text = '') {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== '') element.textContent = text;
        return element;
    }

    function icon(name, className = '') {
        const element = document.createElement('i');
        element.setAttribute('data-lucide', name);
        if (className) element.className = className;
        return element;
    }

    function renderIcons(root) {
        window.renderLucideIcons?.(root || document);
    }

    function archiveTargetLabel(target) {
        const value = String(target || '').trim() || '归档目标未配置';
        return `将整理至：${value}`;
    }

    async function api(path, payload, {signal} = {}) {
        const response = await fetch(`/api/guangya/directory-scrape${path}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload || {}),
            signal,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '目录刮削请求失败');
        return data;
    }

    function setButtonContent(button, iconName, label, spinning = false) {
        const glyph = icon(iconName, spinning ? 'is-spinning' : '');
        const text = node('span', '', label);
        button.replaceChildren(glyph, text);
        renderIcons(button);
    }

    function setRowState(row, action, value, message = '') {
        if (!row || !action) return;
        action.dataset.state = value;
        const defaultLabels = {
            loading: '检查中',
            deleting: '删除中',
            queued: '排队中',
            running: '整理中',
            done: '已完成',
            error: '失败',
        };
        action.title = message || defaultLabels[value] || '操作';
        if (value === 'loading') setButtonContent(action, 'loader-circle', '检查中', true);
        else if (value === 'deleting') setButtonContent(action, 'loader-circle', '删除中', true);
        else if (value === 'queued') setButtonContent(action, 'clock-3', '排队中');
        else if (value === 'running') setButtonContent(action, 'loader-circle', '整理中', true);
        else if (value === 'done') setButtonContent(action, 'circle-check', '已完成');
        else if (value === 'error') setButtonContent(action, 'triangle-alert', '失败');
        else setButtonContent(action, 'ellipsis', '操作');
    }

    function closeMenu({restoreFocus = false} = {}) {
        if (menu.hidden) return;
        menu.hidden = true;
        state.activeAction?.setAttribute('aria-expanded', 'false');
        if (restoreFocus) state.menuReturnFocus?.focus({preventScroll: true});
        state.menuReturnFocus = null;
        state.menuOpenedAt = 0;
    }

    function closeMenuAfterViewportChange(event) {
        // 聚焦或显示刚打开的菜单时，部分 Chromium/WebView 会派发一次可信的
        // scroll/resize。仅忽略这种浏览器自身事件；程序主动派发的事件仍应关闭菜单。
        const isInitialBrowserEvent = event?.isTrusted
            && !menu.hidden
            && performance.now() - state.menuOpenedAt < 160;
        if (isInitialBrowserEvent) return;
        closeMenu();
    }

    function viewportBounds() {
        const viewport = window.visualViewport;
        return viewport
            ? {
                left: viewport.offsetLeft,
                top: viewport.offsetTop,
                right: viewport.offsetLeft + viewport.width,
                bottom: viewport.offsetTop + viewport.height,
            }
            : {left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight};
    }

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(value, maximum));
    }

    function positionMenu(event, anchorToButton) {
        menu.hidden = false;
        const menuRect = menu.getBoundingClientRect();
        const viewport = viewportBounds();
        const padding = 8;
        let x = event.clientX;
        let y = event.clientY;
        if (anchorToButton) {
            const anchor = event.currentTarget.getBoundingClientRect();
            x = anchor.right - menuRect.width;
            y = anchor.bottom + 6;
            if (y + menuRect.height > viewport.bottom - padding) {
                y = anchor.top - menuRect.height - 6;
            }
        } else {
            if (x + menuRect.width > viewport.right - padding) x -= menuRect.width;
            if (y + menuRect.height > viewport.bottom - padding) y -= menuRect.height;
        }
        menu.style.left = `${clamp(
            x,
            viewport.left + padding,
            viewport.right - menuRect.width - padding,
        )}px`;
        menu.style.top = `${clamp(
            y,
            viewport.top + padding,
            viewport.bottom - menuRect.height - padding,
        )}px`;
    }

    function configureMenu(item) {
        const canScrape = Boolean(item.is_dir || item.is_video === true);
        menu.querySelectorAll('[data-scrape-action]').forEach((button) => {
            button.hidden = !canScrape;
        });
        const deleteItemButton = menu.querySelector('[data-browser-action="delete-item"]');
        if (!deleteItemButton) return;
        deleteItemButton.hidden = false;
        const isDirectory = item.is_dir === true;
        const title = deleteItemButton.querySelector('[data-delete-title]');
        const hint = deleteItemButton.querySelector('[data-delete-hint]');
        if (title) title.textContent = isDirectory ? '删除整个目录' : '删除文件';
        if (hint) hint.textContent = isDirectory
            ? '目录及内容将进入光鸭回收站'
            : '可在光鸭回收站恢复';
    }

    function openMenu(item, row, action, event, anchorToButton) {
        event.preventDefault();
        event.stopPropagation();
        if (action.disabled) return;
        closeMenu();
        state.activeItem = item;
        state.activeRow = row;
        state.activeAction = action;
        state.parentId = window.gyNavigator?.state?.().id || '';
        state.menuReturnFocus = action;
        action.setAttribute('aria-expanded', 'true');
        configureMenu(item);
        state.menuOpenedAt = performance.now();
        positionMenu(event, anchorToButton);
        menu.querySelector('[role="menuitem"]:not([hidden])')?.focus({preventScroll: true});
    }

    function bindMediaRow(row, item, action) {
        row.dataset.mediaId = String(item.file_id);
        action.addEventListener('click', (event) => openMenu(
            item, row, action, event, true,
        ));
        row.addEventListener('contextmenu', (event) => openMenu(
            item, row, action, event, false,
        ));
    }

    function placeholder(target, iconName, title, copy) {
        const box = node('div', 'gy-scrape-placeholder');
        box.append(icon(iconName), node('strong', '', title), node('span', '', copy));
        target.replaceChildren(box);
        renderIcons(target);
    }

    function openModal() {
        modalLifecycle.open(state.activeAction, {initialFocus: elements.query});
    }

    function closeModal() {
        state.requestVersion += 1;
        state.searchController?.abort();
        state.previewController?.abort();
        state.externalController?.abort();
        state.searchController = null;
        state.previewController = null;
        state.externalController = null;
        state.pendingSearchKey = '';
        state.pendingPreviewKey = '';
        state.pendingExternalKey = '';
        modalLifecycle.close();
        elements.candidates.classList.remove('is-loading');
    }

    function isSingleFileScope() {
        return Boolean(state.activeDirectory && !state.activeDirectory.is_dir);
    }

    function isNsfwOnly() {
        return Boolean(state.inspection?.nsfw_only);
    }

    function syncRecognitionMode() {
        const nsfwOnly = isNsfwOnly();
        modal.toggleAttribute('data-nsfw-only', nsfwOnly);
        const queryLabel = elements.query.closest('label')?.querySelector('span');
        const typeLabel = elements.type.closest('label')?.querySelector('span');
        const candidateTitle = document.getElementById('gyScrapeCandidateTitle');
        const candidateTab = modal.querySelector('[data-scrape-mobile-pane="candidates"] span');

        if (nsfwOnly) elements.type.value = 'movie';
        elements.type.disabled = nsfwOnly;
        elements.query.placeholder = nsfwOnly ? '输入番号，或保留包含番号的文件名' : '输入电影或剧集名称';
        if (queryLabel) queryLabel.textContent = nsfwOnly ? '番号或文件名' : '搜索名称';
        if (typeLabel) typeLabel.textContent = nsfwOnly ? '归档结构' : '媒体类型';
        if (candidateTitle) candidateTitle.textContent = nsfwOnly ? 'MetaTube 精确候选' : '媒体候选';
        if (candidateTab) candidateTab.textContent = nsfwOnly ? '番号候选' : '媒体候选';
        if (elements.cleanBtn) elements.cleanBtn.hidden = nsfwOnly;
        if (elements.externalBtn) elements.externalBtn.hidden = nsfwOnly;
        setButtonContent(elements.search, 'search', nsfwOnly ? '识别番号' : '搜索');
    }

    function syncEpisodeFields(mediaType = elements.type.value) {
        positionControls.sync(mediaType);
    }

    function scrapeDialog() {
        return modal.querySelector('.gy-scrape-dialog');
    }

    function setMobilePane(pane) {
        const dialog = scrapeDialog();
        if (!dialog) return;
        const normalized = pane === 'preview' ? 'preview' : 'candidates';
        dialog.dataset.mobilePane = normalized;
        modal.querySelectorAll('[data-scrape-mobile-pane]').forEach((button) => {
            const active = button.dataset.scrapeMobilePane === normalized;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    function setPlanReady(ready) {
        scrapeDialog()?.classList.toggle('has-plan', Boolean(ready));
    }

    function episodePreviewPayload(mediaType) {
        return positionControls.payload(mediaType);
    }

    function invalidatePreview() {
        state.requestVersion += 1;
        state.previewController?.abort();
        state.previewController = null;
        state.pendingPreviewKey = '';
        state.preview = null;
        setPlanReady(false);
        elements.run.disabled = true;
        elements.status.textContent = '搜索条件已更新';
        elements.planSummary.textContent = '请重新选择候选并生成预览';
    }

    function pendingVideos(source = state.inspection) {
        return Array.isArray(source?.pending_videos) ? source.pending_videos : [];
    }

    function renderInspectionSummary(inspection) {
        if (!elements.inspectionBar || !elements.inspectionSummary || !elements.inspectionHint) return;
        const counts = inspection?.counts || {};
        const pending = pendingVideos(inspection);
        const eligible = Number(counts.video || 0);
        elements.inspectionBar.dataset.state = pending.length ? 'pending' : 'ready';
        elements.inspectionSummary.textContent = pending.length
            ? `${eligible} 可刮削 · ${pending.length} 待确认`
            : `${eligible} 个视频已确认`;
        elements.inspectionHint.textContent = pending.length
            ? '待确认文件不会移动'
            : '可生成命名预览';
        const summary = elements.inspectionSummary.textContent;
        const hint = elements.inspectionHint.textContent;
        elements.inspectionBar.title = pending.length
            ? `${summary} · ${hint}\n${pending.map((item) => `${item.name}：${item.reason || '无法确认媒体归属'}`).join('\n')}`
            : `${summary} · ${hint}`;
    }

    function resetModal(inspection) {
        state.inspection = inspection;
        state.selectedCandidate = null;
        state.preview = null;
        setPlanReady(false);
        setMobilePane('candidates');
        elements.directory.textContent = `${inspection.directory.name} · ${inspection.counts.video} 个视频`;
        renderInspectionSummary(inspection);
        elements.query.value = inspection.suggested_query || inspection.directory.name || '';
        elements.type.value = ['movie', 'tv'].includes(inspection.media_type)
            ? inspection.media_type
            : 'auto';
        if (elements.numbering) elements.numbering.value = 'auto';
        const inspectionSeason = inspection.season === null || inspection.season === undefined
            || inspection.season === '' ? null : Number(inspection.season);
        const inspectionEpisode = inspection.episode === null || inspection.episode === undefined
            || inspection.episode === '' ? null : Number(inspection.episode);
        elements.season.value = Number.isInteger(inspectionSeason)
            && inspectionSeason >= 0 && inspectionSeason <= 99
            ? String(inspectionSeason)
            : '';
        elements.episode.value = Number.isInteger(inspectionEpisode)
            && inspectionEpisode >= 1 && inspectionEpisode <= 999
            ? String(inspectionEpisode)
            : '';
        positionControls.markClean();
        syncEpisodeFields();
        elements.archiveTarget.textContent = archiveTargetLabel(
            inspection.archive_target.name || inspection.archive_target.id,
        );
        elements.planSummary.textContent = '确认前不会写入云盘';
        elements.status.textContent = '尚未选择';
        elements.run.disabled = true;
        elements.run.querySelector('span').textContent = '确认并开始刮削';
        elements.search.disabled = false;
        elements.search.querySelector('span').textContent = '搜索';
        state.externalController?.abort();
        state.externalController = null;
        state.pendingExternalKey = '';
        elements.externalBtn.disabled = false;
        setButtonContent(elements.externalBtn, 'book-open-check', '豆瓣 / BGM 线索');
        elements.externalHints.hidden = true;
        elements.externalHints.replaceChildren();
        syncRecognitionMode();
        elements.candidateCount.textContent = '0 项';
        placeholder(
            elements.candidates,
            isNsfwOnly() ? 'shield-check' : 'scan-search',
            isNsfwOnly() ? '正在准备番号识别' : '正在准备媒体搜索',
            isNsfwOnly()
                ? '只会查询 MetaTube 的完全一致番号，不会调用或回退 TMDB。'
                : '搜索结果会保留在这里，切换候选不会改变弹窗尺寸。',
        );
        placeholder(
            elements.detail,
            'clapperboard',
            isNsfwOnly() ? '选择精确番号候选' : '选择一个媒体候选',
            isNsfwOnly()
                ? '确认 MetaTube 身份后，将逐项核对源文件与成人归档分类目录。'
                : '选择候选后，将逐项核对源文件、季集映射和最终归档名称。',
        );
    }

    async function inspectDirectory(item, row, action) {
        setRowState(row, action, 'loading', '正在检查所选媒体');
        const payload = item.is_dir
            ? {directory_id: item.file_id}
            : {file_id: item.file_id};
        try {
            return await api('/inspect', payload);
        } catch (error) {
            setRowState(row, action, 'error', error.message);
            window.appAlert?.({
                type: 'error',
                title: '媒体检查失败',
                message: error.message,
            });
            throw error;
        }
    }

    function poster(url, className) {
        const box = node('div', className);
        if (url) {
            const image = document.createElement('img');
            image.src = url;
            image.alt = '';
            image.width = className.includes('candidate') ? 46 : 64;
            image.height = className.includes('candidate') ? 69 : 96;
            image.loading = 'lazy';
            image.addEventListener('error', () => {
                box.replaceChildren(icon('image-off'));
                renderIcons(box);
            });
            box.appendChild(image);
        } else {
            box.appendChild(icon('image-off'));
        }
        return box;
    }

    function candidateMeta(candidate) {
        const provider = String(candidate.provider || 'tmdb').toLowerCase();
        const identity = provider === 'metatube'
            ? `MetaTube ${candidate.number || candidate.external_id || ''}`.trim()
            : `TMDB ${candidate.tmdb_id || candidate.external_id || ''}`.trim();
        return [
            candidate.media_type === 'tv' ? '剧集' : '电影',
            candidate.year || '年份未知',
            identity,
        ].join(' · ');
    }

    function candidateFallbackTitle(candidate) {
        const provider = String(candidate.provider || 'tmdb').toLowerCase();
        if (provider === 'metatube') {
            return candidate.number || candidate.external_id || 'MetaTube 媒体';
        }
        return `TMDB ${candidate.tmdb_id || candidate.external_id || ''}`.trim();
    }

    function renderCandidates(candidates) {
        elements.candidates.replaceChildren();
        elements.candidateCount.textContent = `${candidates.length} 项`;
        if (!candidates.length) {
            placeholder(
                elements.candidates,
                'search-x',
                isNsfwOnly() ? '没有找到完全一致的番号' : '没有找到候选',
                isNsfwOnly()
                    ? '请检查文件名中的番号；专用来源不会改走 TMDB 或采用相似结果。'
                    : '可以修改搜索名称或切换电影/剧集类型后重试。',
            );
            return;
        }
        candidates.forEach((candidate) => {
            const button = node('button', 'gy-scrape-candidate');
            button.type = 'button';
            button.setAttribute('aria-pressed', 'false');
            button.appendChild(poster(candidate.poster_url, 'gy-scrape-candidate-poster'));
            const copy = node('span', 'gy-scrape-candidate-copy');
            copy.append(
                node('strong', '', candidate.title || candidateFallbackTitle(candidate)),
                node('span', '', candidate.original_title || candidateMeta(candidate)),
                node('span', '', candidateMeta(candidate)),
            );
            const score = node(
                'span',
                'gy-scrape-score',
                `${Math.round(Number(candidate.score || 0) * 100)}%`,
            );
            const candidateState = node('span', 'gy-scrape-candidate-state');
            candidateState.setAttribute('aria-hidden', 'true');
            candidateState.append(
                icon('chevron-right', 'gy-scrape-candidate-arrow'),
                icon('check', 'gy-scrape-candidate-check'),
            );
            button.append(copy, score, candidateState);
            button.addEventListener('click', () => selectCandidate(candidate, button));
            elements.candidates.appendChild(button);
        });
        renderIcons(elements.candidates);
    }

    function externalProviderLabel(provider) {
        if (provider === 'douban') return '豆瓣';
        if (provider === 'bangumi') return 'Bangumi';
        return String(provider || '外部资料');
    }

    function renderExternalHints(data) {
        const items = Array.isArray(data.items) ? data.items : [];
        const errors = Array.isArray(data.errors) ? data.errors : [];
        elements.externalHints.hidden = false;
        elements.externalHints.replaceChildren();

        const head = node('div', 'gy-scrape-external-head');
        head.append(
            node('strong', '', '外部资料线索'),
            node('span', '', data.advisory || '仅辅助改写 TMDB 搜索词'),
        );
        elements.externalHints.appendChild(head);

        if (!items.length) {
            const empty = node('div', 'gy-scrape-external-empty');
            empty.append(icon('book-x'), node('span', '', '豆瓣 / Bangumi 暂无可用线索'));
            elements.externalHints.appendChild(empty);
        }
        items.forEach((item) => {
            const button = node('button', 'gy-scrape-external-item');
            button.type = 'button';
            const copy = node('span', 'gy-scrape-external-copy');
            copy.append(
                node('strong', '', item.title || item.original_title || '未命名条目'),
                node('span', '', [
                    item.original_title,
                    item.year,
                    item.rating ? `评分 ${Number(item.rating).toFixed(1)}` : '',
                ].filter(Boolean).join(' · ')),
            );
            button.append(
                node('span', 'gy-scrape-external-source', externalProviderLabel(item.provider)),
                copy,
                icon('arrow-right'),
            );
            button.addEventListener('click', async () => {
                elements.query.value = item.title || item.original_title || elements.query.value;
                if (['movie', 'tv'].includes(item.media_type)) {
                    elements.type.value = item.media_type;
                    syncEpisodeFields(item.media_type);
                }
                await searchCandidates();
            });
            elements.externalHints.appendChild(button);
        });
        errors.forEach((error) => {
            elements.externalHints.appendChild(node(
                'div',
                'gy-scrape-external-error',
                `${externalProviderLabel(error.provider)}：${error.message || '查询失败'}`,
            ));
        });
        renderIcons(elements.externalHints);
    }

    async function loadExternalHints() {
        if (!state.inspection) return;
        if (isNsfwOnly()) {
            window.appAlert?.({
                type: 'info',
                title: '成人番号专用来源',
                message: '该来源只使用 MetaTube 精确番号识别，不调用豆瓣、Bangumi 或 TMDB。',
            });
            return;
        }
        const query = elements.query.value.trim();
        if (!query) {
            window.appAlert?.({type: 'warning', title: '缺少搜索名称', message: '请先输入媒体名称'});
            return;
        }
        const requestKey = JSON.stringify([
            state.inspection.inspection_id,
            query,
            elements.type.value,
        ]);
        if (state.pendingExternalKey === requestKey) return;
        state.externalController?.abort();
        const controller = new AbortController();
        state.externalController = controller;
        state.pendingExternalKey = requestKey;
        elements.externalBtn.disabled = true;
        setButtonContent(elements.externalBtn, 'loader-circle', '查询线索', true);
        try {
            const data = await api('/external-hints', {
                inspection_id: state.inspection.inspection_id,
                query,
                media_type: elements.type.value,
            }, {signal: controller.signal});
            renderExternalHints(data);
        } catch (error) {
            if (error.name !== 'AbortError') {
                window.appAlert?.({type: 'error', title: '外部资料查询失败', message: error.message});
            }
        } finally {
            if (state.externalController === controller) {
                state.externalController = null;
                state.pendingExternalKey = '';
                elements.externalBtn.disabled = false;
                setButtonContent(elements.externalBtn, 'book-open-check', '豆瓣 / BGM 线索');
            }
        }
    }

    function addChip(container, value) {
        if (!value) return;
        container.appendChild(node('span', 'gy-scrape-chip', value));
    }

    function planIdentity(name, fallback = '') {
        const match = String(name || '').match(/S(\d{1,2})E(\d{1,3})/i);
        if (!match) return fallback;
        return `S${match[1].padStart(2, '0')}E${match[2].padStart(2, '0')}`;
    }

    function planState(plan) {
        if (plan.action === 'move') {
            if (plan.conflict_decision === 'replace') {
                return {label: '替换', tone: 'replace'};
            }
            return {label: '归档', tone: 'move'};
        }
        if (plan.action === 'conflict') return {label: '冲突', tone: 'conflict'};
        return {label: '跳过', tone: 'skip'};
    }

    function appendMapRow(container, {
        fileId = '', source = '', sourceMeta = '', identity = '', action = '',
        tone = 'move', target = '', targetMeta = '', note = '', rowClass = '',
    }) {
        const row = node('div', `gy-scrape-map-row is-${tone}${rowClass ? ` ${rowClass}` : ''}`);
        if (fileId) row.dataset.fileId = String(fileId);

        const sourceCell = node('div', 'gy-scrape-map-source');
        sourceCell.appendChild(node('strong', '', source));
        if (sourceMeta) sourceCell.appendChild(node('small', '', sourceMeta));

        const stateCell = node('div', 'gy-scrape-map-state');
        stateCell.appendChild(icon(tone === 'conflict' ? 'triangle-alert' : 'arrow-right'));
        if (identity) stateCell.appendChild(node(
            'span',
            `gy-scrape-identity${identity.startsWith('S00') ? ' is-special' : ''}`,
            identity,
        ));
        stateCell.appendChild(node('small', `is-${tone}`, action));

        const targetCell = node('div', 'gy-scrape-map-target');
        targetCell.appendChild(node('strong', '', target));
        if (targetMeta) targetCell.appendChild(node('small', '', targetMeta));
        if (note) targetCell.appendChild(node('em', '', note));

        row.append(sourceCell, stateCell, targetCell);
        container.appendChild(row);
    }

    function renderDetail(candidate, preview = null) {
        elements.detail.replaceChildren();
        elements.detail.setAttribute('aria-busy', preview ? 'false' : 'true');
        const media = preview?.match ? {...candidate, ...preview.match} : candidate;
        const provider = String(media.provider || candidate.provider || 'tmdb').toLowerCase();

        const profile = node('div', 'gy-scrape-media-summary');
        profile.appendChild(poster(media.poster_url, 'gy-scrape-poster'));
        const copy = node('div', 'gy-scrape-profile-copy');
        copy.append(
            node('h3', '', media.title || candidateFallbackTitle(media)),
            node('p', 'gy-scrape-original', media.original_title || ''),
        );
        const chips = node('div', 'gy-scrape-chips');
        addChip(chips, media.media_type === 'tv' ? '剧集' : '电影');
        addChip(chips, media.year || '');
        addChip(
            chips,
            provider === 'metatube'
                ? `MetaTube ${media.number || media.external_id || ''}`.trim()
                : `TMDB ${media.tmdb_id || media.external_id || ''}`.trim(),
        );
        if (media.vote_average) addChip(chips, `评分 ${Number(media.vote_average).toFixed(1)}`);
        (media.genres || []).slice(0, 2).forEach((genre) => addChip(chips, genre));
        copy.appendChild(chips);
        profile.appendChild(copy);

        const detailUrl = provider === 'metatube'
            ? String(media.homepage || '')
            : `https://www.themoviedb.org/${media.media_type === 'tv' ? 'tv' : 'movie'}/${encodeURIComponent(media.tmdb_id)}`;
        if (/^https?:\/\//i.test(detailUrl)) {
            const detailLink = node('a', 'gy-scrape-tmdb-link');
            detailLink.href = detailUrl;
            detailLink.target = '_blank';
            detailLink.rel = 'noopener noreferrer';
            detailLink.title = provider === 'metatube' ? '打开元数据来源页面' : '在 TMDB 查看媒体档案';
            detailLink.append(
                node('span', '', provider === 'metatube' ? '来源页面' : 'TMDB 页面'),
                icon('external-link'),
            );
            profile.appendChild(detailLink);
        }
        elements.detail.appendChild(profile);

        const planBox = node('div', 'gy-scrape-plan');
        const targetBar = node('div', 'gy-scrape-plan-target');
        const targetCopy = node('div');
        targetCopy.appendChild(node('span', '', '目标路径'));
        const namingRule = node(
            'small',
            '',
            media.media_type === 'tv' ? '标准 SxxExx 命名' : '标准电影命名',
        );

        if (!preview) {
            targetCopy.appendChild(node('strong', '', '正在生成无副作用预览…'));
            targetBar.append(targetCopy, namingRule);
            planBox.appendChild(targetBar);
            const loading = node('div', 'gy-scrape-map-loading');
            loading.append(icon('loader-circle', 'is-spinning'), node('span', '', '正在核对文件与归档目标'));
            planBox.appendChild(loading);
            elements.detail.appendChild(planBox);
            renderIcons(elements.detail);
            return;
        }

        const plans = preview.plans || [];
        const first = plans[0] || {};
        const targetRoot = preview.archive_target?.name || preview.archive_target?.id || '';
        targetCopy.appendChild(node(
            'strong',
            '',
            [targetRoot, first.target_path].filter(Boolean).join(' / ') || '未生成目标目录',
        ));
        targetBar.append(targetCopy, namingRule);
        planBox.appendChild(targetBar);

        const mapHead = node('div', 'gy-scrape-map-head');
        mapHead.append(
            node('span', '', '原始扫描文件（SOURCE）'),
            node('span', '', '映射'),
            node('span', '', '规范化归档目标（TARGET）'),
        );
        planBox.appendChild(mapHead);

        const files = node('div', 'gy-scrape-plan-files');
        plans.forEach((plan) => {
            const stateInfo = planState(plan);
            const targetIdentity = (
                Number.isInteger(plan.season) && Number.isInteger(plan.episode)
                    ? `S${String(plan.season).padStart(2, '0')}E${String(plan.episode).padStart(2, '0')}`
                    : planIdentity(plan.new_name, candidate.media_type === 'movie' ? '电影' : '')
            );
            const mapping = plan.episode_mapping || {};
            const sourceIdentity = (
                Number.isInteger(plan.source_season) && Number.isInteger(plan.source_episode)
                    ? `S${String(plan.source_season).padStart(2, '0')}E${String(plan.source_episode).padStart(2, '0')}`
                    : ''
            );
            const sourceMeta = [
                plan.original_path || '',
                mapping.changed && sourceIdentity ? `发布编号 ${sourceIdentity}` : '',
            ].filter(Boolean).join(' · ');
            appendMapRow(files, {
                fileId: plan.file_id,
                source: plan.original_name || '',
                sourceMeta,
                identity: targetIdentity,
                action: mapping.changed ? `${stateInfo.label} · 已映射` : stateInfo.label,
                tone: stateInfo.tone,
                target: plan.new_name || plan.original_name || '',
                targetMeta: plan.target_path || '',
                note: plan.conflict_note || plan.note || '',
            });
        });

        const planByFileId = new Map(plans.map((plan) => [String(plan.file_id), plan]));
        const companionPlans = preview.companion_plans || [];
        companionPlans.forEach((plan) => {
            const parent = planByFileId.get(String(plan.video_file_id || '')) || {};
            const moving = plan.action === 'move';
            appendMapRow(files, {
                fileId: plan.file_id,
                source: plan.original_name || '',
                sourceMeta: plan.relative_dir || '',
                identity: plan.role === 'subtitle' ? '字幕' : '元数据',
                action: moving ? '归档' : '跳过',
                tone: moving ? 'move' : 'skip',
                target: plan.target_name || `${plan.original_name || ''}（不会移动）`,
                targetMeta: moving ? (parent.target_path || '') : '',
                note: plan.note || '',
                rowClass: 'is-companion',
            });
        });

        const pending = pendingVideos(preview);
        pending.forEach((item) => {
            appendMapRow(files, {
                fileId: item.file_id,
                source: item.name || '',
                sourceMeta: item.relative_dir || '',
                identity: '待确认',
                action: '不移动',
                tone: 'pending',
                target: `${item.name || ''}（不会移动）`,
                note: item.reason || '无法确认媒体归属',
                rowClass: 'is-pending',
            });
        });
        planBox.appendChild(files);
        elements.detail.appendChild(planBox);
        renderIcons(elements.detail);
    }

    async function selectCandidate(candidate, button) {
        let overrides;
        try {
            syncEpisodeFields(candidate.media_type);
            overrides = episodePreviewPayload(candidate.media_type);
        } catch (error) {
            elements.status.textContent = '预览参数无效';
            elements.planSummary.textContent = error.message;
            return;
        }
        const previewKey = JSON.stringify([
            state.inspection?.inspection_id || '', candidate.provider || 'tmdb',
            candidate.external_id || candidate.tmdb_id,
            candidate.media_type, overrides.numbering_mode || 'auto',
            overrides.season ?? null, overrides.episode ?? null,
        ]);
        if (state.pendingPreviewKey === previewKey) return;
        state.previewController?.abort();
        const controller = new AbortController();
        state.previewController = controller;
        state.pendingPreviewKey = previewKey;
        state.selectedCandidate = candidate;
        state.preview = null;
        setPlanReady(false);
        setMobilePane('preview');
        elements.run.disabled = true;
        elements.status.textContent = '正在生成预览';
        elements.candidates.querySelectorAll('.gy-scrape-candidate').forEach((item) => {
            const selected = item === button;
            item.classList.toggle('is-selected', selected);
            item.setAttribute('aria-pressed', selected ? 'true' : 'false');
        });
        renderDetail(candidate);
        const version = ++state.requestVersion;
        try {
            const preview = await api('/preview', {
                inspection_id: state.inspection.inspection_id,
                tmdb_id: candidate.tmdb_id,
                provider: candidate.provider || 'tmdb',
                external_id: candidate.external_id || candidate.tmdb_id || '',
                media_type: candidate.media_type,
                ...overrides,
            }, {signal: controller.signal});
            if (version !== state.requestVersion) return;
            state.preview = preview;
            setPlanReady(true);
            renderDetail(candidate, preview);
            elements.archiveTarget.textContent = archiveTargetLabel(
                preview.archive_target?.name || preview.archive_target?.id,
            );
            const moving = (preview.plans || []).filter((plan) => plan.action === 'move').length;
            const conflicts = (preview.plans || []).filter((plan) => plan.action === 'conflict').length;
            const skipped = (preview.plans || []).length - moving - conflicts;
            const pending = pendingVideos(preview).length;
            const summary = [`${moving} 个归档`];
            if (skipped) summary.push(`${skipped} 个跳过`);
            summary.push(conflicts ? `${conflicts} 个冲突` : '冲突检查：无冲突');
            if (pending) summary.push(`${pending} 个待确认不移动`);
            elements.planSummary.textContent = summary.join(' · ');
            elements.status.textContent = `预览完成 · ${moving + skipped + conflicts + pending} 项`;
            elements.run.disabled = false;
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (version !== state.requestVersion) return;
            elements.status.textContent = '预览失败';
            elements.planSummary.textContent = error.message;
            window.appAlert?.({type: 'error', title: '无法生成归档预览', message: error.message});
        } finally {
            if (state.previewController === controller) {
                state.previewController = null;
                state.pendingPreviewKey = '';
            }
        }
    }

    async function searchCandidates() {
        const query = elements.query.value.trim();
        if (!query || !state.inspection) return;
        const searchKey = JSON.stringify([
            state.inspection.inspection_id, query, elements.type.value,
        ]);
        if (state.pendingSearchKey === searchKey) return;
        state.searchController?.abort();
        state.previewController?.abort();
        const controller = new AbortController();
        state.searchController = controller;
        state.previewController = null;
        state.pendingSearchKey = searchKey;
        state.pendingPreviewKey = '';
        state.preview = null;
        state.selectedCandidate = null;
        setPlanReady(false);
        setMobilePane('candidates');
        elements.run.disabled = true;
        elements.status.textContent = isNsfwOnly() ? '正在核对番号' : '正在更新候选';
        elements.planSummary.textContent = isNsfwOnly()
            ? '只接受完全一致的 MetaTube 番号结果'
            : '选择候选后生成归档预览';
        placeholder(
            elements.detail,
            'route',
            isNsfwOnly() ? '选择精确番号候选' : '选择候选生成预览',
            isNsfwOnly()
                ? '候选返回后，请核对番号、标题与最终成人归档分类目录。'
                : '搜索结果更新后，请选择候选核对季集映射和最终归档名称。',
        );
        const previous = elements.search.querySelector('span')?.textContent || '搜索';
        elements.search.disabled = true;
        elements.search.querySelector('span').textContent = '搜索中';
        elements.candidates.classList.add('is-loading');
        const version = ++state.requestVersion;
        try {
            const data = await api('/search', {
                inspection_id: state.inspection.inspection_id,
                query,
                media_type: isNsfwOnly() ? 'movie' : elements.type.value,
            }, {signal: controller.signal});
            if (version !== state.requestVersion) return;
            renderCandidates(data.candidates || []);
            elements.status.textContent = data.candidates?.length ? '请选择候选' : '没有候选';
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (version !== state.requestVersion) return;
            window.appAlert?.({type: 'error', title: '媒体搜索失败', message: error.message});
        } finally {
            if (version === state.requestVersion) {
                elements.search.disabled = false;
                elements.search.querySelector('span').textContent = previous;
                elements.candidates.classList.remove('is-loading');
            }
            if (state.searchController === controller) {
                state.searchController = null;
                state.pendingSearchKey = '';
            }
        }
    }

    async function openManual(directory, row, action, inspection = null, candidates = null) {
        const resolved = inspection || await inspectDirectory(directory, row, action);
        state.activeDirectory = directory;
        state.activeRow = row;
        state.activeAction = action;
        resetModal(resolved);
        openModal();
        if (Array.isArray(candidates) && candidates.length) {
            renderCandidates(candidates);
            elements.status.textContent = '自动匹配需要人工确认';
        } else {
            await searchCandidates();
        }
        setRowState(row, action, 'idle');
    }

    const TASK_POLL_INTERVAL_MS = 1000;
    const TASK_POLL_MAX_ATTEMPTS = 1800;
    const taskPollEntries = new Map();
    let taskPollTimer = null;
    let taskPollController = null;
    let taskPollInFlight = false;
    let taskPollGeneration = 0;
    let taskPollingDisposed = false;

    function clearTaskPollTimer() {
        if (taskPollTimer !== null) window.clearTimeout(taskPollTimer);
        taskPollTimer = null;
    }

    function scheduleTaskPoll(delay = TASK_POLL_INTERVAL_MS) {
        if (
            taskPollingDisposed
            || document.hidden
            || taskPollInFlight
            || taskPollTimer !== null
            || !taskPollEntries.size
        ) return;
        taskPollTimer = window.setTimeout(() => {
            taskPollTimer = null;
            runTaskPoll().catch(() => {});
        }, delay);
    }

    async function refreshTaskParent(parentId) {
        if (window.gyNavigator?.state?.().id === parentId) {
            await window.gyNavigator.reload();
        }
    }

    function countTaskFailures(stats) {
        const countFailure = (value) => {
            if (Array.isArray(value)) return value.length;
            const number = Number(value || 0);
            return Number.isFinite(number) ? number : (value ? 1 : 0);
        };
        return [
            'failed',
            'replacement_cleanup_failed',
            'empty_dir_cleanup_failed',
            'source_dir_cleanup_failed',
            'audit_failures',
            'scan_errors',
        ].reduce((total, key) => total + countFailure(stats[key]), 0);
    }

    async function applyTaskPollStatus(entry, status) {
        const {taskId, row, action} = entry;
        const matchesTask = (item) => String(item?.id || '') === taskId;
        const queued = (status.operation_queue?.items || []).find(matchesTask);
        if (queued) {
            const ahead = Number(queued.ahead || 0);
            setRowState(
                row,
                action,
                'queued',
                ahead > 0 ? `已排队，前方 ${ahead} 项` : '已排队，等待当前任务结束',
            );
            return false;
        }
        const history = (status.operation_history || []).find(matchesTask);
        const task = matchesTask(status) ? status : history;
        if (!task) return false;
        if (task.status === 'running') {
            setRowState(row, action, 'running', '目录刮削任务执行中');
            return false;
        }
        if (['completed', 'partial'].includes(task.status)) {
            const stats = task.result?.stats || {};
            const failures = countTaskFailures(stats);
            const pending = Number(stats.pending_confirmation || 0);
            if (failures > 0 || task.status === 'partial') {
                const visibleFailures = Math.max(1, failures);
                setRowState(row, action, 'error', `${visibleFailures} 项处理失败`);
                window.appAlert?.({
                    type: 'warning',
                    title: '目录刮削部分完成',
                    message: `${visibleFailures} 项处理失败，请到整理日志查看详情。`,
                });
                return true;
            }
            if (pending > 0) {
                const files = Array.isArray(stats.pending_files) ? stats.pending_files : [];
                setRowState(row, action, 'done', `${pending} 项待确认`);
                window.appAlert?.({
                    type: 'warning',
                    title: '目录刮削已完成，仍有文件待确认',
                    message: [
                        `${pending} 个无法安全归属的文件已保留在源目录，没有移动。`,
                        files.slice(0, 3).map((item) => item.name).join('、'),
                        '可在目录中对这些文件单独右键刮削。',
                    ].filter(Boolean).join(' '),
                });
                return true;
            }
            setRowState(row, action, 'done', '目录刮削完成');
            return true;
        }
        if (['failed', 'stopped'].includes(task.status)) {
            throw new Error(task.error || task.message || '目录刮削未完成');
        }
        return false;
    }

    async function runTaskPoll() {
        if (taskPollingDisposed || document.hidden || taskPollInFlight || !taskPollEntries.size) return;
        taskPollInFlight = true;
        const generation = taskPollGeneration;
        const controller = new AbortController();
        taskPollController = controller;
        const refreshParents = new Set();
        try {
            const response = await fetch('/api/guangya/organize/status', {signal: controller.signal});
            const status = await response.json();
            if (!response.ok) throw new Error(status.error || '任务状态读取失败');
            if (generation !== taskPollGeneration || document.hidden || taskPollingDisposed) return;
            for (const [taskId, entry] of [...taskPollEntries.entries()]) {
                entry.attempts += 1;
                try {
                    const finished = await applyTaskPollStatus(entry, status);
                    if (finished) {
                        taskPollEntries.delete(taskId);
                        refreshParents.add(entry.parentId);
                    }
                    else if (entry.attempts >= TASK_POLL_MAX_ATTEMPTS) {
                        setRowState(
                            entry.row,
                            entry.action,
                            'running',
                            '任务仍在后台运行，请稍后查看整理日志',
                        );
                        taskPollEntries.delete(taskId);
                    }
                } catch (error) {
                    setRowState(entry.row, entry.action, 'error', error.message);
                    window.appAlert?.({type: 'error', title: '目录刮削失败', message: error.message});
                    taskPollEntries.delete(taskId);
                }
            }
            for (const parentId of refreshParents) {
                try {
                    await refreshTaskParent(parentId);
                } catch (_error) {
                    window.appAlert?.({
                        type: 'warning',
                        title: '任务已完成，但目录刷新失败',
                        message: '请手动刷新当前目录确认最新内容。',
                    });
                }
            }
        } catch (error) {
            if (error?.name === 'AbortError' || generation !== taskPollGeneration) return;
            for (const entry of taskPollEntries.values()) {
                setRowState(entry.row, entry.action, 'error', error.message);
            }
            taskPollEntries.clear();
            window.appAlert?.({type: 'error', title: '目录刮削失败', message: error.message});
        } finally {
            if (taskPollController === controller) taskPollController = null;
            taskPollInFlight = false;
            scheduleTaskPoll();
        }
    }

    function pollTask(taskId, directory, row, action, parentId) {
        const normalizedTaskId = String(taskId || '');
        if (!normalizedTaskId) return;
        if (action?.dataset?.state !== 'queued') {
            setRowState(row, action, 'running', '目录刮削任务执行中');
        }
        const current = taskPollEntries.get(normalizedTaskId);
        taskPollEntries.set(normalizedTaskId, {
            taskId: normalizedTaskId,
            directory,
            row,
            action,
            parentId,
            attempts: current?.attempts || 0,
        });
        scheduleTaskPoll();
    }

    document.addEventListener('visibilitychange', () => {
        taskPollGeneration += 1;
        clearTaskPollTimer();
        if (document.hidden) {
            taskPollController?.abort();
            return;
        }
        scheduleTaskPoll(0);
    });
    window.addEventListener('pagehide', (event) => {
        taskPollingDisposed = true;
        taskPollGeneration += 1;
        clearTaskPollTimer();
        taskPollController?.abort();
        taskPollController = null;
        if (!event.persisted) taskPollEntries.clear();
    });
    window.addEventListener('pageshow', (event) => {
        if (!event.persisted) return;
        taskPollingDisposed = false;
        scheduleTaskPoll(0);
    });

    async function runManual() {
        if (!state.preview) return;
        const previewId = state.preview.preview_id;
        const directory = state.activeDirectory;
        const row = state.activeRow;
        const action = state.activeAction;
        const parentId = state.parentId;
        const generation = state.requestVersion;
        elements.run.disabled = true;
        elements.run.querySelector('span').textContent = '提交中';
        try {
            const task = await api('/run', {
                mode: 'manual',
                preview_id: previewId,
            });
            if (generation === state.requestVersion) closeModal();
            if (task.queued) {
                const ahead = Math.max(0, Number(task.queue_position || 1) - 1);
                setRowState(
                    row,
                    action,
                    'queued',
                    ahead > 0 ? `已排队，前方 ${ahead} 项` : '已排队，等待当前任务结束',
                );
            }
            pollTask(task.task_id, directory, row, action, parentId);
        } catch (error) {
            elements.run.disabled = false;
            elements.run.querySelector('span').textContent = '确认并开始刮削';
            window.appAlert?.({type: 'error', title: '任务启动失败', message: error.message});
        }
    }

    async function runAuto(directory, row, action) {
        const parentId = window.gyNavigator?.state?.().id || '';
        const inspection = await inspectDirectory(directory, row, action);
        const counts = inspection.counts || {};
        const pending = Number(counts.pending_video || 0);
        const confirmed = await window.appConfirm?.({
            title: '自动匹配并刮削所选媒体',
            message: [
                `目录：${inspection.directory.name}`,
                `媒体：${counts.video || 0} 个视频 · ${counts.subtitle || 0} 个字幕 · ${counts.metadata || 0} 个元数据`,
                pending ? `另有 ${pending} 个文件无法安全归属，将保留在源目录等待手动刮削。` : '',
                `归档目标：${inspection.archive_target.name || inspection.archive_target.id}`,
                '将使用当前光鸭整理命名与冲突规则；匹配不足时不会移动文件。',
            ].filter(Boolean).join('；'),
            confirmText: '确认并自动刮削',
        });
        if (!confirmed) {
            setRowState(row, action, 'idle');
            return;
        }
        setRowState(row, action, 'loading', '正在自动匹配媒体');
        try {
            const result = await api('/run', {
                mode: 'auto',
                inspection_id: inspection.inspection_id,
            });
            if (result.status === 'requires_manual') {
                await openManual(directory, row, action, inspection, result.candidates || []);
                elements.query.value = result.suggested_query || inspection.suggested_query;
                elements.planSummary.textContent = result.message || '请选择候选后继续';
                return;
            }
            pollTask(
                result.task_id,
                directory,
                row,
                action,
                parentId,
            );
        } catch (error) {
            setRowState(row, action, 'error', error.message);
            window.appAlert?.({type: 'error', title: '自动刮削失败', message: error.message});
        }
    }

    async function deleteItem(item, parentId, row, action) {
        const isDirectory = item.is_dir === true;
        const itemLabel = isDirectory ? '目录' : '文件';
        const confirmed = await window.appConfirm?.({
            title: isDirectory ? '删除整个目录' : '删除文件',
            message: isDirectory
                ? `将删除「${item.name}」及其中全部内容。项目会进入光鸭回收站，恢复请前往光鸭操作。`
                : `将删除「${item.name}」。文件会进入光鸭回收站，恢复请前往光鸭操作。`,
            confirmText: isDirectory ? '删除整个目录' : '删除文件',
            danger: true,
        });
        if (!confirmed) return;

        action.disabled = true;
        setRowState(row, action, 'deleting', `正在删除${itemLabel}`);
        try {
            const response = await fetch('/api/guangya/delete-item', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_id: item.file_id}),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '光鸭删除项目失败');
        } catch (error) {
            action.disabled = false;
            setRowState(row, action, 'error', error.message);
            await window.appAlert?.({
                type: 'error',
                title: `${itemLabel}删除失败`,
                message: error.message,
            });
            return;
        }

        setRowState(row, action, 'done', `${itemLabel}已删除`);
        if (window.gyNavigator?.state?.().id === parentId) {
            try {
                await window.gyNavigator.reload();
            } catch (_error) {
                await window.appAlert?.({
                    type: 'warning',
                    title: `${itemLabel}已删除，但目录刷新失败`,
                    message: '请手动刷新当前目录确认最新内容。',
                });
                return;
            }
        }
        await window.appAlert?.({
            type: 'success',
            title: `${itemLabel}已删除`,
            message: `${item.name} 已移入光鸭回收站。`,
        });
    }

    menu.addEventListener('click', (event) => {
        const button = event.target.closest('[data-scrape-action], [data-browser-action]');
        if (!button || !state.activeItem) return;
        const item = state.activeItem;
        const row = state.activeRow;
        const action = state.activeAction;
        const parentId = state.parentId;
        closeMenu();
        if (button.dataset.scrapeAction === 'manual') {
            openManual(item, row, action).catch(() => {});
        } else if (button.dataset.scrapeAction === 'auto') {
            runAuto(item, row, action).catch(() => {});
        } else if (button.dataset.browserAction === 'delete-item') {
            deleteItem(item, parentId, row, action).catch(() => {});
        }
    });
    document.addEventListener('click', (event) => {
        if (!menu.hidden && !menu.contains(event.target) && !state.activeAction?.contains(event.target)) {
            closeMenu();
        }
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !menu.hidden) closeMenu({restoreFocus: true});
    });
    window.addEventListener('resize', closeMenuAfterViewportChange);
    window.visualViewport?.addEventListener('scroll', closeMenuAfterViewportChange);
    window.visualViewport?.addEventListener('resize', closeMenuAfterViewportChange);
    document.addEventListener('scroll', closeMenuAfterViewportChange, true);
    modal.addEventListener('click', (event) => {
        const paneButton = event.target.closest('[data-scrape-mobile-pane]');
        if (paneButton) setMobilePane(paneButton.dataset.scrapeMobilePane);
    });
    function sanitizeSearchQuery(query) {
        return window.MediaScrapePosition.sanitizeSearchQuery(query, {
            knownEpisode: state.inspection?.episode,
        });
    }

    elements.close.addEventListener('click', closeModal);
    elements.cancel.addEventListener('click', closeModal);
    elements.search.addEventListener('click', searchCandidates);
    elements.externalBtn.addEventListener('click', loadExternalHints);
    if (elements.cleanBtn) {
        elements.cleanBtn.addEventListener('click', () => {
            const raw = elements.query.value;
            const cleaned = sanitizeSearchQuery(raw);
            if (!cleaned) {
                elements.status.textContent = '未识别到可保留的标题，请手动修改';
                elements.query.focus();
                elements.query.select();
                return;
            }
            elements.query.value = cleaned;
            elements.status.textContent = cleaned === raw.trim()
                ? '名称无需精简，正在搜索'
                : '已精简，正在搜索';
            searchCandidates();
        });
    }
    elements.query.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchCandidates();
    });
    elements.query.addEventListener('input', invalidatePreview);
    elements.type.addEventListener('change', () => {
        syncEpisodeFields();
        invalidatePreview();
    });
    elements.numbering?.addEventListener('change', invalidatePreview);
    elements.season.addEventListener('input', invalidatePreview);
    elements.episode.addEventListener('input', invalidatePreview);
    elements.run.addEventListener('click', runManual);

    window.GuangYaDirectoryScrapeUI = {
        bindMediaRow,
        closeMenu,
    };
    renderIcons(menu);
}(window));
