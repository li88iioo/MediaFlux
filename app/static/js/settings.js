(function(){
    const form=document.getElementById('settingsForm');
    const tabs=[...document.querySelectorAll('[data-settings-target]')];
    const panels=[...document.querySelectorAll('[data-settings-panel]')];
    const lockModal=createAppModal(document.getElementById('tmdbLocksModal'));
    const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const INDEXER_SITE_ORDER=['nyaa','mikan','btbtla','1lou','animetosho','tpb','sukebei'];
    const DEFAULT_INDEXER_SITES=INDEXER_SITE_ORDER.slice(0,6);
    const indexerSiteBox=form.querySelector('[data-indexer-site-box]');
    const indexerSiteField=indexerSiteBox?.querySelector('[data-key="INDEXER_ENABLED_SITES"]');
    const indexerSiteInputs=[...(indexerSiteBox?.querySelectorAll('[data-indexer-site]')||[])];
    const resourceResultsToggle=form.querySelector('[data-key="DISCOVERY_RESOURCE_RESULTS_ENABLED"]');
    const indexerSearchToggle=form.querySelector('[data-key="INDEXER_SEARCH_ENABLED"]');

    function syncIndexerSiteSelection(){
        if(!indexerSiteField)return;
        const selected=new Set(indexerSiteInputs.filter(input=>input.checked).map(input=>input.dataset.indexerSite));
        indexerSiteField.value=INDEXER_SITE_ORDER.filter(site=>selected.has(site)).join(',');
    }
    function syncIndexerSiteAvailability(){
        const enabled=Boolean(resourceResultsToggle?.checked&&indexerSearchToggle?.checked);
        if(indexerSiteBox){
            indexerSiteBox.hidden=!enabled;
            indexerSiteBox.setAttribute('aria-hidden',enabled?'false':'true');
        }
        indexerSiteInputs.forEach(input=>{input.disabled=!enabled;});
    }
    function loadIndexerSiteSelection(config){
        const configured=String(config.INDEXER_ENABLED_SITES||'').split(',').map(value=>value.trim().toLowerCase()).filter(Boolean);
        const selected=new Set(configured.length?configured:DEFAULT_INDEXER_SITES);
        if(['1','true','yes','on'].includes(String(config.INDEXER_SUKEBEI_ENABLED||'').toLowerCase()))selected.add('sukebei');
        indexerSiteInputs.forEach(input=>{input.checked=selected.has(input.dataset.indexerSite);});
        syncIndexerSiteSelection();
        syncIndexerSiteAvailability();
    }
    indexerSiteInputs.forEach(input=>input.addEventListener('change',syncIndexerSiteSelection));
    resourceResultsToggle?.addEventListener('change',syncIndexerSiteAvailability);
    indexerSearchToggle?.addEventListener('change',syncIndexerSiteAvailability);

    function activate(target){
        let activeTab=null;
        tabs.forEach(button=>{const active=button.dataset.settingsTarget===target;button.classList.toggle('active',active);button.setAttribute('aria-selected',active?'true':'false');button.tabIndex=active?0:-1;if(active)activeTab=button;});
        panels.forEach(panel=>{const active=panel.dataset.settingsPanel===target;panel.hidden=!active;panel.classList.toggle('active',active);});
        history.replaceState(null,'',`#${target}`);
        if(activeTab&&window.matchMedia('(max-width: 760px)').matches){requestAnimationFrame(()=>activeTab.scrollIntoView({block:'nearest',inline:'center'}));}
    }
    tabs.forEach((button,index)=>{
        button.addEventListener('click',()=>activate(button.dataset.settingsTarget));
        button.addEventListener('keydown',(event)=>{
            let nextIndex=null;
            if(event.key==='ArrowRight'||event.key==='ArrowDown')nextIndex=(index+1)%tabs.length;
            else if(event.key==='ArrowLeft'||event.key==='ArrowUp')nextIndex=(index-1+tabs.length)%tabs.length;
            else if(event.key==='Home')nextIndex=0;
            else if(event.key==='End')nextIndex=tabs.length-1;
            if(nextIndex===null)return;
            event.preventDefault();
            const next=tabs[nextIndex];
            activate(next.dataset.settingsTarget);
            next.focus();
        });
    });
    const initial=window.__mediafluxInitialSettingsTarget||location.hash.slice(1);
    activate(tabs.some(button=>button.dataset.settingsTarget===initial)?initial:'console');

    const saveButtons=[...document.querySelectorAll('[data-save-settings]')];
    let configReady=false;
    form.setAttribute('aria-busy','true');

    function setConfigReady(){
        configReady=true;
        form.setAttribute('aria-busy','false');
        saveButtons.forEach(button=>{
            button.disabled=false;
            button.setAttribute('aria-disabled','false');
            const state=button.closest('[data-settings-panel]')?.querySelector('[data-settings-state]');
            if(!state)return;
            state.className='';
            state.textContent=state.dataset.readyMessage||'仅保存当前分区';
        });
    }

    function setConfigLoadError(){
        form.setAttribute('aria-busy','false');
        saveButtons.forEach(button=>{
            button.disabled=true;
            button.setAttribute('aria-disabled','true');
            const state=button.closest('[data-settings-panel]')?.querySelector('[data-settings-state]');
            if(!state)return;
            state.className='is-error';
            state.textContent='配置读取失败，请刷新页面后重试';
        });
    }

    loadAppConfig().then(config=>{
        fillConfigFields(form,config);
        const configDefaults={TG_NOTIFICATION_ENABLED:'1',TG_NOTIFICATION_LEVEL:'standard',AGENT_ENABLED:'0',LOGIN_WALLPAPER_MODE:'default',DISCOVERY_CACHE_TTL_SECONDS:'21600',DISCOVERY_STALE_TTL_SECONDS:'604800',DISCOVERY_DOUBAN_ENABLED:'1',DISCOVERY_RESOURCE_RESULTS_ENABLED:'1',INDEXER_SEARCH_ENABLED:'1',INDEXER_BTBTLA_MIN_INTERVAL_SECONDS:'5',INDEXER_1LOU_MIN_INTERVAL_SECONDS:'5',INDEXER_1LOU_GOOGLE_ENABLED:'1',DOUBAN_CACHE_TTL_SECONDS:'21600',AI_RECOGNITION_ENABLED:'0',AI_RECOGNITION_CONFIDENCE_THRESHOLD:'0.8',AI_RECOGNITION_REQUESTS_PER_MINUTE:'6',AI_RECOGNITION_DAILY_REQUEST_LIMIT:'100',AI_RECOGNITION_MAX_CONCURRENCY:'2',AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS:'60',ORGANIZE_TAVILY_HINTS_ENABLED:'0',ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT:'20',TMDB_MATCH_MODE:'strict',WEB_SEARCH_ENABLED:'0',TAVILY_SEARCH_DEPTH:'basic',TAVILY_MAX_RESULTS:'5',TAVILY_CACHE_TTL_SECONDS:'900',TAVILY_DAILY_CREDIT_LIMIT:'100',TAVILY_TIMEOUT_SECONDS:'10',AGENT_LLM_ENABLED:'0',AGENT_LLM_PROTOCOL:'auto',AGENT_LLM_TIMEOUT_SECONDS:'12',AGENT_LLM_REQUESTS_PER_MINUTE:'6',AGENT_LIBRARY_PATROL_ENABLED:'0',AGENT_LIBRARY_PATROL_NOTIFY_ENABLED:'0',AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED:'1',AGENT_LIBRARY_PATROL_INTERVAL_HOURS:'24',AGENT_LIBRARY_PATROL_MAX_SERIES:'50'};
        Object.entries(configDefaults).forEach(([key,value])=>{if(config[key])return;const field=form.querySelector(`[data-key="${key}"]`);if(!field)return;if(field.type==='checkbox')field.checked=value==='1';else field.value=value;});
        loadIndexerSiteSelection(config);
        setConfigReady();
        syncTelemetryWidgets();
    }).catch(setConfigLoadError);

    saveButtons.forEach(button=>button.addEventListener('click',async()=>{
        if(!configReady)return;
        const panel=button.closest('[data-settings-panel]');
        const state=panel.querySelector('[data-settings-state]');
        button.disabled=true;
        button.setAttribute('aria-disabled','true');
        button.classList.add('is-saving');
        button.setAttribute('aria-busy','true');
        state.className='';
        state.textContent='正在保存当前分区...';
        try{
            await saveAppConfig(panel);
            state.className='is-success';
            state.textContent='当前分区已保存';
        }
        catch(error){state.className='is-error';state.textContent=error.message;}
        finally{
            button.disabled=!configReady;
            button.classList.remove('is-saving');
            button.setAttribute('aria-busy','false');
            button.setAttribute('aria-disabled',configReady?'false':'true');
        }
    }));

    const agentModelState=document.getElementById('agentModelState');
    const agentModelCapabilities=document.getElementById('agentModelCapabilities');
    const fetchAgentModelsButton=document.getElementById('fetchAgentModelsBtn');
    const testAgentModelButton=document.getElementById('testAgentModelBtn');
    const openAgentModelPickerButton=document.getElementById('openAgentModelPickerBtn');
    const agentModelPickerModalElem=document.getElementById('agentModelPickerModal');
    const agentModelPickerModal=createAppModal(agentModelPickerModalElem);
    const agentModelPickerList=document.getElementById('agentModelPickerList');
    const agentModelSearchInput=document.getElementById('agentModelSearchInput');
    const agentModelCountBadge=document.getElementById('agentModelCountBadge');
    const agentModelInput=document.getElementById('agentLlmModel');
    let cachedAgentModels=[];
    const agentProtocolLabels={responses:'Responses API',chat_completions:'Chat Completions',anthropic_messages:'Anthropic Messages'};

    function renderAgentModelPickerList(filterText=''){
        if(!agentModelPickerList)return;
        const currentModel=(agentModelInput?.value||'').trim();
        const search=filterText.trim().toLowerCase();
        let models=cachedAgentModels;
        if(!models.length && currentModel){
            models=[currentModel];
        }
        const filtered=search ? models.filter(m=>m.toLowerCase().includes(search)) : models;
        if(agentModelCountBadge){
            agentModelCountBadge.textContent=search ? `匹配 ${filtered.length} / 共 ${models.length} 个` : `共 ${models.length} 个模型`;
        }
        if(!filtered.length){
            agentModelPickerList.innerHTML=`<div class="agent-model-picker-empty">${models.length ? '未找到匹配的模型' : '暂无可用模型，请先点击「获取模型」或直接在输入框中填写'}</div>`;
            return;
        }
        agentModelPickerList.innerHTML='';
        filtered.forEach(modelName=>{
            const isSelected=modelName===currentModel;
            const item=document.createElement('button');
            item.type='button';
            item.className='agent-model-picker-item'+(isSelected?' is-selected':'');
            item.setAttribute('role','option');
            item.setAttribute('aria-selected',isSelected?'true':'false');
            item.innerHTML=`<span class="agent-model-name">${escapeHtml(modelName)}</span><span class="agent-model-picker-item-icon"><i data-lucide="${isSelected?'check':'chevron-right'}"></i></span>`;
            item.addEventListener('click',()=>{
                if(agentModelInput){
                    agentModelInput.value=modelName;
                    agentModelInput.dispatchEvent(new Event('input',{bubbles:true}));
                    agentModelInput.dispatchEvent(new Event('change',{bubbles:true}));
                }
                agentModelPickerModal.close();
                if(window.showToast) window.showToast(`已选择模型: ${modelName}`, 'info', 2000);
            });
            agentModelPickerList.appendChild(item);
        });
        window.renderLucideIcons?.(agentModelPickerList);
    }

    openAgentModelPickerButton?.addEventListener('click',()=>{
        if(agentModelSearchInput) agentModelSearchInput.value='';
        renderAgentModelPickerList('');
        agentModelPickerModal.open();
        setTimeout(()=>{ agentModelSearchInput?.focus(); }, 120);
    });

    agentModelSearchInput?.addEventListener('input', (e)=>{
        renderAgentModelPickerList(e.target.value);
    });

    function setAgentModelState(message,tone='idle'){
        agentModelState.textContent=message;
        agentModelState.dataset.tone=tone;
    }
    function renderAgentModelCapabilities(capabilities=null,{testing=false}={}){
        if(!agentModelCapabilities)return;
        agentModelCapabilities.dataset.visible=(capabilities||testing)?'true':'false';
        agentModelCapabilities.setAttribute('aria-hidden',(capabilities||testing)?'false':'true');
        agentModelCapabilities.querySelectorAll('[data-capability]').forEach(item=>{
            const key=item.dataset.capability;
            item.dataset.supported=testing?'pending':(capabilities?.[key]===true?'true':'false');
        });
    }
    function setAgentModelActionsBusy(activeButton,busy){
        [fetchAgentModelsButton,testAgentModelButton,openAgentModelPickerButton].forEach(button=>{if(button) button.disabled=busy;});
        activeButton.classList.toggle('is-loading',busy);
        activeButton.setAttribute('aria-busy',busy?'true':'false');
    }
    function getAgentModelDraft(){
        const panel=form.querySelector('[data-settings-panel="agent"]');
        return {
            base_url:panel.querySelector('[data-key="AGENT_LLM_API_URL"]').value.trim(),
            api_key:panel.querySelector('[data-key="AGENT_LLM_API_KEY"]').value,
            protocol:panel.querySelector('[data-key="AGENT_LLM_PROTOCOL"]').value,
            model:panel.querySelector('[data-key="AGENT_LLM_MODEL"]').value.trim(),
            timeout_seconds:Number(panel.querySelector('[data-key="AGENT_LLM_TIMEOUT_SECONDS"]').value),
        };
    }
    fetchAgentModelsButton?.addEventListener('click',async()=>{
        const draft=getAgentModelDraft();
        if(!draft.base_url){setAgentModelState('请先填写 API Base URL','error');form.querySelector('[data-key="AGENT_LLM_API_URL"]').focus();return;}
        setAgentModelActionsBusy(fetchAgentModelsButton,true);setAgentModelState('正在读取 Provider 模型列表…','testing');
        try{
            const response=await fetch('/api/tools/ai/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base_url:draft.base_url,api_key:draft.api_key,protocol:draft.protocol})});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'模型列表读取失败');
            cachedAgentModels=Array.isArray(data.models)?data.models:[];
            const list=document.getElementById('agentLlmModelOptions');
            if(list) list.replaceChildren(...cachedAgentModels.map(name=>{const option=document.createElement('option');option.value=name;return option;}));
            setAgentModelState(`已读取 ${cachedAgentModels.length} 个模型${data.protocol?' · '+(agentProtocolLabels[data.protocol]||data.protocol):''}`,'success');
            if(cachedAgentModels.length>0){
                if(window.showToast) window.showToast(`已获取 ${cachedAgentModels.length} 个可用模型，请点选`, 'success', 2500);
                if(agentModelSearchInput) agentModelSearchInput.value='';
                renderAgentModelPickerList('');
                agentModelPickerModal.open();
            }
        }catch(error){setAgentModelState(error.message||'模型列表读取失败','error');}
        finally{setAgentModelActionsBusy(fetchAgentModelsButton,false);}
    });
    testAgentModelButton?.addEventListener('click',async()=>{
        const draft=getAgentModelDraft();
        if(!draft.base_url){setAgentModelState('请先填写 API Base URL','error');form.querySelector('[data-key="AGENT_LLM_API_URL"]').focus();return;}
        if(!draft.model){setAgentModelState('请先输入或选择模型名称','error');document.getElementById('agentLlmModel').focus();return;}
        setAgentModelActionsBusy(testAgentModelButton,true);setAgentModelState('正在验证结构化输出、工具调用与流式输出…','testing');renderAgentModelCapabilities(null,{testing:true});
        try{
            const response=await fetch('/api/tools/ai/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(draft)});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'模型连接测试失败');
            const protocol=agentProtocolLabels[data.protocol]||data.protocol||'模型服务';
            const capabilities=data.capabilities&&typeof data.capabilities==='object'?data.capabilities:{structured_output:true,tool_calling:false,streaming:false};
            renderAgentModelCapabilities(capabilities);
            const fullyCapable=capabilities.structured_output===true&&capabilities.tool_calling===true&&capabilities.streaming===true;
            setAgentModelState(
                fullyCapable
                    ? `全功能可用 · ${protocol} · ${data.elapsed_ms||0} ms`
                    : `连接正常，但部分 Agent 能力不可用 · ${protocol} · ${data.elapsed_ms||0} ms`,
                fullyCapable?'success':'warning'
            );
        }catch(error){renderAgentModelCapabilities(null);setAgentModelState(error.message||'模型连接测试失败','error');}
        finally{setAgentModelActionsBusy(testAgentModelButton,false);}
    });

    const testQbButton=document.getElementById('testQbBtn');
    const qbConnectionState=document.getElementById('qbConnectionState');
    const qbConnectionLabel=qbConnectionState?.querySelector('[data-qb-state-label]');
    function setQbConnectionState(tone,label,detail=label,compactLabel=label){
        if(!qbConnectionState)return;
        qbConnectionState.dataset.tone=tone;
        qbConnectionState.className='telemetry-status-badge'+(tone==='idle'?' is-idle':(tone==='error'?' is-error':''));
        if(tone==='success'){
            qbConnectionState.innerHTML='<span class="telemetry-dot"></span>已连通';
        } else if(tone==='testing'){
            qbConnectionState.textContent='测试中…';
        } else if(tone==='error'){
            qbConnectionState.textContent='连接失败';
        } else {
            qbConnectionState.textContent=compactLabel||label;
        }
        qbConnectionState.title=detail;
        const diag=document.getElementById('qbDiagnosticText');
        if(diag){
            if(tone==='success'){
                diag.textContent=label.replace(/^已连接\s*·\s*/,'');
            } else if(tone==='testing'){
                diag.textContent='验证通信中…';
            } else if(tone==='error'){
                diag.textContent=label;
            } else {
                diag.textContent='尚未发起';
            }
            diag.title=detail;
        }
    }
    function qbSecretDraft(field){
        const state=field.dataset.secretState||'empty';
        if(state==='saved'&&!field.value)return '********';
        if(state==='clear'||state==='empty')return '';
        return field.value;
    }
    testQbButton?.addEventListener('click',async()=>{
        const panel=form.querySelector('[data-settings-panel="downloads"]');
        const urlField=panel.querySelector('[data-key="QB_URL"]');
        const usernameField=panel.querySelector('[data-key="QB_USERNAME"]');
        const passwordField=panel.querySelector('[data-key="QB_PASSWORD"]');
        const apiKeyField=panel.querySelector('[data-key="QB_API_KEY"]');
        if(!urlField.value.trim()){
            setQbConnectionState('error','请填写 WebUI 地址','请先填写 qB WebUI 地址','缺少地址');
            urlField.focus();
            return;
        }
        testQbButton.disabled=true;
        testQbButton.classList.add('is-loading');
        testQbButton.setAttribute('aria-busy','true');
        setQbConnectionState('testing','连接测试中…','正在验证网络、认证与 Web API','测试中…');
        try{
            const response=await fetch('/api/downloads/qb/test',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    url:urlField.value.trim(),
                    username:usernameField.value.trim(),
                    password:qbSecretDraft(passwordField),
                    api_key:qbSecretDraft(apiKeyField),
                }),
            });
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'qBittorrent 连接测试失败');
            const auth=data.auth_mode==='api_key'?'API Key':'密码';
            const version=String(data.app_version||'未知版本').replace(/^v/i,'');
            const latency=data.latency_ms||0;
            const versionLabel=version==='未知版本'?version:`v${version}`;
            setQbConnectionState('success',`已连接 · ${versionLabel} · ${auth} · ${latency} ms`,`qBittorrent ${versionLabel} · ${auth}认证 · 延迟 ${latency} ms`,`已连接 · ${latency} ms`);
        }catch(error){
            const message=error.message||'qBittorrent 连接测试失败';
            setQbConnectionState('error',message,message,'连接失败');
        }finally{
            testQbButton.disabled=false;
            testQbButton.classList.remove('is-loading');
            testQbButton.setAttribute('aria-busy','false');
        }
    });

    const tgConnectionBadge=document.getElementById('tgConnectionBadge');
    document.getElementById('testTelegramBtn').addEventListener('click',async()=>{
        const panel=form.querySelector('[data-settings-panel="telegram"]');const state=panel.querySelector('[data-settings-state]');const button=document.getElementById('testTelegramBtn');button.disabled=true;state.className='';state.textContent='正在发送测试消息...';
        if(tgConnectionBadge){tgConnectionBadge.className='telemetry-status-badge';tgConnectionBadge.dataset.tone='testing';tgConnectionBadge.textContent='测试中…';}
        try{
            const response=await fetch('/api/telegram/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:panel.querySelector('[data-key="TG_BOT_TOKEN"]').value,chat_id:panel.querySelector('[data-key="TG_CHAT_ID"]').value})});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'发送失败');
            state.className='is-success';
            state.textContent=data.status||'测试消息已发送';
            if(tgConnectionBadge){tgConnectionBadge.className='telemetry-status-badge';delete tgConnectionBadge.dataset.tone;tgConnectionBadge.innerHTML='<span class="telemetry-dot"></span>已连通';}
        }
        catch(error){
            state.className='is-error';
            state.textContent=error.message;
            if(tgConnectionBadge){tgConnectionBadge.className='telemetry-status-badge is-error';tgConnectionBadge.dataset.tone='error';tgConnectionBadge.textContent='测试失败';}
        }
        finally{button.disabled=false;}
    });

    function syncTelemetryWidgets(){
        const localUrl=document.getElementById('telemetryLocalUrl');
        const endpointLabel=document.getElementById('telemetryEndpointLabel');
        const firewall=document.getElementById('telemetryFirewallScope');
        const copyBtn=document.getElementById('copyLocalUrlBtn');
        if(localUrl)localUrl.textContent=window.location.origin;
        if(endpointLabel)endpointLabel.textContent='当前访问端点';
        if(firewall)firewall.textContent='由 Docker 发布配置控制';
        if(copyBtn){
            copyBtn.title='复制当前访问地址';
            copyBtn.setAttribute('aria-label','复制当前访问地址');
        }

        const tgChatId=form.querySelector('[data-key="TG_CHAT_ID"]')?.value?.trim();
        const tgChatPreview=document.getElementById('tgChatIdPreview');
        if(tgChatPreview)tgChatPreview.textContent=tgChatId?`ID: ${tgChatId}`:'未配置';

        const tgAgentEnabled=form.querySelector('[data-key="TG_AGENT_ENABLED"]')?.checked;
        const tgAgentPreview=document.getElementById('tgAgentRoutePreview');
        if(tgAgentPreview)tgAgentPreview.textContent=tgAgentEnabled?'已启用':'未开启';

        const qbUrl=form.querySelector('[data-key="QB_URL"]')?.value?.trim();
        const qbTargetPreview=document.getElementById('qbTargetPreview');
        if(qbTargetPreview){
            try{
                const parsed=new URL(qbUrl);
                qbTargetPreview.textContent=`${parsed.hostname}:${parsed.port||'80'}`;
            }catch{
                qbTargetPreview.textContent=qbUrl||'未配置';
            }
        }

        const qbCat=form.querySelector('[data-key="TG_QB_CATEGORY"]')?.value?.trim();
        const qbCatPreview=document.getElementById('qbCategoryPreview');
        if(qbCatPreview)qbCatPreview.textContent=qbCat||'telegram';

        const qbApiKey=form.querySelector('[data-key="QB_API_KEY"]')?.value?.trim();
        const qbAuthPreview=document.getElementById('qbAuthModePreview');
        if(qbAuthPreview)qbAuthPreview.textContent=qbApiKey?'API Key':'密码认证';
    }

    form.addEventListener('input',syncTelemetryWidgets);
    form.addEventListener('change',syncTelemetryWidgets);
    syncTelemetryWidgets();

    document.getElementById('copyLocalUrlBtn')?.addEventListener('click',async(e)=>{
        const url=document.getElementById('telemetryLocalUrl')?.textContent;
        if(!url)return;
        try{
            await navigator.clipboard.writeText(url);
            const btn=e.currentTarget;
            const prevTitle=btn.title||btn.getAttribute('aria-label')||'复制访问地址';
            btn.title='已复制';
            const orig=btn.innerHTML;
            btn.innerHTML='<i data-lucide="check"></i>';
            window.renderLucideIcons?.(btn);
            setTimeout(()=>{
                btn.innerHTML=orig;
                btn.title=prevTitle;
                window.renderLucideIcons?.(btn);
            },1500);
        }catch{}
    });

    const checkUpdateBtn = document.getElementById('telemetryCheckUpdateBtn');
    const statusBadge = document.getElementById('telemetryStatusBadge');
    if(checkUpdateBtn && statusBadge){
        checkUpdateBtn.addEventListener('click', async()=>{
            if(checkUpdateBtn.disabled) return;
            checkUpdateBtn.disabled = true;
            checkUpdateBtn.classList.add('is-checking');
            const originalBadgeHtml = statusBadge.innerHTML;
            statusBadge.dataset.tone = 'testing';
            statusBadge.textContent = '检查中…';
            try{
                const res = await fetch('/api/update/check');
                const data = await res.json();
                if(!res.ok || !data.success){
                    throw new Error(data.error || '检查更新失败');
                }
                const update = data.update || {};
                if(update.update_available){
                    const newVer = update.latest_version ? `v${update.latest_version.replace(/^v/i, '')}` : '新版本';
                    statusBadge.className = 'telemetry-status-badge is-update';
                    delete statusBadge.dataset.tone;
                    const releaseUrl = update.release_url || 'https://github.com/li88iioo/MediaFlux/releases';
                    statusBadge.innerHTML = `<a href="${releaseUrl}" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none;display:inline-flex;align-items:center;gap:4px;" title="发现新版本，点击打开发布页"><span>发现 ${newVer}</span><i data-lucide="external-link" style="width:11px;height:11px;"></i></a>`;
                    checkUpdateBtn.title = `发现新版本 ${newVer}`;
                }else{
                    statusBadge.className = 'telemetry-status-badge is-idle';
                    statusBadge.dataset.tone = 'idle';
                    statusBadge.textContent = '已是最新版';
                    setTimeout(()=>{
                        statusBadge.className = 'telemetry-status-badge';
                        delete statusBadge.dataset.tone;
                        statusBadge.innerHTML = originalBadgeHtml;
                        window.renderLucideIcons?.(statusBadge);
                    }, 3000);
                }
            }catch(err){
                window.showToast?.(err?.message || '检查更新失败', 'error', 5200);
                statusBadge.className = 'telemetry-status-badge is-error';
                statusBadge.dataset.tone = 'error';
                statusBadge.textContent = '检查失败';
                setTimeout(()=>{
                    statusBadge.className = 'telemetry-status-badge';
                    delete statusBadge.dataset.tone;
                    statusBadge.innerHTML = originalBadgeHtml;
                    window.renderLucideIcons?.(statusBadge);
                }, 3000);
            }finally{
                checkUpdateBtn.disabled = false;
                checkUpdateBtn.classList.remove('is-checking');
                window.renderLucideIcons?.(statusBadge);
            }
        });
    }

    let lockRequestGeneration=0;
    async function loadTmdbLocks(){
        const generation=++lockRequestGeneration;
        const body=document.getElementById('lockList');
        const state=document.getElementById('lockState');
        const button=document.getElementById('loadLocksBtn');
        if(body.dataset.loaded!=='true')body.innerHTML='<tr><td colspan="6" class="table-empty">读取中...</td></tr>';
        state.textContent='';
        button.disabled=true;
        button.setAttribute('aria-busy','true');
        try{
            const query=document.getElementById('lockSearch').value.trim();
            const response=await fetch('/api/tools/locks'+(query?'?q='+encodeURIComponent(query):''));
            const rows=await response.json();
            if(generation!==lockRequestGeneration)return;
            if(!response.ok)throw new Error(rows.error||'映射锁读取失败');
            body.dataset.loaded='true';
            if(!rows.length){body.innerHTML='<tr><td colspan="6" class="table-empty">暂无映射锁</td></tr>';return;}
            body.replaceChildren();
            rows.forEach(row=>{
                const tr=document.createElement('tr');
                const active=Number(row.key_version)===1&&row.lock_source==='manual';
                const season=Number(row.season);
                const scope=active?[row.parent_path||'根目录',row.media_type==='tv'?(season>=0?`剧集 · S${String(season).padStart(2,'0')}`:'剧集'):'电影'].join(' · '):'旧锁（已停用）';
                [row.raw_name,scope,`tmdb-${row.tmdb_id}`,row.title||'-',row.year||'-'].forEach(value=>{const td=document.createElement('td');td.textContent=value;td.title=value;tr.appendChild(td);});
                const action=document.createElement('td');
                const deleteButton=document.createElement('button');
                deleteButton.type='button';
                deleteButton.className='icon-action danger';
                deleteButton.title='删除映射锁';
                deleteButton.setAttribute('aria-label','删除映射锁');
                deleteButton.innerHTML='<i data-lucide="trash-2"></i>';
                deleteButton.addEventListener('click',async event=>{
                    const confirmed=await appConfirm({trigger:event.currentTarget,title:'删除 TMDB 映射锁',message:`删除「${row.raw_name}」在「${scope}」的锁定结果后，整理时会重新识别。`,confirmText:'删除映射锁',danger:true});
                    if(!confirmed)return;
                    deleteButton.disabled=true;
                    deleteButton.setAttribute('aria-busy','true');
                    try{
                        const result=await fetch('/api/tools/locks/'+row.id,{method:'DELETE'});
                        const data=await result.json().catch(()=>({}));
                        if(!result.ok||data.error||data.success!==true)throw new Error(data.error||'映射锁不存在或已被删除');
                        state.textContent='映射锁已删除';
                        await loadTmdbLocks();
                    }catch(error){
                        state.textContent=error.message||'删除失败';
                    }finally{
                        deleteButton.disabled=false;
                        deleteButton.setAttribute('aria-busy','false');
                    }
                });
                action.appendChild(deleteButton);
                tr.appendChild(action);
                body.appendChild(tr);
            });
            renderLucideIcons(body);
        }catch(error){
            if(generation!==lockRequestGeneration)return;
            body.dataset.loaded='true';
            body.innerHTML='<tr><td colspan="6" class="table-empty">'+escapeHtml(error.message)+'</td></tr>';
        }finally{
            if(generation===lockRequestGeneration){
                button.disabled=false;
                button.setAttribute('aria-busy','false');
            }
        }
    }
    document.getElementById('openLocksBtn').addEventListener('click',event=>{lockModal.open(event.currentTarget);loadTmdbLocks();});
    document.getElementById('loadLocksBtn').addEventListener('click',loadTmdbLocks);
    document.getElementById('lockSearch').addEventListener('keydown',event=>{if(event.key==='Enter')loadTmdbLocks();});

    function renderProxyResults(data){
        const result=document.getElementById('proxyResult');const summary=data.summary||{};result.className='proxy-result proxy-result-list '+(summary.failed?'is-error':'is-ok');
        result.innerHTML=`<div class="proxy-result-summary"><strong>${summary.passed||0}/${summary.total||0} 可达</strong><span>${data.proxy_used?'通过代理':'直连'} · ${summary.elapsed_ms||0} ms</span></div><div class="proxy-target-list">${(data.results||[]).map(item=>`<article class="proxy-target-item ${item.ok?'is-ok':'is-error'}"><span class="connection-dot ${item.ok?'online':'offline'}"></span><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.host||item.url||'')}</small></div><span>${item.ok?`HTTP ${item.status_code} · ${item.elapsed_ms} ms`:escapeHtml(item.error||('HTTP '+(item.status_code||'-')))}</span></article>`).join('')}</div>`;
    }
    document.getElementById('proxyTestBtn').addEventListener('click',async()=>{
        const button=document.getElementById('proxyTestBtn');const result=document.getElementById('proxyResult');button.disabled=true;result.hidden=false;result.className='proxy-result proxy-result-list';result.textContent='正在并行测试固定目标...';
        try{const response=await fetch('/api/tools/proxy/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({use_proxy:document.getElementById('proxyUseProxy').checked})});const data=await response.json();if(!response.ok)throw new Error(data.error||'测试失败');renderProxyResults(data);}
        catch(error){result.className='proxy-result proxy-result-list is-error';result.textContent=error.message||'测试请求失败';}
        finally{button.disabled=false;}
    });

    const testTmdbBtn = document.getElementById('testTmdbBtn');
    if (testTmdbBtn) {
        testTmdbBtn.addEventListener('click', async () => {
            const state = document.getElementById('tmdbConnectionState');
            const apiKeyInput = document.getElementById('tmdbApiKeyInput');
            const apiUrlInput = document.getElementById('tmdbApiUrlInput');
            testTmdbBtn.disabled = true;
            if (state) {
                state.className = 'tmdb-connection-status is-testing';
                state.innerHTML = '<i data-lucide="loader-circle"></i><span>正在测试 TMDB API 连通性...</span>';
                window.renderLucideIcons?.(state);
            }
            try {
                const response = await fetch('/api/tools/tmdb/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        api_key: apiKeyInput?.value || '',
                        api_url: apiUrlInput?.value || ''
                    })
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '连接测试失败');
                if (state) {
                    state.className = 'tmdb-connection-status is-ok';
                    state.innerHTML = `<i data-lucide="check-circle"></i><span>${escapeHtml(data.message || '连接正常')}</span>`;
                    window.renderLucideIcons?.(state);
                }
            } catch (error) {
                if (state) {
                    state.className = 'tmdb-connection-status is-error';
                    state.innerHTML = `<i data-lucide="alert-circle"></i><span>${escapeHtml(error.message || '连接失败')}</span>`;
                    window.renderLucideIcons?.(state);
                }
            } finally {
                testTmdbBtn.disabled = false;
            }
        });
    }

    const resetTmdbConnectionState = () => {
        const state = document.getElementById('tmdbConnectionState');
        if (!state || state.classList.contains('is-testing')) return;
        state.className = 'tmdb-connection-status is-idle';
        state.innerHTML = '<span>TMDB 连接状态</span>';
    };
    ['tmdbApiKeyInput', 'tmdbApiUrlInput'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', resetTmdbConnectionState);
    });

    document.querySelectorAll('.preset-chip[data-preset-url]').forEach(btn => {
        btn.addEventListener('click', () => {
            const urlInput = document.getElementById('tmdbApiUrlInput');
            if (urlInput) {
                urlInput.value = btn.dataset.presetUrl;
                urlInput.dispatchEvent(new Event('input', { bubbles: true }));
                urlInput.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
    });
})();
