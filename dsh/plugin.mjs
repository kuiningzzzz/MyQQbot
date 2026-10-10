import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {defineTool} from '@deepseek-ai/dsh-tools';
import {denial} from './policy.mjs';

export const name = 'myqqbot-bridge';
export const inject = ['agents', 'sessions', 'agentLoop', 'tools', 'systemPrompt', 'sessionPersistence', 'fs', 'compaction'];

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
};

export async function apply(ctx) {
  const workspace = process.env.QQBOT_WORKSPACE;
  const token = process.env.QQBOT_DSH_TOKEN;
  const home = process.env.DSH_HOME;
  const handles = new Map();
  const tasks = new Map();
  const manifest = path.join(home, 'qqbot-sessions.json');
  const retentionFile = path.join(home, 'qqbot-retention.json');
  const retained = fs.existsSync(retentionFile) ? JSON.parse(fs.readFileSync(retentionFile, 'utf8')) : {};
  const stored = id => fs.existsSync(path.join(home, 'sessions')) && fs.readdirSync(path.join(home, 'sessions')).some(project => fs.existsSync(path.join(home, 'sessions', project, id)));
  let generations = fs.existsSync(manifest) ? JSON.parse(fs.readFileSync(manifest, 'utf8')) : {};
  const currentTask = agent => tasks.get(agent?.id);
  ctx.tools.guard(exec => denial(currentTask(exec.agent), exec.name, exec.arguments, workspace));
  async function callback(task, toolName, args, signal) {
    const response = await fetch(process.env.QQBOT_CALLBACK, {method: 'POST', headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({task_id: task.task_id, name: toolName, arguments: args}), signal});
    const result = await response.json();
    if (!result.ok) throw new Error(result.error);
    return result.result;
  }
  for (const [toolName, [description, parameters]] of Object.entries(definitions)) {
    ctx.tools.register(defineTool({name: toolName, description, parameters,
      output: {schema: {type: 'json'}, render: (_args, value) => text(JSON.stringify(value))},
      async execute(args, exec) {
        const task = currentTask(exec.agent);
        if (!task || task.cancelled) throw new Error('TASK_EXPIRED');
        return callback(task, toolName, args, exec.signal);
      }}));
  }
  async function getHandle(kind, data) {
    let generation = generations[kind];
    // Roll physical logs weekly without importing their raw history. Logical
    // conversations persist via memory + Python's retained recent context.
    if (generation && generation.expires <= Date.now()) {
      const old = handles.get(kind);
      if (old) await old.dispose();
      handles.delete(kind);
      generation = null;
    }
    if (handles.has(kind)) return handles.get(kind);
    const setup = (agentCtx, agent) => {
      agentCtx.systemPrompt.section({name: 'qqbot:instructions', order: 0, interpolate: false, text: () => {
        const task = currentTask(agent);
        const skill = fs.readFileSync(path.join(workspace, '.agents/skills/memory-management/SKILL.md'), 'utf8');
        return (task?.system || '') + '\n【必读记忆规则】\n' + skill;
      }});
    };
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
  let busy = false;
  async function run(data) {
    if (busy) throw new Error('运行中，请顺序提交任务');
    busy = true;
    let handle;
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
      const task = {...data, cancelled: false};
      tasks.set(agent.id, task);
      const before = agent.session.snapshotEvents().length;
      agent.followup({id: randomUUID(), role: 'user', source: {kind: 'user'}, content: text(data.prompt)});
      await agent.whenIdle();
      await ctx.sessions.flush(agent.session);
      const events = agent.session.snapshotEvents(before);
      const end = events.filter(e => e.type === 'turn/end').at(-1);
      const output = agent.session.deriveMessages().filter(m => m.role === 'assistant').at(-1);
      return {cancelled: task.cancelled, finish_reason: end?.data?.reason?.kind, error: end?.data?.reason?.error, final_response: (output?.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n')};
    } finally {
      if (handle) tasks.delete(handle.agent.id);
      busy = false;
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
      else if (request.url === '/cancel') {
        for (const handle of handles.values()) {
          const task = currentTask(handle.agent);
          if (task?.task_id === data.task_id) { task.cancelled = true; handle.agent.cancel({kind: 'user'}); await handle.agent.whenIdle(); }
        }
        result = {cancelled: true};
      } else if (request.url === '/close') {
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
    for (const handle of handles.values()) await handle.dispose();
    await new Promise(resolve => server.close(resolve));
  });
}
