// Media Agent：唯一 AgentEvent 流的 Web 适配器。
(function () {
    'use strict';

    const page = document.querySelector('.agent-page');
    if (!page) return;

    const consoleNode = page.querySelector('.agent-console');
    const transcript = document.getElementById('agentTranscript');
    const composer = document.getElementById('agentComposer');
    const promptInput = document.getElementById('agentPrompt');
    const sendButton = document.getElementById('agentSend');
    const sessionStopButton = document.getElementById('agentStopSession');
    const newSessionButton = document.getElementById('agentNewSession');
    const resumeButton = document.getElementById('agentResumeLatestSession');
    const historyButton = document.getElementById('toggleAgentRail');
    const historyRail = document.getElementById('agentHistoryRail');
    const sessionList = document.getElementById('agentSessionList');
    const sessionCount = document.getElementById('agentSessionCount');
    const sessionStatus = document.getElementById('agentSessionStatus');
    const responseStatus = document.getElementById('agentResponseStatus');
    const nextActions = document.getElementById('agentStartActions');
    const resumeSlot = document.getElementById('agentStartResume');
    const composerActions = composer?.querySelector('.agent-composer-actions');
    const nextActionsStatus = document.getElementById('agentStartActionsStatus');
    const newRepliesButton = document.getElementById('agentNewReplies');
    const sessionSearch = document.getElementById('agentSessionSearch');
    const DRAFT_PREFIX = 'mediaflux.agent.drafts.v1.';
    const DRAFT_TTL_MS = 6 * 60 * 60 * 1000;
    const MAX_DRAFTS = 20;
    let scopedTrustBar = null;
    let scopedTrustExpiryTimer = null;

    const SESSION_KEY = 'mediaflux.agent.kernel.session.v1';
    const LAYOUT_KEY = 'mediaflux.agent.kernel.layout.v1';
    const restoreNotice = document.getElementById('agentRestoreNotice');
    const restoreText = document.getElementById('agentRestoreText');
    const restoreActions = document.getElementById('agentRestoreActions');
    const SESSION_RE = /^[A-Za-z0-9_-]{16,64}$/;
    const MAX_TRANSCRIPT_ITEMS = 120;
    const STREAM_MARKDOWN_INTERVAL_MS = 72;
    const SESSION_POLL_INTERVAL_MS = 1750;
    const SESSION_POLL_MAX_INTERVAL_MS = 8000;
    const RECEIPT_POLL_INTERVAL_MS = 5000;
    const RECEIPT_POLL_MAX_INTERVAL_MS = 30000;
    const RECEIPT_POLL_LIMIT = 2880;
    const RECEIPT_OBSERVATION_MAX_MS = 24 * 60 * 60 * 1000;
    const MAX_MARKDOWN_DEPTH = 4;
    const STREAM_INTERRUPTED_NOTICE = '连接中断，后续结果尚未确认；可刷新会话核对状态，不要重复提交。';
    const CONFIRMATION_STOP_NOTICE = '已停止等待；已提交操作可能继续执行，请核对任务状态。';
    const SESSION_STOP_MESSAGES = {
        stopped: '会话任务已停止，等待中的后台续行已取消。',
        already_stopped: '当前会话没有可停止的活动任务。',
        stopping: '停止请求已受理，仍在等待任务收尾。',
        partially_stopping: '已请求停止可取消的会话任务；部分后台任务不可取消，可能继续运行，请核对任务状态。',
        partial: '已停止可取消的会话任务；部分外部任务不可取消，可能继续运行，请核对任务状态。',
        critical_pending: '已记录停止请求；任务仍在不可中断阶段，请等待收尾并核对状态。',
        superseded: '会话状态已变化，无法确认当前任务已停止，请核对任务状态。',
        stop_unconfirmed: '会话停止尚未确认，任务可能仍在运行；请核对状态或重试停止。',
    };
    const TOOL_LABELS = {
        cloud: '读取光鸭云盘',
        guangya: '读取光鸭云盘',
        library: '查询媒体库',
        provider: '查询实时服务',
        downloads: '查询下载任务',
        download: '处理下载任务',
        indexer: '搜索资源',
        resource: '搜索资源',
        rss: '检查 RSS',
        media: '检查媒体订阅',
        discovery: '检索媒体信息',
        web: '查询公开信息',
        strm: '检查 STRM',
        local_media: '检查本地媒体',
        automation: '检查自动化任务',
        config: '检查项目配置',
    };

    let draftScope = '';
    let sessionId = storedSessionId() || createId('session');
    let sessionItems = [];
    let followOutput = true;
    let candidateExpiryTimer = null;
    const memoryDrafts = new Map();
    const sessionEdits = new Set();
    let latestSessionId = '';
    let activeRequest = null;
    const receiptObservationWindows = new Map();
    let receiptResumeNeeded = false;
    let receiptResumeInFlight = false;
    let receiptResumeRequested = false;
    let historyController = null;
    let sessionLoadGeneration = 0;
    let busy = false;
    let sessionStopPending = false;
    let initialRestore = consoleNode?.dataset.initialRestore === 'true';
    let startupAttempt = 0;
    let startupController = null;

    function createId(prefix) {
        let value = '';
        if (globalThis.crypto?.randomUUID) {
            value = globalThis.crypto.randomUUID().replaceAll('-', '');
        } else if (globalThis.crypto?.getRandomValues) {
            const bytes = new Uint8Array(24);
            globalThis.crypto.getRandomValues(bytes);
            value = Array.from(bytes, (item) => item.toString(16).padStart(2, '0')).join('');
        } else {
            value = `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
        }
        return `${prefix}_${value}`.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
    }

    function storedSessionId() {
        try {
            const key = draftScope ? `${SESSION_KEY}.${draftScope}` : SESSION_KEY;
            const value = localStorage.getItem(key) || '';
            return SESSION_RE.test(value) ? value : '';
        } catch (_) { return ''; }
    }

    function rememberSession(value) {
        if (sessionId !== value) renderScopedTrust(null);
        sessionId = value;
        try {
            localStorage.setItem(SESSION_KEY, value);
            if (draftScope) localStorage.setItem(`${SESSION_KEY}.${draftScope}`, value);
        } catch (_) { /* private mode */ }
    }

    function clipText(value, limit) {
        const text = String(value || '').slice(0, limit);
        // DOM maxlength 按 UTF-16 计数；截断时不要把 emoji 的代理对切成非法 JSON 文本。
        return /[\uD800-\uDBFF]$/.test(text) ? text.slice(0, -1) : text;
    }

    function classifyPublicTask(task) {
        if (task === null || task === undefined) return 'untracked';
        const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
        const text = value => typeof value === 'string' && value.length > 0;
        if (!object(task) || !Number.isInteger(task.v) || task.v !== 1
            || !text(task.task_id) || !Number.isInteger(task.epoch) || task.epoch <= 0
            || !Number.isInteger(task.revision) || task.revision <= 0
            || task.acceptance_scope !== 'declared_obligations'
            || !['partial', 'blocked', 'complete', 'cancelled'].includes(task.status)
            || !text(task.root_goal) || !Array.isArray(task.obligations) || !task.obligations.length) return 'unknown';
        const ids = new Set(), pending = new Set();
        for (const item of task.obligations) {
            if (!object(item) || !text(item.id) || ids.has(item.id) || !text(item.description)
                || item.required !== true || !['pending', 'blocked', 'verified'].includes(item.state)) return 'unknown';
            ids.add(item.id);
            if (item.state !== 'verified') pending.add(item.id);
        }
        const remaining = task.remaining_obligation_ids;
        if (!Array.isArray(remaining) || !remaining.every(value => typeof value === 'string')
            || new Set(remaining).size !== remaining.length || remaining.length !== pending.size
            || !remaining.every(id => pending.has(id)) || !Array.isArray(task.blockers)
            || !task.blockers.every(item => object(item) && text(item.text))) return 'unknown';
        if (task.status === 'complete' && (remaining.length || task.blockers.length)) return 'unknown';
        return task.status;
    }

    function clearTaskSnapshot() {
        document.getElementById('agentTaskSnapshot')?.remove();
    }

    function placeTaskSnapshot(panel = document.getElementById('agentTaskSnapshot')) {
        if (!panel) return;
        if (consoleNode?.classList.contains('is-empty')) {
            const startPanel = consoleNode.querySelector('.agent-start-panel');
            if (startPanel && panel.parentElement !== startPanel) startPanel.prepend(panel);
        } else if (composer && panel.nextElementSibling !== composer) {
            composer.before(panel);
        }
    }

    function renderTaskSnapshot(task) {
        const status = classifyPublicTask(task);
        if (status === 'untracked') {
            clearTaskSnapshot();
            return;
        }
        const labels = {
            complete: '已核验全部检查项', partial: `${task?.remaining_obligation_ids?.length} 项待核验`,
            blocked: '任务有阻塞', cancelled: '任务已取消', unknown: '任务进度待核对',
        };
        let panel = document.getElementById('agentTaskSnapshot');
        if (!panel) {
            panel = element('details', 'agent-task-snapshot');
            panel.id = 'agentTaskSnapshot';
            panel.append(element('summary', ''), element('div', 'agent-task-details'));
        }
        const taskId = status === 'unknown' ? '' : task.task_id;
        if (panel.dataset.taskId !== taskId) panel.open = false;
        panel.dataset.taskId = taskId;
        panel.dataset.taskStatus = status;
        panel.firstElementChild.textContent = labels[status];
        const details = panel.lastElementChild;
        const scrollTop = details.scrollTop;
        if (status === 'unknown') {
            details.replaceChildren(element('p', '', '暂时无法读取任务进度，请核对会话记录后再继续。'));
        } else {
            details.replaceChildren(element('p', '', `目标：${clipText(task.root_goal, 240)}`));
            details.append(element('p', 'text-muted', '仅反映本会话记录的检查项，不代表其他后续操作已完成。'));
            const remaining = new Set(task.remaining_obligation_ids);
            const descriptions = task.obligations.filter(item => remaining.has(item.id));
            descriptions.slice(0, 4).forEach(item => details.append(element('p', '', `待核验：${clipText(item.description, 240)}`)));
            task.blockers.slice(0, 3).forEach(item => details.append(element('p', '', `阻塞：${clipText(item.text, 240)}`)));
            if (descriptions.length > 4 || task.blockers.length > 3) details.append(element('p', 'text-muted', '仅显示部分检查项与阻塞。'));
        }
        placeTaskSnapshot(panel);
        details.scrollTop = scrollTop;
    }

    function consumeTerminalTask(event) {
        if (['turn.completed', 'turn.failed', 'turn.cancelled'].includes(event.type)) renderTaskSnapshot(event.payload?.task);
    }

    function readDrafts() {
        if (!draftScope) return {};
        try {
            const value = JSON.parse(sessionStorage.getItem(DRAFT_PREFIX + draftScope) || '{}');
            if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
            return Object.fromEntries(Object.entries(value).filter(([id, draft]) =>
                SESSION_RE.test(id) && typeof draft?.text === 'string' && draft.text.length <= 1000 &&
                Number.isFinite(draft.updated_at) && draft.updated_at <= Date.now() &&
                Date.now() - draft.updated_at < DRAFT_TTL_MS
            ).sort((a, b) => b[1].updated_at - a[1].updated_at).slice(0, MAX_DRAFTS));
        } catch (_) { return {}; }
    }

    function saveDraft() {
        const text = clipText(promptInput?.value, 1000);
        memoryDrafts.set(sessionId, text);
        if (memoryDrafts.size > MAX_DRAFTS) memoryDrafts.delete(memoryDrafts.keys().next().value);
        if (!draftScope) return;
        const drafts = readDrafts();
        const looksSensitive = /(?:password|passwd|api[_-]?key|secret|token|cookie|authorization|密码|密钥|令牌)\s*[:=]/i.test(text) || /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(text);
        if (text && !looksSensitive) drafts[sessionId] = {text, updated_at: Date.now()};
        else delete drafts[sessionId];
        const bounded = Object.fromEntries(Object.entries(drafts)
            .sort((a, b) => b[1].updated_at - a[1].updated_at).slice(0, MAX_DRAFTS));
        try { sessionStorage.setItem(DRAFT_PREFIX + draftScope, JSON.stringify(bounded)); } catch (_) { /* storage optional */ }
        rememberSession(sessionId);
    }

    function removeDraft(id) {
        memoryDrafts.delete(id);
        if (!draftScope) return;
        const drafts = readDrafts();
        delete drafts[id];
        try { sessionStorage.setItem(DRAFT_PREFIX + draftScope, JSON.stringify(drafts)); } catch (_) { /* storage optional */ }
    }

    function restoreDraft() {
        if (!promptInput) return;
        promptInput.value = memoryDrafts.has(sessionId)
            ? memoryDrafts.get(sessionId) : (readDrafts()[sessionId]?.text || '');
        resizePrompt();
    }

    function configureDraftScope(value) {
        if (typeof value !== 'string' || !/^[a-f0-9]{32,64}$/.test(value) || draftScope === value) return false;
        // 首次鉴权响应前输入的内容属于当前页面，不被迟到的持久草稿覆盖。
        let typed = String(promptInput?.value || '');
        const accountChanged = Boolean(draftScope);
        if (accountChanged) {
            clearTaskSnapshot();
            stopInitialRestore();
            // 同一页面的登录主体改变时，不把旧主体的请求、内存或输入传给新主体。
            ++sessionLoadGeneration;
            invalidateActiveRequest();
            setBusy(false);
            expireCandidateCards();
            memoryDrafts.clear();
            typed = '';
            if (promptInput) promptInput.value = '';
            transcript?.replaceChildren();
            followOutput = true;
            if (newRepliesButton) newRepliesButton.hidden = true;
        }
        draftScope = value;
        if (accountChanged) sessionId = storedSessionId() || createId('session');
        const scopedSession = storedSessionId();
        if (!busy && !typed && scopedSession) sessionId = scopedSession;
        if (!typed && !busy) restoreDraft();
        saveDraft();
        if (accountChanged) setConsoleEmpty(true);
        return accountChanged;
    }

    function fillDraft(text) {
        const value = clipText(String(text || '').trim(), 1000);
        if (!promptInput || !value) return;
        if (promptInput.value.trim() && promptInput.value.trim() !== value) {
            announce(responseStatus, '输入框已有草稿，请先发送或清空后再选择。');
            window.showToast?.('已保留输入框中的草稿，请先发送或清空后再选择', 'warning');
            promptInput.focus();
            return;
        }
        promptInput.value = value;
        saveDraft();
        resizePrompt();
        promptInput.focus();
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function icon(name) {
        const node = document.createElement('i');
        node.setAttribute('data-lucide', name);
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function renderIcons(root) {
        window.renderLucideIcons?.(root || page);
    }

    function announce(node, value) {
        if (node) node.textContent = String(value || '');
    }

    function setConsoleEmpty(empty) {
        consoleNode?.classList.toggle('is-empty', Boolean(empty));
        placeTaskSnapshot();
        if (!initialRestore) {
            try { localStorage.setItem(LAYOUT_KEY, JSON.stringify({session_id: sessionId, mode: empty ? 'empty' : 'conversation'})); } catch (_) { /* 可选布局提示。 */ }
        }
        const resumeParent = empty && resumeSlot ? resumeSlot : composerActions;
        if (resumeButton && resumeParent && resumeButton.parentElement !== resumeParent) {
            const wasFocused = document.activeElement === resumeButton;
            resumeParent.append(resumeButton);
            if (wasFocused && !resumeButton.disabled) resumeButton.focus({preventScroll: true});
        }
        if (promptInput) {
            promptInput.placeholder = empty
                ? (promptInput.dataset.emptyPlaceholder || '询问 MediaFlux')
                : (promptInput.dataset.activePlaceholder || '继续描述或调整任务');
        }
        resizePrompt();
    }

    function transcriptNearBottom() {
        return !transcript || transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 140;
    }

    function scrollToBottom(force = false) {
        if (!transcript) return;
        if (!force && !followOutput) {
            if (newRepliesButton) newRepliesButton.hidden = false;
            return;
        }
        if (force) followOutput = true;
        if (newRepliesButton) newRepliesButton.hidden = true;
        transcript.scrollTop = transcript.scrollHeight;
        requestAnimationFrame(() => {
            if (followOutput) transcript.scrollTop = transcript.scrollHeight;
        });
    }

    function pruneTranscript() {
        if (!transcript) return;
        while (transcript.children.length > MAX_TRANSCRIPT_ITEMS) {
            transcript.firstElementChild?.remove();
        }
    }

    function appendMessage(role, {recovered = false, scroll = true} = {}) {
        const item = element('article', `agent-message agent-message-${role}`);
        if (recovered) item.classList.add('is-recovered');
        const mark = element('div', 'agent-message-mark');
        mark.append(icon(role === 'user' ? 'user-round' : 'bot'));
        const body = element('div', 'agent-message-body');
        item.append(mark, body);
        transcript?.append(item);
        pruneTranscript();
        setConsoleEmpty(false);
        renderIcons(item);
        if (scroll) scrollToBottom(true);
        return {item, body};
    }

    function appendUser(text, options = {}) {
        const view = appendMessage('user', options);
        view.body.append(element('p', '', text));
        return view;
    }

    function appendText(parent, value) {
        const text = String(value || '');
        if (!text) return;
        const previous = parent.lastChild;
        if (previous?.nodeType === 3) previous.nodeValue += text;
        else parent.append(document.createTextNode(text));
    }

    function safeMarkdownLink(rawHref) {
        const value = String(rawHref || '').trim();
        if (!value) return null;
        if (value.startsWith('#') || (value.startsWith('/') && !value.startsWith('//')) || value.startsWith('?')) {
            return {href: value, external: false};
        }
        try {
            const parsed = new URL(value, window.location.href);
            const protocol = parsed.protocol.toLowerCase();
            if (!['http:', 'https:', 'mailto:'].includes(protocol)) return null;
            return {
                href: parsed.href,
                external: protocol === 'mailto:' || parsed.origin !== window.location.origin,
            };
        } catch (_) {
            return null;
        }
    }

    function trimBareUrl(value) {
        let url = String(value || '');
        while (/[.,;:!?，。；：！？》】）}]$/.test(url)) url = url.slice(0, -1);
        return url;
    }

    function appendMarkdownLink(parent, label, rawHref, depth) {
        const target = safeMarkdownLink(rawHref);
        if (!target) {
            appendInlineMarkdown(parent, label, depth + 1);
            return;
        }
        const anchor = element('a', 'agent-md-link');
        anchor.href = target.href;
        if (target.external) {
            anchor.target = '_blank';
            anchor.rel = 'noopener noreferrer';
        }
        if (/^(?:https?:\/\/|mailto:)/i.test(label)) appendText(anchor, label);
        else appendInlineMarkdown(anchor, label, depth + 1);
        parent.append(anchor);
    }

    function markdownLinkAt(source, start) {
        const image = source.startsWith('![', start);
        const labelStart = start + (image ? 2 : 1);
        const labelEnd = source.indexOf('](', labelStart);
        if (labelEnd < 0) return null;
        let cursor = labelEnd + 2;
        let nesting = 0;
        let escaped = false;
        for (; cursor < source.length; cursor += 1) {
            const character = source[cursor];
            if (escaped) {
                escaped = false;
                continue;
            }
            if (character === '\\') {
                escaped = true;
                continue;
            }
            if (character === '(') nesting += 1;
            if (character === ')' && nesting > 0) nesting -= 1;
            else if (character === ')' && nesting === 0) break;
        }
        if (cursor >= source.length) return null;
        const destination = source.slice(labelEnd + 2, cursor).trim();
        const match = destination.match(/^(?:<([^>]+)>|([^\s]+))(?:\s+["'].*["'])?$/);
        if (!match) return null;
        return {
            image,
            label: source.slice(labelStart, labelEnd),
            href: match[1] || match[2] || '',
            end: cursor + 1,
        };
    }

    function appendInlineMarkdown(parent, input, depth = 0) {
        const source = String(input || '');
        if (!source || depth > MAX_MARKDOWN_DEPTH) {
            appendText(parent, source);
            return;
        }
        let index = 0;
        while (index < source.length) {
            const escapedCharacter = source[index + 1] || '';
            if (source[index] === '\\' && '\\`*{}[]()#+-.!_|>~'.includes(escapedCharacter)) {
                appendText(parent, source[index + 1]);
                index += 2;
                continue;
            }

            if (source[index] === '`') {
                const marker = source.slice(index).match(/^`+/)?.[0] || '`';
                const end = source.indexOf(marker, index + marker.length);
                if (end >= 0) {
                    const code = element('code', 'agent-md-inline-code', source.slice(index + marker.length, end).replace(/^ | $/g, ''));
                    parent.append(code);
                    index = end + marker.length;
                    continue;
                }
            }

            if (source.startsWith('![', index) || source[index] === '[') {
                const link = markdownLinkAt(source, index);
                if (link) {
                    if (link.image) {
                        const alt = element('span', 'agent-md-image-alt');
                        alt.setAttribute('role', 'img');
                        alt.setAttribute('aria-label', link.label || '图片');
                        alt.append(icon('image'), element('span', '', link.label || '图片'));
                        parent.append(alt);
                    } else {
                        appendMarkdownLink(parent, link.label, link.href, depth);
                    }
                    index = link.end;
                    continue;
                }
            }

            const strongMarker = source.startsWith('**', index) ? '**' : source.startsWith('__', index) ? '__' : '';
            if (strongMarker) {
                const end = source.indexOf(strongMarker, index + 2);
                if (end > index + 2) {
                    const strong = document.createElement('strong');
                    appendInlineMarkdown(strong, source.slice(index + 2, end), depth + 1);
                    parent.append(strong);
                    index = end + 2;
                    continue;
                }
            }

            if (source.startsWith('~~', index)) {
                const end = source.indexOf('~~', index + 2);
                if (end > index + 2) {
                    const deleted = document.createElement('del');
                    appendInlineMarkdown(deleted, source.slice(index + 2, end), depth + 1);
                    parent.append(deleted);
                    index = end + 2;
                    continue;
                }
            }

            const emphasisMarker = source[index] === '*' ? '*' : source[index] === '_' ? '_' : '';
            if (emphasisMarker) {
                const previous = source[index - 1] || '';
                const next = source[index + 1] || '';
                const canOpen = next && !/\s/.test(next) && !(emphasisMarker === '_' && /[\p{L}\p{N}]/u.test(previous));
                const end = canOpen ? source.indexOf(emphasisMarker, index + 1) : -1;
                if (end > index + 1) {
                    const emphasis = document.createElement('em');
                    appendInlineMarkdown(emphasis, source.slice(index + 1, end), depth + 1);
                    parent.append(emphasis);
                    index = end + 1;
                    continue;
                }
            }

            if (source[index] === '<') {
                const autoLink = source.slice(index).match(/^<(https?:\/\/[^>]+|mailto:[^>]+)>/i);
                if (autoLink) {
                    appendMarkdownLink(parent, autoLink[1], autoLink[1], depth);
                    index += autoLink[0].length;
                    continue;
                }
            }

            const beginsBareLink = source.startsWith('http://', index) || source.startsWith('https://', index);
            const bareLink = beginsBareLink ? source.slice(index).match(/^https?:\/\/[^\s<]+/i) : null;
            if (bareLink) {
                const href = trimBareUrl(bareLink[0]);
                appendMarkdownLink(parent, href, href, depth);
                index += href.length;
                continue;
            }

            let next = index + 1;
            while (next < source.length) {
                const character = source[next];
                if ('\\`*_[~<'.includes(character)
                    || character === '['
                    || (character === '!' && source[next + 1] === '[')
                    || source.startsWith('http://', next)
                    || source.startsWith('https://', next)) break;
                next += 1;
            }
            appendText(parent, source.slice(index, next));
            index = next;
        }
    }

    function splitMarkdownTableRow(value) {
        let line = String(value || '').trim();
        if (line.startsWith('|')) line = line.slice(1);
        if (line.endsWith('|')) line = line.slice(0, -1);
        const cells = [];
        let current = '';
        let codeMarker = false;
        for (let index = 0; index < line.length; index += 1) {
            const character = line[index];
            const next = line[index + 1] || '';
            if (character === '\\' && ['|', '\\', '`'].includes(next)) {
                current += next;
                index += 1;
                continue;
            }
            if (character === '`') codeMarker = !codeMarker;
            if (character === '|' && !codeMarker) {
                cells.push(current.trim());
                current = '';
            } else {
                current += character;
            }
        }
        cells.push(current.trim());
        return cells;
    }

    function markdownTableDefinition(lines, index) {
        if (index + 1 >= lines.length || !lines[index].includes('|')) return null;
        const header = splitMarkdownTableRow(lines[index]);
        const separators = splitMarkdownTableRow(lines[index + 1]);
        if (header.length < 2 || header.length !== separators.length) return null;
        if (!separators.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s/g, '')))) return null;
        return {header, separators};
    }

    function markdownListItem(value) {
        const unordered = String(value || '').match(/^\s{0,3}[-+*•]\s+(.+)$/);
        if (unordered) return {ordered: false, value: unordered[1], start: 1};
        const ordered = String(value || '').match(/^\s{0,3}(\d{1,4})[.)、]\s+(.+)$/);
        return ordered ? {ordered: true, value: ordered[2], start: Number(ordered[1]) || 1} : null;
    }

    function isMarkdownBlockStart(lines, index) {
        const line = String(lines[index] || '');
        return /^\s{0,3}(?:`{3,}|~{3,})/.test(line)
            || /^\s{0,3}#{1,6}\s+/.test(line)
            || /^\s{0,3}>/.test(line)
            || /^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line)
            || Boolean(markdownListItem(line))
            || Boolean(markdownTableDefinition(lines, index));
    }

    function appendMarkdownBlocks(root, input, depth = 0) {
        const lines = String(input || '').replace(/\r\n?/g, '\n').split('\n');
        let index = 0;
        while (index < lines.length) {
            const raw = lines[index];
            const line = raw.trim();
            if (!line) {
                index += 1;
                continue;
            }

            const fence = raw.match(/^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)\s*$/);
            if (fence) {
                const marker = fence[1];
                const language = String(fence[2] || '').replace(/[^A-Za-z0-9_+.-]/g, '').slice(0, 24);
                const values = [];
                index += 1;
                while (index < lines.length && !new RegExp(`^\\s{0,3}${marker[0]}{${marker.length},}\\s*$`).test(lines[index])) {
                    values.push(lines[index]);
                    index += 1;
                }
                if (index < lines.length) index += 1;
                const frame = element('div', 'agent-md-code-frame');
                if (language) frame.append(element('span', 'agent-md-code-language', language));
                const pre = document.createElement('pre');
                pre.append(element('code', '', values.join('\n')));
                frame.append(pre);
                root.append(frame);
                continue;
            }

            const heading = raw.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
            if (heading) {
                const level = heading[1].length;
                const title = element(`h${Math.min(6, level + 1)}`, `agent-md-heading agent-md-heading-${level}`);
                appendInlineMarkdown(title, heading[2]);
                root.append(title);
                index += 1;
                continue;
            }

            if (/^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(raw)) {
                root.append(document.createElement('hr'));
                index += 1;
                continue;
            }

            if (/^\s{0,3}>/.test(raw)) {
                const quoteLines = [];
                while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
                    quoteLines.push(lines[index].replace(/^\s{0,3}>\s?/, ''));
                    index += 1;
                }
                const quote = document.createElement('blockquote');
                if (depth < MAX_MARKDOWN_DEPTH) appendMarkdownBlocks(quote, quoteLines.join('\n'), depth + 1);
                else appendText(quote, quoteLines.join('\n'));
                root.append(quote);
                continue;
            }

            const table = markdownTableDefinition(lines, index);
            if (table) {
                const scroll = element('div', 'agent-md-table-scroll');
                scroll.tabIndex = 0;
                scroll.setAttribute('role', 'region');
                scroll.setAttribute('aria-label', 'Markdown 表格');
                const tableNode = document.createElement('table');
                const head = document.createElement('thead');
                const headRow = document.createElement('tr');
                table.header.forEach((value, cellIndex) => {
                    const cell = document.createElement('th');
                    const separator = table.separators[cellIndex].replace(/\s/g, '');
                    if (separator.startsWith(':') && separator.endsWith(':')) cell.className = 'is-center';
                    else if (separator.endsWith(':')) cell.className = 'is-right';
                    appendInlineMarkdown(cell, value);
                    headRow.append(cell);
                });
                head.append(headRow);
                tableNode.append(head);
                const body = document.createElement('tbody');
                index += 2;
                while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
                    const values = splitMarkdownTableRow(lines[index]);
                    const row = document.createElement('tr');
                    table.header.forEach((_, cellIndex) => {
                        const cell = document.createElement('td');
                        const separator = table.separators[cellIndex].replace(/\s/g, '');
                        if (separator.startsWith(':') && separator.endsWith(':')) cell.className = 'is-center';
                        else if (separator.endsWith(':')) cell.className = 'is-right';
                        appendInlineMarkdown(cell, values[cellIndex] || '');
                        row.append(cell);
                    });
                    body.append(row);
                    index += 1;
                }
                tableNode.append(body);
                scroll.append(tableNode);
                root.append(scroll);
                continue;
            }

            const listItem = markdownListItem(raw);
            if (listItem) {
                const list = document.createElement(listItem.ordered ? 'ol' : 'ul');
                if (listItem.ordered && listItem.start !== 1) list.start = listItem.start;
                while (index < lines.length) {
                    const item = markdownListItem(lines[index]);
                    if (!item || item.ordered !== listItem.ordered) break;
                    const row = document.createElement('li');
                    const task = item.value.match(/^\[([ xX])\]\s+(.+)$/);
                    if (task) {
                        row.className = 'agent-md-task';
                        const marker = element('span', 'agent-md-task-marker', task[1].toLowerCase() === 'x' ? '✓' : '');
                        marker.setAttribute('aria-hidden', 'true');
                        row.append(marker);
                        appendInlineMarkdown(row, task[2]);
                    } else {
                        appendInlineMarkdown(row, item.value);
                    }
                    list.append(row);
                    index += 1;
                }
                root.append(list);
                continue;
            }

            const paragraphLines = [];
            while (index < lines.length && lines[index].trim() && (paragraphLines.length === 0 || !isMarkdownBlockStart(lines, index))) {
                paragraphLines.push(lines[index]);
                index += 1;
            }
            const paragraphText = paragraphLines.map((value) => value.trim()).join(' ');
            const paragraph = element('p', root.childElementCount === 0 && paragraphText.length <= 140 ? 'agent-answer-lead' : '');
            paragraphLines.forEach((value, lineIndex) => {
                const hardBreak = /\s{2}$/.test(value) || /\\$/.test(value);
                appendInlineMarkdown(paragraph, value.replace(/(?:\s{2}|\\)$/, '').trim());
                if (lineIndex < paragraphLines.length - 1) paragraph.append(hardBreak ? document.createElement('br') : document.createTextNode(' '));
            });
            root.append(paragraph);
        }
    }

    function parseTextBlocks(text) {
        const root = element('div', 'agent-rich-text');
        appendMarkdownBlocks(root, text);
        return root;
    }

    function replaceRichText(target, text) {
        const rendered = parseTextBlocks(text);
        target.classList.add('agent-rich-text');
        target.replaceChildren(...rendered.childNodes);
        if (target.querySelector('[data-lucide]')) renderIcons(target);
    }

    function cancelTurnMarkdownRender(turn) {
        if (!turn) return;
        if (turn.markdownTimer !== null) window.clearTimeout(turn.markdownTimer);
        if (turn.markdownFrame !== null) window.cancelAnimationFrame(turn.markdownFrame);
        turn.markdownTimer = null;
        turn.markdownFrame = null;
    }

    function normalizeStreamingMarkdown(text) {
        const value = String(text || '');
        const lines = value.split('\n');
        const last = lines[lines.length - 1] || '';
        const markers = last.match(/\*\*/g);
        if (markers && markers.length % 2 === 1 && !/\*\*\s*$/.test(last)) {
            lines[lines.length - 1] = `${last.replace(/\*$/, '')}**`;
            return lines.join('\n');
        }
        return value;
    }

    function renderTurnMarkdown(turn, text, {immediate = false} = {}) {
        if (!turn?.text) return;
        turn.pendingMarkdown = String(text || '');
        const commit = () => {
            const shouldFollow = transcriptNearBottom();
            turn.markdownTimer = null;
            turn.markdownFrame = null;
            replaceRichText(turn.text, normalizeStreamingMarkdown(turn.pendingMarkdown));
            turn.lastMarkdownRender = performance.now();
            if (shouldFollow) scrollToBottom(true);
        };
        if (immediate) {
            cancelTurnMarkdownRender(turn);
            commit();
            return;
        }
        if (turn.markdownTimer !== null || turn.markdownFrame !== null) return;
        const delay = Math.max(0, STREAM_MARKDOWN_INTERVAL_MS - (performance.now() - turn.lastMarkdownRender));
        turn.markdownTimer = window.setTimeout(() => {
            turn.markdownTimer = null;
            turn.markdownFrame = window.requestAnimationFrame(commit);
        }, delay);
    }

    function createStreamingCard() {
        const card = element('section', 'agent-result-card agent-streaming');
        const head = element('div', 'agent-stream-head');
        head.append(icon('loader-circle'), element('span', '', '正在理解任务'));
        const text = element('div', 'agent-stream-text agent-rich-text');
        const steps = element('div', 'agent-stream-steps');
        card.append(head, text, steps);
        return {
            card,
            head,
            headText: head.querySelector('span'),
            text,
            steps,
        };
    }

    function createAssistantTurn({recovered = false, scroll = true} = {}) {
        const view = appendMessage('assistant', {recovered, scroll});
        const stream = createStreamingCard();
        view.body.append(stream.card);
        renderIcons(stream.card);
        return {
            ...view,
            ...stream,
            rounds: new Map(),
            currentRound: 0,
            toolSteps: new Map(),
            effectReceipts: new Map(),
            completedPlanIds: new Set(),
            approvalNode: null,
            pendingMarkdown: '',
            markdownTimer: null,
            markdownFrame: null,
            lastMarkdownRender: 0,
        };
    }

    function setTurnStatus(turn, label, iconName = 'loader-circle') {
        if (!turn?.head) return;
        // 阶段切换先提交已收到的正文；正文不能依赖可能暂停的动画帧才追上状态。
        if (turn.markdownTimer !== null || turn.markdownFrame !== null) {
            renderTurnMarkdown(turn, turn.pendingMarkdown, {immediate: true});
        }
        if (!turn.headText?.isConnected) {
            turn.head.replaceChildren(icon(iconName), element('span', '', label));
            turn.headText = turn.head.querySelector('span');
            renderIcons(turn.head);
            return;
        }
        turn.headText.textContent = label;
        const currentIcon = turn.head.querySelector('svg');
        if (currentIcon?.getAttribute('data-lucide') !== iconName) {
            currentIcon?.replaceWith(icon(iconName));
            renderIcons(turn.head);
        }
    }

    function toolLabel(tool, label = '') {
        const explicit = String(label || '').trim();
        if (explicit) return explicit;
        const prefix = String(tool || '').split('.', 1)[0].toLowerCase();
        return TOOL_LABELS[prefix] || '调用项目能力';
    }

    function updateStep(turn, key, label, {warning = false, pending = false, iconName = ''} = {}) {
        if (!turn?.steps || !key) return;
        let row = turn.toolSteps.get(key);
        if (!row) {
            row = element('div', 'agent-stream-step');
            row.dataset.stepKey = key;
            turn.toolSteps.set(key, row);
            turn.steps.append(row);
        }
        row.classList.toggle('is-warning', warning);
        row.classList.toggle('is-pending', pending);
        row.replaceChildren(
            icon(iconName || (pending ? 'loader-circle' : warning ? 'triangle-alert' : 'check')),
            element('span', '', label),
        );
        renderIcons(row);
        scrollToBottom();
    }

    function buildToolTrace(turn) {
        if (!turn?.steps || !turn.steps.childElementCount) {
            turn?.toolTrace?.remove();
            if (turn) turn.toolTrace = null;
            turn?.steps?.remove();
            return null;
        }
        const trace = turn.toolTrace || element('details', 'agent-tool-trace');
        let summary = trace.querySelector('.agent-tool-trace-summary');
        if (!summary) {
            summary = element('summary', 'agent-tool-trace-summary');
            summary.append(icon('list-checks'), element('span'), icon('chevron-down'));
            trace.append(summary);
        }
        const label = summary.querySelector('span');
        if (label) label.textContent = `执行过程 · ${turn.steps.childElementCount} 步`;
        if (turn.steps.parentElement !== trace) trace.append(turn.steps);
        turn.toolTrace = trace;
        renderIcons(trace);
        return trace;
    }

    function addRecoveredToolTrace(turn, tools, labels = []) {
        if (!Array.isArray(tools)) return;
        for (const [index, name] of tools.entries()) {
            const normalized = String(name || '').trim();
            if (!normalized) continue;
            updateStep(turn, `recovered:${index}:${normalized}`, `${toolLabel(normalized, labels[index])}完成`);
        }
    }

    function publicSummary(value) {
        if (value && typeof value === 'object') {
            for (const key of ['summary', 'message', 'title', 'status']) {
                if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
            }
        }
        return typeof value === 'string' ? value.trim() : '';
    }

    function finalizeAnswer(turn, text) {
        turn.failed = false;
        turn.followupFailed = false;
        cancelTurnMarkdownRender(turn);
        promoteTurnCard(turn);
        const answer = typeof text === 'string' ? text : '';
        if (!answer.trim() && turn.candidateGroup) {
            turn.head?.remove();
            turn.text?.remove();
            scrollToBottom();
            return;
        }
        if (!answer.trim() && turn.effectReceipts?.size) {
            finalizeError(turn, '服务端未提供最终答复；请以执行回执核对状态。');
            return;
        }
        if (!answer.trim()) {
            turn.item?.remove();
            setConsoleEmpty(!transcript?.childElementCount);
            return;
        }
        turn.card.classList.remove('agent-streaming', 'is-interrupted');
        turn.card.classList.add('has-narrative', 'is-conversation');
        turn.item?.classList.remove('is-confirmation');
        turn.head.remove();
        turn.text.className = 'agent-narrative agent-rich-text';
        replaceRichText(turn.text, answer);
        const trace = buildToolTrace(turn);
        if (trace) turn.card.append(trace);
        if (turn.candidateGroup && !turn.card.querySelector('.agent-result-downloads')) {
            const link = element('a', 'agent-result-downloads', '查看下载任务');
            link.href = '/downloads';
            turn.card.append(link);
            syncCandidateButtons();
        }
        scrollToBottom();
    }

    function finalizeError(turn, message, {cancelled = false} = {}) {
        turn.failed = true;
        turn.cancelled = cancelled;
        cancelTurnMarkdownRender(turn);
        const trustedReceipt = trustedEffectReceipt(turn);
        const protectedWriteStop = cancelled && turn.confirmationRequested === true;
        const finalMessage = protectedWriteStop
            ? CONFIRMATION_STOP_NOTICE
            : message || (cancelled ? '本轮处理已停止。' : 'Agent 暂时无法完成该请求。');
        promoteTurnCard(turn);
        if (trustedReceipt) {
            turn.followupFailed = true;
            turn.card.classList.remove('agent-streaming', 'is-interrupted', 'agent-cancelled');
            turn.card.classList.add('has-narrative', 'is-conversation');
            turn.card.classList.toggle('is-interrupted', Boolean(turn.effectError));
            turn.item?.classList.remove('is-confirmation');
            setTurnStatus(turn, '已保留服务端回执，后续状态需核对', 'triangle-alert');
            turn.text.className = 'agent-narrative agent-rich-text';
            replaceRichText(
                turn.text,
                turn.effectError && [...turn.effectReceipts.values()].every(value => value.receipt)
                    ? trustedReceipt : `${trustedReceipt}\n\n⚠️ 后续流程未完成：${finalMessage}`,
            );
            const trace = buildToolTrace(turn);
            if (trace) turn.card.append(trace);
            scrollToBottom();
            return;
        }
        turn.card.classList.remove('agent-streaming');
        turn.card.classList.add(protectedWriteStop ? 'is-interrupted' : cancelled ? 'agent-cancelled' : 'is-interrupted');
        setTurnStatus(
            turn,
            protectedWriteStop ? '已停止等待，状态待核对' : cancelled ? '已停止' : '未能完成',
            protectedWriteStop ? 'triangle-alert' : cancelled ? 'circle-stop' : 'triangle-alert',
        );
        turn.text.textContent = finalMessage;
        const trace = buildToolTrace(turn);
        if (trace) turn.card.append(trace);
        if (turn.requestMessage && !turn.boundSelection && !turn.card.querySelector('.agent-retry-draft')) {
            const actions = element('div', 'agent-retry-actions');
            const retry = element('button', 'agent-retry-draft', '放回输入框修改');
            retry.type = 'button';
            retry.dataset.agentDraft = turn.requestMessage;
            actions.append(retry);
            turn.card.append(actions);
        }
        scrollToBottom();
    }

    function approvalTargetLabel(value) {
        const target = String(value || '').trim().toLowerCase();
        return ({guangya: '光鸭云盘', qb: 'qBittorrent', qbittorrent: 'qBittorrent', both: 'qBittorrent ＋ 光鸭'})[target] || target;
    }

    function scalarPreviewRows(data, confirmation = {}) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
        const rows = [];
        const object = String(confirmation.object || '').trim();
        if (object) rows.push(['操作对象', object.slice(0, 320)]);
        const target = approvalTargetLabel(data.target);
        if (target) rows.push(['目标', target.slice(0, 80)]);
        const count = Number.isInteger(data.count) ? data.count : Number.isInteger(data.total) ? data.total : null;
        if (count !== null) rows.push(['数量', `${count} 项`]);
        for (const folder of (Array.isArray(data.receiving_folders) ? data.receiving_folders : []).slice(0, 3)) rows.push(['接收目录', String(folder)]);
        for (const [key, label] of [['selected', '已选择'], ['review_required', '待复核']]) {
            if (Number.isInteger(data[key])) rows.push([label, `${data[key]} 项`]);
        }
        return rows.slice(0, 6);
    }

    function approvalListItem(value) {
        if (typeof value === 'string') return value.trim();
        if (!value || typeof value !== 'object') return '';
        for (const key of ['title', 'summary', 'action', 'description', 'name']) {
            if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
        }
        return '';
    }

    function buildApprovalScope(data) {
        if (!data || typeof data !== 'object') return null;
        const resources = Array.isArray(data.resources) ? data.resources : data.resource ? [data.resource] : [];
        const effects = Array.isArray(data.effects) ? data.effects : [];
        if (!resources.length && !effects.length) return null;
        const scope = element('div', 'agent-confirmation-scope');
        if (resources.length) {
            scope.append(element('h4', '', '将处理'));
            const list = element('ul', 'agent-confirmation-list');
            const remaining = element('ul', 'agent-confirmation-list');
            for (const item of resources) {
                if (!item || typeof item !== 'object') continue;
                const title = approvalListItem(item) || '未命名资源';
                const site = String(item.site_name || '').trim();
                const position = Number.isInteger(item.position) ? `#${item.position} · ` : '';
                const destination = list.childElementCount < 8 ? list : remaining;
                destination.append(element('li', '', `${position}${title}${site ? ` · ${site}` : ''}`));
            }
            if (list.childElementCount) scope.append(list);
            if (remaining.childElementCount) {
                const more = element('details', 'agent-confirmation-editor');
                more.append(element('summary', '', `另有 ${remaining.childElementCount} 项，展开核对`), remaining);
                scope.append(more);
            }
        }
        if (effects.length) {
            scope.append(element('h4', '', '执行内容'));
            const list = element('ul', 'agent-confirmation-list');
            for (const item of effects.slice(0, 5)) {
                const text = approvalListItem(item);
                if (text) list.append(element('li', '', text));
            }
            if (list.childElementCount) scope.append(list);
        }
        return scope.childElementCount ? scope : null;
    }

    const unconfirmedEffectMessage = '执行结果尚未确认，请先查询实际业务状态，勿直接重复提交。';

    function rememberEffectReceipt(turn, planId, payload) {
        if (!turn || !planId) return;
        if (!(turn.effectReceipts instanceof Map)) turn.effectReceipts = new Map();
        const previous = turn.effectReceipts.get(planId) || {};
        const receipt = typeof payload.receipt === 'string' && payload.receipt.trim()
            ? payload.receipt : previous.receipt || '';
        const summary = publicSummary(payload.result)
            || String(payload.message || '').trim()
            || previous.summary || '';
        const record = {
            receipt, summary,
            followupPending: payload.result?.data?.background_job?.followup_pending === true,
        };
        turn.effectReceipts.set(planId, record);
        return record;
    }

    function trustedEffectReceipt(turn) {
        return [...(turn?.effectReceipts?.values() || [])].map(({receipt, summary}) => {
            if (receipt) return receipt;
            const detail = summary ? `服务端摘要：${summary}` : unconfirmedEffectMessage;
            return `⚠️ 执行状态未知。${detail}`;
        }).join('\n\n');
    }

    function buildConfirmationEditor(approval, card, confirm) {
        const properties = approval.editing?.schema?.properties || {};
        if (!Object.keys(properties).length) return null;
        const original = approval.editing.values || {};
        const details = element('details', 'agent-confirmation-editor');
        details.append(element('summary', '', '修改参数'));
        const form = element('form', 'agent-confirmation-form');
        const feedback = element('p', 'agent-confirmation-feedback', '修改后需重新预检，新卡仍需确认；有效期不延长。');
        feedback.setAttribute('role', 'status');
        const controls = new Map();
        const labels = {enabled: '启用', notify_enabled: '发送通知', name: '名称', title: '标题', label: '标签',
            policy: '设置项', value: '设置值', target: '目标', positions: '选择序号', max_series: '检查上限', interval_hours: '间隔（小时）'};
        for (const [key, schema] of Object.entries(properties)) {
            const label = element('label', 'agent-confirmation-field');
            label.append(element('span', '', schema.title || labels[key] || key));
            const value = original[key];
            const choices = schema.enum || (schema.type === 'boolean' ? [true, false] : null);
            let input;
            if (choices) {
                input = element('select');
                if (!(key in original)) input.append(new Option('保持未设置', ''));
                choices.forEach(choice => input.append(new Option(typeof choice === 'boolean' ? (choice ? '是' : '否') : String(choice), JSON.stringify(choice))));
                input.value = key in original ? JSON.stringify(value) : '';
            } else {
                input = element(schema.type === 'array' || schema.oneOf ? 'textarea' : 'input');
                if (input.tagName === 'INPUT') input.type = ['integer', 'number'].includes(schema.type) ? 'number' : 'text';
                input.value = value === undefined ? '' : input.tagName === 'TEXTAREA' ? JSON.stringify(value) : String(value);
                if (schema.minimum !== undefined) input.min = schema.minimum;
                if (schema.maximum !== undefined) input.max = schema.maximum;
                if (schema.maxLength !== undefined) input.maxLength = schema.maxLength;
                if (schema.minLength !== undefined) input.minLength = schema.minLength;
                if (schema.type === 'integer') input.step = '1';
                if (schema.type === 'number') input.step = 'any';
                if (input.tagName === 'TEXTAREA') label.append(element('small', '', schema.type === 'array' ? '使用 JSON 列表，例如 [1,2]' : '使用 JSON 值，例如 10 或 "默认"'));
            }
            input.dataset.confirmationField = key;
            label.append(input);
            form.append(label);
            controls.set(key, {input, schema, initial: input.value});
        }
        const submit = element('button', 'agent-confirmation-explain', '重新预检');
        submit.type = 'submit';
        const discard = element('button', 'agent-confirmation-cancel', '放弃修改');
        discard.type = 'button';
        form.append(feedback, submit, discard);
        const changed = () => [...controls.values()].some(({input, initial}) => input.value !== initial);
        const reset = () => {
            if (busy || card.classList.contains('is-expired')) return;
            controls.forEach(({input, initial}) => { input.value = initial; });
            confirm.disabled = false;
            card.querySelectorAll('[data-trust-grant]').forEach(button => { button.disabled = false; });
            feedback.textContent = '修改后需重新预检，新卡仍需确认；有效期不延长。';
            details.open = false;
            details.querySelector('summary').focus({preventScroll: true});
        };
        discard.addEventListener('click', reset);
        details.addEventListener('keydown', event => {
            if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); reset(); }
        });
        form.addEventListener('input', () => {
            confirm.disabled = changed();
            card.querySelectorAll('[data-trust-grant]').forEach(button => { button.disabled = changed(); });
            feedback.textContent = changed() ? '参数已修改，请重新预检后确认。' : '修改后需重新预检，新卡仍需确认；有效期不延长。';
        });
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (busy || !changed()) return;
            const patch = {};
            try {
                for (const [key, {input, schema, initial}] of controls) {
                    if (input.value === initial) continue;
                    if (['integer', 'number'].includes(schema.type) && (!input.value.trim() || !Number.isFinite(Number(input.value)))) throw new Error('invalid number');
                    patch[key] = input.tagName === 'SELECT' || input.tagName === 'TEXTAREA'
                        ? JSON.parse(input.value) : ['integer', 'number'].includes(schema.type) ? Number(input.value) : input.value;
                }
            } catch (_) {
                feedback.textContent = '请输入有效的 JSON 值，原卡尚未变更。';
                return;
            }
            const target = sessionId, generation = sessionLoadGeneration;
            card.querySelectorAll('button, input, select, textarea').forEach(control => { control.disabled = true; });
            form.setAttribute('aria-busy', 'true');
            setBusy(true);
            feedback.style.minHeight = `${feedback.getBoundingClientRect().height}px`;
            feedback.textContent = '正在重新预检，尚未执行变更。';
            try {
                const payload = await fetchJSON('/api/agent/actions/confirm/edit', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: target, plan_id: approval.plan_id, arguments: patch, request_id: createId('edit')}),
                });
                if (sessionId !== target || sessionLoadGeneration !== generation) return;
                if (payload.plan) {
                    showApproval(approvalTurnForCard(card), payload.plan);
                    transcript.querySelector(`[data-effect-confirm="${CSS.escape(payload.plan.plan_id)}"]`)?.focus({preventScroll: true});
                } else {
                    card.classList.add('is-expired');
                    feedback.textContent = '预检完成，无需变更。旧卡已失效，未执行写入。';
                }
            } catch (error) {
                if (sessionId !== target || sessionLoadGeneration !== generation) return;
                card.classList.add('is-expired');
                feedback.textContent = error.payload?.old_plan_invalidated
                    ? `修改失败，旧卡已失效，未执行变更。${error.message}`
                    : `修改状态待核对，请刷新会话；不会自动执行。${error.message}`;
            } finally {
                form.removeAttribute('aria-busy');
                setBusy(false);
            }
        });
        details.append(form);
        return details;
    }

    function renderScopedTrust(grant) {
        if (scopedTrustExpiryTimer) clearTimeout(scopedTrustExpiryTimer);
        if (!scopedTrustBar) {
            scopedTrustBar = element('div', 'agent-scoped-trust-bar');
            scopedTrustBar.hidden = true;
            composer?.before(scopedTrustBar);
        }
        scopedTrustBar.hidden = !grant?.active;
        if (!grant?.active) return;
        const scopeLabel = `本地探索收藏 · TMDB ${grant.scope?.media_type === 'movie' ? '电影' : '剧集'}`;
        const text = element('span', '', `${scopeLabel} · 剩余 ${grant.remaining} 次 · 至 ${new Date(grant.expires_at * 1000).toLocaleTimeString()}`);
        const revoke = element('button', 'agent-action-btn', '撤销预授权');
        revoke.type = 'button';
        const target = sessionId;
        revoke.addEventListener('click', async () => {
            revoke.disabled = true;
            try {
                await fetchJSON('/api/agent/actions/trust', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: target, operation: 'revoke', request_id: createId('revoke')})});
                if (sessionId === target) {
                    renderScopedTrust(null);
                    announce(responseStatus, '本会话预授权已撤销，后续变更需逐次确认。');
                }
            } catch (error) {
                if (sessionId === target) announce(responseStatus, error.message || '撤销状态待核对，请重试。');
            } finally { revoke.disabled = false; }
        });
        scopedTrustBar.replaceChildren(text, revoke);
        scopedTrustExpiryTimer = setTimeout(() => {
            if (sessionId === target) renderScopedTrust(null);
        }, Math.max(0, grant.expires_at * 1000 - Date.now()));
    }

    function buildScopedTrust(approval, card) {
        if (!approval.trust_eligible) return null;
        const details = element('details', 'agent-confirmation-editor');
        details.append(element('summary', '', '限定低风险预授权（默认关闭）'));
        const form = element('form', 'agent-confirmation-form');
        const tool = element('label', 'agent-confirmation-trust-check');
        const toolCheck = element('input');
        toolCheck.type = 'checkbox';
        toolCheck.required = true;
        tool.append(toolCheck, element('span', '', `允许动作：${approval.confirmation?.action || approval.tool_name}`));
        const scope = element('label', 'agent-confirmation-trust-check');
        const scopeCheck = element('input');
        scopeCheck.type = 'checkbox';
        scopeCheck.required = true;
        const scopeLabel = `本地探索收藏 · TMDB ${approval.trust_scope?.media_type === 'movie' ? '电影' : '剧集'}`;
        scope.append(scopeCheck, element('span', '', `范围：仅当前会话，${scopeLabel}，允许添加不同条目`));
        const count = element('label', 'agent-confirmation-field');
        count.append(element('span', '', '最多自动执行次数'));
        const maxActions = element('select');
        maxActions.required = true;
        maxActions.append(new Option('请选择次数上限', ''), new Option('5 次', '5'), new Option('10 次', '10'), new Option('20 次', '20'));
        count.append(maxActions);
        const duration = element('label', 'agent-confirmation-field');
        duration.append(element('span', '', '有效期'));
        const select = element('select');
        select.required = true;
        select.append(new Option('请选择有效期', ''), new Option('5 分钟', '300'), new Option('15 分钟', '900'), new Option('30 分钟', '1800'));
        duration.append(select);
        const feedback = element('p', 'agent-confirmation-feedback');
        feedback.setAttribute('role', 'status');
        const showStatus = grant => {
            feedback.textContent = grant?.active
                ? `本会话预授权剩余 ${grant.remaining} 次，有效至 ${new Date(grant.expires_at * 1000).toLocaleTimeString()}。可随时撤销；取消卡片不会撤销预授权。`
                : '默认关闭。启用后仅所选来源和类型的收藏添加可自动执行；删除、其他来源或类型仍需新确认。本卡仍需点击确认执行。';
        };
        showStatus(approval.scoped_trust);
        const grant = element('button', 'agent-confirmation-explain', '启用所选预授权');
        grant.type = 'submit';
        grant.dataset.trustGrant = '';
        const revoke = element('button', 'agent-confirmation-cancel', '撤销本会话预授权');
        revoke.type = 'button';
        form.append(tool, scope, count, duration, feedback, grant, revoke);
        const change = async operation => {
            if (busy) return;
            const target = sessionId, generation = sessionLoadGeneration;
            const controls = [...card.querySelectorAll('button, input, select, textarea')];
            const disabled = controls.map(control => control.disabled);
            controls.forEach(control => { control.disabled = true; });
            form.setAttribute('aria-busy', 'true');
            setBusy(true);
            feedback.style.minHeight = `${feedback.getBoundingClientRect().height}px`;
            feedback.textContent = operation === 'grant' ? '正在保存所选预授权，尚未执行本卡。' : '正在撤销预授权。';
            try {
                const payload = await fetchJSON('/api/agent/actions/trust', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: target, request_id: createId('trust'), operation,
                        ...(operation === 'grant' ? {plan_id: approval.plan_id, tool_name: approval.tool_name, scope: approval.trust_scope,
                            max_actions: Number(maxActions.value), ttl_seconds: Number(select.value)} : {})}),
                });
                if (sessionId === target && sessionLoadGeneration === generation) {
                    showStatus(payload);
                    renderScopedTrust(payload);
                }
            } catch (error) {
                if (sessionId === target && sessionLoadGeneration === generation) feedback.textContent = error.message || '保存失败，请核对后重试。';
            } finally {
                controls.forEach((control, index) => { control.disabled = disabled[index]; });
                form.removeAttribute('aria-busy');
                setBusy(false);
            }
        };
        form.addEventListener('submit', event => { event.preventDefault(); change('grant'); });
        revoke.addEventListener('click', () => change('revoke'));
        details.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !busy) {
                event.preventDefault(); event.stopPropagation(); form.reset(); details.open = false;
                details.querySelector('summary').focus({preventScroll: true});
            }
        });
        details.append(form);
        return details;
    }

    function buildApproval(approval) {
        const card = element('section', 'agent-confirmation-card');
        if (String(approval.effect || '').toUpperCase() === 'DANGER') {
            card.classList.add('is-risk-danger');
        }
        card.dataset.planId = approval.plan_id || '';
        const head = element('div', 'agent-confirmation-head');
        const heading = element('div', 'agent-confirmation-heading');
        const title = element('div', 'agent-confirmation-title');
        const confirmation = approval.confirmation && typeof approval.confirmation === 'object' ? approval.confirmation : {};
        title.append(
            element('span', '', '安全执行计划'),
            element('strong', '', String(confirmation.action || '确认后执行变更')),
        );
        heading.append(title);
        const risk = element('span', 'agent-confirmation-risk', String(approval.effect || 'WRITE').toUpperCase() === 'DANGER' ? '高风险' : '需确认');
        if (String(approval.effect || '').toUpperCase() === 'DANGER') risk.classList.add('is-danger');
        head.append(heading, risk);

        const intro = element('div', 'agent-confirmation-intro');
        intro.append(parseTextBlocks(String(confirmation.preflight_summary || '').trim() || publicSummary(approval.preview) || publicSummary(approval.result) || '预检已完成。'));
        const facts = element('dl', 'agent-confirmation-facts');
        const previewData = approval.preview?.data;
        for (const [key, value] of scalarPreviewRows(previewData, confirmation)) {
            const row = element('div', 'agent-confirmation-fact');
            row.append(element('dt', '', key), element('dd', '', value));
            facts.append(row);
        }
        const scope = buildApprovalScope(previewData);
        const details = element('dl', 'agent-confirmation-details');
        for (const [label, value] of [['执行影响', confirmation.impact], ['如何撤销', confirmation.reversibility]]) {
            const text = String(value || '').trim();
            if (!text) continue;
            const row = element('div', 'agent-confirmation-detail');
            row.append(element('dt', '', label), element('dd', '', text));
            details.append(row);
        }
        const status = element('div', 'agent-confirmation-status');
        const preflight = element('p', 'agent-confirmation-preflight');
        preflight.append(icon('shield-check'), element('span', '', '系统只冻结了计划，尚未写入任何变更。'));
        status.append(preflight);
        if (approval.expires_at) {
            const expiry = element('p', 'agent-confirmation-copy');
            expiry.append(icon('clock-3'), element('span', 'agent-confirmation-time-copy', `有效期至 ${approval.expires_at}`));
            status.append(expiry);
        }
        const actions = element('div', 'agent-confirmation-actions');
        const cancel = element('button', 'agent-confirmation-cancel', '取消');
        cancel.type = 'button';
        cancel.dataset.effectCancel = approval.plan_id || '';
        const confirm = element('button', 'agent-confirmation-submit', '确认执行');
        confirm.type = 'button';
        confirm.dataset.effectConfirm = approval.plan_id || '';
        const explain = element('button', 'agent-confirmation-explain', '只解释');
        explain.type = 'button';
        explain.dataset.effectExplain = approval.plan_id || '';
        actions.append(cancel, explain, confirm);
        status.append(element('p', 'agent-confirmation-copy', '普通讨论会保留本卡；取消或新的变更预检才会使原卡失效，有效期不延长。'));
        card.append(head, intro);
        if (facts.childElementCount) card.append(facts);
        if (scope) card.append(scope);
        if (details.childElementCount) card.append(details);
        const editor = buildConfirmationEditor(approval, card, confirm);
        if (editor) card.append(editor);
        const trust = buildScopedTrust(approval, card);
        if (trust) card.append(trust);
        card.append(status, actions);
        renderIcons(card);
        return card;
    }

    function showApproval(turn, approval) {
        if (approval.plan_id) expireVisibleApprovals(String(approval.plan_id));
        cancelTurnMarkdownRender(turn);
        turn.pendingMarkdown = '';
        const card = buildApproval(approval);
        turn.activePlanId = String(approval.plan_id || '');
        const trace = buildToolTrace(turn);
        if (trace) {
            const status = card.querySelector('.agent-confirmation-status');
            card.insertBefore(trace, status || null);
        }
        const receipt = trustedEffectReceipt(turn);
        if (receipt) {
            const copy = element('div', 'agent-confirmation-result agent-rich-text');
            replaceRichText(copy, receipt);
            card.insertBefore(copy, card.querySelector('.agent-confirmation-status'));
        }
        // 确认卡包含真实写操作按钮，不应继承消息入场位移动画；否则在快速
        // 预检完成时按钮会短暂移动，既影响触控，也会造成自动化点击不稳定。
        turn.item?.classList.add('is-confirmation');
        const container = turn.approvalContainer || turn.card;
        container.replaceWith(card);
        turn.approvalContainer = null;
        turn.card = card;
        turn.approvalNode = card;
        card._agentTurn = turn;
        scrollToBottom(true);
    }

    function approvalTurnForCard(card) {
        if (card?._agentTurn) return card._agentTurn;
        const group = card?.closest('.agent-candidates');
        const turn = {
            item: card?.closest('.agent-message') || group || null,
            card,
            head: null,
            headText: null,
            text: null,
            steps: card?.querySelector('.agent-stream-steps') || null,
            rounds: new Map(),
            currentRound: 0,
            toolSteps: new Map(),
            effectReceipts: new Map(),
            completedPlanIds: new Set(),
            approvalNode: card,
            approvalContainer: null,
            activePlanId: card?.dataset.planId || '',
            candidateGroup: group,
            candidateSelection: card?._candidateSelection || null,
            boundSelection: Boolean(group),
            requestMessage: '',
            pendingMarkdown: '',
            markdownTimer: null,
            markdownFrame: null,
            lastMarkdownRender: 0,
        };
        if (card) card._agentTurn = turn;
        return turn;
    }

    function showExecutingApproval(card, detail = '执行完成前不会接受另一项写操作。') {
        card.querySelectorAll('.agent-confirmation-editor').forEach(details => { details.open = false; });
        card.querySelectorAll('input, select, textarea').forEach(control => { control.disabled = true; });
        const preflight = card.querySelector('.agent-confirmation-preflight span');
        if (preflight) preflight.textContent = '已确认，正在等待实际执行结果。';
        const actions = card.querySelector('.agent-confirmation-actions');
        let copy = card.querySelector('.agent-confirmation-executing-copy');
        if (!copy) {
            const executing = element('div', 'agent-confirmation-executing');
            const mark = element('span', 'agent-confirmation-executing-mark');
            mark.append(icon('loader-circle'));
            copy = element('span', 'agent-confirmation-executing-copy');
            copy.append(element('strong', '', '正在执行已确认计划'), element('small', '', detail));
            executing.append(mark, copy);
            actions?.replaceChildren(executing);
            renderIcons(card);
        } else {
            copy.querySelector('small').textContent = detail;
        }
        return copy.querySelector('small');
    }

    function promoteTurnCard(turn) {
        if (turn?.approvalNode && turn.approvalNode === turn.card) restoreTurnFromApproval(turn, turn.card);
        if (!turn?.approvalContainer || turn.approvalContainer === turn.card) return;
        turn.approvalContainer.replaceWith(turn.card);
        turn.approvalContainer = null;
    }

    function restoreTurnFromApproval(turn, card) {
        const stream = createStreamingCard();
        const trace = turn.toolTrace || card.querySelector('.agent-tool-trace');
        const steps = trace?.querySelector('.agent-stream-steps') || turn.steps;
        if (steps) {
            stream.steps.replaceWith(trace || steps);
            stream.steps = steps;
        }
        turn.toolTrace = trace;
        card.append(stream.card);
        Object.assign(turn, stream, {approvalNode: null});
        turn.approvalContainer = card;
        turn.item?.classList.remove('is-confirmation');
        renderIcons(stream.card);
    }

    function replaceApprovalWithResult(card, text, {error = false, cancelled = false} = {}) {
        const result = element('section', `agent-result-card${error ? ' is-interrupted' : ''}${cancelled ? ' agent-cancelled' : ''}`);
        const head = element('div', 'agent-stream-head');
        head.append(icon(error ? 'triangle-alert' : cancelled ? 'circle-stop' : 'circle-check-big'), element('span', '', error ? '执行失败' : cancelled ? '已取消' : '执行完成'));
        const body = element('div', 'agent-stream-text agent-rich-text');
        replaceRichText(body, text);
        result.append(head, body);
        if (card.closest('.agent-candidates') && !cancelled) {
            const link = element('a', 'agent-result-downloads', '查看下载任务');
            link.href = '/downloads';
            result.append(link);
        }
        const group = card.closest('.agent-candidates');
        card.replaceWith(result);
        renderIcons(result);
        syncCandidateButtons();
        if (!group) scrollToBottom();
    }

    function expireVisibleApprovals(keepPlanId = '') {
        transcript?.querySelectorAll('.agent-confirmation-card[data-plan-id]').forEach((card) => {
            if (card.dataset.planId === keepPlanId || card.classList.contains('is-expired')) return;
            card.classList.add('is-expired');
            card.querySelectorAll('.agent-confirmation-editor').forEach(details => { details.open = false; });
            card.querySelectorAll('button, input, select, textarea').forEach((button) => { button.disabled = true; });
            const status = card.querySelector('.agent-confirmation-preflight span');
            if (status) status.textContent = keepPlanId
                ? '已由新的计划替代，本计划不会执行。'
                : '该计划已取消或失效，不能再确认。';
        });
        syncCandidateButtons();
    }

    function rememberCandidateEffectResult(turn, result) {
        const state = turn?.candidateGroup?._candidateState;
        if (!state) return;
        const handled = Array.isArray(result?.data?.items) ? result.data.items : [];
        for (const item of handled) {
            if (item?.status !== 'failed' && Number.isInteger(item?.position)) state.selected.delete(item.position);
        }
        saveCandidateDraft(turn.candidateGroup);
    }

    function isTerminalEvent(event) {
        return ['turn.completed', 'turn.failed', 'turn.cancelled'].includes(event?.type);
    }

    function turnNotice(status) {
        return {waiting: '任务已交接，正在等待后续结果', approval_required: '等待下一项确认',
            failed: '请求失败', cancelled: '请求已停止', interrupted: '请求未能完成，状态已同步'}[status] || '本轮回复已结束';
    }

    function applyEvent(turn, event) {
        const payload = event?.payload && typeof event.payload === 'object' ? event.payload : {};
        consumeTerminalTask(event);
        switch (event?.type) {
        case 'turn.started':
            turn.confirmationRequested = payload.kind === 'confirmation';
            turn.streamDisplayEnabled = payload.stream_display_enabled !== false;
            setTurnStatus(turn, payload.kind === 'confirmation' ? '正在执行已确认计划' : '正在理解任务');
            break;
        case 'capabilities.selected':
            setTurnStatus(turn, '已准备相关能力，正在规划');
            break;
        case 'model.started':
            turn.currentRound = Number(payload.round || turn.currentRound + 1);
            turn.rounds.set(turn.currentRound, '');
            setTurnStatus(turn, turn.currentRound > 1 ? '正在汇总结果' : '正在规划下一步');
            break;
        case 'model.delta': {
            if (turn.streamDisplayEnabled === false) break;
            const round = Number(payload.round || turn.currentRound || 1);
            const value = `${turn.rounds.get(round) || ''}${String(payload.delta || '')}`;
            turn.rounds.set(round, value);
            renderTurnMarkdown(turn, value);
            break;
        }
        case 'model.tool_call': {
            const key = `call:${payload.call_id || event.sequence}`;
            updateStep(turn, key, `${toolLabel(payload.tool, payload.label)}…`, {pending: true});
            setTurnStatus(turn, toolLabel(payload.tool, payload.label));
            break;
        }
        case 'tool.started':
            setTurnStatus(turn, toolLabel(payload.tool, payload.label));
            break;
        case 'tool.progress': {
            if (Object.prototype.hasOwnProperty.call(payload, 'candidate_view') && payload.candidate_view === null) removeCandidateView(turn);
            const summary = payload.phase === 'background_job' && typeof payload.summary === 'string'
                ? payload.summary.trim() : publicSummary(payload);
            if (summary) setTurnStatus(turn, summary.slice(0, 100));
            break;
        }
        case 'tool.completed':
            if (payload.result?.candidate_view) renderCandidateView(turn, payload.result.candidate_view);
            else if (Object.prototype.hasOwnProperty.call(payload.result || {}, 'candidate_view')) removeCandidateView(turn);
            updateStep(turn, `call:${payload.call_id || event.sequence}`, `${toolLabel(payload.tool, payload.label)}完成`);
            break;
        case 'tool.failed':
            updateStep(turn, `call:${payload.call_id || event.sequence}`, `${toolLabel(payload.tool, payload.label)}未完成，正在调整`, {warning: true});
            setTurnStatus(turn, '正在调整方案');
            break;
        case 'effect.preview_started':
            setTurnStatus(turn, '正在生成安全变更预览');
            break;
        case 'effect.approval_required':
            if (payload.plan) {
                updateStep(
                    turn,
                    `call:${payload.call_id || event.sequence}`,
                    `${toolLabel(payload.tool, payload.label)}预检完成`,
                );
                showApproval(turn, {
                    plan_id: payload.plan.plan_id,
                    tool_name: payload.plan.tool_name || payload.tool,
                    effect: payload.plan.effect,
                    preview: payload.plan.preview || {},
                    result: payload.result || {},
                    confirmation: payload.plan.confirmation || {},
                    expires_at: payload.plan.expires_at || '',
                    editing: payload.plan.editing,
                    trust_eligible: payload.plan.trust_eligible,
                    trust_scope: payload.plan.trust_scope,
                });
            }
            break;
        case 'effect.completed': {
            if (payload.scoped_trust) renderScopedTrust(payload.scoped_trust);
            const planId = String(payload.plan_id || turn.activePlanId || '');
            const record = rememberEffectReceipt(turn, planId, payload);
            if (planId) {
                turn.completedPlanIds?.add(planId);
                updateStep(
                    turn,
                    `effect:${planId}`,
                    record?.receipt ? '已记录服务端执行回执' : '执行状态未知，待核对服务端摘要',
                    {warning: !record?.receipt, iconName: 'file-text'},
                );
            }
            rememberCandidateEffectResult(turn, payload.result || {});
            break;
        }
        case 'effect.failed': {
            const planId = String(payload.plan_id || turn.activePlanId || '');
            const record = rememberEffectReceipt(turn, planId, payload);
            if (planId) {
                updateStep(
                    turn,
                    `effect:${planId}`,
                    record?.receipt ? '已记录失败回执' : '执行状态未知，待核对服务端摘要',
                    {warning: true, iconName: 'file-text'},
                );
            }
            turn.effectError = String(payload.message || '操作未能确认，请以服务端回执核对。').trim();
            break;
        }
        case 'turn.completed': {
            const status = String(payload.status || '').toLowerCase();
            turn.waiting = status === 'waiting';
            if (status === 'approval_required') break;
            const answer = typeof payload.answer === 'string' ? payload.answer : '';
            if (status === 'effect_completed' && !answer.trim()) finalizeError(turn, `${unconfirmedEffectMessage}服务端未提供最终答复。`);
            else finalizeAnswer(turn, answer);
            break;
        }
        case 'turn.failed':
            finalizeError(turn, turn.effectError || payload.message || 'Agent 暂时无法完成该请求。');
            break;
        case 'turn.cancelled':
            finalizeError(turn, '本轮处理已停止。', {cancelled: true});
            break;
        default:
            break;
        }
    }

    async function readEventStream(response, consume) {
        if (!response.ok) {
            const text = await response.text();
            let message = `请求失败（HTTP ${response.status}）`;
            try { message = JSON.parse(text).error || message; } catch (_) { /* non-json */ }
            const error = new Error(message);
            error.httpStatus = response.status;
            throw error;
        }
        if (!response.body?.getReader) throw new Error('当前浏览器不支持流式响应');
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', {fatal: true});
        let buffer = '';
        try {
            while (true) {
                const {value, done} = await reader.read();
                if (value) buffer += decoder.decode(value, {stream: !done});
                let lineEnd = buffer.indexOf('\n');
                while (lineEnd >= 0) {
                    const line = buffer.slice(0, lineEnd).trim();
                    buffer = buffer.slice(lineEnd + 1);
                    if (line) {
                        const event = JSON.parse(line);
                        consume(event);
                        if (isTerminalEvent(event)) {
                            // 业务终态已经持久化，不等待HTTP EOF，更不能让迟到Abort覆盖结果。
                            reader.cancel().catch(() => {});
                            return event;
                        }
                    }
                    lineEnd = buffer.indexOf('\n');
                }
                if (done) break;
            }
            buffer += decoder.decode();
            if (buffer.trim()) {
                const event = JSON.parse(buffer.trim());
                consume(event);
                if (isTerminalEvent(event)) return event;
            }
            return null;
        } finally {
            try { reader.releaseLock(); } catch (_) { /* already released */ }
        }
    }

    async function fetchJSON(url, options = {}) {
        const response = await fetch(url, options);
        const text = await response.text();
        let payload = {};
        if (text) {
            try { payload = JSON.parse(text); } catch (_) { payload = {}; }
        }
        if (!response.ok) {
            const error = new Error(payload.error || (typeof payload.detail === 'string' ? payload.detail : '') || `请求失败（HTTP ${response.status}）`);
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function isCurrentActiveRequest(active) {
        return Boolean(active && activeRequest === active
            && active.sessionId === sessionId
            && active.sessionGeneration === sessionLoadGeneration);
    }

    function clearActiveObservation(active) {
        if (active?.pollTimer) clearTimeout(active.pollTimer);
        if (active) active.pollTimer = null;
        active?.observerController?.abort();
        if (active) active.observerController = null;
    }

    function invalidateActiveRequest() {
        const active = activeRequest;
        if (!active) return;
        activeRequest = null;
        clearActiveObservation(active);
        active.controller?.abort();
    }

    function clearReceiptObserverForNewRequest() {
        if (activeRequest?.receiptOnly) invalidateActiveRequest();
    }

    function sameActiveTurn(turn, active) {
        if (!turn || String(turn.request_id || '') !== active.requestId) return false;
        if (active.turnId && turn.turn_id && String(turn.turn_id) !== active.turnId) return false;
        if (active.kernelGeneration != null && turn.generation != null
            && String(turn.generation) !== String(active.kernelGeneration)) return false;
        return true;
    }

    async function reconcilePendingApprovals(active) {
        const rejectedPlanId = active?.kind === 'confirm' ? active.turn?.activePlanId : '';
        if (!isCurrentActiveRequest(active)
            || (!rejectedPlanId && !transcript?.querySelector('.agent-confirmation-card[data-plan-id]:not(.is-expired)'))) return;
        const controller = new AbortController();
        const abort = () => controller.abort();
        const timeout = window.setTimeout(abort, 3000);
        active.controller.signal.addEventListener('abort', abort, {once: true});
        try {
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(active.sessionId)}`, {
                signal: controller.signal, cache: 'no-store',
                headers: {'X-Agent-Request-Id': active.requestId},
            });
            if (!isCurrentActiveRequest(active) || payload?.session_id !== active.sessionId) return;
            // protected 会暂时隐藏已接管的票据；其它活动轮次也不能作为本轮的撤卡依据。
            if (payload.active_turn && (payload.active_turn.protected || !sameActiveTurn(payload.active_turn, active))) return;
            if (rejectedPlanId) {
                // HTTP 拒绝发生在领取票据之前，未必有本请求的 last_turn；只恢复只读核实的同一待确认计划。
                if (payload.active_turn || payload.pending_approval?.plan_id !== rejectedPlanId) return;
                const card = buildApproval({...payload.pending_approval, scoped_trust: payload.scoped_trust});
                card._candidateSelection = active.turn.candidateSelection;
                active.turn.card.after(card);
                approvalTurnForCard(card);
                expireVisibleApprovals(rejectedPlanId);
                return true;
            }
            // 缺少本轮终态的旧/缺省快照不能证明票据已撤销。
            if (!sameActiveTurn(payload.last_turn, active) || payload.last_turn.status === 'interrupted') return;
            if (payload.pending_approval === null) expireVisibleApprovals();
            else if (payload.pending_approval?.plan_id) expireVisibleApprovals(String(payload.pending_approval.plan_id));
        } catch (_) {
            // 无法核实就保持现状，不把网络故障伪装成取消，也不重放写操作。
        } finally {
            window.clearTimeout(timeout);
            active.controller.signal.removeEventListener('abort', abort);
        }
    }

    function activeProgressText(turn, active) {
        const detail = String(turn?.detail || '').trim();
        if (active.cancelAccepted || turn?.status === 'cancelling') {
            return detail ? `正在停止 · ${detail}` : '正在停止';
        }
        return detail || '后台仍在处理';
    }

    function prepareActiveStatus(active, detail = '') {
        if (active.kind === 'pending_approval' && active.pendingCard?.isConnected) {
            active.pendingCard.querySelectorAll('button').forEach(button => { button.disabled = true; });
            active.statusNode = active.pendingCard.querySelector('.agent-confirmation-preflight span');
            detail = `尚未执行 · ${detail || '正在完成预览'}`;
        } else if (active.kind === 'candidate_preview' && active.candidateGroup?.isConnected) {
            let status = active.candidateGroup.querySelector('[data-agent-active-status]');
            if (!status) {
                status = element('p', 'agent-candidates-note agent-candidate-feedback');
                status.dataset.agentActiveStatus = 'true';
                active.candidateGroup._candidateState?.output.append(status);
            }
            active.statusNode = status;
        } else if (active.kind === 'confirm' && active.pendingCard?.isConnected) {
            active.statusNode = showExecutingApproval(active.pendingCard, detail || '正在等待实际执行结果。');
            active.statusNode.dataset.agentActiveStatus = 'true';
        } else if (active.turn?.headText?.isConnected) {
            active.statusNode = active.turn.headText;
            active.statusNode.dataset.agentActiveStatus = 'true';
        } else if (active.candidateGroup?.isConnected) {
            let status = active.candidateGroup.querySelector('[data-agent-active-status]');
            if (!status) {
                status = element('p', 'agent-candidates-note agent-candidate-feedback');
                status.dataset.agentActiveStatus = 'true';
                active.candidateGroup._candidateState?.output.append(status);
            }
            active.statusNode = status;
        }
        if (active.statusNode && detail) active.statusNode.textContent = detail;
        if (active.candidateGroup?.isConnected) {
            active.candidateGroup.dataset.previewing = 'true';
            active.candidateGroup.querySelectorAll('[data-effect-confirm]').forEach(button => { button.disabled = true; });
        }
    }

    function updateActiveProgress(active, turn) {
        if (!isCurrentActiveRequest(active)) return;
        active.turnId = active.turnId || String(turn.turn_id || '');
        active.kernelGeneration = active.kernelGeneration ?? turn.generation;
        active.protected = turn.protected === true;
        const detail = activeProgressText(turn, active);
        prepareActiveStatus(active, detail);
        setBusy(true);
    }

    function scheduleActiveObservation(active, delay = SESSION_POLL_INTERVAL_MS) {
        if (!isCurrentActiveRequest(active) || active.pollTimer) return;
        active.pollTimer = window.setTimeout(() => {
            active.pollTimer = null;
            observeActiveSession(active);
        }, delay);
    }

    function receiptSnapshotKey(payload) {
        return JSON.stringify([payload.messages, payload.pending_approval, payload.candidate_view, payload.scoped_trust]);
    }

    function finishReceiptObservation(active, {completed = false} = {}) {
        if (!isCurrentActiveRequest(active)) return;
        clearActiveObservation(active);
        activeRequest = null;
        active.observing = false;
        active.controller?.abort();
        if (completed) receiptObservationWindows.delete(active.sessionId);
    }

    function scheduleReceiptObservation(active) {
        if (!isCurrentActiveRequest(active)) return;
        const receiptWindow = receiptObservationWindows.get(active.sessionId);
        const elapsed = receiptWindow ? Date.now() - receiptWindow.startedAt : RECEIPT_OBSERVATION_MAX_MS;
        if (!receiptWindow || receiptWindow.polls >= RECEIPT_POLL_LIMIT || elapsed >= RECEIPT_OBSERVATION_MAX_MS) {
            finishReceiptObservation(active);
            return;
        }
        const delay = Math.min(active.receiptPollDelay, RECEIPT_OBSERVATION_MAX_MS - elapsed);
        active.receiptPollDelay = Math.min(RECEIPT_POLL_MAX_INTERVAL_MS, delay * 2);
        scheduleActiveObservation(active, delay);
    }

    function startReceiptObservation(active, payload = null) {
        if (!isCurrentActiveRequest(active)) return;
        // 仅已明确交接的任务继续观察；普通完成/失败/确认卡不能被一次
        // 额外的会话恢复覆盖。正在跟踪的旧任务在新回合结束后继续核验。
        const handedOff = active.turn?.waiting || [...(active.turn?.effectReceipts?.values() || [])]
            .some(record => record.followupPending);
        if (!payload && !handedOff && !receiptObservationWindows.has(active.sessionId)) return;
        const newForegroundTurn = !active.receiptOnly
            && (active.kind === 'query' || active.kind === 'confirm');
        active.receiptOnly = true;
        active.observing = true;
        if (newForegroundTurn || !receiptObservationWindows.has(active.sessionId)) {
            receiptObservationWindows.set(active.sessionId, {startedAt: Date.now(), polls: 0});
        }
        active.receiptPollDelay = RECEIPT_POLL_INTERVAL_MS;
        if (payload) active.receiptSnapshotKey = receiptSnapshotKey(payload);
        if (active.candidateGroup?.isConnected) active.candidateGroup.dataset.previewing = 'false';
        setBusy(false);
        resizePrompt();
        if (document.hidden) {
            receiptResumeNeeded = true;
            invalidateActiveRequest();
            return;
        }
        receiptResumeNeeded = false;
        if (payload || active.turn?.waiting) scheduleReceiptObservation(active);
        else observeActiveSession(active);
    }

    function pauseReceiptObservation() {
        if (!activeRequest?.receiptOnly) return;
        receiptResumeNeeded = true;
        invalidateActiveRequest();
    }

    function resumeReceiptObservation() {
        if (document.hidden || !receiptResumeNeeded) return;
        if (receiptResumeInFlight) {
            receiptResumeRequested = true;
            return;
        }
        receiptResumeNeeded = false;
        receiptResumeInFlight = true;
        const targetId = sessionId;
        loadSession(targetId, {closeHistory: false, preserveScroll: true})
            .then((loaded) => {
                if (!loaded && sessionId === targetId) receiptResumeNeeded = true;
            })
            .finally(() => {
                receiptResumeInFlight = false;
                if (receiptResumeRequested) {
                    receiptResumeRequested = false;
                    resumeReceiptObservation();
                }
            });
    }

    function finishObservedTurn(active, payload, lastTurn) {
        if (!isCurrentActiveRequest(active) || !sameActiveTurn(lastTurn, active)) return;
        clearActiveObservation(active);
        active.controller?.abort();
        renderSessionSnapshot(payload, {active, terminalTurn: lastTurn, preserveScroll: true});
        setBusy(false);
        resizePrompt();
        announce(responseStatus, turnNotice(lastTurn.status));
        refreshSessions({quiet: true});
        if (Number(payload.pending_effect_count) > 0) {
            startReceiptObservation(active, payload);
        } else {
            activeRequest = null;
        }
    }

    function stopActiveUnconfirmed(active, message) {
        if (!isCurrentActiveRequest(active)) return;
        clearActiveObservation(active);
        activeRequest = null;
        active.controller?.abort();
        if (active.turn?.effectReceipts?.size) {
            finalizeError(active.turn, message);
        } else if (active.turn?.card) {
            active.turn.card.classList.remove('agent-streaming');
            active.turn.card.classList.add('is-interrupted');
        }
        if (active.statusNode?.isConnected) active.statusNode.textContent = message;
        if (active.candidateGroup?.isConnected) active.candidateGroup.dataset.previewing = 'false';
        setBusy(false);
        resizePrompt();
        announce(responseStatus, message);
    }

    async function observeActiveSession(active) {
        if (!isCurrentActiveRequest(active)) return;
        if (active.receiptOnly && document.hidden) {
            pauseReceiptObservation();
            return;
        }
        if (active.receiptOnly && active.observerController) return;
        if (active.receiptOnly) {
            const receiptWindow = receiptObservationWindows.get(active.sessionId);
            if (!receiptWindow || receiptWindow.polls >= RECEIPT_POLL_LIMIT
                || Date.now() - receiptWindow.startedAt >= RECEIPT_OBSERVATION_MAX_MS) {
                finishReceiptObservation(active);
                return;
            }
            receiptWindow.polls += 1;
        }
        const controller = new AbortController();
        active.observerController = controller;
        try {
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(active.sessionId)}`, {signal: controller.signal, cache: 'no-store', headers: {'X-Agent-Request-Id': active.requestId}});
            if (!isCurrentActiveRequest(active)) return;
            if (configureDraftScope(payload.draft_scope) || !isCurrentActiveRequest(active)) return;
            if (payload?.session_id !== active.sessionId || !Array.isArray(payload?.messages)) throw new Error('会话状态响应无效');
            renderTaskSnapshot(payload.task);
            if (active.receiptOnly) {
                if (payload.active_turn) {
                    finishReceiptObservation(active);
                    return;
                }
                const snapshotKey = receiptSnapshotKey(payload);
                const pendingCount = Math.max(0, Number(payload.pending_effect_count) || 0);
                const changed = active.receiptSnapshotKey !== snapshotKey;
                if (changed || pendingCount === 0) {
                    const lastTurn = payload.last_turn;
                    const terminalTurn = lastTurn
                        && lastTurn.status !== 'completed'
                        ? lastTurn : null;
                    // 首次观察同一交接只建立快照基线，保留正在显示的消息与卡片。
                    if (active.receiptSnapshotKey != null || !sameActiveTurn(lastTurn, active) || lastTurn.status !== 'waiting') {
                        renderSessionSnapshot(payload, {active, terminalTurn, preserveScroll: true});
                    }
                    active.receiptSnapshotKey = snapshotKey;
                }
                if (pendingCount > 0) scheduleReceiptObservation(active);
                else {
                    finishReceiptObservation(active, {completed: true});
                    refreshSessions({quiet: true});
                }
                return;
            }
            active.pollDelay = SESSION_POLL_INTERVAL_MS;
            const lastTurn = payload.last_turn;
            if (sameActiveTurn(lastTurn, active)) {
                finishObservedTurn(active, payload, lastTurn);
                return;
            }
            if (payload.active_turn) {
                if (!sameActiveTurn(payload.active_turn, active)) {
                    stopActiveUnconfirmed(active, '会话已有新轮次，原请求状态未确认；请刷新核对。');
                    return;
                }
                active.missingSnapshots = 0;
                if (payload.active_turn.status === 'cancelling') active.cancellationObserved = true;
                updateActiveProgress(active, payload.active_turn);
            } else {
                active.missingSnapshots = (active.missingSnapshots || 0) + 1;
                if (active.missingSnapshots >= 3) {
                    stopActiveUnconfirmed(active, '任务状态未确认：会话中没有匹配的活动轮次或终态；不会自动重试。');
                    return;
                }
            }
            scheduleActiveObservation(active);
        } catch (error) {
            if (!isCurrentActiveRequest(active) || error?.name === 'AbortError') return;
            if (active.receiptOnly) {
                scheduleReceiptObservation(active);
                return;
            }
            active.pollDelay = Math.min(SESSION_POLL_MAX_INTERVAL_MS, Math.max(SESSION_POLL_INTERVAL_MS, (active.pollDelay || SESSION_POLL_INTERVAL_MS) * 2));
            announce(responseStatus, active.cancelAccepted
                ? '停止请求已受理，正在核对最终状态'
                : '连接中断，正在只读核对任务状态');
            scheduleActiveObservation(active, active.pollDelay);
        } finally {
            if (active.observerController === controller) active.observerController = null;
        }
    }

    function startActiveObservation(active) {
        if (!isCurrentActiveRequest(active)) return;
        active.observing = true;
        prepareActiveStatus(active, active.statusNode?.textContent || '正在只读核对任务状态');
        announce(responseStatus, active.cancelNotice || (active.cancelAccepted
            ? '停止请求已受理，正在核对最终状态' : '连接中断，正在只读核对任务状态'));
        observeActiveSession(active);
    }

    function streamFailureMessage(turn, error) {
        if (turn?.effectError) return turn.effectError;
        if (Number.isInteger(error?.httpStatus)) {
            const reason = String(error.message || `请求失败（HTTP ${error.httpStatus}）`).trim();
            return `${reason}\n\n⚠️ 请核对状态，不要重复提交。`;
        }
        return STREAM_INTERRUPTED_NOTICE;
    }

    function setBusy(value) {
        busy = Boolean(value);
        if (promptInput) promptInput.disabled = false;
        sendButton?.setAttribute('aria-busy', String(busy));
        syncSend();
        syncCandidateButtons();
        resumeButton && (resumeButton.disabled = busy || !latestSessionId);
    }

    function syncSend() {
        const showStop = busy || sessionStopPending;
        if (sendButton) {
            sendButton.hidden = showStop;
            sendButton.disabled = showStop || initialRestore || !promptInput?.value.trim();
        }
        if (sessionStopButton) {
            sessionStopButton.hidden = !showStop;
            sessionStopButton.disabled = initialRestore || sessionStopPending;
        }
        nextActions?.querySelectorAll('[data-agent-action-key]').forEach(button => { button.disabled = busy || initialRestore; });
    }

    function resizePrompt() {
        if (!promptInput) return;
        promptInput.style.height = 'auto';
        promptInput.style.height = `${Math.min(160, Math.max(44, promptInput.scrollHeight))}px`;
        syncSend();
        syncViewportHeight();
    }

    function queryRequest(message, requestId, signal, selection = null, targetSessionId = sessionId, actionKey = '') {
        return fetch('/api/agent/query', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session_id: targetSessionId, request_id: requestId, stream: true,
                ...(actionKey ? {action_key: actionKey} : {message, ...(selection ? {selection} : {})})}),
            signal,
        });
    }

    async function sendQuery(text, actionKey = '') {
        if (busy || sessionStopPending || initialRestore || !text.trim()) return;
        clearReceiptObserverForNewRequest();
        const message = text.trim();
        ++sessionLoadGeneration;
        expireCandidateCards();
        appendUser(message);
        const turn = createAssistantTurn();
        turn.requestMessage = message;
        turn.boundSelection = Boolean(actionKey);
        if (!actionKey) {
            promptInput.value = '';
            saveDraft();
        }
        scrollToBottom(true);
        resizePrompt();
        rememberSession(sessionId);
        const targetSessionId = sessionId;
        const controller = new AbortController();
        const requestId = createId('rq');
        const active = {
            controller, requestId, turn, sessionId: targetSessionId,
            sessionGeneration: sessionLoadGeneration, kind: 'query', turnId: '',
            pollTimer: null, observerController: null, observing: false,
            protected: false, cancelAccepted: false,
        };
        activeRequest = active;
        setBusy(true);
        announce(responseStatus, 'Media Agent 正在处理请求');
        try {
            const response = await queryRequest(message, requestId, controller.signal, null, targetSessionId, actionKey);
            if (!isCurrentActiveRequest(active)) return;
            const terminalEvent = await readEventStream(response, (event) => {
                if (!isCurrentActiveRequest(active)) return;
                if (event.request_id && String(event.request_id) !== active.requestId) return;
                active.turnId = active.turnId || String(event.turn_id || '');
                if (active.cancelAccepted && isTerminalEvent(event)) return;
                applyEvent(turn, event);
            });
            if (!isCurrentActiveRequest(active)) return;
            if (!terminalEvent || active.cancelAccepted) {
                startActiveObservation(active);
                return;
            }
            await reconcilePendingApprovals(active);
            if (!isCurrentActiveRequest(active)) return;
            announce(responseStatus, turnNotice(turn.failed ? (turn.cancelled ? 'cancelled' : 'failed') : terminalEvent.payload?.status));
            startReceiptObservation(active);
            refreshSessions({quiet: true});
        } catch (error) {
            if (!isCurrentActiveRequest(active)) return;
            if (Number.isInteger(error?.httpStatus) && error.httpStatus >= 400 && error.httpStatus < 500) {
                finalizeError(turn, streamFailureMessage(turn, error));
                announce(responseStatus, '请求未执行，请核对状态');
                if (actionKey && error.httpStatus === 409) refreshNextActions();
            } else startActiveObservation(active);
        } finally {
            if (activeRequest === active && !active.observing) {
                activeRequest = null;
                clearActiveObservation(active);
                setBusy(false);
                resizePrompt();
                refreshSessions({quiet: true});
            }
        }
    }

    async function stopSessionTasks() {
        if (initialRestore || sessionStopPending) return;
        const targetSessionId = sessionId;
        const generation = sessionLoadGeneration;
        sessionStopPending = true;
        sessionStopButton?.setAttribute('aria-busy', 'true');
        syncSend();
        try {
            const payload = await fetchJSON('/api/agent/session/stop', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: targetSessionId}),
            });
            if (sessionId !== targetSessionId || generation !== sessionLoadGeneration) return;
            const message = SESSION_STOP_MESSAGES[payload.status] || SESSION_STOP_MESSAGES.stop_unconfirmed;
            announce(responseStatus, message);
            window.showToast?.(message, payload.stopped === true ? 'success' : 'warning', 6500);
            const active = activeRequest;
            if (isCurrentActiveRequest(active)) active.cancelNotice = message;
            if (isCurrentActiveRequest(active) && !active.receiptOnly
                && ['stop_requested', 'already_cancelling'].includes(payload.model_turn)) {
                active.cancelAccepted = true;
                setBusy(true);
            }
            await loadSession(targetSessionId, {closeHistory: false, preserveScroll: true});
            if (isCurrentActiveRequest(active) && !active.receiptOnly && !active.observing) startActiveObservation(active);
        } catch (_) {
            if (sessionId !== targetSessionId || generation !== sessionLoadGeneration) return;
            announce(responseStatus, SESSION_STOP_MESSAGES.stop_unconfirmed);
            window.showToast?.(SESSION_STOP_MESSAGES.stop_unconfirmed, 'warning', 6500);
        } finally {
            sessionStopPending = false;
            sessionStopButton?.setAttribute('aria-busy', 'false');
            syncSend();
        }
    }

    async function explainEffect(button) {
        if (busy) return;
        const card = button.closest('.agent-confirmation-card');
        const planId = button.dataset.effectExplain || '';
        if (!card || !planId) return;
        if (card.querySelector('[data-explanation-input]')) {
            card.querySelector('[data-explanation-input]').focus();
            return;
        }
        const form = element('div', 'agent-explanation-form');
        const input = element('textarea', 'agent-explanation-input');
        input.dataset.explanationInput = '';
        input.maxLength = 12000;
        input.placeholder = '只解释本计划，不执行或修改。请输入问题';
        input.setAttribute('aria-label', '解释问题');
        const send = element('button', '', '发送解释问题');
        const stop = element('button', '', '停止这次解释（不取消计划）');
        send.type = stop.type = 'button';
        stop.hidden = true;
        form.append(input, send, stop);
        card.append(form);
        input.focus();
        send.addEventListener('click', async () => {
            const message = input.value.trim();
            if (!message || busy) return;
            const target = sessionId;
            const generation = sessionLoadGeneration;
            const requestId = createId('explain');
            card.querySelectorAll('button').forEach(item => { item.disabled = true; });
            stop.hidden = false;
            stop.disabled = false;
            setBusy(true);
            stop.onclick = async () => {
                stop.disabled = true;
                await fetchJSON('/api/agent/query/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: target, request_id: requestId})});
            };
            try {
                await fetchJSON('/api/agent/actions/explain', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: target, plan_id: planId, message, request_id: requestId, stream: false})});
            } catch (error) {
                announce(responseStatus, error.message || '解释连接中断；正在恢复原计划');
            } finally {
                if (sessionId === target && sessionLoadGeneration === generation) {
                    setBusy(false);
                    await loadSession(target, {closeHistory: false, preserveScroll: true});
                }
            }
        });
    }

    async function confirmEffect(button) {
        if (busy) return;
        const card = button.closest('.agent-confirmation-card');
        const planId = button.dataset.effectConfirm || '';
        if (!card || !planId) return;
        const turn = approvalTurnForCard(card);
        if (!turn || turn.completedPlanIds?.has(String(planId))) return;
        clearReceiptObserverForNewRequest();
        card.querySelectorAll('button').forEach(item => { item.disabled = true; });
        turn.activePlanId = planId;
        turn.confirmationRequested = true;
        restoreTurnFromApproval(turn, card);
        setTurnStatus(turn, '正在执行已确认计划');
        showExecutingApproval(card);
        const targetSessionId = sessionId;
        const controller = new AbortController();
        const requestId = createId('confirm');
        const active = {
            controller, requestId, turn, sessionId: targetSessionId,
            sessionGeneration: sessionLoadGeneration, kind: 'confirm', pendingCard: card,
            turnId: '', pollTimer: null, observerController: null, observing: false,
            protected: false, cancelAccepted: false,
        };
        activeRequest = active;
        setBusy(true);
        announce(responseStatus, 'Media Agent 正在执行已确认计划');
        try {
            const response = await fetch('/api/agent/actions/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({plan_id: planId, session_id: targetSessionId, request_id: requestId, stream: true}),
                signal: controller.signal,
            });
            if (!isCurrentActiveRequest(active)) return;
            const terminalEvent = await readEventStream(response, event => {
                if (!isCurrentActiveRequest(active)) return;
                if (event.request_id && String(event.request_id) !== active.requestId) return;
                active.turnId = active.turnId || String(event.turn_id || '');
                if (active.cancelAccepted && isTerminalEvent(event)) return;
                applyEvent(turn, event);
            });
            if (!isCurrentActiveRequest(active)) return;
            if (!terminalEvent || active.cancelAccepted) {
                startActiveObservation(active);
                return;
            }
            announce(responseStatus, turnNotice(turn.failed ? (turn.cancelled ? 'cancelled' : 'failed') : terminalEvent.payload?.status));
            startReceiptObservation(active);
            refreshSessions({quiet: true});
        } catch (error) {
            if (!isCurrentActiveRequest(active)) return;
            if (Number.isInteger(error?.httpStatus) && error.httpStatus >= 400 && error.httpStatus < 500) {
                finalizeError(turn, streamFailureMessage(turn, error));
                announce(responseStatus, error.message || '执行未完成，请核对状态');
                if (await reconcilePendingApprovals(active) && isCurrentActiveRequest(active)) {
                    finalizeError(turn, `${error.message}\n\n已核实本计划尚未执行，仍待确认。请稍后手动重试或取消，不会自动提交。`);
                }
            } else startActiveObservation(active);
        } finally {
            if (activeRequest === active && !active.observing) {
                activeRequest = null;
                clearActiveObservation(active);
                setBusy(false);
                refreshSessions({quiet: true});
            }
        }
    }

    async function cancelEffect(button) {
        if (busy) return;
        const card = button.closest('.agent-confirmation-card');
        const planId = button.dataset.effectCancel || '';
        if (!card || !planId) return;
        clearReceiptObserverForNewRequest();
        card.querySelectorAll('button').forEach((item) => { item.disabled = true; });
        try {
            const payload = await fetchJSON('/api/agent/actions/confirm/discard', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({plan_id: planId, session_id: sessionId, request_id: createId('cancel')}),
            });
            replaceApprovalWithResult(
                card,
                payload.discarded ? '本次计划已取消，没有执行任何写操作。' : '该确认已过期或已处理。',
                {cancelled: true},
            );
        } catch (error) {
            if (card.closest('.agent-candidates')) {
                card.querySelector('.agent-confirmation-status')?.append(element('p', 'agent-candidates-note', '取消状态尚未确认，请刷新会话核对；不会自动重试或提交下载。'));
            } else replaceApprovalWithResult(card, error?.message || '暂时无法取消该计划。', {error: true});
        } finally {
            refreshSessions({quiet: true});
        }
    }

    function sessionTime(value) {
        const numeric = Number(value);
        const date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        return new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).format(date);
    }

    function renderSessionList(items) {
        sessionItems = (Array.isArray(items) ? items : []).filter((item) => SESSION_RE.test(String(item?.session_id || ''))).slice(0, 100)
            .map((item) => ({...item, pinned: item.pinned === true}));
        if (!sessionList) return;
        const newest = [...sessionItems].sort((a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0));
        latestSessionId = newest[0]?.session_id || '';
        if (resumeButton) resumeButton.disabled = busy || !latestSessionId;
        const query = String(sessionSearch?.value || '').trim().normalize('NFKC').toLocaleLowerCase();
        const sorted = [...sessionItems].sort((a, b) => Number(b.pinned) - Number(a.pinned) ||
            (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0));
        const existing = new Map([...sessionList.querySelectorAll('.agent-session-item')].map(row => [row.dataset.sessionId, row]));
        const ids = new Set(sorted.map(item => item.session_id));
        const scrollTop = sessionList.scrollTop;
        const focused = sessionList.contains(document.activeElement) ? document.activeElement : null;
        let shown = 0;
        for (const [id, row] of existing) if (!ids.has(id)) row.remove();
        for (const item of sorted) {
            let row = existing.get(item.session_id);
            if (!row) {
                row = element('div', 'agent-session-item');
                row.dataset.sessionId = item.session_id;
                const open = element('button', 'agent-session-open');
                open.type = 'button';
                open.dataset.sessionOpen = item.session_id;
                open.append(element('strong', ''), element('small', ''));
                const controls = element('div', 'agent-session-controls');
                for (const [name, mark, label] of [['pin', 'pin', '置顶'], ['rename', 'pencil', '重命名'], ['delete', 'trash-2', '删除']]) {
                    const button = element('button', `agent-session-${name}`);
                    button.type = 'button';
                    button.dataset[`session${name[0].toUpperCase()}${name.slice(1)}`] = item.session_id;
                    button.title = label;
                    button.append(icon(mark));
                    controls.append(button);
                }
                row.append(open, controls);
                renderIcons(row);
            }
            const title = String(item.title || '新对话');
            const open = row.querySelector('.agent-session-open');
            open.querySelector('strong').textContent = title;
            open.querySelector('small').textContent = `${item.pinned ? '置顶 · ' : ''}${item.message_count || 0} 条消息${sessionTime(item.updated_at) ? ` · ${sessionTime(item.updated_at)}` : ''}`;
            open.title = title;
            row.classList.toggle('is-active', item.session_id === sessionId);
            const pin = row.querySelector('[data-session-pin]');
            pin.setAttribute('aria-pressed', String(item.pinned));
            pin.setAttribute('aria-label', `${item.pinned ? '取消置顶' : '置顶'}会话 ${title}`);
            pin.title = item.pinned ? '取消置顶' : '置顶';
            row.querySelector('[data-session-rename]').setAttribute('aria-label', `重命名会话 ${title}`);
            row.querySelector('[data-session-delete]').setAttribute('aria-label', `删除会话 ${title}`);
            row.hidden = Boolean(query && !title.normalize('NFKC').toLocaleLowerCase().includes(query));
            if (!row.hidden) shown += 1;
            sessionList.append(row);
        }
        let empty = sessionList.querySelector('.agent-session-empty');
        if (!shown) {
            if (!empty) {
                empty = element('div', 'agent-session-empty');
                empty.append(icon('message-circle-dashed'), element('span', ''));
                sessionList.append(empty);
                renderIcons(empty);
            }
            empty.querySelector('span').textContent = query ? '没有匹配的会话，试试其他标题' : '尚无已保存的对话';
        } else empty?.remove();
        if (sessionCount) sessionCount.textContent = `${shown} 条`;
        sessionList.scrollTop = scrollTop;
        if (focused?.isConnected && !focused.closest('[hidden]') && document.activeElement !== focused) focused.focus({preventScroll: true});
    }

    function editSessionTitle(id) {
        if (sessionEdits.has(id)) return;
        const item = sessionItems.find(item => item.session_id === id);
        const row = [...sessionList.querySelectorAll('.agent-session-item')].find(row => row.dataset.sessionId === id);
        if (!item || !row || row.querySelector('form')) return;
        const form = element('form', 'agent-session-editor');
        const input = document.createElement('input');
        input.type = 'text';
        input.maxLength = 80;
        input.value = String(item.title || '');
        input.setAttribute('aria-label', '会话名称');
        const save = element('button', '', '保存');
        save.type = 'submit';
        const cancel = element('button', '', '取消');
        cancel.type = 'button';
        const close = () => {
            const restoreFocus = form.contains(document.activeElement) || document.activeElement === document.body;
            form.remove();
            row.classList.remove('is-editing');
            if (restoreFocus && historyRail?.open) row.querySelector('[data-session-rename]')?.focus({preventScroll: true});
        };
        cancel.addEventListener('click', close);
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') { event.stopPropagation(); event.preventDefault(); close(); }
        });
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const title = input.value.trim();
            if (!title) { announce(sessionStatus, '会话名称不能为空'); input.focus(); return; }
            if (await patchSession(id, {title})) close();
        });
        form.append(input, save, cancel);
        row.classList.add('is-editing');
        row.append(form);
        input.focus();
        input.select();
    }

    async function patchSession(id, values) {
        if (sessionEdits.has(id)) return false;
        sessionEdits.add(id);
        const row = [...sessionList.querySelectorAll('.agent-session-item')].find(row => row.dataset.sessionId === id);
        const controls = [...(row?.querySelectorAll('button,input') || [])];
        const focused = controls.includes(document.activeElement) ? document.activeElement : null;
        const selection = focused && typeof focused.selectionStart === 'number'
            ? [focused.selectionStart, focused.selectionEnd] : null;
        controls.forEach(control => { control.disabled = true; });
        try {
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(id)}`, {
                method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values),
            });
            const updated = payload.session;
            if (!updated || updated.session_id !== id) throw new Error('会话更新结果无效，请刷新列表核验');
            sessionItems = sessionItems.map(item => item.session_id === id ? {...item, ...updated} : item);
            renderSessionList(sessionItems);
            announce(sessionStatus, '会话已更新');
            return true;
        } catch (error) {
            announce(sessionStatus, error?.message || '会话更新失败，请稍后重试');
            return false;
        } finally {
            sessionEdits.delete(id);
            controls.forEach(control => { control.disabled = false; });
            // disabled 会使键盘焦点落到 body；失败后恢复，但不抢走用户主动移到别处的焦点。
            if (focused?.isConnected && historyRail?.open &&
                (document.activeElement === document.body || document.activeElement === focused)) {
                focused.focus({preventScroll: true});
                if (selection && typeof focused.setSelectionRange === 'function') focused.setSelectionRange(...selection);
            }
        }
    }

    async function refreshNextActions() {
        if (!nextActions) return;
        nextActions.setAttribute('aria-busy', 'true');
        try {
            const payload = await fetchJSON('/api/agent/next-actions');
            const actions = (Array.isArray(payload.actions) ? payload.actions : []).filter(item =>
                typeof item?.title === 'string' && typeof item.prompt === 'string' && item.prompt.trim()).slice(0, 3);
            const nodes = [];
            for (const action of actions) {
                const group = element('div', 'agent-composer-actions');
                group.setAttribute('role', 'group');
                group.setAttribute('aria-label', action.title.slice(0, 80));
                const button = element('button', 'agent-start-action');
                button.type = 'button';
                button.dataset.agentActionKey = String(action.id || '');
                button.dataset.agentActionTitle = action.title.slice(0, 80);
                button.setAttribute('aria-label', `只读检查：${action.title.slice(0, 80)}`);
                button.disabled = busy || initialRestore || !button.dataset.agentActionKey;
                button.title = String(action.description || action.title).slice(0, 300);
                button.append(element('span', '', action.title.slice(0, 80)), icon('arrow-up-right'));
                const draft = element('button', 'agent-retry-draft');
                draft.type = 'button';
                draft.dataset.agentDraft = clipText(action.prompt, 1000);
                draft.setAttribute('aria-label', `填入草稿：${action.title.slice(0, 80)}`);
                draft.title = '填入草稿后修改，不立即执行';
                draft.append(icon('square-pen'));
                group.append(button, draft);
                nodes.push(group);
            }
            nextActions.replaceChildren(...nodes);
            if (nextActionsStatus) nextActionsStatus.textContent = actions.length
                ? '点击仅做只读检查 · 也可填入草稿修改'
                : (payload.snapshot_status === 'unavailable' ? '待办暂时不可用，仍可直接提问' : '暂时没有待处理事项，也可以直接提问');
            renderIcons(nextActions);
        } catch (_) {
            if (nextActionsStatus) nextActionsStatus.textContent = '待办暂时不可用，仍可直接提问';
        } finally { nextActions.setAttribute('aria-busy', 'false'); }
    }

    async function refreshSessions({quiet = false, signal = null} = {}) {
        historyController?.abort();
        const controller = new AbortController();
        historyController = controller;
        const abort = () => controller.abort();
        if (signal?.aborted) controller.abort();
        else signal?.addEventListener('abort', abort, {once: true});
        if (!quiet) sessionList?.setAttribute('aria-busy', 'true');
        try {
            const payload = await fetchJSON('/api/agent/sessions', {signal: controller.signal});
            if (!Array.isArray(payload?.sessions)) throw new Error('会话列表响应无效');
            if (historyController !== controller) return;
            configureDraftScope(payload.draft_scope);
            renderSessionList(payload.sessions || []);
            announce(sessionStatus, '会话列表已更新');
            return payload;
        } catch (error) {
            if (error?.name !== 'AbortError' && !quiet) announce(sessionStatus, '会话列表加载失败');
            return null;
        } finally {
            signal?.removeEventListener('abort', abort);
            if (historyController === controller) {
                historyController = null;
                sessionList?.setAttribute('aria-busy', 'false');
            }
        }
    }

    function renderRecoveredApproval(approval, {scroll = true} = {}) {
        if (!approval?.plan_id) return null;
        const view = appendMessage('assistant', {recovered: true, scroll});
        const card = buildApproval(approval);
        view.body.append(card);
        const turn = approvalTurnForCard(card);
        turn.item = view.item;
        return card;
    }

    function renderSessionSnapshot(payload, {active = null, terminalTurn = null, preserveScroll = false} = {}) {
        renderScopedTrust(payload.scoped_trust);
        renderTaskSnapshot(payload.task);
        const previousFollow = followOutput;
        const previousScrollTop = transcript?.scrollTop || 0;
        expireCandidateCards();
        transcript?.replaceChildren();
        const candidateGroups = new Map();
        for (const message of payload.messages || []) {
            if (message.role === 'user') appendUser(String(message.content || ''), {recovered: true, scroll: !preserveScroll});
            else if (message.role === 'assistant') {
                const folded = message.candidate_result_ref === payload.candidate_view?.ref && payload.candidate_view?.last_result;
                const content = folded ? String(message.candidate_followup || '') : String(message.content || '');
                if (folded && !content) continue;
                const turn = createAssistantTurn({recovered: true, scroll: !preserveScroll});
                addRecoveredToolTrace(turn, message.tools, message.tool_labels);
                finalizeAnswer(turn, content);
                if (message.candidate_view) {
                    const group = renderCandidateView(turn, message.candidate_view);
                    if (group) candidateGroups.set(String(message.candidate_view.ref || ''), group);
                }
            }
        }

        const candidateRef = String(payload.candidate_view?.ref || '');
        const candidateGroup = candidateRef ? candidateGroups.get(candidateRef) : null;

        let pendingCard = null;
        if (payload.pending_approval) {
            payload.pending_approval = {...payload.pending_approval, scoped_trust: payload.scoped_trust};
            const data = payload.pending_approval.preview?.data;
            if (candidateGroup && data?.source_type === 'resource_candidates') {
                pendingCard = buildApproval(payload.pending_approval);
                candidateGroup._candidateState.output.append(pendingCard);
            } else pendingCard = renderRecoveredApproval(payload.pending_approval, {scroll: !preserveScroll});
        }

        if (active && isCurrentActiveRequest(active) && payload.active_turn && sameActiveTurn(payload.active_turn, active)) {
            active.turnId = active.turnId || String(payload.active_turn.turn_id || '');
            active.kernelGeneration = active.kernelGeneration ?? payload.active_turn.generation;
            active.pendingCard = pendingCard;
            active.candidateGroup = candidateGroup;
            if (pendingCard) {
                // 服务端仍返回pending_approval即尚未消费，不能伪装成已经确认。
                active.kind = 'pending_approval';
                active.turn = approvalTurnForCard(pendingCard);
                active.turn.activePlanId = pendingCard.dataset.planId || '';
                prepareActiveStatus(active, activeProgressText(payload.active_turn, active));
                active.statusNode.dataset.agentActiveStatus = 'true';
            } else if (candidateGroup) {
                active.kind = 'candidate_preview';
                active.statusNode = null;
                prepareActiveStatus(active, activeProgressText(payload.active_turn, active));
            } else {
                active.kind = active.kind || 'query';
                const turn = createAssistantTurn({recovered: true, scroll: !preserveScroll});
                turn.boundSelection = false;
                turn.requestMessage = [...(payload.messages || [])].reverse().find(message => message.role === 'user')?.content || '';
                active.turn = turn;
                prepareActiveStatus(active, activeProgressText(payload.active_turn, active));
            }
            updateActiveProgress(active, payload.active_turn);
        }

        if (terminalTurn && ['failed', 'cancelled', 'interrupted'].includes(terminalTurn.status)) {
            const existingAnswer = [...(payload.messages || [])].reverse().find(message => message.role === 'assistant')?.content;
            const message = String(terminalTurn.message || (terminalTurn.status === 'cancelled'
                ? '本轮处理已停止。' : '任务未能完成，请核对会话状态。'));
            if (!existingAnswer || String(existingAnswer).trim() !== message.trim()) {
                const turn = createAssistantTurn({recovered: true, scroll: !preserveScroll});
                turn.boundSelection = active?.kind === 'candidate_preview';
                turn.requestMessage = active?.turn?.requestMessage || '';
                turn.confirmationRequested = active?.kind === 'confirm';
                const failureMessage = terminalTurn.status === 'failed' && message === '本轮未能完成'
                    ? '当前会话摘要未提供具体失败原因。可查看日志定位；涉及文件变更时，请先核对结果。'
                    : message;
                finalizeError(turn, failureMessage, {cancelled: terminalTurn.status === 'cancelled'});
            }
        }

        expireCandidateCards(candidateGroup || null);
        syncCandidateButtons();
        followOutput = preserveScroll ? previousFollow : true;
        if (followOutput) scrollToBottom(true);
        else if (transcript) {
            transcript.scrollTop = previousScrollTop;
            if (newRepliesButton) newRepliesButton.hidden = false;
        }
        setConsoleEmpty(!transcript?.childElementCount);
        return {candidateGroup, pendingCard};
    }

    async function loadSession(targetId, {closeHistory = true, startup = false, signal = null, preserveScroll = false} = {}) {
        if (!SESSION_RE.test(targetId)) return false;
        if (!startup) stopInitialRestore();
        const switching = targetId !== sessionId;
        const restoreResumeFocus = switching && document.activeElement === resumeButton;
        const previousSessionId = sessionId;
        if (switching) {
            clearTaskSnapshot();
            saveDraft();
            ++sessionLoadGeneration;
            invalidateActiveRequest();
            rememberSession(targetId);
            restoreDraft();
            setBusy(true);
        } else if (activeRequest?.receiptOnly && activeRequest.sessionId === targetId) {
            invalidateActiveRequest();
        }
        const active = !switching && activeRequest?.sessionId === targetId ? activeRequest : null;
        const generation = active ? sessionLoadGeneration : ++sessionLoadGeneration;
        try {
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(targetId)}`, {signal});
            if (payload?.session_id !== targetId || !Array.isArray(payload?.messages)) throw new Error('会话内容响应无效');
            if (generation !== sessionLoadGeneration || (active && !isCurrentActiveRequest(active))) return false;
            if (configureDraftScope(payload.draft_scope) || generation !== sessionLoadGeneration
                || (active && !isCurrentActiveRequest(active))) return false;
            if (!(Number(payload.pending_effect_count) > 0)) receiptObservationWindows.delete(targetId);
            if (active) {
                renderTaskSnapshot(payload.task);
                const lastTurn = payload.last_turn;
                if (sameActiveTurn(lastTurn, active)) {
                    finishObservedTurn(active, payload, lastTurn);
                } else if (payload.active_turn && sameActiveTurn(payload.active_turn, active)) {
                    if (payload.active_turn.status === 'cancelling') active.cancellationObserved = true;
                    updateActiveProgress(active, payload.active_turn);
                }
            } else {
                const lastTurn = payload.last_turn;
                const terminalTurn = lastTurn
                    && lastTurn.status !== 'completed'
                    && String(lastTurn.request_id || '').trim()
                    && String(lastTurn.turn_id || '').trim()
                    ? lastTurn : null;
                const recovered = payload.active_turn ? {
                    controller: new AbortController(),
                    requestId: String(payload.active_turn.request_id || ''),
                    turnId: String(payload.active_turn.turn_id || ''),
                    kernelGeneration: payload.active_turn.generation,
                    sessionId: targetId,
                    sessionGeneration: generation,
                    kind: '',
                    protected: payload.active_turn.protected === true,
                    cancelAccepted: payload.active_turn.status === 'cancelling',
                    pollTimer: null,
                    observerController: null,
                    observing: true,
                } : Number(payload.pending_effect_count) > 0 ? {
                    controller: new AbortController(),
                    requestId: String(payload.last_turn?.request_id || ''),
                    turnId: String(payload.last_turn?.turn_id || ''),
                    sessionId: targetId,
                    sessionGeneration: generation,
                    pollTimer: null,
                    observerController: null,
                    observing: true,
                    receiptOnly: true,
                } : null;
                if (recovered) activeRequest = recovered;
                const recoveredTurn = recovered && !recovered.receiptOnly ? recovered : null;
                renderSessionSnapshot(payload, {active: recoveredTurn, terminalTurn: recoveredTurn ? null : terminalTurn, preserveScroll});
                if (recovered?.receiptOnly) startReceiptObservation(recovered, payload);
                else if (recovered) scheduleActiveObservation(recovered);
                else setBusy(false);
            }
            if (restoreResumeFocus && resumeButton && !resumeButton.disabled) resumeButton.focus({preventScroll: true});
            if (closeHistory) closeHistoryRail();
            if (!startup) refreshSessions({quiet: true});
            return true;
        } catch (error) {
            if (generation === sessionLoadGeneration) {
                if (switching && sessionId === targetId) {
                    rememberSession(previousSessionId);
                    restoreDraft();
                    setBusy(false);
                }
                announce(sessionStatus, error?.message || '会话加载失败');
            }
            return false;
        }
    }

    async function deleteSession(targetId) {
        if (busy || !SESSION_RE.test(targetId)) return;
        try {
            await fetchJSON(`/api/agent/sessions/${encodeURIComponent(targetId)}`, {method: 'DELETE'});
            removeDraft(targetId);
            if (targetId === sessionId) {
                promptInput.value = '';
                startNewSession();
            }
            await refreshSessions();
        } catch (error) {
            announce(sessionStatus, error?.message || '会话删除失败');
        }
    }

    function startNewSession() {
        clearTaskSnapshot();
        stopInitialRestore();
        saveDraft();
        ++sessionLoadGeneration;
        invalidateActiveRequest();
        setBusy(false);
        expireCandidateCards();
        rememberSession(createId('session'));
        restoreDraft();
        followOutput = true;
        if (newRepliesButton) newRepliesButton.hidden = true;
        transcript?.replaceChildren();
        setConsoleEmpty(true);
        promptInput?.focus();
        closeHistoryRail();
        refreshSessions({quiet: true});
    }

    function openHistoryRail() {
        if (!historyRail) return;
        if (typeof historyRail.showModal === 'function') {
            if (!historyRail.open) historyRail.showModal();
        } else {
            historyRail.setAttribute('open', '');
        }
        historyButton?.setAttribute('aria-expanded', 'true');
        document.getElementById('agent-session-heading')?.focus({preventScroll: true});
        // 初始恢复已经在加载列表，打开抽屉只观察它，不中止并替换其请求。
        if (!startupController) refreshSessions();
    }

    function closeHistoryRail() {
        if (!historyRail) return;
        if (typeof historyRail.close === 'function' && historyRail.open) historyRail.close();
        else historyRail.removeAttribute('open');
        historyButton?.setAttribute('aria-expanded', 'false');
    }

    function scheduleCandidateExpiry(group) {
        if (candidateExpiryTimer !== null) clearTimeout(candidateExpiryTimer);
        candidateExpiryTimer = null;
        const expiresAt = Number(group?._candidateState?.view?.expires_at);
        if (!group || !Number.isFinite(expiresAt)) return;
        candidateExpiryTimer = setTimeout(() => {
            candidateExpiryTimer = null;
            syncCandidateButtons();
        }, Math.max(0, Math.min(2147483647, expiresAt * 1000 - Date.now() + 25)));
    }

    function expireCandidateCards(except = null) {
        if (candidateExpiryTimer !== null) clearTimeout(candidateExpiryTimer);
        candidateExpiryTimer = null;
        transcript?.querySelectorAll('.agent-candidates').forEach(group => {
            if (group !== except) group.dataset.expired = 'true';
        });
        syncCandidateButtons();
        if (except?.isConnected) scheduleCandidateExpiry(except);
    }

    function removeCandidateView(turn) {
        const group = turn?.candidateGroup;
        if (!group) return;
        scheduleCandidateExpiry(null);
        group.remove();
        turn.candidateGroup = null;
        syncCandidateButtons();
    }

    function candidateStorageKey(ref) {
        return `mediaflux:agent:batch:${draftScope}:${sessionId}:${ref}`;
    }

    function saveCandidateDraft(group) {
        const state = group._candidateState;
        if (!state) return;
        try {
            sessionStorage.setItem(state.storageKey, JSON.stringify({
                positions: [...state.selected], target: state.target, expanded: state.details.open,
            }));
        } catch (_) { /* Storage can be disabled; keep the current in-memory selection. */ }
    }

    function candidateShortName(item) {
        const range = item.coverage;
        const coverage = Array.isArray(range) && Number.isInteger(range[1]) && Number.isInteger(range[2])
            ? `${range[0] ? `S${String(range[0]).padStart(2, '0')} · ` : ''}${String(range[1]).padStart(2, '0')}–${String(range[2]).padStart(2, '0')} 集`
            : clipText(item.title, 48);
        const specs = ['resolution', 'effect', 'media'].map(key => item.tags?.[key]).filter(value => typeof value === 'string' && value.trim() && !coverage.toLowerCase().includes(value.toLowerCase()));
        const title = clipText(item.media_title || item.title, 48);
        return `#${item.position} · ${title}${coverage !== title ? ` · ${coverage}` : ''}${specs.length ? ` · ${specs.join(' / ')}` : ''}`;
    }

    function syncCandidateButtons() {
        transcript?.querySelectorAll('.agent-candidates').forEach(group => {
            const state = group._candidateState;
            if (!state) return;
            const expired = group.dataset.expired === 'true' || !state.view.selection_ref || state.view.expires_at * 1000 <= Date.now();
            const available = state.view.targets?.find(target => target.value === state.target)?.available === true;
            const pending = Boolean(group.querySelector('.agent-confirmation-card:not(.is-expired)'));
            group.querySelectorAll('[data-candidate-control]').forEach(control => { control.disabled = busy || expired || pending; });
            state.preview.disabled = busy || expired || pending || !available || !state.selected.size;
            state.preview.textContent = group.dataset.previewing === 'true' ? '正在预检…' : `预览下载 ${state.selected.size} 项`;
            state.preview.setAttribute('aria-busy', String(group.dataset.previewing === 'true'));
            state.count.textContent = `已选 ${state.selected.size} / ${state.view.items.length} 项`;
            const occupied = new Set();
            let overlap = false;
            for (const item of state.view.items.filter(item => state.selected.has(item.position))) {
                if (!Array.isArray(item.coverage)) continue;
                const [season, start, end] = item.coverage;
                if (!Number.isInteger(start) || !Number.isInteger(end) || end - start > 1000) continue;
                for (let ep = start; ep <= end; ep++) {
                    const key = `${item.media_scope || item.media_title || ''}:${season}:${ep}`;
                    if (occupied.has(key)) overlap = true;
                    occupied.add(key);
                }
            }
            state.note.textContent = expired ? '此批候选仅供回看，请重新搜索后选择。'
                : pending ? '请核对下方整批预览，确认后才提交；取消可继续改选。'
                : !available ? '当前目标尚未配置或登录，请切换可用目标，或先完成设置。'
                : overlap ? '已选版本包含重叠集数；若不需要保留多个版本，请取消重叠项。'
                : '选择与切换目标不会提交下载；预检后仍需确认一次。';
            state.note.classList.toggle('is-warning', overlap && !expired);
            group.querySelectorAll('[data-candidate-position]').forEach(input => {
                input.checked = state.selected.has(Number(input.dataset.candidatePosition));
            });
        });
    }

    function renderCandidateView(turn, view) {
        if (!view || !Array.isArray(view.items) || typeof view.ref !== 'string' || !Number.isFinite(view.expires_at)) return;
        if (turn.candidateGroup?.dataset.candidateView === view.ref) return turn.candidateGroup;
        const items = view.items.filter(item => typeof item?.title === 'string' && Number.isInteger(item.position) && item.position > 0);
        if (!items.length) return;
        const previousGroup = turn.candidateGroup;
        const group = element('section', 'agent-candidates');
        group.dataset.candidateView = view.ref;
        group.setAttribute('aria-label', '资源批量选择');
        const heading = element('div', 'agent-candidates-heading');
        const recommended = (Array.isArray(view.recommended_positions) ? view.recommended_positions : []).filter(pos => items.some(item => item.position === pos));
        heading.append(element('strong', '', recommended.length ? '推荐组合' : '搜索结果'), element('span', '', `${items.length} 个版本`));
        const summary = element('ul', 'agent-candidate-recommendation');
        for (const item of items.filter(item => recommended.includes(item.position))) summary.append(element('li', '', candidateShortName(item)));
        if (!summary.childElementCount) summary.append(element('li', '', '请选择需要的版本，再预览下载。'));
        let stored = null;
        try { stored = JSON.parse(sessionStorage.getItem(candidateStorageKey(view.ref)) || 'null'); } catch (_) { /* Optional draft. */ }
        const positions = Array.isArray(stored?.positions) ? stored.positions : recommended;
        const handled = Array.isArray(view.last_result?.handled_positions) ? view.last_result.handled_positions : [];
        const selected = new Set(positions.filter(pos => items.some(item => item.position === pos) && !handled.includes(pos)));
        const targets = Array.isArray(view.targets) ? view.targets : [];
        const target = targets.some(item => item.value === stored?.target) ? stored.target : view.target || 'guangya';
        const toolbar = element('div', 'agent-candidate-toolbar');
        const targetLabel = element('label', 'agent-candidate-target');
        targetLabel.append(element('span', '', view.target_source === 'saved_preference' ? '下载目标 · 已保存偏好' : '下载目标 · 可切换'));
        const select = document.createElement('select');
        select.setAttribute('aria-label', '下载目标');
        select.dataset.candidateControl = 'target';
        for (const option of targets) {
            if (!['qb', 'guangya', 'both'].includes(option.value)) continue;
            const node = element('option', '', `${option.label}${option.available ? '' : '（未就绪）'}`);
            node.value = option.value;
            select.append(node);
        }
        select.value = target;
        targetLabel.append(select);
        const preview = element('button', 'agent-candidate-select');
        preview.type = 'button';
        preview.dataset.candidateControl = 'preview';
        preview.dataset.candidateSelect = view.selection_ref || '';
        toolbar.append(targetLabel, preview);
        const details = document.createElement('details');
        details.className = 'agent-candidates-more';
        details.open = stored?.expanded === true;
        const detailsTitle = element('summary', '', '挑选版本');
        const count = element('span', 'agent-candidate-count');
        detailsTitle.append(count);
        const list = element('div', 'agent-candidate-list');
        for (const item of items) {
            const row = element('article', 'agent-candidate-row');
            const label = element('label', 'agent-candidate-option');
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.dataset.candidateControl = 'position';
            input.dataset.candidatePosition = String(item.position);
            input.setAttribute('aria-label', `选择候选 ${item.position}`);
            const info = element('span', 'agent-candidate-info');
            info.append(element('strong', '', candidateShortName(item)), element('span', 'agent-candidate-meta', [item.size_text, item.site_name].filter(Boolean).join(' · ')));
            label.append(input, info);
            const detail = document.createElement('details');
            detail.className = 'agent-candidate-detail';
            detail.append(element('summary', '', '详情'), element('p', '', item.title.slice(0, 300)));
            for (const reason of [...(Array.isArray(item.reasons) ? item.reasons : []), ...(Array.isArray(item.warnings) ? item.warnings : [])].filter(item => typeof item === 'string').slice(0, 8)) detail.append(element('p', '', reason.slice(0, 120)));
            row.append(label, detail);
            list.append(row);
            input.addEventListener('change', () => {
                if (input.checked) selected.add(item.position); else selected.delete(item.position);
                syncCandidateButtons(); saveCandidateDraft(group);
            });
        }
        details.append(detailsTitle, list);
        const note = element('p', 'agent-candidates-note');
        note.setAttribute('aria-live', 'polite');
        const output = element('div', 'agent-candidate-output');
        group._candidateState = {view: {...view, items}, selected, target, details, preview, count, note, output, storageKey: candidateStorageKey(view.ref)};
        select.addEventListener('change', () => { group._candidateState.target = select.value; syncCandidateButtons(); saveCandidateDraft(group); });
        details.addEventListener('toggle', () => saveCandidateDraft(group));
        group.append(heading, summary, toolbar, note, details, output);
        turn.candidateGroup = group;
        if (previousGroup?.isConnected) previousGroup.replaceWith(group);
        else turn.card.append(group);
        if (typeof view.last_result?.text === 'string' && view.last_result.text) {
            const restored = element('section');
            output.append(restored);
            replaceApprovalWithResult(restored, view.last_result.text);
        }
        syncCandidateButtons();
        scheduleCandidateExpiry(group);
        scrollToBottom();
        return group;
    }

    async function selectCandidate(button) {
        const group = button.closest('.agent-candidates');
        const state = group?._candidateState;
        if (busy || button.disabled || !state) return;
        syncCandidateButtons();
        if (button.disabled) return;
        clearReceiptObserverForNewRequest();
        const selection = {ref: state.view.selection_ref, positions: [...state.selected].sort((a, b) => a - b), target: state.target};
        const message = `预览候选 ${selection.positions.map(pos => `#${pos}`).join('、')}，下载目标：${approvalTargetLabel(selection.target)}。`;
        const targetSessionId = sessionId;
        const requestId = createId('rq');
        const controller = new AbortController();
        const active = {
            controller, requestId, sessionId: targetSessionId,
            sessionGeneration: sessionLoadGeneration, kind: 'candidate_preview',
            turn: {boundSelection: true, failed: false}, candidateGroup: group,
            turnId: '', pollTimer: null, observerController: null, observing: false,
            protected: false, cancelAccepted: false,
        };
        activeRequest = active;
        group.dataset.previewing = 'true';
        setBusy(true);
        saveCandidateDraft(group);
        announce(responseStatus, '正在生成整批资源预览，尚未下载');
        try {
            const response = await queryRequest(message, requestId, controller.signal, selection, targetSessionId);
            if (!isCurrentActiveRequest(active)) return;
            const terminalEvent = await readEventStream(response, event => {
                if (!isCurrentActiveRequest(active)) return;
                if (event.request_id && String(event.request_id) !== active.requestId) return;
                active.turnId = active.turnId || String(event.turn_id || '');
                if (active.cancelAccepted && isTerminalEvent(event)) return;
                const payload = event.payload || {};
                consumeTerminalTask(event);
                if (event.type === 'effect.approval_required' && payload.plan) {
                    const plan = payload.plan;
                    if (plan.plan_id) expireVisibleApprovals(String(plan.plan_id));
                    const card = buildApproval({...plan, result: payload.result});
                    card._candidateSelection = selection;
                    state.output.replaceChildren(card);
                    active.pendingCard = card;
                } else if (event.type === 'turn.failed') {
                    const notice = element('p', 'agent-candidates-note agent-candidate-feedback', payload.message || '预检未完成，请重试。');
                    if (active.pendingCard?.isConnected) {
                        state.output.querySelector('.agent-candidate-feedback')?.remove();
                        state.output.append(notice);
                    } else state.output.replaceChildren(notice);
                } else if (event.type === 'turn.cancelled') {
                    state.output.replaceChildren(element('p', 'agent-candidates-note', '本次预检已停止。'));
                } else if (event.type === 'turn.completed' && payload.status !== 'approval_required') {
                    state.output.replaceChildren(element('p', 'agent-candidates-note', payload.answer || '未生成可执行计划，请检查目标与资源状态。'));
                }
            });
            if (!isCurrentActiveRequest(active)) return;
            if (!terminalEvent || active.cancelAccepted) startActiveObservation(active);
            else {
                await reconcilePendingApprovals(active);
                if (!isCurrentActiveRequest(active)) return;
                announce(responseStatus, terminalEvent.type === 'turn.cancelled' ? '请求已停止' : '预检已完成');
                startReceiptObservation(active);
                refreshSessions({quiet: true});
            }
        } catch (error) {
            if (!isCurrentActiveRequest(active)) return;
            if (Number.isInteger(error?.httpStatus) && error.httpStatus >= 400 && error.httpStatus < 500) {
                const notice = element('p', 'agent-candidates-note agent-candidate-feedback', error.message || '预检未完成。');
                state.output.querySelector('.agent-candidate-feedback')?.remove();
                state.output.append(notice);
                announce(responseStatus, '预检请求未执行，请核对状态');
            } else startActiveObservation(active);
        } finally {
            if (activeRequest === active && !active.observing) {
                group.dataset.previewing = 'false';
                activeRequest = null;
                clearActiveObservation(active);
                setBusy(false);
                refreshSessions({quiet: true});
            }
        }
    }

    function hideRestoreNotice() {
        const transferFocus = restoreNotice?.contains(document.activeElement);
        if (restoreNotice) restoreNotice.hidden = true;
        if (restoreActions) restoreActions.hidden = true;
        if (transferFocus) promptInput?.focus({preventScroll: true});
    }

    function stopInitialRestore() {
        ++startupAttempt;
        startupController?.abort();
        startupController = null;
        initialRestore = false;
        consoleNode?.classList.remove('is-restoring');
        consoleNode?.removeAttribute('data-initial-restore');
        consoleNode?.setAttribute('aria-busy', 'false');
        hideRestoreNotice();
        syncSend();
    }

    async function restoreInitialSession() {
        const attempt = ++startupAttempt;
        const generation = sessionLoadGeneration;
        startupController?.abort();
        const controller = new AbortController();
        startupController = controller;
        hideRestoreNotice();
        if (restoreText) restoreText.textContent = '正在恢复上次对话…';
        consoleNode?.setAttribute('aria-busy', initialRestore ? 'true' : 'false');
        // 不用闪烁骨架掩盖假空态；慢请求才给固定位置的文字反馈。
        const noticeTimer = setTimeout(() => {
            if (attempt === startupAttempt && initialRestore && restoreNotice) restoreNotice.hidden = false;
        }, 500);
        const timeout = setTimeout(() => controller.abort(), 12_000);
        try {
            const payload = await refreshSessions({quiet: true, signal: controller.signal});
            if (attempt !== startupAttempt || generation !== sessionLoadGeneration) return;
            if (!payload) throw new Error('会话列表暂不可用');
            if (storedSessionId()) {
                const loaded = await loadSession(sessionId, {closeHistory: false, startup: true, signal: controller.signal});
                if (attempt !== startupAttempt) return;
                if (!loaded) throw new Error('会话内容暂不可用');
            }
            stopInitialRestore();
            setConsoleEmpty(!transcript?.childElementCount);
        } catch (_) {
            if (attempt !== startupAttempt) return;
            if (initialRestore && restoreNotice) {
                restoreNotice.hidden = false;
                if (restoreActions) restoreActions.hidden = false;
                if (restoreText) restoreText.textContent = controller.signal.aborted
                    ? '恢复对话超时。可以重试，或开始新会话；已有历史不会被删除。'
                    : '暂时无法恢复上次对话。可以重试，或开始新会话；已有历史不会被删除。';
                consoleNode?.setAttribute('aria-busy', 'false');
            }
        } finally {
            clearTimeout(noticeTimer);
            clearTimeout(timeout);
            if (startupController === controller) startupController = null;
            syncSend();
        }
    }

    function syncViewportHeight() {
        const height = window.visualViewport?.height || window.innerHeight;
        document.documentElement.style.setProperty('--agent-viewport-height', `${Math.round(height)}px`);
        consoleNode?.style.setProperty('--agent-composer-height', `${Math.round(composer?.getBoundingClientRect().height || 100)}px`);
    }

    composer?.addEventListener('submit', (event) => {
        event.preventDefault();
        sendQuery(promptInput?.value || '');
    });
    promptInput?.addEventListener('input', () => { resizePrompt(); saveDraft(); });
    transcript?.addEventListener('scroll', () => {
        followOutput = transcriptNearBottom();
        if (followOutput && newRepliesButton) newRepliesButton.hidden = true;
    }, {passive: true});
    newRepliesButton?.addEventListener('click', () => scrollToBottom(true));
    function handlePageHide() {
        saveDraft();
        if (activeRequest?.receiptOnly) receiptResumeNeeded = true;
        ++sessionLoadGeneration;
        invalidateActiveRequest();
        startupController?.abort();
        historyController?.abort();
        if (candidateExpiryTimer !== null) clearTimeout(candidateExpiryTimer);
        candidateExpiryTimer = null;
    }

    function handlePageShow(event) {
        if (event.persisted) {
            receiptResumeNeeded = true;
            resumeReceiptObservation();
        }
    }

    window.addEventListener('pagehide', handlePageHide);
    window.addEventListener('pageshow', handlePageShow);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) pauseReceiptObservation();
        else resumeReceiptObservation();
    });
    window.addEventListener('blur', pauseReceiptObservation);
    window.addEventListener('focus', resumeReceiptObservation);
    promptInput?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            composer?.requestSubmit();
        }
    });
    sessionStopButton?.addEventListener('click', stopSessionTasks);
    newSessionButton?.addEventListener('click', startNewSession);
    document.getElementById('agentRestoreRetry')?.addEventListener('click', restoreInitialSession);
    document.getElementById('agentRestoreNew')?.addEventListener('click', startNewSession);
    resumeButton?.addEventListener('click', () => latestSessionId && loadSession(latestSessionId));
    historyButton?.addEventListener('click', openHistoryRail);
    historyRail?.addEventListener('cancel', (event) => {
        event.preventDefault();
        closeHistoryRail();
    });
    historyRail?.addEventListener('click', (event) => {
        if (event.target === historyRail || event.target.closest('[data-agent-history-close]')) closeHistoryRail();
    });
    sessionSearch?.addEventListener('input', () => renderSessionList(sessionItems));
    sessionList?.addEventListener('click', (event) => {
        const open = event.target.closest('[data-session-open]');
        const remove = event.target.closest('[data-session-delete]');
        const rename = event.target.closest('[data-session-rename]');
        const pin = event.target.closest('[data-session-pin]');
        if (rename) editSessionTitle(rename.dataset.sessionRename);
        if (pin) {
            const item = sessionItems.find(item => item.session_id === pin.dataset.sessionPin);
            if (item) patchSession(item.session_id, {pinned: !item.pinned});
        }
        if (open) loadSession(open.dataset.sessionOpen || '');
        if (remove) deleteSession(remove.dataset.sessionDelete || '');
    });
    transcript?.addEventListener('click', (event) => {
        const candidate = event.target.closest('[data-candidate-select]');
        if (candidate) selectCandidate(candidate);
        const confirm = event.target.closest('[data-effect-confirm]');
        const cancel = event.target.closest('[data-effect-cancel]');
        const explain = event.target.closest('[data-effect-explain]');
        if (explain) explainEffect(explain);
        if (confirm) confirmEffect(confirm);
        if (cancel) cancelEffect(cancel);
    });
    page.addEventListener('click', (event) => {
        const action = event.target.closest('[data-agent-action-key]');
        if (action && !action.disabled) sendQuery(action.dataset.agentActionTitle, action.dataset.agentActionKey);
        const draft = event.target.closest('[data-agent-draft]');
        if (draft) fillDraft(draft.dataset.agentDraft);
    });
    window.visualViewport?.addEventListener('resize', syncViewportHeight, {passive: true});
    window.addEventListener('resize', syncViewportHeight, {passive: true});

    syncViewportHeight();
    setConsoleEmpty(!initialRestore);
    refreshNextActions();
    renderIcons(page);
    restoreInitialSession();
})();
