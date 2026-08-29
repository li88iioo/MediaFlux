let activeTab = 'organize';
let loadingOrg = false;
let organizeRequestSerial = 0;
let overviewRequestSerial = 0;
let organizePage = 1;
const ORGANIZE_LOG_PAGE_SIZE = 20;
let runtimeSource = null, runtimePaused = false, runtimeLines = [], runtimeOffset = 0, runtimeStreamId = '', runtimeCheckpoint = '', runtimeGeneration = 0;
let runtimeRequestSerial = 0, runtimeReconnectTimer = null, runtimeClearPending = false;
let organizeDetail = null, selectedOrganizeCandidate = null, organizeTaskPolling = false;
let organizeRows = [], selectedOrganizeLogs = new Map();
const organizeModal = createAppModal(document.getElementById('organizeDetailModal'));

function api(path, opts={}) { return fetch(path, {headers:{'Content-Type':'application/json'}, ...opts}); }
function _esc(v) { const d=document.createElement('div'); d.textContent=v==null?'':String(v); return d.innerHTML; }
function _attr(v) { return _esc(v).replaceAll('\"','&quot;').replaceAll("'",'&#39;'); }

const orgStatusMap = {success:['成功','done'], failed:['失败','failed'], skipped:['跳过','paused'], manual:['待确认','paused'], processing:['处理中','running'], reverted:['已回退','running'], interrupted:['操作中断','failed'], reorganizing:['重新整理中','running'], returning:['送回中','running'], reverting:['回退中','running'], deleting:['删除中','running'], deleted:['已删除','failed'], partial_failed:['部分失败','failed'], revert_failed:['回退失败','failed']};

function lockElementHeight(element) {
    if (!element) return () => {};
    const height = element.getBoundingClientRect().height;
    if (height > 0) element.style.minHeight = `${Math.ceil(height)}px`;
    return () => requestAnimationFrame(() => requestAnimationFrame(() => { element.style.minHeight = ''; }));
}

function animateLogSummary(values) {
    Object.entries(values).forEach(([id, value]) => {
        const target = document.getElementById(id);
        if (!target) return;
        if (window.MFAnim) window.MFAnim.countUp(target, value, {duration: 0.6});
        else target.textContent = String(value);
    });
}

function switchTab(tab) {
    activeTab = tab;
    document.getElementById('tabOrganize').classList.toggle('active', tab==='organize' || tab==='scrape');
    document.getElementById('tabRuntime').classList.toggle('active', tab==='runtime');
    document.getElementById('organizePanel').style.display = tab==='organize' ? '' : 'none';
    document.getElementById('scrapePanel').style.display = tab==='scrape' ? '' : 'none';
    document.getElementById('runtimePanel').style.display = tab==='runtime' ? '' : 'none';
    if(tab==='runtime') startRuntimeLogs(); else stopRuntimeLogs();
    window.history.replaceState(null,'','#'+tab);
    refreshActive();
}

async function refreshActive() {
    if (activeTab !== 'organize') return;
    const button=document.getElementById('refreshLogsBtn');
    if(button?.getAttribute('aria-busy')==='true')return;
    if(button){button.disabled=true;button.setAttribute('aria-busy','true');button.classList.add('is-refreshing');}
    try { await Promise.all([loadOverview(), loadOrganize()]); }
    finally {
        if(button){button.disabled=false;button.setAttribute('aria-busy','false');button.classList.remove('is-refreshing');}
    }
}

function loadOverview() {
    const requestSerial=++overviewRequestSerial;
    return api('/api/logs/overview').then(async response=>{
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'概览读取失败');
        if(requestSerial!==overviewRequestSerial)return false;
        const overview=data.timeline||data.organize;
        if(!overview||typeof overview!=='object')throw new Error('概览响应无效');
        animateLogSummary({
            orgSuccess:Number(overview.success)||0,
            orgFailed:Number(overview.failed)||0,
            orgSkipped:Number(overview.skipped)||0,
            orgReverted:Number(overview.reverted)||0,
        });
        return true;
    }).catch(error=>{
        if(requestSerial!==overviewRequestSerial)return false;
        const refresh=document.getElementById('lastRefresh');
        refresh.textContent=`概览刷新失败 · ${error.message||'读取失败'} · 已保留上次结果`;
        refresh.title=error.message||'概览读取失败';
        return false;
    });
}

function revealOrganizePagination() {
    const pagination=document.getElementById('organizePagination');
    pagination.classList.remove('is-initializing');
    pagination.setAttribute('aria-busy','false');
    pagination.removeAttribute('aria-hidden');
}

function loadOrganize(page=organizePage) {
    const requestSerial = ++organizeRequestSerial;
    loadingOrg = true;
    organizePage=Math.max(1,Number(page)||1);
    const origin = document.getElementById('orgOrigin').value;
    const status = document.getElementById('orgStatus').value;
    const q = document.getElementById('orgKeyword').value.trim();
    const params = [`page=${organizePage}`,`page_size=${ORGANIZE_LOG_PAGE_SIZE}`,`origin=${encodeURIComponent(origin)}`];
    if (status) params.push('status='+encodeURIComponent(status));
    if (q) params.push('q='+encodeURIComponent(q));
    return api('/api/logs/organize/timeline?'+params.join('&')).then(async response=>{
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'读取失败');
        if(requestSerial!==organizeRequestSerial)return;
        const rows=data.items||[];const pages=Number(data.pages)||0;const total=Number(data.total)||0;
        organizePage=Number(data.page)||organizePage;
        document.getElementById('organizePageInfo').textContent=total?`共 ${total} 条 · 第 ${organizePage} / ${pages} 页`:'共 0 条';
        document.getElementById('organizePrev').disabled=organizePage<=1;
        document.getElementById('organizeNext').disabled=!pages||organizePage>=pages;
        const body = document.getElementById('organizeList');
        const releaseHeight=lockElementHeight(body.closest('.table-wrap'));
        if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="table-empty">暂无整理日志</td></tr>'; organizeRows=[];selectedOrganizeLogs.clear();updateOrganizeBatchState();revealOrganizePagination();releaseHeight();return; }
        organizeRows=rows;selectedOrganizeLogs.clear();
        body.innerHTML = rows.map(r=>{
            const st = orgStatusMap[r.status] || [r.raw_status||r.status, ''];
            const tmdbTag = r.tmdb_id ? `<span class="tag-mini" style="margin-left:6px;">${_esc(r.tmdb_id)}</span>` : '';
            const legacyTag = r.legacy_incomplete ? '<span class="tag-mini is-warning" style="margin-left:6px;">旧记录只读</span>' : '';
            const originalDisplay = r.origin==='local' ? r.original_path : [r.original_path,r.original_name].filter(Boolean).join('/');
            const reason = r.error || r.warning || '';
            const skipReason = r.status==='skipped'&&r.error
                ? `<span class="organize-log-reason">跳过原因：${_esc(r.error)}</span>` : '';
            const reasonLabel = r.status==='manual' ? '待确认原因' : '说明';
            const reasonMarkup = skipReason || (reason ? `<span class="organize-log-reason">${reasonLabel}：${_esc(reason)}</span>` : '');
            const selectable = r.actions?.batch ? `<input type="checkbox" class="organize-row-select" data-key="${_esc(r.record_key)}" aria-label="选择光鸭整理日志 ${r.id}">` : '<span class="logs-origin-lock" title="本地记录只读汇总">—</span>';
            const action = r.actions?.detail ? `<button class="revert-btn detail-btn" data-id="${r.id}">详情</button>` : '<span class="text-muted">只读</span>';
            return `<tr data-origin="${_esc(r.origin)}">
                <td>${selectable}</td>
                <td><span class="logs-origin-badge is-${_esc(r.origin)}">${_esc(r.origin_label)}</span><small class="logs-origin-source" title="${_attr(r.source_label)}">${_esc(r.source_label)}</small></td>
                <td class="path-cell-long" title="${_attr(originalDisplay)}"><span>${_esc(originalDisplay||'-')}${tmdbTag}${legacyTag}</span>${reasonMarkup}</td>
                <td class="path-cell-long" title="${_attr(r.new_path)}">${_esc(r.new_path||'-')}</td>
                <td><span class="status-pill ${st[1]}" title="${_attr(r.raw_status||'')}">${_esc(st[0])}</span></td>
                <td class="text-muted">${_esc(r.updated_at||r.created_at||'-')}</td>
                <td>${action}</td>
            </tr>`;
        }).join('');
        body.querySelectorAll('.organize-row-select').forEach(input=>{
            input.addEventListener('change',()=>toggleOrganizeSelection(input.dataset.key,input.checked));
        });
        body.querySelectorAll('.detail-btn').forEach(btn=>{
            btn.addEventListener('click', ()=>openOrganizeDetail(parseInt(btn.dataset.id,10),btn));
        });
        document.getElementById('organizeSelectAll').checked=false;updateOrganizeBatchState();
        document.getElementById('lastRefresh').textContent = '更新于 '+new Date().toLocaleTimeString();
        revealOrganizePagination();
        releaseHeight();
    }).catch(error=>{
        if(requestSerial!==organizeRequestSerial)return;
        const hasRows=organizeRows.length>0;
        document.getElementById('lastRefresh').textContent=`刷新失败 · ${error.message||'读取失败'}${hasRows?' · 已保留上次结果':''}`;
        if(!hasRows)document.getElementById('organizeList').innerHTML='<tr><td colspan="7" class="table-empty">读取失败，请稍后重试</td></tr>';
    })
      .finally(()=>{if(requestSerial===organizeRequestSerial)loadingOrg=false;});
}

