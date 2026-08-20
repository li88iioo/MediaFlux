let loading = false;
let overviewRefreshQueued = false;
let overviewQueuedManual = false;
let overviewPromise = Promise.resolve(false);
let qbActionBusy = false;
let issueBatchBusy = false;
let logBatchBusy = false;
let downloadLogPage = 1;
let downloadIssuePage = 1;
let downloadLogRequestSerial = 0;
let downloadIssueRequestSerial = 0;
let hasLoadedDownloadLogs = false;
let hasLoadedDownloadIssues = false;
let currentQbTasks = [];
let currentIssueIds = [];
let currentLogIds = [];
const selectedQbHashes = new Set();
const selectedIssueIds = new Set();
const selectedLogIds = new Set();
const issueActionBusy = new Set();
const DOWNLOAD_LOG_PAGE_SIZE = 20;
const DOWNLOAD_ISSUE_PAGE_SIZE = 20;
const stateNames = {downloading:'下载中', stalledDL:'等待连接', stalledUP:'等待上传', uploading:'上传中', pausedDL:'已暂停', pausedUP:'已暂停', stoppedDL:'已停止', stoppedUP:'已停止', queuedDL:'排队中', queuedUP:'排队中', checkingDL:'校验中', checkingUP:'校验中', checkingResumeData:'检查恢复数据', moving:'移动文件中', forcedDL:'强制下载', forcedUP:'强制上传', metaDL:'获取元数据', forcedMetaDL:'强制获取元数据', error:'错误', missingFiles:'缺文件', unknown:'未知'};
const qbPausedStates = new Set(['pausedDL','pausedUP','stoppedDL','stoppedUP']);
const qbStoppableStates = new Set(['downloading','forcedDL','metaDL','forcedMetaDL','queuedDL','stalledDL','uploading','queuedUP','stalledUP','forcedUP']);
const qbTransientStates = new Set(['checkingDL','checkingUP','checkingResumeData','moving']);

function qbTaskControl(state) {
    if(qbPausedStates.has(state))return {action:'resume',label:'恢复',icon:'play',disabled:false};
    if(qbStoppableStates.has(state))return {action:'pause',label:'暂停',icon:'pause',disabled:false};
    if(qbTransientStates.has(state))return {action:'',label:'处理中',icon:'loader-circle',disabled:true};
    return {action:'',label:'不可操作',icon:'circle-slash-2',disabled:true};
}

function switchDlTab(tabName, syncUrl=true) {
    const isTasks = tabName === 'tasks';
    const isIssues = tabName === 'issues';
    const isLogs = tabName === 'logs';
    document.getElementById('tabTasksBtn').classList.toggle('active', isTasks);
    document.getElementById('tabIssuesBtn').classList.toggle('active', isIssues);
    document.getElementById('tabLogsBtn').classList.toggle('active', isLogs);
    document.getElementById('viewTasks').style.display = isTasks ? 'block' : 'none';
    document.getElementById('viewIssues').style.display = isIssues ? 'block' : 'none';
    document.getElementById('viewLogs').style.display = isLogs ? 'block' : 'none';
    document.getElementById('dlLogsFilters').style.display = isLogs ? 'grid' : 'none';
    if(isIssues&&!hasLoadedDownloadIssues)loadIssues(downloadIssuePage);
    if(isLogs&&!hasLoadedDownloadLogs)loadLogs(downloadLogPage);
    if(syncUrl){
        const url=new URL(window.location.href);
        if(isTasks)url.searchParams.delete('view');else url.searchParams.set('view',tabName);
        window.history.replaceState({},'',url);
    }
}

