import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {defineTool} from '@deepseek-ai/dsh-tools';
import {denial, within} from './policy.mjs';

export const name = 'myqqbot-bridge';
export const inject = ['agents', 'sessions', 'agentLoop', 'tools', 'systemPrompt', 'sessionPersistence', 'fs', 'compaction', 'subagents', 'workingDirectory'];

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
  send_message: ['向current_scene发文字，1至4000字符，本轮最多4次；检查回执', {message: {type: 'string', required: true}}],
  send_forward_message: ['向当前会话发机器人身份的聊天记录卡片；messages顺序文本1至20条，各1至4000字符、总计最多20000，本轮最多1次。长文或用户要求时使用，成功后不重复正文，不冒充他人', {messages: {type: 'array', items: {type: 'string'}, required: true}}],
  inspect_message: ['读取当前会话中已知或引用消息的真实发送者', {message_id: {type: 'string', required: true}}],
  recall_message: ['撤回当前会话真实消息。本人申请撤回自己的消息直接批准；第三方申请结合误发、隐私、骚扰等上下文判断，不限申请者角色。对象不明先inspect_message；reason写判断依据。permission_denied是机器人QQ权限不足', {message_id: {type: 'string', required: true}, reason: {type: 'string', required: true}}],
  get_bot_help: ['按需获取最新admin帮助全文、模块用法与开关；主人帮助仅返回宿主确认的主人任务。了解模块不代表可调用其业务队列', {}],
  list_attachments: ['列出当前QQ会话收到的附件', {}],
  download_attachment: ['先发简短确认后下载附件到当前会话files目录', {attachment_id: {type: 'string', required: true}, path: {type: 'string'}}],
  send_file: ['将files_directory内已创建、下载或修改的文件发送为当前群的群文件或当前私聊文件；path为文件路径，name为可选显示文件名；单文件最多20MiB，等待NapCat回执。需文件开关开启且当前为明确请求', {path: {type: 'string', required: true}, name: {type: 'string'}}],
  search_memory: ['本地向量与文本检索；scope为auto/user/group/common，auto搜索全部正式记忆。target_qq仅筛选用户记忆，target_group仅筛选群记忆；只查指定群时用scope=group和target_group。也可原生grep/read', {query: {type: 'string', required: true}, target_qq: {type: 'string'}, target_group: {type: 'string'}, scope: {type: 'string'}}],
  spawn_task: ['启动一次性后台执行者，立即返回内部task_id；prompt写目标、必要上下文和交付要求。结果自动回主对话，文字由主对话交付、文件由执行者send_file；不自动发受理回执，不继承后续请求，不提升权限', {title: {type: 'string', required: true}, prompt: {type: 'string', required: true}}],
  task_status: ['查询当前请求者在当前QQ会话的后台任务状态，省略task_id列出最近任务', {task_id: {type: 'string'}}],
  cancel_task: ['取消当前请求者在当前QQ会话的指定后台任务；文件修改不回滚', {task_id: {type: 'string', required: true}}],
  schedule_create: ['建立当前会话任务：固定原文kind=fixed/content，动态生成或工作kind=ai/prompt（默认ai）。北京时间；-y 月-日-时-分-秒，-m 日-时-分-秒，-w 星期1至7-时-分-秒，-d 时-分-秒，-o 年-月-日-时-分-秒。农历加-cn，个位数无需补零；循环取下一次，一次性过去时间拒绝。content保留空白，群艾特用[CQ:at,qq=QQ号]；delivery仅AI可用auto/message/silent。闰月等细则见qqbot-guide', {kind:{type:'string'},flag:{type:'string',required:true},time:{type:'string',required:true},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string'}}],
  schedule_list: ['列出当前QQ会话定时任务、类型、内容、唯一ID和下次时间；省略kind列出固定消息和AI两类，可传fixed或ai筛选。不能查看其他会话', {kind:{type:'string'}}],
  schedule_update: ['修改当前会话任务，只传变化字段；task_id来自list，保持原类型。fixed用content、ai用prompt，时间格式同create，delivery仅AI可用。enabled暂停/启用；授权按当前请求者重算，不能跨会话', {task_id:{type:'string',required:true},flag:{type:'string'},time:{type:'string'},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string'},enabled:{type:'boolean'}}],
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
      const skillPath = path.join(workspace, '.agents/skills/memory-management/SKILL.md');
      return (task?.system || '') + '\n【记忆SKILL提示】\n记忆操作前执行者用skill或read读取：' + skillPath + '；普通聊天无需加载。';
    }});
  }
  // A fresh Spawn scope must receive frozen authority before its first step.
  ctx.on('agent/created', ({agent}) => {
    const task = pendingChildren.get(agent.id);
    if (!task || agent.session.header.origin !== 'subagent') return;
    tasks.set(agent.id, task);
    task.agent_id = agent.id;
    instructions(agent.ctx, agent, task);
    // rc.2 removed request.toolFilter. Bind the child scope before its first
    // model step, independently of the changing shared parent's authority.
    agent.ctx.tools.restrict({deny: task.role === 'worker'
      ? ['spawn_task', 'task_status', 'cancel_task', 'send_message', 'send_forward_message']
      : ['spawn_task', 'task_status', 'cancel_task']});
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
      // Activation.result settles after native flush and child teardown.
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
  function cancel(job) {
    job.task.cancelled = true;
    job.controller.abort(); // Cancels admission before startActivation publishes.
    // After publication the admission signal no longer owns execution.
    // Issue disposal synchronously; callers join job.done where required.
    job.run?.dispose().catch(error => process.stderr.write('QQBOT_CANCEL ' + error.message + '\n'));
  }
  async function activate(provider, parent, task, label, prompt, controller) {
    const childId = randomUUID();
    pendingChildren.set(childId, task);
    try {
      const activation = await ctx.subagents.startActivation({provider, childId, label,
        delivery: 'caller', signal: controller.signal,
        request: {parent, cwd: task.cwd, prompt: text(prompt), maxDepth: 1}});
      // Cancellation may race with publication; never leave an accepted child
      // running merely because its admission signal was already aborted.
      if (controller.signal.aborted) await activation.dispose();
      return activation;
    } finally { pendingChildren.delete(childId); }
  }
  async function dispatch(name, args, exec, parent) {
    for (const [id, job] of jobs) if (job.finished && Date.now() - job.finished > 3600000) jobs.delete(id);
    if (name !== 'spawn_task') {
      if (args.task_id && !owned(jobs.get(args.task_id), parent)) return {success: false, code: 'task_not_found'};
      const selected = [...jobs.values()].filter(job => owned(job, parent) && (!args.task_id || args.task_id === job.task.task_id));
      if (name === 'cancel_task') {
        for (const job of selected) if (!job.finished) {
          try { await callback(job.task, '__revoke', {}); }
          finally { cancel(job); }
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
        return activate('spawn', exec.agent, task, args.title,
          JSON.stringify({assignment: args.prompt, origin: prepared.context}), job.controller);
      });
      creation = start.then(() => {}, () => {});
      job.run = await start;
      job.state = 'running';
      job.timer = setTimeout(() => cancel(job), Math.max(1, task.expires_at - Date.now()));
      job.done = job.run.result.then(result => settle(job, result), error => settle(job, {stopReason: 'error', output: text(error.message)}));
      return {success: true, task_id: id, state: 'running', mode: 'spawn-one-shot', delivery: '完成后作为资料回主对话，按上下文显式调用发送工具交付；允许静默，无自动受理回执'};
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
    // 工作目录变了（例如从容器部署迁到原生部署）旧物理会话就不能复用：
    // dsh 不允许改变会话的 cwd，这里直接轮换成新会话，Python 会从档案重新播种上下文。
    if (generation && generation.cwd !== data.cwd) {
      generation = null;
      delete generations[kind];
      writeJSON(manifest, generations);
    }
    // Roll physical logs weekly without importing their raw history. Logical
    // conversations persist via memory + Python's retained recent context.
    if (generation && generation.expires <= Date.now()) {
      // Do not dispose a parent underneath live children. Retention guard and
      // child deadlines cancel expired work before the next rotation.
      const children = [...jobs.values()].filter(job => job.parent_id === handles.get(kind)?.agent.id && !job.finished);
      for (const job of children) cancel(job);
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
      generation = {id: kind + '-' + randomUUID(), created: Date.now(), expires: Math.min(data.expires_at, Date.now() + 7 * 86400000), cwd: data.cwd};
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
      // Native filesystem/shell/Spawn now consult the effective directory
      // service, rather than the immutable original session header alone.
      if (path.resolve(await ctx.workingDirectory.ensure(agent)) !== path.resolve(data.cwd)) throw new Error('对话工作目录不能改变');
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
        return activate('fork', parent.agent, task, '定时任务', data.prompt, job.controller);
      });
      creation = start.then(() => {}, () => {});
      job.run = await start;
      job.timer = setTimeout(() => cancel(job), Math.max(1, task.expires_at-Date.now()));
      const result = await job.run.result;
      return {cancelled:task.cancelled,finish_reason:result.stopReason,
        final_response:(result.output||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n')};
    } finally {
      clearTimeout(job.timer);
      try {
        if (job.run) {
          await job.run.dispose();
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
        if (job) cancel(job);
        const branch = scheduled.get(data.task_id);
        if (branch) {cancel(branch);await branch.done;}
        for (const handle of handles.values()) {
          const task = currentTask(handle.agent);
          if (task?.task_id === data.task_id) { task.cancelled = true; handle.agent.cancel({kind: 'user'}); await handle.agent.whenIdle(); }
        }
        result = {cancelled: true};
      } else if (request.url === '/close') {
        closing = true;
        for (const job of jobs.values()) cancel(job);
        for (const job of scheduled.values()) cancel(job);
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
    for (const job of jobs.values()) cancel(job);
    for (const job of scheduled.values()) cancel(job);
    await creation;
    await Promise.allSettled([...jobs.values()].map(job => job.done));
    await Promise.allSettled([...scheduled.values()].map(job => job.done));
    for (const handle of handles.values()) await handle.dispose();
    await new Promise(resolve => server.close(resolve));
  });
}