function toggleOrganizeSelection(recordKey,checked){const row=organizeRows.find(item=>item.record_key===recordKey&&item.actions?.batch);if(!row)return;if(checked)selectedOrganizeLogs.set(recordKey,row);else selectedOrganizeLogs.delete(recordKey);updateOrganizeBatchState();}
document.getElementById('organizePrev').addEventListener('click',()=>loadOrganize(organizePage-1));
document.getElementById('organizeNext').addEventListener('click',()=>loadOrganize(organizePage+1));
function updateOrganizeBatchState(){const count=selectedOrganizeLogs.size;const selectable=organizeRows.filter(row=>row.actions?.batch);document.getElementById('organizeBatchCount').textContent=`已选 ${count} 条光鸭记录`;['organizeBatchRenameBtn','organizeBatchRevertBtn','organizeBatchDeleteBtn'].forEach(id=>document.getElementById(id).disabled=count<2);const all=selectable.length>0&&count===selectable.length;document.getElementById('organizeSelectAll').checked=all;document.getElementById('organizeSelectAll').indeterminate=count>0&&!all;}
function _setOrganizeBatchState(message,type=''){const el=document.getElementById('organizeBatchState');el.textContent=message||'';el.className='inline-save-state'+(type?' is-'+type:'');}
document.getElementById('organizeSelectAll').addEventListener('change',event=>{const checked=event.currentTarget.checked;document.querySelectorAll('.organize-row-select').forEach(input=>{input.checked=checked;toggleOrganizeSelection(input.dataset.key,checked);});});
async function runOrganizeBatch(action){const rows=[...selectedOrganizeLogs.values()];if(rows.length<2)return;const movieRows=rows.filter(row=>row.media_type!=='tv');if(movieRows.length){_setOrganizeBatchState(`批量操作仅支持剧集，已混入 ${movieRows.length} 条电影或未知类型日志`,'error');return;}const labels={reorganize:'按各自已保存的 TMDB 映射批量改名',revert:'批量回退最近操作',delete:'将所选剧集媒体组移入光鸭回收站'};let confirmText='';const confirmed=await appConfirm({title:action==='delete'?'批量移入光鸭回收站':'确认剧集批量操作',message:`${labels[action]}，共 ${rows.length} 条日志。${action==='delete'?'这会将日志明确记录的媒体文件移入光鸭回收站。MediaFlux 不提供恢复按钮。':''}`,confirmText:action==='delete'?'移入回收站':'开始执行',danger:action==='delete',verifyText:action==='delete'?'DELETE':'',verifyLabel:'输入 DELETE 确认批量移入回收站'});if(!confirmed)return;if(action==='delete')confirmText='DELETE';const entries=rows.map(row=>({log_id:row.id,expected_version:row.version,operation_token:_operationToken()}));_setOrganizeBatchState('批量操作已提交，正在执行...');try{const response=await api('/api/logs/organize/batch',{method:'POST',body:JSON.stringify({action,entries,confirm:confirmText})});const data=await response.json();if(!response.ok)throw new Error(data.error||'批量操作提交失败');const task=await waitOrganizeTask(data.task_id,null,{reopen:false});const failed=task.result?.failed||[];_setOrganizeBatchState(failed.length?`批次完成，${failed.length} 条失败，可打开详情检查`:`批次完成，共处理 ${rows.length} 条` ,failed.length?'error':'success');loadOrganize();}catch(error){_setOrganizeBatchState(error.message,'error');}}
document.getElementById('organizeBatchRenameBtn').addEventListener('click',()=>runOrganizeBatch('reorganize'));
document.getElementById('organizeBatchRevertBtn').addEventListener('click',()=>runOrganizeBatch('revert'));
document.getElementById('organizeBatchDeleteBtn').addEventListener('click',()=>runOrganizeBatch('delete'));
document.getElementById('closeScrapeBtn').addEventListener('click',()=>switchTab('organize'));
document.getElementById('clearOrganizeLogsBtn').addEventListener('click',async(event)=>{
    const confirmed=await appConfirm({
        trigger:event.currentTarget,
        title:'清理整理记录',
        message:'清理数据库中的光鸭与本地整理记录、媒体组快照和操作步骤。不会删除或移动任何云端、本地媒体文件；排队中或正在执行的记录会自动跳过。',
        confirmText:'清理记录',
        danger:true,
        verifyText:'CLEAR',
        verifyLabel:'输入 CLEAR 确认清理整理记录',
    });
    if(!confirmed)return;
    try{
        const response=await api('/api/logs/organize',{method:'DELETE',body:JSON.stringify({confirm:'CLEAR'})});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'清理整理记录失败');
        selectedOrganizeLogs.clear();
        _setOrganizeBatchState(`已清理 ${data.deleted||0} 条记录${data.skipped_busy?`，跳过 ${data.skipped_busy} 条运行中记录`:''}`,'success');
        loadOverview();loadOrganize();
    }catch(error){
        await appAlert({type:'error',title:'清理失败',message:error.message||'清理整理记录失败'});
    }
});

