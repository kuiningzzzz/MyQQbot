import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {defineTool} from '@deepseek-ai/dsh-tools';
import {denial, within} from './policy.mjs';

export const name = 'myqqbot-bridge';
export const inject = ['agents', 'sessions', 'agentLoop', 'tools', 'systemPrompt', 'sessionPersistence', 'fs', 'compaction', 'subagents'];

const text = value => [{type: 'text', text: value}];
function writeJSON(file, value) {
  const temporary = file + '.' + randomUUID() + '.tmp';
  try {
    fs.writeFileSync(temporary, JSON.stringify(value), {flag: 'wx'});
    fs.renameSync(temporary, file);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}
const definitions = {
  send_message: ['发送一条消息到当前任务绑定的QQ会话，等待NapCat回执', {message: {type: 'string', required: true}}],
  send_forward_message: ['发送QQ聊天记录卡片到当前会话', {messages: {type: 'array', items: {type: 'string'}, required: true}}],
  inspect_message: ['读取当前会话中已知或引用消息的真实发送者', {message_id: {type: 'string', required: true}}],
  recall_message: ['撤回消息；本人申请直接批准，第三方申请根据上下文判断', {message_id: {type: 'string', required: true}, reason: {type: 'string', required: true}}],
  list_attachments: ['列出当前QQ会话收到的附件', {}],
  download_attachment: ['先发简短确认后下载附件到当前会话files目录', {attachment_id: {type: 'string', required: true}, path: {type: 'string'}}],
  send_file: ['将files_directory内已创建、下载或修改的文件发送为当前群的群文件或当前私聊文件；path为文件路径，name为可选显示文件名；单文件最多20MiB，等待NapCat回执。需文件开关开启且当前为明确请求', {path: {type: 'string', required: true}, name: {type: 'string'}}],
  search_memory: ['本地向量与文本检索，无额外Agent；也可直接原生grep/read记忆文件', {query: {type: 'string', required: true}, target_qq: {type: 'string'}, scope: {type: 'string'}}],
  spawn_task: ['立即启动一次性后台Spawn子Agent并返回task_id，不等待执行完成。所有读写、搜索、记忆编辑、附件处理、撤回等工作均须委派。说明目标、必要上下文与交付要求，不能提升授权。结果自动回原会话，无需轮询。', {title: {type: 'string', required: true}, prompt: {type: 'string', required: true}}],
  task_status: ['查询当前请求者在当前QQ会话的后台任务状态，省略task_id列出最近任务', {task_id: {type: 'string'}}],
  cancel_task: ['取消当前请求者在当前QQ会话的指定后台任务；文件修改不回滚', {task_id: {type: 'string', required: true}}],
  schedule_create: ['建立当前QQ会话定时任务。确定的文本用kind=fixed和content（/timing-task，到时直接发送原文）；需要执行时动态获取、AI生成或处理工作用kind=ai和prompt（/timing-task-AI）。kind省略默认ai。固定消息content保留空格换行，群聊艾特用[CQ:at,qq=具体QQ号]，私聊显示普通@QQ号；delivery仅AI可用（auto/message/silent）。不能指定目标会话或授权。北京时间；-y MM-DD-HH-MM-SS，-m DD-HH-MM-SS，-w 01至07-HH-MM-SS（周一至周日），-d HH-MM-SS，-o YY-MM-DD-HH-MM-SS（支持四位年份）。循环安排下一次，一次性过去时间拒绝。', {kind:{type:'string'},flag:{type:'string',required:true},time:{type:'string',required:true},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string'}}],
  schedule_list: ['列出当前QQ会话定时任务、类型、内容、唯一ID和下次时间；省略kind列出固定消息和AI两类，可传fixed或ai筛选。不能查看其他会话', {kind:{type:'string'}}],
  schedule_update: ['修改当前QQ会话固定消息或AI定时任务，task_id取自查询结果；保持原类型，只填写需要修改的字段。固定消息正文用content，群聊艾特用[CQ:at,qq=具体QQ号]；AI提示词用prompt，delivery仅AI可用（auto/message/silent）。enabled可暂停/启用；AI授权按当前真实请求者重新计算。不能修改其他会话', {task_id:{type:'string',required:true},flag:{type:'string'},time:{type:'string'},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string'},enabled:{type:'boolean'}}],
  schedule_delete: ['按task_id删除当前QQ会话的固定消息或AI定时任务，不能操作其他群或私聊', {task_id:{type:'string',required:true}}],
  integrate_memory: ['整理新增记忆队列，复用宿主100条批次的暂存、校验与冲突保护。无需逐个编辑他人画像；batches可选1至20，返回数量，不泄露其他会话内容', {batches:{type:'integer'}}],
};

export async function apply(ctx) {
  const workspace = process.env.QQBOT_WORKSPACE;
  const token = process.env.QQBOT_DSH_TOKEN;
  const home = process.env.DSH_HOME;
  const handles = new Map();
  const tasks = new Map();
  const jobs = new Map();
  const scheduled = new Map();
  const pendingChildren = new Map();
  let creation = Promise.resolve();
  let closing = false;
  const manifest = path.join(home, 'qqbot-sessions.json');
  const retentionFile = path.join(home, 'qqbot-retention.json');
  const branchFile = path.join(home, 'qqbot-branches.json');
  const branches = fs.existsSync(branchFile) ? JSON.parse(fs.readFileSync(branchFile, 'utf8')) : {};
  const retained = fs.existsSync(retentionFile) ? JSON.parse(fs.readFileSync(retentionFile, 'utf8')) : {};
  const stored = id => fs.existsSync(path.join(home, 'sessions')) && fs.readdirSync(path.join(home, 'sessions')).some(project => fs.existsSync(path.join(home, 'sessions', project, id)));
  let generations = fs.existsSync(manifest) ? JSON.parse(fs.readFileSync(manifest, 'utf8')) : {};
  function removeBranch(id) {
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)) throw new Error('非法分支ID');
    const root = path.join(home, 'sessions');
    if (fs.existsSync(root)) for (const entry of fs.readdirSync(root, {withFileTypes:true})) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      const target = path.join(root, entry.name, id);
      if (fs.existsSync(target)) {
        if (fs.lstatSync(target).isSymbolicLink() || !within(root, target)) throw new Error('分支日志路径越界');
        fs.rmSync(target, {recursive:true});
      }
    }
    delete retained[id]; delete branches[id];
    writeJSON(retentionFile, retained); writeJSON(branchFile, branches);
  }
  // A crash leaves a cleanup receipt, never an orphaned retained fork.
  for (const id of Object.keys(branches)) removeBranch(id);
  const currentTask = agent => tasks.get(agent?.id);
  ctx.tools.guard(exec => denial(currentTask(exec.agent), exec.name, exec.arguments, workspace));
  async function callback(task, toolName, args, signal) {
    const response = await fetch(process.env.QQBOT_CALLBACK, {method: 'POST', headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({task_id: task.task_id, name: toolName, arguments: args}), signal});
    const result = await response.json();
    if (!result.ok) throw new Error(result.error);
    return result.result;
  }
  function instructions(agentCtx, agent, fixedTask) {
    agentCtx.systemPrompt.section({name: 'qqbot:instructions', order: 0, interpolate: false, text: () => {
      const task = fixedTask || currentTask(agent);
      const skill = fs.readFileSync(path.join(workspace, '.agents/skills/memory-management/SKILL.md'), 'utf8');
      const boundary = task?.role === 'main' ? '主对话只聊天与分派，SKILL中的所有读写/搜索均交给spawn_task子Agent执行。'
        : task?.role === 'worker' ? '你是一次性Spawn执行者；SKILL中的聊天回复由主对话交付，你只返回最终结果，可用send_file交付文件，不能spawn或聊天。'
        : task?.role === 'scheduled' ? '你是独立定时fork执行者，执行本次[定时任务]，权限只来自宿主，不继承历史身份；不能spawn；完成后分支日志删除。'
        : '独立记忆整理对话按批次规则返回JSON，不能聊天或spawn。';
      return (task?.system || '') + '\n【必读记忆规则】\n' + skill + '\n【本Agent角色边界】\n' + boundary;
    }});
  }
  // A fresh Spawn scope must receive frozen authority before its first step.
  ctx.on('agent/created', ({agent}) => {
    const task = pendingChildren.get(agent.session.header.parentSession);
    if (!task || agent.session.header.origin !== 'subagent') return;
    tasks.set(agent.id, task);
    task.agent_id = agent.id;
    instructions(agent.ctx, agent, task);
    if (task.role === 'scheduled') {
      branches[agent.id] = true;
      writeJSON(branchFile, branches);
      return;
    }
    retained[agent.id] = task.expires_at;
    writeJSON(retentionFile, retained);
  });
  const owned = (job, task) => job && job.task.qq === task.qq && job.task.scene_dir === task.scene_dir;
  async function settle(job, result) {
    clearTimeout(job.timer);
    job.state = job.task.cancelled ? 'cancelled' : result.stopReason === 'completed' ? 'completed' : 'failed';
    try {
      if (job.run?.localAgent) await ctx.sessions.flush(job.run.localAgent.session);
      await callback(job.task, '__finish', {task_id: job.task.task_id, title: job.title, state: job.state,
        output: (result.output || []).filter(b => b.type === 'text').map(b => b.text).join('\n').slice(0, 24000)});
    } catch (error) {
      process.stderr.write('QQBOT_BACKGROUND_RESULT ' + error.message + '\n');
    } finally {
      tasks.delete(job.task.agent_id);
      if (job.run) {
        try { await job.run.dispose(); }
        catch (error) { process.stderr.write('QQBOT_BACKGROUND_DISPOSE ' + error.message + '\n'); }
      }
      job.run = null;
      job.finished = Date.now();
      job.task = {task_id: job.task.task_id, qq: job.task.qq, scene_dir: job.task.scene_dir, cancelled: job.task.cancelled};
      const finished = [...jobs.entries()].filter(([, value]) => value.finished);
      for (const [id] of finished.slice(0, Math.max(0, finished.length - 50))) jobs.delete(id);
    }
  }
  async function dispatch(name, args, exec, parent) {
    for (const [id, job] of jobs) if (job.finished && Date.now() - job.finished > 3600000) jobs.delete(id);
    if (name !== 'spawn_task') {
      if (args.task_id && !owned(jobs.get(args.task_id), parent)) return {success: false, code: 'task_not_found'};
      const selected = [...jobs.values()].filter(job => owned(job, parent) && (!args.task_id || args.task_id === job.task.task_id));
      if (name === 'cancel_task') {
        for (const job of selected) if (!job.finished) {
          job.task.cancelled = true;
          try { await callback(job.task, '__revoke', {}); }
          finally { job.controller.abort(); }
        }
      }
      return {success: true, tasks: selected.map(job => ({task_id: job.task.task_id, title: job.title, state: job.task.cancelled ? 'cancelled' : job.state}))};
    }
    if (closing || exec.signal.aborted) throw new Error('TASK_EXPIRED');
    if (!args.title?.trim() || args.title.length > 120 || !args.prompt?.trim() || args.prompt.length > 16000) throw new Error('任务标题或说明长度无效');
    const running = [...jobs.values()].filter(job => !job.finished);
    if (running.length >= 4 || running.filter(job => owned(job, parent)).length >= 2) return {success: false, code: 'task_capacity', error: '同时最多4个后台任务，每位请求者每会话最多2个'};
    const id = randomUUID();
    const task = {...parent, task_id: id, role: 'worker', system: parent.worker_system, cancelled: false};
    const job = {task, parent_id: exec.agent.id, title: args.title, state: 'starting', controller: new AbortController()};
    jobs.set(id, job);
    try {
      const prepared = await callback(parent, '__spawn', {task_id: id, title: args.title}, exec.signal);
      // Only scope creation is serialized. Model/tool execution is independent.
      const start = creation.then(async () => {
        if (closing || job.controller.signal.aborted) throw new Error('TASK_EXPIRED');
        pendingChildren.set(exec.agent.id, task);
        try {
          return await ctx.subagents.start('spawn', {parent: exec.agent, label: args.title,
            prompt: text(JSON.stringify({assignment: args.prompt, origin: prepared.context})),
            signal: job.controller.signal, maxDepth: 1,
            toolFilter: {deny: ['spawn_task', 'task_status', 'cancel_task', 'send_message', 'send_forward_message']}});
        } finally { pendingChildren.delete(exec.agent.id); }
      });
      creation = start.then(() => {}, () => {});
      job.run = await start;
      job.state = 'running';
      job.timer = setTimeout(() => { task.cancelled = true; job.controller.abort(); }, Math.max(1, task.expires_at - Date.now()));
      job.done = job.run.result.then(result => settle(job, result), error => settle(job, {stopReason: 'error', output: text(error.message)}));
      return {success: true, task_id: id, state: 'running', mode: 'spawn-one-shot', delivery: '完成后自动回原QQ会话报告'};
    } catch (error) {
      await settle(job, {stopReason: 'error', output: text(error.message)});
      throw error;
    }
  }
  for (const [toolName, [description, parameters]] of Object.entries(definitions)) {
    ctx.tools.register(defineTool({name: toolName, description, parameters,
      output: {schema: {type: 'json'}, render: (_args, value) => text(JSON.stringify(value))},
      async execute(args, exec) {
        const task = currentTask(exec.agent);
        if (!task || task.cancelled) throw new Error('TASK_EXPIRED');
        if (['spawn_task', 'task_status', 'cancel_task'].includes(toolName)) return dispatch(toolName, args, exec, task);
        return callback(task, toolName, args, exec.signal);
      }}));
  }
  let rootCreation = Promise.resolve();
  function getHandle(kind, data) {
    const start = rootCreation.then(() => createHandle(kind, data));
    rootCreation = start.then(() => {}, () => {});
    return start;
  }
  async function createHandle(kind, data) {
    let generation = generations[kind];
    // Roll physical logs weekly without importing their raw history. Logical
    // conversations persist via memory + Python's retained recent context.
    if (generation && generation.expires <= Date.now()) {
      // Do not dispose a parent underneath live children. Retention guard and
      // child deadlines cancel expired work before the next rotation.
      const children = [...jobs.values()].filter(job => job.parent_id === handles.get(kind)?.agent.id && !job.finished);
      for (const job of children) { job.task.cancelled = true; job.controller.abort(); }
      await Promise.allSettled(children.map(job => job.done));
      const old = handles.get(kind);
      if (old) await old.dispose();
      handles.delete(kind);
      generation = null;
    }
    if (handles.has(kind)) return handles.get(kind);
    const setup = (agentCtx, agent) => instructions(agentCtx, agent);
    let handle;
    if (generation && stored(generation.id)) {
      handle = await ctx.agents.resume({resumeSessionId: generation.id, agentOptions: data.options, setup});
    } else {
      generation = {id: kind + '-' + randomUUID(), created: Date.now(), expires: Math.min(data.expires_at, Date.now() + 7 * 86400000)};
      handle = await ctx.agents.create({sessionId: generation.id, meta: {cwd: data.cwd}, agentOptions: data.options, setup});
      generations[kind] = generation;
      writeJSON(manifest, generations);
    }
    handles.set(kind, handle);
    return handle;
  }
  const busy = new Set();
  async function run(data) {
    if (busy.has(data.kind)) throw new Error('同一对话运行中，请顺序提交任务');
    busy.add(data.kind);
    let handle;
    let lift;
    try {
      if (!['chat', 'memory'].includes(data.kind)) throw new Error('未知对话');
      handle = await getHandle(data.kind, {...data, options: {provider: 'deepseek-official', model: 'deepseek-flash'}});
      const agent = handle.agent;
      // Memory stages change each batch; native tools consult session.cwd.
      if (agent.session.header.cwd !== data.cwd) throw new Error('对话工作目录不能改变');
      const generation = generations[data.kind];
      generation.expires = Math.min(generation.expires, data.expires_at);
      retained[generation.id] = generation.expires;
      writeJSON(retentionFile, retained);
      writeJSON(manifest, generations);
      const task = {...data, expires_at: generation.expires, cancelled: false};
      tasks.set(agent.id, task);
      lift = agent.ctx.tools.restrict(data.role === 'main'
        ? {allow: data.delivery ? ['send_message', 'send_forward_message'] : ['send_message', 'send_forward_message', 'spawn_task', 'task_status', 'cancel_task']}
        : {deny: ['spawn_task', 'task_status', 'cancel_task']});
      const before = agent.session.snapshotEvents().length;
      agent.followup({id: randomUUID(), role: 'user', source: {kind: 'user'}, content: text(data.prompt)});
      await agent.whenIdle();
      await ctx.sessions.flush(agent.session);
      const events = agent.session.snapshotEvents(before);
      const end = events.filter(e => e.type === 'turn/end').at(-1);
      const output = agent.session.deriveMessages().filter(m => m.role === 'assistant').at(-1);
      return {cancelled: task.cancelled, finish_reason: end?.data?.reason?.kind, error: end?.data?.reason?.error, final_response: (output?.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n')};
    } finally {
      lift?.();
      if (handle) tasks.delete(handle.agent.id);
      busy.delete(data.kind);
    }
  }
  async function runScheduled(data) {
    if (closing || data.role !== 'scheduled' || !data.prompt.startsWith('[定时任务]')) throw new Error('定时任务请求无效');
    const task = {...data, role:'scheduled', kind:'chat', cancelled:false};
    const job = {task, controller:new AbortController(), run:null};
    scheduled.set(data.task_id, job);
    try {
      const start = creation.then(async () => {
        if (closing || job.controller.signal.aborted) throw new Error('TASK_EXPIRED');
        const parent = await getHandle('chat', {...data, options:{provider:'deepseek-official',model:'deepseek-flash'}});
        task.expires_at = Math.min(task.expires_at, generations.chat.expires);
        pendingChildren.set(parent.agent.id, task);
        try {
          return await ctx.subagents.start('fork', {parent:parent.agent, label:'定时任务', prompt:text(data.prompt),
            signal:job.controller.signal, maxDepth:1,
            toolFilter:{deny:['spawn_task','task_status','cancel_task']}});
        } finally {pendingChildren.delete(parent.agent.id);}
      });
      creation = start.then(() => {}, () => {});
      job.run = await start;
      job.timer = setTimeout(() => {task.cancelled=true;job.controller.abort();}, Math.max(1, task.expires_at-Date.now()));
      const result = await job.run.result;
      return {cancelled:task.cancelled,finish_reason:result.stopReason,
        final_response:(result.output||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n')};
    } finally {
      clearTimeout(job.timer);
      try {
        if (job.run) {
          try {await ctx.sessions.flush(job.run.localAgent.session);}
          finally {await job.run.dispose();}
        }
      } finally {
        tasks.delete(task.agent_id);
        try {if (task.agent_id) removeBranch(task.agent_id);}
        finally {scheduled.delete(data.task_id);}
      }
    }
  }
  const server = http.createServer(async (request, response) => {
    let result;
    try {
      if (request.headers.authorization !== 'Bearer ' + token || request.method !== 'POST') throw new Error('未授权');
      let body = '';
      for await (const chunk of request) { body += chunk; if (body.length > 2 * 1024 * 1024) throw new Error('请求过大'); }
      const data = JSON.parse(body);
      if (request.url === '/run') result = await run(data);
      else if (request.url === '/scheduled') {
        const execution = runScheduled(data);
        const job = scheduled.get(data.task_id);
        if (job) job.done = execution.catch(() => {});
        result = await execution;
      }
      else if (request.url === '/cancel') {
        const job = jobs.get(data.task_id);
        if (job) { job.task.cancelled = true; job.controller.abort(); }
        const branch = scheduled.get(data.task_id);
        if (branch) {branch.task.cancelled=true;branch.controller.abort();await branch.done;}
        for (const handle of handles.values()) {
          const task = currentTask(handle.agent);
          if (task?.task_id === data.task_id) { task.cancelled = true; handle.agent.cancel({kind: 'user'}); await handle.agent.whenIdle(); }
        }
        result = {cancelled: true};
      } else if (request.url === '/close') {
        closing = true;
        for (const job of jobs.values()) { job.task.cancelled = true; job.controller.abort(); }
        for (const job of scheduled.values()) {job.task.cancelled=true;job.controller.abort();}
        await creation;
        await Promise.allSettled([...jobs.values()].map(job => job.done));
        await Promise.allSettled([...scheduled.values()].map(job => job.done));
        for (const handle of handles.values()) await handle.dispose();
        handles.clear();
        result = {closed: true};
      } else throw new Error('未知请求');
      response.end(JSON.stringify({ok: true, result}));
    } catch (error) { response.end(JSON.stringify({ok: false, error: error.message})); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  process.stdout.write('QQBOT_READY http://127.0.0.1:' + server.address().port + '\n');
  ctx.on('dispose', async () => {
    closing = true;
    for (const job of jobs.values()) { job.task.cancelled = true; job.controller.abort(); }
    for (const job of scheduled.values()) {job.task.cancelled=true;job.controller.abort();}
    await creation;
    await Promise.allSettled([...jobs.values()].map(job => job.done));
    await Promise.allSettled([...scheduled.values()].map(job => job.done));
    for (const handle of handles.values()) await handle.dispose();
    await new Promise(resolve => server.close(resolve));
  });
}
