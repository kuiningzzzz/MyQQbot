featureAdminControls.timing = async row => {
  const q = s => row.querySelector(s), form = q('.timing-form'), list = q('.timing-list');
  let tasks = [], editing = null;
  const fields = form.elements;
  const formats = {'-y':'MM-DD-HH-MM-SS，如 10-01-00-00-00','-m':'DD-HH-MM-SS，如 01-08-00-00','-w':'01至07-HH-MM-SS，01周一，07周日','-d':'HH-MM-SS，如 08-00-00','-o':'YY-MM-DD-HH-MM-SS，也支持四位年份'};
  const hint = () => {q('.timing-format').textContent = formats[fields.flag.value]; fields.time.placeholder = formats[fields.flag.value].split('，')[0]; fields.delivery.disabled = fields.kind.value !== 'ai'; fields.owner_grant.disabled = fields.kind.value !== 'ai';};
  fields.flag.addEventListener('change',hint); fields.kind.addEventListener('change',hint); hint();
  async function request(url, options={}) {
    const r = await fetch(url,options); const data = await r.json();
    if(!r.ok) throw new Error(typeof data.detail==='string'?data.detail:'请求失败，请刷新重试');
    return data;
  }
  const json = (method,body) => ({method, headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function edit(task) {
    editing = task; form.hidden = false; form.reset();
    q('.timing-form-status').textContent = '';
    q('.timing-form-title').textContent = task ? '编辑任务 '+task.id : '新增任务';
    if(task) {
      const [type,id] = task.scene.split(':');
      for(const key of ['kind','flag','time','bot_qq','requester','delivery']) fields[key].value = task[key] || '';
      fields.scene_type.value = type; fields.scene_id.value = id; fields.content.value = task.content;
      fields.enabled.checked = task.enabled; fields.owner_grant.checked = task.owner_grant;
    }
    hint(); fields.scene_id.focus();
  }
  const element = (tag,text,cls) => {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
  function render() {
    list.replaceChildren(); const filter=q('.timing-filter').value, search=q('.timing-search').value.toLowerCase();
    const selected=tasks.filter(t=>(!filter||t.kind===filter)&&[t.scene,t.id,t.content].join(' ').toLowerCase().includes(search));
    list.append(element('p',`显示 ${selected.length} / ${tasks.length} 个任务`,'timing-meta'));
    for(const t of selected) {
      const card=element('div',undefined,'timing-card');
      card.append(element('strong',`${t.kind==='ai'?'AI任务':'固定消息'} · ${t.scene} · ${t.enabled?'启用':'暂停 / 已完成'}`));
      card.append(element('div',`ID：${t.id} · 下次：${t.next_run_text}（北京时间）`,'timing-meta'));
      if(t.kind==='ai')card.append(element('div',`执行身份 QQ ${t.requester} · ${t.owner_grant?'主人授权':'普通权限'} · ${t.delivery==='silent'?'静默执行':t.delivery==='message'?'发送结果':'按提示词决定发送'}`,'timing-meta'));
      card.append(element('pre',t.command));
      if(t.last_run) {
        const last=t.last_run;
        card.append(element('div',`最近执行：${last.status} · ${new Date(last.started*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}`,'timing-meta'));
        const detail=element('details'); detail.append(element('summary','执行结果')); detail.append(element('pre',last.result||'等待执行回执')); card.append(detail);
      }
      const actions=element('div',undefined,'timing-actions');
      for(const [label,action] of [['编辑',()=>edit(t)],[t.enabled?'暂停':'启用',async()=>{await request('/api/timing/tasks/'+t.id,json('PUT',{revision:t.revision,enabled:!t.enabled}));await load();}],['删除',async()=>{if(!confirm('删除此定时任务？正在运行的任务也会停止，已写入文件不会回滚。'))return;await request('/api/timing/tasks/'+t.id+'?revision='+t.revision,{method:'DELETE'});if(editing?.id===t.id){form.hidden=true;editing=null;}await load();}]]) {
        const button=element('button',label);button.type='button';button.className='secondary';button.onclick=async()=>{button.disabled=true;try{await action();}catch(e){q('.timing-status').textContent=e.message;}finally{button.disabled=false;}};actions.append(button);
      }
      card.append(actions);list.append(card);
    }
  }
  async function load() {const data=await request('/api/timing/tasks');tasks=data.tasks;q('.timing-status').textContent=data.error||(!data.enabled?'定时模块已关闭，任务保留，暂不执行。':'所有时间均为北京时间。');render();}
  q('.timing-new').onclick=()=>edit(null);q('.timing-cancel').onclick=()=>{form.hidden=true;editing=null;};
  q('.timing-refresh').onclick=async()=>{try{await load();}catch(e){q('.timing-status').textContent=e.message;}};
  q('.timing-filter').onchange=render;q('.timing-search').oninput=render;
  form.onsubmit=async event=>{
    event.preventDefault(); const button=q('.timing-save'),status=q('.timing-form-status');button.disabled=true;status.textContent='正在保存…';
    const body={kind:fields.kind.value,scene:fields.scene_type.value+':'+fields.scene_id.value,flag:fields.flag.value,time:fields.time.value,
      enabled:fields.enabled.checked,delivery:fields.delivery.value,requester:fields.requester.value||'0',owner_grant:fields.owner_grant.checked,bot_qq:fields.bot_qq.value};
    body[body.kind==='ai'?'prompt':'content']=fields.content.value;
    if(editing)body.revision=editing.revision;
    try{await request('/api/timing/tasks'+(editing?'/'+editing.id:''),json(editing?'PUT':'POST',body));form.hidden=true;editing=null;await load();}
    catch(e){status.textContent=e.message;}finally{button.disabled=false;}
  };
  await load();
};
