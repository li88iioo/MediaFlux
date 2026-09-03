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
    const stopButton = document.getElementById('agentStop');
    const newSessionButton = document.getElementById('agentNewSession');
    const resumeButton = document.getElementById('agentResumeLatestSession');
    const historyButton = document.getElementById('toggleAgentRail');
    const historyRail = document.getElementById('agentHistoryRail');
    const sessionList = document.getElementById('agentSessionList');
    const sessionCount = document.getElementById('agentSessionCount');
    const sessionStatus = document.getElementById('agentSessionStatus');
    const responseStatus = document.getElementById('agentResponseStatus');

    const SESSION_KEY = 'mediaflux.agent.kernel.session.v1';
    const SESSION_RE = /^[A-Za-z0-9_-]{16,64}$/;
    const MAX_TRANSCRIPT_ITEMS = 120;
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

    let sessionId = storedSessionId() || createId('session');
    let latestSessionId = '';
    let activeRequest = null;
    let historyController = null;
    let sessionLoadGeneration = 0;
    let busy = false;

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
            const value = localStorage.getItem(SESSION_KEY) || '';
            return SESSION_RE.test(value) ? value : '';
        } catch (_) {
            return '';
        }
    }

    function rememberSession(value) {
        sessionId = value;
        try { localStorage.setItem(SESSION_KEY, value); } catch (_) { /* private mode */ }
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
        if (promptInput) {
            promptInput.placeholder = empty
                ? (promptInput.dataset.emptyPlaceholder || '询问 MediaFlux')
                : (promptInput.dataset.activePlaceholder || '继续描述或调整任务');
        }
    }

    function transcriptNearBottom() {
        return !transcript || transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 140;
    }

    function scrollToBottom(force = false) {
        if (!transcript || (!force && !transcriptNearBottom())) return;
        requestAnimationFrame(() => {
            transcript.scrollTop = transcript.scrollHeight;
        });
    }

    function pruneTranscript() {
        if (!transcript) return;
        while (transcript.children.length > MAX_TRANSCRIPT_ITEMS) {
            transcript.firstElementChild?.remove();
        }
    }

    function appendMessage(role, {recovered = false} = {}) {
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
        scrollToBottom(true);
        return {item, body};
    }

    function appendUser(text, options = {}) {
        const view = appendMessage('user', options);
        view.body.append(element('p', '', text));
        return view;
    }

    function parseTextBlocks(text) {
        const root = element('div', 'agent-rich-text');
        const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
        let list = null;
        let firstParagraph = true;
        const flushList = () => {
            if (list) root.append(list);
            list = null;
        };
        for (const raw of lines) {
            const line = raw.trim();
            if (!line) {
                flushList();
                continue;
            }
            const bullet = line.match(/^(?:[-*+•]|\d{1,3}[.)、])\s+(.+)$/);
            if (bullet) {
                if (!list) list = element('ul');
                list.append(element('li', '', bullet[1]));
                continue;
            }
            flushList();
            const paragraph = element('p', firstParagraph && line.length <= 140 ? 'agent-answer-lead' : '', line);
            root.append(paragraph);
            firstParagraph = false;
        }
        flushList();
        return root;
    }

    function replaceRichText(target, text) {
        const rendered = parseTextBlocks(text);
        target.replaceChildren(...rendered.childNodes);
    }

    function createAssistantTurn({recovered = false} = {}) {
        const view = appendMessage('assistant', {recovered});
        const card = element('section', 'agent-result-card agent-streaming');
        const head = element('div', 'agent-stream-head');
        head.append(icon('loader-circle'), element('span', '', '正在理解任务'));
        const text = element('div', 'agent-stream-text');
        const steps = element('div', 'agent-stream-steps');
        card.append(head, text, steps);
        view.body.append(card);
        renderIcons(card);
        return {
            ...view,
            card,
            head,
            headText: head.querySelector('span'),
            text,
            steps,
            rounds: new Map(),
            currentRound: 0,
            completedTools: new Set(),
            approvalNode: null,
        };
    }

    function setTurnStatus(turn, label, iconName = 'loader-circle') {
        if (!turn?.head) return;
        turn.head.replaceChildren(icon(iconName), element('span', '', label));
        renderIcons(turn.head);
    }

    function toolLabel(tool) {
        const prefix = String(tool || '').split('.', 1)[0].toLowerCase();
        return TOOL_LABELS[prefix] || '调用项目能力';
    }

    function addStep(turn, key, label, {warning = false, pending = false} = {}) {
        if (!turn?.steps || turn.completedTools.has(key)) return;
        turn.completedTools.add(key);
        const row = element('div', `agent-stream-step${warning ? ' is-warning' : ''}`);
        row.dataset.stepKey = key;
        row.append(icon(pending ? 'loader-circle' : warning ? 'triangle-alert' : 'check'), element('span', '', label));
        turn.steps.append(row);
        renderIcons(row);
        scrollToBottom();
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
        turn.card.classList.remove('agent-streaming', 'is-interrupted');
        turn.card.classList.add('has-narrative', 'is-conversation');
        turn.head.remove();
        turn.steps.remove();
        turn.text.className = 'agent-narrative';
        replaceRichText(turn.text, text || '查询已完成。');
        scrollToBottom(true);
    }

    function finalizeError(turn, message, {cancelled = false} = {}) {
        turn.card.classList.remove('agent-streaming');
        turn.card.classList.add(cancelled ? 'agent-cancelled' : 'is-interrupted');
        setTurnStatus(turn, cancelled ? '已停止' : '未能完成', cancelled ? 'circle-stop' : 'triangle-alert');
        turn.text.textContent = message || (cancelled ? '本次任务已停止。' : 'Agent 暂时无法完成该请求。');
        turn.steps.replaceChildren();
        scrollToBottom(true);
    }

    function scalarPreviewRows(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
        const rows = [];
        for (const [key, value] of Object.entries(data)) {
            if (rows.length >= 8 || value === null || typeof value === 'object') continue;
            const text = String(value).trim();
            if (!text) continue;
            rows.push([key.slice(0, 60), text.slice(0, 320)]);
        }
        return rows;
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
        title.append(element('span', '', '行动计划'), element('strong', '', '确认后执行变更'));
        heading.append(title);
        const risk = element('span', 'agent-confirmation-risk', String(approval.effect || 'WRITE').toUpperCase() === 'DANGER' ? '高风险' : '需确认');
        if (String(approval.effect || '').toUpperCase() === 'DANGER') risk.classList.add('is-danger');
        head.append(heading, risk);

        const intro = element('div', 'agent-confirmation-intro');
        intro.append(parseTextBlocks(publicSummary(approval.preview) || publicSummary(approval.result) || '预检已完成。'));
        const facts = element('dl', 'agent-confirmation-facts');
        const previewData = approval.preview?.data;
        for (const [key, value] of scalarPreviewRows(previewData)) {
            const row = element('div', 'agent-confirmation-fact');
            row.append(element('dt', '', key), element('dd', '', value));
            facts.append(row);
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
        actions.append(cancel, confirm);
        card.append(head, intro);
        if (facts.childElementCount) card.append(facts);
        card.append(status, actions);
        renderIcons(card);
        return card;
    }

    function showApproval(turn, approval) {
        const card = buildApproval(approval);
        turn.card.replaceWith(card);
        turn.approvalNode = card;
        scrollToBottom(true);
    }

    function replaceApprovalWithResult(card, text, {error = false, cancelled = false} = {}) {
        const result = element('section', `agent-result-card${error ? ' is-interrupted' : ''}${cancelled ? ' agent-cancelled' : ''}`);
        const head = element('div', 'agent-stream-head');
        head.append(icon(error ? 'triangle-alert' : cancelled ? 'circle-stop' : 'circle-check-big'), element('span', '', error ? '执行失败' : cancelled ? '已取消' : '执行完成'));
        const body = element('div', 'agent-stream-text', text);
        result.append(head, body);
        card.replaceWith(result);
        renderIcons(result);
        scrollToBottom(true);
    }

    function expireVisibleApprovals() {
        transcript?.querySelectorAll('.agent-confirmation-card[data-plan-id]').forEach((card) => {
            card.classList.add('is-expired');
            card.querySelectorAll('button').forEach((button) => { button.disabled = true; });
            const status = card.querySelector('.agent-confirmation-preflight span');
            if (status) status.textContent = '已由新的任务替代，本计划不会执行。';
        });
    }

    function applyEvent(turn, event) {
        const payload = event?.payload && typeof event.payload === 'object' ? event.payload : {};
        switch (event?.type) {
        case 'turn.started':
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
            const round = Number(payload.round || turn.currentRound || 1);
            const value = `${turn.rounds.get(round) || ''}${String(payload.delta || '')}`;
            turn.rounds.set(round, value);
            turn.text.textContent = value;
            scrollToBottom();
            break;
        }
        case 'model.tool_call': {
            const key = `call:${payload.call_id || event.sequence}`;
            addStep(turn, key, `${toolLabel(payload.tool)}…`, {pending: true});
            turn.text.textContent = '';
            setTurnStatus(turn, toolLabel(payload.tool));
            break;
        }
        case 'tool.started':
            setTurnStatus(turn, toolLabel(payload.tool));
            break;
        case 'tool.progress': {
            const summary = publicSummary(payload);
            if (summary) setTurnStatus(turn, summary.slice(0, 100));
            break;
        }
        case 'tool.completed':
            addStep(turn, `done:${payload.call_id || event.sequence}`, `${toolLabel(payload.tool)}完成`);
            break;
        case 'tool.failed':
            addStep(turn, `failed:${payload.call_id || event.sequence}`, '当前方法不可用，已交回模型调整', {warning: true});
            setTurnStatus(turn, '正在调整方案');
            break;
        case 'effect.preview_started':
            setTurnStatus(turn, '正在生成安全变更预览');
            break;
        case 'effect.approval_required':
            if (payload.plan) {
                showApproval(turn, {
                    plan_id: payload.plan.plan_id,
                    tool_name: payload.plan.tool_name || payload.tool,
                    effect: payload.plan.effect,
                    preview: payload.plan.preview || {},
                    result: payload.result || {},
                    expires_at: payload.plan.expires_at || '',
                });
            }
            break;
        case 'effect.completed':
            turn.effectResult = payload.result || {};
            break;
        case 'effect.failed':
            turn.effectError = payload.message || '确认执行失败。';
            break;
        case 'turn.completed':
            if (payload.status === 'success') finalizeAnswer(turn, payload.answer || '查询已完成。');
            else if (payload.status === 'effect_completed') {
                finalizeAnswer(turn, publicSummary(turn.effectResult) || '操作已完成并通过写后校验。');
            }
            break;
        case 'turn.failed':
            finalizeError(turn, payload.message || turn.effectError || 'Agent 暂时无法完成该请求。');
            break;
        case 'turn.cancelled':
            finalizeError(turn, '本次任务已停止。', {cancelled: true});
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
            throw new Error(message);
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
                    if (line) consume(JSON.parse(line));
                    lineEnd = buffer.indexOf('\n');
                }
                if (done) break;
            }
            buffer += decoder.decode();
            if (buffer.trim()) consume(JSON.parse(buffer.trim()));
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
        if (!response.ok) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
        return payload;
    }

    function setBusy(value, {stoppable = false} = {}) {
        busy = Boolean(value);
        if (promptInput) promptInput.disabled = busy;
        if (sendButton) {
            sendButton.hidden = busy && stoppable;
            sendButton.disabled = busy || !promptInput?.value.trim();
            sendButton.setAttribute('aria-busy', String(busy));
        }
        if (stopButton) {
            stopButton.hidden = !(busy && stoppable);
            stopButton.disabled = !(busy && stoppable);
        }
        newSessionButton && (newSessionButton.disabled = busy);
        resumeButton && (resumeButton.disabled = busy || !latestSessionId);
    }

    function syncSend() {
        if (sendButton && !busy) sendButton.disabled = !promptInput?.value.trim();
    }

    function resizePrompt() {
        if (!promptInput) return;
        promptInput.style.height = 'auto';
        promptInput.style.height = `${Math.min(160, Math.max(44, promptInput.scrollHeight))}px`;
        syncSend();
    }

    async function sendQuery(text) {
        if (busy || !text.trim()) return;
        const message = text.trim();
        expireVisibleApprovals();
        appendUser(message);
        const turn = createAssistantTurn();
        promptInput.value = '';
        resizePrompt();
        rememberSession(sessionId);
        const controller = new AbortController();
        const requestId = createId('rq');
        activeRequest = {controller, requestId, turn, sessionId};
        setBusy(true, {stoppable: true});
        announce(responseStatus, 'Media Agent 正在处理请求');
        try {
            const response = await fetch('/api/agent/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    message,
                    session_id: sessionId,
                    request_id: requestId,
                    stream: true,
                }),
                signal: controller.signal,
            });
            await readEventStream(response, (event) => {
                if (activeRequest?.requestId !== requestId) return;
                applyEvent(turn, event);
            });
            announce(responseStatus, 'Media Agent 已完成');
        } catch (error) {
            if (error?.name === 'AbortError') finalizeError(turn, '本次任务已停止。', {cancelled: true});
            else finalizeError(turn, error?.message || 'Agent 暂时不可用。');
            announce(responseStatus, error?.name === 'AbortError' ? '请求已停止' : '请求失败');
        } finally {
            if (activeRequest?.requestId === requestId) activeRequest = null;
            setBusy(false);
            resizePrompt();
            refreshSessions({quiet: true});
        }
    }

    async function stopActiveRequest() {
        const active = activeRequest;
        if (!active) return;
        stopButton.disabled = true;
        try {
            await Promise.race([
                fetchJSON('/api/agent/query/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: active.sessionId, request_id: active.requestId}),
                }),
                new Promise((resolve) => setTimeout(resolve, 1200)),
            ]);
        } catch (_) { /* stream abort remains authoritative for the browser */ }
        active.controller.abort();
    }

    async function confirmEffect(button) {
        if (busy) return;
        const card = button.closest('.agent-confirmation-card');
        const planId = button.dataset.effectConfirm || '';
        if (!card || !planId) return;
        const buttons = [...card.querySelectorAll('button')];
        buttons.forEach((item) => { item.disabled = true; });
        const actions = card.querySelector('.agent-confirmation-actions');
        const executing = element('div', 'agent-confirmation-executing');
        const mark = element('span', 'agent-confirmation-executing-mark');
        mark.append(icon('loader-circle'));
        const copy = element('span', 'agent-confirmation-executing-copy');
        copy.append(element('strong', '', '正在执行已确认计划'), element('small', '', '执行完成前不会接受另一项写操作。'));
        executing.append(mark, copy);
        actions?.replaceChildren(executing);
        renderIcons(card);
        setBusy(true);
        const controller = new AbortController();
        const requestId = createId('confirm');
        try {
            const response = await fetch('/api/agent/actions/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    plan_id: planId,
                    session_id: sessionId,
                    request_id: requestId,
                    stream: true,
                }),
                signal: controller.signal,
            });
            let result = {};
            let error = '';
            await readEventStream(response, (event) => {
                if (event.type === 'effect.completed') result = event.payload?.result || {};
                if (event.type === 'effect.failed' || event.type === 'turn.failed') {
                    error = event.payload?.message || error;
                }
            });
            replaceApprovalWithResult(card, error || publicSummary(result) || '操作已完成并通过写后校验。', {error: Boolean(error)});
        } catch (error) {
            replaceApprovalWithResult(card, error?.message || '确认执行失败，请重新查询状态。', {error: true});
        } finally {
            setBusy(false);
            refreshSessions({quiet: true});
        }
    }

    async function cancelEffect(button) {
        if (busy) return;
        const card = button.closest('.agent-confirmation-card');
        const planId = button.dataset.effectCancel || '';
        if (!card || !planId) return;
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
            replaceApprovalWithResult(card, error?.message || '暂时无法取消该计划。', {error: true});
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
        sessionList?.replaceChildren();
        const sessions = Array.isArray(items) ? items.filter((item) => SESSION_RE.test(String(item?.session_id || ''))) : [];
        if (sessionCount) sessionCount.textContent = String(sessions.length);
        latestSessionId = sessions[0]?.session_id || '';
        if (resumeButton) resumeButton.disabled = busy || !latestSessionId;
        if (!sessions.length) {
            const empty = element('div', 'agent-session-empty');
            empty.append(icon('message-circle-dashed'), element('span', '', '尚无已保存的对话'));
            sessionList?.append(empty);
            renderIcons(empty);
            return;
        }
        for (const item of sessions) {
            const row = element('div', `agent-session-item${item.session_id === sessionId ? ' is-active' : ''}`);
            row.dataset.sessionId = item.session_id;
            const open = element('button', 'agent-session-open');
            open.type = 'button';
            open.dataset.sessionOpen = item.session_id;
            open.append(element('strong', '', item.title || '新对话'), element('small', '', `${item.message_count || 0} 条消息${sessionTime(item.updated_at) ? ` · ${sessionTime(item.updated_at)}` : ''}`));
            const remove = element('button', 'agent-session-delete');
            remove.type = 'button';
            remove.dataset.sessionDelete = item.session_id;
            remove.setAttribute('aria-label', `删除会话 ${item.title || ''}`);
            remove.append(icon('trash-2'));
            row.append(open, remove);
            sessionList?.append(row);
        }
        renderIcons(sessionList);
    }

    async function refreshSessions({quiet = false} = {}) {
        historyController?.abort();
        const controller = new AbortController();
        historyController = controller;
        if (!quiet) sessionList?.setAttribute('aria-busy', 'true');
        try {
            const payload = await fetchJSON('/api/agent/sessions', {signal: controller.signal});
            if (historyController !== controller) return;
            renderSessionList(payload.sessions || []);
            announce(sessionStatus, '会话列表已更新');
        } catch (error) {
            if (error?.name !== 'AbortError' && !quiet) announce(sessionStatus, '会话列表加载失败');
        } finally {
            if (historyController === controller) {
                historyController = null;
                sessionList?.setAttribute('aria-busy', 'false');
            }
        }
    }

    function renderRecoveredApproval(approval) {
        if (!approval?.plan_id) return;
        const view = appendMessage('assistant', {recovered: true});
        view.body.append(buildApproval(approval));
    }

    async function loadSession(targetId, {closeHistory = true} = {}) {
        if (busy || !SESSION_RE.test(targetId)) return;
        const generation = ++sessionLoadGeneration;
        try {
            const payload = await fetchJSON(`/api/agent/sessions/${encodeURIComponent(targetId)}`);
            if (generation !== sessionLoadGeneration) return;
            rememberSession(targetId);
            transcript?.replaceChildren();
            for (const message of payload.messages || []) {
                if (message.role === 'user') appendUser(String(message.content || ''), {recovered: true});
                else if (message.role === 'assistant') {
                    const turn = createAssistantTurn({recovered: true});
                    finalizeAnswer(turn, String(message.content || ''));
                }
            }
            renderRecoveredApproval(payload.pending_approval);
            setConsoleEmpty(!transcript?.childElementCount);
            if (closeHistory) closeHistoryRail();
            refreshSessions({quiet: true});
        } catch (error) {
            announce(sessionStatus, error?.message || '会话加载失败');
        }
    }

    async function deleteSession(targetId) {
        if (busy || !SESSION_RE.test(targetId)) return;
        try {
            await fetchJSON(`/api/agent/sessions/${encodeURIComponent(targetId)}`, {method: 'DELETE'});
            if (targetId === sessionId) startNewSession();
            await refreshSessions();
        } catch (error) {
            announce(sessionStatus, error?.message || '会话删除失败');
        }
    }

    function startNewSession() {
        if (busy) return;
        ++sessionLoadGeneration;
        rememberSession(createId('session'));
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
        refreshSessions();
    }

    function closeHistoryRail() {
        if (!historyRail) return;
        if (typeof historyRail.close === 'function' && historyRail.open) historyRail.close();
        else historyRail.removeAttribute('open');
        historyButton?.setAttribute('aria-expanded', 'false');
    }

    function syncViewportHeight() {
        const height = window.visualViewport?.height || window.innerHeight;
        document.documentElement.style.setProperty('--agent-viewport-height', `${Math.round(height)}px`);
    }

    composer?.addEventListener('submit', (event) => {
        event.preventDefault();
        sendQuery(promptInput?.value || '');
    });
    promptInput?.addEventListener('input', resizePrompt);
    promptInput?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            composer?.requestSubmit();
        }
    });
    stopButton?.addEventListener('click', stopActiveRequest);
    newSessionButton?.addEventListener('click', startNewSession);
    resumeButton?.addEventListener('click', () => latestSessionId && loadSession(latestSessionId));
    historyButton?.addEventListener('click', openHistoryRail);
    historyRail?.addEventListener('cancel', (event) => {
        event.preventDefault();
        closeHistoryRail();
    });
    historyRail?.addEventListener('click', (event) => {
        if (event.target === historyRail || event.target.closest('[data-agent-history-close]')) closeHistoryRail();
    });
    sessionList?.addEventListener('click', (event) => {
        const open = event.target.closest('[data-session-open]');
        const remove = event.target.closest('[data-session-delete]');
        if (open) loadSession(open.dataset.sessionOpen || '');
        if (remove) deleteSession(remove.dataset.sessionDelete || '');
    });
    transcript?.addEventListener('click', (event) => {
        const confirm = event.target.closest('[data-effect-confirm]');
        const cancel = event.target.closest('[data-effect-cancel]');
        if (confirm) confirmEffect(confirm);
        if (cancel) cancelEffect(cancel);
    });
    page.addEventListener('click', (event) => {
        const draft = event.target.closest('[data-agent-draft]');
        if (!draft || busy) return;
        promptInput.value = draft.dataset.agentDraft || '';
        resizePrompt();
        promptInput.focus();
    });
    window.visualViewport?.addEventListener('resize', syncViewportHeight, {passive: true});
    window.addEventListener('resize', syncViewportHeight, {passive: true});

    syncViewportHeight();
    resizePrompt();
    setConsoleEmpty(true);
    renderIcons(page);
    refreshSessions({quiet: true}).then(() => {
        if (storedSessionId()) loadSession(sessionId, {closeHistory: false});
    });
})();
