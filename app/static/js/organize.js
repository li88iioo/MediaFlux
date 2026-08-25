(function(){
    const workspace=document.getElementById('organizeWorkspace');
    const isRules=document.body.classList.contains('organize-rules-page');
    if(isRules){
        setupTabGroup(workspace);
        const ruleTabs=[...workspace.querySelectorAll('[data-tab-target]')];
        const validRuleTabs=new Set(ruleTabs.map(button=>button.dataset.tabTarget));
        let applyingRuleHash=false;
        const activateRuleHash=()=>{
            const requested=window.location.hash.slice(1);
            const normalized=validRuleTabs.has(requested)?requested:'naming';
            const button=ruleTabs.find(item=>item.dataset.tabTarget===normalized);
            if(!button||button.classList.contains('active'))return;
            applyingRuleHash=true;
            button.click();
            applyingRuleHash=false;
        };
        ruleTabs.forEach(button=>button.addEventListener('click',()=>{
            if(applyingRuleHash)return;
            const tab=button.dataset.tabTarget;
            const url=new URL(window.location.href);
            url.hash=tab==='naming'?'':tab;
            window.history.replaceState(window.history.state,'',`${url.pathname}${url.search}${url.hash}`);
        }));
        activateRuleHash();
        window.addEventListener('hashchange',activateRuleHash);
    }
    const sourceInput=document.getElementById('organizeSourceDirs');
    let sources=[];
    let pollTimer=null;
    let scheduleTimer=null;
    let lastStatusRenderKey='';
    let configReady=false;
    let statusRequestSerial=0;
    let organizeActionBusy=false;
    let organizeStatusRunning=false;
    const configFieldLocks=Array.from(workspace.querySelectorAll('[data-key]'),field=>({field,disabled:field.disabled}));
    configFieldLocks.forEach(({field})=>{field.disabled=true;});

    function readExtensionDefaults(kind){
        return Array.from(
            document.querySelectorAll(`[data-extension-editor="${kind}"] [data-extension-value]`),
            node=>node.dataset.extensionValue||'',
        ).filter(Boolean);
    }
    const extensionDefaults={
        video:readExtensionDefaults('video'),
        metadata:readExtensionDefaults('metadata'),
    };
    function normalizeExtensionTokens(raw){
        const values=[];String(raw||'').split(/[,，\s]+/).forEach(token=>{const value=token.trim().toLowerCase().replace(/^\.+/,'');if(/^[a-z0-9]{1,10}$/.test(value)&&!values.includes(value))values.push(value);});return values;
    }
    function createExtensionEditor(kind,hiddenId){
        const root=document.querySelector(`[data-extension-editor="${kind}"]`);const hidden=document.getElementById(hiddenId);if(!root||!hidden)return null;
        const list=root.querySelector('.organize-ext-list');const input=root.querySelector('.organize-ext-input');const reset=root.querySelector('.organize-ext-reset');let values=[];
        function sync(){hidden.value=values.join(',');list.replaceChildren();values.forEach(value=>{const chip=document.createElement('span');chip.className='organize-ext-chip';chip.append(document.createTextNode(value));const remove=document.createElement('button');remove.type='button';remove.title=`移除 ${value}`;remove.setAttribute('aria-label',`移除 ${value}`);remove.textContent='×';remove.addEventListener('click',()=>{values=values.filter(item=>item!==value);sync();input.focus();});chip.appendChild(remove);list.appendChild(chip);});}
        function add(raw){const additions=normalizeExtensionTokens(raw);if(!additions.length)return;values=[...new Set([...values,...additions])];input.value='';sync();}
        function set(raw){values=normalizeExtensionTokens(raw);if(!values.length)values=[...extensionDefaults[kind]];sync();}
        root.addEventListener('click',event=>{if(!event.target.closest('button'))input.focus();});
        input.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===','||event.key==='，'){event.preventDefault();add(input.value);}else if(event.key==='Backspace'&&!input.value&&values.length){values.pop();sync();}});
        input.addEventListener('blur',()=>add(input.value));input.addEventListener('paste',event=>{const text=event.clipboardData?.getData('text')||'';if(/[,，\s]/.test(text)){event.preventDefault();add(text);}});
        reset.addEventListener('click',()=>set(extensionDefaults[kind].join(',')));
        set(extensionDefaults[kind].join(','));
        return {set,ensure(){if(!values.length)set(extensionDefaults[kind].join(','));}};
    }
    const extensionEditors=isRules?{video:createExtensionEditor('video','r_video_exts'),metadata:createExtensionEditor('metadata','r_metadata_exts')}:{};

    const regexModalElement=document.getElementById('tmdbRegexRulesModal');
    const regexRulesModal=regexModalElement?createAppModal(regexModalElement):null;
    const regexRuleBody=document.getElementById('tmdbRegexRuleTableBody');
    let regexRules=[];
    let regexRulesLoaded=false;
    const regexTargetLabels={filename:'文件名',parent:'父路径',both:'父路径 + 文件名'};
    const regexMediaLabels={any:'跟随解析',movie:'电影',tv:'剧集'};

    const preprocessModalElement=document.getElementById('preprocessRulesModal');
    const preprocessRulesModal=preprocessModalElement?createAppModal(preprocessModalElement):null;
    const preprocessRuleBody=document.getElementById('preprocessRuleTableBody');
    let preprocessRules=[];
    let preprocessRulesLoaded=false;
    const preprocessActionLabels={delete:'删除',replace:'替换',season_override:'季号覆盖',season_offset:'季号偏移',episode_offset:'集数偏移'};
    const preprocessScopeLabels={filename:'文件名',parent:'父路径',both:'父路径 + 文件名'};
    const recognitionKnowledgeElement=document.getElementById('recognitionKnowledgeModal');
    const recognitionKnowledgeModal=recognitionKnowledgeElement?createAppModal(recognitionKnowledgeElement):null;
    const recognitionKnowledgeBody=document.getElementById('recognitionKnowledgeTableBody');
    let recognitionKnowledge=[];
    let recognitionKnowledgeLoaded=false;
    let recognitionKnowledgeRequestSerial=0;
    const recognitionKnowledgeTypeLabels={release_group:'前置发布组',release_suffix:'尾部制作组'};
    const recognitionKnowledgeSourceLabels={builtin:'内置',learned:'AI 学习',user:'用户'};


    function recognitionKnowledgePayload(){
        return {knowledge_type:document.getElementById('recognitionKnowledgeType').value,canonical_value:document.getElementById('recognitionKnowledgeCanonical').value.trim(),aliases:document.getElementById('recognitionKnowledgeAliases').value,disabled:document.getElementById('recognitionKnowledgeDisabled').value==='1'};
    }
    function resetRecognitionKnowledgeForm(item={}){
        document.getElementById('recognitionKnowledgeId').value=item.id||'';document.getElementById('recognitionKnowledgeType').value=item.knowledge_type||'release_group';document.getElementById('recognitionKnowledgeCanonical').value=item.canonical_value||'';document.getElementById('recognitionKnowledgeAliases').value=(item.aliases||[]).filter(value=>value!==item.canonical_value).join(', ');document.getElementById('recognitionKnowledgeDisabled').value=item.disabled?'1':'0';document.getElementById('recognitionKnowledgeEditorTitle').textContent=item.id?`编辑词条 #${item.id}`:'新建词条';document.getElementById('recognitionKnowledgeFormState').textContent='';const evidence=document.getElementById('recognitionKnowledgeEvidence');if(item.source==='learned'){evidence.textContent=`AI 学习样本 ${item.success_count||0}/2 · ${item.disabled?'尚未启用':'已通过双样本验证并热加载'}`;}else if(item.source==='builtin'){evidence.textContent='内置词条随版本提供，可以停用或修改，但不能删除；后续种子升级不会覆盖你的修改。';}else{evidence.textContent='用户词条保存后立即热加载，自动整理、人工识别、本地整理与 TG 整理共用。';}
    }
    function renderRecognitionKnowledgeEmpty(message){recognitionKnowledgeBody.replaceChildren();const empty=document.createElement('div');empty.className='tmdb-regex-rule-empty';empty.textContent=message;recognitionKnowledgeBody.appendChild(empty);}
    function createRecognitionKnowledgeCard(item){
        const card=document.createElement('article');card.className='tmdb-regex-rule-card recognition-knowledge-card';card.classList.toggle('is-disabled',Boolean(item.disabled));card.classList.toggle('is-pending',item.source==='learned'&&item.disabled);
        const copy=document.createElement('div');copy.className='tmdb-regex-rule-copy';copy.tabIndex=0;copy.setAttribute('role','button');const open=()=>resetRecognitionKnowledgeForm(item);copy.addEventListener('click',open);copy.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open();}});
        const main=document.createElement('div');main.className='tmdb-regex-rule-main';const dot=document.createElement('span');dot.className=`connection-dot ${item.disabled?'offline':'online'}`;const name=document.createElement('strong');name.textContent=item.canonical_value;const source=document.createElement('span');source.className='recognition-knowledge-source';source.textContent=recognitionKnowledgeSourceLabels[item.source]||item.source;main.append(dot,name,source);
        const meta=document.createElement('div');meta.className='tmdb-regex-rule-meta';meta.textContent=`${recognitionKnowledgeTypeLabels[item.knowledge_type]||item.knowledge_type} · ${item.disabled?'停用':'启用'} · ${item.aliases?.length||0} 个别名${item.source==='learned'?` · 样本 ${item.success_count||0}/2`:''}`;const aliases=document.createElement('code');aliases.className='tmdb-regex-rule-pattern';aliases.textContent=(item.aliases||[]).join(' · ');copy.append(main,meta,aliases);
        const actions=document.createElement('div');actions.className='tmdb-regex-rule-actions';const edit=document.createElement('button');edit.type='button';edit.className='icon-action';edit.title='编辑';edit.innerHTML='<i data-lucide="pencil"></i>';edit.addEventListener('click',open);actions.appendChild(edit);if(item.source!=='builtin'){const remove=document.createElement('button');remove.type='button';remove.className='icon-action danger';remove.title='删除';remove.innerHTML='<i data-lucide="trash-2"></i>';remove.addEventListener('click',async event=>{const confirmed=await appConfirm({trigger:event.currentTarget,title:'删除本地识别词条',message:`删除「${item.canonical_value}」后会立即停止识别其所有别名。`,confirmText:'删除词条',danger:true});if(!confirmed)return;const response=await fetch(`/api/tools/recognition-knowledge/${item.id}`,{method:'DELETE'});const data=await response.json();if(!response.ok){await appAlert({type:'error',title:'删除失败',message:data.error||'无法删除词条'});return;}if(String(item.id)===document.getElementById('recognitionKnowledgeId').value)resetRecognitionKnowledgeForm();await loadRecognitionKnowledge(true);});actions.appendChild(remove);}card.append(copy,actions);return card;
    }
    function renderRecognitionKnowledge(){if(!recognitionKnowledge.length){renderRecognitionKnowledgeEmpty('暂无符合条件的词条');return;}recognitionKnowledgeBody.replaceChildren(...recognitionKnowledge.map(createRecognitionKnowledgeCard));renderLucideIcons(recognitionKnowledgeBody);}
    function updateRecognitionKnowledgeSummary(summary){const target=document.getElementById('recognitionKnowledgeSummary');if(target)target.textContent=`启用 ${summary?.enabled??0} · AI 学习 ${summary?.learned??0} · 用户 ${summary?.user??0}`;}
    async function loadRecognitionKnowledge(background=false){
        const serial=++recognitionKnowledgeRequestSerial;const state=document.getElementById('recognitionKnowledgeListState');if(state)state.textContent=background?'正在刷新，保留当前列表…':'正在读取词库…';if(!recognitionKnowledgeLoaded&&!recognitionKnowledge.length)renderRecognitionKnowledgeEmpty('正在读取词库…');const q=document.getElementById('recognitionKnowledgeSearch')?.value.trim()||'';const type=document.getElementById('recognitionKnowledgeFilter')?.value||'';try{const response=await fetch(`/api/tools/recognition-knowledge?q=${encodeURIComponent(q)}&knowledge_type=${encodeURIComponent(type)}`);const data=await response.json();if(serial!==recognitionKnowledgeRequestSerial)return;if(!response.ok)throw new Error(data.error||'词库读取失败');const scrollTop=recognitionKnowledgeBody.scrollTop;recognitionKnowledge=Array.isArray(data.items)?data.items:[];recognitionKnowledgeLoaded=true;renderRecognitionKnowledge();recognitionKnowledgeBody.scrollTop=scrollTop;updateRecognitionKnowledgeSummary(data.summary||{});if(state)state.textContent=`显示 ${recognitionKnowledge.length} 条 · 保存后即时生效`;}catch(error){if(serial!==recognitionKnowledgeRequestSerial)return;if(state)state.textContent=error.message||'词库读取失败';if(!recognitionKnowledge.length)renderRecognitionKnowledgeEmpty('词库读取失败');}
    }
    async function saveRecognitionKnowledge(event){event.preventDefault();const id=document.getElementById('recognitionKnowledgeId').value;const button=document.getElementById('saveRecognitionKnowledgeBtn');const state=document.getElementById('recognitionKnowledgeFormState');button.disabled=true;state.textContent='正在保存…';try{const response=await fetch(id?`/api/tools/recognition-knowledge/${id}`:'/api/tools/recognition-knowledge',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(recognitionKnowledgePayload())});const data=await response.json();if(!response.ok)throw new Error(data.error||'词条保存失败');state.textContent=id?'词条已更新并热加载':'词条已创建并热加载';resetRecognitionKnowledgeForm(data);await loadRecognitionKnowledge(true);}catch(error){state.textContent=error.message||'词条保存失败';}finally{button.disabled=false;}}
    let recognitionKnowledgeSearchTimer=null;
    function scheduleRecognitionKnowledgeSearch(){clearTimeout(recognitionKnowledgeSearchTimer);recognitionKnowledgeSearchTimer=setTimeout(()=>loadRecognitionKnowledge(true),220);}

    function preprocessRulePayload(){
        const numeric=document.getElementById('preprocessNumericValue').value.trim();
        return {
            name:document.getElementById('preprocessRuleName').value.trim(),
            matcher_type:document.getElementById('preprocessMatcherType').value,
            pattern:document.getElementById('preprocessPattern').value,
            scope:document.getElementById('preprocessScope').value,
            action:document.getElementById('preprocessAction').value,
            replacement:document.getElementById('preprocessReplacement').value,
            numeric_value:numeric===''?null:parseInt(numeric),
            priority:parseInt(document.getElementById('preprocessPriority').value||0),
            disabled:document.getElementById('preprocessDisabled').value==='1',
        };
    }

    function syncPreprocessActionFields(){
        const action=document.getElementById('preprocessAction').value;
        const replacementField=document.getElementById('preprocessReplacementField');
        const numericField=document.getElementById('preprocessNumericField');
        replacementField.classList.toggle('preprocess-field-hidden',action!=='replace');
        numericField.classList.toggle('preprocess-field-hidden',!['season_override','season_offset','episode_offset'].includes(action));
        const numeric=document.getElementById('preprocessNumericValue');
        const label=document.getElementById('preprocessNumericLabel');
        if(action==='season_override'){numeric.min='0';numeric.value=numeric.value===''?'1':Math.max(0,parseInt(numeric.value||0));label.textContent='目标季号';}
        else if(action==='season_offset'){numeric.min='-999';numeric.value=numeric.value===''?'1':numeric.value;label.textContent='季号偏移量';}
        else if(action==='episode_offset'){numeric.min='-999';numeric.value=numeric.value===''?'1':numeric.value;label.textContent='集数偏移量';}
    }

    function markActivePreprocessRule(){
        if(!preprocessRuleBody)return;
        const activeId=document.getElementById('preprocessRuleId').value;
        preprocessRuleBody.querySelectorAll('.tmdb-regex-rule-card').forEach(card=>card.classList.toggle('is-active',card.dataset.ruleId===activeId));
    }

    function resetPreprocessRuleForm(rule){
        const value=rule||{};
        document.getElementById('preprocessRuleId').value=value.id||'';
        document.getElementById('preprocessRuleName').value=value.name||'';
        document.getElementById('preprocessMatcherType').value=value.matcher_type||'text';
        document.getElementById('preprocessPattern').value=value.pattern||'';
        document.getElementById('preprocessScope').value=value.scope||'filename';
        document.getElementById('preprocessAction').value=value.action||'delete';
        document.getElementById('preprocessReplacement').value=value.replacement||'';
        document.getElementById('preprocessNumericValue').value=value.numeric_value??1;
        document.getElementById('preprocessPriority').value=value.priority??0;
        document.getElementById('preprocessDisabled').value=value.disabled?'1':'0';
        document.getElementById('preprocessEditorTitle').textContent=value.id?`${value.builtin?'推荐规则':'编辑规则'} #${value.id}`:'新建规则';
        document.getElementById('preprocessRuleFormState').textContent=value.builtin?'修改后仍可用“恢复推荐”还原默认值':'';
        renderPreprocessPreview(null);
        syncPreprocessActionFields();
        markActivePreprocessRule();
        if(!value.id)document.getElementById('preprocessRuleName').focus({preventScroll:true});
    }

    function renderPreprocessPreview(data,error){
        const box=document.getElementById('preprocessSamplePreview');
        box.replaceChildren();
        box.classList.toggle('is-error',Boolean(error));
        const strong=document.createElement('strong');
        const span=document.createElement('span');
        if(error){strong.textContent='样例预览失败';span.textContent=error;box.append(strong,span);return;}
        if(!data){strong.textContent='等待样例预览';span.textContent='填写样例后可检查标题与季集变化。';const code=document.createElement('code');code.textContent='NO SAMPLE / NO WRITE';box.append(strong,span,code);return;}
        strong.textContent=data.matched?'规则已命中':'规则未命中';
        span.textContent=data.matched?`应用 ${data.applied_rules.length} 条动作；这里只展示识别投影，不修改原文件。`:'当前匹配内容未命中样例。';
        const diff=document.createElement('div');diff.className='preprocess-preview-diff';
        [['文件名',data.filename_after],['父路径',data.parent_path_after||'—'],['季 / 集',`${data.season_after??'—'} / ${data.episode_after??'—'}`]].forEach(([label,value])=>{const b=document.createElement('b');b.textContent=label;const code=document.createElement('code');code.textContent=value;diff.append(b,code);});
        box.append(strong,span,diff);
    }

    function renderPreprocessRuleEmpty(message){
        preprocessRuleBody.replaceChildren();
        const empty=document.createElement('div');empty.className='tmdb-regex-rule-empty';empty.textContent=message;preprocessRuleBody.appendChild(empty);
    }

    function createPreprocessRuleCard(rule){
        const card=document.createElement('article');card.className='tmdb-regex-rule-card preprocess-rule-card';card.dataset.ruleId=String(rule.id);
        card.classList.toggle('is-disabled',Boolean(rule.disabled));card.classList.toggle('is-builtin',Boolean(rule.builtin));
        const copy=document.createElement('div');copy.className='tmdb-regex-rule-copy';copy.tabIndex=0;copy.setAttribute('role','button');copy.setAttribute('aria-label',`编辑规则 ${rule.name}`);
        const open=()=>resetPreprocessRuleForm(rule);copy.addEventListener('click',open);copy.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open();}});
        const main=document.createElement('div');main.className='tmdb-regex-rule-main';const dot=document.createElement('span');dot.className=`connection-dot ${rule.disabled?'offline':'online'}`;const name=document.createElement('strong');name.textContent=rule.name;main.append(dot,name);
        if(rule.builtin){const badge=document.createElement('span');badge.className='preprocess-badge';badge.textContent='推荐';main.appendChild(badge);}
        const priority=document.createElement('span');priority.className='tmdb-regex-priority';priority.textContent=`P${rule.priority}`;main.appendChild(priority);
        const meta=document.createElement('div');meta.className='tmdb-regex-rule-meta';meta.textContent=`${preprocessActionLabels[rule.action]||rule.action} · ${rule.matcher_type==='regex'?'正则':'文本'} · ${preprocessScopeLabels[rule.scope]||rule.scope} · ${rule.disabled?'停用':'启用'}`;
        const pattern=document.createElement('code');pattern.className='tmdb-regex-rule-pattern';pattern.textContent=rule.pattern;copy.append(main,meta,pattern);
        const actions=document.createElement('div');actions.className='tmdb-regex-rule-actions';const edit=document.createElement('button');edit.type='button';edit.className='icon-action';edit.title='编辑';edit.innerHTML='<i data-lucide="pencil"></i>';edit.addEventListener('click',open);actions.appendChild(edit);
        if(!rule.builtin){const remove=document.createElement('button');remove.type='button';remove.className='icon-action danger';remove.title='删除';remove.innerHTML='<i data-lucide="trash-2"></i>';remove.addEventListener('click',async event=>{const confirmed=await appConfirm({trigger:event.currentTarget,title:'删除识别预处理规则',message:`删除「${rule.name}」后将立即停止应用。`,confirmText:'删除规则',danger:true});if(!confirmed)return;const response=await fetch(`/api/tools/recognition-preprocess-rules/${rule.id}`,{method:'DELETE'});const data=await response.json();if(!response.ok){await appAlert({type:'error',title:'删除失败',message:data.error||'无法删除规则'});return;}if(String(rule.id)===document.getElementById('preprocessRuleId').value)resetPreprocessRuleForm();await loadPreprocessRules(true);});actions.appendChild(remove);}
        card.append(copy,actions);return card;
    }

    function renderPreprocessRules(){
        if(!preprocessRules.length){renderPreprocessRuleEmpty('暂无规则，可恢复推荐或新建规则');return;}
        preprocessRuleBody.replaceChildren(...preprocessRules.map(createPreprocessRuleCard));markActivePreprocessRule();renderLucideIcons(preprocessRuleBody);
    }

    function updatePreprocessSummary(summary){
        const target=document.getElementById('preprocessRuleSummary');if(!target)return;
        target.textContent=`推荐 ${summary?.builtin??0} · 自定义 ${summary?.custom??0} · 启用 ${summary?.enabled??0}`;
    }

    async function loadPreprocessRules(background=false){
        const state=document.getElementById('preprocessRuleListState');if(state)state.textContent=background?'正在刷新，保留当前列表…':'正在读取规则…';
        if(preprocessRuleBody&&!preprocessRulesLoaded&&!preprocessRules.length)renderPreprocessRuleEmpty('正在读取规则…');
        try{const response=await fetch('/api/tools/recognition-preprocess-rules');const data=await response.json();if(!response.ok)throw new Error(data.error||'规则读取失败');preprocessRules=Array.isArray(data.rules)?data.rules:[];preprocessRulesLoaded=true;renderPreprocessRules();updatePreprocessSummary(data.summary||{});if(state)state.textContent=`${preprocessRules.length}/200 条 · 高优先级先执行`;}catch(error){if(state)state.textContent=error.message||'规则读取失败';const summary=document.getElementById('preprocessRuleSummary');if(summary)summary.textContent='规则状态读取失败';if(preprocessRuleBody&&!preprocessRules.length)renderPreprocessRuleEmpty('规则读取失败');}
    }

    async function previewPreprocessRule(){
        const filename=document.getElementById('preprocessSampleFilename').value.trim();if(!filename){renderPreprocessPreview(null,'请输入样例文件名');return false;}
        const button=document.getElementById('previewPreprocessRuleBtn');button.disabled=true;
        try{const season=document.getElementById('preprocessSampleSeason').value.trim();const episode=document.getElementById('preprocessSampleEpisode').value.trim();const response=await fetch('/api/tools/recognition-preprocess-rules/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule:preprocessRulePayload(),filename,parent_path:document.getElementById('preprocessSampleParent').value.trim(),season:season===''?null:parseInt(season),episode:episode===''?null:parseInt(episode)})});const data=await response.json();if(!response.ok)throw new Error(data.error||'样例预览失败');renderPreprocessPreview(data);return true;}catch(error){renderPreprocessPreview(null,error.message||'样例预览失败');return false;}finally{button.disabled=false;}
    }

    async function savePreprocessRule(event){
        event.preventDefault();const state=document.getElementById('preprocessRuleFormState');const button=document.getElementById('savePreprocessRuleBtn');const id=document.getElementById('preprocessRuleId').value;button.disabled=true;state.textContent='正在保存…';
        try{const response=await fetch(id?`/api/tools/recognition-preprocess-rules/${id}`:'/api/tools/recognition-preprocess-rules',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(preprocessRulePayload())});const data=await response.json();if(!response.ok)throw new Error(data.error||'规则保存失败');state.textContent=id?'规则已更新':'规则已创建';resetPreprocessRuleForm(data);await loadPreprocessRules(true);}catch(error){state.textContent=error.message||'规则保存失败';}finally{button.disabled=false;}
    }

    async function restorePreprocessRules(event){
        const confirmed=await appConfirm({trigger:event.currentTarget,title:'恢复推荐预处理规则',message:'将恢复推荐规则的匹配内容、动作、优先级和默认启停状态；自定义规则不会删除。',confirmText:'恢复推荐'});if(!confirmed)return;
        const button=event.currentTarget;button.disabled=true;try{const response=await fetch('/api/tools/recognition-preprocess-rules/restore-defaults',{method:'POST'});const data=await response.json();if(!response.ok)throw new Error(data.error||'恢复失败');await loadPreprocessRules(true);resetPreprocessRuleForm();}catch(error){await appAlert({type:'error',title:'恢复失败',message:error.message||'无法恢复推荐规则'});}finally{button.disabled=false;}
    }

    function regexRulePayload(){
        const season=document.getElementById('tmdbRegexSeasonOverride').value.trim();
        return {
            name:document.getElementById('tmdbRegexRuleName').value.trim(),
            pattern:document.getElementById('tmdbRegexRulePattern').value,
            match_target:document.getElementById('tmdbRegexMatchTarget').value,
            tmdb_id:document.getElementById('tmdbRegexTmdbId').value.trim(),
            media_type:document.getElementById('tmdbRegexMediaType').value,
            season_override:season===''?null:parseInt(season),
            priority:parseInt(document.getElementById('tmdbRegexPriority').value||0),
            disabled:document.getElementById('tmdbRegexDisabled').value==='1',
            sample_filename:document.getElementById('tmdbRegexSampleFilename').value.trim(),
            sample_parent_path:document.getElementById('tmdbRegexSampleParent').value.trim(),
        };
    }

    function markActiveRegexRule(){
        if(!regexRuleBody)return;
        const activeId=document.getElementById('tmdbRegexRuleId').value;
        regexRuleBody.querySelectorAll('.tmdb-regex-rule-card').forEach(card=>card.classList.toggle('is-active',card.dataset.ruleId===activeId));
    }

    function resetRegexRuleForm(rule){
        const value=rule||{};
        document.getElementById('tmdbRegexRuleId').value=value.id||'';
        document.getElementById('tmdbRegexRuleName').value=value.name||'';
        document.getElementById('tmdbRegexRulePattern').value=value.pattern||'';
        document.getElementById('tmdbRegexMatchTarget').value=value.match_target||'filename';
        document.getElementById('tmdbRegexTmdbId').value=value.tmdb_id||'';
        document.getElementById('tmdbRegexMediaType').value=value.media_type||'any';
        document.getElementById('tmdbRegexSeasonOverride').value=value.season_override??'';
        document.getElementById('tmdbRegexPriority').value=value.priority??0;
        document.getElementById('tmdbRegexDisabled').value=value.disabled?'1':'0';
        document.getElementById('tmdbRegexEditorTitle').textContent=value.id?`编辑规则 #${value.id}`:'新建规则';
        document.getElementById('tmdbRegexRuleFormState').textContent='';
        renderRegexPreview(null);
        syncRegexSeasonField();
        markActiveRegexRule();
        if(!value.id)document.getElementById('tmdbRegexRuleName').focus({preventScroll:true});
    }

    function syncRegexSeasonField(){
        const movie=document.getElementById('tmdbRegexMediaType').value==='movie';
        const field=document.getElementById('tmdbRegexSeasonOverride');
        field.disabled=movie;
        if(movie)field.value='';
    }

    function renderRegexPreview(data,error){
        const box=document.getElementById('tmdbRegexSamplePreview');
        box.replaceChildren();
        box.classList.toggle('is-error',Boolean(error));
        const strong=document.createElement('strong');
        const span=document.createElement('span');
        const code=document.createElement('code');
        if(error){
            strong.textContent='样例校验失败';
            span.textContent=error;
            code.textContent='INVALID RULE';
        }else if(!data){
            strong.textContent='等待样例预览';
            span.textContent='填写样例文件名后可检查命中与目标。';
            code.textContent='NO SAMPLE / NO WRITE';
        }else{
            const target=data.verified_target||{};
            strong.textContent=data.matched?'样例命中':'样例未命中';
            span.textContent=data.matched?`已校验：${target.title||`TMDB ${data.tmdb_id}`}${target.year?` (${target.year})`:''} · ${target.media_type==='tv'?'剧集':'电影'}${data.season_override===null?'':` · 季覆盖 S${String(data.season_override).padStart(2,'0')}`}`:'当前表达式未命中样例，请检查匹配范围与媒体类型。';
            code.textContent=data.sample||'';
        }
        box.append(strong,span,code);
    }

    function renderRegexRuleEmpty(message){
        regexRuleBody.replaceChildren();
        const empty=document.createElement('div');
        empty.className='tmdb-regex-rule-empty';
        empty.textContent=message;
        regexRuleBody.appendChild(empty);
    }

    function createRegexRuleCard(rule){
        const card=document.createElement('article');
        card.className='tmdb-regex-rule-card';
        card.dataset.ruleId=String(rule.id);
        card.classList.toggle('is-disabled',Boolean(rule.disabled));

        const copy=document.createElement('div');
        copy.className='tmdb-regex-rule-copy';
        copy.tabIndex=0;
        copy.setAttribute('role','button');
        copy.setAttribute('aria-label',`编辑规则 ${rule.name}`);
        const openRule=()=>resetRegexRuleForm(rule);
        copy.addEventListener('click',openRule);
        copy.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openRule();}});

        const main=document.createElement('div');
        main.className='tmdb-regex-rule-main';
        const dot=document.createElement('span');
        dot.className=`connection-dot ${rule.disabled?'offline':'online'}`;
        const name=document.createElement('strong');
        name.textContent=rule.name;
        const priority=document.createElement('span');
        priority.className='tmdb-regex-priority';
        priority.textContent=`P${rule.priority}`;
        main.append(dot,name,priority);

        const meta=document.createElement('div');
        meta.className='tmdb-regex-rule-meta';
        const season=rule.season_override===null||rule.season_override===undefined?'':` · S${String(rule.season_override).padStart(2,'0')}`;
        [`${regexTargetLabels[rule.match_target]||rule.match_target} · ${regexMediaLabels[rule.media_type]||rule.media_type}`,`tmdb-${rule.tmdb_id}${season}`].forEach(text=>{const item=document.createElement('span');item.textContent=text;meta.appendChild(item);});
        const pattern=document.createElement('code');
        pattern.className='tmdb-regex-rule-pattern';
        pattern.textContent=rule.pattern;
        copy.append(main,meta,pattern);

        const actions=document.createElement('div');
        actions.className='tmdb-regex-rule-actions';
        const edit=document.createElement('button');
        edit.type='button';
        edit.className='icon-action';
        edit.title='编辑规则';
        edit.setAttribute('aria-label','编辑规则');
        edit.innerHTML='<i data-lucide="pencil"></i>';
        edit.addEventListener('click',()=>resetRegexRuleForm(rule));
        const remove=document.createElement('button');
        remove.type='button';
        remove.className='icon-action danger';
        remove.title='删除规则';
        remove.setAttribute('aria-label','删除规则');
        remove.innerHTML='<i data-lucide="trash-2"></i>';
        remove.addEventListener('click',async event=>{
            const confirmed=await appConfirm({trigger:event.currentTarget,title:'删除 TMDB 强制匹配规则',message:`删除「${rule.name}」后，命中该规则的文件将回到普通搜索链路。`,confirmText:'删除规则',danger:true});
            if(!confirmed)return;
            const response=await fetch(`/api/tools/tmdb-regex-rules/${rule.id}`,{method:'DELETE'});
            const data=await response.json();
            if(!response.ok){
                await appAlert({type:'error',title:'删除失败',message:data.error||'无法删除规则'});
                return;
            }
            if(String(rule.id)===document.getElementById('tmdbRegexRuleId').value)resetRegexRuleForm();
            await loadRegexRules(true);
        });
        actions.append(edit,remove);
        card.append(copy,actions);
        return card;
    }

    function renderRegexRules(){
        if(!regexRules.length){renderRegexRuleEmpty('暂无规则，可新建第一条强制匹配规则');return;}
        regexRuleBody.replaceChildren(...regexRules.map(createRegexRuleCard));
        markActiveRegexRule();
        renderLucideIcons(regexRuleBody);
    }

    async function loadRegexRules(background=false){
        const state=document.getElementById('tmdbRegexRuleListState');
        state.textContent=background?'正在刷新，保留当前列表…':'正在读取规则…';
        if(!regexRulesLoaded&&!regexRules.length)renderRegexRuleEmpty('正在读取规则…');
        try{
            const response=await fetch('/api/tools/tmdb-regex-rules');
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'规则读取失败');
            regexRules=Array.isArray(data)?data:[];
            regexRulesLoaded=true;
            renderRegexRules();
            state.textContent=`${regexRules.length}/200 条 · 高优先级先执行`;
        }catch(error){
            state.textContent=error.message||'规则读取失败';
            if(!regexRules.length)renderRegexRuleEmpty('规则读取失败');
        }
    }

    async function previewRegexRule(){
        const filename=document.getElementById('tmdbRegexSampleFilename').value.trim();
        if(!filename){renderRegexPreview(null,'请输入样例文件名');return false;}
        const button=document.getElementById('previewTmdbRegexRuleBtn');
        button.disabled=true;
        try{
            const response=await fetch('/api/tools/tmdb-regex-rules/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule:regexRulePayload(),filename,parent_path:document.getElementById('tmdbRegexSampleParent').value.trim(),media_type:document.getElementById('tmdbRegexMediaType').value})});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'样例预览失败');
            renderRegexPreview(data);
            return Boolean(data.matched);
        }catch(error){
            renderRegexPreview(null,error.message||'样例预览失败');
            return false;
        }finally{
            button.disabled=false;
        }
    }

    async function saveRegexRule(event){
        event.preventDefault();
        const state=document.getElementById('tmdbRegexRuleFormState');
        const button=document.getElementById('saveTmdbRegexRuleBtn');
        const id=document.getElementById('tmdbRegexRuleId').value;
        button.disabled=true;
        state.textContent='正在校验并保存…';
        try{
            if(document.getElementById('tmdbRegexSampleFilename').value.trim()&&!(await previewRegexRule()))throw new Error('样例校验未通过');
            const response=await fetch(id?`/api/tools/tmdb-regex-rules/${id}`:'/api/tools/tmdb-regex-rules',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(regexRulePayload())});
            const data=await response.json();
            if(!response.ok)throw new Error(data.error||'规则保存失败');
            state.textContent=id?'规则已更新':'规则已创建';
            resetRegexRuleForm(data);
            await loadRegexRules(true);
        }catch(error){
            state.textContent=error.message||'规则保存失败';
        }finally{
            button.disabled=false;
        }
    }

    function parseSources(raw){
        let items=[];const canonical=String(raw??'').trim();
        try{items=canonical?JSON.parse(canonical):[];}catch(_){return [];}
        if(!Array.isArray(items))return [];
        const seen=new Set();return items.map((item,index)=>typeof item==='string'?{id:item,name:`源目录${index+1}`} : item)
            .filter(item=>item&&String(item.id||'').trim()&&String(item.id)!=='0'&&!seen.has(String(item.id))&&seen.add(String(item.id)))
            .map((item,index)=>({id:String(item.id),name:String(item.name||`源目录${index+1}`)}));
    }
    function syncSources(){if(sourceInput)sourceInput.value=JSON.stringify(sources);}
    function renderSources(){
        const list=document.getElementById('organizeSourceList');
        if(!list)return;
        list.replaceChildren();
        const countText=document.getElementById('organizeSourceCountText');
        if(countText)countText.textContent=`(支持多选 · 已选 ${sources.length} 项)`;
        if(!sources.length){
            const empty=document.createElement('div');
            empty.className='organize-source-empty';
            empty.innerHTML='<i data-lucide="folder-dashed"></i><span>尚未选择源目录，请点击右上角批量选择</span>';
            list.appendChild(empty);
            renderLucideIcons(list);
            return;
        }
        sources.forEach((source,index)=>{
            const row=document.createElement('div');
            row.className='organize-source-row';
            const badge=document.createElement('span');
            badge.className='organize-src-badge';
            badge.textContent=`SRC-${index+1}`;
            const icon=document.createElement('i');
            icon.dataset.lucide='folder';
            icon.className='organize-folder-icon';
            const textWrap=document.createElement('div');
            textWrap.className='organize-source-text-wrap';
            const idSpan=document.createElement('span');
            idSpan.className='organize-source-id';
            idSpan.textContent=source.name?`${source.name} (ID: ${source.id})`:source.id;
            textWrap.appendChild(idSpan);
            const remove=document.createElement('button');
            remove.type='button';
            remove.className='organize-row-remove';
            remove.title='移除';
            remove.setAttribute('aria-label',`移除 ${source.name||source.id}`);
            remove.innerHTML='<i data-lucide="x"></i>';
            remove.addEventListener('click',()=>{
                sources=sources.filter(item=>item.id!==source.id);
                syncSources();
                renderSources();
                saveConfig();
            });
            row.append(badge,icon,textWrap,remove);
            list.appendChild(row);
        });
        renderLucideIcons(list);
    }
    function renderTarget(id,name){
        document.getElementById('dstDirId').value=id||'';
        document.getElementById('dstDirName').value=name||'';
        const box=document.getElementById('organizeTargetValue');
        if(!box)return;
        box.replaceChildren();
        if(id){
            const strong=document.createElement('strong');
            strong.textContent=name?`${name} (ID: ${id})`:id;
            box.appendChild(strong);
        }else{
            const strong=document.createElement('strong');
            strong.textContent='未选择归档目标目录';
            const span=document.createElement('span');
            span.textContent='整理后文件归档到这里';
            box.append(strong,span);
        }
    }
    function pickDirectory(mode){
        openGuangYaDirectoryPicker({
            modalId:'organizeDirModal',title:mode==='source'?'批量选择整理源目录':'选择归档目标目录',
            multiple:mode==='source',selected:mode==='source'?sources:[],allowRoot:false,
            onSelect:directory=>{
                if(mode==='source'){
                    sources=directory.map(item=>({id:item.id,name:item.name}));
                    syncSources();
                    renderSources();
                    saveConfig();
                }else{
                    renderTarget(directory.id,directory.name);
                    saveConfig();
                }
            },
        });
    }
    function bool(id){return document.getElementById(id).checked;}
    function payload(){return {source_dirs:sources,target_dir_id:document.getElementById('dstDirId').value};}
    function setNsfwPanelExpanded(expanded,{focusEndpoint=false}={}){
        const panel=document.getElementById('nsfwProviderPanel');const body=document.getElementById('nsfwProviderBody');const disclosure=document.getElementById('nsfwProviderDisclosure');
        body.hidden=!expanded;panel.classList.toggle('is-collapsed',!expanded);disclosure.setAttribute('aria-expanded',String(expanded));
        if(expanded&&focusEndpoint)requestAnimationFrame(()=>document.getElementById('r_nsfw_endpoint').focus({preventScroll:true}));
    }
    function syncNsfwPanel({collapseWhenDisabled=false,focusEndpoint=false}={}){
        const enabled=bool('r_nsfw');const panel=document.getElementById('nsfwProviderPanel');
        panel.classList.toggle('is-disabled',!enabled);document.getElementById('nsfwProviderState').textContent=enabled?'已启用':'未启用';
        document.querySelectorAll('.nsfw-dependent').forEach(field=>{field.disabled=!enabled;});
        if(enabled)setNsfwPanelExpanded(true,{focusEndpoint});else if(collapseWhenDisabled)setNsfwPanelExpanded(false);
    }
    function updateDependencies(){
        syncNsfwPanel();
        const keepMulti=bool('r_keep_multi');document.getElementById('r_keep_remux').disabled=!keepMulti;
        const notify=bool('r_notify');document.getElementById('r_library_notify').disabled=!notify;document.getElementById('r_strm_notify').disabled=!notify||!bool('r_strm');
        document.getElementById('r_emby').disabled=!bool('r_strm');
        const scheduleEnabled=bool('r_schedule_enabled');document.getElementById('r_schedule_cron').disabled=!scheduleEnabled;
        const cronRow=document.getElementById('organizeCronRow');if(cronRow){cronRow.style.display=scheduleEnabled?'grid':'none';}
    }
    function finishConfigLoad(success){
        const button=document.getElementById('saveOrganizeConfigBtn');const state=document.getElementById('organizeSaveState');configReady=success;button.disabled=!success;button.setAttribute('aria-busy','false');
        if(success)configFieldLocks.forEach(({field,disabled})=>{field.disabled=disabled;});
        state.textContent=success?(isRules?'保存后立即成为 Web、TG、定时与本地整理的正式规则':'保存来源与目标，后续执行自动使用统一整理规则'):'配置读取失败，请刷新页面后重试';state.className=success?'':'is-error';
    }
    async function saveConfig(){
        if(!configReady)return;
        if(sourceInput)syncSources();if(isRules){extensionEditors.video?.ensure();extensionEditors.metadata?.ensure();}const state=document.getElementById('organizeSaveState');state.textContent='正在保存...';
        try{await saveAppConfig(workspace);state.textContent=isRules?'整理规则已保存':'目录配置已保存';state.className='is-success';if(!isRules)await loadStatus();}
        catch(error){state.textContent=error.message;state.className='is-error';}
    }
    async function preview(){
        if(!sources.length){await appAlert({type:'warning',title:'未选择源目录',message:'至少选择一个光鸭源目录后才能预览整理计划。'});return;}
        const btn=document.getElementById('previewBtn');btn.disabled=true;
        try{
            const maxFiles=Number(document.getElementById('r_maxfiles').value||100);
            if(!Number.isInteger(maxFiles)||maxFiles<1||maxFiles>1000)throw new Error('预览数量上限必须是 1-1000 的整数');
            const body={...payload(),max_files:maxFiles};
            const response=await fetch('/api/guangya/organize/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
            const data=await response.json();if(!response.ok)throw new Error(data.error||'预览失败');renderPreview(data);
        }catch(error){await appAlert({type:'error',title:'预览失败',message:error.message||'无法生成整理计划'});}finally{btn.disabled=false;}
    }
    function renderPreview(data){
        const card=document.getElementById('previewCard');card.hidden=false;const stats=data.stats||{};
        document.getElementById('previewStats').textContent=`总数 ${stats.total||0} · 匹配 ${stats.matched||0} · 需确认 ${stats.need_confirm||0} · 跳过 ${stats.skipped||0}`;
        const list=document.getElementById('planList');list.replaceChildren();
        if(!(data.plans||[]).length){list.innerHTML='<div class="empty-state"><p>没有符合规则的视频</p></div>';return;}
        (data.plans||[]).forEach(plan=>{
            const row=document.createElement('div');row.className='recent-item';
            const icon=document.createElement('div');icon.className='recent-poster';icon.innerHTML=`<i data-lucide="${plan.action==='move'?'check':'circle-help'}"></i>`;
            const info=document.createElement('div');info.className='recent-info';
            const name=document.createElement('div');name.className='recent-name';name.textContent=`[${plan.source_name}] ${plan.original_name}`;
            const original=document.createElement('div');original.className='recent-sub';original.textContent=`原：${plan.original_path||'/'} `;
            const target=document.createElement('div');target.className='recent-sub';target.textContent=plan.action==='move'?`新：${plan.target_path}/${plan.new_name}`:plan.note;
            const variant=document.createElement('div');variant.className='recent-sub';variant.textContent=`版本分类：${plan.variant_label||'未识别版本'} · 后缀：${plan.variant_suffix||'无'} · ${plan.conflict_note||'执行时按同一规则重新检查目标目录'}`;
            info.append(name,original,target,variant);row.append(icon,info);list.appendChild(row);
        });renderLucideIcons(list);card.scrollIntoView({behavior:'smooth',block:'start'});
    }
    function setOrganizeActionBusy(busy){
        organizeActionBusy=busy;if(isRules)return;
        document.getElementById('runOrganizeBtn').disabled=busy||organizeStatusRunning;document.getElementById('stopOrganizeBtn').disabled=busy||!organizeStatusRunning;document.getElementById('cleanEmptyBtn').disabled=busy||organizeStatusRunning;
    }
    async function run(){
        if(organizeActionBusy)return;
        if(!sources.length||!document.getElementById('dstDirId').value){await appAlert({type:'warning',title:'整理范围不完整',message:'请选择至少一个源目录，并指定唯一的归档目标目录。'});return;}
        const confirmed=await appConfirm({title:'执行网盘整理',message:`将实际整理 ${sources.length} 个源目录，移动光鸭文件，并按当前规则处理重命名、元数据和冲突。请先检查预览计划。`,confirmText:'执行整理',danger:true,verifyText:'ORGANIZE',verifyLabel:'输入 ORGANIZE 确认执行云盘写操作'});if(!confirmed)return;
        setOrganizeActionBusy(true);
        try{const response=await fetch('/api/guangya/organize/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||'启动失败');startPolling();}
        catch(error){await appAlert({type:'error',title:'整理任务启动失败',message:error.message||'无法启动整理任务'});}finally{setOrganizeActionBusy(false);await loadStatus();}
    }
    async function stop(){
        if(organizeActionBusy)return;const confirmed=await appConfirm({title:'停止整理任务',message:'停止后，已经完成的云盘移动不会自动回滚。',confirmText:'停止任务',danger:true});if(!confirmed)return;
        setOrganizeActionBusy(true);try{const response=await fetch('/api/guangya/organize/stop',{method:'POST'});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||'停止失败');}
        catch(error){await appAlert({type:'error',title:'停止任务失败',message:error.message||'无法停止整理任务'});}finally{setOrganizeActionBusy(false);await loadStatus();}
    }
    async function cleanEmpty(){
        if(organizeActionBusy)return;if(!sources.length){await appAlert({type:'warning',title:'未选择源目录',message:'至少选择一个光鸭源目录后才能清理空文件夹。'});return;}
        const confirmed=await appConfirm({title:'清理空目录',message:`扫描 ${sources.length} 个源目录并删除其中的空子目录。源根目录不会删除。`,confirmText:'清理空目录',danger:true});if(!confirmed)return;
        setOrganizeActionBusy(true);
        try{
            const response=await fetch('/api/guangya/organize/clean-empty',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_dirs:sources})});
            const data=await response.json().catch(()=>({}));
            if(!response.ok)throw new Error(data.error||'清理失败');
            const failed=(data.scan_failures||0)+(data.delete_failures||0);
            if(data.partial||failed){
                await appAlert({type:'warning',title:'空目录清理部分完成',message:`已清理 ${data.cleaned||0} 个空目录，${failed} 项未完成。请检查光鸭连接与目录权限后重试。`});
                return;
            }
            await appAlert({type:'success',title:'空目录清理完成',message:`已清理 ${data.cleaned||0} 个空目录。`});
        }
        catch(error){await appAlert({type:'error',title:'空目录清理失败',message:error.message||'无法清理空目录'});}
        finally{setOrganizeActionBusy(false);await loadStatus();}
    }
    function renderScheduleStatus(schedule){
        if(!schedule)return;
        const next=document.getElementById('organizeScheduleNext');const last=document.getElementById('organizeScheduleLast');
        if(!next||!last)return;
        if(!schedule.enabled)next.textContent='自动整理未启用';
        else if(schedule.config_error)next.textContent=`调度暂停：${schedule.config_error}`;
        else if(!schedule.cron_valid)next.textContent='调度暂停：Cron 必须是标准 5 段格式';
        else next.textContent=schedule.next_run?`下次运行：${schedule.next_run}`:'正在计算下次运行时间';
        const result=schedule.last_result||{};if(!Object.keys(result).length)return;
        const outcome={started:'已启动',completed:'已完成',failed:'失败',stopped:'已停止',skipped:'已跳过'}[result.outcome]||result.outcome||'未知';
        const stats=result.stats||{};const counts=Object.keys(stats).length?` · 移动 ${stats.moved||0} / 跳过 ${stats.skipped||0} / 失败 ${stats.failed||0}`:'';
        last.textContent=`最近结果：${outcome}${result.finished_at||result.started_at?` · ${result.finished_at||result.started_at}`:''}${counts}${result.message?` · ${result.message}`:''}`;
    }
    function renderGroupProgress(data){
        // 组级进度只做局部文本更新：运行期间元素始终占位，避免出现/消失造成布局跳动。
        const el=document.getElementById('organizeGroupProgress');
        if(!el)return;
        const progress=data.group_progress||{};
        const total=Number(progress.total||0);
        const index=Number(progress.current_index||0);
        const running=['running','stopping'].includes(data.status);
        if(!running||!total){
            if(!el.hidden){el.hidden=true;el.textContent='';}
            return;
        }
        const parts=[`正在处理 ${index||0}/${total}`];
        if(progress.current_group)parts.push(progress.current_group);
        if(progress.current_stage_label)parts.push(progress.current_stage_label);
        const fileTotal=Number(progress.current_file_total||0);
        const fileIndex=Number(progress.current_file_index||0);
        if(fileTotal)parts.push(`文件 ${fileIndex}/${fileTotal}`);
        const text=parts.join(' · ');
        if(el.hidden)el.hidden=false;
        if(el.textContent!==text)el.textContent=text;
    }
    function renderStatus(data){
        const renderKey=JSON.stringify({
            status:data.status||'',message:data.message||'',error:data.error||'',
            current_source:data.current_source||'',stats:data.stats||{},schedule:data.schedule||{},
            group_progress:data.group_progress||{}
        });
        if(renderKey===lastStatusRenderKey)return;
        lastStatusRenderKey=renderKey;

        renderScheduleStatus(data.schedule);
        if(isRules)return;
        renderGroupProgress(data);
        const tag=document.getElementById('organizeStateTag');
        const running=['running','stopping'].includes(data.status);organizeStatusRunning=running;
        tag.textContent={idle:'空闲',running:'整理中',stopping:'停止中',completed:'已完成',partial:'部分完成',stopped:'已停止',failed:'失败'}[data.status]||data.status;
        const tagTone=running?'is-active':data.status==='failed'?'is-danger':['completed','partial'].includes(data.status)?'is-success':'';
        tag.className=`organize-state-tag${tagTone?` ${tagTone}`:''}`;

        const iconDiv=document.getElementById('organizeStatusIcon');
        const iconName=data.status==='running'?'loader-2':['completed','partial'].includes(data.status)?'check-circle-2':data.status==='failed'?'alert-circle':'sparkles';
        if(iconDiv.dataset.iconName!==iconName){
            iconDiv.dataset.iconName=iconName;
            iconDiv.innerHTML=`<i data-lucide="${iconName}" ${data.status==='running'?'class="spin"':''}></i>`;
            renderLucideIcons(iconDiv);
        }else{
            iconDiv.querySelector('svg')?.classList.toggle('spin',data.status==='running');
        }

        const stats=data.stats||{};
        document.getElementById('organizeStatusTitle').textContent=data.message||'暂无任务';
        document.getElementById('organizeStatusDetail').textContent=data.error||`${data.current_source||''}${Object.keys(stats).length?` · 已移动 ${stats.moved||0} · 跳过 ${stats.skipped||0} · 失败 ${stats.failed||0}`:' · 建议先预览识别结果再执行'}`;

        document.getElementById('runOrganizeBtn').disabled=organizeActionBusy||running;
        document.getElementById('stopOrganizeBtn').disabled=organizeActionBusy||!running;
        document.getElementById('cleanEmptyBtn').disabled=organizeActionBusy||running;
        if(!running&&pollTimer){clearInterval(pollTimer);pollTimer=null;}
    }
    async function loadStatus(){const serial=++statusRequestSerial;try{const response=await fetch('/api/guangya/organize/status');const data=await response.json();if(serial===statusRequestSerial)renderStatus(data);}catch(_){} }
    function startPolling(){loadStatus();if(!pollTimer)pollTimer=setInterval(loadStatus,2000);}


    document.getElementById('saveOrganizeConfigBtn').addEventListener('click',saveConfig);
    if(isRules){
    document.getElementById('openPreprocessRulesBtn').addEventListener('click',event=>{preprocessRulesModal.open(event.currentTarget);loadPreprocessRules(preprocessRulesLoaded);});
    document.getElementById('newPreprocessRuleBtn').addEventListener('click',()=>resetPreprocessRuleForm());
    document.getElementById('restorePreprocessRulesBtn').addEventListener('click',restorePreprocessRules);
    document.getElementById('previewPreprocessRuleBtn').addEventListener('click',previewPreprocessRule);
    document.getElementById('preprocessRuleForm').addEventListener('submit',savePreprocessRule);
    document.getElementById('preprocessAction').addEventListener('change',syncPreprocessActionFields);
    document.getElementById('openRecognitionKnowledgeBtn').addEventListener('click',event=>{recognitionKnowledgeModal.open(event.currentTarget);loadRecognitionKnowledge(recognitionKnowledgeLoaded);});
    document.getElementById('newRecognitionKnowledgeBtn').addEventListener('click',()=>resetRecognitionKnowledgeForm());
    document.getElementById('recognitionKnowledgeForm').addEventListener('submit',saveRecognitionKnowledge);
    document.getElementById('recognitionKnowledgeSearch').addEventListener('input',scheduleRecognitionKnowledgeSearch);
    document.getElementById('recognitionKnowledgeFilter').addEventListener('change',()=>loadRecognitionKnowledge(true));
    document.getElementById('openTmdbRegexRulesBtn').addEventListener('click',event=>{regexRulesModal.open(event.currentTarget);loadRegexRules(regexRulesLoaded);});
    document.getElementById('newTmdbRegexRuleBtn').addEventListener('click',()=>resetRegexRuleForm());
    document.getElementById('previewTmdbRegexRuleBtn').addEventListener('click',previewRegexRule);
    document.getElementById('tmdbRegexRuleForm').addEventListener('submit',saveRegexRule);
    document.getElementById('tmdbRegexMediaType').addEventListener('change',syncRegexSeasonField);
    document.querySelectorAll('.cron-preset-btn').forEach(btn=>{btn.addEventListener('click',()=>{const cronInput=document.getElementById('r_schedule_cron');if(cronInput){cronInput.value=btn.dataset.cron;cronInput.dispatchEvent(new Event('input',{bubbles:true}));}});});
    ['r_keep_multi','r_notify','r_strm','r_schedule_enabled'].forEach(id=>document.getElementById(id).addEventListener('change',updateDependencies));
    document.getElementById('r_nsfw').addEventListener('change',()=>{updateDependencies();syncNsfwPanel({collapseWhenDisabled:true,focusEndpoint:bool('r_nsfw')});});
    document.getElementById('nsfwProviderDisclosure').addEventListener('click',()=>setNsfwPanelExpanded(document.getElementById('nsfwProviderBody').hidden));

    loadPreprocessRules(false);
    loadRecognitionKnowledge(false);
    syncPreprocessActionFields();
    loadAppConfig().then(config=>{
        fillConfigFields(workspace,config);
        extensionEditors.video?.set(config.GY_ORGANIZE_VIDEO_EXTS||extensionDefaults.video.join(','));
        extensionEditors.metadata?.set(config.GY_ORGANIZE_METADATA_EXTS||extensionDefaults.metadata.join(','));
        if(!config.GY_ORGANIZE_SMALL_FILE_MB)document.getElementById('r_small').value='10';
        if(!config.GY_ORGANIZE_NSFW_CATEGORY_NAME)document.getElementById('r_nsfw_category').value='成人内容';
        if(!config.GY_ORGANIZE_NSFW_TIMEOUT_SECONDS)document.getElementById('r_nsfw_timeout').value='8';
        if(!config.GY_ORGANIZE_SCHEDULE_CRON)document.getElementById('r_schedule_cron').value='0 4 * * *';
        if(!config.GY_ORGANIZE_CONFLICT_STRATEGY)document.getElementById('r_conflict').value='1';
        if(!config.GY_ORGANIZE_AUTOMATIC_MATCH_PRESET)document.getElementById('r_automatic_match_preset').value='balanced';
        ['r_region','r_year','r_remux','r_res','r_dolby','r_clean','r_notify','r_library_notify','r_strm','r_strm_notify','r_emby'].forEach(id=>{
            const field=document.getElementById(id);const key=field.dataset.key;if(config[key]===undefined||config[key]==='')field.checked=true;
        });
        finishConfigLoad(true);updateDependencies();syncNsfwPanel({collapseWhenDisabled:true});
    }).catch(()=>{updateDependencies();syncNsfwPanel({collapseWhenDisabled:true});finishConfigLoad(false);});
    loadStatus();
    if(!scheduleTimer)scheduleTimer=setInterval(loadStatus,30000);
    }else{
    document.getElementById('addOrganizeSourceBtn').addEventListener('click',()=>pickDirectory('source'));
    document.getElementById('pickOrganizeTargetBtn').addEventListener('click',()=>pickDirectory('target'));
    document.getElementById('previewBtn').addEventListener('click',preview);
    document.getElementById('runOrganizeBtn').addEventListener('click',run);
    document.getElementById('stopOrganizeBtn').addEventListener('click',stop);
    document.getElementById('cleanEmptyBtn').addEventListener('click',cleanEmpty);
    const cleanCheckbox=document.getElementById('r_clean');
    if(cleanCheckbox)cleanCheckbox.addEventListener('change',()=>saveConfig());
    loadAppConfig().then(config=>{
        fillConfigFields(workspace,config);sources=parseSources(config.GY_ORGANIZE_SOURCE_DIRS);syncSources();renderSources();
        renderTarget(config.GY_ORGANIZE_TARGET_DIR||'',config.GY_ORGANIZE_TARGET_DIR_NAME||'');
        if(cleanCheckbox&&config.GY_ORGANIZE_CLEAN_EMPTY!==undefined&&config.GY_ORGANIZE_CLEAN_EMPTY!==''){
            cleanCheckbox.checked=String(config.GY_ORGANIZE_CLEAN_EMPTY)==='true'||config.GY_ORGANIZE_CLEAN_EMPTY===true||config.GY_ORGANIZE_CLEAN_EMPTY==='1'||config.GY_ORGANIZE_CLEAN_EMPTY===1;
        }
        finishConfigLoad(true);
    }).catch(()=>{renderSources();renderTarget('','');finishConfigLoad(false);});
    loadStatus().then(()=>{if(!['IDLE','空闲','已完成','DONE','部分完成','PARTIAL','已停止','STOPPED','失败','FAILED'].includes(document.getElementById('organizeStateTag')?.textContent?.trim()))startPolling();});
    }
})();