function _operationToken(){return (crypto.randomUUID?.()||String(Date.now())+'-'+Math.random().toString(16).slice(2)).replaceAll('-','');}
function _setOrganizeState(message,type=''){const el=document.getElementById('organizeOperationState');if(el){el.textContent=message||'';el.className='inline-save-state'+(type?' is-'+type:'');}}
function _formatRole(role){return ({video:'视频',subtitle:'字幕',nfo:'NFO',image:'图片',metadata:'元数据'}[role]||role||'文件');}
function _extractMediaTags(filename){
    if(!filename)return [];
    const tags=[];
    const fn=String(filename);
    const has=pattern=>new RegExp(`(?:^|[^A-Z0-9])(?:${pattern})(?=$|[^A-Z0-9])`,'i').test(fn);
    if(has('2160P|4K|UHD'))tags.push('4K UHD');
    else if(has('1080P|FHD'))tags.push('1080p');
    else if(has('720P'))tags.push('720p');
    if(has('H[._ -]?265|HEVC|X265'))tags.push('HEVC');
    else if(has('H[._ -]?264|AVC|X264'))tags.push('AVC');
    else if(has('AV1'))tags.push('AV1');
    if(has('DV|DOVI|DOLBY[._ -]+VISION'))tags.push('Dolby Vision');
    else if(has('HDR10\\+|HDR10PLUS'))tags.push('HDR10+');
    else if(has('HDR'))tags.push('HDR');
    else if(has('SDR'))tags.push('SDR');
    if(has('WEB[._ -]?DL'))tags.push('WEB-DL');
    else if(has('REMUX'))tags.push('REMUX');
    else if(has('BLU[._ -]?RAY|BDMV'))tags.push('BluRay');
    if(has('ATMOS'))tags.push('Atmos');
    else if(has('TRUEHD'))tags.push('TrueHD');
    else if(has('DTS[._ -]?HD'))tags.push('DTS-HD');
    else if(has('AAC'))tags.push('AAC');
    return tags.slice(0,4);
}
function _formatReleasePosition(position){
    if(!position||typeof position!=='object')return '-';
    const season=position.season!=null?`S${String(position.season).padStart(2,'0')}`:'';
    const episode=position.episode!=null?`E${String(position.episode).padStart(2,'0')}`:'';
    return `${season}${episode}`||'-';
}
function _formatReleaseEvidence(item){
    if(!item||typeof item!=='object')return '-';
    const sourceMap={filename:'文件名',parent:'父目录',parent_directory:'父目录',release_context:'发布信息',recognition_preprocess:'预处理',explicit_marker:'显式标记',title:'标题',year:'年份',regex:'正则'};
    const kindMap={title:'标题',year:'年份',season:'季号',episode:'集号',preprocess_rule:'预处理规则',tmdb_id:'TMDB ID',strip_tail:'剥离尾缀'};
    const source=sourceMap[item?.source]||item?.source||'解析';
    const kind=kindMap[item?.kind]||item?.kind||'证据';
    const value=item?.value==null?'':String(item.value);
    return `${source} · ${kind}${value?`：${value}`:''}`;
}
function _renderReleaseParse(parse){
    const section=document.getElementById('organizeReleaseParseSection');
    const target=document.getElementById('organizeReleaseParse');
    const valid=parse&&typeof parse==='object';
    section.hidden=!valid;
    if(!valid){target.replaceChildren();return;}
    const sourcePosition=_formatReleasePosition(parse.source_position);
    const effectivePosition=_formatReleasePosition(parse.effective_position);
    const isPositionShifted = sourcePosition !== effectivePosition && sourcePosition !== '-' && effectivePosition !== '-';
    const mediaTypeLabel = parse.media_type==='tv'?'剧集':parse.media_type==='movie'?'电影':(parse.media_type||'');
    const evidence=(Array.isArray(parse.evidence)?parse.evidence:[]).slice(0,3);
    const rules=(Array.isArray(parse.preprocess_rules)?parse.preprocess_rules:[]).slice(0,3).map(rule=>({source:'预处理',kind:'规则',value:rule?.name||rule?.action||'规则'}));
    const proof=[...evidence,...rules].slice(0,3);
    target.innerHTML=`
        <div class="organize-parse-card">
            <span class="organize-parse-label">清洗标题</span>
            <div class="organize-parse-main">
                <strong class="organize-parse-title" title="${_attr(parse.title||'')}">${_esc(parse.title||'未提取')}</strong>
                <div class="organize-parse-tags">
                    ${mediaTypeLabel ? `<span class="organize-type-badge"><i data-lucide="${parse.media_type==='tv'?'tv':'film'}"></i>${_esc(mediaTypeLabel)}</span>` : ''}
                    ${parse.year ? `<span class="organize-year-badge">${_esc(parse.year)}</span>` : ''}
                </div>
            </div>
        </div>
        <div class="organize-parse-card">
            <span class="organize-parse-label">季集映射</span>
            <div class="organize-parse-main">
                <code class="organize-pos-badge">${_esc(isPositionShifted ? `${sourcePosition} → ${effectivePosition}` : (effectivePosition || '-'))}</code>
                <small class="organize-parse-sub">${isPositionShifted ? '已纠偏映射/覆盖' : '未发生位置换算'}</small>
            </div>
        </div>
        <div class="organize-parse-card organize-parse-card-proof">
            <span class="organize-parse-label">主要证据</span>
            <div class="organize-proof-chips">
                ${proof.length ? proof.map(item => `<span class="organize-proof-chip" title="${_attr(_formatReleaseEvidence(item))}">${_esc(_formatReleaseEvidence(item))}</span>`).join('') : '<small class="text-muted">历史记录未保存证据快照</small>'}
            </div>
        </div>`;
}
function _renderOrganizeDetail(data){
    organizeDetail=data;selectedOrganizeCandidate=null;
    document.getElementById('organizeDetailTitle').textContent=`整理日志 #${data.id}`;
    const [statusLabel, statusClass] = orgStatusMap[data.status] || [data.status, 'running'];
    const identityLabel=data.provider==='metatube'&&data.external_id?`MetaTube · ${data.external_id}`:(data.tmdb_id?`TMDB-${data.tmdb_id}`:'');
    const subtitleParts = [data.title, data.year, identityLabel].filter(Boolean);
    document.getElementById('organizeDetailSubtitle').innerHTML = `
        <span class="organize-detail-status-badge ${statusClass}">
            <span class="status-dot"></span>
            <span>${_esc(statusLabel)}</span>
        </span>
        <span class="organize-detail-media-meta">${_esc(subtitleParts.join(' · ') || '媒体组纠偏与操作审计')}</span>
    `;
    const notice=document.getElementById('organizeSafetyNotice');notice.hidden=!data.safety_notice;notice.textContent=data.safety_notice||'';
    const originalName = data.original_name || '-';
    const currentName = data.current_name || '-';
    const originalParentId = data.original_parent_id || '-';
    const currentParentId = data.current_parent_id || '-';
    document.getElementById('organizeDetailSummary').innerHTML = `
        <div class="organize-flow-hero">
            <div class="organize-flow-node from-node">
                <div class="organize-flow-node-badge">
                    <i data-lucide="folder-input"></i>
                    <span>原始来源</span>
                </div>
                <div class="organize-flow-filename" title="${_attr(originalName)}">
                    <strong>${_esc(originalName)}</strong>
                </div>
                <div class="organize-flow-meta">
                    <span class="organize-meta-chip" title="原始目录 ID: ${_attr(originalParentId)}">
                        <i data-lucide="database"></i>
                        <span>目录 ID: ${_esc(originalParentId)}</span>
                    </span>
                </div>
            </div>
            <div class="organize-flow-arrow">
                <div class="organize-flow-status-pill ${statusClass}">
                    <span class="status-dot"></span>
                    <span>${_esc(statusLabel)}</span>
                </div>
                <div class="organize-flow-connector"><i data-lucide="arrow-right"></i></div>
            </div>
            <div class="organize-flow-node to-node">
                <div class="organize-flow-node-badge is-target">
                    <i data-lucide="film"></i>
                    <span>归档目标</span>
                </div>
                <div class="organize-flow-filename is-target" title="${_attr(currentName)}">
                    <strong>${_esc(currentName)}</strong>
                </div>
                <div class="organize-flow-meta">
                    <span class="organize-meta-chip" title="当前目录 ID: ${_attr(currentParentId)}">
                        <i data-lucide="database"></i>
                        <span>目录 ID: ${_esc(currentParentId)}</span>
                    </span>
                </div>
            </div>
        </div>
        ${data.error ? `
            <div class="organize-summary-alert ${data.status==='skipped'?'is-skipped':'is-error'}">
                <i data-lucide="${data.status==='skipped'?'info':'alert-triangle'}"></i>
                <div>
                    <strong>${data.status==='skipped'?'跳过说明':'错误诊断'}</strong>
                    <p>${_esc(data.error)}</p>
                </div>
            </div>
        ` : ''}
    `;
    _renderReleaseParse(data.release_parse);
    const items=data.items||[];
    document.getElementById('organizeDetailItems').innerHTML=items.length?items.map(item=>{
        const fn = item.current_name || item.original_name || item.file_id;
        const tags = _extractMediaTags(fn);
        const rawRole = String(item.role || 'video');
        const role = ['video','subtitle','nfo','image','metadata'].includes(rawRole) ? rawRole : 'metadata';
        const roleIcon = role==='video' ? 'video' : role==='subtitle' ? 'captions' : 'file-text';
        const roleLabel = _formatRole(rawRole);
        const st = item.status || '-';
        const stClass = st==='success'?'done':st==='deleted'?'failed':'paused';
        return `
            <div class="organize-item organize-item-card">
                <div class="organize-item-role-col">
                    <span class="organize-item-role role-${role}">
                        <i data-lucide="${roleIcon}"></i>
                        <span>${_esc(roleLabel)}</span>
                    </span>
                </div>
                <div class="organize-item-content">
                    <div class="organize-item-name-row">
                        <strong class="organize-item-name" title="${_attr(fn)}">${_esc(fn)}</strong>
                        ${tags.length ? `<div class="organize-item-tags">${tags.map(t=>`<span class="organize-spec-tag">${_esc(t)}</span>`).join('')}</div>` : ''}
                    </div>
                    <div class="organize-item-meta">
                        <span>ID: ${_esc(item.file_id || '-')}</span>
                        <span>·</span>
                        <span>父目录: ${_esc(item.current_parent_id || '-')}</span>
                    </div>
                    ${item.error ? `<div class="organize-item-reason"><i data-lucide="alert-circle"></i><span>${item.status==='skipped'?'跳过原因':'错误'}：${_esc(item.error)}</span></div>` : ''}
                </div>
                <div class="organize-item-status-col">
                    <span class="status-pill ${stClass}">${_esc(st)}</span>
                </div>
            </div>
        `;
    }).join(''):'<div class="table-empty">旧记录没有媒体组成员快照</div>';
    const operations=data.operations||[];document.getElementById('organizeOperationSection').hidden=!operations.length;document.getElementById('organizeDetailOperations').innerHTML=operations.map(step=>`<div class="organize-operation"><span class="status-pill ${step.status==='success'?'done':step.status==='failed'?'failed':'running'}">${_esc(step.status)}</span><div><strong>${_esc(step.action)}</strong><small>${_esc(step.from_name||'-')} → ${_esc(step.to_name||'-')}</small>${step.error?`<small class="organize-item-reason">错误：${_esc(step.error)}</small>`:''}</div><time>${_esc(step.finished_at||step.started_at||'-')}</time></div>`).join('');
    const audits=data.delete_audits||[];document.getElementById('organizeDeleteAuditSection').hidden=!audits.length;document.getElementById('organizeDeleteAudits').innerHTML=audits.map(audit=>`<div class="organize-delete-audit"><span class="status-pill ${audit.status==='success'?'done':audit.status==='failed'?'failed':'paused'}">${_esc(audit.status)}</span><div><strong>${_esc(audit.reason||'未记录原因')}</strong><small>${_esc(audit.file_name||audit.file_id)}${audit.replacement_name?` → 替换为 ${_esc(audit.replacement_name)}`:''}</small><small>${_esc(audit.provider||'guangya')} · ${_esc(audit.provider_result||audit.error||'-')}</small></div></div>`).join('');
    document.getElementById('organizeTmdbQuery').value=data.title||data.original_name||'';
    document.getElementById('organizeTmdbYear').value=data.year||'';
    document.getElementById('organizeTmdbType').value=data.media_type==='tv'?'tv':'movie';
    document.getElementById('organizeSeasonOverride').value=data.season??'';
    document.getElementById('organizeEpisodeOverride').value=data.episode??'';
    syncOrganizeRecognitionFields();
    syncOrganizePositionFields();
    document.getElementById('organizeCorrectionPanel').hidden=!data.allowed_actions.search;
    document.getElementById('organizeReorganizeBtn').disabled=!data.allowed_actions.reorganize;
    document.getElementById('organizeReturnBtn').disabled=!data.allowed_actions.return_to_source;
    document.getElementById('organizeRevertBtn').disabled=!data.allowed_actions.revert;
    const deleteButton=document.getElementById('organizeDeleteBtn');
    deleteButton.disabled=!data.allowed_actions.delete;
    deleteButton.hidden=!data.allowed_actions.delete;
    document.getElementById('organizeTmdbCandidates').replaceChildren();const namingPreview=document.getElementById('organizeNamingPreview');namingPreview.replaceChildren();namingPreview.hidden=true;if(!organizeTaskPolling)_setOrganizeState('');
    window.renderLucideIcons?.(document.getElementById('organizeDetailModal'));
}
async function openOrganizeDetail(logId,trigger){
    organizeModal.open(trigger);_setOrganizeState('正在读取媒体组快照...');
    try{const response=await api('/api/logs/organize/'+logId);const data=await response.json();if(!response.ok)throw new Error(data.error||'详情加载失败');_renderOrganizeDetail(data);}catch(error){_setOrganizeState(error.message,'error');}
}
function organizeRecognitionProfile(){return organizeDetail?.recognition||{provider:'tmdb',label:'TMDB',nsfw_only:false,query_placeholder:'输入片名或剧名'};}
function syncOrganizeRecognitionFields(){const profile=organizeRecognitionProfile();const nsfwOnly=Boolean(profile.nsfw_only);document.getElementById('organizeRecognitionTitle').textContent=nsfwOnly?'重新识别 · MetaTube':'重新识别';const query=document.getElementById('organizeTmdbQuery');query.placeholder=profile.query_placeholder||(nsfwOnly?'输入番号或包含番号的文件名':'输入片名或剧名');document.querySelectorAll('[data-recognition-tmdb-only]').forEach(element=>{element.hidden=nsfwOnly;});document.getElementById('organizeRecognitionSearchLabel').textContent=nsfwOnly?'识别番号':'搜索';if(nsfwOnly)document.getElementById('organizeTmdbType').value='movie';}
async function searchOrganizeTmdb(){
    if(!organizeDetail)return;const profile=organizeRecognitionProfile();const query=document.getElementById('organizeTmdbQuery').value.trim();const year=profile.nsfw_only?'':document.getElementById('organizeTmdbYear').value.trim();const media_type=profile.nsfw_only?'movie':document.getElementById('organizeTmdbType').value;_setOrganizeState(`正在搜索 ${profile.label||'媒体信息'}...`);
    try{const response=await api(`/api/logs/organize/${organizeDetail.id}/recognition/search`,{method:'POST',body:JSON.stringify({query,year,media_type})});const data=await response.json();if(!response.ok)throw new Error(data.error||'搜索失败');renderOrganizeCandidates(data.candidates||[]);_setOrganizeState(data.candidates?.length?'请选择候选并预览命名':'没有找到精确候选',data.candidates?.length?'success':'error');}catch(error){_setOrganizeState(error.message,'error');}
}
function renderOrganizeCandidates(candidates){const box=document.getElementById('organizeTmdbCandidates');box.replaceChildren();candidates.forEach(candidate=>{const row=document.createElement('button');row.type='button';row.className='candidate-item organize-candidate';const provider=String(candidate.provider||'tmdb').toLowerCase();const identity=provider==='metatube'?`MetaTube · ${candidate.external_id||'-'}`:(provider==='clean_title'?`清洗标题 · ${candidate.external_id||'-'}`:`TMDB-${candidate.tmdb_id||candidate.external_id||'-'}`);const action=provider==='clean_title'?'清洗标题后入库':`${Math.round((candidate.score||0)*100)}%`;row.innerHTML=`<div class="candidate-info"><strong>${_esc(candidate.title||candidate.external_id||candidate.tmdb_id)}</strong><span class="text-muted">${_esc(candidate.year||'-')} · ${_esc(identity)}</span></div><span class="tag-mini">${_esc(action)}</span>`;row.addEventListener('click',()=>selectOrganizeCandidate(candidate,row));box.appendChild(row);});}
function syncOrganizePositionFields(){const isTv=!organizeRecognitionProfile().nsfw_only&&document.getElementById('organizeTmdbType').value==='tv';document.getElementById('organizePositionFields').hidden=!isTv;}
function organizeCandidatePayload(candidate=selectedOrganizeCandidate){if(!candidate)return null;const payload={...candidate};payload.provider=String(payload.provider||'tmdb').toLowerCase();payload.external_id=String(payload.external_id||payload.tmdb_id||'');payload.tmdb_id=String(payload.tmdb_id||'');if(payload.media_type==='tv'){const season=document.getElementById('organizeSeasonOverride').value.trim();const episode=document.getElementById('organizeEpisodeOverride').value.trim();if(season!=='')payload.season=Number(season);if(episode!=='')payload.episode=Number(episode);}return payload;}
let organizePreviewSequence=0;
async function previewOrganizeCandidate(candidate){const payload=organizeCandidatePayload(candidate);if(!payload)return;const sequence=++organizePreviewSequence;_setOrganizeState('正在生成无副作用预览...');try{const response=await api(`/api/logs/organize/${organizeDetail.id}/reorganize/preview`,{method:'POST',body:JSON.stringify(payload)});const data=await response.json();if(sequence!==organizePreviewSequence)return;if(!response.ok)throw new Error(data.error||'预览失败');const box=document.getElementById('organizeNamingPreview');const fullTarget=[data.target_path,data.file_name].filter(Boolean).join('/');box.hidden=false;box.innerHTML=`<div><span class="text-muted">影片目录</span><code>${_esc(data.media_dir||data.target_path||'-')}</code></div><div><span class="text-muted">视频文件</span><code>${_esc(data.file_name||'-')}</code></div><div class="organize-preview-full"><span class="text-muted">完整目标</span><code>${_esc(fullTarget||'-')}</code></div><div class="organize-preview-rule"><span class="text-muted">规则来源</span><code>命名规则来自「整理规则 → 识别与命名」</code></div>${(data.items||[]).filter(item=>item.role!=='video').map(item=>`<div><span class="text-muted">${_esc(_formatRole(item.role))}</span><code>${_esc(item.to_name)}</code></div>`).join('')}`;_setOrganizeState('预览完成，尚未写入云盘','success');}catch(error){if(sequence===organizePreviewSequence)_setOrganizeState(error.message,'error');}}
async function selectOrganizeCandidate(candidate,row){selectedOrganizeCandidate=candidate;document.querySelectorAll('.organize-candidate').forEach(item=>item.classList.toggle('selected',item===row));await previewOrganizeCandidate(candidate);}
async function waitOrganizeTask(taskId,logId,{reopen=true}={}){organizeTaskPolling=true;try{for(let attempt=0;attempt<180;attempt++){await new Promise(resolve=>setTimeout(resolve,1000));const response=await api('/api/guangya/organize/status');const task=await response.json();if(!response.ok)throw new Error(task.error||'任务状态读取失败');if(task.id!==taskId){continue;}if(task.status==='completed'){const warnings=task.result?.warnings||[];loadOverview();loadOrganize();if(reopen&&logId)await openOrganizeDetail(logId);_setOrganizeState(warnings.length?`云端操作已完成；${warnings.join('；')}`:'云端操作已完成','success');return task;}if(task.status==='failed'){throw new Error(task.error||task.message||'后台操作失败');}_setOrganizeState(task.message||'后台操作执行中...');}throw new Error('后台操作仍在执行，请稍后重新打开详情查看');}finally{organizeTaskPolling=false;}}
async function runOrganizeAction(path,method='POST',extra={}){if(!organizeDetail)return;const logId=organizeDetail.id;_setOrganizeState('操作已提交，正在后台执行...');const response=await api(`/api/logs/organize/${logId}${path}`,{method,body:JSON.stringify({operation_token:_operationToken(),expected_version:organizeDetail.version,...extra})});const data=await response.json();if(!response.ok)throw new Error(data.error||'操作提交失败');_setOrganizeState(data.message||'操作已启动');await waitOrganizeTask(data.task_id,logId);}
async function runOrganizeReorganize(){if(!selectedOrganizeCandidate){_setOrganizeState('请先选择一个识别候选并检查预览','error');return;}const payload=organizeCandidatePayload();const position=payload.media_type==='tv'?[payload.season!==undefined?`S${String(payload.season).padStart(2,'0')}`:'',payload.episode!==undefined?`E${String(payload.episode).padStart(2,'0')}`:''].filter(Boolean).join(''):'';const confirmed=await appConfirm({title:'重新整理媒体组',message:`按 ${payload.title||payload.external_id||payload.tmdb_id}${position?` · ${position}`:''} 重新整理整个媒体组。`,confirmText:'执行重新整理',danger:true});if(!confirmed)return;try{await runOrganizeAction('/reorganize','POST',payload);}catch(error){_setOrganizeState(error.message,'error');}}
async function returnOrganizeToSource(){const confirmed=await appConfirm({title:'送回源目录',message:'将视频和全部伴随文件送回各自保存的原始父目录，并恢复原文件名。',confirmText:'送回源目录',danger:true});if(!confirmed)return;try{await runOrganizeAction('/return-to-source');}catch(error){_setOrganizeState(error.message,'error');}}
async function revertOrganize(){const confirmed=await appConfirm({title:'回退最近操作',message:'按最近一次成功操作的持久化步骤回退，不会从路径猜测文件名。',confirmText:'执行回退',danger:true});if(!confirmed)return;try{await runOrganizeAction('/revert');}catch(error){_setOrganizeState(error.message,'error');}}
async function deleteOrganizeGroup(){const confirmed=await appConfirm({title:'移入光鸭回收站',message:'会将该日志明确记录的整个媒体组移入光鸭回收站。MediaFlux 不提供恢复按钮。',confirmText:'移入回收站',danger:true,verifyText:'DELETE',verifyLabel:'输入 DELETE 确认移入回收站'});if(!confirmed)return;try{await runOrganizeAction('','DELETE',{confirm:'DELETE'});}catch(error){_setOrganizeState(error.message,'error');}}
document.getElementById('organizeTmdbSearchBtn').addEventListener('click',searchOrganizeTmdb);
document.getElementById('organizeTmdbQuery').addEventListener('keydown',event=>{if(event.key==='Enter')searchOrganizeTmdb();});
document.getElementById('organizeTmdbType').addEventListener('change',syncOrganizePositionFields);
let organizePositionPreviewTimer=null;
for(const id of ['organizeSeasonOverride','organizeEpisodeOverride'])document.getElementById(id).addEventListener('input',()=>{clearTimeout(organizePositionPreviewTimer);organizePositionPreviewTimer=setTimeout(()=>{if(selectedOrganizeCandidate)previewOrganizeCandidate(selectedOrganizeCandidate);},300);});
document.getElementById('organizeReorganizeBtn').addEventListener('click',runOrganizeReorganize);
document.getElementById('organizeReturnBtn').addEventListener('click',returnOrganizeToSource);
document.getElementById('organizeRevertBtn').addEventListener('click',revertOrganize);
document.getElementById('organizeDeleteBtn').addEventListener('click',deleteOrganizeGroup);

