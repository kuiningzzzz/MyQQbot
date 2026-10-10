featureAdminControls.ai = async function (row) {
  const $ = s => row.querySelector(s);
  const status = $('[data-ai-status]');
  const fields = {};
  const searchFields = {};
  let openedPath = '', fileHash = '';
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    return data;
  }
  function action(selector, work) {
    $(selector).onclick = async () => {
      const button = $(selector); button.disabled = true; status.textContent = '处理中…';
      try { await work(); } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    };
  }
  function field(parent, key, label, value, options, target=fields) {
    const wrapper = document.createElement('label'); wrapper.style.cssText='display:block;margin:10px 0'; wrapper.append(document.createTextNode(label+' '));
    const input = document.createElement(options ? 'select' : 'input');
    if (options) for (const option of options) { const el=document.createElement('option'); el.value=option; el.textContent=option; input.append(el); }
    else input.type = typeof value === 'boolean' ? 'checkbox' : typeof value === 'number' ? 'number' : key === 'api_key' ? 'password' : 'text';
    if (input.type === 'checkbox') input.checked=value; else input.value=value ?? '';
    if (input.type === 'number') input.step='any';
    if (key === 'api_key') { input.autocomplete='new-password'; input.placeholder='留空保留；清除请使用下方复选框'; }
    wrapper.append(input); parent.append(wrapper); target[key]=input;
  }
  const config = await request('/api/ai/config');
  const groups = {
    identity: {bot_qq:'机器人QQ号',nickname:'网名／唤醒姓名',account_description:'账号身份说明'},
    model: {api_url:'视觉与总结API（OpenAI协议）',dsh_api_url:'dsh Messages API地址（留空自动加 /anthropic）',api_key:'API Key',model:'主模型',vision_model:'视觉模型',thinking_mode:'思考模式',reasoning_effort:'思考强度',max_tokens:'最大生成Token',context_token_limit:'总上下文Token上限',context_compress_threshold:'上下文压缩比例',timeout_seconds:'视觉请求超时（秒）',dsh_timeout_seconds:'dsh任务超时（秒）',tool_round_limit:'工具轮数上限',chat_enabled:'启用聊天',summary_enabled:'启用群聊总结'},
    memory: {memory_enabled:'启用记忆',memory_auto_learn:'启用后台整合',raw_retention_days:'原文保留天数（固定30）',memory_batch_size:'自动整理消息数（固定100）',vector_index_enabled:'启用向量索引',embedding_model:'本地嵌入模型名称或路径',timezone:'时区'}
  };
  for (const [group, labels] of Object.entries(groups)) for (const [key,label] of Object.entries(labels)) {
    field($(`[data-ai-${group}]`),key,label,key==='api_key'?'':config[key],key==='thinking_mode'?['inherit','enabled','disabled']:key==='reasoning_effort'?['high','max']:null);
  }
  field($('[data-ai-model]'),'clear_key','清除已保存的API Key',false);
  field($('[data-ai-workspace]'),'workspace_enabled','启用文件工作区与文件发送',config.workspace_enabled);
  field($('[data-ai-workspace]'),'ocr_enabled','启用本地OCR',config.ocr_enabled);
  action('[data-workspace-status]',async()=>{const data=await request('/api/ai/workspace/status');status.textContent=data.success?'dsh原生工作区已就绪。':data.error;});
  $('[data-ai-prompt]').value=config.agent_prompt;
  function values(target) { return Object.fromEntries(Object.entries(target).map(([key,input])=>[key,input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value])); }
  action('[data-ai-save]',async()=>{
    const body=values(fields); body.api_key=body.clear_key?'':body.api_key||null; delete body.clear_key; body.agent_prompt=$('[data-ai-prompt]').value;
    await request('/api/ai/config',body); fields.api_key.value=''; fields.clear_key.checked=false; status.textContent='AI设置已保存。';
  });
  const search=await request('/api/ai/search');
  for (const [key,label] of Object.entries({enabled:'启用搜索',provider:'搜索服务',api_url:'搜索API地址',api_key:'搜索API Key',max_results:'单次结果数',timeout_seconds:'搜索超时（秒）'})) field($('[data-ai-search]'),key,label,key==='api_key'?'':search[key],key==='provider'?['tavily','searxng']:null,searchFields);
  field($('[data-ai-search]'),'clear_key','清除搜索API Key',false,null,searchFields);
  action('[data-search-save]',async()=>{const body=values(searchFields);body.api_key=body.clear_key?'':body.api_key||null;delete body.clear_key;await request('/api/ai/search',body);searchFields.api_key.value='';status.textContent='搜索设置已保存。';});
  async function refresh(){const data=await request('/api/ai/files');const list=$('[data-memory-list]');list.replaceChildren();for(const path of data.files){const option=document.createElement('option');option.value=path;option.textContent=path;list.append(option);}status.textContent='文件列表已更新。';}
  $('[data-memory-list]').onchange=()=>{$('[data-memory-path]').value=$('[data-memory-list]').value;};
  action('[data-memory-refresh]',refresh);
  action('[data-memory-open]',async()=>{const path=$('[data-memory-path]').value||$('[data-memory-list]').value;const data=await request('/api/ai/file?path='+encodeURIComponent(path));openedPath=path;fileHash=data.hash;$('[data-memory-path]').value=path;$('[data-memory-content]').value=data.content;status.textContent='文件已打开。';});
  action('[data-memory-save]',async()=>{if(openedPath!==$('[data-memory-path]').value||!fileHash)throw new Error('请先打开或新建目标文件');const data=await request('/api/ai/file',{path:openedPath,content:$('[data-memory-content]').value,expected_hash:fileHash});fileHash=data.hash;status.textContent=data.indexed?'文件和索引已保存。':'文件已保存；'+(data.index_error||'向量索引已关闭');});
  action('[data-reindex]',async()=>{const data=await request('/api/ai/reindex',{});status.textContent=data.success?'索引重建完成。':data.error;});
  action('[data-memory-test]',async()=>{const data=await request('/memory/query',{query:$('[data-memory-query]').value,target_qq:$('[data-memory-qq]').value||null,scope:'auto'});$('[data-memory-result]').textContent=JSON.stringify(data,null,2);status.textContent='检索完成。';});
  action('[data-integrate]',async()=>{const data=await request('/memory/integrate',{mode:'queue'});$('[data-memory-result]').textContent=JSON.stringify(data,null,2);status.textContent='整合完成。';});
  fields.raw_retention_days.disabled=true; fields.memory_batch_size.disabled=true;
  await refresh();
  status.textContent=`API Key：${config.api_key_configured?'已配置':'未配置'}；已识别机器人QQ：${config.observed_self_ids.join('、')||'尚未连接'}\ndsh ${config.dsh_version}；待整理 ${config.pending_messages} 条\n工作区：${config.workspace_path}\n记忆规则：${config.memory_skill_path}\n${config.index_error||config.integration_error||''}`;
};
