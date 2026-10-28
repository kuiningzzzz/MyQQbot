featureAdminControls.ai = async function (row) {
  const $ = selector => row.querySelector(selector);
  const $$ = selector => [...row.querySelectorAll(selector)];
  const fields = {};
  let items = [], openedPath = '', fileHash = '', savedContent = '', openRevision = 0;
  const hints = {PERSON:'职业、称呼、关系等稳定背景。', PREFERENCE:'长期喜好、习惯与沟通偏好。', RECENTNESS:'近期计划、状态与待办，建议注明日期。', EVENT:'具体经历、项目背景或持续更新的主题。', GROUP:'本群群规、专属梗、共同活动与项目；群号为身份主键，主题名称可自由命名。', GLOSSARY:'跨群、跨用户通用的术语解释。', WORLD:'全局共享的公开知识；可填写多层主题路径，如科技/人工智能/模型进展。Markdown进入索引和维护，其他资料文件不直接索引。', SOUL:'自身人格，每轮作为系统人格全文加载。保持简洁、稳定，可由Agent自主适度更新，不参与向量检索。'};
  function status(message, error=false) {
    $('[data-ai-status]').textContent=message;
    $('[data-ai-status]').dataset.error=String(error);
  }
  async function request(path, body) {
    const response=await fetch(path, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));
    return data;
  }
  function action(selector, work) {
    $(selector).onclick=async()=>{
      const button=$(selector); button.disabled=true;
      try { await work(); } catch(error) { status(error.message,true); }
      finally { button.disabled=false; updateDirty(); }
    };
  }
  function tab(name) {
    $$('[data-ai-tab]').forEach(button=>{const selected=button.dataset.aiTab===name;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
    $$('[data-ai-view]').forEach(view=>view.hidden=view.dataset.aiView!==name);
    row.dispatchEvent(new CustomEvent('ai-tab-open', {detail:name}));
  }
  const tabs=$$('[data-ai-tab]');
  tabs.forEach((button,index)=>{
    button.onclick=()=>tab(button.dataset.aiTab);
    button.onkeydown=event=>{
      if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
        event.preventDefault();
        const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
        tab(tabs[next].dataset.aiTab);tabs[next].focus();
      }
    };
  });
  function field(parent,key,label,value) {
    const wrapper=document.createElement('label'),input=document.createElement('input'),title=document.createElement('span');
    input.type=typeof value==='boolean'?'checkbox':'text';title.textContent=label;
    wrapper.className=input.type==='checkbox'?'ai-check':'ai-field';
    if(input.type==='checkbox'){input.checked=value;wrapper.append(input,title);}else{input.value=value??'';wrapper.append(title,input);}
    parent.append(wrapper);fields[key]=input;
  }
  const config=await request('/api/ai/config');
  let personaHash=config.agent_prompt_hash;
  field($('[data-ai-switches]'),'stickers_enabled','启用表情库自动同步、后台理解与Agent发送',config.stickers_enabled);
  initStickerPanel(row);
  for(const [key,label] of Object.entries({bot_qq:'机器人QQ号',nickname:'网名／唤醒姓名',account_description:'账号身份说明'}))field($('[data-ai-identity]'),key,label,config[key]);
  for(const [key,label] of Object.entries({chat_enabled:'启用聊天',autonomy_enabled:'启用主动生活（持续循环，可自主休息）',summary_enabled:'启用群聊总结',memory_enabled:'启用长时记忆',memory_auto_learn:'自动整理消息队列',workspace_enabled:'启用文件下载与发送',browser_enabled:'启用后台网页浏览与界面操作',ocr_enabled:'启用图片／PDF OCR',audio_enabled:'启用云端声音理解',voice_enabled:'启用语音合成与QQ语音发送',vector_index_enabled:'启用本地向量检索'}))field($('[data-ai-switches]'),key,label,config[key]);
  for(const [key,label] of Object.entries({embedding_model:'嵌入模型名称或本地目录',timezone:'时间显示时区'}))field($('[data-ai-memory]'),key,label,config[key]);
  $('[data-ai-prompt]').value=config.agent_prompt;
  function renderRuntime(data) {
    $('[data-ai-pending]').textContent=data.pending_messages;
    const container=$('[data-ai-runtime]');container.replaceChildren();
    const media=data.media_queue||{};
    for(const [label,value] of [['模型','deepseek-flash'],['API Key',data.api_key_configured?'环境已配置':'环境未配置：请设置 DEEPSEEK_API_KEY'],['联网搜索','DeepSeek 原生搜索'],['dsh版本',data.dsh_version],['媒体后台解析',`等待 ${media.queued||0} · 重试 ${media.retry||0} · 进行中 ${media.running||0} · 失败 ${media.failed||0}`],['待整理媒体补充',data.pending_media_memory||0],['机器人账号',data.observed_self_ids.join('、')||'尚未连接'],['工作区',data.workspace_path],['记忆规则',data.memory_skill_path]]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;container.append(dt,dd);
    }
  }
  renderRuntime(config);
  let lifeBusy=false,lifeCursor=0,lifeFirst=0,lifeGeneration=0;
  function showLife(data){
    const until=data.until?new Date(data.until*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'—';
    const chain=data.sleep_chain;
    const waiting=data.mode==='waiting';
    const scenes=Object.entries(data.conversation_waits||{}).map(([scene,item])=>`${scene}（至 ${new Date(item.until*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'})}，参与者：${item.participants.join('、')||'等待接话'}）`).join('\n');
    $('[data-life-state]').textContent=`${data.text}\n主动模式：${data.active?'运行中':'未运行'} · 当前目标：${data.focus} · ${waiting?'待聊至':'休息至'}：${until}（北京时间）${waiting?'\n会话待机：等待续话，不调用模型，不计入睡眠。':''}${scenes?'\n接话窗口：\n'+scenes:''}${['sleeping','deep_sleep'].includes(data.mode)?'\n休息原因：'+(data.reason||'未填写')+' · 唤醒事件：'+((data.wake_on||[]).join('、')||'仅到期／强制叫醒')+'\n事件筛选：'+JSON.stringify(data.filters||{}):''}${chain?'\n本段累计实际休息 '+(chain.seconds/3600).toFixed(2)+' 小时，共 '+chain.parts+' 次安排':''}${data.last_error?'\n最近错误：'+data.last_error:''}`;
    const list=$('[data-life-contacts]');list.replaceChildren();
    for(const [scene,policy] of Object.entries(data.contacts)){
      const button=document.createElement('button');button.type='button';button.className='ai-secondary';button.style.margin='4px';
      button.textContent=`${scene} · ${policy.enabled?'允许':'暂停'} · ${policy.start}—${policy.end}`;
      button.onclick=()=>{$('[data-life-scene]').value=scene;$('[data-life-start]').value=policy.start;$('[data-life-end]').value=policy.end;$('[data-life-contact-enabled]').checked=policy.enabled;};list.append(button);
    }
    if(!list.childElementCount)list.textContent='尚未设置主动联系许可；Agent仍可在自己的工作区活动。';
  }
  async function lifeLogs(mode='latest'){
    if(mode==='live'&&$('[data-life-filter]').value==='sleep_summary')mode='latest';
    if(lifeBusy)return;lifeBusy=true;
    const generation=lifeGeneration;
    try{
      const params=new URLSearchParams({limit:100,kind:$('[data-life-filter]').value});
      if(mode==='live'&&lifeCursor)params.set('after',lifeCursor);
      if(mode==='older'&&lifeFirst)params.set('before',lifeFirst);
      const data=await request('/api/ai/activity?'+params);
      if(generation!==lifeGeneration)return;
      showLife(data.state);
      const list=$('[data-life-log-list]');
      if(mode!=='live'){list.replaceChildren();lifeCursor=0;lifeFirst=0;}
      if(mode==='live'&&data.entries.length&&!list.childElementCount)list.replaceChildren();
      for(const entry of data.entries){
        lifeCursor=Math.max(lifeCursor,entry.id);lifeFirst=lifeFirst?Math.min(lifeFirst,entry.id):entry.id;
        const detail=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
        detail.className='ai-result';summary.textContent=`#${entry.id} ${new Date(entry.time*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})} · ${entry.kind} · ${entry.role||'宿主'} ${entry.scene} ${entry.task.slice(0,8)}`;
        pre.textContent=JSON.stringify(entry.data,null,2);detail.append(summary,pre);list.append(detail);
      }
      while(list.childElementCount>300)list.firstElementChild.remove();
      if(!list.childElementCount)list.textContent='暂无符合条件的日志。';
    }finally{lifeBusy=false;}
  }
  action('[data-life-refresh]',async()=>showLife(await request('/api/ai/life')));
  action('[data-life-wake]',async()=>{showLife(await request('/api/ai/life/wakeup',{}));status('已强制叫醒。');});
  action('[data-life-sleep]',async()=>{await request('/api/ai/life/sleep',{duration_seconds:Number($('[data-life-seconds]').value),deep:$('[data-life-deep]').checked});showLife(await request('/api/ai/life'));status('休息状态已保存。');});
  action('[data-life-contact-save]',async()=>{await request('/api/ai/life/contacts',{action:'set',scene:$('[data-life-scene]').value,policy:{enabled:$('[data-life-contact-enabled]').checked,start:$('[data-life-start]').value,end:$('[data-life-end]').value}});showLife(await request('/api/ai/life'));status('主动联系许可已保存。');});
  action('[data-life-contact-delete]',async()=>{await request('/api/ai/life/contacts',{action:'delete',scene:$('[data-life-scene]').value});showLife(await request('/api/ai/life'));});
  action('[data-life-logs]',async()=>{lifeGeneration++;await lifeLogs();});
  action('[data-life-older]',async()=>{$('[data-life-live]').checked=false;await lifeLogs('older');});
  $('[data-life-filter]').onchange=async()=>{lifeGeneration++;try{await lifeLogs();}catch(error){status(error.message,true);}};
  row.addEventListener('ai-tab-open',async event=>{if(event.detail==='life'){try{await lifeLogs();}catch(error){status(error.message,true);}}});
  const lifeTimer=setInterval(async()=>{
    if(!row.isConnected){clearInterval(lifeTimer);return;}
    if(document.hidden||$('[data-ai-view="life"]').hidden||!$('[data-life-live]').checked)return;
    try{await lifeLogs('live');}catch(error){status(error.message,true);}
  },2000);
  async function renderAudio(){
    const data=await request('/api/ai/audio/status');
    $('[data-audio-state]').textContent=`百炼Key：${data.configured?'已配置':'未配置'}；声音理解：${data.hearing_enabled?'开启':'关闭'}（${data.audio_model}）；语音发送：${data.voice_enabled?'开启':'关闭'}（${data.tts_model} / ${data.voice}）。临时语音缓存最多${data.cache_limit_mib}MiB，保留${data.cache_hours}小时。${data.last_error?'最近错误：'+data.last_error:''}`;
  }
  await renderAudio();
  action('[data-audio-refresh]',renderAudio);
  action('[data-audio-preview]',async()=>{
    status('正在合成试听音频…');
    const data=await request('/api/ai/audio/preview',{text:$('[data-audio-text]').value});
    if(!data.success)throw new Error(data.error||data.code||'合成未完成');
    const player=$('[data-audio-player]');player.src=data.audio_url;player.hidden=false;player.load();
    await renderAudio();status('试听音频已准备好，点击播放即可。没有发送QQ消息。');
  });
  action('[data-audio-analyze]',async()=>{
    const file=$('[data-audio-file]').files[0];
    if(!file)throw new Error('请先选择音频。');
    if(file.size>20*1024*1024)throw new Error('音频最多20MiB。');
    status('正在后台请求声音理解…');
    const response=await fetch('/api/ai/audio/analyze',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
    const data=await response.json();
    if(!response.ok)throw new Error(data.detail||'声音理解失败');
    $('[data-audio-result]').textContent=data.description;await renderAudio();status('音频解析完成。没有发送QQ消息。');
  });
  action('[data-ai-save]',async()=>{
    const body=Object.fromEntries(Object.entries(fields).map(([key,input])=>[key,input.type==='checkbox'?input.checked:input.value]));
    body.agent_prompt=$('[data-ai-prompt]').value;
    body.agent_prompt_hash=personaHash;
    const data=await request('/api/ai/config',body);personaHash=data.agent_prompt_hash;
    renderRuntime(data);await renderAudio();status('账号、人设与功能设置已保存。');
  });
  function dirty(){return !!openedPath&&$('[data-memory-content]').value!==savedContent;}
  function updateDirty(){const changed=dirty();$('[data-memory-dirty]').textContent=changed?'未保存修改':'已保存';$('[data-memory-save]').disabled=!changed;}
  function mayLeave(){return !dirty()||window.confirm('当前记忆有未保存的修改，确定放弃这些修改吗？');}
  $('[data-memory-content]').oninput=updateDirty;
  window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
  function showFile(data) {
    openedPath=data.path;fileHash=data.hash;savedContent=data.content;
    $('[data-memory-content]').value=data.content;
    $('[data-memory-empty]').hidden=true;$('[data-memory-editor]').hidden=false;
    const item=items.find(item=>item.path===openedPath);
    $('[data-memory-title]').textContent=item?.title||openedPath.split('/').at(-1);
    $('[data-memory-meta]').textContent=(item?.category==='WORLD'?'世界知识 · ':item?.category==='SOUL'?'自身人格 · 每轮加载 · ':item?.group_id?`群 ${item.group_id} · `:item?.qq?`${item.nickname} · QQ ${item.qq} · `:'公共记忆 · ')+openedPath+(item?.updated_at?' · '+new Date(item.updated_at*1000).toLocaleString():'');
    updateDirty();renderList();
  }
  async function openFile(path,force=false){
    if(!force&&(path===openedPath||!mayLeave()))return;
    const revision=++openRevision;
    const data=await request('/api/ai/file?path='+encodeURIComponent(path));
    if(revision!==openRevision)return;
    if(!data.exists)throw new Error('文件已被删除，请刷新列表。');
    showFile(data);status('已打开记忆。');
  }
  function renderList(){
    const container=$('[data-memory-list]');container.replaceChildren();
    const query=$('[data-memory-filter]').value.trim().toLowerCase(),category=$('[data-memory-category]').value;
    const visible=items.filter(item=>(!category||item.category===category)&&`${item.nickname} ${item.qq} ${item.group_id||''} ${item.title} ${item.path}`.toLowerCase().includes(query));
    let previous=null;
    for(const item of visible){
      const group=item.category==='WORLD'?'world':item.category==='SOUL'?'soul':item.group_id?`group:${item.group_id}`:item.qq?`user:${item.qq}`:'common';
      if(group!==previous){const heading=document.createElement('div');heading.className='ai-file-group';heading.textContent=item.category==='WORLD'?'世界知识':item.category==='SOUL'?'自身人格 · 每轮加载':item.group_id?`群聊 · ${item.group_id}`:item.qq?`${item.nickname} · ${item.qq}`:'公共记忆';container.append(heading);previous=group;}
      const button=document.createElement('button');button.type='button';button.className='ai-file';button.setAttribute('aria-pressed',String(item.path===openedPath));
      button.append(document.createTextNode(item.title));
      if(['EVENT','GROUP','WORLD'].includes(item.category)){const subtitle=document.createElement('small');subtitle.textContent=item.category==='WORLD'?item.path.slice('world/'.length):item.category==='GROUP'?'群聊记忆':'主题与事件';button.append(subtitle);}
      button.onclick=async()=>{try{await openFile(item.path);}catch(error){status(error.message,true);}};container.append(button);
    }
    if(!visible.length){const empty=document.createElement('p');empty.className='ai-muted';empty.textContent=items.length?'没有匹配的记忆。':'暂无记忆，点击“新建记忆”开始。';container.append(empty);}
  }
  async function refresh(){
    const data=await request('/api/ai/files');items=data.items;
    $('[data-ai-users]').textContent=new Set(items.map(item=>item.qq).filter(Boolean)).size;
    $('[data-ai-file-count]').textContent=items.length;renderList();
  }
  $('[data-memory-filter]').oninput=renderList;$('[data-memory-category]').onchange=renderList;
  action('[data-memory-refresh]',async()=>{await refresh();renderRuntime(await request('/api/ai/config'));status('列表已刷新，当前编辑内容保留。');});
  action('[data-memory-reload]',async()=>{if(mayLeave())await openFile(openedPath,true);});
  action('[data-memory-save]',async()=>{
    const path=openedPath,content=$('[data-memory-content]').value;
    const data=await request('/api/ai/file',{path,content,expected_hash:fileHash});
    if(openedPath===path){fileHash=data.hash;savedContent=content;}
    await refresh();updateDirty();
    status(path.startsWith('soul/')?'人格已保存，下轮上下文生效；不参与向量检索。':data.indexed?'记忆与索引已保存。':'记忆已保存。'+(data.index_error||'本地向量索引未启用，文本检索仍可使用。'));
  });
  action('[data-memory-delete]',async()=>{
    if(!window.confirm(`确定删除这份记忆吗？\n${openedPath}${dirty()?'\n未保存的修改也将丢弃。':''}`))return;
    const path=openedPath;
    await request('/api/ai/file/delete',{path,expected_hash:fileHash});
    if(openedPath===path){++openRevision;openedPath='';fileHash='';savedContent='';$('[data-memory-editor]').hidden=true;$('[data-memory-empty]').hidden=false;}
    await refresh();status('记忆文件已删除。');
  });
  const dialog=$('[data-memory-create]');
  function createCategory(){
    const category=$('[data-create-category]').value;
    $('[data-create-user-field]').hidden=['GLOSSARY','GROUP','SOUL','WORLD'].includes(category);$('[data-create-qq]').required=!['GLOSSARY','GROUP','SOUL','WORLD'].includes(category);
    $('[data-create-group-field]').hidden=category!=='GROUP';$('[data-create-group]').required=category==='GROUP';
    $('[data-create-title-field]').hidden=!['EVENT','GROUP','SOUL','WORLD'].includes(category);$('[data-create-title]').required=['EVENT','GROUP','SOUL','WORLD'].includes(category);$('[data-create-hint]').textContent=hints[category];$('[data-create-title]').placeholder=category==='WORLD'?'例如：科技/人工智能/模型进展':'例如：摄影项目';
  }
  $('[data-create-category]').onchange=createCategory;
  $('[data-memory-new]').onclick=()=>{
    if(!mayLeave())return;
    const selected=items.find(item=>item.path===openedPath);$('[data-create-qq]').value=selected?.qq||'';$('[data-create-group]').value=selected?.group_id||'';$('[data-create-error]').textContent='';createCategory();dialog.showModal();
  };
  $('[data-create-cancel]').onclick=()=>dialog.close();
  $('[data-memory-create-form]').onsubmit=async event=>{
    event.preventDefault();const button=$('[data-create-submit]');button.disabled=true;
    try{
      const data=await request('/api/ai/file/create',{qq:$('[data-create-qq]').value.trim(),group_id:$('[data-create-group]').value.trim(),category:$('[data-create-category]').value,title:$('[data-create-title]').value.trim()});
      $('[data-memory-filter]').value='';$('[data-memory-category]').value='';
      await refresh();showFile(data);dialog.close();status('记忆文件已创建，可以开始编辑。');$('[data-memory-content]').focus();
    }catch(error){$('[data-create-error]').textContent=error.message;}finally{button.disabled=false;}
  };
  action('[data-memory-test]',async()=>{
    const query=$('[data-memory-query]').value.trim();if(!query)throw new Error('请输入查询内容。');status('正在检索记忆…');
    const data=await request('/memory/query',{query,target_qq:$('[data-memory-qq]').value.trim()||null,target_group:$('[data-memory-group]').value.trim()||null,scope:$('[data-memory-scope]').value});
    if(!data.success)throw new Error(data.error||'检索失败');
    const container=$('[data-memory-result]');container.replaceChildren();
    for(const hit of data.memories){
      const card=document.createElement('div');card.className='ai-result';const button=document.createElement('button');button.type='button';button.className='ai-secondary';button.textContent=hit.source||'打开记忆';
      button.onclick=async()=>{try{if(!mayLeave())return;await openFile(hit.source,true);tab('memory');}catch(error){status(error.message,true);}};
      const text=document.createElement('pre');text.textContent=hit.content;card.append(button,text);container.append(card);
    }
    if(!data.memories.length)container.textContent='没有找到相关记忆。';status(`检索完成，找到 ${data.memories.length} 条结果。`);
  });
  action('[data-integrate]',async()=>{
    status('正在整理消息队列…');const data=await request('/memory/integrate',{mode:'queue'});
    $('[data-maintenance-result]').textContent=`已处理 ${data.processed} 条消息，更新 ${data.updated_files?.length||0} 个记忆文件。`;
    await refresh();renderRuntime(await request('/api/ai/config'));status('记忆整理完成。');
  });
  action('[data-reindex]',async()=>{status('正在重建索引…');const data=await request('/api/ai/reindex',{});if(!data.success)throw new Error(data.error||'本地向量索引未启用');status('索引重建完成。');});
  action('[data-workspace-status]',async()=>{const data=await request('/api/ai/workspace/status');if(!data.success)throw new Error(data.error);status('dsh原生工作区已就绪。');});
  await refresh();
  if(!config.api_key_configured)status('尚未配置模型密钥：请在服务器 .env 中设置 DEEPSEEK_API_KEY，然后重启服务。',true);
  else if(config.integration_error||config.index_error)status(config.integration_error||config.index_error,true);
};