function renderRuntimeLogs(){
    const filter=document.getElementById('runtimeFilter').value.trim().toLowerCase();
    const level=document.getElementById('runtimeLevel').value;
    let visible=runtimeLines;
    if(level) visible=visible.filter(line=>line.includes(' | ' + level.padEnd(7, ' ') + ' |'));
    if(filter) visible=visible.filter(line=>line.toLowerCase().includes(filter));
    const view=document.getElementById('runtimeLogView');view.textContent=visible.join('\n');
    if(document.getElementById('runtimeAutoScroll').checked)view.scrollTop=view.scrollHeight;
}
function appendRuntimeLine(line){
    runtimeLines.push(String(line||''));
    if(runtimeLines.length>3000)runtimeLines.splice(0,runtimeLines.length-3000);
    if(!runtimePaused)renderRuntimeLogs();
}
function connectRuntimeStream(requestSerial=runtimeRequestSerial){
    if(requestSerial!==runtimeRequestSerial||activeTab!=='runtime'||runtimeSource)return;
    const state=document.getElementById('runtimeState');
    const params=new URLSearchParams({offset:String(runtimeOffset),stream_id:runtimeStreamId,checkpoint:runtimeCheckpoint,generation:String(runtimeGeneration)});
    const source=new EventSource('/api/logs/runtime/stream?'+params.toString());
    runtimeSource=source;
    const isCurrent=()=>requestSerial===runtimeRequestSerial&&activeTab==='runtime'&&runtimeSource===source;
    const applyCursor=payload=>{runtimeOffset=payload.offset??runtimeOffset;runtimeStreamId=payload.stream_id??runtimeStreamId;runtimeCheckpoint=payload.checkpoint??runtimeCheckpoint;runtimeGeneration=payload.generation??runtimeGeneration;};
    source.addEventListener('open',()=>{if(!isCurrent())return;state.textContent='实时连接正常';state.className='runtime-log-state is-online';});
    source.addEventListener('log',event=>{if(!isCurrent())return;const payload=JSON.parse(event.data);applyCursor(payload);appendRuntimeLine(payload.line);});
    source.addEventListener('cursor',event=>{if(!isCurrent())return;applyCursor(JSON.parse(event.data||'{}'));});
    source.addEventListener('reset',event=>{if(!isCurrent())return;const payload=JSON.parse(event.data||'{}');applyCursor(payload);if(payload.reason==='cleared'){runtimeLines=[];renderRuntimeLogs();return;}appendRuntimeLine(payload.notice||(payload.reason==='tail_rebase'?'--- 日志更新较快，已跳至最新片段 ---':'--- 日志文件已轮转，继续读取新文件 ---'));});
    source.onerror=()=>{
        if(!isCurrent()){source.close();return;}
        state.textContent='连接中断，正在从最新位置重连';state.className='runtime-log-state is-error';
        source.close();runtimeSource=null;
        window.clearTimeout(runtimeReconnectTimer);
        runtimeReconnectTimer=setTimeout(()=>connectRuntimeStream(requestSerial),1500);
    };
}
async function startRuntimeLogs(){
    if(runtimeSource&&activeTab==='runtime')return;
    const requestSerial=++runtimeRequestSerial;
    window.clearTimeout(runtimeReconnectTimer);runtimeReconnectTimer=null;
    const state=document.getElementById('runtimeState');state.textContent='正在加载历史日志...';state.className='runtime-log-state is-connecting';
    try{
        const response=await fetch('/api/logs/runtime?lines=300');
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'日志加载失败');
        if(requestSerial!==runtimeRequestSerial||activeTab!=='runtime')return;
        runtimeLines=data.lines||[];runtimeOffset=data.offset||0;runtimeStreamId=data.stream_id||'';runtimeCheckpoint=data.checkpoint||'';runtimeGeneration=data.generation||0;renderRuntimeLogs();connectRuntimeStream(requestSerial);
    }catch(error){
        if(requestSerial!==runtimeRequestSerial||activeTab!=='runtime')return;
        state.textContent=`${error.message}，正在重试`;state.className='runtime-log-state is-error';
        window.clearTimeout(runtimeReconnectTimer);
        runtimeReconnectTimer=setTimeout(()=>{
            if(requestSerial===runtimeRequestSerial&&activeTab==='runtime')startRuntimeLogs();
        },1500);
    }
}
function stopRuntimeLogs(){
    ++runtimeRequestSerial;
    window.clearTimeout(runtimeReconnectTimer);runtimeReconnectTimer=null;
    if(runtimeSource){runtimeSource.close();runtimeSource=null;}
    const state=document.getElementById('runtimeState');state.textContent='未连接';state.className='runtime-log-state';
}
document.getElementById('runtimeFilter').addEventListener('input',renderRuntimeLogs);
document.getElementById('runtimeLevel').addEventListener('change',renderRuntimeLogs);
document.getElementById('runtimePauseBtn').addEventListener('click',event=>{runtimePaused=!runtimePaused;event.currentTarget.innerHTML=`<i data-lucide="${runtimePaused?'play':'pause'}"></i>${runtimePaused?'继续':'暂停'}`;if(!runtimePaused)renderRuntimeLogs();});
async function clearRuntimeLogs(){
    if(runtimeClearPending)return;
    runtimeClearPending=true;
    const button=document.getElementById('runtimeClearBtn');const state=document.getElementById('runtimeState');button.disabled=true;
    const requestSerial=++runtimeRequestSerial;
    window.clearTimeout(runtimeReconnectTimer);runtimeReconnectTimer=null;
    if(runtimeSource){runtimeSource.close();runtimeSource=null;}
    state.textContent='正在清空持久化日志...';state.className='runtime-log-state is-connecting';
    try{
        const response=await api('/api/logs/runtime',{method:'DELETE'});const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'清空日志失败');
        if(requestSerial!==runtimeRequestSerial||activeTab!=='runtime')return;
        runtimeLines=[];runtimeOffset=data.offset||0;runtimeStreamId=data.stream_id||'';runtimeCheckpoint=data.checkpoint||'';runtimeGeneration=data.generation||0;renderRuntimeLogs();
        state.textContent='日志已清空，实时连接正常';state.className='runtime-log-state is-online';connectRuntimeStream(requestSerial);
    }catch(error){
        if(requestSerial!==runtimeRequestSerial||activeTab!=='runtime')return;
        state.textContent=`清空失败：${error.message||'未知错误'}`;state.className='runtime-log-state is-error';
        connectRuntimeStream(requestSerial);
        await appAlert({type:'error',title:'清空日志失败',message:error.message||'持久化日志未被清空，请稍后重试。'});
    }finally{runtimeClearPending=false;button.disabled=false;}
}
document.getElementById('runtimeClearBtn').addEventListener('click',clearRuntimeLogs);
window.addEventListener('beforeunload',stopRuntimeLogs);