function api(path, opts={}) { return fetch(path, {headers:{'Content-Type':'application/json'}, ...opts}); }
async function readApiResponse(response) {
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.error||data.detail||'请求失败');
    return data;
}
function esc(v) { const d=document.createElement('div'); d.textContent=v==null?'':String(v); return d.innerHTML; }
function attr(v) { return esc(v).replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function bytes(n) { n=Number(n)||0; if(n<1024) return n+' B'; const units=['KB','MB','GB','TB']; let i=-1; do { n/=1024; i++; } while(n>=1024&&i<units.length-1); return n.toFixed(n>=100?0:n>=10?1:2)+' '+units[i]; }
function speed(n) { return bytes(n)+'/s'; }
function eta(n) { n=Number(n)||0; if(n<0||n>=8640000) return '未知'; if(n<60)return n+'秒'; const h=Math.floor(n/3600),m=Math.floor((n%3600)/60); return h?`${h}小时${m}分`:`${m}分钟`; }
function pct(n) { return Math.max(0,Math.min(100,(Number(n)||0)*100)); }
function setOnline(dot, online) { dot.classList.toggle('online', !!online); dot.classList.toggle('offline', !online); }

function parseEntryTitle(title) {
    if(!title) return { mainTitle: '-', tags: [] };
    const tagRegex = /\[(.*?)\]|【(.*?)】|\((.*?)\)/g;
    const tags = [];
    let match;
    while((match = tagRegex.exec(title)) !== null) {
        const val = (match[1] || match[2] || match[3] || '').trim();
        if(val && val.length <= 15 && !tags.includes(val)) {
            tags.push(val);
        }
    }
    let mainTitle = title.replace(/\[.*?\]|【.*?】|\(.*?\)/g, ' ').replace(/\s+/g, ' ').trim();
    if(!mainTitle) mainTitle = title;
    return { mainTitle, tags: tags.slice(0, 5) };
}

function currentQbHashes() {
    return currentQbTasks.map(task=>String(task.hash||'').trim().toLowerCase()).filter(Boolean);
}

function syncQbSelectionControls() {
    const hashes=currentQbHashes();
    const available=new Set(hashes);
    [...selectedQbHashes].forEach(hash=>{if(!available.has(hash))selectedQbHashes.delete(hash);});
    const count=selectedQbHashes.size;
    const selectAll=document.getElementById('qbSelectAll');
    selectAll.disabled=!hashes.length||qbActionBusy;
    selectAll.checked=Boolean(hashes.length)&&count===hashes.length;
    selectAll.indeterminate=count>0&&count<hashes.length;
    document.querySelector('.qb-select-all').classList.toggle('is-active',count>0);
    document.getElementById('qbSelectionLabel').textContent=count?`已选择 ${count} 项`:'选择任务';
    ['qbBulkResume','qbBulkPause','qbBulkDelete'].forEach(id=>{
        document.getElementById(id).disabled=!count||qbActionBusy;
    });
    document.querySelectorAll('[data-qb-select]').forEach(input=>{
        const selected=selectedQbHashes.has(String(input.value||'').toLowerCase());
        input.checked=selected;
        input.disabled=qbActionBusy;
        input.closest('.qb-task-row')?.classList.toggle('is-selected',selected);
    });
}

function syncIssueSelectionControls() {
    const available=new Set(currentIssueIds);
    [...selectedIssueIds].forEach(id=>{if(!available.has(id))selectedIssueIds.delete(id);});
    const selectedOnPage=currentIssueIds.filter(id=>selectedIssueIds.has(id)).length;
    const count=selectedIssueIds.size;
    const busy=issueBatchBusy||issueActionBusy.size>0;
    const selectAll=document.getElementById('issueSelectAll');
    selectAll.disabled=!currentIssueIds.length||busy;
    selectAll.checked=Boolean(currentIssueIds.length)&&selectedOnPage===currentIssueIds.length;
    selectAll.indeterminate=selectedOnPage>0&&selectedOnPage<currentIssueIds.length;
    const toolbar=document.getElementById('issueBatchToolbar');
    toolbar.setAttribute('aria-busy',issueBatchBusy?'true':'false');
    toolbar.querySelector('.download-batch-select-all').classList.toggle('is-active',count>0);
    document.getElementById('issueSelectionLabel').textContent=issueBatchBusy?`正在处理 ${count} 项…`:(count?`已选择 ${count} 项`:'选择待处理');
    ['issueBulkQb','issueBulkGuangya','issueBulkBoth','issueBulkClear'].forEach(id=>{
        document.getElementById(id).disabled=!count||busy;
    });
    document.querySelectorAll('[data-issue-select]').forEach(input=>{
        const id=Number(input.value)||0;
        const selected=selectedIssueIds.has(id);
        input.checked=selected;
        input.disabled=busy;
        input.closest('.download-batch-row')?.classList.toggle('is-selected',selected);
    });
}

function syncLogSelectionControls() {
    const available=new Set(currentLogIds);
    [...selectedLogIds].forEach(id=>{if(!available.has(id))selectedLogIds.delete(id);});
    const selectedOnPage=currentLogIds.filter(id=>selectedLogIds.has(id)).length;
    const count=selectedLogIds.size;
    const selectAll=document.getElementById('logSelectAll');
    selectAll.disabled=!currentLogIds.length||logBatchBusy;
    selectAll.checked=Boolean(currentLogIds.length)&&selectedOnPage===currentLogIds.length;
    selectAll.indeterminate=selectedOnPage>0&&selectedOnPage<currentLogIds.length;
    const toolbar=document.getElementById('logBatchToolbar');
    toolbar.setAttribute('aria-busy',logBatchBusy?'true':'false');
    toolbar.querySelector('.download-batch-select-all').classList.toggle('is-active',count>0);
    document.getElementById('logSelectionLabel').textContent=logBatchBusy?`正在清理 ${count} 项…`:(count?`已选择 ${count} 项`:'选择日志');
    ['logBulkClear'].forEach(id=>{
        document.getElementById(id).disabled=!count||logBatchBusy;
    });
    document.querySelectorAll('[data-log-select]').forEach(input=>{
        const id=Number(input.value)||0;
        const selected=selectedLogIds.has(id);
        input.checked=selected;
        input.disabled=logBatchBusy;
        input.closest('.download-batch-row')?.classList.toggle('is-selected',selected);
    });
}

function renderQb(qb) {
    const incomingTasks=Array.isArray(qb.tasks)?qb.tasks:[];
    const keepPreviousTasks=qb.error_code==='connection_failed'&&currentQbTasks.length>0&&!incomingTasks.length;
    if(!keepPreviousTasks)currentQbTasks=incomingTasks;
    const taskCount=currentQbTasks.length;
    const qbCountEl=document.getElementById('qbCount');
    if(window.MFAnim && qbCountEl) window.MFAnim.countUp(qbCountEl, taskCount);
    else if(qbCountEl) qbCountEl.textContent = taskCount;
    document.getElementById('tabTasksBadge').textContent = taskCount;
    document.getElementById('qbStatus').textContent = qb.online ? (qb.version?.app||'在线') : (qb.error||'离线');
    setOnline(document.getElementById('qbDot'), qb.online);
    if(!qb.online) { document.getElementById('dlSpeed').textContent='—'; document.getElementById('upSpeed').textContent='上传 —'; }
    if(qb.transfer) { document.getElementById('dlSpeed').textContent=speed(qb.transfer.dl_info_speed); document.getElementById('upSpeed').textContent='上传 '+speed(qb.transfer.up_info_speed); }
    const body=document.getElementById('qbList');
    if(!taskCount) {
        selectedQbHashes.clear();
        if(qb.error_code==='not_configured') { body.innerHTML='<tr><td colspan="4"><div class="qb-empty-state"><strong>未连接到 qBittorrent</strong><span>配置下载器后即可查看实时任务和传输状态。</span><a class="jump-btn" href="/settings#downloads">前往 qB 配置</a></div></td></tr>'; syncQbSelectionControls(); return; }
        if(qb.error_code==='connection_failed') { body.innerHTML='<tr><td colspan="4"><div class="qb-empty-state"><strong>连接失败，请检查地址、认证信息和网络</strong><span>确认 qB WebUI 可访问后重试。</span><a class="jump-btn" href="/settings#downloads">前往 qB 配置</a></div></td></tr>'; syncQbSelectionControls(); return; }
        body.innerHTML=`<tr><td colspan="4" class="table-empty">${esc(qb.error||'暂无 qB 任务')}</td></tr>`; syncQbSelectionControls(); return;
    }
    const available=new Set(currentQbHashes());
    [...selectedQbHashes].forEach(hash=>{if(!available.has(hash))selectedQbHashes.delete(hash);});
    body.innerHTML=currentQbTasks.map(t=>{
        const hash=String(t.hash||'').trim().toLowerCase();
        const selected=selectedQbHashes.has(hash);
        const p=pct(t.progress);
        const control=qbTaskControl(t.state);
        const stateClass = qbPausedStates.has(t.state) ? 'paused' : ((t.state === 'error'||t.state === 'missingFiles') ? 'failed' : ((qbStoppableStates.has(t.state)||qbTransientStates.has(t.state)) ? 'running' : ''));
        const controlAttrs=control.disabled?'disabled aria-disabled="true"':`onclick="qbAction('${control.action}','${attr(hash)}')"`;
        const parsed = parseEntryTitle(t.name);

        return `<tr class="qb-task-row${selected?' is-selected':''}" data-qb-hash="${attr(hash)}">
            <td class="dl-task-title-cell">
                <div class="qb-task-title-layout">
                    <label class="qb-task-check" title="选择任务">
                        <input type="checkbox" data-qb-select value="${attr(hash)}" aria-label="选择 ${attr(t.name||'任务')}" ${selected?'checked':''}>
                    </label>
                    <div class="qb-task-copy">
                        <div class="dl-mobile-header-row">
                            <span class="status-pill ${stateClass}">${esc(stateNames[t.state]||t.state||'未知')}</span>
                            <div style="display:inline-flex;gap:4px;">
                                <button class="rss-btn" type="button" style="padding:3px 8px;width:76px;height:26px;font-size:11px;justify-content:center;" title="${control.label}" ${controlAttrs}>
                                    <i data-lucide="${control.icon}" style="width:13px;height:13px;"></i><span>${control.label}</span>
                                </button>
                                <button class="rss-btn is-danger" type="button" style="padding:3px 7px;height:26px;" title="仅移除任务，不删除文件" onclick="qbDelete('${attr(hash)}')">
                                    <i data-lucide="trash-2" style="width:13px;height:13px;"></i>
                                </button>
                            </div>
                        </div>
                        <div class="dl-task-main-title" title="${attr(t.name)}">${esc(parsed.mainTitle)}</div>
                        <div class="dl-task-tags">
                            ${parsed.tags.map(tag=>`<span class="dl-task-tag">${esc(tag)}</span>`).join('')}
                            ${t.save_path ? `<span class="dl-task-path-text" title="${attr(t.save_path)}"><i data-lucide="folder"></i>${esc(t.category ? '['+t.category+'] ' : '')}${esc(t.save_path)}</span>` : ''}
                        </div>
                    </div>
                </div>
            </td>
            <td class="progress-cell" style="min-width: 180px;">
                <div class="dl-progress-bar-wrap">
                    <div class="dl-progress-track">
                        <div class="dl-progress-fill ${stateClass}" style="width:${p}%"></div>
                    </div>
                    <div class="dl-progress-meta">
                        <span style="font-weight:600;color:var(--text-primary);">${p.toFixed(1)}%</span>
                        <span>${bytes(t.downloaded)} / ${bytes(t.size)}</span>
                        <span>↓ ${speed(t.dlspeed)}</span>
                        <span>ETA ${eta(t.eta)}</span>
                    </div>
                </div>
            </td>
            <td class="desktop-only-cell">
                <span class="status-pill ${stateClass}">${esc(stateNames[t.state]||t.state||'未知')}</span>
            </td>
            <td class="action-cell desktop-only-cell" style="text-align:right;">
                <div style="display:inline-flex;gap:4px;justify-content:flex-end;">
                    <button class="rss-btn" type="button" style="padding:3px 8px;width:76px;height:26px;font-size:11px;justify-content:center;" title="${control.label}" ${controlAttrs}>
                        <i data-lucide="${control.icon}" style="width:13px;height:13px;"></i><span>${control.label}</span>
                    </button>
                    <button class="rss-btn is-danger" type="button" style="padding:3px 7px;height:26px;" title="仅移除任务，不删除文件" onclick="qbDelete('${attr(hash)}')">
                        <i data-lucide="trash-2" style="width:13px;height:13px;"></i>
                    </button>
                </div>
            </td>
        </tr>`;
    }).join('');
    syncQbSelectionControls();
    window.renderLucideIcons?.(body);
}

function downloadLogSourceSvg(key) {
    if(key==='qb'){
        return `<svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
            <circle cx="12" cy="12" r="10" fill="currentColor"/>
            <circle cx="8.4" cy="11.3" r="2.8" fill="none" stroke="#fff" stroke-width="1.7"/>
            <path d="m10.4 13.4 1.6 1.6" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round"/>
            <path d="M13.8 7.8v8.4m0-8.4h2.2c1.3 0 2.1.7 2.1 1.8s-.8 1.8-2.1 1.8h-2.2m0 0h2.5c1.3 0 2.1.9 2.1 2.3 0 1.5-.8 2.5-2.1 2.5h-2.5" fill="none" stroke="#fff" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;
    }
    if(key==='guangya'){
        return `<svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
            <circle cx="12" cy="12" r="10" fill="currentColor"/>
            <path d="M6.1 13.1c0-3.2 2.4-5.7 5.5-5.7 2.5 0 4.6 1.6 5.3 3.9h.9c1.2 0 2 .6 2 1.5 0 .6-.4 1.1-1.1 1.4l-2.2 1c-.9 2-2.7 3.3-4.9 3.3-3.1 0-5.5-2.3-5.5-5.4Z" fill="#fff"/>
            <path d="M16.2 11.6h2.5c.8 0 1.3.4 1.3 1s-.4 1-1.1 1.3l-2.6 1.2c.3-1.2.3-2.3-.1-3.5Z" fill="#ffd45c"/>
            <circle cx="13.2" cy="10.5" r=".82" fill="currentColor"/>
            <path d="M6.3 4.4v2.7M5 5.75h2.7" fill="none" stroke="#fff" stroke-width="1.35" stroke-linecap="round"/>
        </svg>`;
    }
    return `<svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <circle cx="12" cy="12" r="10" fill="currentColor" opacity=".14"/>
        <path d="M7.5 8.5h9v7h-9zM9 6.5h6M9.5 17.5h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
}

function downloadLogSourceHtml(source) {
    const key=String(source||'').trim().toLowerCase();
    const item=key==='qb'
        ? {label:'qBittorrent',tone:'is-qb'}
        : key==='guangya'
            ? {label:'光鸭',tone:'is-guangya'}
            : {label:String(source||'未知来源'),tone:'is-other'};
    return `<span class="source-label download-log-source-badge ${item.tone}">
        <span class="download-log-source-icon">${downloadLogSourceSvg(key)}</span>
        <span class="download-log-source-name">${esc(item.label)}</span>
    </span>`;
}

function downloadLogTarget(path) {
    const raw=String(path||'').trim();
    const normalized=raw.toLowerCase();
    if(normalized==='[magnet]'||normalized.startsWith('magnet:')){
        return {icon:'magnet',label:'磁力链接',detail:'MAGNET · 已脱敏',title:'磁力下载来源；哈希与 tracker 已隐藏'};
    }
    if(normalized==='[https]'||normalized.startsWith('https://')){
        return {icon:'link-2',label:'种子直链',detail:'HTTPS · 已脱敏',title:'通过 HTTPS 种子地址提交；详细地址已隐藏'};
    }
    if(normalized==='[http]'||normalized.startsWith('http://')){
        return {icon:'link-2',label:'种子直链',detail:'HTTP · 已脱敏',title:'通过 HTTP 种子地址提交；详细地址已隐藏'};
    }
    if(normalized==='[torrent]'||normalized.endsWith('.torrent')){
        return {icon:'file-down',label:'种子文件',detail:'TORRENT',title:'通过种子文件提交'};
    }
    if(!raw){
        return {icon:'minus',label:'未记录',detail:'无来源信息',title:'没有记录下载来源'};
    }
    return {icon:'file-down',label:'下载来源',detail:'已安全隐藏',title:'下载来源已记录，详细内容不在列表中展示'};
}

function downloadLogTargetHtml(path) {
    const target=downloadLogTarget(path);
    return `<div class="download-log-target" title="${attr(target.title)}">
        <span class="download-log-target-icon" aria-hidden="true"><i data-lucide="${target.icon}"></i></span>
        <span class="download-log-target-copy">
            <span class="download-log-target-label">${esc(target.label)}</span>
            <span class="download-log-target-detail">${esc(target.detail)}</span>
        </span>
    </div>`;
}

function renderLogs(data) {
    const rows=data.items||[]; const page=Number(data.page)||1; const pages=Number(data.pages)||0; const total=Number(data.total)||0;
    downloadLogPage=page;
    currentLogIds=rows.map(row=>Number(row.id)||0).filter(Boolean);
    const logCountEl=document.getElementById('logCount');
    if(window.MFAnim && logCountEl) window.MFAnim.countUp(logCountEl, total);
    else if(logCountEl) logCountEl.textContent=total;
    const logBadgeEl=document.getElementById('tabLogsBadge');
    if(window.MFAnim && logBadgeEl) window.MFAnim.countUp(logBadgeEl, total);
    else if(logBadgeEl) logBadgeEl.textContent=total;
    document.getElementById('downloadLogPageInfo').textContent=total?`共 ${total} 条 · 第 ${page} / ${pages} 页`:'共 0 条';
    document.getElementById('downloadLogPrev').disabled=page<=1;
    document.getElementById('downloadLogNext').disabled=!pages||page>=pages;
    const body=document.getElementById('logList');
    if(!rows.length){body.innerHTML='<tr><td colspan="5" class="table-empty">暂无下载日志</td></tr>';syncLogSelectionControls();return;}
    const statuses={success:['成功','done'],existing:['已存在','done'],failed:['失败','failed'],submitted:['已提交','running'],downloading:['下载中','running'],unverified:['待确认','paused'],outcome_unknown:['结果待确认','paused']};
    body.innerHTML=rows.map(r=>{
        const id=Number(r.id)||0;
        const selected=selectedLogIds.has(id);
        const st=statuses[r.status]||[r.status,''];
        const meta=[r.backend_task_id?`任务 #${r.backend_task_id}`:'',r.error||''].filter(Boolean).join(' · ');
        const parsed = parseEntryTitle(r.title);

        return `<tr class="download-batch-row download-log-row${selected?' is-selected':''}" data-log-id="${id}">
            <td class="download-log-source-cell">
                <div class="download-log-source-layout">
                    <label class="download-row-check" title="选择下载日志">
                        <input type="checkbox" data-log-select value="${id}" aria-label="选择日志 ${attr(r.title||id)}" ${selected?'checked':''}>
                    </label>
                    ${downloadLogSourceHtml(r.source)}
                </div>
                <span class="status-pill ${st[1]} download-log-mobile-status">${esc(st[0])}</span>
            </td>
            <td class="dl-task-title-cell download-log-title-cell">
                <div class="download-log-title-copy">
                    <div class="dl-task-main-title" title="${attr(r.title)}">${esc(parsed.mainTitle||'-')}</div>
                    ${parsed.tags.length ? `<div class="dl-task-tags">${parsed.tags.map(t=>`<span class="dl-task-tag">${esc(t)}</span>`).join('')}</div>` : ''}
                    ${meta ? `<div style="font-size:10px;color:var(--text-muted);margin-top:2px;">${esc(meta)}</div>` : ''}
                    <div class="download-log-mobile-details">
                        ${downloadLogTargetHtml(r.path)}
                        <span class="download-log-mobile-time">${esc(r.created_at||'-')}</span>
                    </div>
                </div>
            </td>
            <td class="desktop-only-cell"><span class="status-pill ${st[1]}">${esc(st[0])}</span></td>
            <td class="download-log-target-cell desktop-only-cell">${downloadLogTargetHtml(r.path)}</td>
            <td class="download-log-time desktop-only-cell">${esc(r.created_at||'-')}</td>
        </tr>`;
    }).join('');
    syncLogSelectionControls();
    window.renderLucideIcons?.(body);
}

function renderIssues(data) {
    const rows=data.items||[]; const page=Number(data.page)||1; const pages=Number(data.pages)||0; const total=Number(data.total)||0;
    downloadIssuePage=page;
    currentIssueIds=rows.map(row=>Number(row.id)||0).filter(Boolean);
    const issueBadgeEl=document.getElementById('tabIssuesBadge');
    if(window.MFAnim && issueBadgeEl) window.MFAnim.countUp(issueBadgeEl, total);
    else if(issueBadgeEl) issueBadgeEl.textContent=total;
    document.getElementById('downloadIssuePageInfo').textContent=total?`共 ${total} 条 · 第 ${page} / ${pages} 页`:'共 0 条';
    document.getElementById('downloadIssuePrev').disabled=page<=1;
    document.getElementById('downloadIssueNext').disabled=!pages||page>=pages;
    const body=document.getElementById('issueList');
    if(!rows.length){body.innerHTML='<tr><td colspan="4" class="table-empty">当前没有需要处理的下载请求</td></tr>';syncIssueSelectionControls();return;}
    const origins={telegram:'Telegram',rss:'RSS',web:'网页',indexer:'资源搜索'};
    const kinds={magnet:'磁力',torrent:'种子',ed2k:'ED2K',http:'HTTP',share:'分享转存'};
    body.innerHTML=rows.map(r=>{
        const id=Number(r.id)||0;
        const selected=selectedIssueIds.has(id);
        const parsed=parseEntryTitle(r.title);
        const stages=Array.isArray(r.stages)?r.stages:[];
        const issues=stages.map(stage=>`<div class="download-attention-issue"><span class="download-attention-stage">${esc(stage.label||stage.key||'异常')}</span><span class="download-attention-message">${esc(stage.error||'状态异常，请核对相关服务与运行日志。')}</span></div>`).join('')||'<div class="download-attention-issue"><span class="download-attention-message">状态异常，请核对相关服务与运行日志。</span></div>';
        const meta=[`请求 #${r.id}`,origins[r.origin]||r.origin,kinds[r.kind]||r.kind].filter(Boolean).join(' · ');
        const capabilities=r.retry_targets||{};
        const rowBusy=issueActionBusy.has(id);
        const busy=issueBatchBusy||rowBusy;
        const actionButton=(target,label,icon)=>{
            const capability=capabilities[target]||{};
            const enabled=Boolean(capability.enabled);
            const reason=capability.reason||`无法重新下载到${label}`;
            return `<button type="button" class="rss-btn download-attention-action is-${target}" data-issue-resubmit="${target}" data-request-id="${r.id}" data-enabled="${enabled?'1':'0'}" onclick="resubmitIssue(${r.id},'${target}')" ${busy||!enabled?'disabled':''} aria-busy="${rowBusy?'true':'false'}" title="${attr(enabled?`重新下载到${label}`:reason)}"><i data-lucide="${icon}"></i><span>${esc(label)}</span></button>`;
        };
        return `<tr class="download-batch-row download-issue-row${selected?' is-selected':''}" data-issue-id="${id}">
            <td class="dl-task-title-cell">
                <div class="download-select-layout">
                    <label class="download-row-check" title="选择待处理请求">
                        <input type="checkbox" data-issue-select value="${id}" aria-label="选择待处理请求 ${attr(r.title||id)}" ${selected?'checked':''}>
                    </label>
                    <div class="download-select-copy">
                        <div class="dl-task-main-title" title="${attr(r.title)}">${esc(parsed.mainTitle||r.title||'未命名下载请求')}</div>
                        ${parsed.tags.length?`<div class="dl-task-tags">${parsed.tags.map(tag=>`<span class="dl-task-tag">${esc(tag)}</span>`).join('')}</div>`:''}
                        <div class="download-attention-meta">${esc(meta)}</div>
                    </div>
                </div>
            </td>
            <td><div class="download-attention-issues">${issues}</div></td>
            <td class="text-muted download-attention-time">${esc(r.updated_at||r.created_at||'-')}</td>
            <td class="download-attention-action-cell">
                <div class="download-attention-operation" aria-label="待处理操作">
                    <div class="download-attention-actions" role="group" aria-label="重新下载目标">
                        ${actionButton('guangya','光鸭','cloud-download')}
                        ${actionButton('qb','qB','download')}
                        ${actionButton('both','两者','copy-plus')}
                    </div>
                    <button type="button" class="rss-btn download-attention-clear" data-issue-clear data-request-id="${r.id}" onclick="clearIssue(${r.id})" ${busy?'disabled':''} aria-busy="${rowBusy?'true':'false'}" title="移出待处理；不会删除文件、任务或日志"><i data-lucide="x"></i><span>清除</span></button>
                </div>
            </td>
        </tr>`;
    }).join('');
    syncIssueSelectionControls();
    window.renderLucideIcons?.(body);
}

function loadOverview(manual=false) {
    overviewRefreshQueued=true;
    overviewQueuedManual=overviewQueuedManual||manual;
    if(loading||qbActionBusy)return overviewPromise;
    loading=true;
    overviewPromise=(async()=>{
        let succeeded=false;
        try{
            while(overviewRefreshQueued&&!qbActionBusy){
                const currentManual=overviewQueuedManual;
                overviewRefreshQueued=false;
                overviewQueuedManual=false;
                try{
                    const data=await api('/api/downloads/overview').then(readApiResponse);
                    renderQb(data.qb||{tasks:[],error:'读取失败'});
                    document.getElementById('lastRefresh').textContent=currentManual?'刚刚更新':'更新于 '+new Date().toLocaleTimeString();
                    succeeded=true;
                }catch(error){
                    document.getElementById('qbStatus').textContent='请求失败';
                    if(currentManual&&window.appAlert)appAlert({type:'error',title:'刷新失败',message:error.message||'无法读取下载器状态。'});
                }
            }
        }finally{
            loading=false;
        }
        return succeeded;
    })();
    return overviewPromise;
}

function loadLogs(page=1){
    const requestedPage=Math.max(1,Number(page)||1);
    const requestSerial=++downloadLogRequestSerial;
    const s=document.getElementById('logSource').value,st=document.getElementById('logStatus').value,k=document.getElementById('logKeyword').value.trim();
    const q=[`page=${requestedPage}`,`page_size=${DOWNLOAD_LOG_PAGE_SIZE}`];
    if(s)q.push('source='+encodeURIComponent(s));if(st)q.push('status='+encodeURIComponent(st));if(k)q.push('keyword='+encodeURIComponent(k));
    return api('/api/downloads/logs?'+q.join('&')).then(readApiResponse).then(data=>{
        if(requestSerial!==downloadLogRequestSerial)return false;
        renderLogs(data);hasLoadedDownloadLogs=true;return true;
    }).catch(error=>{
        if(requestSerial!==downloadLogRequestSerial)return false;
        if(!hasLoadedDownloadLogs){document.getElementById('logList').innerHTML='<tr><td colspan="5" class="table-empty">读取失败</td></tr>';}
        if(window.appAlert)appAlert({type:'error',title:'下载日志读取失败',message:error.message||'请稍后重试。'});
        return false;
    });
}
function loadIssues(page=1){
    const requestedPage=Math.max(1,Number(page)||1);
    const requestSerial=++downloadIssueRequestSerial;
    return api(`/api/downloads/issues?page=${requestedPage}&page_size=${DOWNLOAD_ISSUE_PAGE_SIZE}`).then(readApiResponse).then(data=>{
        if(requestSerial!==downloadIssueRequestSerial)return false;
        renderIssues(data);hasLoadedDownloadIssues=true;return true;
    }).catch(error=>{
        if(requestSerial!==downloadIssueRequestSerial)return false;
        if(!hasLoadedDownloadIssues){document.getElementById('issueList').innerHTML='<tr><td colspan="4" class="table-empty">读取失败</td></tr>';}
        if(window.appAlert)appAlert({type:'error',title:'待处理请求读取失败',message:error.message||'请稍后重试。'});
        return false;
    });
}

function syncIssueActionButtons(requestId){
    const rowBusy=issueActionBusy.has(Number(requestId));
    const disabled=issueBatchBusy||rowBusy;
    document.querySelectorAll(`[data-request-id="${Number(requestId)}"]`).forEach(button=>{
        const enabled=!button.hasAttribute('data-issue-resubmit')||button.dataset.enabled==='1';
        button.disabled=disabled||!enabled;
        button.setAttribute('aria-busy',rowBusy?'true':'false');
        button.classList.toggle('is-busy',rowBusy);
    });
}

async function resubmitIssue(requestId,target){
    const id=Number(requestId)||0;
    if(!id||issueBatchBusy||issueActionBusy.has(id))return false;
    const targetNames={guangya:'光鸭',qb:'qBittorrent',both:'qBittorrent 与光鸭'};
    const targetName=targetNames[target]||'下载服务';
    const confirmed=await appConfirm({
        title:'重新下载资源',
        message:`将使用原请求保存的资源重新提交到${targetName}，旧记录会保留用于审计。`,
        confirmText:`重新下载到${target==='both'?'两者':target==='qb'?'qB':'光鸭'}`,
    });
    if(!confirmed)return false;

    issueActionBusy.add(id);
    syncIssueActionButtons(id);
    syncIssueSelectionControls();
    let receivedResponse=false;
    try{
        const response=await api(`/api/downloads/issues/${id}/resubmit`,{method:'POST',body:JSON.stringify({target})});
        receivedResponse=true;
        const data=await response.json().catch(()=>({}));
        if(!response.ok||!data.ok)throw new Error(data.error||'重新提交失败');
        const succeeded=(data.succeeded||[]).map(item=>item==='qb'?'qB':item==='guangya'?'光鸭':item);
        const failed=(data.failed||[]).map(item=>item==='qb'?'qB':item==='guangya'?'光鸭':item);
        const partial=failed.length>0;
        appAlert({
            type:partial?'warning':'success',
            title:partial?'下载任务部分提交成功':'下载任务已重新提交',
            message:`新请求 #${data.request_id}${succeeded.length?` · 已提交到 ${succeeded.join('、')}`:''}${failed.length?` · ${failed.join('、')} 提交失败，请查看新的待处理记录`:''}`,
        });
        await Promise.all([loadIssues(downloadIssuePage),loadOverview(true)]);
        return true;
    }catch(error){
        appAlert({
            type:'error',
            title:'重新下载失败',
            message:receivedResponse?(error.message||'下载服务拒绝了本次提交'):'提交状态未知，请先刷新待处理列表核对，不要立即重复提交。',
        });
        return false;
    }finally{
        issueActionBusy.delete(id);
        syncIssueActionButtons(id);
        syncIssueSelectionControls();
    }
}

async function clearIssue(requestId){
    const id=Number(requestId)||0;
    if(!id||issueBatchBusy||issueActionBusy.has(id))return false;
    const confirmed=await appConfirm({
        title:'移出待处理',
        message:'仅将本记录移出待处理；不会删除下载任务、文件或日志，原状态和错误会保留审计。',
        confirmText:'移出待处理',
    });
    if(!confirmed)return false;

    const row=document.querySelector(`.download-issue-row[data-issue-id="${id}"]`);
    if(row && window.MFAnim){
        window.MFAnim.slideOutAndCollapse(row);
    }

    issueActionBusy.add(id);
    syncIssueActionButtons(id);
    syncIssueSelectionControls();
    let receivedResponse=false;
    try{
        const response=await api(`/api/downloads/issues/${id}/clear`,{method:'POST'});
        receivedResponse=true;
        const data=await response.json().catch(()=>({}));
        if(!response.ok||!data.ok)throw new Error(data.error||'清除失败');
        appAlert({
            type:'success',
            title:'已移出待处理',
            message:'原请求和异常记录已保留，未删除下载文件、下载任务或下载日志。',
        });
        await loadIssues(downloadIssuePage);
        return true;
    }catch(error){
        appAlert({
            type:'error',
            title:'移出待处理失败',
            message:receivedResponse?(error.message||'请求未被清除'):'操作状态未知，请刷新待处理列表核对。',
        });
        return false;
    }finally{
        issueActionBusy.delete(id);
        syncIssueActionButtons(id);
        syncIssueSelectionControls();
    }
}

function syncVisibleIssueActions(){currentIssueIds.forEach(syncIssueActionButtons);}

async function resubmitIssuesBatch(target){
    const requestIds=[...selectedIssueIds];
    if(issueBatchBusy||issueActionBusy.size||!requestIds.length)return false;
    const targetNames={qb:'qBittorrent',guangya:'光鸭',both:'qBittorrent 与光鸭'};
    const shortNames={qb:'qB',guangya:'光鸭',both:'两者'};
    const confirmed=await appConfirm({
        title:`批量重新下载到${shortNames[target]||'下载服务'}`,
        message:`将选择的 ${requestIds.length} 条待处理请求逐条重新提交到${targetNames[target]||'下载服务'}。不支持的记录会跳过，旧记录继续保留审计。`,
        confirmText:`提交 ${requestIds.length} 条`,
    });
    if(!confirmed)return false;

    issueBatchBusy=true;
    syncIssueSelectionControls();
    syncVisibleIssueActions();
    let receivedResponse=false;
    try{
        const response=await api('/api/downloads/issues/batch/resubmit',{method:'POST',body:JSON.stringify({request_ids:requestIds,target})});
        receivedResponse=true;
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'批量重新提交失败');
        selectedIssueIds.clear();
        const completed=Number(data.succeeded||0)+Number(data.partial||0);
        const failed=Number(data.failed||0);
        const skipped=Number(data.skipped||0);
        const type=failed?(completed?'warning':'error'):((Number(data.partial||0)||skipped)?'warning':'success');
        appAlert({
            type,
            title:type==='success'?'批量重新提交完成':type==='error'?'批量重新提交失败':'批量重新提交已处理',
            message:`成功 ${Number(data.succeeded||0)} 条 · 部分成功 ${Number(data.partial||0)} 条 · 跳过 ${skipped} 条 · 失败 ${failed} 条`,
        });
        await Promise.all([loadIssues(downloadIssuePage),loadOverview(true),loadLogs(downloadLogPage)]);
        return completed>0;
    }catch(error){
        appAlert({
            type:'error',
            title:'批量重新提交失败',
            message:receivedResponse?(error.message||'下载服务拒绝了本次批量提交'):'提交状态未知，请刷新待处理列表核对，不要立即重复提交。',
        });
        return false;
    }finally{
        issueBatchBusy=false;
        syncIssueSelectionControls();
        syncVisibleIssueActions();
    }
}

async function clearIssuesBatch(){
    const requestIds=[...selectedIssueIds];
    if(issueBatchBusy||issueActionBusy.size||!requestIds.length)return false;
    const confirmed=await appConfirm({
        title:`清理 ${requestIds.length} 条待处理`,
        message:'仅将选中的记录移出待处理；不会删除下载任务、实际文件、原请求或下载日志。',
        confirmText:`清理 ${requestIds.length} 条`,
        danger:true,
    });
    if(!confirmed)return false;

    issueBatchBusy=true;
    syncIssueSelectionControls();
    syncVisibleIssueActions();
    let receivedResponse=false;
    try{
        const response=await api('/api/downloads/issues/batch/clear',{method:'POST',body:JSON.stringify({request_ids:requestIds})});
        receivedResponse=true;
        const data=await response.json().catch(()=>({}));
        if(!response.ok||!data.ok)throw new Error(data.error||'批量清理失败');
        selectedIssueIds.clear();
        appAlert({
            type:Number(data.skipped||0)?'warning':'success',
            title:'待处理批量清理完成',
            message:`已移出 ${Number(data.cleared||0)} 条 · 已清理 ${Number(data.already_cleared||0)} 条 · 跳过 ${Number(data.skipped||0)} 条；任务、文件和日志均未删除。`,
        });
        await loadIssues(downloadIssuePage);
        return true;
    }catch(error){
        appAlert({
            type:'error',
            title:'待处理批量清理失败',
            message:receivedResponse?(error.message||'部分记录未能清理'):'操作状态未知，请刷新待处理列表核对。',
        });
        return false;
    }finally{
        issueBatchBusy=false;
        syncIssueSelectionControls();
        syncVisibleIssueActions();
    }
}

async function clearLogsBatch(){
    const logIds=[...selectedLogIds];
    if(logBatchBusy||!logIds.length)return false;
    const confirmed=await appConfirm({
        title:`删除 ${logIds.length} 条下载日志`,
        message:'将永久删除选中的日志记录；不会停止 qB/光鸭任务，也不会删除已下载文件或下载请求。',
        confirmText:`删除 ${logIds.length} 条日志`,
        danger:true,
    });
    if(!confirmed)return false;

    logBatchBusy=true;
    syncLogSelectionControls();
    let receivedResponse=false;
    try{
        const response=await api('/api/downloads/logs/batch/clear',{method:'POST',body:JSON.stringify({log_ids:logIds})});
        receivedResponse=true;
        const data=await response.json().catch(()=>({}));
        if(!response.ok||!data.ok)throw new Error(data.error||'日志清理失败');
        selectedLogIds.clear();
        appAlert({
            type:Number(data.missing||0)?'warning':'success',
            title:'下载日志清理完成',
            message:`已删除 ${Number(data.deleted||0)} 条${Number(data.missing||0)?` · ${Number(data.missing||0)} 条已不存在`:''}；下载任务和文件未受影响。`,
        });
        await loadLogs(downloadLogPage);
        return true;
    }catch(error){
        appAlert({
            type:'error',
            title:'下载日志清理失败',
            message:receivedResponse?(error.message||'日志未能删除'):'操作状态未知，请刷新下载日志核对。',
        });
        return false;
    }finally{
        logBatchBusy=false;
        syncLogSelectionControls();
    }
}

async function runQbAction(action, hashes, clearSelection=false) {
    const unique=[...new Set((hashes||[]).map(hash=>String(hash||'').trim().toLowerCase()).filter(Boolean))];
    if(qbActionBusy||!unique.length)return false;
    qbActionBusy=true;
    syncQbSelectionControls();
    let succeeded=false;
    try {
        const response=await api('/api/downloads/qb/'+action,{method:'POST',body:JSON.stringify({hashes:unique})});
        const data=await response.json().catch(()=>({}));
        if(!response.ok||data.error)throw new Error(data.error||'任务操作失败');
        if(clearSelection)selectedQbHashes.clear();
        succeeded=true;
    } catch(error) {
        appAlert({type:'error',title:'qB 操作失败',message:error.message||'无法连接下载服务，请检查 qBittorrent 配置和网络状态。'});
    } finally {
        qbActionBusy=false;
        syncQbSelectionControls();
    }
    if(succeeded)await loadOverview(true);
    return succeeded;
}

function qbAction(action, hash){return runQbAction(action,[hash],false);}
async function qbDelete(hash){
    const confirmed=await appConfirm({title:'移除 qB 任务',message:'只从 qBittorrent 移除任务，不删除已经下载的文件。',confirmText:'移除任务',danger:true});
    if(!confirmed)return false;
    const row=document.querySelector(`.qb-task-row[data-qb-hash="${hash}"]`);
    if(row && window.MFAnim){
        window.MFAnim.slideOutAndCollapse(row);
    }
    return runQbAction('delete',[hash],false);
}
async function qbBulkAction(action){
    const hashes=[...selectedQbHashes];
    if(!hashes.length)return false;
    if(action==='delete'){
        const confirmed=await appConfirm({title:`移除 ${hashes.length} 个 qB 任务`,message:'只从 qBittorrent 移除任务，不删除已经下载的文件。',confirmText:`移除 ${hashes.length} 个任务`,danger:true});
        if(!confirmed)return false;
    }
    return runQbAction(action,hashes,true);
}

document.getElementById('qbList').addEventListener('change',event=>{
    const input=event.target.closest('[data-qb-select]');
    if(!input)return;
    const hash=String(input.value||'').toLowerCase();
    if(input.checked)selectedQbHashes.add(hash);else selectedQbHashes.delete(hash);
    syncQbSelectionControls();
});
document.getElementById('qbSelectAll').addEventListener('change',event=>{
    currentQbHashes().forEach(hash=>{if(event.target.checked)selectedQbHashes.add(hash);else selectedQbHashes.delete(hash);});
    syncQbSelectionControls();
});
document.getElementById('qbBulkResume').addEventListener('click',()=>qbBulkAction('resume'));
document.getElementById('qbBulkPause').addEventListener('click',()=>qbBulkAction('pause'));
document.getElementById('qbBulkDelete').addEventListener('click',()=>qbBulkAction('delete'));
document.getElementById('issueList').addEventListener('change',event=>{
    const input=event.target.closest('[data-issue-select]');
    if(!input)return;
    const id=Number(input.value)||0;
    if(input.checked)selectedIssueIds.add(id);else selectedIssueIds.delete(id);
    syncIssueSelectionControls();
});
document.getElementById('issueSelectAll').addEventListener('change',event=>{
    currentIssueIds.forEach(id=>{if(event.target.checked)selectedIssueIds.add(id);else selectedIssueIds.delete(id);});
    syncIssueSelectionControls();
});
document.getElementById('issueBulkQb').addEventListener('click',()=>resubmitIssuesBatch('qb'));
document.getElementById('issueBulkGuangya').addEventListener('click',()=>resubmitIssuesBatch('guangya'));
document.getElementById('issueBulkBoth').addEventListener('click',()=>resubmitIssuesBatch('both'));
document.getElementById('issueBulkClear').addEventListener('click',clearIssuesBatch);
document.getElementById('logList').addEventListener('change',event=>{
    const input=event.target.closest('[data-log-select]');
    if(!input)return;
    const id=Number(input.value)||0;
    if(input.checked)selectedLogIds.add(id);else selectedLogIds.delete(id);
    syncLogSelectionControls();
});
document.getElementById('logSelectAll').addEventListener('change',event=>{
    currentLogIds.forEach(id=>{if(event.target.checked)selectedLogIds.add(id);else selectedLogIds.delete(id);});
    syncLogSelectionControls();
});
document.getElementById('logBulkClear').addEventListener('click',clearLogsBatch);
document.getElementById('downloadLogPrev').addEventListener('click',()=>loadLogs(downloadLogPage-1));
document.getElementById('downloadLogNext').addEventListener('click',()=>loadLogs(downloadLogPage+1));
document.getElementById('downloadIssuePrev').addEventListener('click',()=>loadIssues(downloadIssuePage-1));
document.getElementById('downloadIssueNext').addEventListener('click',()=>loadIssues(downloadIssuePage+1));
let overviewTimer=null;
function syncOverviewPolling(){
    if(document.hidden){if(overviewTimer){clearInterval(overviewTimer);overviewTimer=null;}return;}
    loadOverview();if(!overviewTimer)overviewTimer=setInterval(()=>loadOverview(),10000);
}
document.addEventListener('visibilitychange',syncOverviewPolling);
syncOverviewPolling();
const initialDownloadView=new URLSearchParams(window.location.search).get('view');
const normalizedDownloadView=['tasks','issues','logs'].includes(initialDownloadView)?initialDownloadView:'tasks';
switchDlTab(normalizedDownloadView,false);
