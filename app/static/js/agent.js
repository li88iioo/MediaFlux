// Media Agent 工作区：确定性工具查询、结构化结果与确认门交互。
(function () {
    const page = document.querySelector('.agent-page');
    if (!page) return;

    const consoleNode = page.querySelector('.agent-console');
    const transcript = document.getElementById('agentTranscript');
    const composer = document.getElementById('agentComposer');
    const promptInput = document.getElementById('agentPrompt');
    const sendButton = document.getElementById('agentSend');
    const stopButton = document.getElementById('agentStop');
    const newSessionButton = document.getElementById('agentNewSession');
    const resumeLatestSessionButton = document.getElementById('agentResumeLatestSession');
    const railToggleButton = document.getElementById('toggleAgentRail');
    const historyRail = document.getElementById('agentHistoryRail');
    const capabilityNode = document.getElementById('agentCapabilities');
    const sessionListNode = document.getElementById('agentSessionList');
    const sessionCountNode = document.getElementById('agentSessionCount');
    const sessionStatusNode = document.getElementById('agentSessionStatus');
    const responseStatusNode = document.getElementById('agentResponseStatus');
    const MIN_PENDING_MS = 320;
    const AGENT_CANCEL_TIMEOUT_MS = 1500;
    const MAX_RENDERED_ITEMS = 8;
    const MAX_TRANSCRIPT_MESSAGES = 120;
    const MAX_GENERIC_SECTIONS = 4;
    const confirmationTimers = new Map();
    let conversationGeneration = 0;
    let activeController = null;
    let activeQuery = null;
    let historyController = null;
    let requestInFlight = false;
    let confirmationInFlight = false;
    let sessionResetInFlight = false;
    let sessionsLoadGeneration = 0;
    let sessionsController = null;
    let agentSessionId = createAgentSessionId();
    let latestSessionId = '';
    let restoringHistory = false;
    let historyReturnFocus = true;
    let visualViewportFrame = 0;
    const directToolActions = new WeakMap();
    const confirmationPrepareActions = new WeakMap();

    function createAgentSessionId() {
        if (globalThis.crypto?.randomUUID) {
            return globalThis.crypto.randomUUID().replaceAll('-', '');
        }
        const bytes = new Uint8Array(24);
        if (globalThis.crypto?.getRandomValues) {
            globalThis.crypto.getRandomValues(bytes);
            return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
        }
        return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`
            .replace(/[^a-z0-9]/gi, '')
            .padEnd(24, '0')
            .slice(0, 64);
    }

    function sessionPayload(payload = {}, sessionId = agentSessionId) {
        return {...payload, session_id: sessionId};
    }

    function createAgentRequestId() {
        return `rq_${createAgentSessionId()}`.slice(0, 64);
    }

    function node(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined && text !== null) element.textContent = String(text);
        return element;
    }

    function cleanDisplayLine(value) {
        return String(value || '')
            .replace(/\[([^\]]+)]\((?:https?:\/\/)?[^)]+\)/g, '$1')
            .replace(/^#{1,6}\s+/, '')
            .replace(/^[*+]\s+/, '- ')
            .replace(/^\d{1,2}[.)、]\s+/, '- ')
            .replace(/^(?:(?:\*{1,2}|_{1,2})(?:结论|Agent\s*解读|依据|下一步(?:建议)?|关键数据(?:与范围)?)\s*[:：]?(?:\*{1,2}|_{1,2})\s*[:：]?\s*|(?:结论|Agent\s*解读|依据|下一步(?:建议)?|关键数据(?:与范围)?)(?:\s*[:：]\s*|\s*$))/i, '')
            .replace(/\*\*|__|`/g, '')
            .replace(/(^|\s)[*_]([^*_\n]+)[*_](?=$|\s|[，。；;：:])/g, '$1$2')
            .replace(/\s*[（(]\s*(?:内部状态|内部检查|系统内部状态|仅供内部参考)\s*[）)]\s*/gi, '')
            .trim();
    }

    function normalizeDisplaySource(value) {
        return String(value || '')
            .replace(/\r\n?/g, '\n')
            .replace(/(?:^|\s+)(?:(?:\*{1,2}|_{1,2})(?:结论|Agent\s*解读|依据|下一步(?:建议)?|关键数据(?:与范围)?)\s*[:：]?(?:\*{1,2}|_{1,2})\s*[:：]?\s*|(?:结论|Agent\s*解读|依据|下一步(?:建议)?|关键数据(?:与范围)?)\s*[:：]\s*)/gi, '\n\n')
            .replace(/\s+[*+•]\s+(?=(?:\*{1,2}|_{1,2})?\S)/g, '\n- ')
            .replace(/\s+\d{1,2}[.)、]\s+(?=\S)/g, '\n- ')
            .trim()
            .slice(0, 1800);
    }

    function readableParagraphs(value) {
        const text = cleanDisplayLine(value);
        if (!text) return [];
        const sentences = text.match(/[^。！？!?]+(?:[。！？!?]+|$)/g)?.map((item) => item.trim()).filter(Boolean) || [text];
        const paragraphs = [];
        let current = '';
        let sentenceCount = 0;
        sentences.forEach((sentence) => {
            if (current && (sentenceCount >= 2 || current.length + sentence.length > 150)) {
                paragraphs.push(current);
                current = '';
                sentenceCount = 0;
            }
            current += sentence;
            sentenceCount += 1;
        });
        if (current) paragraphs.push(current);
        return paragraphs;
    }

    function renderTextBlocks(value, className = 'agent-rich-text', options = {}) {
        const source = normalizeDisplaySource(value);
        if (!source) return null;
        const wrapper = node('div', className);
        const promoteFirst = Boolean(options.promoteFirst);
        let list = null;
        let paragraphCount = 0;

        const flushList = () => {
            if (!list) return;
            if (list.childElementCount) wrapper.append(list);
            list = null;
        };
        const appendParagraph = (paragraph) => {
            const isLead = promoteFirst && paragraphCount === 0 && paragraph.length <= 120;
            wrapper.append(node('p', isLead ? 'agent-answer-lead' : '', paragraph));
            paragraphCount += 1;
        };

        source.split('\n').forEach((rawLine) => {
            const line = cleanDisplayLine(rawLine);
            if (!line) {
                flushList();
                return;
            }
            const bullet = line.match(/^[-*•]\s+(.+)$/);
            if (bullet) {
                if (!list) list = node('ul');
                list.append(node('li', '', bullet[1].trim()));
                return;
            }
            flushList();
            readableParagraphs(line).forEach(appendParagraph);
        });
        flushList();
        return wrapper.childElementCount ? wrapper : null;
    }

    function replaceTextBlocks(target, value, options = {}) {
        if (!target) return;
        const rendered = renderTextBlocks(value, 'agent-rich-text', options);
        target.replaceChildren(...(rendered ? Array.from(rendered.childNodes) : []));
    }

    function icon(name) {
        const element = document.createElement('i');
        element.setAttribute('data-lucide', name);
        element.setAttribute('aria-hidden', 'true');
        return element;
    }

    function refreshIcons(root) {
        if (restoringHistory) return;
        window.renderLucideIcons?.(root || page);
    }

    function sleep(milliseconds) {
        return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    function transcriptIsNearBottom() {
        return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 120;
    }

    function scrollToLatest({force = true} = {}) {
        if (restoringHistory || (!force && !transcriptIsNearBottom())) return;
        requestAnimationFrame(() => {
            transcript.scrollTop = transcript.scrollHeight;
        });
    }

    function syncConversationLayout() {
        const isEmpty = !transcript.querySelector('.agent-message');
        consoleNode?.classList.toggle('is-empty', isEmpty);
        promptInput.placeholder = isEmpty
            ? (promptInput.dataset.emptyPlaceholder || '询问 MediaFlux')
            : (promptInput.dataset.activePlaceholder || '继续描述或调整任务');
    }

    function focusResult(element) {
        if (!element) return;
        element.tabIndex = -1;
        element.focus({preventScroll: true});
    }

    function announceSessionStatus(message) {
        if (!sessionStatusNode) return;
        sessionStatusNode.textContent = '';
        window.requestAnimationFrame(() => {
            sessionStatusNode.textContent = String(message || '');
        });
    }

    function announceResponseStatus(message) {
        if (!responseStatusNode || restoringHistory) return;
        responseStatusNode.textContent = '';
        window.requestAnimationFrame(() => {
            responseStatusNode.textContent = String(message || '');
        });
    }

    function responseAnnouncement(payload) {
        if (payload?.mode === 'confirmation_required') return 'Agent 已完成检查，需要确认后才能执行操作。';
        const result = payload?.result && typeof payload.result === 'object' ? payload.result : {};
        const display = payload?.display && typeof payload.display === 'object' ? payload.display : {};
        const summary = cleanDisplayLine(display.summary || result.summary || payload?.answer || '').slice(0, 160);
        return summary ? `Agent 回答已生成：${summary}` : 'Agent 回答已生成。';
    }

    async function discardStaleConfirmation(payload, sessionId) {
        const confirmationId = String(
            payload?.action_plan?.plan_id
            || payload?.confirmation?.confirmation_id
            || '',
        ).trim();
        if (confirmationId) await discardConfirmation(confirmationId, sessionId);
    }

    const STATUS_LABELS = {
        accepted: '已提交', active: '运行中', attention: '需关注', completed: '已完成',
        conflict: '状态冲突', degraded: '能力受限', empty: '暂无数据', failed: '失败',
        disabled: '已停用', environment_override: '环境变量覆盖', healthy: '健康', idle: '空闲',
        no_changes: '无需变更', not_configured: '未配置', partial: '部分可用',
        ready: '就绪', running: '运行中', success: '成功', unavailable: '暂不可用',
        unknown: '未知', unsupported: '暂不支持', waiting: '等待中', warning: '提醒',
        outcome_unknown: '结果待确认', selection_required: '请选择目标',
        updates_available: '发现缺集', up_to_date: '已是最新', inconclusive: '无法确认',
        not_missing: '未确认缺失', cannot_determine: '无法判断', not_supported: '暂不支持',
        ambiguous: '结果不唯一', unmapped: '未映射', not_found: '未找到', found: '已找到',
        incomplete: '配置不完整', blocked: '依赖受阻',
        comparison_unavailable: '需人工核对', review_required: '待核对',
        clarification_required: '需要说明', confirmation_required: '等待确认',
        pending: '等待执行', retry_wait: '等待重试', cancelled: '已取消',
        not_run: '尚未运行', not_created: '尚未创建', succeeded: '已完成',
    };

    const WORKSPACE_OVERVIEW_TOOLS = new Set([
        'workspace.health', 'workspace.todo', 'workspace.briefing',
    ]);
    const WORKFLOW_ACTIONS = {
        'workspace.briefing': {label: '查看系统简报', prompt: '系统简报'},
        'config.diagnose': {label: '检查项目配置', prompt: '检查项目配置'},
        'config.diagnose_media_servers': {label: '检查媒体服务器', prompt: '检查媒体服务器状态'},
        'downloads.diagnose_queue': {label: '检查下载队列', prompt: '检查下载队列状态'},
        'rss.diagnose': {label: '诊断 RSS', prompt: '诊断 RSS 订阅'},
        'guangya.organize.status': {label: '查看整理状态', prompt: '查看光鸭云盘整理状态'},
        'strm.status': {label: '查看 STRM 状态', prompt: '查看 STRM 同步进度'},
        'strm.triage_failures': {label: '查看失败分诊', prompt: '查看 STRM 失败分诊'},
        'local_media.diagnose': {label: '诊断本地媒体', prompt: '诊断本地媒体'},
        'automation.diagnose_pipeline': {label: '诊断自动化链路', prompt: '诊断自动化链路'},
        'indexer.diagnose_readiness': {label: '诊断资源站', prompt: '诊断资源站是否就绪'},
    };
    const AREA_LABELS = {
        workspace: '工作区', configuration: '项目配置', media_servers: '媒体服务器',
        downloads: '下载队列', rss: 'RSS 订阅', organize: '云盘整理', strm: 'STRM 同步',
        local_media: '本地媒体', indexer: '资源站', indexers: '资源站', automation: '自动化链路',
    };
    const AREA_ICONS = {
        workspace: 'layout-dashboard', configuration: 'settings-2', media_servers: 'server-cog',
        downloads: 'circle-arrow-down', rss: 'rss', organize: 'folder-sync', strm: 'scan-line',
        local_media: 'folder-search-2', indexer: 'radio-tower', indexers: 'radio-tower', automation: 'workflow',
    };

    function normalizeStatus(status) {
        return String(status || 'unknown').trim().toLowerCase();
    }

    function statusLabel(status) {
        const value = normalizeStatus(status);
        return STATUS_LABELS[value] || '暂无法确认';
    }

    function statusTone(status, ok) {
        const value = normalizeStatus(status);
        if (['unavailable', 'failed', 'error', 'unsupported'].includes(value)) return 'is-error';
        if (['partial', 'attention', 'warning', 'degraded', 'disabled', 'incomplete', 'blocked', 'conflict', 'not_configured', 'environment_override', 'updates_available', 'inconclusive', 'not_missing', 'cannot_determine', 'not_supported', 'ambiguous', 'unmapped', 'clarification_required', 'outcome_unknown', 'selection_required'].includes(value)) return 'is-warning';
        if (['healthy', 'success', 'completed', 'accepted', 'ready', 'active', 'running', 'no_changes', 'up_to_date', 'found'].includes(value)) return 'is-good';
        if (ok === false) return 'is-error';
        return '';
    }

    function readableKey(value) {
        return String(value || '')
            .replace(/_/g, ' ')
            .replace(/\b\w/g, (character) => character.toUpperCase());
    }

    function primitiveText(value) {
        if (value === true) return '是';
        if (value === false) return '否';
        if (value === null || value === undefined || value === '') return '—';
        return String(value);
    }

    function primitiveEntries(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
        return Object.entries(value).filter(([, item]) => (
            item === null || ['string', 'number', 'boolean'].includes(typeof item)
        ));
    }

    function objectTitle(value, fallback) {
        if (!value || typeof value !== 'object') return fallback;
        for (const key of ['display_name', 'title', 'name', 'label', 'source', 'status', 'code']) {
            if (value[key] !== undefined && value[key] !== null && String(value[key]).trim()) {
                return String(value[key]);
            }
        }
        return fallback;
    }

    function renderFactGrid(entries) {
        const list = node('dl', 'agent-data-grid');
        entries.slice(0, 12).forEach(([key, value]) => {
            const fact = node('div', 'agent-data-fact');
            fact.append(node('dt', '', readableKey(key)), node('dd', '', primitiveText(value)));
            list.append(fact);
        });
        return list;
    }

    function renderArraySection(key, values) {
        const section = node('section', 'agent-data-section');
        section.append(node('strong', '', `${readableKey(key)} · ${values.length}`));
        const list = node('ul', 'agent-data-list');
        values.slice(0, MAX_RENDERED_ITEMS).forEach((value, index) => {
            const item = node('li');
            if (value && typeof value === 'object' && !Array.isArray(value)) {
                const title = node('strong', '', objectTitle(value, `项目 ${index + 1}`));
                const details = primitiveEntries(value)
                    .filter(([name]) => !['display_name', 'title', 'name', 'label'].includes(name))
                    .slice(0, 4)
                    .map(([name, entry]) => `${readableKey(name)}: ${primitiveText(entry)}`)
                    .join(' · ');
                item.append(title);
                if (details) item.append(node('span', '', details));
            } else {
                item.textContent = primitiveText(value);
            }
            list.append(item);
        });
        if (values.length > MAX_RENDERED_ITEMS) {
            list.append(node('li', '', `另有 ${values.length - MAX_RENDERED_ITEMS} 项未展开`));
        }
        section.append(list);
        return section;
    }

    function safeCount(value) {
        if (Array.isArray(value)) return value.length;
        const count = Number(value);
        return Number.isFinite(count) && count >= 0 ? count : 0;
    }

    function createOverviewMetric(label, value, iconName, tone = '') {
        const item = node('div', `agent-overview-metric ${tone}`.trim());
        const mark = node('span', 'agent-overview-metric-icon');
        mark.append(icon(iconName));
        const copy = node('span', 'agent-overview-metric-copy');
        copy.append(node('small', '', label), node('strong', '', value));
        item.append(mark, copy);
        return item;
    }

    function renderCoverage(coverage) {
        if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage)) return null;
        const groups = [
            ['unavailable', '暂不可用'],
            ['not_configured', '未配置'],
            ['disabled', '已停用'],
            ['not_probed', '本次未探测'],
        ];
        const visible = groups.filter(([key]) => Array.isArray(coverage[key]) && coverage[key].length);
        if (!visible.length) return null;
        const section = node('section', 'agent-overview-coverage');
        section.append(node('strong', '', '覆盖边界'));
        const list = node('div', 'agent-overview-coverage-list');
        visible.forEach(([key, label]) => {
            const item = node('span');
            item.append(node('b', '', label), document.createTextNode(` ${coverage[key].join('、')}`));
            list.append(item);
        });
        section.append(list);
        return section;
    }

    function renderWorkspaceOverview(data) {
        if (!data || typeof data !== 'object' || !Array.isArray(data.areas)) return null;
        const wrapper = node('div', 'agent-overview');
        const metrics = node('div', 'agent-overview-metrics');
        if (Object.hasOwn(data, 'attention_total')) {
            metrics.append(createOverviewMetric('需关注', safeCount(data.attention_total), 'circle-alert', 'is-attention'));
        }
        if (Object.hasOwn(data, 'active_total')) {
            metrics.append(createOverviewMetric('处理中', safeCount(data.active_total), 'activity', 'is-active'));
        }
        if (Object.hasOwn(data, 'waiting_total')) {
            metrics.append(createOverviewMetric('等待中', safeCount(data.waiting_total), 'clock-3'));
        }
        metrics.append(createOverviewMetric('已检查区域', data.areas.length, 'scan-search'));
        wrapper.append(metrics);

        const section = node('section', 'agent-overview-areas');
        const heading = node('div', 'agent-overview-section-head');
        heading.append(node('strong', '', '工作区状态'), node('span', '', `${data.areas.length} 个区域`));
        section.append(heading);
        const list = node('div', 'agent-overview-list');
        data.areas.slice(0, MAX_RENDERED_ITEMS).forEach((area) => {
            if (!area || typeof area !== 'object') return;
            const source = String(area.source || 'workspace');
            const row = node('article', 'agent-overview-row');
            const mark = node('span', 'agent-overview-row-icon');
            mark.append(icon(AREA_ICONS[source] || 'circle-dashed'));
            const copy = node('div', 'agent-overview-row-copy');
            const title = node('div', 'agent-overview-row-title');
            title.append(
                node('strong', '', AREA_LABELS[source] || readableKey(source)),
                node('span', `agent-status ${statusTone(area.status, area.status !== 'unavailable')}`, statusLabel(area.status)),
            );
            copy.append(title);
            const counts = [];
            if (Object.hasOwn(area, 'attention_count')) counts.push(`${safeCount(area.attention_count)} 项关注`);
            if (Object.hasOwn(area, 'active_count')) counts.push(`${safeCount(area.active_count)} 项处理中`);
            if (Object.hasOwn(area, 'waiting_count')) counts.push(`${safeCount(area.waiting_count)} 项等待`);
            if (counts.length) copy.append(node('p', '', counts.join(' · ')));
            if (Array.isArray(area.reason_codes) && area.reason_codes.length) {
                const reasons = node('div', 'agent-overview-reasons');
                area.reason_codes.slice(0, 4).forEach((reason) => reasons.append(node('span', '', readableKey(reason))));
                copy.append(reasons);
            }
            row.append(mark, copy);
            const action = WORKFLOW_ACTIONS[area.next_tool];
            if (action && ['attention', 'unavailable', 'partial', 'warning', 'degraded', 'disabled', 'not_configured'].includes(normalizeStatus(area.status))) {
                const button = node('button', 'agent-overview-action', action.label);
                button.type = 'button';
                button.dataset.agentPrompt = action.prompt;
                button.append(icon('arrow-up-right'));
                row.append(button);
            }
            list.append(row);
        });
        section.append(list);
        wrapper.append(section);
        const coverage = renderCoverage(data.coverage);
        if (coverage) wrapper.append(coverage);
        return wrapper;
    }

    function renderWorkspaceNextActions(data) {
        if (!data || typeof data !== 'object' || !Array.isArray(data.actions)) return null;
        const actions = data.actions.filter((action) => (
            action
            && typeof action === 'object'
            && /^[a-z0-9_]{3,64}$/.test(String(action.action_key || ''))
            && action.risk === 'read'
            && action.requires_confirmation === false
        )).slice(0, MAX_RENDERED_ITEMS);
        const wrapper = node('div', 'agent-next-actions');
        const heading = node('div', 'agent-next-actions-head');
        const title = node('div', 'agent-next-actions-title');
        const mark = node('span', 'agent-next-actions-mark');
        mark.append(icon(actions.length ? 'route' : 'circle-check-big'));
        const copy = node('div');
        copy.append(
            node('strong', '', actions.length ? '建议先处理这些事项' : '工作区暂时无需介入'),
            node('span', '', actions.length
                ? '每项执行前都会重新核对实时状态，仅运行对应的只读诊断。'
                : '当前快照没有需要立即检查的行动。'),
        );
        title.append(mark, copy);
        heading.append(title, node('span', 'agent-next-actions-count', `${actions.length} 项`));
        wrapper.append(heading);
        if (!actions.length) return wrapper;

        const list = node('div', 'agent-next-actions-list');
        actions.forEach((action) => {
            const source = String(action.source || 'workspace');
            const actionKey = String(action.action_key);
            const row = node('article', 'agent-next-action');
            const sourceMark = node('span', 'agent-next-action-icon');
            sourceMark.append(icon(AREA_ICONS[source] || 'scan-search'));
            const actionCopy = node('div', 'agent-next-action-copy');
            const eyebrow = node('span', 'agent-next-action-source', AREA_LABELS[source] || readableKey(source));
            const actionTitle = node('strong', '', String(action.label || '查看详情'));
            const why = node('p', '', String(action.why || '重新核对当前状态并返回诊断结果。'));
            const meta = node('span', 'agent-next-action-meta', `${safeCount(action.attention_count)} 项需关注 · 只读检查`);
            actionCopy.append(eyebrow, actionTitle, why, meta);
            const button = node('button', 'agent-next-action-button', '开始检查');
            button.type = 'button';
            button.dataset.agentWorkspaceAction = actionKey;
            button.dataset.agentWorkspaceLabel = String(action.label || '工作区检查');
            button.append(icon('arrow-up-right'));
            row.append(sourceMark, actionCopy, button);
            list.append(row);
        });
        wrapper.append(list);
        return wrapper;
    }

    const EPISODE_AUDIT_TOOLS = new Set([
        'library.audit_episodes', 'library.check_updates',
    ]);
    const SEARCH_TOOLS = new Set([
        'workspace.search', 'library.search', 'discovery.search', 'indexer.search_resources',
    ]);
    const PUBLIC_NARRATIVE_SOURCES = new Set(['llm', 'system', 'native']);
    const COLLAPSIBLE_NARRATIVE_DETAIL_TOOLS = new Set([
        'workspace.search', 'library.search', 'discovery.search', 'web.search',
    ]);
    const SEARCH_SOURCE_LABELS = {
        library: '本地媒体', rss: 'RSS 订阅', downloads: '下载任务', organize: '整理任务',
        local_media: '本地媒体任务', discovery: '影视元数据', indexer: '资源站',
    };
    const SEARCH_SOURCE_ICONS = {
        library: 'library-big', rss: 'rss', downloads: 'circle-arrow-down', organize: 'folder-sync',
        local_media: 'folder-search-2', discovery: 'clapperboard', indexer: 'radio-tower',
    };

    function compactText(value, limit = 180) {
        const text = String(value || '').replace(/\s+/g, ' ').trim();
        return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…` : text;
    }

    function safeExternalUrl(value) {
        try {
            const parsed = new URL(String(value || ''));
            if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return '';
            return parsed.href;
        } catch (_error) {
            return '';
        }
    }

    function createMetric(label, value, detail = '') {
        const item = node('div', 'agent-media-metric');
        item.append(node('span', '', label), node('strong', '', primitiveText(value)));
        if (detail) item.append(node('small', '', detail));
        return item;
    }

    function renderSourceCoverage(sources) {
        if (!Array.isArray(sources) || !sources.length) return null;
        const section = node('section', 'agent-media-sources');
        const head = node('div', 'agent-section-head');
        head.append(node('strong', '', '媒体服务器覆盖'), node('span', '', `${sources.length} 个来源`));
        section.append(head);
        const list = node('div', 'agent-media-source-list');
        sources.slice(0, MAX_RENDERED_ITEMS).forEach((source) => {
            if (!source || typeof source !== 'object') return;
            const row = node('div', 'agent-media-source');
            const mark = node('span', 'agent-search-group-icon');
            mark.append(icon('server'));
            const copy = node('div', 'agent-media-source-copy');
            copy.append(
                node('strong', '', source.server_name || source.server_type || '媒体服务器'),
                node('small', '', [
                    source.server_type,
                    `${safeCount(source.local_episode_count)} 集本地收录`,
                    source.truncated ? '结果已截断' : '',
                ].filter(Boolean).join(' · ')),
            );
            row.append(mark, copy, node('span', `agent-status ${statusTone(source.status, source.status !== 'unavailable')}`, statusLabel(source.status)));
            list.append(row);
        });
        section.append(list);
        return section;
    }

    function renderSeriesEpisodeCount(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !Object.hasOwn(data, 'local_episode_count')) return null;

        const rawCount = data.local_episode_count;
        const count = rawCount === null || rawCount === '' ? null : Number(rawCount);
        const hasCount = Number.isFinite(count) && count >= 0;
        const sources = Array.isArray(data.sources) ? data.sources : [];
        const seasons = Array.isArray(data.seasons)
            ? data.seasons.filter((item) => item && typeof item === 'object').slice(0, 12)
            : [];
        const wrapper = node('section', `agent-series-count${hasCount ? '' : ' is-empty'}`);
        const hero = node('div', 'agent-series-count-hero');
        const mark = node('span', 'agent-series-count-mark');
        mark.append(icon(hasCount ? 'list-video' : (result.status === 'not_found' ? 'search-x' : 'server-off')));
        const copy = node('div', 'agent-series-count-copy');
        copy.append(
            node('span', 'agent-series-count-eyebrow', hasCount ? 'LOCAL LIBRARY' : 'LOCAL LOOKUP'),
            node('strong', '', data.title || '本地剧集统计'),
        );
        const sourceCount = safeCount(data.matched_source_count);
        const seasonCount = safeCount(data.season_count);
        const meta = hasCount
            ? [seasonCount ? `${seasonCount} 季` : '', sourceCount ? `${sourceCount} 个媒体服务器` : '', '不含特别篇'].filter(Boolean).join(' · ')
            : (result.status === 'not_found' ? '已查询当前启用的 Jellyfin / Emby' : '媒体服务器暂未返回可统计数据');
        copy.append(node('span', 'agent-series-count-meta', meta));
        hero.append(mark, copy);
        if (hasCount) {
            const value = node('div', 'agent-series-count-value');
            value.append(node('strong', '', String(count)), node('span', '', '集'));
            hero.append(value);
        } else {
            hero.append(node('span', `agent-status ${statusTone(result.status, false)}`, statusLabel(result.status)));
        }
        wrapper.append(hero);

        if (hasCount && seasons.length) {
            const seasonList = node('div', 'agent-series-count-seasons');
            seasons.forEach((season) => {
                const seasonNumber = safeCount(season.season);
                const episodeCount = safeCount(season.count);
                const item = node('div', 'agent-series-count-season');
                item.append(
                    node('span', '', `第 ${seasonNumber} 季`),
                    node('strong', '', `${episodeCount} 集`),
                );
                seasonList.append(item);
            });
            wrapper.append(seasonList);
        }

        const needsSourceDetails = sources.length > 1 || sources.some((source) => !['ready', 'unmapped'].includes(String(source?.status || '')));
        if (needsSourceDetails) {
            const coverage = renderSourceCoverage(sources);
            if (coverage) wrapper.append(coverage);
        }

        const notes = [];
        if (safeCount(data.ignored_specials)) notes.push(`未计入第 0 季特别篇 ${safeCount(data.ignored_specials)} 项`);
        if (safeCount(data.ignored_unknown)) notes.push(`有 ${safeCount(data.ignored_unknown)} 个条目缺少季集编号`);
        if (data.truncated === true) notes.push('读取达到当前安全上限，数字可能是下限');
        if (notes.length) {
            const note = node('p', 'agent-series-count-note', notes.join('；'));
            note.prepend(icon('info'));
            wrapper.append(note);
        }
        return wrapper;
    }

    function renderEpisodeAudit(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        if (!Object.hasOwn(data, 'missing_count') && !Object.hasOwn(data, 'check_definition') && !Array.isArray(data.sources)) return null;
        const wrapper = node('div', 'agent-media-report');
        const intro = node('header', 'agent-media-report-head');
        const title = node('div');
        title.append(
            node('span', 'agent-media-eyebrow', data.media_type === 'movie' ? 'MOVIE CHECK' : 'EPISODE AUDIT'),
            node('h4', '', data.title || data.query || '剧集完整性核对'),
        );
        const scope = [
            data.media_type === 'movie' ? '' : (data.season ? `第 ${data.season} 季` : '全部季度'),
            data.as_of ? `截至 ${data.as_of}` : '',
        ].filter(Boolean).join(' · ');
        if (scope) title.append(node('p', '', scope));
        intro.append(title);
        if (data.tmdb_id) intro.append(node('span', 'agent-id-badge', `TMDB ${data.tmdb_id}`));
        wrapper.append(intro);

        if (data.check_definition === 'movie_library_presence_with_resource_followup') {
            const metrics = node('div', 'agent-media-metrics');
            metrics.append(
                createMetric('同名条目', safeCount(data.exact_match_count), data.local_match_status === 'found' ? '标题已精确核验' : '尚未精确命中'),
                createMetric('可能匹配', safeCount(data.possible_match_count), '仅作为人工核对线索'),
                createMetric('可用服务器', safeCount(data.available_server_count), safeCount(data.unavailable_server_count) ? `${safeCount(data.unavailable_server_count)} 个暂不可用` : '查询已完成'),
            );
            wrapper.append(metrics);

            const sourceItems = [];
            (Array.isArray(data.sources) ? data.sources : []).forEach((source) => {
                const items = Array.isArray(source?.items) ? source.items : [];
                if (!items.length) {
                    if (source?.status === 'unavailable') sourceItems.push({source, item: null});
                    return;
                }
                items.forEach((item) => sourceItems.push({source, item}));
            });
            if (sourceItems.length) {
                const sourceSection = node('section', 'agent-media-sources');
                const sourceHead = node('div', 'agent-section-head');
                const hiddenMatches = safeCount(data.matches_truncated);
                sourceHead.append(node('strong', '', '媒体库核对'), node('span', '', `${sourceItems.length} 条线索${hiddenMatches ? ` · 另 ${hiddenMatches} 条未展开` : ''}`));
                const list = node('div', 'agent-media-source-list');
                sourceItems.slice(0, MAX_RENDERED_ITEMS).forEach(({source, item}) => {
                    const row = node('div', 'agent-media-source');
                    const mark = node('span', 'agent-search-group-icon');
                    mark.append(icon(item ? 'film' : 'server-off'));
                    const copy = node('div', 'agent-media-source-copy');
                    copy.append(
                        node('strong', '', item?.title || source?.server_name || '媒体服务器'),
                        node('small', '', [
                            source?.server_name || source?.server_type,
                            item?.year,
                            item?.match === 'exact_title' ? '同名' : (item ? '可能匹配' : '暂不可用'),
                        ].filter(Boolean).join(' · ')),
                    );
                    row.append(mark, copy, node('span', `agent-status ${statusTone(source?.status, source?.status === 'ready')}`, statusLabel(source?.status)));
                    list.append(row);
                });
                sourceSection.append(sourceHead, list);
                wrapper.append(sourceSection);
            }

            const boundary = node('section', 'agent-capability-boundary');
            const boundaryMark = node('span');
            boundaryMark.append(icon('shield-alert'));
            const boundaryCopy = node('div');
            boundaryCopy.append(
                node('strong', '', '本次不自动判断“电影版本已更新”'),
                node('p', '', data.comparison?.reason || '本地版本缺少可可靠比较的发行版、分辨率与编码基线。'),
            );
            boundary.append(boundaryMark, boundaryCopy);
            wrapper.append(boundary);

            const followup = (Array.isArray(data.resource_followups) ? data.resource_followups : []).find((action) => {
                const args = action?.arguments;
                return action?.tool === 'indexer.search_resources'
                    && typeof args?.title === 'string' && args.title.trim().length > 0 && args.title.length <= 120
                    && args?.media_type === 'movie';
            });
            if (followup) {
                const argumentsValue = {
                    title: followup.arguments.title.trim(),
                    media_type: 'movie',
                };
                const year = String(followup.arguments.year || '').trim();
                if (/^\d{4}$/.test(year)) argumentsValue.year = year;
                const actions = node('div', 'agent-movie-actions');
                const button = node('button', 'agent-config-action agent-movie-followup', String(followup.label || '搜索资源站候选').slice(0, 80));
                button.type = 'button';
                button.dataset.agentDirectTool = 'true';
                button.setAttribute('aria-busy', 'false');
                button.append(icon('search'));
                directToolActions.set(button, {tool: followup.tool, label: button.textContent, arguments: argumentsValue});
                actions.append(button);
                wrapper.append(actions);
            }
            return wrapper;
        }

        if (data.check_definition === 'movie_version_comparison_unavailable') {
            const boundary = node('section', 'agent-capability-boundary');
            const mark = node('span');
            mark.append(icon('shield-alert'));
            const copy = node('div');
            copy.append(
                node('strong', '', '当前只核对是否入库，不判断电影版本更新'),
                node('p', '', '媒体服务器与 TMDB 没有提供足够可靠的发行版、压制版或文件版本比较依据。'),
            );
            boundary.append(mark, copy);
            wrapper.append(boundary);
            return wrapper;
        }

        const auditCounts = ['expected_aired', 'local_episode_count', 'missing_count'].map((key) => Number(data[key]));
        const hasReliableCounts = auditCounts.every((value) => Number.isFinite(value) && value >= 0);
        if (!hasReliableCounts) {
            const boundary = node('section', 'agent-capability-boundary');
            const mark = node('span');
            mark.append(icon('shield-question'));
            const copy = node('div');
            copy.append(
                node('strong', '', '本次审计未形成可靠缺集结论'),
                node('p', '', result.error || `当前状态：${statusLabel(result.status)}。请先处理媒体服务器、映射或数据源问题后重试。`),
            );
            boundary.append(mark, copy);
            wrapper.append(boundary);
            const incompleteCoverage = renderSourceCoverage(data.sources);
            if (incompleteCoverage) wrapper.append(incompleteCoverage);
            return wrapper;
        }

        const metrics = node('div', 'agent-media-metrics');
        metrics.append(
            createMetric('已播普通集', auditCounts[0], 'TMDB 截止日期口径'),
            createMetric('本地收录', auditCounts[1], '已启用媒体服务器'),
            createMetric('确认缺失', auditCounts[2], auditCounts[2] ? '需要后续处理' : '未发现缺集'),
        );
        wrapper.append(metrics);

        if (Array.isArray(data.missing_sample) && data.missing_sample.length) {
            const missing = node('section', 'agent-missing-episodes');
            const head = node('div', 'agent-section-head');
            head.append(node('strong', '', '缺失集数'), node('span', '', data.missing_sample_truncated ? '仅展示部分结果' : `${data.missing_sample.length} 集`));
            const followups = new Map();
            (Array.isArray(data.resource_followups) ? data.resource_followups : []).forEach((action) => {
                const argumentsValue = action?.arguments;
                const season = Number(argumentsValue?.season);
                const episode = Number(argumentsValue?.episode);
                const query = String(argumentsValue?.query || '').trim();
                if (action?.tool !== 'library.search_missing_episode_resources'
                    || !Number.isInteger(season) || season < 1 || season > 100
                    || !Number.isInteger(episode) || episode < 1 || episode > 1000
                    || !query || query.length > 120) return;
                const normalizedArguments = {query, season, episode};
                const tmdbId = String(argumentsValue?.tmdb_id || '').trim();
                const asOf = String(argumentsValue?.as_of || '').trim();
                if (/^[0-9]{1,10}$/.test(tmdbId)) normalizedArguments.tmdb_id = tmdbId;
                if (/^\d{4}-\d{2}-\d{2}$/.test(asOf)) normalizedArguments.as_of = asOf;
                followups.set(`${season}:${episode}`, {
                    tool: action.tool,
                    label: String(action.label || `搜索 S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')} 资源`).slice(0, 80),
                    arguments: normalizedArguments,
                });
            });
            const chips = node('div', 'agent-episode-chips');
            data.missing_sample.slice(0, 24).forEach((episodeValue) => {
                const seasonValue = safeCount(episodeValue?.season);
                const episodeNumber = safeCount(episodeValue?.episode);
                const season = String(seasonValue).padStart(2, '0');
                const number = String(episodeNumber).padStart(2, '0');
                const label = `S${season}E${number}`;
                const action = followups.get(`${seasonValue}:${episodeNumber}`);
                if (!action) {
                    chips.append(node('span', '', label));
                    return;
                }
                const button = node('button', 'agent-episode-chip', label);
                button.type = 'button';
                button.setAttribute('aria-label', action.label);
                button.setAttribute('aria-busy', 'false');
                button.append(icon('search'));
                directToolActions.set(button, action);
                chips.append(button);
            });
            if (data.missing_sample.length > 24 || data.missing_sample_truncated) {
                chips.append(node('span', 'is-more', '更多结果未展开'));
            }
            missing.append(head, chips);
            if (data.resource_followups_truncated && followups.size) {
                const note = node('p', 'agent-episode-followup-note');
                note.append(
                    icon('info'),
                    node('span', '', `当前提供前 ${followups.size} 集快捷搜索；其余可按季度重新检查。`),
                );
                missing.append(note);
            }
            wrapper.append(missing);
        }
        const coverage = renderSourceCoverage(data.sources);
        if (coverage) wrapper.append(coverage);
        return wrapper;
    }

    const LIBRARY_AUDIT_TOOLS = new Set([
        'library.audit_library_episodes', 'library.start_episode_audit',
        'agent.job_status', 'library.patrol_status',
    ]);

    function episodeCode(item) {
        const season = Number(item?.season);
        const episode = Number(item?.episode);
        if (!Number.isInteger(season) || season < 0 || !Number.isInteger(episode) || episode < 1) return '';
        return `S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')}`;
    }

    function libraryAuditStatus(data, result = {}) {
        const taskStatus = normalizeStatus(data?.task_status);
        const outcome = normalizeStatus(data?.outcome || data?.patrol_status || result?.status);
        if (['pending', 'running', 'retry_wait', 'cancelled', 'failed'].includes(taskStatus)) return taskStatus;
        if (taskStatus === 'not_created') return 'not_run';
        return outcome;
    }

    function libraryAuditFinding(item) {
        if (!item || typeof item !== 'object') return null;
        const title = compactText(item.title || '未命名剧集', 96);
        const missingCount = safeCount(item.missing_count);
        const row = node('article', 'agent-library-audit-finding');
        const mark = node('span', 'agent-library-audit-finding-mark');
        mark.append(icon(item.status === 'inconclusive' ? 'circle-help' : 'list-x'));
        const copy = node('div', 'agent-library-audit-finding-copy');
        const head = node('div', 'agent-library-audit-finding-head');
        head.append(
            node('strong', '', title),
            node('span', `agent-status ${statusTone(item.status, item.status !== 'inconclusive')}`,
                item.status === 'inconclusive' ? '暂无法确认' : `缺 ${missingCount} 集`),
        );
        copy.append(head);
        const samples = Array.isArray(item.missing_sample)
            ? item.missing_sample.map(episodeCode).filter(Boolean).slice(0, 8)
            : [];
        const detail = samples.length
            ? `${samples.join(' · ')}${item.missing_sample_truncated ? ' · …' : ''}`
            : (item.status === 'inconclusive' ? '本地库存或 TMDB 清单不完整' : '缺失集号待进一步核对');
        copy.append(node('p', '', detail));
        row.append(mark, copy);
        return row;
    }

    function renderLibraryAudit(toolName, data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        const state = libraryAuditStatus(data, result);
        const checked = safeCount(data.checked_series_count ?? data.progress_current);
        const total = safeCount(data.progress_total);
        const updates = safeCount(data.updates_available_count);
        const missing = safeCount(data.missing_episode_count);
        const inconclusive = safeCount(data.inconclusive_count);
        const unmapped = safeCount(data.unmapped_series_count);
        const localSeries = safeCount(data.local_series_count);
        const eligibleSeries = safeCount(data.comparison_eligible_count ?? data.mapped_series_count);
        const running = ['pending', 'running', 'retry_wait'].includes(state);
        const confirmation = normalizeStatus(result.status) === 'confirmation_required';
        const wrapper = node('div', 'agent-library-audit agent-media-report');

        const overview = node('section', 'agent-library-audit-overview');
        const overviewMark = node('span', 'agent-library-audit-overview-mark');
        overviewMark.append(icon(confirmation ? 'shield-check' : running ? 'loader-circle' : 'library-big'));
        const overviewCopy = node('div', 'agent-library-audit-overview-copy');
        let heading = '媒体库缺集核对';
        let description = '读取媒体服务器的本地剧集库存，并与 TMDB 截至指定日期的已播清单核对。';
        if (confirmation) {
            heading = '后台巡检尚未开始';
            description = `确认后创建可恢复任务，每批最多核对 ${safeCount(data.max_series)} 部；不会自动下载、删除或整理文件。`;
        } else if (state === 'pending') {
            heading = '后台巡检正在排队';
            description = '任务尚未形成缺集结论；开始运行后会分批保存进度。';
        } else if (state === 'running') {
            heading = '正在核对本地库存与已播清单';
            description = total > 0 ? `当前进度 ${checked}/${total}；完成前不会把零值解释为没有缺集。` : `已检查 ${checked} 部，正在统计完整范围。`;
        } else if (state === 'retry_wait') {
            heading = '巡检将在稍后自动重试';
            description = '当前结果不是最终结论，请检查媒体服务器与 TMDB 连接。';
        } else if (state === 'not_run') {
            heading = '尚无全库巡检结果';
            description = data.enabled === false ? '自动巡检当前未启用；仍可进行即时只读核对。' : '任务尚未运行，不代表媒体库没有缺集。';
        } else if (state === 'inconclusive' || state === 'unavailable' || state === 'not_configured' || state === 'failed') {
            heading = '本次核对覆盖不完整';
            description = '下方数字仅代表已成功核对的范围，不能据此判断整个媒体库完整。';
        } else if (state === 'updates_available') {
            heading = `发现 ${updates} 部剧集存在已播缺集`;
            description = '仅列出安全摘要；后续找资源与提交下载仍需要单独操作和确认。';
        } else if (state === 'up_to_date') {
            heading = '已核对范围内暂未发现已播缺集';
            description = '该结论只覆盖本次成功映射且数据完整的剧集。';
        }
        overviewCopy.append(node('strong', '', heading), node('p', '', description));
        overview.append(overviewMark, overviewCopy, node('span', `agent-status ${statusTone(state, result.ok)}`, statusLabel(state)));
        wrapper.append(overview);

        if (!confirmation) {
            const metrics = node('div', 'agent-library-audit-metrics agent-media-metrics');
            const checkedValue = running && checked === 0 ? '统计中' : checked;
            if (!running && ['inconclusive', 'empty'].includes(state) && (localSeries || unmapped)) {
                metrics.append(
                    createMetric('媒体库读取', `${localSeries} 部`, '来自 Jellyfin / Emby 的本地剧集'),
                    createMetric('可比较', `${eligibleSeries} 部`, '具备可靠 TMDB 映射'),
                    createMetric('缺少映射', `${unmapped} 部`, '这不等于媒体库为空'),
                );
            } else {
                metrics.append(
                    createMetric('实际核对', checkedValue, total > 0 && running ? `计划范围 ${total} 部` : '成功读取并完成对比'),
                    createMetric('确认缺集', running ? '—' : `${updates} 部`, running ? '等待任务完成' : '只统计已播缺集'),
                    createMetric('缺失集数', running ? '—' : missing, inconclusive || unmapped ? '另有覆盖不完整项' : '可继续按剧集找资源'),
                );
            }
            wrapper.append(metrics);
        }

        const findings = Array.isArray(data.findings) ? data.findings : [];
        if (findings.length) {
            const section = node('section', 'agent-library-audit-findings');
            const head = node('div', 'agent-section-head');
            head.append(node('strong', '', '缺集明细'), node('span', '', `${Math.min(findings.length, 8)} 项摘要`));
            const list = node('div', 'agent-library-audit-finding-list');
            findings.slice(0, 8).forEach((item) => {
                const finding = libraryAuditFinding(item);
                if (finding) list.append(finding);
            });
            section.append(head, list);
            if (data.findings_truncated || findings.length > 8) {
                section.append(node('p', 'agent-library-audit-note', '这里只展示前 8 项；其余结果仍保留在巡检记录中。'));
            }
            wrapper.append(section);
        }

        const coverageNotes = [];
        if (unmapped) coverageNotes.push(`${unmapped} 部缺少可靠 TMDB 映射`);
        if (inconclusive) coverageNotes.push(`${inconclusive} 部暂时无法确认`);
        if (data.continuation_pending) coverageNotes.push('本批进度已保存，后台将继续下一批');
        if (data.deadline_exhausted) coverageNotes.push('本次达到时间上限，结果只覆盖已完成部分');
        if (coverageNotes.length) {
            const coverage = node('div', 'agent-library-audit-coverage');
            coverage.append(icon('info'), node('span', '', coverageNotes.join('；')));
            wrapper.append(coverage);
        }
        return wrapper;
    }

    function createResourceActions(item) {
        const resultId = String(item?.result_id || '');
        if (!/^[A-Za-z0-9_-]{16,128}$/.test(resultId)) return null;
        const actions = node('div', 'agent-resource-actions');
        [['qb', 'qBittorrent', 'download'], ['guangya', '光鸭', 'cloud-download']].forEach(([target, label, iconName]) => {
            const button = node('button', 'agent-resource-action', label);
            button.type = 'button';
            button.dataset.agentResourceId = resultId;
            button.dataset.agentTarget = target;
            button.append(icon(iconName));
            actions.append(button);
        });
        return actions;
    }

    function searchItemTitle(item, fallback = '未命名结果') {
        return item?.display_name || item?.title || item?.name || fallback;
    }

    function mediaMetadata(item) {
        const episode = item?.episode ? `第 ${item.episode} 集` : '';
        const season = item?.season ? `第 ${item.season} 季` : '';
        return [item?.media_type, item?.year, item?.series_name, season, episode].filter(Boolean).join(' · ');
    }

    function renderSearchItem(item, source) {
        const row = node('article', 'agent-search-item');
        const copy = node('div', 'agent-search-item-copy');
        copy.append(node('strong', '', searchItemTitle(item)));
        const meta = [];
        if (source === 'indexer') {
            meta.push(item.site_name || item.site_id, item.size_text);
            if (Number.isFinite(Number(item.seeders))) meta.push(`${safeCount(item.seeders)} 做种`);
            if (item.published_at) meta.push(item.published_at);
        } else if (source === 'discovery') {
            meta.push(item.provider, mediaMetadata(item));
            if (item.rating !== null && item.rating !== undefined && item.rating !== '') meta.push(`${item.rating_source || '评分'} ${item.rating}`);
        } else {
            meta.push(mediaMetadata(item), item.status ? statusLabel(item.status) : '', item.published_at || item.updated_at || '');
        }
        const compactMeta = meta.filter(Boolean).join(' · ');
        if (compactMeta) copy.append(node('small', '', compactMeta));
        const description = compactText(item?.overview || item?.description || '', 150);
        if (description) copy.append(node('p', '', description));
        row.append(copy);
        if (source === 'indexer') {
            const actions = createResourceActions(item);
            if (actions) row.append(actions);
        } else if (item?.rating !== null && item?.rating !== undefined && item?.rating !== '') {
            row.append(node('span', 'agent-search-score', String(item.rating)));
        }
        return row;
    }

    function renderSearchGroup({source, title, status = 'success', items = [], returned, truncated = false}) {
        const section = node('section', 'agent-search-group');
        const head = node('header', 'agent-search-group-head');
        const identity = node('div', 'agent-search-group-identity');
        const mark = node('span', 'agent-search-group-icon');
        mark.append(icon(SEARCH_SOURCE_ICONS[source] || 'search'));
        const copy = node('div');
        copy.append(node('strong', '', title || SEARCH_SOURCE_LABELS[source] || readableKey(source)));
        copy.append(node('small', '', `${safeCount(returned ?? items.length)} 项${truncated ? ' · 结果已截断' : ''}`));
        identity.append(mark, copy);
        head.append(identity, node('span', `agent-status ${statusTone(status, status !== 'unavailable')}`, statusLabel(status)));
        section.append(head);
        if (Array.isArray(items) && items.length) {
            const list = node('div', 'agent-search-list');
            items.slice(0, MAX_RENDERED_ITEMS).forEach((item) => list.append(renderSearchItem(item, source)));
            if (items.length > MAX_RENDERED_ITEMS || truncated) {
                list.append(node('div', 'agent-search-more', `当前展开 ${Math.min(items.length, MAX_RENDERED_ITEMS)} 项，更多结果未展示`));
            }
            section.append(list);
        } else {
            const normalized = normalizeStatus(status);
            const unavailable = ['unavailable', 'disabled', 'not_configured', 'failed', 'error'].includes(normalized);
            const message = unavailable
                ? '该来源本次未能完成检索。'
                : (normalized === 'partial' ? '该来源仅完成部分检索，暂未返回可展示结果。' : '该来源没有匹配结果。');
            section.append(node('p', 'agent-search-empty', message));
        }
        return section;
    }

    function renderSearchErrors(errors) {
        if (!Array.isArray(errors) || !errors.length) return null;
        const section = node('section', 'agent-search-errors');
        const head = node('div', 'agent-section-head');
        head.append(node('strong', '', '未完成来源'), node('span', '', `${errors.length} 项`));
        const list = node('div', 'agent-search-error-list');
        errors.slice(0, 4).forEach((error) => {
            const source = error && typeof error === 'object'
                ? error.provider || error.site_name || error.site_id || error.source || '数据源'
                : '数据源';
            const code = error && typeof error === 'object' ? error.code || error.status || 'unavailable' : 'unavailable';
            list.append(node('span', '', `${source} · ${statusLabel(code)}`));
        });
        section.append(head, list);
        return section;
    }

    function renderIndexerSearch(data, verification = null, resultStatus = 'success') {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        const wrapper = node('div', 'agent-search-report');
        if (verification) {
            const verify = node('section', `agent-verification ${verification.verified_missing ? 'is-verified' : ''}`.trim());
            const mark = node('span');
            mark.append(icon(verification.verified_missing ? 'badge-check' : 'shield-question'));
            const copy = node('div');
            copy.append(
                node('strong', '', verification.verified_missing ? '已确认目标为已播缺集' : '未通过缺集确认门'),
                node('p', '', [verification.title, verification.season ? `第 ${verification.season} 季` : '', verification.episode ? `第 ${verification.episode} 集` : ''].filter(Boolean).join(' · ')),
            );
            verify.append(mark, copy);
            wrapper.append(verify);
        }
        const summary = node('div', 'agent-search-summary');
        summary.append(
            createMetric('返回资源', safeCount(data.returned ?? data.items?.length), data.cached ? '来自短期缓存' : '实时检索'),
            createMetric('成功站点', safeCount(data.providers_succeeded ?? data.sites_succeeded), `${safeCount(data.providers_attempted ?? data.sites_attempted)} 个已尝试`),
            createMetric('检索状态', statusLabel(resultStatus), data.has_more ? '站点仍有更多结果' : '当前检索范围'),
        );
        wrapper.append(summary);
        wrapper.append(renderSearchGroup({
            source: 'indexer', title: '多站资源', status: resultStatus,
            items: Array.isArray(data.items) ? data.items : [], returned: data.returned, truncated: Boolean(data.has_more),
        }));
        const errors = renderSearchErrors(data.errors);
        if (errors) wrapper.append(errors);
        return wrapper;
    }

    function renderWebSearch(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        const wrapper = node('div', 'agent-web-report');
        const context = node('section', 'agent-web-context');
        const identity = node('div', 'agent-web-context-copy');
        const mark = node('span', 'agent-web-context-icon');
        mark.append(icon('globe-2'));
        const copy = node('div');
        copy.append(
            node('span', 'agent-web-eyebrow', 'CONTROLLED WEB SEARCH'),
            node('strong', '', compactText(data.query || '网页搜索', 160)),
            node('p', '', '结果来自受控外部搜索，仅展示摘要；打开网页后请自行核验来源。'),
        );
        identity.append(mark, copy);
        const badges = node('div', 'agent-web-badges');
        const provider = String(data.provider || 'tavily').trim();
        const depth = String(data.search_depth || '').trim();
        const topic = String(data.topic || '').trim();
        badges.append(node('span', '', provider || 'Tavily'));
        if (topic) badges.append(node('span', '', topic === 'news' ? '新闻' : '通用'));
        if (depth) badges.append(node('span', '', depth === 'advanced' ? '深度检索' : '基础检索'));
        badges.append(node('span', data.cached ? 'is-cached' : '', data.cached ? '缓存命中' : '实时结果'));
        context.append(identity, badges);
        wrapper.append(context);

        const results = Array.isArray(data.results) ? data.results : [];
        if (!results.length) {
            const empty = node('div', 'agent-web-empty');
            empty.append(icon('search-x'), node('span', '', '当前关键词没有可展示的安全网页结果。'));
            wrapper.append(empty);
            return wrapper;
        }

        const list = node('div', 'agent-web-results');
        results.slice(0, MAX_RENDERED_ITEMS).forEach((item) => {
            if (!item || typeof item !== 'object') return;
            const href = safeExternalUrl(item.url);
            if (!href) return;
            const article = node('article', 'agent-web-result');
            const title = node('a', 'agent-web-result-title');
            title.href = href;
            title.target = '_blank';
            title.rel = 'noopener noreferrer';
            title.append(node('span', '', compactText(item.title || item.source || '网页结果', 240)), icon('arrow-up-right'));
            const meta = node('div', 'agent-web-result-meta');
            meta.append(node('span', '', compactText(item.source || new URL(href).hostname, 100)));
            if (item.published_date) meta.append(node('time', '', compactText(item.published_date, 40)));
            const score = Number(item.score);
            if (Number.isFinite(score) && score > 0) meta.append(node('span', '', `相关度 ${Math.round(Math.min(score, 1) * 100)}%`));
            const snippet = compactText(item.snippet || '', 360);
            article.append(title, meta);
            if (snippet) article.append(node('p', '', snippet));
            list.append(article);
        });
        if (!list.childElementCount) {
            const empty = node('div', 'agent-web-empty');
            empty.append(icon('shield-alert'), node('span', '', '搜索结果未通过安全链接校验，已全部隐藏。'));
            wrapper.append(empty);
            return wrapper;
        }
        wrapper.append(list);
        const footer = node('div', 'agent-web-foot');
        footer.append(
            node('span', '', `${safeCount(data.total ?? results.length)} 条结果`),
            node('span', '', data.cached ? '未消耗新额度' : `本次额度 ${safeCount(data.credits_used)} credit`),
            node('span', '', Number.isFinite(Number(data.elapsed_ms)) ? `${safeCount(data.elapsed_ms)} ms` : statusLabel(result.status)),
        );
        wrapper.append(footer);
        return wrapper;
    }

    function renderUnifiedSearch(toolName, data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        if (toolName === 'indexer.search_resources') return renderIndexerSearch(data, null, result.status);
        const wrapper = node('div', 'agent-search-report');
        const summary = node('div', 'agent-search-summary');
        summary.append(
            createMetric('查询', data.query || '—', toolName === 'workspace.search' ? '跨工作区追踪' : '确定性搜索'),
            createMetric('返回结果', safeCount(data.returned ?? data.total), data.has_more ? '仍有更多结果' : '当前结果集'),
        );
        if (toolName === 'workspace.search') {
            summary.append(createMetric('访问范围', data.network_accessed ? '联网 + 本地' : '仅本地', data.database_accessed ? '已查询数据库' : '未查询数据库'));
        } else if (toolName === 'discovery.search') {
            summary.append(createMetric('成功来源', safeCount(data.providers_succeeded), `${safeCount(data.providers_attempted)} 个已尝试`));
        } else {
            summary.append(createMetric('匹配状态', statusLabel(data.match_status || 'unknown'), `${Array.isArray(data.sources) ? data.sources.length : 0} 个媒体服务`));
        }
        wrapper.append(summary);

        if (toolName === 'workspace.search') {
            (Array.isArray(data.sections) ? data.sections : []).forEach((section) => {
                wrapper.append(renderSearchGroup({
                    source: section.source, status: section.status, items: section.items,
                    returned: section.returned, truncated: Boolean(section.truncated),
                }));
            });
        } else if (toolName === 'library.search') {
            (Array.isArray(data.sources) ? data.sources : []).forEach((source) => {
                wrapper.append(renderSearchGroup({
                    source: 'library', title: source.server_name || source.server_type || '媒体服务器',
                    status: source.status, items: source.items, returned: source.returned,
                    truncated: safeCount(source.returned) > MAX_RENDERED_ITEMS,
                }));
            });
        } else if (toolName === 'discovery.search') {
            wrapper.append(renderSearchGroup({
                source: 'discovery', title: '外部影视元数据', status: result.status || 'unknown',
                items: data.items, returned: data.returned, truncated: Boolean(data.has_more),
            }));
            const errors = renderSearchErrors(data.errors);
            if (errors) wrapper.append(errors);
        }
        return wrapper.childElementCount ? wrapper : null;
    }

    function renderMissingEpisodeResources(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !data.verification) return null;
        const hasSearchContract = data.search && typeof data.search === 'object'
            && Array.isArray(data.search.items)
            && (Array.isArray(data.search.sites_attempted) || Object.hasOwn(data.search, 'returned'));
        if (hasSearchContract) return renderIndexerSearch(data.search, data.verification, result.status);
        const wrapper = node('div', 'agent-search-report');
        const verification = data.verification;
        const verify = node('section', 'agent-verification');
        const mark = node('span');
        mark.append(icon('shield-question'));
        const copy = node('div');
        copy.append(
            node('strong', '', '指定集尚未被可靠确认缺失'),
            node('p', '', [verification.title, verification.season ? `第 ${verification.season} 季` : '', verification.episode ? `第 ${verification.episode} 集` : ''].filter(Boolean).join(' · ')),
        );
        verify.append(mark, copy);
        wrapper.append(verify);
        if (verification.verified_missing) {
            const boundary = node('section', 'agent-capability-boundary');
            const boundaryMark = node('span');
            boundaryMark.append(icon('radio-tower'));
            const boundaryCopy = node('div');
            boundaryCopy.append(
                node('strong', '', '缺集已确认，但资源站检索未完成'),
                node('p', '', result.error || `当前状态：${statusLabel(result.status)}。这不代表资源站中没有相关资源。`),
            );
            boundary.append(boundaryMark, boundaryCopy);
            wrapper.append(boundary);
        }
        return wrapper;
    }

    function renderMissingSeasonResources(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !data.verification) return null;
        const wrapper = node('div', 'agent-search-report agent-season-resource-report');
        const verification = data.verification;
        const verified = Boolean(verification.verified_missing);
        const verify = node('section', `agent-verification${verified ? ' is-verified' : ''}`);
        const mark = node('span');
        mark.append(icon(verified ? 'shield-check' : 'shield-question'));
        const copy = node('div');
        copy.append(
            node('strong', '', verified ? '季度缺集已重新核验' : '该季缺集尚未可靠确认'),
            node('p', '', [verification.title, verification.season ? `第 ${verification.season} 季` : '', verification.as_of ? `截止 ${verification.as_of}` : ''].filter(Boolean).join(' · ')),
        );
        verify.append(mark, copy);
        wrapper.append(verify);

        const episodes = Array.isArray(data.episodes) ? data.episodes : [];
        if (!verified || !episodes.length) {
            const boundary = node('section', 'agent-capability-boundary');
            const boundaryMark = node('span');
            boundaryMark.append(icon('list-x'));
            const boundaryCopy = node('div');
            boundaryCopy.append(
                node('strong', '', result.status === 'not_missing' ? '当前季度无需补集' : '未执行批量资源站搜索'),
                node('p', '', result.error || result.summary || `当前状态：${statusLabel(result.status)}。`),
            );
            boundary.append(boundaryMark, boundaryCopy);
            wrapper.append(boundary);
            return wrapper;
        }

        const candidateCount = episodes.reduce((total, item) => {
            const items = Array.isArray(item?.search?.items) ? item.search.items : [];
            return total + items.length;
        }, 0);
        const metrics = node('div', 'agent-media-metrics');
        metrics.append(
            createMetric('确认缺集', safeCount(data.missing_total), '本次完整季度审计'),
            createMetric('本批处理', safeCount(data.processed), `${safeCount(data.failed)} 集搜索未完成`),
            createMetric('候选资源', candidateCount, safeCount(data.remaining) ? `剩余 ${safeCount(data.remaining)} 集` : '已覆盖当前批次'),
        );
        wrapper.append(metrics);

        const groups = node('div', 'agent-season-resource-groups');
        episodes.slice(0, 3).forEach((episodeValue) => {
            const search = episodeValue?.search && typeof episodeValue.search === 'object' ? episodeValue.search : {};
            const label = String(episodeValue?.episode_label || '').slice(0, 16)
                || `S${String(safeCount(episodeValue?.season)).padStart(2, '0')}E${String(safeCount(episodeValue?.episode)).padStart(2, '0')}`;
            const items = Array.isArray(search.items) ? search.items : [];
            const group = renderSearchGroup({
                source: 'indexer',
                title: `${label} · 资源站候选`,
                status: String(episodeValue?.status || 'unknown'),
                items,
                returned: items.length,
                truncated: Boolean(search.has_more),
            });
            group.classList.add('agent-season-resource-group');
            const errors = renderSearchErrors(search.errors);
            if (errors) group.append(errors);
            groups.append(group);
        });
        wrapper.append(groups);

        if (data.truncated || safeCount(data.remaining)) {
            const note = node('p', 'agent-episode-followup-note');
            note.append(icon('info'), document.createTextNode(`本次最多处理 3 集；其余 ${safeCount(data.remaining)} 集可再次按季度检索。`));
            wrapper.append(note);
        }
        return wrapper;
    }

    function renderConfigExplanation(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !data.label) return null;
        const wrapper = node('section', 'agent-config-report');
        const head = node('header', 'agent-config-report-head');
        const identity = node('div', 'agent-config-identity');
        const mark = node('span', 'agent-config-mark');
        mark.append(icon('settings-2'));
        const copy = node('div');
        copy.append(
            node('span', 'agent-config-eyebrow', 'CONFIGURATION MAP'),
            node('h4', '', String(data.label)),
        );
        if (data.purpose) copy.append(node('p', '', String(data.purpose)));
        identity.append(mark, copy);
        head.append(
            identity,
            node('span', `agent-status ${statusTone(data.status || result.status, result.ok)}`, statusLabel(data.status || result.status)),
        );
        wrapper.append(head);

        const required = Array.isArray(data.required_field_labels) ? data.required_field_labels : [];
        const missing = Array.isArray(data.missing_field_labels) ? data.missing_field_labels : [];
        const missingLabels = new Set(missing.map((item) => String(item)));
        const blocked = Array.isArray(data.blocked_capabilities) ? data.blocked_capabilities : [];
        const matrix = node('div', 'agent-config-matrix');

        const requirements = node('section', 'agent-config-section');
        requirements.append(node('strong', '', '必要条件'));
        if (required.length) {
            const chips = node('div', 'agent-config-chips');
            required.forEach((item) => {
                const label = String(item);
                const chip = node('span', missingLabels.has(label) ? 'is-missing' : 'is-present');
                chip.append(icon(missingLabels.has(label) ? 'circle-alert' : 'circle-check'), document.createTextNode(label));
                chips.append(chip);
            });
            requirements.append(chips);
        } else {
            requirements.append(node('p', '', '该功能由受控开关和依赖关系决定，不需要在此暴露配置字段。'));
        }

        const capabilities = node('section', 'agent-config-section');
        capabilities.append(node('strong', '', blocked.length ? '当前受影响能力' : '能力状态'));
        if (blocked.length) {
            const list = node('ul', 'agent-config-list');
            blocked.slice(0, MAX_RENDERED_ITEMS).forEach((item) => list.append(node('li', '', String(item))));
            capabilities.append(list);
        } else {
            capabilities.append(node('p', '', '必要条件已满足，当前未发现被配置阻断的能力。'));
        }
        matrix.append(requirements, capabilities);
        wrapper.append(matrix);

        if (data.managed_by_environment) {
            const notice = node('div', 'agent-config-notice');
            notice.append(icon('server-cog'), node('span', '', '此开关由运行环境管理，Agent 不会尝试覆盖部署配置。'));
            wrapper.append(notice);
        }

        const action = data.agent_action;
        const prompt = typeof action?.prompt === 'string' ? action.prompt.trim() : '';
        if (action?.supported === true
            && action.tool === 'config.set_feature_state'
            && action.requires_confirmation === true
            && prompt && prompt.length <= 160) {
            const actionBar = node('div', 'agent-config-action-bar');
            actionBar.append(node('span', '', '可由 Agent 生成变更预检，确认前不会写入配置。'));
            const button = node('button', 'agent-config-action', '生成开启预检');
            button.type = 'button';
            button.dataset.agentPrompt = prompt;
            button.append(icon('arrow-up-right'));
            actionBar.append(button);
            wrapper.append(actionBar);
        }
        return wrapper;
    }

    function renderConfigDiagnosis(data, result = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
        const issues = Array.isArray(data.issues)
            ? data.issues.filter((item) => item && typeof item === 'object' && item.message)
            : [];
        const wrapper = node('section', 'agent-config-diagnosis');
        if (!issues.length) {
            const copy = node('div');
            copy.append(
                node('strong', '', '关键配置项已通过'),
                node('p', '', '当前没有发现会阻止核心功能运行的配置问题。'),
            );
            wrapper.append(icon('circle-check'), copy);
            return wrapper;
        }
        const copy = node('div');
        copy.append(node('strong', '', `${issues.length} 项需要处理`));
        const list = node('ul');
        issues.slice(0, MAX_RENDERED_ITEMS).forEach((item) => {
            const message = cleanDisplayLine(item.message);
            if (message) list.append(node('li', '', message));
        });
        copy.append(list);
        wrapper.append(icon(result.ok ? 'circle-check' : 'circle-alert'), copy);
        return wrapper;
    }


    const READ_PLAN_LABELS = {
        'workspace.health': '工作区健康',
        'workspace.todo': '待办概览',
        'workspace.briefing': '系统简报',
        'workspace.next_actions': '建议行动',
        'config.diagnose': '项目配置',
        'config.diagnose_media_servers': '媒体服务器',
        'config.feature_summary': '功能状态',
        'downloads.diagnose_queue': '下载队列',
        'rss.diagnose': 'RSS 订阅',
        'strm.status': 'STRM 状态',
        'strm.diagnose': 'STRM 诊断',
        'strm.triage_failures': 'STRM 失败分诊',
        'local_media.diagnose': '本地媒体',
        'indexer.diagnose_readiness': '资源站',
        'automation.diagnose_pipeline': '自动化链路',
        'library.patrol_status': '媒体库巡检',
        'library.count_series_episodes': '统计本地集数',
        'agent.action_history': '操作历史',
    };

    function renderReadPlan(data, {attentionOnly = false} = {}) {
        if (!data || typeof data !== 'object' || !Array.isArray(data.steps)) return null;
        const steps = data.steps
            .filter((step) => step && typeof step === 'object')
            .filter((step) => !attentionOnly || step?.result?.ok !== true)
            .slice(0, 4);
        if (!steps.length) return null;

        const wrapper = node('section', 'agent-read-plan');
        const head = node('header', 'agent-read-plan-head');
        const title = node('div');
        title.append(
            node('span', 'agent-read-plan-eyebrow', attentionOnly ? '需要留意' : '核对明细'),
            node('strong', '', attentionOnly ? '未完成的检查' : '复合检查'),
        );
        head.append(title, node('span', 'agent-read-plan-count', `${steps.length} 个步骤`));
        wrapper.append(head);

        const list = node('ol', 'agent-read-plan-list');
        steps.forEach((step, index) => {
            const result = step.result && typeof step.result === 'object' ? step.result : {};
            const toolName = String(step.tool_name || '');
            const item = node('li', `agent-read-plan-step ${statusTone(result.status, result.ok)}`);
            const marker = node('span', 'agent-read-plan-index', String(index + 1).padStart(2, '0'));
            const copy = node('div', 'agent-read-plan-copy');
            const row = node('div', 'agent-read-plan-title');
            row.append(
                node('strong', '', READ_PLAN_LABELS[toolName] || '诊断步骤'),
                node('span', `agent-status ${statusTone(result.status, result.ok)}`, statusLabel(result.status)),
            );
            copy.append(row, node('p', '', String(result.summary || '该步骤未返回可展示摘要。').slice(0, 240)));
            const elapsed = Number(step.elapsed_ms);
            const meta = node('span', 'agent-read-plan-meta', Number.isFinite(elapsed) ? `${Math.max(0, elapsed)} ms` : '已完成');
            item.append(marker, copy, meta);
            list.append(item);
        });
        wrapper.append(list);
        return wrapper;
    }

    function renderSpecializedData(toolName, data, result = {}, {attentionOnly = false} = {}) {
        const name = String(toolName || '');
        if (name === 'agent.read_plan') return renderReadPlan(data, {attentionOnly});
        if (name === 'workspace.next_actions') return renderWorkspaceNextActions(data);
        if (WORKSPACE_OVERVIEW_TOOLS.has(name)) return renderWorkspaceOverview(data);
        if (name === 'library.count_series_episodes') return renderSeriesEpisodeCount(data, result);
        if (EPISODE_AUDIT_TOOLS.has(name)) return renderEpisodeAudit(data, result);
        if (LIBRARY_AUDIT_TOOLS.has(name)) return renderLibraryAudit(name, data, result);
        if (name === 'library.search_missing_episode_resources') return renderMissingEpisodeResources(data, result);
        if (name === 'library.search_missing_season_resources') return renderMissingSeasonResources(data, result);
        if (name === 'config.diagnose') return renderConfigDiagnosis(data, result);
        if (name === 'config.explain_component') return renderConfigExplanation(data, result);
        if (name === 'web.search') return renderWebSearch(data, result);
        if (SEARCH_TOOLS.has(name)) return renderUnifiedSearch(name, data, result);
        return null;
    }

    function renderData(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !Object.keys(data).length) return null;
        const wrapper = node('div', 'agent-data-sections');
        let sectionCount = 0;
        const topFacts = primitiveEntries(data);
        if (topFacts.length) {
            const section = node('section', 'agent-data-section');
            section.append(node('strong', '', '检查摘要'), renderFactGrid(topFacts.slice(0, 8)));
            wrapper.append(section);
            sectionCount += 1;
        }
        Object.entries(data).forEach(([key, value]) => {
            if (sectionCount >= MAX_GENERIC_SECTIONS) return;
            if (Array.isArray(value) && value.length) {
                wrapper.append(renderArraySection(key, value));
                sectionCount += 1;
                return;
            }
            if (value && typeof value === 'object' && !Array.isArray(value)) {
                const entries = primitiveEntries(value);
                if (!entries.length) return;
                const section = node('section', 'agent-data-section');
                section.append(node('strong', '', readableKey(key)), renderFactGrid(entries.slice(0, 8)));
                wrapper.append(section);
                sectionCount += 1;
            }
        });
        return wrapper.childElementCount ? wrapper : null;
    }

    function renderResultDisclosure(content, label = '查看详细结果') {
        if (!content) return null;
        const details = node('details', 'agent-result-disclosure');
        const summary = node('summary');
        summary.append(icon('list-tree'), node('span', '', label));
        details.append(summary, content);
        return details;
    }

    function renderNarrativeState(display, result) {
        const rawStatus = normalizeStatus(result?.status);
        const status = display?.status && typeof display.status === 'object' ? display.status : {};
        const publicKey = String(status.key || '').trim();
        if (['clarification_required', 'selection_required'].includes(rawStatus)) return null;
        const error = String(display?.error || '').trim();
        const needsAttention = ['attention', 'unavailable'].includes(publicKey)
            || result?.ok === false || Boolean(error);
        if (!needsAttention) return null;
        const section = node('section', 'agent-result-state');
        const head = node('div', 'agent-result-state-head');
        const tone = {good: 'is-good', warning: 'is-warning', error: 'is-error'}[status.tone]
            || statusTone(rawStatus, result?.ok);
        head.append(
            node('span', `agent-status ${tone}`, String(status.label || statusLabel(rawStatus))),
            node('strong', '', publicKey === 'unavailable' || result?.ok === false ? '需要处理' : '状态提示'),
        );
        section.append(head);
        if (error) {
            const errorCopy = renderTextBlocks(error, 'agent-result-error agent-rich-text');
            if (errorCopy) section.append(errorCopy);
        }
        return section;
    }

    function responseGuidance(payload) {
        const candidates = [
            ...(Array.isArray(payload?.presentation?.guidance) ? payload.presentation.guidance : []),
            ...(Array.isArray(payload?.display?.guidance) ? payload.display.guidance : []),
            ...(Array.isArray(payload?.guidance) ? payload.guidance : []),
        ];
        const seen = new Set();
        return candidates.flatMap((item) => {
            if (!item || typeof item !== 'object') return [];
            const prompt = String(item.prompt || '').trim().slice(0, 240);
            if (!prompt || seen.has(prompt)) return [];
            seen.add(prompt);
            return [{
                prompt,
                label: String(item.label || prompt).trim().slice(0, 80),
                kind: item.kind === 'read' ? 'read' : 'draft',
            }];
        }).slice(0, 3);
    }

    function responseNotices(payload) {
        const candidates = [
            ...(Array.isArray(payload?.presentation?.notices) ? payload.presentation.notices : []),
            ...(Array.isArray(payload?.display?.notices) ? payload.display.notices : []),
            ...(Array.isArray(payload?.notices) ? payload.notices : []),
        ];
        const seen = new Set();
        return candidates.flatMap((item) => {
            const text = String(item || '').trim().slice(0, 220);
            if (!text || seen.has(text)) return [];
            seen.add(text);
            return [text];
        }).slice(0, 3);
    }

    function renderNotices(payload) {
        const notices = responseNotices(payload);
        if (!notices.length) return null;
        const section = node('aside', 'agent-notices');
        section.setAttribute('aria-label', '数据说明');
        const mark = node('span', 'agent-notices-mark');
        mark.append(icon('info'));
        const copy = node('div', 'agent-notices-copy');
        copy.append(node('strong', '', '数据说明'));
        notices.forEach((notice) => copy.append(node('p', '', notice)));
        section.append(mark, copy);
        return section;
    }

    function renderNarrative(presentation) {
        if (!PUBLIC_NARRATIVE_SOURCES.has(presentation?.source) || presentation?.kind !== 'narrative') return null;
        const narrative = String(presentation.narrative || '').trim();
        if (!narrative) return null;
        return renderTextBlocks(narrative, 'agent-narrative agent-rich-text', {promoteFirst: true});
    }

    function responseInspectionTrace(payload) {
        const rawItems = Array.isArray(payload?.agent_trace) ? payload.agent_trace : [];
        const partial = payload?.agent_partial?.complete === false;
        const attentionOnly = PUBLIC_NARRATIVE_SOURCES.has(payload?.presentation?.source)
            && payload?.presentation?.kind === 'narrative';
        const projected = rawItems.flatMap((item) => {
            if (!item || typeof item !== 'object') return [];
            const label = String(item.label || '').trim().slice(0, 80);
            if (!label) return [];
            return [{
                label,
                ok: item.ok === true,
                summary: String(item.summary || '').trim().slice(0, 240),
            }];
        });
        const items = projected.filter((item) => !attentionOnly || !item.ok).slice(0, 6);
        if (!items.length || (items.length === 1 && !partial && !attentionOnly)) return null;
        return {
            items,
            partial,
            attentionOnly,
            total: attentionOnly ? items.length : rawItems.length,
        };
    }

    function renderInspectionTrace(payload) {
        const trace = responseInspectionTrace(payload);
        if (!trace) return null;
        const section = node('section', 'agent-inspection-trace');
        section.setAttribute('aria-label', '本次核对范围');
        const head = node('div', 'agent-inspection-trace-head');
        const heading = node('div');
        heading.append(
            node(
                'span',
                'agent-inspection-kicker',
                trace.attentionOnly ? '需要留意' : (trace.partial ? '部分完成' : '核对来源'),
            ),
            node('h4', '', `${trace.attentionOnly ? '未完成' : '本次核对'} · ${trace.total} 项`),
        );
        head.append(
            heading,
            node(
                'span',
                `agent-inspection-state ${trace.partial ? 'is-warning' : 'is-good'}`,
                trace.partial ? '部分完成' : '核对完成',
            ),
        );
        const list = node('div', 'agent-inspection-list');
        list.setAttribute('role', 'list');
        trace.items.forEach((item) => {
            const row = node('div', `agent-inspection-item ${item.ok ? 'is-good' : 'is-warning'}`);
            row.setAttribute('role', 'listitem');
            const mark = node('span', 'agent-inspection-mark');
            mark.append(icon(item.ok ? 'check' : 'triangle-alert'));
            const copy = node('div', 'agent-inspection-copy');
            copy.append(node('strong', '', item.label));
            if (item.summary) copy.append(node('p', '', item.summary));
            row.append(
                mark,
                copy,
                node('span', 'agent-inspection-result', item.ok ? '完成' : '需关注'),
            );
            list.append(row);
        });
        section.append(head, list);
        if (!trace.attentionOnly && trace.total > trace.items.length) {
            section.append(node('p', 'agent-inspection-more', `另有 ${trace.total - trace.items.length} 项已核对`));
        }
        return section;
    }

    function renderGuidance(payload) {
        const items = responseGuidance(payload);
        if (!items.length) return null;
        const section = node('section', 'agent-guidance');
        section.append(node('h4', '', '下一步'));
        const actions = node('div', 'agent-guidance-actions');
        items.forEach((item) => {
            const button = node('button', `agent-guidance-action is-${item.kind}`);
            button.type = 'button';
            if (item.kind === 'read') button.dataset.agentPrompt = item.prompt;
            else button.dataset.agentDraft = item.prompt;
            button.append(
                icon(item.kind === 'read' ? 'arrow-up-right' : 'pencil-line'),
                node('span', '', item.label),
            );
            actions.append(button);
        });
        section.append(actions);
        return section;
    }

    function createMessage(kind, label, iconName) {
        const article = node('article', `agent-message agent-message-${kind}`);
        const mark = node('div', 'agent-message-mark');
        mark.append(icon(iconName));
        const body = node('div', 'agent-message-body');
        body.append(node('span', 'agent-message-label', label));
        article.append(mark, body);
        return {article, body};
    }

    function transcriptMessageMustRemain(message,preserved){
        if(preserved.has(message)||message===activeQuery?.pending)return true;
        if(message.querySelector('.agent-pending, .agent-streaming'))return true;
        return [...message.querySelectorAll('.agent-confirmation-card')].some(card=>(
            confirmationTimers.has(card)||[...card.querySelectorAll('button')].some(button=>!button.disabled)
        ));
    }

    function pruneTranscript({preserve=[]}={}){
        const messages=[...transcript.children].filter(message=>message.classList?.contains('agent-message'));
        let excess=messages.length-MAX_TRANSCRIPT_MESSAGES;
        if(excess<=0)return;
        const preserved=new Set(preserve.filter(Boolean));
        for(const message of messages){
            if(excess<=0)break;
            if(transcriptMessageMustRemain(message,preserved))continue;
            message.querySelectorAll('.agent-confirmation-card').forEach(clearConfirmationTimer);
            message.remove();
            excess-=1;
        }
    }

    function appendUserMessage(message) {
        const view = createMessage('user', 'YOU', 'user-round');
        view.body.append(node('p', '', message));
        transcript.append(view.article);
        pruneTranscript({preserve:[view.article]});
        syncConversationLayout();
        refreshIcons(view.article);
        if (window.MFAnim && typeof window.MFAnim.popIn === 'function') {
            window.MFAnim.popIn(view.article, { duration: 0.2, y: 8 });
        }
        scrollToLatest();
    }

    function appendPendingMessage() {
        const view = createMessage('assistant', 'MEDIAFLUX AGENT', 'loader-circle');
        const card = node('div', 'agent-result-card agent-pending');
        card.setAttribute('aria-busy', 'true');
        card.append(node('strong', '', '正在选择工具并核对数据…'), node('div', 'agent-pending-line'));
        view.body.append(card);
        transcript.append(view.article);
        pruneTranscript({preserve:[view.article]});
        syncConversationLayout();
        refreshIcons(view.article);
        if (window.MFAnim && typeof window.MFAnim.popIn === 'function') {
            window.MFAnim.popIn(view.article, { duration: 0.2, y: 8 });
        }
        scrollToLatest();
        return view.article;
    }

    function renderResultCard(payload) {
        const result = payload?.result && typeof payload.result === 'object' ? payload.result : {};
        const display = payload?.display && typeof payload.display === 'object' ? payload.display : {};
        const displayStatus = display?.status && typeof display.status === 'object' ? display.status : {};
        const card = node('div', 'agent-result-card');
        const mode = String(payload?.mode || '');
        const narrative = renderNarrative(payload?.presentation);
        if (mode === 'conversation' || mode === 'clarification') {
            card.classList.add('is-conversation');
            const fallback = mode === 'clarification'
                ? '还需要你补充一些信息才能继续。'
                : '暂时没有可展示的回复。';
            const copy = narrative || renderTextBlocks(
                display.summary || display.error || result.summary || result.error || fallback,
                'agent-conversation-copy agent-rich-text',
                {promoteFirst: true},
            );
            if (copy) card.append(copy);
            const trace = renderInspectionTrace(payload);
            if (trace) card.append(trace);
            const state = renderNarrativeState(display, result);
            if (state) card.append(state);
            const notices = renderNotices(payload);
            if (notices) card.append(notices);
            const guidance = renderGuidance(payload);
            if (guidance) card.append(guidance);
            return card;
        }

        if (narrative) {
            card.classList.add('has-narrative');
            card.append(narrative);
            const state = renderNarrativeState(display, result);
            if (state) card.append(state);
        } else {
            const head = node('div', 'agent-result-head');
            const summary = renderTextBlocks(
                display.summary || result.summary || 'Agent 已返回结果',
                'agent-result-summary agent-rich-text',
                {promoteFirst: true},
            );
            if (summary) head.append(summary);
            const tone = {good: 'is-good', warning: 'is-warning', error: 'is-error'}[displayStatus.tone]
                || statusTone(result.status, result.ok);
            head.append(node('span', `agent-status ${tone}`, String(displayStatus.label || statusLabel(result.status))));
            card.append(head);
            if (display.error || result.error) {
                const errorCopy = renderTextBlocks(display.error || result.error, 'agent-result-error agent-rich-text');
                if (errorCopy) card.append(errorCopy);
            }
        }
        const trace = renderInspectionTrace(payload);
        if (trace) card.append(trace);
        const toolName = String(payload?.tool_call?.name || '');
        const hideRepeatedReadPlan = toolName === 'agent.read_plan' && Boolean(narrative) && Boolean(trace);
        const specializedData = hideRepeatedReadPlan ? null : renderSpecializedData(
            toolName,
            result.data,
            result,
            {attentionOnly: Boolean(narrative)},
        );
        const genericData = specializedData || hideRepeatedReadPlan ? null : renderData(display.details);
        if (specializedData) {
            const detail = narrative && COLLAPSIBLE_NARRATIVE_DETAIL_TOOLS.has(toolName)
                ? renderResultDisclosure(specializedData, '查看核对明细')
                : specializedData;
            if (detail) card.append(detail);
        } else if (genericData) {
            card.append(renderResultDisclosure(genericData));
        }
        const notices = renderNotices(payload);
        if (notices) card.append(notices);
        const guidance = payload?.tool_call?.name === 'config.diagnose' ? null : renderGuidance(payload);
        if (guidance) card.append(guidance);
        return card;
    }

    function reuseAssistantMessage(pendingNode, label, iconName) {
        if (!pendingNode?.isConnected) {
            const view = createMessage('assistant', label, iconName);
            transcript.append(view.article);
            pruneTranscript({preserve:[view.article]});
            syncConversationLayout();
            return view;
        }
        const article = pendingNode;
        article.className = 'agent-message agent-message-assistant';
        const mark = article.querySelector('.agent-message-mark');
        const body = article.querySelector('.agent-message-body');
        if (mark) mark.replaceChildren(icon(iconName));
        if (body) body.replaceChildren(node('span', 'agent-message-label', label));
        return {article, body};
    }

    function confirmationPayloadFromActionPlan(payload) {
        const legacy = payload?.confirmation && typeof payload.confirmation === 'object'
            ? payload.confirmation
            : {};
        const plan = payload?.action_plan && typeof payload.action_plan === 'object'
            ? payload.action_plan
            : {};
        const planId = String(plan.plan_id || '').trim();
        if (!planId) return legacy;
        return {
            ...legacy,
            confirmation_id: planId,
            risk: String(plan.risk || legacy.risk || 'write'),
            expires_in: Number(plan.expires_in || legacy.expires_in || 0),
            contract: {
                version: 1,
                action: String(plan.title || '执行受控操作'),
                object: String(plan.target || '当前预检选中的对象'),
                impact: String(plan.impact || '执行后会应用预检通过的受控变更。'),
                reversibility: String(plan.reversibility || '执行后可能需要手动撤销。'),
                preflight_at: String(plan.preflight_at || ''),
                preflight_summary: String(plan.preflight_summary || ''),
                risk: String(plan.risk || legacy.risk || 'write'),
            },
        };
    }

    function appendAssistantResponse(payload, pendingNode) {
        const keepPinned = transcriptIsNearBottom();
        const streamingCard = pendingNode?.querySelector?.('.agent-result-card.agent-streaming');
        const view = streamingCard
            ? {article: pendingNode, body: pendingNode.querySelector('.agent-message-body')}
            : reuseAssistantMessage(pendingNode, 'MEDIAFLUX AGENT', 'bot');
        const confirmationPayload = confirmationPayloadFromActionPlan(payload);
        const confirmationRequired = payload?.mode === 'confirmation_required'
            && confirmationPayload?.confirmation_id;
        const renderedCard = confirmationRequired
            ? renderConfirmation(confirmationPayload, payload.tool_call, payload)
            : renderResultCard(payload);
        let resultCard = renderedCard;
        if (streamingCard) {
            if (confirmationRequired) {
                streamingCard.replaceWith(renderedCard);
            } else {
                streamingCard.className = renderedCard.className;
                streamingCard.removeAttribute('aria-busy');
                streamingCard.replaceChildren(...renderedCard.childNodes);
                resultCard = streamingCard;
            }
        } else {
            view.body.append(resultCard);
        }
        pruneTranscript({preserve:[view.article]});
        refreshIcons(view.article);
        if (!streamingCard && window.MFAnim && typeof window.MFAnim.popIn === 'function' && !restoringHistory) {
            window.MFAnim.popIn(resultCard, { duration: 0.22, y: 6 });
        }
        if (keepPinned) scrollToLatest({force: true});
        announceResponseStatus(responseAnnouncement(payload));
        return view.article;
    }

    function createRetryDraftButton(draft) {
        const normalized = String(draft || '').trim();
        if (!normalized) return null;
        const edit = node('button', 'agent-retry-draft');
        edit.type = 'button';
        edit.dataset.agentDraft = normalized;
        edit.append(icon('edit-3'), node('span', '', '编辑指令'));
        return edit;
    }

    function createRetryActions(draft) {
        const normalized = String(draft || '').trim();
        if (!normalized) return null;
        const actions = node('div', 'agent-retry-actions');
        const retry = node('button', 'agent-retry-immediate');
        retry.type = 'button';
        retry.dataset.agentPrompt = normalized;
        retry.append(icon('refresh-cw'), node('span', '', '立即重试'));
        const edit = createRetryDraftButton(normalized);
        actions.append(retry, edit);
        return actions;
    }

    function appendRequestError(error, pendingNode, draft = '') {
        const view = reuseAssistantMessage(pendingNode, 'REQUEST FAILED', 'triangle-alert');
        const card = node('div', 'agent-error-card');
        card.append(
            node('strong', '', error?.message || '请求暂时无法完成'),
            node('p', 'agent-result-error', error?.status === 429
                ? '请求频率已达到限制，请稍后再试。'
                : '现有对话已保留，你可以修改指令后重试。'),
        );
        const retryActions = createRetryActions(draft);
        if (retryActions) card.append(retryActions);
        view.body.append(card);
        pruneTranscript({preserve:[view.article]});
        refreshIcons(view.article);
        if (window.MFAnim && typeof window.MFAnim.shake === 'function') {
            window.MFAnim.shake(card, { intensity: 6, duration: 0.32 });
        }
        scrollToLatest();
        announceResponseStatus(error?.status === 429 ? '请求频率已达到限制，请稍后再试。' : 'Agent 请求失败，现有对话已保留。');
    }

    const AGENT_PHASE_LABELS = {
        routing: '正在理解任务并选择安全工具…',
        running: '正在读取数据并执行检查…',
        reviewing: '已取得数据，正在核对结果…',
        answering: '正在组织答复…',
    };

    function beginStreamingMessage(pendingNode, phase = 'routing') {
        const view = reuseAssistantMessage(pendingNode, 'MEDIAFLUX AGENT', 'bot');
        const card = node('div', 'agent-result-card agent-streaming');
        card.setAttribute('aria-busy', 'true');
        const head = node('div', 'agent-stream-head');
        head.append(icon('sparkles'), node('span', '', AGENT_PHASE_LABELS[phase] || AGENT_PHASE_LABELS.routing));
        const content = node('div', 'agent-stream-text agent-rich-text');
        content.setAttribute('aria-live', 'off');
        const textNode = document.createTextNode('');
        content.append(textNode);
        const steps = node('div', 'agent-stream-steps');
        steps.setAttribute('aria-hidden', 'true');
        card.append(head, steps, content);
        view.body.append(card);
        refreshIcons(view.article);
        return {article: view.article, card, content, head, steps, textNode};
    }

    function appendStreamingStep(streamView, event) {
        if (!streamView?.steps) return;
        const row = node('div', `agent-stream-step ${event.ok === false ? 'is-warning' : ''}`);
        row.append(icon(event.ok === false ? 'triangle-alert' : 'check'), node('span', '', String(event.label || '检查完成')));
        streamView.steps.append(row);
        while (streamView.steps.children.length > 4) streamView.steps.firstElementChild?.remove();
        refreshIcons(streamView.article);
    }

    function updateStreamingPhase(streamView, phase) {
        if (!streamView?.head) return;
        streamView.head.replaceChildren(
            icon(phase === 'answering' ? 'sparkles' : 'loader-circle'),
            node('span', '', AGENT_PHASE_LABELS[phase] || AGENT_PHASE_LABELS.running),
        );
        refreshIcons(streamView.article);
    }

    function markRequestStopped(operation, message = '本次任务已停止，结果未写入会话历史。') {
        if (!operation || operation.stopRendered) return;
        operation.stopRendered = true;
        const view = operation.streamView || reuseAssistantMessage(
            operation.pending,
            'MEDIAFLUX AGENT',
            'square',
        );
        const article = view.article || operation.pending;
        const body = article?.querySelector('.agent-message-body');
        const card = node('div', 'agent-result-card agent-cancelled');
        const head = node('div', 'agent-stream-head');
        head.append(icon('square'), node('span', '', '任务已停止'));
        card.append(head, node('p', 'agent-stream-text', message));
        if (body) {
            body.replaceChildren(node('span', 'agent-message-label', 'MEDIAFLUX AGENT'), card);
        }
        pruneTranscript({preserve:[article]});
        refreshIcons(article);
        scrollToLatest();
        announceResponseStatus('Agent 任务已停止，结果未写入会话历史。');
    }

    function markStreamInterrupted(streamView, message, draft = '') {
        if (!streamView?.card) return;
        streamView.card.removeAttribute('aria-busy');
        streamView.card.classList.remove('agent-streaming');
        streamView.card.classList.add('is-interrupted');
        streamView.head.replaceChildren(icon('triangle-alert'), node('span', '', '生成中断'));
        streamView.card.append(node('p', 'agent-stream-error', message || '当前内容未完成，请重试。'));
        const retryActions = createRetryActions(draft);
        if (retryActions) streamView.card.append(retryActions);
        refreshIcons(streamView.article);
        scrollToLatest();
        announceResponseStatus('Agent 回答生成中断，可以重试或编辑指令。');
    }

    async function responseError(response) {
        const text = await response.text();
        let payload = {};
        if (text) {
            try { payload = JSON.parse(text); } catch (_) { payload = {}; }
        }
        const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
        error.status = response.status;
        error.payload = payload;
        return error;
    }

    function isOperationCurrent(operation) {
        return Boolean(
            operation
            && !operation.stopped
            && activeQuery === operation
            && operation.generation === conversationGeneration
            && !operation.controller.signal.aborted
        );
    }

    async function readAgentStream(response, pendingNode, operation) {
        if (!response.ok) throw await responseError(response);
        const contentType = String(response.headers.get('content-type') || '').toLowerCase();
        if (!contentType.includes('application/x-ndjson') || !response.body) {
            const payload = await response.json();
            if (!isOperationCurrent(operation)) {
                return {payload: null, streamed: false, cancelled: true, streamView: null};
            }
            return {payload, streamed: false, cancelled: false};
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let answer = '';
        let finalPayload = null;
        let streamView = null;
        let cancelled = false;
        const cancelReader = () => {
            try { reader.cancel().catch(() => {}); } catch (_) { /* reader already closed */ }
        };
        operation.controller.signal.addEventListener('abort', cancelReader, {once: true});
        if (operation.controller.signal.aborted) cancelReader();

        const consumeLine = (line) => {
            if (!isOperationCurrent(operation)) {
                cancelled = true;
                return;
            }
            const trimmed = line.trim();
            if (!trimmed) return;
            let event;
            try { event = JSON.parse(trimmed); } catch (_) {
                throw new Error('Agent 流式响应格式无效');
            }
            if (event.request_id && event.request_id !== operation.requestId) {
                throw new Error('Agent 流式响应与当前请求不匹配');
            }
            if (event.type === 'status') {
                streamView ||= beginStreamingMessage(pendingNode, event.phase);
                operation.streamView = streamView;
                updateStreamingPhase(streamView, event.phase);
                return;
            }
            if (event.type === 'step') {
                streamView ||= beginStreamingMessage(pendingNode, event.phase || 'running');
                operation.streamView = streamView;
                updateStreamingPhase(streamView, event.phase || 'running');
                if (event.step === 'tool_finish') appendStreamingStep(streamView, event);
                return;
            }
            if (event.type === 'delta') {
                const delta = String(event.delta || '');
                if (!delta) return;
                streamView ||= beginStreamingMessage(pendingNode, 'answering');
                operation.streamView = streamView;
                const keepPinned = transcriptIsNearBottom();
                answer += delta;
                streamView.textNode.appendData(delta);
                if (keepPinned) scrollToLatest({force: true});
                return;
            }
            if (event.type === 'cancelled') {
                cancelled = true;
                operation.cancelMessage = String(event.message || '');
                return;
            }
            if (event.type === 'final') {
                finalPayload = event.payload;
                return;
            }
            if (event.type === 'error') {
                const error = new Error(event.message || '回答生成中断');
                error.partial = Boolean(answer);
                error.streamView = streamView;
                throw error;
            }
        };

        try {
            while (true) {
                const {value, done} = await reader.read();
                if (!isOperationCurrent(operation)) {
                    cancelled = true;
                    break;
                }
                buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
                if (buffer.length > 512 * 1024) throw new Error('Agent 流式响应过大');
                let newline = buffer.indexOf('\n');
                while (newline >= 0) {
                    const line = buffer.slice(0, newline);
                    buffer = buffer.slice(newline + 1);
                    consumeLine(line);
                    if (cancelled) break;
                    newline = buffer.indexOf('\n');
                }
                if (done || cancelled) break;
            }
            if (!cancelled && isOperationCurrent(operation) && buffer.trim()) consumeLine(buffer);
            if (cancelled || !isOperationCurrent(operation)) {
                return {payload: null, streamed: Boolean(answer), cancelled: true, streamView};
            }
            if (!finalPayload || typeof finalPayload !== 'object') {
                throw new Error('Agent 流在完成前中断');
            }
            return {payload: finalPayload, streamed: Boolean(answer), cancelled: false, streamView};
        } catch (error) {
            if (answer) {
                error.partial = true;
                error.streamView = streamView;
            }
            throw error;
        } finally {
            operation.controller.signal.removeEventListener('abort', cancelReader);
            try { await reader.cancel(); } catch (_) { /* reader may already be closed */ }
            try { reader.releaseLock(); } catch (_) { /* cancelled readers may release eagerly */ }
        }
    }


    async function fetchJSON(url, options = {}) {
        const response = await fetch(url, options);
        const text = await response.text();
        let payload = {};
        if (text) {
            try {
                payload = JSON.parse(text);
            } catch (_) {
                payload = {};
            }
        }
        if (!response.ok) {
            const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function setBusy(busy, {stoppable = false} = {}) {
        requestInFlight = busy;
        const showStop = Boolean(busy && stoppable && stopButton);
        sendButton.hidden = showStop;
        syncSendAvailability();
        promptInput.disabled = busy || confirmationInFlight || sessionResetInFlight;
        sendButton.classList.toggle('is-busy', Boolean(busy && !showStop));
        sendButton.setAttribute('aria-busy', String(busy));
        if (stopButton) {
            stopButton.hidden = !showStop;
            stopButton.disabled = !showStop;
            stopButton.setAttribute('aria-busy', 'false');
        }
        const iconNode = sendButton.querySelector('svg, i[data-lucide]');
        if (iconNode) {
            const replacement = icon(busy && !showStop ? 'loader-circle' : 'arrow-up');
            iconNode.replaceWith(replacement);
            refreshIcons(sendButton);
        }
        syncSessionLifecycleControls();
    }

    function syncSessionLifecycleControls() {
        const switchBlocked = requestInFlight || confirmationInFlight || sessionResetInFlight;
        const newSessionBlocked = confirmationInFlight || sessionResetInFlight;
        const switchReason = requestInFlight
            ? '任务执行中，完成后可新建、切换或删除会话。'
            : confirmationInFlight
            ? '写操作正在执行，完成后可新建、切换或删除会话。'
            : sessionResetInFlight
                ? '会话正在更新，请稍候。'
                : '';
        const newSessionReason = confirmationInFlight
            ? '写操作正在执行，完成后可新建会话。'
            : sessionResetInFlight
                ? '会话正在更新，请稍候。'
                : '';
        newSessionButton.disabled = newSessionBlocked;
        newSessionButton.setAttribute('aria-busy', String(newSessionBlocked));
        newSessionButton.title = newSessionReason || (requestInFlight ? '停止当前任务并开始新会话' : '新会话');
        if (resumeLatestSessionButton) {
            const canResume = Boolean(latestSessionId && latestSessionId !== agentSessionId && !switchBlocked);
            resumeLatestSessionButton.disabled = !canResume;
            resumeLatestSessionButton.setAttribute('aria-busy', String(switchBlocked));
            resumeLatestSessionButton.title = switchReason || (canResume ? '继续上次会话' : '当前没有可恢复的其他会话');
        }
        sessionListNode.querySelectorAll('button').forEach((button) => {
            button.disabled = switchBlocked;
            if (switchReason) button.title = switchReason;
            else button.removeAttribute('title');
        });
        sessionListNode.setAttribute('aria-busy', String(switchBlocked));
    }

    function setConfirmationBusy(busy) {
        confirmationInFlight = busy;
        syncSessionLifecycleControls();
        syncSendAvailability();
        promptInput.disabled = busy || requestInFlight || sessionResetInFlight;
        page.querySelectorAll('.agent-confirmation-card:not(.is-cancelled):not(.is-expired) .agent-confirmation-actions button')
            .forEach((button) => { button.disabled = busy || sessionResetInFlight; });
    }

    function setSessionTransitionBusy(busy) {
        sessionResetInFlight = busy;
        syncSessionLifecycleControls();
        syncSendAvailability();
        promptInput.disabled = busy || requestInFlight || confirmationInFlight;
        page.querySelectorAll('.agent-confirmation-card:not(.is-cancelled):not(.is-expired) .agent-confirmation-actions button')
            .forEach((button) => { button.disabled = busy || confirmationInFlight; });
    }

    function markVisibleConfirmationsRevoked(message = '会话已切换，原行动计划已取消。') {
        page.querySelectorAll('.agent-confirmation-card:not(.is-cancelled):not(.is-expired)').forEach((card) => {
            clearConfirmationTimer(card);
            card.classList.add('is-cancelled');
            card.dataset.confirmationId = '';
            card.querySelector('.agent-confirmation-actions')?.replaceChildren(
                node('span', 'agent-result-meta', message),
            );
        });
    }

    function syncSendAvailability() {
        const hasPrompt = Boolean(promptInput.value.trim());
        composer.classList.toggle('has-prompt', hasPrompt);
        sendButton.disabled = requestInFlight || confirmationInFlight || sessionResetInFlight || !hasPrompt;
        sendButton.title = hasPrompt ? '发送 (Enter)' : '输入内容后发送';
    }

    function resizePrompt() {
        promptInput.style.height = 'auto';
        if (!promptInput.value.trim()) {
            promptInput.style.height = '';
            syncSendAvailability();
            return;
        }
        promptInput.style.height = `${Math.min(promptInput.scrollHeight, 140)}px`;
        syncSendAvailability();
    }

    async function stopActiveQuery({announceFailure = true} = {}) {
        const operation = activeQuery;
        if (!operation) return;
        operation.stopped = true;
        markRequestStopped(operation);
        const cancelController = new AbortController();
        const cancelTimeout = window.setTimeout(
            () => cancelController.abort(),
            AGENT_CANCEL_TIMEOUT_MS,
        );
        const cancelRequest = fetchJSON('/api/agent/query/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(sessionPayload({request_id: operation.requestId}, operation.sessionId)),
            signal: cancelController.signal,
        });
        operation.controller.abort();
        if (activeQuery === operation) {
            activeQuery = null;
            activeController = null;
            setBusy(false);
        }
        try {
            await cancelRequest;
        } catch (error) {
            if (announceFailure) {
                announceSessionStatus('停止请求未被服务端确认；会话重置仍会阻止旧结果写入。');
            }
        } finally {
            window.clearTimeout(cancelTimeout);
            if (!sessionResetInFlight) promptInput.focus({preventScroll: true});
        }
    }

    async function sendMessage(message) {
        const normalized = String(message || '').trim();
        if (!normalized || requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        const requestSessionId = agentSessionId;
        const controller = new AbortController();
        const operation = {
            requestId: createAgentRequestId(),
            sessionId: requestSessionId,
            generation,
            controller,
            pending: null,
            streamView: null,
            stopped: false,
            stopRendered: false,
        };
        activeController = controller;
        activeQuery = operation;
        appendUserMessage(normalized);
        promptInput.value = '';
        resizePrompt();
        setBusy(true, {stoppable: true});
        const pending = appendPendingMessage();
        operation.pending = pending;
        const startedAt = performance.now();
        try {
            const response = await fetch('/api/agent/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({
                    message: normalized,
                    stream: true,
                    request_id: operation.requestId,
                }, requestSessionId)),
                signal: controller.signal,
            });
            const streamed = await readAgentStream(response, pending, operation);
            if (streamed.cancelled || operation.stopped) {
                markRequestStopped(operation, operation.cancelMessage);
                return;
            }
            if (!streamed.streamed) {
                await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            }
            if (activeQuery !== operation || generation !== conversationGeneration) {
                await discardStaleConfirmation(streamed.payload, requestSessionId);
                return;
            }
            appendAssistantResponse(streamed.payload, pending);
            loadSessions();
        } catch (error) {
            if (error?.name === 'AbortError' || operation.stopped || activeQuery !== operation || generation !== conversationGeneration) {
                if (operation.stopped) markRequestStopped(operation);
                return;
            }
            if (error?.partial && error?.streamView) {
                markStreamInterrupted(error.streamView, error.message, normalized);
                return;
            }
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            appendRequestError(error, pending, normalized);
        } finally {
            if (activeQuery === operation) {
                activeQuery = null;
                activeController = null;
                setBusy(false);
                promptInput.focus({preventScroll: true});
            }
        }
    }

    async function runDirectTool(button) {
        const action = directToolActions.get(button);
        if (!action || requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        const requestSessionId = agentSessionId;
        activeController = new AbortController();
        appendUserMessage(action.label);
        setBusy(true);
        button.disabled = true;
        button.classList.add('is-busy');
        button.setAttribute('aria-busy', 'true');
        const iconNode = button.querySelector('svg, i[data-lucide]');
        if (iconNode) {
            iconNode.replaceWith(icon('loader-circle'));
            refreshIcons(button);
        }
        const pending = appendPendingMessage();
        const startedAt = performance.now();
        try {
            const payload = await fetchJSON(`/api/agent/tools/${action.tool}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({arguments: action.arguments}, requestSessionId)),
                signal: activeController.signal,
            });
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            if (generation !== conversationGeneration) {
                await discardStaleConfirmation(payload, requestSessionId);
                return;
            }
            appendAssistantResponse(payload, pending);
            loadSessions();
        } catch (error) {
            if (error?.name === 'AbortError' || generation !== conversationGeneration) return;
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            appendRequestError(error, pending);
        } finally {
            if (generation === conversationGeneration) {
                activeController = null;
                setBusy(false);
                if (button.isConnected) {
                    button.disabled = false;
                    button.classList.remove('is-busy');
                    button.setAttribute('aria-busy', 'false');
                    const busyIcon = button.querySelector('svg, i[data-lucide]');
                    if (busyIcon) {
                        busyIcon.replaceWith(icon('search'));
                        refreshIcons(button);
                    }
                }
            }
        }
    }

    async function runWorkspaceAction(button) {
        const actionKey = String(button?.dataset.agentWorkspaceAction || '');
        if (!/^[a-z0-9_]{3,64}$/.test(actionKey)) return;
        if (requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        const requestSessionId = agentSessionId;
        activeController = new AbortController();
        appendUserMessage(button.dataset.agentWorkspaceLabel || '执行工作区检查');
        setBusy(true);
        button.disabled = true;
        button.classList.add('is-busy');
        button.setAttribute('aria-busy', 'true');
        const iconNode = button.querySelector('svg, i[data-lucide]');
        if (iconNode) {
            iconNode.replaceWith(icon('loader-circle'));
            refreshIcons(button);
        }
        const pending = appendPendingMessage();
        const startedAt = performance.now();
        let stale = false;
        try {
            const payload = await fetchJSON('/api/agent/workspace-actions/invoke', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({action_key: actionKey}, requestSessionId)),
                signal: activeController.signal,
            });
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            if (generation !== conversationGeneration) return;
            appendAssistantResponse(payload, pending);
            loadSessions();
        } catch (error) {
            if (error?.name === 'AbortError' || generation !== conversationGeneration) return;
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            stale = error?.status === 409;
            appendRequestError(error, pending);
        } finally {
            if (generation === conversationGeneration) {
                activeController = null;
                setBusy(false);
                if (button.isConnected) {
                    button.disabled = stale;
                    button.classList.remove('is-busy');
                    button.classList.toggle('is-stale', stale);
                    button.setAttribute('aria-busy', 'false');
                    const label = button.firstChild;
                    if (label?.nodeType === Node.TEXT_NODE && stale) label.textContent = '状态已变化';
                    const busyIcon = button.querySelector('svg, i[data-lucide]');
                    if (busyIcon) {
                        busyIcon.replaceWith(icon(stale ? 'refresh-cw' : 'arrow-up-right'));
                        refreshIcons(button);
                    }
                }
            }
        }
    }

    async function prepareResourceSubmission(button) {
        const resultId = String(button?.dataset.agentResourceId || '');
        const target = String(button?.dataset.agentTarget || '');
        if (!/^[A-Za-z0-9_-]{16,128}$/.test(resultId) || !['qb', 'guangya'].includes(target)) return;
        if (requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        const requestSessionId = agentSessionId;
        const controller = new AbortController();
        activeController = controller;
        const targetLabel = target === 'qb' ? 'qBittorrent' : '光鸭';
        appendUserMessage(`准备将所选资源提交到 ${targetLabel}`);
        setBusy(true);
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        const pending = appendPendingMessage();
        const startedAt = performance.now();
        try {
            const payload = await fetchJSON('/api/agent/actions/indexer.submit_resource/prepare', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({arguments: {result_id: resultId, target}}, requestSessionId)),
                signal: controller.signal,
            });
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            if (generation !== conversationGeneration) {
                await discardStaleConfirmation(payload, requestSessionId);
                return;
            }
            appendAssistantResponse(payload, pending);
        } catch (error) {
            if (error?.name === 'AbortError' || generation !== conversationGeneration) return;
            await sleep(Math.max(0, MIN_PENDING_MS - (performance.now() - startedAt)));
            appendRequestError(error, pending);
        } finally {
            if (generation === conversationGeneration) {
                if (activeController === controller) activeController = null;
                setBusy(false);
                if (button.isConnected) {
                    button.disabled = false;
                    button.setAttribute('aria-busy', 'false');
                }
            }
        }
    }

    function renderConfirmation(confirmation, toolCall = {}, payload = {}) {
        const confirmationId = String(confirmation.confirmation_id || '');
        const contract = confirmation?.contract && typeof confirmation.contract === 'object'
            ? confirmation.contract
            : {};
        const action = String(contract.action || '执行受控操作');
        const riskLabels = {low_write: '低风险写入', write: '写入操作', danger: '高风险操作'};
        const risk = String(contract.risk || confirmation.risk || 'write');
        const card = node('section', 'agent-confirmation-card');
        card.dataset.confirmationId = confirmationId;
        card.dataset.confirmationAction = action;
        const toolName = String(toolCall?.name || '').trim();
        const toolArguments = toolCall?.arguments && typeof toolCall.arguments === 'object'
            ? {...toolCall.arguments}
            : null;
        if (toolName && toolArguments) {
            confirmationPrepareActions.set(card, {toolName, arguments: toolArguments});
        }
        const head = node('div', 'agent-confirmation-head');
        const title = node('div');
        title.append(node('span', '', '行动计划'), node('strong', '', action));
        head.append(title, node('span', `agent-confirmation-risk is-${risk}`, riskLabels[risk] || '写入操作'));
        const expires = Number(confirmation.expires_in || 0);
        const expiryCopy = node('p', 'agent-confirmation-copy');
        const preflightTime = node('span', 'agent-confirmation-preflight-time');
        const countdown = node(
            'span',
            'agent-confirmation-countdown',
            expires > 0
                ? `计划剩余 ${expires} 秒，只可执行一次。`
                : '选择执行后才会应用写操作。',
        );
        card.append(head);

        const intro = renderNarrative(payload?.presentation) || renderTextBlocks(
            payload?.display?.summary || payload?.result?.summary || '',
            'agent-confirmation-intro agent-rich-text',
            {promoteFirst: true},
        );
        if (intro) card.append(intro);

        const facts = node('dl', 'agent-confirmation-facts');
        [
            ['操作对象', contract.object],
            ['将会发生', contract.impact],
            ['如何撤销', contract.reversibility],
        ].forEach(([label, value]) => {
            const text = String(value || '').trim();
            if (!text) return;
            const row = node('div', 'agent-confirmation-fact');
            row.append(node('dt', '', label), node('dd', '', text));
            facts.append(row);
        });
        if (facts.childElementCount) card.append(facts);
        if (contract.preflight_summary) {
            const preflight = node('div', 'agent-confirmation-preflight');
            preflight.append(icon('clipboard-check'), node('span', '', String(contract.preflight_summary)));
            card.append(preflight);
        }
        const preflightAt = String(contract.preflight_at || '').trim();
        if (preflightAt) {
            const parsed = new Date(preflightAt);
            const displayTime = Number.isNaN(parsed.getTime())
                ? preflightAt
                : parsed.toLocaleString([], {hour12: false});
            preflightTime.textContent = `预检时间 ${displayTime}`;
        }
        if (preflightTime.textContent) expiryCopy.append(preflightTime, document.createTextNode(' · '));
        expiryCopy.append(countdown);
        card.append(expiryCopy);
        const actions = node('div', 'agent-confirmation-actions');
        const cancel = node('button', 'agent-confirmation-cancel', '取消');
        cancel.type = 'button';
        const confirm = node('button', 'agent-confirmation-submit', '执行');
        confirm.type = 'button';
        confirm.setAttribute('aria-label', `执行行动计划：${action}`);
        cancel.addEventListener('click', async () => {
            if (cancel.disabled || confirmationInFlight || sessionResetInFlight) return;
            clearConfirmationTimer(card);
            cancel.disabled = true;
            confirm.disabled = true;
            const status = node('span', 'agent-result-meta', '正在取消行动计划…');
            actions.replaceChildren(status);
            focusResult(status);
            const discarded = await discardConfirmation(confirmationId);
            card.classList.add('is-cancelled');
            card.dataset.confirmationId = '';
            status.textContent = discarded
                ? '已取消本次行动计划；没有执行写操作。'
                : '取消请求未能立即送达；行动计划将在短时间后自动失效。';
        });
        confirm.addEventListener('click', () => confirmAction(confirmationId, card, confirm, cancel));
        actions.append(cancel, confirm);
        card.append(actions);
        if (expires > 0) startConfirmationCountdown(card, countdown, confirm, cancel, expires);
        return card;
    }

    function clearConfirmationTimer(card) {
        const timer = confirmationTimers.get(card);
        if (timer) window.clearInterval(timer);
        confirmationTimers.delete(card);
    }

    function clearAllConfirmationTimers() {
        confirmationTimers.forEach((timer) => window.clearInterval(timer));
        confirmationTimers.clear();
    }

    function renderReprepareAction(card, message) {
        const actions = card.querySelector('.agent-confirmation-actions');
        if (!actions) return;
        const status = node('span', 'agent-result-meta', message);
        const prepare = confirmationPrepareActions.get(card);
        if (!prepare) {
            actions.replaceChildren(status);
            return;
        }
        const button = node('button', 'agent-confirmation-reprepare');
        button.type = 'button';
        button.append(icon('refresh-cw'), node('span', '', '重新预检'));
        button.addEventListener('click', () => reprepareConfirmation(card, button));
        actions.replaceChildren(status, button);
        refreshIcons(actions);
    }

    async function reprepareConfirmation(card, button) {
        const prepare = confirmationPrepareActions.get(card);
        if (!prepare || requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        setConfirmationBusy(true);
        try {
            const payload = await fetchJSON(`/api/agent/actions/${encodeURIComponent(prepare.toolName)}/prepare`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({arguments: prepare.arguments})),
            });
            if (generation !== conversationGeneration) {
                await discardStaleConfirmation(payload);
                return;
            }
            if (payload?.mode !== 'confirmation_required' || !confirmationPayloadFromActionPlan(payload)?.confirmation_id) {
                throw new Error('重新预检未返回有效行动计划');
            }
            const replacement = renderConfirmation(confirmationPayloadFromActionPlan(payload), payload.tool_call, payload);
            card.replaceWith(replacement);
            focusResult(replacement);
        } catch (error) {
            if (generation !== conversationGeneration) return;
            button.disabled = false;
            button.setAttribute('aria-busy', 'false');
            const oldError = card.querySelector('.agent-result-error');
            oldError?.remove();
            card.append(node('p', 'agent-result-error', error?.message || '重新预检失败，请稍后重试。'));
        } finally {
            if (generation === conversationGeneration) setConfirmationBusy(false);
        }
    }

    function startConfirmationCountdown(card, countdown, confirmButton, cancelButton, expiresIn) {
        const deadline = Date.now() + Math.max(1, Number(expiresIn)) * 1000;
        const update = () => {
            const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            if (remaining > 0) {
                countdown.textContent = `计划剩余 ${remaining} 秒，只可执行一次。`;
                return;
            }
            clearConfirmationTimer(card);
            card.classList.add('is-expired');
            card.dataset.confirmationId = '';
            confirmButton.disabled = true;
            cancelButton.disabled = true;
            countdown.textContent = '行动计划已过期。请重新提交原任务生成新的预检。';
            renderReprepareAction(card, '票据已失效，未执行任何写操作。');
        };
        update();
        if (!card.classList.contains('is-expired')) {
            confirmationTimers.set(card, window.setInterval(update, 1000));
        }
    }

    async function discardConfirmation(confirmationId, sessionId = agentSessionId) {
        if (!confirmationId) return false;
        try {
            const payload = await fetchJSON('/api/agent/actions/confirm/discard', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({confirmation_id: confirmationId}, sessionId)),
            });
            return payload?.discarded !== false;
        } catch (_) {
            return false;
        }
    }

    function renderConfirmationExecutionState(card) {
        const actions = card.querySelector('.agent-confirmation-actions');
        if (!actions) return null;
        const reservedHeight = Math.ceil(actions.getBoundingClientRect().height);
        if (reservedHeight > 0) actions.style.minHeight = `${reservedHeight}px`;
        const status = node('div', 'agent-confirmation-executing');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        const mark = node('span', 'agent-confirmation-executing-mark');
        mark.append(icon('loader-circle'));
        const copy = node('span', 'agent-confirmation-executing-copy');
        copy.append(
            node('strong', '', `正在执行：${card.dataset.confirmationAction || '受控写操作'}`),
            node('small', '', '服务端会先完成本次写入；新会话、切换和删除将在结束后恢复，请勿重复提交。'),
        );
        status.append(mark, copy);
        actions.replaceChildren(status);
        refreshIcons(actions);
        return status;
    }

    function renderConsumedConfirmation(card, payload) {
        clearConfirmationTimer(card);
        card.classList.add('is-cancelled');
        card.dataset.confirmationId = '';
        const actions = card.querySelector('.agent-confirmation-actions');
        const planFailed = payload?.action_plan?.status === 'failed';
        const status = node(
            'span',
            'agent-result-meta',
            planFailed ? '行动计划已处理，但操作未成功。' : '行动计划已执行，结果如下。',
        );
        actions?.replaceChildren(status);
        const response = appendAssistantResponse(payload);
        focusResult(response?.querySelector('.agent-result-card') || status);
    }

    async function confirmAction(confirmationId, card, confirmButton, cancelButton) {
        if (!confirmationId || confirmButton.disabled || confirmationInFlight || sessionResetInFlight) return;
        const generation = conversationGeneration;
        clearConfirmationTimer(card);
        confirmButton.disabled = true;
        cancelButton.disabled = true;
        setConfirmationBusy(true);
        const copy = card.querySelector('.agent-confirmation-copy');
        if (copy) copy.textContent = '行动计划已领取；服务端正在受控执行。';
        renderConfirmationExecutionState(card);
        try {
            const payload = await fetchJSON('/api/agent/actions/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sessionPayload({confirmation_id: confirmationId})),
            });
            if (generation !== conversationGeneration) return;
            renderConsumedConfirmation(card, payload);
            await loadSessions();
        } catch (error) {
            if (generation !== conversationGeneration) return;
            if (error?.payload?.result && error?.payload?.tool_call) {
                renderConsumedConfirmation(card, error.payload);
                await loadSessions();
                return;
            }
            if (error?.status === 409) {
                clearConfirmationTimer(card);
                card.classList.add('is-expired');
                card.dataset.confirmationId = '';
                renderReprepareAction(card, '行动计划已失效，请重新提交原任务生成新的预检。');
                focusResult(card.querySelector('.agent-confirmation-actions'));
                return;
            }
            card.classList.add('is-cancelled');
            card.dataset.confirmationId = '';
            const actions = card.querySelector('.agent-confirmation-actions');
            const status = node(
                'span',
                'agent-result-meta',
                '行动计划结果未知。请先核对任务状态，勿重复提交；如需重试请重新生成预检。',
            );
            actions?.replaceChildren(status);
            const oldError = card.querySelector('.agent-result-error');
            oldError?.remove();
            const errorMessage = node('p', 'agent-result-error', error?.message || '服务端执行结果未返回。');
            card.append(errorMessage);
            focusResult(status);
        } finally {
            if (generation === conversationGeneration) setConfirmationBusy(false);
        }
    }

    function formatSessionTime(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '刚刚';
        return new Intl.DateTimeFormat('zh-CN', {
            month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
        }).format(date);
    }

    function emptyTranscript() {
        clearAllConfirmationTimers();
        promptInput.value = '';
        resizePrompt();
        transcript.replaceChildren();
        syncConversationLayout();
        transcript.scrollTop = 0;
    }

    function renderSessionList(sessions) {
        const items = Array.isArray(sessions) ? sessions : [];
        sessionCountNode.textContent = String(items.length);
        sessionListNode.replaceChildren();
        if (!items.length) {
            const empty = node('div', 'agent-session-empty');
            empty.append(icon('message-circle-dashed'), node('span', '', '尚无已保存的查询摘要'));
            sessionListNode.append(empty);
            refreshIcons(sessionListNode);
            syncSessionLifecycleControls();
            return;
        }
        items.forEach((session) => {
            const sessionId = String(session?.session_id || '');
            if (!/^[A-Za-z0-9_-]{16,64}$/.test(sessionId)) return;
            const item = node('article', `agent-session-item${sessionId === agentSessionId ? ' is-active' : ''}`);
            item.dataset.sessionId = sessionId;
            const open = node('button', 'agent-session-open');
            open.type = 'button';
            open.dataset.agentSessionOpen = sessionId;
            if (sessionId === agentSessionId) open.setAttribute('aria-current', 'page');
            open.append(
                node('strong', '', session?.title || '未命名会话'),
                node('small', '', `${Math.max(0, Number(session?.message_count) || 0)} 条 · ${formatSessionTime(session?.updated_at)}`),
            );
            const remove = node('button', 'agent-session-delete');
            remove.type = 'button';
            remove.dataset.agentSessionDelete = sessionId;
            remove.setAttribute('aria-label', `删除会话：${session?.title || '未命名会话'}`);
            remove.append(icon('trash-2'));
            item.append(open, remove);
            sessionListNode.append(item);
        });
        refreshIcons(sessionListNode);
        syncSessionLifecycleControls();
    }

    function renderRecoveredSession(session) {
        transcript.setAttribute('aria-live', 'off');
        transcript.setAttribute('aria-busy', 'true');
        restoringHistory = true;
        try {
            emptyTranscript();
            const messages = Array.isArray(session?.messages) ? session.messages : [];
            messages.forEach((entry) => {
                const data = entry?.data && typeof entry.data === 'object' ? entry.data : {};
                let article = null;
                if (entry?.role === 'user' && typeof data.text === 'string') {
                    appendUserMessage(data.text);
                    article = transcript.lastElementChild;
                } else if (entry?.role === 'assistant') {
                    const narrative = typeof data.narrative === 'string'
                        ? data.narrative.trim()
                        : '';
                    article = appendAssistantResponse({
                        mode: data.mode || 'read_only',
                        tool_call: {},
                        guidance: Array.isArray(data.guidance) ? data.guidance : [],
                        notices: Array.isArray(data.notices) ? data.notices : [],
                        presentation: narrative ? {
                            source: PUBLIC_NARRATIVE_SOURCES.has(data.presentation_source)
                                ? data.presentation_source
                                : 'llm',
                            kind: 'narrative',
                            narrative,
                            guidance: Array.isArray(data.guidance) ? data.guidance : [],
                            notices: Array.isArray(data.notices) ? data.notices : [],
                        } : undefined,
                        result: {
                            ok: data.ok === true,
                            status: data.status || 'unknown',
                            summary: data.summary || '已恢复历史结果摘要',
                            error: data.error || '',
                            suggestions: Array.isArray(data.suggestions) ? data.suggestions : [],
                            data: {}, evidence: [],
                        },
                    });
                }
                article?.classList.add('is-recovered');
            });
        } finally {
            restoringHistory = false;
        }
        syncConversationLayout();
        refreshIcons(transcript);
        transcript.scrollTop = transcript.scrollHeight;
        window.queueMicrotask(() => {
            transcript.setAttribute('aria-live', 'off');
            transcript.setAttribute('aria-busy', 'false');
        });
    }

    async function openSession(sessionId) {
        if (requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const normalized = String(sessionId || '');
        if (!/^[A-Za-z0-9_-]{16,64}$/.test(normalized)) return;
        const previousSessionId = agentSessionId;
        // 当前活动会话可能仍挂着待确认票据；重复打开既不应清空确认卡，
        // 也不应把仍有效的服务端票据隐藏在恢复历史之后。
        if (previousSessionId === normalized) return;
        const generation = ++conversationGeneration;
        activeController?.abort();
        historyController?.abort();
        historyController = new AbortController();
        sessionListNode.setAttribute('aria-busy', 'true');
        setSessionTransitionBusy(true);
        try {
            if (previousSessionId !== normalized) {
                await fetchJSON('/api/agent/session/reset', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: previousSessionId}),
                    signal: historyController.signal,
                });
                if (generation !== conversationGeneration) return;
                markVisibleConfirmationsRevoked();
            }
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(normalized)}`, {
                signal: historyController.signal,
            });
            if (generation !== conversationGeneration) return;
            agentSessionId = normalized;
            renderRecoveredSession(payload.session || {});
            sessionListNode.querySelectorAll('.agent-session-item').forEach((item) => {
                const isActive = item.dataset.sessionId === normalized;
                item.classList.toggle('is-active', isActive);
                const openButton = item.querySelector('[data-agent-session-open]');
                if (isActive) openButton?.setAttribute('aria-current', 'page');
                else openButton?.removeAttribute('aria-current');
            });
            closeHistoryRail({returnFocus: false});
            announceSessionStatus('历史会话已恢复，可以继续输入。');
        } catch (error) {
            if (error?.name !== 'AbortError' && generation === conversationGeneration) appendRequestError(error);
        } finally {
            if (generation === conversationGeneration) {
                historyController = null;
                sessionListNode.setAttribute('aria-busy', 'false');
                setSessionTransitionBusy(false);
                promptInput.focus({preventScroll: true});
            }
        }
    }

    async function loadSessions() {
        const loadGeneration = ++sessionsLoadGeneration;
        sessionsController?.abort();
        const controller = new AbortController();
        sessionsController = controller;
        sessionListNode.setAttribute('aria-busy', 'true');
        try {
            const payload = await fetchJSON('/api/agent/sessions', {signal: controller.signal});
            if (loadGeneration !== sessionsLoadGeneration) return;
            const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
            latestSessionId = sessions
                .map((session) => String(session?.session_id || ''))
                .find((sessionId) => /^[A-Za-z0-9_-]{16,64}$/.test(sessionId)) || '';
            renderSessionList(sessions);
        } catch (error) {
            if (error?.name === 'AbortError' || loadGeneration !== sessionsLoadGeneration) return;
            latestSessionId = '';
            sessionCountNode.textContent = '—';
            sessionListNode.replaceChildren(node('div', 'agent-session-empty', '会话摘要暂不可用'));
            syncSessionLifecycleControls();
        } finally {
            if (loadGeneration === sessionsLoadGeneration) {
                sessionsController = null;
                sessionListNode.setAttribute('aria-busy', 'false');
            }
        }
    }

    async function deleteSession(sessionId) {
        if (requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const normalized = String(sessionId || '');
        if (!/^[A-Za-z0-9_-]{16,64}$/.test(normalized)) return;
        const button = sessionListNode.querySelector(`[data-agent-session-delete="${CSS.escape(normalized)}"]`);
        const sessionTitle = button?.closest('.agent-session-item')?.querySelector('.agent-session-open strong')?.textContent
            || '未命名会话';
        if (button && !button.classList.contains('is-armed')) {
            button.classList.add('is-armed');
            button.setAttribute('aria-label', `再次点击确认删除会话：${sessionTitle}`);
            announceSessionStatus(`将删除会话“${sessionTitle}”，请在 4 秒内再次点击删除按钮确认。`);
            window.setTimeout(() => {
                if (!button.isConnected || !button.classList.contains('is-armed')) return;
                button.classList.remove('is-armed');
                button.setAttribute('aria-label', `删除会话：${sessionTitle}`);
                announceSessionStatus(`已取消删除会话“${sessionTitle}”。`);
            }, 4000);
            return;
        }
        const sessionItem = button?.closest('.agent-session-item');
        if (sessionItem && window.MFAnim && typeof window.MFAnim.slideOutAndCollapse === 'function') {
            window.MFAnim.slideOutAndCollapse(sessionItem);
        }
        setSessionTransitionBusy(true);
        try {
            await fetchJSON(`/api/agent/sessions/${encodeURIComponent(normalized)}`, {method: 'DELETE'});
            if (agentSessionId === normalized) {
                conversationGeneration += 1;
                agentSessionId = createAgentSessionId();
                emptyTranscript();
            }
            await loadSessions();
            announceSessionStatus(`会话“${sessionTitle}”已删除。`);
        } catch (error) {
            announceSessionStatus(`删除会话“${sessionTitle}”失败。`);
            appendRequestError(error);
        } finally {
            setSessionTransitionBusy(false);
        }
    }

    async function resetConversation() {
        if (confirmationInFlight || sessionResetInFlight) return;
        const previousSessionId = agentSessionId;
        const generation = ++conversationGeneration;
        setSessionTransitionBusy(true);
        try {
            await stopActiveQuery({announceFailure: false});
            activeController?.abort();
            historyController?.abort();
            activeController = null;
            historyController = null;
            requestInFlight = false;
            setBusy(false);
            await fetchJSON('/api/agent/session/reset', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: previousSessionId}),
            });
            if (generation !== conversationGeneration) return;
            agentSessionId = createAgentSessionId();
            emptyTranscript();
            sessionListNode.querySelectorAll('.agent-session-item').forEach((item) => {
                item.classList.remove('is-active');
                item.querySelector('[data-agent-session-open]')?.removeAttribute('aria-current');
            });
        } catch (error) {
            if (generation === conversationGeneration) appendRequestError(error);
        } finally {
            if (generation === conversationGeneration) {
                setSessionTransitionBusy(false);
                promptInput.focus({preventScroll: true});
            }
        }
    }

    async function loadCapabilities() {
        if (!capabilityNode) return;
        try {
            const payload = await fetchJSON('/api/agent/capabilities');
            const tools = Array.isArray(payload.tools) ? payload.tools : [];
            const writes = tools.filter((tool) => tool?.requires_confirmation).length;
            capabilityNode.replaceChildren(
                node('strong', '', tools.length),
                node('span', '', `${tools.length - writes} 个只读工具 · ${writes} 个确认后执行`),
            );
        } catch (_) {
            capabilityNode.replaceChildren(
                node('strong', '', '—'),
                node('span', '', '能力清单暂不可用，不影响直接提问'),
            );
        }
    }

    composer.addEventListener('submit', (event) => {
        event.preventDefault();
        sendMessage(promptInput.value);
    });
    promptInput.addEventListener('input', resizePrompt);
    promptInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            composer.requestSubmit();
        }
    });
    newSessionButton.addEventListener('click', resetConversation);
    resumeLatestSessionButton?.addEventListener('click', () => {
        if (!latestSessionId || requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        openSession(latestSessionId);
    });
    stopButton?.addEventListener('click', () => { stopActiveQuery(); });
    function openHistoryRail() {
        if (!historyRail || historyRail.open) return;
        if (typeof historyRail.showModal === 'function') historyRail.showModal();
        else historyRail.setAttribute('open', '');
        railToggleButton?.setAttribute('aria-expanded', 'true');
        historyRail.querySelector('[data-agent-history-close]')?.focus({preventScroll: true});
    }

    function closeHistoryRail({returnFocus = true} = {}) {
        if (!historyRail?.open) return;
        historyReturnFocus = returnFocus;
        if (typeof historyRail.close === 'function') {
            historyRail.close();
            return;
        }
        historyRail.removeAttribute('open');
        railToggleButton?.setAttribute('aria-expanded', 'false');
        if (historyReturnFocus) railToggleButton?.focus({preventScroll: true});
        historyReturnFocus = true;
    }

    railToggleButton?.addEventListener('click', () => {
        if (historyRail?.open) closeHistoryRail();
        else openHistoryRail();
    });
    historyRail?.addEventListener('close', () => {
        railToggleButton?.setAttribute('aria-expanded', 'false');
        if (historyReturnFocus) railToggleButton?.focus({preventScroll: true});
        historyReturnFocus = true;
    });
    historyRail?.addEventListener('click', (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest('[data-agent-history-close]')) {
            closeHistoryRail();
            return;
        }
        if (event.target !== historyRail) return;
        const bounds = historyRail.getBoundingClientRect();
        const outside = event.clientX < bounds.left || event.clientX > bounds.right
            || event.clientY < bounds.top || event.clientY > bounds.bottom;
        if (outside) closeHistoryRail();
    });
    sessionListNode.addEventListener('click', (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const remove = target?.closest('[data-agent-session-delete]');
        if (remove) {
            deleteSession(remove.dataset.agentSessionDelete);
            return;
        }
        const open = target?.closest('[data-agent-session-open]');
        if (open) openSession(open.dataset.agentSessionOpen);
    });
    page.addEventListener('click', (event) => {
        const workspaceActionTrigger = event.target instanceof Element
            ? event.target.closest('[data-agent-workspace-action]')
            : null;
        if (workspaceActionTrigger) {
            runWorkspaceAction(workspaceActionTrigger);
            return;
        }
        const directToolTrigger = event.target instanceof Element
            ? event.target.closest('.agent-episode-chip, [data-agent-direct-tool]')
            : null;
        if (directToolTrigger) {
            runDirectTool(directToolTrigger);
            return;
        }
        const resourceTrigger = event.target instanceof Element
            ? event.target.closest('[data-agent-resource-id][data-agent-target]')
            : null;
        if (resourceTrigger) {
            prepareResourceSubmission(resourceTrigger);
            return;
        }
        const trigger = event.target instanceof Element
            ? event.target.closest('[data-agent-prompt], [data-agent-draft]')
            : null;
        if (!trigger || requestInFlight || confirmationInFlight || sessionResetInFlight) return;
        const draft = trigger.dataset.agentDraft;
        if (draft) {
            promptInput.value = draft;
            resizePrompt();
            promptInput.focus({preventScroll: true});
            const placeholder = ['剧名', '片名'].map((value) => draft.indexOf(value)).find((value) => value >= 0);
            if (placeholder !== undefined) promptInput.setSelectionRange(placeholder, placeholder + 2);
            return;
        }
        promptInput.value = trigger.dataset.agentPrompt || '';
        resizePrompt();
        composer.requestSubmit();
    });

    function syncVisualViewport() {
        const viewport = window.visualViewport;
        if (!viewport) return;
        if (visualViewportFrame) window.cancelAnimationFrame(visualViewportFrame);
        visualViewportFrame = window.requestAnimationFrame(() => {
            visualViewportFrame = 0;
            const viewportHeight = Math.max(1, Math.round(viewport.height));
            document.documentElement.style.setProperty('--agent-viewport-height', `${viewportHeight}px`);
            const keyboardVisible = viewport.height < window.innerHeight - 80;
            page.classList.toggle('has-visual-keyboard', keyboardVisible);
            if (keyboardVisible && document.activeElement === promptInput) scrollToLatest({force: true});
        });
    }

    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', syncVisualViewport, {passive: true});
        window.visualViewport.addEventListener('scroll', syncVisualViewport, {passive: true});
        window.addEventListener('pagehide', () => {
            window.visualViewport?.removeEventListener('resize', syncVisualViewport);
            window.visualViewport?.removeEventListener('scroll', syncVisualViewport);
        }, {once: true});
        syncVisualViewport();
    }

    syncConversationLayout();
    resizePrompt();
    Promise.allSettled([loadCapabilities(), loadSessions()]);
})();
