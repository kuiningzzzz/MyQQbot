import fs from 'node:fs';
import path from 'node:path';

export function canonical(value) {
  let target = path.resolve(value);
  const tail = [];
  while (!fs.existsSync(target)) {
    const parent = path.dirname(target);
    if (parent === target) break;
    tail.unshift(path.basename(target));
    target = parent;
  }
  return path.join(fs.realpathSync(target), ...tail);
}

export function within(root, target) {
  const relative = path.relative(canonical(root), canonical(target));
  return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative));
}

export function denial(task, name, args, workspace) {
  if (!task || task.cancelled) return 'TASK_EXPIRED: 当前任务已结束';
  if (task.expires_at && task.expires_at <= Date.now()) return 'TASK_EXPIRED: 上下文已过保留期';
  const dispatch = new Set(['spawn_task', 'task_status', 'cancel_task']);
  if (task.role === 'main') {
    const allowed = ['send_message', 'send_forward_message', 'find_sticker', 'send_sticker', 'send_voice', ...(task.delivery ? [] : dispatch)];
    if (allowed.includes(name)) return;
    return 'DELEGATION_REQUIRED: 主对话只能聊天和分派，执行工作必须spawn子Agent';
  }
  if (dispatch.has(name)) return 'PERMISSION_DENIED: 只有主对话可分派任务，子Agent只能一次性返回';
  if (task.role === 'worker' && ['send_message', 'send_forward_message', 'send_sticker', 'send_voice'].includes(name)) return 'PERMISSION_DENIED: 子任务结果由主对话交付，文件使用send_file';
  if (name === 'delete_memory_file') return task.kind === 'memory' && task.role === 'memory' ? undefined : 'PERMISSION_DENIED: 仅记忆维护可删除暂存文件';
  if (['bash', 'pwsh'].includes(name)) {
    if (task.kind !== 'chat' || !['worker', 'scheduled'].includes(task.role)) return 'PERMISSION_DENIED: 只有聊天执行者和定时任务可运行命令';
    if (!task.owner && (args.sandbox_permissions !== undefined || args.run_in_background)) return 'PERMISSION_DENIED: 普通命令不能提升权限或脱离任务后台运行';
    return;
  }
  if (task.owner) return;
  const fileTools = new Set(['read', 'write', 'edit', 'glob', 'grep']);
  const qqTools = new Set(['send_message', 'send_forward_message', 'find_sticker', 'send_sticker', 'send_voice', 'synthesize_voice', 'recall_message', 'inspect_message', 'get_bot_help', 'list_attachments', 'download_attachment', 'send_file', 'web_search', 'web_fetch', 'search_memory', 'schedule_create', 'schedule_list', 'schedule_update', 'schedule_delete', 'integrate_memory']);
  if (name === 'read_media') return task.kind === 'chat' ? undefined : 'PERMISSION_DENIED: 记忆整理只能读取已完成的媒体补充';
  if (name === 'skill' || name === 'search_memory') return;
  if (qqTools.has(name)) {
    if (task.kind !== 'chat') return 'PERMISSION_DENIED: 记忆整理对话不能使用QQ或网络工具';
    return;
  }
  if (!fileTools.has(name)) return 'OWNER_REQUIRED: 任意命令和服务器管理操作仅允许主人当前任务';
  const target = path.resolve(task.cwd, args.file_path ?? args.path ?? '.');
  if (name === 'glob' || name === 'grep') {
    for (const key of ['pattern', 'glob', 'include']) {
      if (name === 'grep' && key === 'pattern') continue; // Regex, not a filesystem path.
      const value = args[key];
      if (typeof value === 'string' && (path.isAbsolute(value) || value.split(/[\\/]/).includes('..'))) return 'PERMISSION_DENIED: 搜索路径越界';
    }
    if (!within(workspace, target)) return 'OWNER_REQUIRED: 不能搜索工作区外目录';
    // Search tools spawn rg directly and do not use the fs provider. Reject
    // symlink-containing search roots, including directory symlinks below it.
    const check = dir => {
      for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
        if (entry.isSymbolicLink() || (entry.isFile() && fs.statSync(path.join(dir, entry.name)).nlink > 1)) throw new Error('搜索目录包含符号链接');
        if (entry.isDirectory()) check(path.join(dir, entry.name));
      }
    };
    try { if (fs.statSync(target).isDirectory()) check(target); } catch (e) { return 'PERMISSION_DENIED: ' + e.message; }
    return;
  }
  if (!within(workspace, target)) return 'OWNER_REQUIRED: 工作区外读写需要主人';
  if (fs.existsSync(target) && fs.statSync(target).isFile() && fs.statSync(target).nlink > 1) return 'PERMISSION_DENIED: 禁止硬链接';
  if (name === 'read') return;
  const memoryRelative = path.relative(path.join(task.cwd, 'memory'), target).split(path.sep).join('/');
  const memoryFile = /^(user\/[0-9]+\/(PERSON|PREFERENCE|RECENTNESS)\.md|user\/[0-9]+\/memory\/[^/<>:"|?*]+\.md|group\/[0-9]+\/[^/\r\n<>:"|?*]+\.md|common\/GLOSSARY\.md|soul\/(?:[^/\r\n<>:"|?*]+\/)*[^/\r\n<>:"|?*]+\.md)$/.test(memoryRelative);
  if (task.kind === 'memory') {
    for (const qq of task.self_ids || []) {
      if (within(path.join(task.cwd, 'memory/user', qq), target)) return 'PERMISSION_DENIED: 不能给机器人建立画像';
    }
    if (memoryFile) return;
    return 'PERMISSION_DENIED: 整理任务只能修改本批暂存记忆';
  }
  if (within(path.join(workspace, 'files', task.scene_dir), target)) return;
  if (memoryFile && memoryRelative.startsWith('soul/')) return;
  if (memoryFile && within(path.join(workspace, 'memory/user', task.qq), target)) return;
  if (memoryFile && /^[0-9]+$/.test(task.group_id || '') && within(path.join(workspace, 'memory/group', task.group_id), target)) return;
  return 'PERMISSION_DENIED: 只能修改自己的记忆、当前群记忆、soul人格与当前会话文件；宿主规则和他人资料需要主人';
}
