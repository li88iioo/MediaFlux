(() => {
    'use strict';
    const root = document.querySelector('[data-weekly-calendar]');
    if (!root) return;
    const byId = (id) => document.getElementById(id);
    const names = {tencent: '腾讯视频', iqiyi: '爱奇艺', youku: '优酷'};
    const statusNames = {loading: '读取中', ok: '已同步', partial: '部分数据', stale: '旧缓存', unavailable: '不可用'};
    const labels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const days = new Map();
    const cards = new Map();
    const watchStates = new Map();
    let watchRevision = 0;
    const refresh = byId('calendar-refresh');
    const sourceToggle = byId('calendar-source-toggle');
    const sourcePanel = byId('calendar-source-panel');
    let snapshot = null;
    let selectedDay = root.dataset.today;
    let timer = null;
    let controller = null;
    let version = 0;
    let inFlight = false;
    let resumeNeeded = false;
    let failed = false;
    const text = (value) => typeof value === 'string' ? value : '';
    const setText = (el, value) => { if (el.textContent !== value) el.textContent = value; };
    const node = (tag, className, value) => {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (value !== undefined) el.textContent = value;
        return el;
    };
    const validDate = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
        && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
    const weekDates = (start) => Array.from({length: 7}, (_, index) => {
        const date = new Date(`${start}T00:00:00Z`);
        date.setUTCDate(date.getUTCDate() + index);
        return date.toISOString().slice(0, 10);
    });
    const validTime = (value) => typeof value === 'string' && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value) ? value : '';
    const audience = (value) => ['free', 'member'].includes(value) ? value : 'unknown';
    const audienceNames = {unknown: '平台排期', free: '免费排期', member: '会员排期'};
    function timestamp(value) {
        if (!text(value) || !Number.isFinite(Date.parse(value))) return '';
        return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value));
    }
    const mediaId = (value) => {
        const id = typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : text(value);
        return /^\d{1,20}$/.test(id) ? id : '';
    };
    function posterURL(value) {
        // 只接受后端签名代理路径；不接受外站、query、路径跳转或任意 provider。
        if (typeof value !== 'string' || !/^\/(?:discovery-poster\/(?:tmdb|douban)|discovery-calendar-poster\/(?:tencent|iqiyi|youku))\/[A-Za-z0-9_.-]{1,2048}$/.test(value)) return '';
        const url = new URL(value, location.origin);
        // 精确对照原路径，拒绝 URL 规范化吞掉的 . / .. 或末尾换行。
        return url.origin === location.origin && url.pathname === value && !url.search && !url.hash ? url.href : '';
    }
    const isPlatformPoster = (url) => Boolean(url && url.startsWith(`${location.origin}/discovery-calendar-poster/`));
    function labelPoster(record, url) {
        const label = isPlatformPoster(url) ? '平台原图，仅作封面，不代表已匹配资料' : '';
        record.image.title = label; record.image.alt = label;
    }
    function platformURL(value, source) {
        const hosts = {tencent: ['v.qq.com', 'm.v.qq.com'], iqiyi: ['www.iqiyi.com', 'm.iqiyi.com'],
            youku: ['www.youku.com', 'v.youku.com', 'm.youku.com', 'youku.com']}[source];
        if (!Array.isArray(hosts) || typeof value !== 'string' || !value.startsWith('https://') || /[\\\s]/.test(value)) return '';
        try {
            const url = new URL(value);
            return url.protocol === 'https:' && hosts.includes(url.hostname) && !url.username && !url.password && !url.port
                ? url.href : '';
        } catch (_) { return ''; }
    }
    function profileURL(provider, id) {
        return id && ['tmdb', 'douban'].includes(provider)
            ? `/discovery?detail_provider=${provider}&detail_type=tv&detail_id=${id}` : '';
    }
    function detailURL(value, item) {
        if (typeof value !== 'string' || !value.startsWith('/discovery?') || /[\\\s]/.test(value)) return '';
        try {
            const url = new URL(value, location.origin);
            const params = url.searchParams;
            const provider = params.get('detail_provider');
            const expected = provider === 'tmdb' ? mediaId(item.tmdb_id) : provider === 'douban' ? mediaId(item.douban_id) : '';
            if (url.origin !== location.origin || url.pathname !== '/discovery' || url.hash || [...params].length !== 3
                || params.get('detail_type') !== 'tv' || !expected || params.get('detail_id') !== expected) return '';
            return profileURL(provider, expected);
        } catch (_) { return ''; }
    }
    function watchIdentity(item) {
        const value = item.watchlist;
        const tmdb = mediaId(item.tmdb_id); const douban = mediaId(item.douban_id);
        const provider = tmdb ? 'tmdb' : douban ? 'douban' : '';
        const id = tmdb || douban;
        if (!value || !provider || value.provider !== provider || value.media_type !== 'tv'
            || mediaId(value.external_id) !== id || typeof value.in_watchlist !== 'boolean') return null;
        const token = value.poster_token;
        if (typeof token !== 'string' || (token && !/^[A-Za-z0-9_.-]{1,2048}$/.test(token))) return null;
        return {provider, external_id: id, media_type: 'tv', poster_token: token, in_watchlist: value.in_watchlist,
            key: `${provider}:tv:${id}`};
    }
    function setStatus(message, tone = 'normal') {
        const status = byId('calendar-status');
        setText(status, message); status.title = message; status.setAttribute('aria-label', message); status.dataset.tone = tone;
    }
    function showPoster(record, url) {
        labelPoster(record, '');
        // 无安全地址是元数据缺失；只有实际尝试过的合法候选全部失败才提示加载失败。
        const exhausted = record.posterCandidates.length > 0 && record.posterCandidates.every((candidate) => record.failedPosters.has(candidate));
        setText(record.posterNote, url ? '封面加载中' : exhausted ? '封面加载失败' : record.posterUnmatched ? '暂未匹配海报' : '暂无海报');
        record.posterNote.title = url ? '正在加载封面，失败后会尝试备用图。' : exhausted ? '封面及备用图加载失败，可刷新日历重试。'
            : record.posterUnmatched ? '尚未匹配 TMDB 或豆瓣资料，暂无可用海报。' : '尚无可用海报地址，并非封面加载失败。';
        if (!url) { record.image.hidden = true; record.posterNote.hidden = false; return; }
        record.image.hidden = false; record.image.style.opacity = '0'; record.posterNote.hidden = false;
        if (record.image.getAttribute('src') !== url) record.image.src = url;
    }
    function syncPosters(record, item, retryFailed) {
        const safe = [...new Set([...(Array.isArray(item.poster_urls) ? item.poster_urls : []), item.poster_url]
            .map(posterURL).filter(Boolean))];
        // 保留 TMDB/豆瓣的既有顺序，平台原图只能作为最后兜底，不是资料匹配依据。
        const candidates = [...safe.filter((url) => !isPlatformPoster(url)), ...safe.filter(isPlatformPoster)].slice(0, 6);
        record.posterCandidates = candidates;
        record.posterUnmatched = item.mapping_status === 'unmatched' && !mediaId(item.tmdb_id) && !mediaId(item.douban_id);
        record.failedPosters = new Set([...record.failedPosters].filter((url) => candidates.includes(url)));
        if (record.loadedPoster && candidates.includes(record.loadedPoster)) return;
        record.loadedPoster = '';
        const current = record.image.getAttribute('src');
        if (current && candidates.includes(current) && !record.failedPosters.has(current)) return;
        let next = candidates.find((url) => !record.failedPosters.has(url));
        // 全部失败仅在用户明确刷新时再试一次；成功备用图永不因同数据刷新回跳主图。
        if (!next && retryFailed && candidates.length) {
            record.failedPosters.clear(); record.image.removeAttribute('src'); next = candidates[0];
        }
        if (!candidates.length) record.image.removeAttribute('src');
        showPoster(record, next);
    }
    function paintWatch(record) {
        const identity = record.watchIdentity;
        const state = identity && watchStates.get(identity.key);
        const active = Boolean(state?.active); const pending = Boolean(state?.pending);
        const button = record.watchButton;
        // 仅缺少身份时原生禁用；保存期间保留键盘焦点，重复操作由 state.pending 拦截。
        button.disabled = !identity;
        button.setAttribute('aria-disabled', String(!identity || pending));
        button.dataset.watchlistKey = identity?.key || '';
        button.setAttribute('aria-pressed', String(active)); button.setAttribute('aria-busy', String(pending));
        button.classList.toggle('is-active', active); button.classList.toggle('is-pending', pending);
        record.card.classList.toggle('is-watchlisted', active);
        button.title = !identity ? '需先匹配 TMDB 或豆瓣并取得收藏身份' : pending ? '正在保存收藏' : active ? '移出探索收藏' : '加入探索收藏';
        button.setAttribute('aria-label', button.title);
        if (button.dataset.iconState !== String(active)) {
            const icon = node('i'); icon.dataset.lucide = active ? 'bookmark-check' : 'bookmark'; icon.setAttribute('aria-hidden', 'true');
            button.replaceChildren(icon); button.dataset.iconState = String(active);
        }
    }
    function paintWatchIdentity(key) {
        for (const record of cards.values()) if (record.watchIdentity?.key === key) paintWatch(record);
    }
    async function toggleWatch(record) {
        const identity = record.watchIdentity;
        if (!identity) return;
        const state = watchStates.get(identity.key);
        if (!state || state.pending) return;
        const previous = state.active;
        state.active = !previous; state.pending = true; state.revision = ++watchRevision;
        paintWatchIdentity(identity.key);
        const abort = new AbortController(); const timeout = setTimeout(() => abort.abort(), 20000);
        try {
            const path = previous ? `/api/discovery/watchlist/${identity.provider}/tv/${identity.external_id}` : '/api/discovery/watchlist';
            const response = await fetch(path, {
                method: previous ? 'DELETE' : 'POST', credentials: 'same-origin', signal: abort.signal,
                headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''},
                ...(previous ? {} : {body: JSON.stringify({provider: identity.provider, media_type: 'tv', external_id: identity.external_id,
                    title: record.watchTitle, year: record.watchYear, poster_token: identity.poster_token})}),
            });
            if (!response.ok || (await response.json()).success !== true) throw new Error('watchlist failed');
            setStatus(state.active ? '已加入探索收藏' : '已移出探索收藏');
        } catch (_) {
            state.active = previous;
            setStatus('收藏操作失败，已恢复原状态；请稍后重试', 'error');
        } finally {
            clearTimeout(timeout); state.pending = false; state.revision = ++watchRevision; paintWatchIdentity(identity.key);
        }
    }
    function setLink(link, href, description) {
        link.title = href ? description : `${description}暂不可用`;
        link.setAttribute('aria-label', link.title);
        if (href) {
            if (link.getAttribute('href') !== href) link.setAttribute('href', href);
            link.removeAttribute('aria-disabled'); link.removeAttribute('tabindex');
        } else {
            link.removeAttribute('href'); link.setAttribute('aria-disabled', 'true'); link.tabIndex = -1;
        }
    }
    function profileLink(className, value) {
        const link = node('a', className, value); link.dataset.mediaProfileLink = '';
        return link;
    }
    function createCard(key) {
        const card = node('article', 'discovery-card wc-card');
        card.dataset.key = key;
        const open = profileLink('discovery-card-open');
        const poster = node('div', 'discovery-poster');
        const image = node('img');
        image.width = 200; image.height = 300; image.alt = ''; image.loading = 'lazy'; image.decoding = 'async'; image.referrerPolicy = 'no-referrer'; image.hidden = true;
        const posterNote = node('span', 'wc-poster-note', '暂无海报');

        const stamp = node('span', 'discovery-source-stamp');
        const rating = node('span', 'discovery-rating'); rating.setAttribute('role', 'img');
        const cache = node('span', 'wc-cache', '旧缓存'); cache.hidden = true;
        poster.append(image, posterNote, stamp, rating, cache); open.append(poster);
        const copy = node('div', 'discovery-card-copy');
        const meta = node('div', 'discovery-card-source');
        const kind = node('span'); const access = node('span', 'wc-audience'); meta.append(kind, access);
        const title = node('h3', 'discovery-card-title');
        const description = node('p', 'sr-only wc-description');
        description.id = `calendar-description-${encodeURIComponent(key)}`;
        card.setAttribute('aria-describedby', description.id);
        const footer = node('div', 'discovery-card-footer');
        const scheduleCopy = node('div', 'wc-schedule-copy');
        const time = node('strong', 'wc-update-time'); const caption = node('span', 'sr-only wc-caption');
        const mapping = profileLink('discovery-map-state wc-tmdb-link');
        const mappingIcon = node('i'); mappingIcon.dataset.lucide = 'link'; mappingIcon.setAttribute('aria-hidden', 'true');
        const mappingLabel = node('span'); mapping.append(mappingIcon, mappingLabel); scheduleCopy.append(time, mapping);
        const watchButton = node('button', 'discovery-card-action discovery-watchlist-action'); watchButton.type = 'button';
        footer.append(scheduleCopy, watchButton); copy.append(meta, title, footer);
        card.append(open, copy, description, caption);
        const record = {card, open, image, posterNote, stamp, rating, cache, kind, access, title, description, time, caption,
            mapping, mappingLabel, watchButton, watchIdentity: null, posterCandidates: [], failedPosters: new Set(), loadedPoster: ''};
        image.addEventListener('load', () => {
            const url = image.getAttribute('src');
            if (!record.posterCandidates.includes(url)) return;
            record.loadedPoster = url; image.hidden = false; image.style.opacity = '1'; posterNote.hidden = true;
            labelPoster(record, url);
        });
        image.addEventListener('error', () => {
            const url = image.getAttribute('src');
            if (!record.posterCandidates.includes(url)) return;
            record.failedPosters.add(url); record.loadedPoster = '';
            showPoster(record, record.posterCandidates.find((candidate) => !record.failedPosters.has(candidate)));
        });
        watchButton.addEventListener('click', (event) => { event.stopPropagation(); void toggleWatch(record); });
        cards.set(key, record); return record;
    }
    function updateCard(item, day, requestWatchRevision, retryFailed) {
        const key = JSON.stringify([item.stable_id, day]);
        const r = cards.get(key) || createCard(key);
        const isStale = item.stale === true;
        r.card.dataset.day = day; r.card.dataset.source = item.source;
        r.card.dataset.stableId = item.stable_id; r.card.dataset.stale = String(isStale);
        setText(r.title, text(item.title)); setText(r.stamp, names[item.source]);
        setText(r.kind, `动漫${text(item.year) ? ` · ${item.year}` : ''}`);
        // 日期完全来自服务分桶；不读取 free_weekdays，也不将无排期库存补进今天。
        const events = Array.isArray(item.events) ? item.events.filter((event) => event && event.date === day).map((event) => ({
            time: validTime(event.update_time), caption: text(event.schedule), audience: audience(event.audience),
        })) : [];
        const schedules = events.length ? events : [{time: validTime(item.update_time), caption: text(item.schedule), audience: audience(item.schedule_audience)}];
        // 主排期由服务优选（可能优先免费分支）；events 只做完整说明，不重选更早的会员时间。
        const mode = audience(item.schedule_audience);
        setText(r.access, audienceNames[mode]); r.access.dataset.audience = mode;
        let times = Array.isArray(item.update_times) ? [...new Set(item.update_times.map(validTime).filter(Boolean))] : [];
        if (!times.length && validTime(item.update_time)) times = [item.update_time];
        setText(r.time, times.length ? times.join(' / ') : '时间未注明');
        const caption = text(item.schedule);
        setText(r.caption, schedules.length > 1 ? `${schedules.length} 条排期 · ${caption}` : caption);
        const details = schedules.map((event) => `${event.time || '时间未注明'} · ${audienceNames[event.audience]}${event.caption ? ` · ${event.caption}` : ''}`).join('\n');
        const staleNote = isStale ? '旧缓存：以下为上次核验的排期，可能已变化，请到平台原页确认。' : '';
        const freeProgress = text(item.free_progress) ? `已核验免费进度：${item.free_progress}` : '';
        const description = [staleNote, text(item.overview), details, freeProgress].filter(Boolean).join('\n');
        setText(r.description, description); r.title.title = [text(item.title), description].join('\n');
        r.time.title = details; r.caption.title = details; r.access.title = details;
        r.cache.hidden = !isStale; r.cache.title = staleNote;
        const hasRating = typeof item.rating === 'number' && Number.isFinite(item.rating) && item.rating >= 0 && item.rating <= 10;
        const score = hasRating ? item.rating.toFixed(1) : '暂无';
        setText(r.rating, `★ ${score}`); r.rating.dataset.available = String(hasRating);
        r.rating.setAttribute('aria-label', hasRating ? `TMDB 评分 ${score} / 10` : 'TMDB 评分暂无');
        syncPosters(r, item, retryFailed);
        const tmdb = mediaId(item.tmdb_id);
        const detail = detailURL(item.detail_url, item);
        // 当前没有详情入口时，保留经过平台白名单校验的原页入口。
        const originalPage = !text(item.detail_url) ? platformURL(item.url, item.source) : '';
        setLink(r.open, detail || originalPage, `${text(item.title)} · ${originalPage ? `前往${names[item.source]}原页（新窗口）` : '媒体详情'}`);
        if (originalPage) {
            delete r.open.dataset.mediaProfileLink; r.open.target = '_blank'; r.open.rel = 'noopener noreferrer';
        } else {
            r.open.dataset.mediaProfileLink = ''; r.open.removeAttribute('target'); r.open.removeAttribute('rel');
        }
        setLink(r.mapping, profileURL('tmdb', tmdb), 'TMDB 资料');
        r.mapping.classList.toggle('is-mapped', Boolean(tmdb));
        const mappingLabel = tmdb ? 'TMDB 已映射' : item.mapping_status === 'pending' ? 'TMDB 匹配中'
            : item.mapping_status === 'not_configured' ? 'TMDB 未配置' : 'TMDB 未匹配';
        setText(r.mappingLabel, mappingLabel);
        r.mapping.title = !tmdb && mediaId(item.douban_id) ? `${mappingLabel} · 已匹配豆瓣资料` : mappingLabel;
        r.mapping.setAttribute('aria-label', r.mapping.title);
        r.watchIdentity = watchIdentity(item); r.watchTitle = text(item.title);
        r.watchYear = /^\d{4}$/.test(text(item.year)) ? item.year : '';
        if (r.watchIdentity) {
            const identity = r.watchIdentity;
            const state = watchStates.get(identity.key);
            if (!state) watchStates.set(identity.key, {active: identity.in_watchlist, pending: false, revision: 0});
            else if (!state.pending && state.revision <= requestWatchRevision) state.active = identity.in_watchlist;
        }
        paintWatch(r);
        return r;
    }
    function buildWeek(start, today) {
        const dates = weekDates(start);
        if (!dates.includes(selectedDay)) selectedDay = dates.includes(today) ? today : dates[0];
        setText(byId('calendar-week'), `${start.slice(5).replace('-', '/')} — ${dates[6].slice(5).replace('-', '/')} · 上海时间`);
        for (const [date, value] of days) {
            if (!dates.includes(date)) { value.grid.remove(); value.tab.remove(); days.delete(date); }
        }
        dates.forEach((date, index) => {
            let record = days.get(date);
            if (!record) {
                const grid = node('div', 'discovery-grid wc-grid'); grid.id = `calendar-day-${date}`; grid.dataset.date = date;
                grid.setAttribute('role', 'tabpanel'); grid.tabIndex = 0;
                const tab = node('button', 'discovery-source-tab wc-weekday'); tab.type = 'button'; tab.dataset.date = date;
                tab.id = `calendar-tab-${date}`; tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', grid.id);
                grid.setAttribute('aria-labelledby', tab.id);
                const time = node('time', '', date.slice(5).replace('-', '/')); time.dateTime = date;
                tab.append(node('span', '', labels[index]), time);
                tab.addEventListener('click', () => { selectedDay = date; applyFilters(); });
                const empty = node('div', 'wc-empty'); const emptyTitle = node('strong', '', '正在读取本周排期');
                const emptyText = node('p', '', '请稍候，动漫将按平台提供的日期显示。');
                const retry = node('button', 'jump-btn', '重新读取'); retry.type = 'button'; retry.hidden = true;
                retry.addEventListener('click', () => load(true));
                empty.append(emptyTitle, emptyText, retry); grid.append(empty);
                byId('calendar-days').append(grid); byId('calendar-weekdays').append(tab);
                record = {grid, tab, empty, emptyTitle, emptyText, retry, label: labels[index]}; days.set(date, record);
            }
            record.tab.classList.toggle('is-today', date === today);
            record.tab.setAttribute('aria-label', `${date} ${labels[index]}${date === today ? ' 今天' : ''}`);
        });
    }
    function eligible(item) {
        // 内容分类只接受动漫；动漫剧集的 TMDB/豆瓣详情与收藏身份仍为 tv。
        return item && typeof item.stable_id === 'string' && item.stable_id.length > 0 && text(item.title)
            && Object.hasOwn(names, item.source) && item.category === 'animation';
    }
    function unavailableSources(sources = []) {
        // partial 仅表示有限覆盖；不能把它（或 stale）误报为来源不可用。
        return Object.keys(names).filter((id) => sources.some((item) => item?.id === id && item.status === 'unavailable'));
    }
    function applyFilters() {
        const source = byId('calendar-platform').value;
        const unavailable = unavailableSources(snapshot?.sources);
        const selectedUnavailable = unavailable.includes(source);
        const noSources = unavailable.length === Object.keys(names).length;
        for (const {card} of cards.values()) card.hidden = source !== 'all' && card.dataset.source !== source;
        for (const [date, r] of days) {
            const selected = date === selectedDay;
            r.grid.hidden = !selected; r.tab.classList.toggle('is-active', selected);
            r.tab.setAttribute('aria-selected', String(selected)); r.tab.tabIndex = selected ? 0 : -1;
            const count = [...r.grid.children].filter((el) => el.classList.contains('wc-card') && !el.hidden).length;
            r.empty.hidden = count > 0; r.retry.hidden = !failed; setButtonBusy(r.retry, isCalendarBusy());
            setText(r.emptyTitle, selectedUnavailable ? `${names[source]}排期暂不可用` : failed || noSources ? '排期暂不可用'
                : snapshot ? `暂无可确认的${r.label}更新` : '正在读取本周排期');
            setText(r.emptyText, selectedUnavailable ? `未能获取${names[source]}排期，不代表当天没有更新。可切换其他平台或稍后刷新。`
                : failed || noSources ? '可以稍后重试；来源状态中可查看详细信息。'
                : snapshot ? '当前平台下暂无明确动漫排期，不代表全站当天没有更新。' : '请稍候，动漫将按平台提供的日期显示。');
            if (selected) {
                setText(byId('calendar-day-heading'), `${r.label}更新`);
                setText(byId('calendar-count'), snapshot ? `${count} 部` : failed ? '尚未获取' : '读取中');
            }
        }
    }
    function renderSources(sources) {
        const summaries = []; let degraded = false;
        for (const id of Object.keys(names)) {
            const item = sources.find((value) => value && value.id === id);
            const status = item && Object.hasOwn(statusNames, item.status) ? item.status : 'unavailable';
            const el = sourcePanel.querySelector(`[data-source="${id}"]`); el.dataset.status = status;
            setText(el.querySelector('.wc-source-status'), statusNames[status]);
            setText(el.querySelector('.wc-source-message'), text(item?.message) || (status === 'unavailable' ? '来源暂不可用' : '平台公开排期，不代表全站完整覆盖'));
            const fetched = timestamp(item?.fetched_at);
            setText(el.querySelector('.wc-source-time'), fetched ? `上次成功 ${fetched} · 上海时间` : '尚无成功同步');
            summaries.push(`${names[id]}${statusNames[status]}`);
            degraded ||= ['partial', 'stale', 'unavailable'].includes(status);
        }
        sourceToggle.dataset.tone = degraded ? 'warning' : 'normal';
        sourceToggle.setAttribute('aria-label', `来源状态：${summaries.join('；')}`); sourceToggle.title = summaries.join('；');
    }
    function validate(data) {
        if (!data || data.timezone !== 'Asia/Shanghai' || !validDate(data.today) || !validDate(data.week_start)
            || !Array.isArray(data.days) || data.days.length !== 7 || !Array.isArray(data.sources) || typeof data.refreshing !== 'boolean') throw new Error('invalid calendar');
        const dates = weekDates(data.week_start);
        if (!data.days.every((day, index) => day && day.date === dates[index] && day.weekday === index + 1 && Array.isArray(day.items))) throw new Error('invalid dates');
        return data;
    }
    function render(data, requestWatchRevision, retryFailed) {
        // 在同步更新前取当前焦点，不跨请求保存，避免回包时抢回用户已移开的焦点。
        const focused = root.contains(document.activeElement) ? document.activeElement : null;
        snapshot = data; failed = false; buildWeek(data.week_start, data.today);
        const used = new Set();
        for (const day of data.days) {
            const r = days.get(day.date); let cursor = r.grid.firstElementChild; const seen = new Set();
            for (const item of day.items) {
                if (!eligible(item) || seen.has(item.stable_id)) continue;
                seen.add(item.stable_id); const record = updateCard(item, day.date, requestWatchRevision, retryFailed); used.add(record.card.dataset.key);
                if (record.card !== cursor) r.grid.insertBefore(record.card, cursor);
                cursor = record.card.nextElementSibling;
            }
        }
        for (const [key, record] of cards) if (!used.has(key)) { record.card.remove(); cards.delete(key); }
        renderSources(data.sources); applyFilters();
        // insertBefore 会让被移动节点的子控件失焦；仅恢复仍可见、可聚焦的同一控件。
        if (focused && document.activeElement === document.body && focused.isConnected && root.contains(focused)
            && focused.tabIndex >= 0 && !focused.matches(':disabled') && !focused.closest('[hidden]')
            && focused.getClientRects().length && getComputedStyle(focused).visibility === 'visible') {
            focused.focus({preventScroll: true});
        }
        const updated = timestamp(data.updated_at);
        setText(byId('calendar-sync'), updated ? `最近同步 ${updated} · 上海时间` : '最近同步：尚无成功同步');
        const unavailable = unavailableSources(data.sources);
        const availability = unavailable.length ? `${unavailable.map((id) => names[id]).join('、')}暂不可用 · ${unavailable.length === Object.keys(names).length ? '请稍后重试' : '可切换其他来源'}` : '';
        setStatus(data.refreshing ? '同步中 · 已保留当前内容' : availability || '以平台排期为准 · 不代表已播出', !data.refreshing && availability ? 'warning' : 'normal');
    }
    function isCalendarBusy() { return inFlight || Boolean(snapshot?.refreshing); }
    function setButtonBusy(button, busy) {
        button.setAttribute('aria-disabled', String(busy)); button.setAttribute('aria-busy', String(busy));
    }
    function setBusy() {
        const busy = isCalendarBusy();
        setButtonBusy(refresh, busy);
        refresh.title = busy ? '同步中，保留当前内容' : '刷新日历'; refresh.setAttribute('aria-label', refresh.title);
        byId('calendar-stage').setAttribute('aria-busy', String(busy));
        for (const r of days.values()) setButtonBusy(r.retry, busy);
    }
    function stopTimer() { clearTimeout(timer); timer = null; }
    function schedulePoll() {
        stopTimer(); if (document.hidden || inFlight || !snapshot?.refreshing) return;
        const seconds = Number(snapshot.retry_after);
        timer = setTimeout(() => load(false), Math.min(Math.max(5, Number.isFinite(seconds) ? seconds : 5) * 1000, 2147483647));
    }
    async function load(force) {
        // ARIA 不阻止 click；刷新与重试入口共用真实状态守卫，不能依赖 DOM 属性单飞。
        if (force && isCalendarBusy()) return;
        if (document.hidden) { resumeNeeded = true; return; }
        stopTimer(); controller?.abort(); controller = new AbortController();
        const activeController = controller; const current = ++version; const requestWatchRevision = watchRevision;
        inFlight = true; setBusy();
        if (snapshot) setStatus('同步中 · 已保留当前内容');
        const timeout = setTimeout(() => activeController.abort(), 25000);
        try {
            const response = await fetch(force ? '/api/discovery/calendar/refresh' : '/api/discovery/calendar', {
                method: force ? 'POST' : 'GET', credentials: 'same-origin', signal: activeController.signal,
                headers: force ? {'Accept': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''} : {'Accept': 'application/json'},
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = validate(await response.json());
            if (current !== version || document.hidden) return;
            resumeNeeded = false; render(data, requestWatchRevision, force);
        } catch (_) {
            if (current !== version || document.hidden) return;
            failed = true;
            setStatus(snapshot ? '同步失败 · 已保留上次内容，可重试' : '日历暂不可用，请重试', 'error');
            if (!snapshot) renderSources([]);
            sourceToggle.dataset.tone = 'error'; sourceToggle.title = '日历请求失败，点击查看来源状态';
            sourceToggle.setAttribute('aria-label', sourceToggle.title);
            if (snapshot) snapshot = {...snapshot, refreshing: false};
            applyFilters();
        } finally {
            clearTimeout(timeout);
            if (current === version) { inFlight = false; setBusy(); schedulePoll(); }
        }
    }
    function setSourcePanel(open, returnFocus = false) {
        sourcePanel.hidden = !open; sourceToggle.setAttribute('aria-expanded', String(open));
        if (!open && returnFocus) sourceToggle.focus({preventScroll: true});
    }
    sourceToggle.addEventListener('click', () => setSourcePanel(sourcePanel.hidden));
    byId('calendar-source-close').addEventListener('click', () => setSourcePanel(false, true));
    document.addEventListener('pointerdown', (event) => { if (!sourcePanel.hidden && !sourcePanel.contains(event.target) && !sourceToggle.contains(event.target)) setSourcePanel(false); });
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !sourcePanel.hidden) { event.preventDefault(); setSourcePanel(false, true); } });
    byId('calendar-platform').addEventListener('change', applyFilters);
    byId('calendar-weekdays').addEventListener('keydown', (event) => {
        const tabs = [...byId('calendar-weekdays').children]; const index = tabs.indexOf(event.target);
        if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? 6 : (index + (event.key === 'ArrowRight' ? 1 : -1) + 7) % 7;
        tabs[next].click(); tabs[next].focus({preventScroll: true});
    });
    refresh.addEventListener('click', () => load(true));
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopTimer(); resumeNeeded ||= inFlight;
            if (inFlight) { ++version; controller?.abort(); inFlight = false; setBusy(); }
        } else if (resumeNeeded || !snapshot) load(false);
        else schedulePoll();
    });
    window.addEventListener('pagehide', () => { stopTimer(); ++version; controller?.abort(); });
    window.addEventListener('pageshow', (event) => { if (event.persisted) { inFlight = false; resumeNeeded = true; load(false); } });
    byId('discovery-detail-dialog')?.addEventListener('close', () => { if (!document.hidden) load(false); });
    if (validDate(root.dataset.weekStart)) { buildWeek(root.dataset.weekStart, root.dataset.today); applyFilters(); }
    load(false);
})();
