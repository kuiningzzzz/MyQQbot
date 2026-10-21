import fs from 'node:fs';
import path from 'node:path';
import {SandboxPolicyService} from '@deepseek-ai/dsh-sandbox-policy';
import {canonical, within} from './policy.mjs';

let lookup = () => undefined;
export function bindShellTasks(resolve) { lookup = resolve; }
export function shellTask(sessionId) { return lookup(sessionId); }

export function liveShellTask(task) {
  return task && !task.cancelled && task.expires_at > Date.now() && task.kind === 'chat' &&
    ['worker', 'scheduled'].includes(task.role);
}

export function sceneDirectory(workspace, task) {
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(task.scene_dir || '')) throw new Error('PERMISSION_DENIED: 会话工作目录无效');
  const root = path.resolve(workspace, 'files', task.scene_dir);
  if (canonical(root) !== root) throw new Error('PERMISSION_DENIED: 会话工作目录不能经过符号链接');
  fs.mkdirSync(root, {recursive:true});
  if (canonical(root) !== root) throw new Error('PERMISSION_DENIED: 会话工作目录被替换');
  return root;
}

export function shellPolicy(task, workspace, sessionId) {
  if (!liveShellTask(task)) throw new Error('TASK_EXPIRED: 当前任务无命令执行权限或已结束');
  return {mode:task.owner ? 'danger-full-access' : 'workspace-write',
    workspaceRoot:task.owner ? path.resolve(workspace) : sceneDirectory(workspace, task), sessionId};
}

// A pre-existing hardlink would alias an outside inode through the writable
// directory, defeating path confinement. Internal symlinks are useful (npm's
// .bin); links leaving the scene and special files are never admitted.
export async function auditScene(root, signal, boundary = root) {
  for (const entry of await fs.promises.readdir(root, {withFileTypes:true})) {
    signal?.throwIfAborted();
    const file = path.join(root, entry.name), stat = await fs.promises.lstat(file);
    if (stat.isSymbolicLink()) {
      if (!within(boundary, file)) throw new Error('PERMISSION_DENIED: 命令目录含越界链接');
    } else if (stat.isDirectory()) {
      await auditScene(file, signal, boundary);
    } else if (!stat.isFile() || stat.nlink > 1) {
      throw new Error('PERMISSION_DENIED: 命令目录含硬链接或特殊文件');
    }
  }
}

export default class HostShellPolicy extends SandboxPolicyService {
  resolve(request = {}) {
    const sessionId = request.session?.header.id;
    const task = shellTask(sessionId);
    // Ignore persisted mode overrides and model-requested escalation. QQ
    // authority always comes from the current host task, never old history.
    if (liveShellTask(task)) return shellPolicy(task, process.env.QQBOT_WORKSPACE, sessionId);
    return {mode:'read-only', workspaceRoot:path.resolve(process.env.QQBOT_WORKSPACE), ...(sessionId ? {sessionId} : {})};
  }
}
