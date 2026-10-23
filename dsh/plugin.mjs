import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {defineTool} from '@deepseek-ai/dsh-tools';
import {denial, within} from './policy.mjs';
import {bindShellTasks} from './shell-policy.mjs';
import {mountBrowser} from './browser.mjs';
import {activationError, observeActivationErrors} from './activation-errors.mjs';

export const name = 'myqqbot-bridge';
export const inject = ['agents', 'sessions', 'agentLoop', 'tools', 'systemPrompt', 'sessionPersistence', 'fs', 'compaction', 'subagents', 'workingDirectory', 'shellEnv', 'browserUse'];

const text = value => [{type: 'text', text: value}];
export function stickerShellEnv(task, callback) {
  if (!task?.owner || !['worker', 'scheduled'].includes(task.role) || task.cancelled ||
      !Number.isFinite(task.expires_at) || task.expires_at <= Date.now() || !task.sticker_capability) return {};
  return {DSH_QQBOT_STICKER_TOKEN: task.sticker_capability, DSH_QQBOT_CALLBACK: callback};
}
export function validateScheduledTools(task, names) {
  if (task.role !== 'scheduled') return;
  const missing = ['send_message', 'send_forward_message', 'send_file', 'send_sticker', 'send_voice', 'synthesize_voice'].filter(name => !names.includes(name));
  if (missing.length) throw new Error('SEND_TOOLS_MISSING: 定时分支缺少发送工具：' + missing.join(', '));
}
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
  read_browser_image: ['调用已有视觉服务理解当前会话files目录中的浏览器截图，path使用截图实际保存位置。主模型不能直接看MCP图片时用本工具；不发送QQ消息，不改变网页或赋予权限', {path:{type:'string',required:true}}],
  synthesize_voice: ['后台合成语音，不向QQ发送。正文text为1至1200字符的口语文本；instruction可选，最多500字符，单独描述语气/语速/情绪。使用宿主固定音色，不能选账号或收件人。本轮最多2次，返回当前会话绑定、24小时有效的voice_id；普通执行者将ID和正文返回主对话，主对话再send_voice。失败如实报告', {text:{type:'string',required:true},instruction:{type:'string'}}],
  send_voice: ['向current_scene显式发送语音条，本轮最多2次。voice_id用synthesize_voice的真实回执，或path指定当前会话files目录内已有音频文件；二选一。未合成时主对话先spawn执行者合成，收到voice_id再发送。检查QQ发送回执，合成成功不等于已发送；unknown不重试。普通Spawn禁止本工具，定时分支可用，silent禁止发送', {voice_id:{type:'string'},path:{type:'string'}}],
  find_sticker: ['按情绪、用途或名称从当前机器人账号的本地表情库查找少量候选；不调用视觉模型，不等后台解析。主对话可直接查询；目录未就绪时不要猜ID', {query: {type:'string'}, limit:{type:'integer'}}],
  send_sticker: ['按find_sticker返回的sticker_id向当前QQ会话发送一张表情，本轮最多1张、会话冷却10秒；静默任务禁止发送，结果unknown不重发。增删改委派主人执行者读sticker-management SKILL', {sticker_id:{type:'string',required:true}}],
  send_message: ['向current_scene发文字，1至4000字符，本轮最多4次；检查回执', {message: {type: 'string', required: true}}],
  send_forward_message: ['向当前会话发机器人身份的聊天记录卡片；messages顺序文本1至20条，各1至4000字符、总计最多20000，本轮最多1次。长文或用户要求时使用，成功后不重复正文，不冒充他人', {messages: {type: 'array', items: {type: 'string'}, required: true}}],
  inspect_message: ['读取当前会话中已知或引用消息的真实发送者', {message_id: {type: 'string', required: true}}],
  recall_message: ['撤回当前会话真实消息。本人申请撤回自己的消息直接批准；第三方申请结合误发、隐私、骚扰等上下文判断，不限申请者角色。对象不明先inspect_message；reason写判断依据。permission_denied是机器人QQ权限不足', {message_id: {type: 'string', required: true}, reason: {type: 'string', required: true}}],
  get_bot_help: ['按需获取最新admin帮助全文、模块用法与开关；主人帮助仅返回宿主确认的主人任务。了解模块不代表可调用其业务队列', {}],
  list_attachments: ['列出当前QQ会话收到的附件', {}],
  read_media: ['读取当前会话图片/PDF/语音音频/合并转发的后台解析结果，按message_id或attachment_id查询；可wait_seconds=0至30等待，未完成则返回状态；长结果用next_offset继续读取；retry=true仅明确重试失败解析。主对话须委派执行者使用，未完成前不能声称已看过或听过', {message_id:{type:'string'},attachment_id:{type:'string'},wait_seconds:{type:'integer'},retry:{type:'boolean'},offset:{type:'integer'}}],
  download_attachment: ['先发简短确认后下载附件到当前会话files目录', {attachment_id: {type: 'string', required: true}, path: {type: 'string'}}],
  send_file: ['将files_directory内已创建、下载或修改的文件发送为当前群的群文件或当前私聊文件；path为文件路径，name为可选显示文件名；单文件最多20MiB，等待NapCat回执。需文件开关开启且当前为明确请求', {path: {type: 'string', required: true}, name: {type: 'string'}}],
  search_memory: ['本地向量与文本检索；scope为auto/user/group/common，auto搜索全部正式记忆。target_qq仅筛选用户记忆，target_group仅筛选群记忆；只查指定群时用scope=group和target_group。也可原生grep/read', {query: {type: 'string', required: true}, target_qq: {type: 'string'}, target_group: {type: 'string'}, scope: {type: 'string'}}],
  delete_memory_file: ['仅供记忆维护分支删除暂存副本中的合法记忆文件，path相对cwd/memory，例如user/123/memory/过时主题.md；先读取并确认有效信息已合并。正式记忆在整个维护成功后才提交删除，不能删除原文或任意服务器文件', {path:{type:'string',required:true}}],
  spawn_task: ['主对话的执行入口：联网/查最新/读链接、记忆读写、文件/附件处理与发送、撤回、定时/表情管理、复杂分析或长文均委派。普通用户也可委派联网及权限内工作；主对话缺少执行工具不代表机器人不能做。prompt写目标、必要事实与交付要求；立即返回内部task_id，结果自动回主对话，文字由主对话交付、文件由执行者send_file。不自动发受理回执，不继承后续请求、不提升权限；检查启动回执', {title: {type: 'string', required: true}, prompt: {type: 'string', required: true}, source_message_id:{type:'string'}}],
  task_status: ['查询当前请求者在当前QQ会话的后台任务状态，省略task_id列出最近任务', {task_id: {type: 'string'}, source_message_id:{type:'string'}}],
  cancel_task: ['取消当前请求者在当前QQ会话的指定后台任务；文件修改不回滚', {task_id: {type: 'string', required: true}, source_message_id:{type:'string'}}],
  schedule_create: ['建立当前会话任务：固定原文kind=fixed/content，动态生成或工作kind=ai/prompt（默认ai）。AI须显式选择delivery：播报、提醒、交付结果用message；允许按条件静默用auto；内部维护用silent。北京时间；-y 月-日-时-分-秒，-m 日-时-分-秒，-w 星期1至7-时-分-秒，-d 时-分-秒，-o 年-月-日-时-分-秒。农历加-cn，个位数无需补零；循环取下一次，一次性过去时间拒绝。content保留空白，群艾特用[CQ:at,qq=QQ号]；fixed不能传delivery。闰月等细则见qqbot-guide', {kind:{type:'string'},flag:{type:'string',required:true},time:{type:'string',required:true},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string',enum:['message','auto','silent']}}],
  schedule_list: ['列出当前QQ会话定时任务、类型、内容、唯一ID和下次时间；省略kind列出固定消息和AI两类，可传fixed或ai筛选。不能查看其他会话', {kind:{type:'string'}}],
  schedule_update: ['修改当前会话任务，只传变化字段；task_id来自list，保持原类型。fixed用content、ai用prompt，时间格式同create。AI的delivery：明确要求播报/交付用message，允许按条件静默用auto，内部维护用silent；仅改时间等字段时保留原模式。enabled暂停/启用；授权按当前请求者重算，不能跨会话', {task_id:{type:'string',required:true},flag:{type:'string'},time:{type:'string'},content:{type:'string'},prompt:{type:'string'},delivery:{type:'string',enum:['message','auto','silent']},enabled:{type:'boolean'}}],
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
  // Native activation.result preserves only stopReason. Observe the scoped
  // failure while the child still exists, before its disposable log is removed.
  observeActivationErrors(ctx, agent => currentTask(agent) || pendingChildren.get(agent?.id));
  function branchFailure(task, phase, error) {
    const message = activationError(error);
    process.stderr.write('QQBOT_ACTIVATION_ERROR ' + JSON.stringify({task_id: task.task_id,
      agent_id: task.agent_id, role: task.role, phase, error: message}) + '\n');
    return message;
  }
  const browser = mountBrowser(ctx, currentTask, workspace);
  bindShellTasks(sessionId => [...tasks.values()].find(task => task.session_id === sessionId));
  // Ambient credentials are intentionally scrubbed by dsh. Supply only this
  // execution's narrow, host-issued capability, never the global bridge key.
  ctx.shellEnv.register({name: 'qqbot-sticker-cli', variables: {
    DSH_QQBOT_STICKER_TOKEN: {description: '仅本执行任务的表情管理凭据；禁止打印或转述'},
    DSH_QQBOT_CALLBACK: {description: 'QQbot 表情管理脚本的本机回环地址'},
  }, resolve(exec) {
    return stickerShellEnv(currentTask(exec.agent), process.env.QQBOT_CALLBACK);
  }});
  // Inspect the effective agent scope, not the global registry or the parent's
  // persisted request/header. Run at every model step, including after tools
  // and compaction; only log the first surface or a subsequent change.
  ctx.on('agent/pre-step', ({agent}, next) => {
    const task = currentTask(agent);
    if (task) {
      task.step_revision = task.input_revision || 0;
      const names = ctx.tools.schemas(agent).map(tool => tool.name).sort();
      const signature = JSON.stringify(names);
      if (task.tool_surface !== signature) {
        task.tool_surface = signature;
        process.stderr.write('QQBOT_TOOL_SURFACE ' + JSON.stringify({task_id: task.task_id,
          agent_id: agent.id, role: task.role, kind: task.kind,
          background_delivery: !!task.delivery, tools: names}) + '\n');
      }
      try { validateScheduledTools(task, names); }
      catch (error) {
        task.tool_surface_error = error.message;
        throw error;
      }
    }
    return next();
  });
  ctx.tools.guard(exec => denial(currentTask(exec.agent), exec.name, exec.arguments, workspace));
  async function callback(task, toolName, args, signal) {
    const response = await fetch(process.env.QQBOT_CALLBACK, {method: 'POST', headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({task_id: task.task_id, name: toolName, arguments: args, input_revision:task.step_revision || 0}), signal});
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
    task.session_id = agent.session.header.id;
    task.agent_id = agent.id;
    instructions(agent.ctx, agent, task);
    // rc.2 removed request.toolFilter. Bind the child scope before its first
    // model step, independently of the changing shared parent's authority.
    agent.ctx.tools.restrict(task.role === 'memory' ? {allow: ['read', 'write', 'edit', 'glob', 'grep', 'skill', 'search_memory', 'delete_memory_file']} : {deny: task.role === 'worker'
      ? ['spawn_task', 'task_status', 'cancel_task', 'delete_memory_file', 'send_message', 'send_forward_message', 'send_sticker', 'send_voice']
      : ['spawn_task', 'task_status', 'cancel_task', 'delete_memory_file']});
    if (['scheduled', 'memory'].includes(task.role)) {
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
      browser.release(job.task.agent_id);
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
    if (parent.live_inputs) {
      const grant = await callback(parent, '__source', {source_message_id:args.source_message_id}, exec.signal);
      if (grant.code === 'new_observations_pending') return grant;
      if (typeof grant.qq !== 'string' || typeof grant.owner !== 'boolean') throw new Error('SOURCE_AUTHORITY_INVALID');
      parent = {...parent, ...grant};
    }
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
      const prepared = await callback(parent, '__spawn', {task_id: id, title: args.title,
        ...(args.source_message_id ? {source_message_id:args.source_message_id} : {})}, exec.signal);
      task.sticker_capability = prepared.sticker_capability || '';
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
        if (task?.role === 'main' && ['send_message','send_forward_message','send_sticker','send_voice','spawn_task'].includes(toolName)) {
          if (exec.agent.inbox.nextStep.length) return {success:false,code:'new_observations_pending',error:'先阅读刚插入的发言，再决定回复或委派。'};
        }
        if (!task || task.cancelled) throw new Error('TASK_EXPIRED');
        if (['spawn_task', 'task_status', 'cancel_task'].includes(toolName)) return dispatch(toolName, args, exec, task);
        const result = await callback(task, toolName, args, exec.signal);
        if (task.role === 'main' && ['send_message','send_forward_message','send_sticker','send_voice'].includes(toolName) && result.code !== 'new_observations_pending') task.reply_started = true;
        return result;
      }}));
  }
  let rootCreation = Promise.resolve();
  function getHandle(kind, data) {
    const start = rootCreation.then(() => createHandle(kind, data));
    rootCreation = start.then(() => {}, () => {});
    return start;
  }
  const sameDirectory = (left, right) => typeof left === 'string' && path.relative(path.resolve(left), path.resolve(right)) === '';
  async function retireHandle(kind) {
    const old = handles.get(kind);
    if (!old) return;
    const active = currentTask(old.agent);
    if (active) active.cancelled = true;
    const children = [...jobs.values(), ...scheduled.values()].filter(job => job.parent_id === old.agent.id && !job.finished);
    for (const job of children) cancel(job);
    // Revoke Python-side QQ callbacks and memory transactions as well as native
    // tool execution before a parent is replaced. Completion cannot reauthorize.
    await Promise.allSettled(children.map(job => callback(job.task, '__revoke', {})));
    await Promise.allSettled(children.map(job => job.done));
    await ctx.subagents.drainDescendants([old.agent]);
    await old.dispose();
    browser.release(old.agent.id);
    handles.delete(kind);
  }
  async function createHandle(kind, data) {
    let generation = generations[kind];
    if (generation && typeof generation.cwd !== 'string') {
      // Old manifests predate the cwd field. Observe the immutable native
      // header instead of mistaking missing metadata for a deployment move.
      const snapshot = await ctx.sessionPersistence.stat(generation.id);
      generation.cwd = snapshot?.header.cwd;
    }
    // A genuine move or expiration retires the whole live scope before its
    // manifest is replaced. Keep physical logs until their retention deadline.
    if (!generation || !sameDirectory(generation.cwd, data.cwd) || generation.expires <= Date.now()) {
      await retireHandle(kind);
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
    }
    writeJSON(manifest, generations);
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
      if (!sameDirectory(await ctx.workingDirectory.ensure(agent), data.cwd)) throw new Error('对话工作目录不能改变');
      const generation = generations[data.kind];
      generation.expires = Math.min(generation.expires, data.expires_at);
      retained[generation.id] = generation.expires;
      writeJSON(retentionFile, retained);
      writeJSON(manifest, generations);
      const task = {...data, expires_at: generation.expires, cancelled: false};
      tasks.set(agent.id, task);
      task.session_id = agent.session.header.id;
      lift = agent.ctx.tools.restrict(data.role === 'main'
        ? {allow: data.delivery ? ['send_message', 'send_forward_message', 'find_sticker', 'send_sticker', 'send_voice'] : ['send_message', 'send_forward_message', 'find_sticker', 'send_sticker', 'send_voice', 'spawn_task', 'task_status', 'cancel_task']}
        : {deny: ['spawn_task', 'task_status', 'cancel_task', 'delete_memory_file']});
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
  async function runFork(data) {
    const maintenance = data.role === 'memory' && data.kind === 'memory';
    if (closing || (!maintenance && (data.role !== 'scheduled' || !data.prompt.startsWith('[定时任务]')))) throw new Error('临时分支请求无效');
    if (maintenance && !sameDirectory(data.cwd, path.join(workspace, 'tmp/memory-stage'))) throw new Error('记忆维护必须使用暂存目录');
    const task = {...data, cancelled:false};
    const job = {task, controller:new AbortController(), run:null};
    scheduled.set(data.task_id, job);
    try {
      const start = creation.then(async () => {
        if (closing || job.controller.signal.aborted) throw new Error('TASK_EXPIRED');
        // Fork the real main chat, never rotate it to the staging directory.
        const parent = await getHandle('chat', {...data, cwd:workspace, options:{provider:'deepseek-official',model:'deepseek-flash'}});
        job.parent_id = parent.agent.id;
        task.expires_at = Math.min(task.expires_at, generations.chat.expires);
        return activate('fork', parent.agent, task, maintenance ? '记忆维护' : '定时任务', data.prompt, job.controller);
      });
      creation = start.then(() => {}, () => {});
      job.run = await start;
      job.timer = setTimeout(() => cancel(job), Math.max(1, task.expires_at-Date.now()));
      const result = await job.run.result;
      // The native activation reports a stop reason without the original
      // exception. Preserve this diagnostic for the scheduler/admin run log.
      if (task.tool_surface_error) throw new Error(task.tool_surface_error);
      const failure = result.stopReason === 'error' ? branchFailure(task, 'execution', task.activation_error || task.tool_surface_error) : undefined;
      return {cancelled:task.cancelled,finish_reason:result.stopReason,
        ...(failure ? {error:failure} : {}),
        final_response:(result.output||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n')};
    } catch (error) {
      throw new Error(branchFailure(task, job.run ? 'execution' : 'admission', error));
    } finally {
      clearTimeout(job.timer);
      try {
        if (job.run) {
          await job.run.dispose();
        }
      } finally {
        browser.release(task.agent_id);
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
      else if (request.url === '/observe') {
        const handle = handles.get('chat');
        const task = handle && currentTask(handle.agent);
        if (!task || task.task_id !== data.task_id || task.role !== 'main' || task.delivery || task.cancelled ||
            task.reply_started || task.scene_dir !== data.scene_dir || handle.agent.status === 'idle') result = {accepted:false};
        else {
          handle.agent.inject({id:randomUUID(),role:'user',source:{kind:'user'},content:text(data.prompt)});
          task.live_inputs = true;
          task.input_revision = (task.input_revision || 0) + 1;
          result = {accepted:true, input_revision:task.input_revision};
        }
      }
      else if (request.url === '/scheduled' || request.url === '/maintenance') {
        if ((request.url === '/maintenance') !== (data.kind === 'memory' && data.role === 'memory')) throw new Error('分支角色无效');
        const execution = runFork(data);
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
