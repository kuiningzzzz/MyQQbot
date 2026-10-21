import fs from 'node:fs';
import path from 'node:path';
import {SandboxBashExecutor} from '@deepseek-ai/dsh-bash-sandbox';
import {SandboxPwshExecutor} from '@deepseek-ai/dsh-pwsh-sandbox';
import {shellTask, shellPolicy, liveShellTask} from './shell-policy.mjs';
import {canonical, within} from './policy.mjs';

const TASK = Symbol('qqbot-shell-authority');
const NativeExecutor = process.platform === 'win32' ? SandboxPwshExecutor : SandboxBashExecutor;

export default class ScopedShellExecutor extends NativeExecutor {
  // No interactive dsh approval channel is exposed to QQ users. Mode and
  // writable root are host-owned, while native results still report sandbox
  // enforcement/denials. Do not advertise an unusable escalation parameter.
  get sandboxMode() { return undefined; }

  resolve(request) {
    const sessionId = request.dshEnv?.DSH_SESSION_ID;
    const task = shellTask(sessionId);
    const workspace = process.env.QQBOT_WORKSPACE;
    const policy = shellPolicy(task, workspace, sessionId);
    const spec = super.resolve({...request, sandboxPolicy:policy});
    if (!task.owner) {
      if (path.resolve(spec.workdir) === path.resolve(workspace)) spec.workdir = policy.workspaceRoot;
      if (!within(policy.workspaceRoot, spec.workdir)) throw new Error('PERMISSION_DENIED: 普通命令须在当前会话files目录执行');
      // A private, persistent scratch area; never grant the shared host /tmp.
      const temp = path.join(policy.workspaceRoot, '.tmp');
      if (canonical(temp) !== temp) throw new Error('PERMISSION_DENIED: 临时目录不能是链接');
      fs.mkdirSync(temp, {recursive:true});
      spec.env = {...spec.env, TMPDIR:temp, TMP:temp, TEMP:temp, HOME:policy.workspaceRoot,
        XDG_CACHE_HOME:path.join(temp, 'cache'), XDG_CONFIG_HOME:path.join(temp, 'config'),
        PYTHONPYCACHEPREFIX:path.join(temp, 'pycache')};
      spec.onExpiry = 'kill';
    }
    return {...spec, [TASK]:task};
  }

  async execute(spec) {
    const task = shellTask(spec.sandboxPolicy?.sessionId);
    if (!liveShellTask(task) || task !== spec[TASK]) throw new Error('TASK_EXPIRED: 执行前任务权限已撤销');
    // Native execution retains timeout, process-tree cancellation, bounded
    // output and structured sandbox facts for bash and pwsh alike.
    return super.execute(spec);
  }
}
