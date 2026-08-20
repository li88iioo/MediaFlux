(function(){
    const form=document.getElementById('strmForm');
    const input=document.getElementById('strmSourceDirs');
    const cron=document.getElementById('strmCron');
    const metadata=document.getElementById('strmMetadataEnabled');
    const videoExtsInput=document.getElementById('strmVideoExts');
    const metadataExtsInput=document.getElementById('strmMetadataExts');
    const videoExtsBox=document.getElementById('strmVideoExtsTagBox');
    const metadataExtsBox=document.getElementById('strmMetadataExtsTagBox');
    const strmBaseUrlInput=document.getElementById('strmBaseUrl');
    const strmBaseUrlDetectBtn=document.getElementById('detectStrmBaseUrlBtn');
    const strmBaseUrlCandidates=document.getElementById('strmBaseUrlCandidates');
    const strmBaseUrlState=document.getElementById('strmBaseUrlState');
    const diagnosticResult=document.getElementById('strmIndexDiagnosticResult');
    const diagnosticState=document.getElementById('strmIndexDiagnosticState');
    const diagnosticRefreshBtn=document.getElementById('refreshStrmIndexDiagnosticBtn');
    const diagnosticCleanupBtn=document.getElementById('cleanupStrmTestIndexesBtn');
    const failureList=document.getElementById('strmFailureList');
    const failureState=document.getElementById('strmFailureState');
    const failureSourceFilter=document.getElementById('strmFailureSourceFilter');
    const failureActionFilter=document.getElementById('strmFailureActionFilter');
    const failureStatusFilter=document.getElementById('strmFailureStatusFilter');
    const retrySelectedBtn=document.getElementById('retrySelectedStrmFailuresBtn');
    const retryAllBtn=document.getElementById('retryAllStrmFailuresBtn');
    const selectAllCheckbox=document.getElementById('selectAllStrmFailures');
    const selectAllLabel=document.getElementById('selectAllLabel');
    const clearFailuresBtn=document.getElementById('clearStrmFailuresBtn');
    const selectedFailuresCount=document.getElementById('selectedFailuresCount');
    const failureSelectionStats=document.getElementById('strmFailureSelectionStats');
    const failurePageInfo=document.getElementById('strmFailurePageInfo');
    const failurePrevBtn=document.getElementById('strmFailurePrev');
    const failureNextBtn=document.getElementById('strmFailureNext');
    let failurePage=1;const FAILURE_PAGE_SIZE=120;let failureTotalPages=1;let failureTotalCount=0;
    let sources=[];let pollTimer=null;let diagnosticSnapshot=null;let diagnosticLoading=false;let diagnosticCleanupBusy=false;let failureSnapshot=[];let failureSummary={};let failureLoading=false;let failureRetrying=false;let selectedFailures=new Set();let failureRequestGeneration=0;let failureAbortController=null;
    const strmTabs=[...document.querySelectorAll('.strm-tab-btn')];
    const strmPanels=[...document.querySelectorAll('.strm-tab-panel')];
    const validStrmTabs=new Set(strmTabs.map(button=>button.dataset.tab));
    const strmLoadedTabs=new Set();
    let activeStrmTab='config';
    let strmConfigReady=false;

    const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    function activateStrmTab(tab,{focus=false,updateHash=true}={}){
        const normalized=validStrmTabs.has(tab)?tab:'config';
        activeStrmTab=normalized;
        strmTabs.forEach(button=>{
            const active=button.dataset.tab===normalized;
            button.classList.toggle('active',active);
            button.setAttribute('aria-selected',String(active));
            button.tabIndex=active?0:-1;
            if(active&&focus)button.focus({preventScroll:true});
        });
        strmPanels.forEach(panel=>{panel.hidden=panel.id!==`strmTab_${normalized}`;});
        if(updateHash){
            const url=new URL(window.location.href);
            url.hash=normalized==='config'?'':normalized;
            window.history.replaceState(window.history.state,'',`${url.pathname}${url.search}${url.hash}`);
        }
        if(document.documentElement?.dataset)delete document.documentElement.dataset.strmInitialTab;
        if(strmConfigReady)void ensureStrmTabLoaded(normalized);
    }

    function ensureStrmTabLoaded(tab){
        if(!strmConfigReady||strmLoadedTabs.has(tab))return Promise.resolve();
        strmLoadedTabs.add(tab);
        if(tab==='config'){
            return strmBaseUrlInput.value.trim()?Promise.resolve():loadStrmBaseUrlCandidates();
        }
        if(tab==='schedule')return Promise.all([validateCron(),loadStatus()]);
        if(tab==='diagnostics')return Promise.all([loadIndexDiagnostics(),loadFailures()]);
        return Promise.resolve();
    }

    strmTabs.forEach((button,index)=>{
        button.addEventListener('click',()=>activateStrmTab(button.dataset.tab));
        button.addEventListener('keydown',event=>{
            let nextIndex=null;
            if(event.key==='ArrowRight')nextIndex=(index+1)%strmTabs.length;
            else if(event.key==='ArrowLeft')nextIndex=(index-1+strmTabs.length)%strmTabs.length;
            else if(event.key==='Home')nextIndex=0;
            else if(event.key==='End')nextIndex=strmTabs.length-1;
            if(nextIndex===null)return;
            event.preventDefault();
            activateStrmTab(strmTabs[nextIndex].dataset.tab,{focus:true});
        });
    });
    activateStrmTab(window.location?.hash?.slice(1)||'',{updateHash:false});
    window.addEventListener?.('hashchange',()=>activateStrmTab(window.location?.hash?.slice(1)||'',{updateHash:false}));

    // Tag Input Helper System
    function initTagInput(boxEl, hiddenInputEl, options={}){
        const listEl=boxEl.querySelector('.strm-tag-list');
        const inputEl=boxEl.querySelector('.strm-tag-field');
        const badgeClass=options.badgeClass||'';
        let tags=[];

        function syncHidden(){
            hiddenInputEl.value=tags.join(',');
            if(typeof options.onChange === 'function') options.onChange(tags);
        }
        function renderTags(){
            listEl.innerHTML='';
            tags.forEach((tag,idx)=>{
                const badge=document.createElement('span');
                badge.className='tag-chip-badge'+(badgeClass?' '+badgeClass:'');
                badge.textContent=tag+' ';
                const btn=document.createElement('button');
                btn.type='button';
                btn.className='tag-chip-remove';
                btn.setAttribute('aria-label',`删除 ${tag}`);
                btn.innerHTML='&times;';
                btn.addEventListener('click',e=>{
                    e.stopPropagation();
                    removeTag(idx);
                });
                badge.appendChild(btn);
                listEl.appendChild(badge);
            });
            syncHidden();
        }
        function addTags(rawText){
            if(!rawText)return;
            const items=String(rawText).split(/[,，\r\n\s]+/).map(s=>s.trim().toLowerCase().replace(/^\./,'')).filter(Boolean);
            let added=false;
            items.forEach(item=>{
                if(item && !tags.includes(item)){
                    tags.push(item);
                    added=true;
                }
            });
            if(added) renderTags();
        }
        function removeTag(index){
            if(index>=0 && index<tags.length){
                tags.splice(index,1);
                renderTags();
            }
        }
        function setTagsFromString(rawText){
            tags=(rawText||'').split(/[,，\r\n\s]+/).map(s=>s.trim().toLowerCase().replace(/^\./,'')).filter(Boolean);
            renderTags();
        }

        boxEl.addEventListener('click',e=>{
            if(!e.target.closest('.tag-chip-remove')) inputEl.focus();
        });
        inputEl.addEventListener('keydown',e=>{
            if(e.key==='Enter'||e.key===','||e.key==='，'){
                e.preventDefault();
                addTags(inputEl.value);
                inputEl.value='';
            }else if(e.key==='Backspace'&&!inputEl.value&&tags.length>0){
                removeTag(tags.length-1);
            }
        });
        inputEl.addEventListener('blur',()=>{
            if(inputEl.value.trim()){
                addTags(inputEl.value);
                inputEl.value='';
            }
        });

        return { addTags, removeTag, setTagsFromString, getTags:()=>[...tags] };
    }

    const videoTagInput = initTagInput(videoExtsBox, videoExtsInput);
    const metadataTagInput = initTagInput(metadataExtsBox, metadataExtsInput);

    function syncSourceReservation(count){const rows=Math.min(12,Math.max(1,Number(count)||1));document.documentElement?.style?.setProperty('--strm-source-reserved-height',`${rows*60-8}px`);try{sessionStorage.setItem('mediaflux:strm-source-rows',String(rows));}catch(_) {}}
    function renderSources(){input.value=JSON.stringify(sources);const list=document.getElementById('strmSourceList');syncSourceReservation(sources.length);list.setAttribute('aria-busy','false');if(!sources.length){list.innerHTML='<div class="strm-source-empty"><i data-lucide="folder-plus" style="width:16px;height:16px;vertical-align:-3px;margin-right:6px;color:var(--text-muted);"></i>暂未设置网盘扫描目录</div>';window.renderLucideIcons?.(list);return;}list.innerHTML=sources.map((source,index)=>`<div class="strm-source-item"><div class="strm-source-info"><i data-lucide="folder-check" class="strm-source-icon"></i><div><strong>${esc(source.name)}</strong><span>ID ${esc(source.id)}</span></div></div><button type="button" data-remove-source="${index}" title="移除目录"><i data-lucide="x"></i></button></div>`).join('');window.renderLucideIcons?.(list);list.querySelectorAll('[data-remove-source]').forEach(btn=>btn.addEventListener('click',()=>{sources.splice(Number(btn.dataset.removeSource),1);renderSources();}));}

    const strmAddressWarningText={
        lan_binding_disabled:'当前服务仅监听本机；如需局域网播放，请先在控制台设置中允许专用网络访问。',
        container_address_unreliable:'容器内无法可靠识别宿主机地址，请优先使用当前访问地址、宿主机 IP 或反向代理域名。',
        no_lan_address:'没有检测到可用的局域网 IPv4，请手动填写宿主机地址。',
        multiple_lan_addresses:'检测到多个网卡地址，请选择媒体服务器所在网络能够访问的一项。',
    };
    function browserBaseUrlCandidate(){
        if(!['http:','https:'].includes(window.location.protocol))return null;
        return {url:window.location.origin,source:'browser_origin',label:'当前访问地址'};
    }
    function renderStrmBaseUrlCandidates(payload){
        const rows=[];
        const configured=String(payload.configured||'').replace(/\/$/,'');
        if(configured)rows.push({url:configured,source:'configured',label:'已保存地址'});
        const browser=browserBaseUrlCandidate();
        if(browser)rows.push(browser);
        (payload.candidates||[]).forEach(item=>rows.push(item));
        const unique=[];const seen=new Set();
        rows.forEach(item=>{const url=String(item.url||'').replace(/\/$/,'');if(url&&!seen.has(url)){seen.add(url);unique.push({...item,url});}});
        strmBaseUrlCandidates.replaceChildren();
        const placeholder=document.createElement('option');
        placeholder.value='';
        placeholder.textContent=unique.length?'选择检测到的地址':'未检测到可用地址';
        strmBaseUrlCandidates.appendChild(placeholder);
        unique.forEach(item=>{const option=document.createElement('option');option.value=item.url;option.textContent=`${item.label||'候选地址'} · ${item.url}`;strmBaseUrlCandidates.appendChild(option);});
        strmBaseUrlCandidates.disabled=unique.length===0;
        const warnings=(payload.warnings||[]).map(code=>strmAddressWarningText[code]).filter(Boolean);
        if(browser&&['localhost','127.0.0.1','0.0.0.0'].includes(window.location.hostname)){warnings.unshift('当前浏览器地址仅适合本机访问，其他设备请不要选择 localhost。');}
        strmBaseUrlState.dataset.tone=warnings.length?'warning':'success';
        strmBaseUrlState.textContent=warnings[0]||(unique.length?`已找到 ${unique.length} 个候选地址，请确认后选择。`:'请手动填写媒体服务器可访问的地址。');
    }
    async function loadStrmBaseUrlCandidates(){
        if(strmBaseUrlDetectBtn.disabled)return;
        strmBaseUrlDetectBtn.disabled=true;strmBaseUrlDetectBtn.classList.add('is-loading');strmBaseUrlDetectBtn.setAttribute('aria-busy','true');
        strmBaseUrlState.dataset.tone='testing';strmBaseUrlState.textContent='正在检查监听状态与局域网地址…';
        try{const response=await fetch('/api/strm/base-url-candidates');const data=await response.json();if(!response.ok)throw new Error(data.error||'地址检测失败');renderStrmBaseUrlCandidates(data);}
        catch(error){strmBaseUrlState.dataset.tone='error';strmBaseUrlState.textContent=error.message||'地址检测失败，请手动填写。';}
        finally{strmBaseUrlDetectBtn.disabled=false;strmBaseUrlDetectBtn.classList.remove('is-loading');strmBaseUrlDetectBtn.setAttribute('aria-busy','false');}
    }

    function syncMetadata(){
        const enabled = metadata.checked;
        const metadataRow = document.getElementById('strmMetadataExtRow');
        metadataRow.classList.toggle('is-disabled', !enabled);
        metadataExtsBox.classList.toggle('is-disabled', !enabled);
        document.getElementById('strmMetadataExtField').disabled = !enabled;
    }

    function setTag(text,color){const tag=document.getElementById('strmStateTag');tag.innerHTML=`<i data-lucide="activity"></i><span>${esc(text)}</span>`;tag.className='share-status-badge';if(color==='var(--success)')tag.classList.add('is-success');else if(color==='var(--warning)'||color==='var(--danger)')tag.classList.add('is-danger');window.renderLucideIcons?.(tag);}
    function renderStatus(s){
        const metadataQueue=s.metadata_queue&&typeof s.metadata_queue==='object'?s.metadata_queue:{};
        const metadataPending=Math.max(0,Number(metadataQueue.pending||0));
        const metadataFailed=Math.max(0,Number(metadataQueue.failed||0));
        const metadataPaused=!s.running&&metadataPending>0&&metadataQueue.enabled===false;
        const metadataActive=!s.running&&metadataPending>0&&!metadataPaused;
        setTag(s.running?'同步中':metadataActive?'元数据处理中':metadataPaused?'元数据已暂停':metadataFailed>0?'元数据有失败':s.config_error?'配置不完整':s.enabled?'定时已启用':'定时已停用',s.running||metadataActive?'var(--info)':metadataPaused||metadataFailed>0||s.config_error?'var(--warning)':s.enabled?'var(--success)':'var(--text-muted)');
        document.getElementById('strmNextRun').textContent=s.next_run||(s.config_error||'无定时计划');
        const last=s.last_run||{};
        document.getElementById('strmLastRun').textContent=last.started_at?`${last.started_at}`:'暂无运行记录';
        const lastStats=last.result&&last.result.stats&&typeof last.result.stats==='object'?last.result.stats:{};
        const lastMode=String(last.result?.mode||'');
        const metricNode=document.getElementById('strmLastRunMetrics');
        if(lastMode==='fast_noop')metricNode.textContent='最近快速同步：没有待处理的增量变化';
        else if(Number(lastStats.directories||0)>0)metricNode.textContent=`最近完整扫描：${Number(lastStats.directories||0)} 个目录 · ${Number(lastStats.directory_requests||0)} 次请求 · 峰值 ${Number(lastStats.scan_workers_peak||0)} 线程 · ${Number(lastStats.scan_elapsed_seconds||0).toFixed(1)} 秒`;
        else metricNode.textContent='最近扫描指标将在任务完成后显示';
        const progress=s.progress||{};
        const completed=Math.max(0,Number(progress.completed||0));
        const total=Math.max(0,Number(progress.total||0));
        const percent=Math.max(0,Math.min(100,Number(progress.percent||0)));
        document.getElementById('strmProgressStage').textContent=metadataActive?'STRM 已完成 · 元数据后台处理中':metadataPaused?'伴随元数据同步已关闭 · 队列暂停':String(progress.detail||progress.stage||(s.running?'正在同步':'等待任务'));
        document.getElementById('strmProgressCount').textContent=metadataActive||metadataPaused?`队列 ${metadataPending} 项${Number(metadataQueue.retry_wait||0)>0?` · ${Number(metadataQueue.retry_wait)} 项等待重试`:''}`:`${completed} / ${total} · ${percent}%`;
        document.getElementById('strmProgressBar').style.transform=`scaleX(${metadataActive||metadataPaused?1:percent/100})`;
        const dot=document.getElementById('strmStatusDot');
        if(dot)dot.className='status-dot'+(s.running||metadataActive?' active':'');
        const runtime=Array.isArray(s.source_runtime)?s.source_runtime:[];
        document.getElementById('strmSourceRuntime').innerHTML=runtime.length?runtime.map(row=>`<div><strong>${esc(row.name||row.id)}</strong> · ${esc(row.status||'pending')} · ${Number(row.completed||0)}/${Number(row.total||0)}</div>`).join(''):'<span class="text-muted">暂无来源运行状态</span>';
        const btn=document.getElementById('runStrmNowBtn');
        const fullBtn=document.getElementById('runStrmFullBtn');
        btn.disabled=!!s.running;
        fullBtn.disabled=!!s.running;
        btn.innerHTML=s.running?'<i data-lucide="loader-2" class="spin"></i>同步中...':'<i data-lucide="zap"></i>快速同步';
        fullBtn.innerHTML=s.running?'<i data-lucide="scan-search"></i>完整校准':'<i data-lucide="scan-search"></i>完整校准';
        window.renderLucideIcons?.(btn.parentElement);
        const shouldPoll=!!s.running||(metadataPending>0&&metadataQueue.enabled!==false);
        if(shouldPoll&&!pollTimer)pollTimer=setInterval(loadStatus,2500);
        if(!shouldPoll&&pollTimer){clearInterval(pollTimer);pollTimer=null;}
    }
    async function loadStatus(){try{const response=await fetch('/api/strm/schedule');const data=await response.json();if(!response.ok)throw new Error(data.error||'状态读取失败');renderStatus(data);}catch(error){setTag('状态读取失败','var(--warning)');document.getElementById('strmNextRun').textContent=error.message||'状态读取失败';}}
    function getDiagnosticValue(data, key){
        if(!data||!key)return 0;
        if(key in data)return data[key];
        const parts=String(key).split('.');
        let cur=data;
        for(const p of parts){
            if(cur&&typeof cur==='object'&&p in cur){
                cur=cur[p];
            }else{
                return 0;
            }
        }
        return cur;
    }
    function setDiagnosticState(message,tone='info'){
        diagnosticState.textContent=message;
        diagnosticState.dataset.tone=tone||'info';
        const banner=diagnosticState.closest('.strm-diagnostic-banner');
        if(banner)banner.dataset.tone=tone||'info';
    }
    function syncDiagnosticCleanupAction(confirmed=0){
        const available=Number(confirmed)>0;
        diagnosticCleanupBtn.classList.toggle('is-unavailable',!available);
        diagnosticCleanupBtn.setAttribute('aria-hidden',available?'false':'true');
        diagnosticCleanupBtn.disabled=!available||diagnosticLoading||diagnosticCleanupBusy;
    }
    function renderIndexDiagnostics(data,message=''){
        diagnosticSnapshot=data||{};
        document.querySelectorAll('[data-strm-diagnostic]').forEach(node=>{
            const rawVal=getDiagnosticValue(data,node.dataset.strmDiagnostic);
            const num=Number.isFinite(Number(rawVal))?Number(rawVal):0;
            if(window.MFAnim && typeof window.MFAnim.countUp === 'function'){
                window.MFAnim.countUp(node, num, {duration: 0.6});
            }else{
                node.textContent=String(num);
            }
        });
        const confirmed=Number(data.confirmed_test_artifact||0);
        const metaPending=Number(data.metadata_queue?.pending||0);
        const videoMissing=Number(data.video?.missing||0);
        const metadataMissing=Number(data.metadata?.missing||0);

        syncDiagnosticCleanupAction(confirmed);
        diagnosticResult.setAttribute('aria-busy','false');

        let defaultMsg='索引与本地输出一致，未发现可安全清理的测试残留。';
        let defaultTone='success';
        if(confirmed>0){
            defaultMsg=`确认 ${confirmed} 条隔离测试残留，可在人工确认后清理。`;
            defaultTone='warning';
        }else if(videoMissing>0){
            defaultMsg=`存在 ${videoMissing} 条 STRM 索引对应的本地 .strm 文件缺失，请检查输出目录或重新同步。`;
            defaultTone='warning';
        }else if(metaPending>0){
            defaultMsg=`元数据仍在后台排队生成（待处理 ${metaPending} 项）；这是本地生成状态，不代表云盘媒体缺失。`;
            defaultTone='info';
        }else if(metadataMissing>0){
            defaultMsg=`存在 ${metadataMissing} 条元数据索引对应的本地文件缺失，可重新执行元数据同步。`;
            defaultTone='warning';
        }
        setDiagnosticState(message||defaultMsg,message?'success':defaultTone);
        window.renderLucideIcons?.(diagnosticResult);
    }
    async function loadIndexDiagnostics(){if(diagnosticLoading)return;diagnosticLoading=true;diagnosticRefreshBtn.disabled=true;diagnosticResult.setAttribute('aria-busy','true');syncDiagnosticCleanupAction(Number(diagnosticSnapshot?.confirmed_test_artifact||0));if(diagnosticSnapshot){setDiagnosticState('后台刷新中，保留上次诊断结果…','info');}else{setDiagnosticState('正在读取索引统计…','info');}try{const response=await fetch('/api/strm/index-diagnostics');const data=await response.json();if(!response.ok)throw new Error(data.error||'诊断读取失败');renderIndexDiagnostics(data);}catch(error){diagnosticResult.setAttribute('aria-busy','false');setDiagnosticState(error.message||'诊断读取失败','error');}finally{diagnosticLoading=false;diagnosticRefreshBtn.disabled=false;syncDiagnosticCleanupAction(Number(diagnosticSnapshot?.confirmed_test_artifact||0));}}
    async function cleanupConfirmedTestIndexes(event){
        if(diagnosticCleanupBusy||!diagnosticSnapshot)return;
        const ids=[...(diagnosticSnapshot.confirmed_test_artifact_ids||[])];
        if(!ids.length){
            setDiagnosticState('当前没有可清理的测试索引。');
            return;
        }
        const confirmed=await appConfirm({trigger:event.currentTarget,title:'清理隔离测试索引',message:`将仅从数据库删除 ${ids.length} 条已二次确认的测试索引，不会删除任何文件。`,confirmText:'清理测试索引',danger:true,verifyText:'CLEAN TEST INDEX',verifyLabel:'输入 CLEAN TEST INDEX 确认清理'});
        if(!confirmed)return;
        diagnosticCleanupBusy=true;
        diagnosticCleanupBtn.disabled=true;
        diagnosticCleanupBtn.querySelector('span').textContent='清理中…';
        setDiagnosticState('正在事务内重新核验全部索引…');
        try{
            const response=await fetch('/api/strm/index-diagnostics/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:'CLEAN TEST INDEX',ids})});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'清理失败');
            renderIndexDiagnostics(data.diagnostics,`已安全删除 ${data.deleted} 条测试索引。`);
        }catch(error){
            setDiagnosticState(error.message||'清理失败','error');
        }finally{
            diagnosticCleanupBusy=false;
            diagnosticCleanupBtn.querySelector('span').textContent='清理测试索引';
            syncDiagnosticCleanupAction(Number(diagnosticSnapshot?.confirmed_test_artifact||0));
        }
    }

    function renderFailureSources(summary){const current=failureSourceFilter.value;const rows=summary.sources||[];failureSourceFilter.innerHTML='<option value="">全部来源</option>'+rows.map(row=>`<option value="${esc(row.id)}">${esc(row.name)} (${Number(row.open||0)})</option>`).join('');failureSourceFilter.value=rows.some(row=>row.id===current)?current:'';}
    function renderFailures(payload,message=''){
        failureSnapshot=Array.isArray(payload.items)?payload.items:[];
        failureSummary=payload.summary||{};
        selectedFailures=new Set([...selectedFailures].filter(id=>failureSnapshot.some(row=>row.id===id)));
        renderFailureSources(payload.summary||{});
        const openCount=Number(payload.summary?.open||0);
        const resolvedCount=Number(payload.summary?.resolved||0);
        const summaryBox=document.getElementById('strmFailureSummary');
        if(summaryBox){
            summaryBox.innerHTML=`<span class="strm-stat-badge open"><i data-lucide="alert-circle"></i>待处理 <strong>${openCount}</strong></span><span class="strm-stat-badge resolved"><i data-lucide="check-circle-2"></i>已解决 <strong>${resolvedCount}</strong></span>`;
        }

        failurePage=Number(payload.pagination?.page||failurePage||1);
        failureTotalCount=Number(payload.pagination?.total??(payload.items?payload.items.length:0));
        failureTotalPages=Number(payload.pagination?.total_pages||Math.max(1,Math.ceil(failureTotalCount/FAILURE_PAGE_SIZE)));
        if(failurePageInfo){
            failurePageInfo.textContent=failureTotalCount?`共 ${failureTotalCount} 条 · 第 ${failurePage} / ${failureTotalPages} 页 (每页 ${FAILURE_PAGE_SIZE} 条)`:'共 0 条';
        }
        if(failurePrevBtn)failurePrevBtn.disabled=failureRetrying||failureLoading||failurePage<=1;
        if(failureNextBtn)failureNextBtn.disabled=failureRetrying||failureLoading||failurePage>=failureTotalPages||!failureTotalCount;

        failureList.replaceChildren();
        if(!failureSnapshot.length){
            const emptyTr=document.createElement('tr');
            emptyTr.innerHTML='<td colspan="4" class="table-empty">当前筛选条件下没有失败记录</td>';
            failureList.append(emptyTr);
        }else{
            failureSnapshot.forEach(row=>{
                const tr=document.createElement('tr');
                const isSelected=selectedFailures.has(row.id);
                tr.className='strm-failure-row'+(isSelected?' is-selected':'');
                tr.dataset.id=String(row.id);
                const isMetadata=row.action==='metadata';
                const actionText=isMetadata?'元数据':'STRM 生成';
                const statusText=row.status==='open'?'待处理':'已解决';
                const stateClass=row.status==='open'?'paused':'running';
                const failCountTag=Number(row.failure_count||0)>1?`<span class="dl-task-tag is-danger">失败 ${Number(row.failure_count)} 次</span>`:'';
                const pathText=row.target_rel_path?`<div class="dl-task-path-text" title="${esc(row.target_rel_path)}"><i data-lucide="folder"></i>${esc(row.target_rel_path)}</div>`:'';
                const singleRetryBtn=row.status==='open'?`<button type="button" class="rss-btn" style="padding:3px 8px;height:26px;font-size:11px;" title="重试该项" data-retry-single="${Number(row.id)}"><i data-lucide="rotate-cw" style="width:13px;height:13px;"></i><span>重试</span></button>`:'';
                const singleDeleteBtn=`<button type="button" class="rss-btn is-danger" style="padding:3px 7px;height:26px;" title="清除此记录" data-delete-single="${Number(row.id)}"><i data-lucide="trash-2" style="width:13px;height:13px;"></i></button>`;

                tr.innerHTML=`<td class="dl-task-title-cell"><div class="qb-task-title-layout"><label class="qb-task-check" title="选择失败项"><input type="checkbox" value="${Number(row.id)}" ${isSelected?'checked':''}></label><div class="qb-task-copy"><div class="dl-task-main-title" title="${esc(row.filename)}">${esc(row.filename)}</div><div class="dl-task-tags"><span class="dl-task-tag">${esc(row.source_name||row.source_id)}</span><span class="dl-task-tag">${esc(actionText)}</span>${failCountTag}${pathText}</div></div></div></td><td><div class="download-attention-message" style="max-height:52px;overflow-y:auto;font-family:var(--font-mono,monospace);font-size:11px;color:var(--danger,#ef4444);" title="${esc(row.error||'未知错误')}">${esc(row.error||'未知错误')}</div></td><td style="white-space:nowrap;"><span class="status-pill ${stateClass}">${esc(statusText)}</span></td><td style="text-align:right;white-space:nowrap;"><div style="display:inline-flex;gap:4px;justify-content:flex-end;">${singleRetryBtn}${singleDeleteBtn}</div></td>`;

                const checkbox=tr.querySelector('input');
                checkbox.disabled=row.status!=='open'||failureRetrying;
                checkbox.addEventListener('change',()=>{
                    if(checkbox.checked)selectedFailures.add(row.id);
                    else selectedFailures.delete(row.id);
                    tr.classList.toggle('is-selected',checkbox.checked);
                    syncFailureActions();
                });
                const singleRetry=tr.querySelector('[data-retry-single]');
                if(singleRetry){
                    singleRetry.addEventListener('click',()=>retrySingleFailure(row.id));
                }
                const singleDelete=tr.querySelector('[data-delete-single]');
                if(singleDelete){
                    singleDelete.addEventListener('click',()=>deleteSingleFailure(row.id));
                }
                failureList.append(tr);
            });
        }
        window.renderLucideIcons?.(failureList);
        failureList.setAttribute('aria-busy','false');
        failureState.textContent=message||'失败项只保存稳定标识与脱敏错误；重试会重新读取云端状态。';
        syncFailureActions();
    }
    function syncFailureActions(){
        const selectableRows=failureSnapshot.filter(row=>row.status==='open');
        const openCount=selectableRows.length;
        const totalCount=failureSnapshot.length;
        const selectedCount=selectedFailures.size;

        failureList.querySelectorAll('input[type="checkbox"]').forEach(checkbox=>{
            const row=failureSnapshot.find(item=>Number(item.id)===Number(checkbox.value));
            checkbox.disabled=failureRetrying||!row||row.status!=='open';
            const rowEl=typeof checkbox?.closest==='function'?checkbox.closest('.strm-failure-row'):null;
            if(rowEl&&typeof rowEl.classList?.toggle==='function')rowEl.classList.toggle('is-selected',checkbox.checked);
        });

        if(selectAllCheckbox){
            if(selectableRows.length===0){
                selectAllCheckbox.checked=false;
                selectAllCheckbox.indeterminate=false;
                selectAllCheckbox.disabled=true;
            }else{
                selectAllCheckbox.disabled=failureRetrying||failureLoading;
                const selectableSelected=selectableRows.filter(r=>selectedFailures.has(r.id)).length;
                if(selectableSelected===selectableRows.length){
                    selectAllCheckbox.checked=true;
                    selectAllCheckbox.indeterminate=false;
                }else if(selectableSelected>0){
                    selectAllCheckbox.checked=false;
                    selectAllCheckbox.indeterminate=true;
                }else{
                    selectAllCheckbox.checked=false;
                    selectAllCheckbox.indeterminate=false;
                }
            }
        }

        retrySelectedBtn.disabled=failureRetrying||selectedCount===0;
        retryAllBtn.disabled=failureRetrying||openCount===0||failureStatusFilter.value==='resolved';
        if(clearFailuresBtn){
            clearFailuresBtn.disabled=failureRetrying||failureLoading||totalCount===0;
        }

        if(selectedFailuresCount){
            selectedFailuresCount.textContent=String(selectedCount);
            selectedFailuresCount.style.display=selectedCount>0?'inline-flex':'none';
        }
        if(failureSelectionStats){
            failureSelectionStats.textContent=`已选 ${selectedCount} 项 / 本页 ${totalCount} 条`;
        }
        if(failurePrevBtn)failurePrevBtn.disabled=failureRetrying||failureLoading||failurePage<=1;
        if(failureNextBtn)failureNextBtn.disabled=failureRetrying||failureLoading||failurePage>=failureTotalPages||!failureTotalCount;
    }
    async function loadFailures(targetPage=failurePage){
        const generation=++failureRequestGeneration;
        if(failureAbortController)failureAbortController.abort();
        const controller=typeof AbortController==='function'?new AbortController():null;
        failureAbortController=controller;
        failureLoading=true;
        failureList.setAttribute('aria-busy','true');
        failurePage=Math.max(1,Number(targetPage||1));
        if(failurePrevBtn)failurePrevBtn.disabled=true;
        if(failureNextBtn)failureNextBtn.disabled=true;
        failureState.textContent=failureSnapshot.length?'后台刷新中，保留上次失败列表…':'正在读取失败台账…';
        const query=new URLSearchParams({
            status:failureStatusFilter.value,
            page:String(failurePage),
            page_size:String(FAILURE_PAGE_SIZE),
        });
        if(failureSourceFilter.value)query.set('source_id',failureSourceFilter.value);
        if(failureActionFilter.value)query.set('action',failureActionFilter.value);
        try{
            const response=await fetch(`/api/strm/failures?${query}`,controller?{signal:controller.signal}:undefined);
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'失败台账读取失败');
            if(generation!==failureRequestGeneration)return;
            renderFailures(data);
        }catch(error){
            if(generation!==failureRequestGeneration||error?.name==='AbortError')return;
            failureList.setAttribute('aria-busy','false');
            failureState.textContent=error.message||'失败台账读取失败';
        }finally{
            if(generation===failureRequestGeneration){
                failureLoading=false;
                if(failureAbortController===controller)failureAbortController=null;
                syncFailureActions();
            }
        }
    }
    async function retryFailures(all){if(failureRetrying)return;const ids=[...selectedFailures];if(!all&&!ids.length)return;const confirmed=await appConfirm({title:all?'重试当前全部失败项':'重试选中失败项',message:'将重新解析云端文件状态并仅执行失败动作，不复用历史签名直链。',confirmText:'开始重试'});if(!confirmed)return;failureRetrying=true;syncFailureActions();failureState.textContent='正在重新解析云端状态并重试…';const body=all?{all:true,source_id:failureSourceFilter.value,action:failureActionFilter.value}:{ids};try{const response=await fetch('/api/strm/failures/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok||data.ok===false)throw new Error(data.error||'重试失败');selectedFailures.clear();await loadFailures();const deferred=Number(data.deferred||0);failureState.textContent=`重试完成：解决 ${Number(data.resolved||0)}，仍失败 ${Number(data.failed||0)}${deferred?`，扫描未完成暂缓 ${deferred}`:''}，当前剩余 ${Number(data.remaining||0)}`;}catch(error){failureState.textContent=error.message||'重试失败';}finally{failureRetrying=false;syncFailureActions();}}
    async function clearFailures(){
        if(failureRetrying||failureLoading)return;
        const ids=[...selectedFailures];
        let title='清除失败记录';
        let message='确定从失败台账中清除失败记录吗？这不会影响云盘文件或本地媒体库。';
        let body={};
        if(ids.length>0){
            title=`清除 ${ids.length} 条选中记录`;
            message=`将从失败台账中移除已选中的 ${ids.length} 条记录。不会删除云盘中的任何文件。`;
            body={ids};
        }else if(failureSnapshot.length>0){
            title='清除当前筛选记录';
            message=`确定清除当前筛选条件下的全部 ${failureSnapshot.length} 条失败记录吗？不会删除云盘中的任何文件。`;
            body={all:true,source_id:failureSourceFilter.value,action:failureActionFilter.value,status:failureStatusFilter.value};
        }else{
            return;
        }

        const confirmed=await appConfirm({
            title,
            message,
            confirmText:'确认清除',
            danger:true,
        });
        if(!confirmed)return;

        failureState.textContent='正在清除台账记录…';
        try{
            const response=await fetch('/api/strm/failures/clear',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify(body),
            });
            const data=await response.json();
            if(!response.ok||data.ok===false)throw new Error(data.error||'清除失败');
            selectedFailures.clear();
            await loadFailures();
            failureState.textContent=`已成功清除 ${Number(data.deleted||0)} 条失败记录。`;
        }catch(error){
            failureState.textContent=error.message||'清除失败';
        }
    }
    async function deleteSingleFailure(id){
        if(failureRetrying||failureLoading)return;
        const row=failureSnapshot.find(item=>Number(item.id)===Number(id));
        const confirmed=await appConfirm({
            title:'删除失败记录',
            message:`确定从台账中删除「${row?.filename||id}」的记录吗？`,
            confirmText:'删除记录',
            danger:true,
        });
        if(!confirmed)return;
        try{
            const response=await fetch('/api/strm/failures/clear',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({ids:[Number(id)]}),
            });
            const data=await response.json();
            if(!response.ok||data.ok===false)throw new Error(data.error||'删除失败');
            selectedFailures.delete(Number(id));
            await loadFailures();
            failureState.textContent='记录已删除。';
        }catch(error){
            failureState.textContent=error.message||'删除失败';
        }
    }
    async function retrySingleFailure(id){
        if(failureRetrying)return;
        selectedFailures.clear();
        selectedFailures.add(Number(id));
        await retryFailures(false);
    }
    async function validateCron(){const value=cron.value.trim();const box=document.getElementById('cronPreview');if(!value){box.innerHTML='';box.style.display='none';return;}try{const response=await fetch('/api/strm/schedule/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cron:value})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Cron 校验失败');box.style.display='inline-flex';if(data.valid){box.style.color='var(--accent)';box.style.borderColor='var(--accent-border)';box.style.background='var(--accent-soft)';box.innerHTML=`<i data-lucide="clock" style="width:13px;height:13px;"></i><span>下次运行：${esc(data.next_run)}</span>`;}else{box.style.color='var(--danger)';box.style.borderColor='var(--danger-border)';box.style.background='var(--danger-soft)';box.innerHTML=`<i data-lucide="alert-circle" style="width:13px;height:13px;"></i><span>格式无效：需要 5 段 cron 表达式</span>`;}}catch(error){box.style.display='inline-flex';box.style.color='var(--danger)';box.style.borderColor='var(--danger-border)';box.style.background='var(--danger-soft)';box.textContent=error.message||'Cron 校验失败';}window.renderLucideIcons?.(box);}

    loadAppConfig().then(cfg=>{
        fillConfigFields(form,cfg);
        if(!cfg.STRM_SCHEDULE_CRON)cron.value='0 4 * * *';
        if(!cfg.STRM_NOTIFY_ENABLED)form.querySelector('[data-key="STRM_NOTIFY_ENABLED"]').checked=true;

        // Init Tag Inputs
        videoTagInput.setTagsFromString(cfg.STRM_VIDEO_EXTS || 'mkv,mp4,ts,m2ts');
        metadataTagInput.setTagsFromString(cfg.STRM_METADATA_EXTS || 'nfo,srt,ass,jpg,png');

        const canonical=String(cfg.GY_STRM_SOURCE_DIRS??'').trim();
        try{sources=canonical?JSON.parse(canonical):[];}catch(_){sources=[];}
        if(!Array.isArray(sources))sources=[];
        const seen=new Set();sources=sources.map((item,index)=>typeof item==='string'?{id:item,name:`源目录${index+1}`}:item).filter(item=>item&&String(item.id||'').trim()&&String(item.id)!=='0'&&!seen.has(String(item.id))&&seen.add(String(item.id))).map((item,index)=>({id:String(item.id),name:String(item.name||`源目录${index+1}`)}));
        renderSources();
        syncMetadata();
        strmConfigReady=true;
        void ensureStrmTabLoaded(activeStrmTab);
    });

    strmBaseUrlDetectBtn.addEventListener('click',loadStrmBaseUrlCandidates);
    strmBaseUrlCandidates.addEventListener('change',()=>{
        if(!strmBaseUrlCandidates.value)return;
        strmBaseUrlInput.value=strmBaseUrlCandidates.value;
        strmBaseUrlInput.dispatchEvent(new Event('input',{bubbles:true}));
        const saveState=document.getElementById('strmSaveState');
        saveState.className='is-dirty';saveState.textContent='播放地址已选择，等待保存';
        strmBaseUrlState.dataset.tone='success';strmBaseUrlState.textContent='已填入候选地址；保存后下次同步会更新 STRM 播放链接。';
    });

    document.getElementById('addStrmSourceBtn').addEventListener('click',()=>openGuangYaDirectoryPicker({modalId:'strmStandaloneDirModal',title:'批量选择 STRM 源目录',multiple:true,selected:sources,allowRoot:false,onSelect:dirs=>{sources=dirs.map(item=>({id:item.id,name:item.name}));renderSources();}}));
    metadata.addEventListener('change',syncMetadata);

    // Preset Chip Click Events
    document.querySelectorAll('[data-ext-preset]').forEach(btn=>btn.addEventListener('click',()=>metadataTagInput.setTagsFromString(btn.dataset.extPreset)));
    document.querySelectorAll('[data-video-ext-preset]').forEach(btn=>btn.addEventListener('click',()=>videoTagInput.setTagsFromString(btn.dataset.videoExtPreset)));

    let debounce;cron.addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(validateCron,350);});
    document.getElementById('refreshStrmStatusBtn').addEventListener('click',loadStatus);
    diagnosticRefreshBtn.addEventListener('click',loadIndexDiagnostics);
    diagnosticCleanupBtn.addEventListener('click',cleanupConfirmedTestIndexes);
    document.getElementById('refreshStrmFailuresBtn').addEventListener('click',()=>loadFailures(failurePage));
    [failureSourceFilter,failureActionFilter,failureStatusFilter].forEach(node=>node.addEventListener('change',()=>{failurePage=1;loadFailures(1);}));
    retrySelectedBtn.addEventListener('click',()=>retryFailures(false));
    retryAllBtn.addEventListener('click',()=>retryFailures(true));
    if(failurePrevBtn)failurePrevBtn.addEventListener('click',()=>{if(failurePage>1)loadFailures(failurePage-1);});
    if(failureNextBtn)failureNextBtn.addEventListener('click',()=>{if(failurePage<failureTotalPages)loadFailures(failurePage+1);});
    if(clearFailuresBtn)clearFailuresBtn.addEventListener('click',clearFailures);
    if(selectAllCheckbox){
        selectAllCheckbox.addEventListener('change',()=>{
            const selectableRows=failureSnapshot.filter(row=>row.status==='open');
            if(selectAllCheckbox.checked){
                selectableRows.forEach(row=>selectedFailures.add(row.id));
            }else{
                selectableRows.forEach(row=>selectedFailures.delete(row.id));
            }
            failureList.querySelectorAll('input[type="checkbox"]').forEach(checkbox=>{
                const id=Number(checkbox.value);
                const row=failureSnapshot.find(item=>Number(item.id)===id);
                if(row&&row.status==='open'){
                    checkbox.checked=selectedFailures.has(id);
                    const rowEl=typeof checkbox?.closest==='function'?checkbox.closest('.strm-failure-row'):null;
                    if(rowEl&&typeof rowEl.classList?.toggle==='function')rowEl.classList.toggle('is-selected',checkbox.checked);
                }
            });
            syncFailureActions();
        });
    }
    document.getElementById('runStrmNowBtn').addEventListener('click',async event=>{const confirmed=await appConfirm({trigger:event.currentTarget,title:'快速同步 STRM',message:'仅处理整理链路登记的待同步变化；若快照不一致会提示执行完整校准，不会自动扫描全库或清理失效文件。',confirmText:'快速同步'});if(!confirmed)return;fetch('/api/strm/run/fast',{method:'POST'}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||'启动失败');loadStatus();}).catch(error=>appAlert({type:'error',title:'启动失败',message:error.message||'无法启动快速同步'}));});
    document.getElementById('runStrmFullBtn').addEventListener('click',async event=>{const confirmed=await appConfirm({trigger:event.currentTarget,title:'完整校准 STRM',message:'将按当前并发设置（默认 15 个扫描线程）遍历全部已配置目录，校准 STRM、执行安全清理，并按实际文件变化刷新媒体库。',confirmText:'开始完整校准'});if(!confirmed)return;fetch('/api/strm/run/full',{method:'POST'}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||'启动失败');loadStatus();}).catch(error=>appAlert({type:'error',title:'启动失败',message:error.message||'无法启动完整校准'}));});
    document.getElementById('saveStrmBtn').addEventListener('click',async event=>{const btn=event.currentTarget;const state=document.getElementById('strmSaveState');btn.disabled=true;state.textContent='保存中...';try{await saveAppConfig(form);state.className='is-success';state.textContent='STRM 设置已保存';validateCron();loadStatus();}catch(error){state.className='is-error';state.textContent=error.message;}finally{btn.disabled=false;}});
    if(globalThis.__MEDIAFLUX_STRM_TEST_HOOK__)globalThis.__MEDIAFLUX_STRM_TEST_API__={renderFailures,retryFailures,loadFailures,clearFailures,renderStatus,snapshot:()=>failureSnapshot};
})();