function scrapeText(target,value,fallback='—'){
    const node=typeof target==='string'?document.getElementById(target):target;if(!node)return;
    const text=String(value??'').trim();node.textContent=text||fallback;
}
function scrapeSafeImage(id,url,alt){
    const image=document.getElementById(id);if(!image)return false;const value=String(url||'').trim();
    const safe=value.startsWith('https://');image.hidden=!safe;if(safe){image.src=value;image.alt=alt||'';}else{image.removeAttribute('src');image.alt='';}return safe;
}
function scrapeInfoRows(target,rows){
    const box=typeof target==='string'?document.getElementById(target):target;if(!box)return;box.replaceChildren();
    (rows||[]).forEach(([label,value])=>{const text=Array.isArray(value)?value.filter(Boolean).join('、'):String(value??'').trim();if(!text)return;const row=document.createElement('div');row.className='scrape-lab-info-row';const key=document.createElement('span');key.textContent=label;const val=document.createElement('strong');val.textContent=text;row.append(key,val);box.appendChild(row);});
    if(!box.children.length){const empty=document.createElement('p');empty.className='scrape-lab-inline-empty';empty.textContent='暂无可展示信息';box.appendChild(empty);}
}
function scrapeChips(target,values){
    const box=typeof target==='string'?document.getElementById(target):target;if(!box)return;const nodes=(values||[]).filter(Boolean).map(value=>{const chip=document.createElement('span');chip.className='scrape-lab-chip';chip.textContent=String(value);return chip;});box.replaceChildren(...nodes);
}
function setScrapeBusy(busy){
    const result=document.getElementById('scrapeResult');const button=document.getElementById('scrapeBtn');result.setAttribute('aria-busy',busy?'true':'false');result.dataset.state=busy?'loading':(result.dataset.state==='idle'?'idle':'ready');button.disabled=busy;button.querySelector('span').textContent=busy?'识别中…':'开始识别';
}
async function runScrapePreview(){
    const filename=document.getElementById('scrapeFilename').value.trim();
    const parent_path=document.getElementById('scrapeParentPath').value.trim();
    if(!filename){document.getElementById('scrapeFilename').focus();await appAlert({type:'warning',title:'缺少文件名',message:'输入需要识别的媒体文件名后再执行刮削预览。'});return;}
    setScrapeBusy(true);
    try{const response=await api('/api/tools/scrape/preview',{method:'POST',body:JSON.stringify({filename,parent_path})});const data=await response.json();if(!response.ok)throw new Error(data.error||'识别失败');renderScrapePreview(data);}
    catch(error){const diagnostic=document.getElementById('scrapeDiagnostic');diagnostic.hidden=false;diagnostic.className='scrape-lab-diagnostic is-error';scrapeInfoRows(diagnostic,[['识别失败',error.message||'无法生成识别结果']]);}
    finally{setScrapeBusy(false);}
}
function scrapeCandidateBreakdown(candidate){
    const breakdown=candidate.score_breakdown||{};const box=document.createElement('div');box.className='scrape-lab-candidate-breakdown';
    const percent=value=>`${Math.round(Number(value||0)*100)}%`;
    scrapeInfoRows(box,[['标题',percent(breakdown.title_score)],['原名',percent(breakdown.original_title_score)],['别名',percent(breakdown.alias_score)],['年份',percent(breakdown.year_score)],['年份惩罚',percent(breakdown.year_penalty)],['类型约束',percent(breakdown.media_type_score)],['约束惩罚',percent(breakdown.constraint_penalty)],['最终分数',percent(breakdown.final_score)],['命中标题',breakdown.matched_title],['拒绝原因',breakdown.rejected_constraints]]);
    return box;
}
function renderScrapePreview(data){
    const result=document.getElementById('scrapeResult');const match=data.match||{};const parsed=data.parsed||{};const tags=parsed.resource_tags||{};const diagnostic=data.diagnostic||{};const recognition=data.recognition||{};const folder=recognition.folder_context||{};const decision=recognition.threshold_decision||{};const cleaned=recognition.cleaned_components||{};const ai=recognition.ai||{};const aiInput=ai.input||{};const aiOutput=ai.output||{};const aiSecond=ai.second_search||{};const aiSecondDecision=aiSecond.threshold_decision||{};const naming=data.naming||{};const candidates=Array.isArray(data.candidates)?data.candidates:[];
    result.dataset.state='ready';document.getElementById('scrapeEmpty').hidden=true;document.getElementById('scrapeHero').hidden=false;document.getElementById('scrapeRecognitionContext').hidden=false;document.getElementById('scrapeAiDiagnostic').hidden=false;document.getElementById('scrapeDetailGrid').hidden=false;
    const matched=diagnostic.status==='matched';const status=document.getElementById('scrapeMatchStatus');status.className='scrape-lab-status '+(matched?'is-success':diagnostic.status==='low_confidence'?'is-warning':'is-error');scrapeText(status,matched?'识别命中':diagnostic.status==='low_confidence'?'需要人工确认':'识别未通过');
    const title=match.title||parsed.title||data.filename||'未识别媒体';scrapeText('scrapeTitle',title);scrapeText('scrapeSubtitle',[match.original_title,match.year,match.media_type==='tv'?'剧集':'电影',match.tmdb_id?`TMDB ${match.tmdb_id}`:''].filter(Boolean).join(' · '),'仅完成本地文件名解析');scrapeText('scrapeOverview',match.overview,'TMDB 暂无简介，当前仍可检查解析、规格和候选结果。');
    const hasPoster=scrapeSafeImage('scrapePoster',match.poster_url,`${title} 海报`);document.getElementById('scrapePosterFallback').hidden=hasPoster;scrapeSafeImage('scrapeBackdrop',match.backdrop_url,`${title} 背景图`);
    scrapeChips('scrapeHeroChips',[matched?'自动匹配':diagnostic.status==='low_confidence'?'低置信度':'未匹配',ai.attempted?'AI 回退':'确定性识别',match.vote_average?`评分 ${Number(match.vote_average).toFixed(1)}`:'',tags.resolution,tags.source,tags.media,tags.effect]);
    const diagnosticBox=document.getElementById('scrapeDiagnostic');diagnosticBox.hidden=false;diagnosticBox.className='scrape-lab-diagnostic '+(matched?'is-success':diagnostic.status==='low_confidence'?'is-warning':'is-error');scrapeInfoRows(diagnosticBox,[['识别状态',diagnostic.message||data.error||'匹配成功'],['命中方式',diagnostic.matched_by||'—'],['匹配模式',diagnostic.match_mode||'—'],['置信度',`${Math.round(Number(match.confidence||0)*100)}%`],['通过阈值',`${Math.round(Number(diagnostic.threshold||0)*100)}%`]]);
    scrapeInfoRows('scrapeParsed',[['原始文件名',data.filename],['父目录',data.parent_path],['搜索标题',parsed.title],['年份',parsed.year],['类型',parsed.type==='tv'?'剧集':'电影'],['季号',parsed.season],['集号',parsed.episode],['显式 TMDB ID',parsed.tmdb_id]]);
    scrapeInfoRows('scrapeCleanedComponents',[['归一化标题',recognition.normalized_title],['文件标题',recognition.filename_title],['文件年份',recognition.filename_year],['已清理来源 / 发布组前缀',cleaned.release_prefixes],['Checksum',cleaned.checksums],['已清理制作组后缀',cleaned.release_groups],['移除噪声',cleaned.noise_tokens]]);
    scrapeInfoRows('scrapeFolderContext',[['路径',folder.path],['目录标题',folder.title],['目录年份',folder.year],['媒体类型',folder.media_type==='tv'?'剧集':'电影'],['季号',folder.season],['集号',folder.episode]]);
    scrapeChips('scrapeQueryVariants',recognition.query_variants||[]);
    scrapeInfoRows('scrapeThresholdDecision',[['结果',decision.passed?'通过自动阈值':'进入人工确认'],['最终分数',`${Math.round(Number(decision.score||0)*100)}%`],['阈值',`${Math.round(Number(decision.threshold||0)*100)}%`],['原因',decision.reason],['候选拒绝约束',recognition.rejected_constraints]]);
    const aiReason=({disabled:'功能未启用',deterministic_not_eligible:'确定性结果不需要回退',deterministic_failed:'确定性识别未通过'}[ai.reason]||ai.reason||'未触发');const aiPercent=value=>value===null||value===undefined||value===''?'':`${Math.round(Number(value)*100)}%`;
    scrapeText('scrapePipelineMode',ai.attempted?'确定性失败 → AI 回退':'确定性优先');
    scrapeText('scrapeAiStatus',ai.attempted?(ai.error?'调用失败':'已执行'):'未调用');
    scrapeInfoRows('scrapeAiInput',[['归一化标题',aiInput.normalized_title],['文件标题',aiInput.filename_title],['目录标题',aiInput.folder_title],['目录年份',aiInput.folder_year],['媒体类型',aiInput.media_type==='tv'?'剧集':aiInput.media_type==='movie'?'电影':''],['季号',aiInput.season],['集号',aiInput.episode],['别名',aiInput.aliases]]);
    scrapeInfoRows('scrapeAiOutput',[['标题',aiOutput.title],['原始标题',aiOutput.original_title],['年份',aiOutput.year],['媒体类型',aiOutput.media_type==='tv'?'剧集':aiOutput.media_type==='movie'?'电影':''],['季号',aiOutput.season],['集号',aiOutput.episode],['别名',aiOutput.aliases],['AI 置信度',aiPercent(aiOutput.confidence)]]);
    const aiRevalidation=ai.tmdb_revalidation||{};scrapeInfoRows('scrapeAiDecision',[['运行状态',ai.attempted?'已调用结构化回退':'未调用'],['触发原因',aiReason],['AI 阈值',aiPercent(ai.confidence_threshold)],['二次 TMDB 候选',aiSecond.candidate_count],['本地评分结果',aiSecondDecision.passed?'通过':'进入人工确认'],['本地分数',aiPercent(aiSecondDecision.score)],['本地阈值',aiPercent(aiSecondDecision.threshold)],['TMDB 详情复核',aiRevalidation.passed===true?'通过':aiRevalidation.passed===false?'未通过':'未执行'],['诊断',ai.error]]);
    scrapeInfoRows('scrapeMetadataRows',[['标题',match.title],['原始标题',match.original_title],['上映 / 首播',match.release_date],['状态',match.status],['评分',match.vote_average?`${Number(match.vote_average).toFixed(1)} / 10（${match.vote_count||0}票）`:''],['类型标签',match.genres],['国家 / 地区',match.origin_country],['语言',match.spoken_languages],['播出平台',match.networks],['出品公司',match.production_companies],['季数',match.season_count],['集数',match.episode_count]]);
    scrapeInfoRows('scrapeResourceRows',[['分辨率',tags.resolution],['来源平台',tags.source],['资源类型',tags.media],['特效标签',tags.effect],['视频编码',tags.video_codec],['音频规格',tags.audio],['制作组',tags.release_group]]);
    scrapeInfoRows('scrapeNamingRows',[['标准文件名',naming.file_name],['归档目录',naming.show_dir]]);
    const candidateSection=document.getElementById('scrapeCandidates');const candidateList=document.getElementById('scrapeCandidateList');candidateList.replaceChildren();candidateSection.hidden=!candidates.length;scrapeText('scrapeCandidateCount',`${candidates.length} 个`);
    candidates.forEach((candidate,index)=>{const card=document.createElement('article');card.className='scrape-lab-candidate';const media=document.createElement('div');media.className='scrape-lab-candidate-poster';const image=document.createElement('img');image.width=342;image.height=513;image.alt=`${candidate.title||'候选'} 海报`;if(String(candidate.poster_url||'').startsWith('https://'))image.src=candidate.poster_url;else image.hidden=true;const fallback=document.createElement('span');fallback.textContent=String(index+1).padStart(2,'0');fallback.hidden=!image.hidden;media.append(image,fallback);const body=document.createElement('div');body.className='scrape-lab-candidate-body';const name=document.createElement('strong');name.textContent=candidate.title||'未命名候选';const meta=document.createElement('span');meta.textContent=[candidate.original_title,candidate.year,`匹配 ${Math.round(Number(candidate.score||0)*100)}%`].filter(Boolean).join(' · ');const overview=document.createElement('p');overview.textContent=candidate.overview||'暂无简介';body.append(name,meta,overview,scrapeCandidateBreakdown(candidate));const action=document.createElement('button');action.type='button';action.className='jump-btn';action.textContent=data.locked&&candidate.tmdb_id===match.tmdb_id?'已锁定':'锁定此结果';action.disabled=Boolean(data.locked&&candidate.tmdb_id===match.tmdb_id);action.addEventListener('click',()=>confirmScrapeLock(data,candidate));card.append(media,body,action);candidateList.appendChild(card);});
    renderLucideIcons(result);
}
async function confirmScrapeLock(preview,candidate){
    const confirmed=await appConfirm({title:'锁定 TMDB 映射',message:`把当前目录中的该文件锁定为 ${candidate.title||candidate.tmdb_id} (tmdb-${candidate.tmdb_id})。`,confirmText:'锁定映射'});if(!confirmed)return;
    const response=await api('/api/tools/scrape/confirm',{method:'POST',body:JSON.stringify({filename:preview.filename,parent_path:preview.parent_path||'',tmdb_id:candidate.tmdb_id,title:candidate.title,year:candidate.year,media_type:candidate.media_type,rejected_tmdb_ids:(preview.candidates||[]).map(item=>String(item.tmdb_id||'')).filter(id=>id&&id!==String(candidate.tmdb_id||''))})});const data=await response.json();if(!response.ok)await appAlert({type:'error',title:'映射锁定失败',message:data.error||'锁定失败'});else runScrapePreview();
}
document.getElementById('scrapeBtn').addEventListener('click',runScrapePreview);
['scrapeFilename','scrapeParentPath'].forEach(id=>document.getElementById(id).addEventListener('keydown',event=>{if(event.key==='Enter')runScrapePreview();}));

// 初次加载
const initialTab=window.location.hash.slice(1);
if(['organize','scrape','runtime'].includes(initialTab)) switchTab(initialTab);
else { loadOverview(); loadOrganize(); }
