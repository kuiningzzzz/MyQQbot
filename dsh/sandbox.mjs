import fs from 'node:fs';
import path from 'node:path';
import {SandboxProvider, SandboxUnavailableError} from '@deepseek-ai/dsh-sandbox';
import {grantArgs, launcherPath, probe, LAUNCHER_FAILURE_EXIT, LAUNCHER_BIN} from '@deepseek-ai/node-addon-system/landlock-run';
import {auditScene} from './shell-policy.mjs';
import {canonical} from './policy.mjs';

function unavailable(mode, detail) {
  const error = new SandboxUnavailableError(mode, detail);
  error.message = 'SANDBOX_UNAVAILABLE: ' + detail + '；未执行命令，不会降级为无限制执行';
  return error;
}

// Reuse dsh's versioned Landlock launcher, with a narrower grant list. The
// stock provider grants all of host /tmp; that would expose other sessions,
// especially when the application itself is deployed beneath /tmp.
export default class SceneSandbox extends SandboxProvider {
  constructor(ctx) {
    super(ctx);
    this.enforcement = undefined;
    this.launcher = launcherPath();
  }

  async confine(argv, policy, signal) {
    signal?.throwIfAborted();
    if (process.platform !== 'linux') throw unavailable(policy.mode, '普通命令需要Linux Landlock完整写入隔离；当前平台不支持');
    this.enforcement ??= probe(this.launcher, {timeoutMs:2000});
    if (this.enforcement !== 'full') throw unavailable(policy.mode, 'Linux Landlock不可用或只能部分实施；检查内核/LSM及npm可选平台依赖');
    if (policy.mode !== 'workspace-write') throw unavailable(policy.mode, 'QQ普通命令只支持workspace-write');
    const root = path.resolve(policy.workspaceRoot);
    const scenes = path.resolve(process.env.QQBOT_WORKSPACE, 'files');
    if (canonical(root) !== root || path.dirname(root) !== scenes || !fs.statSync(root).isDirectory()) throw new Error('PERMISSION_DENIED: 非法命令写入根目录');
    await auditScene(root, signal);
    signal?.throwIfAborted();
    return {argv:[this.launcher, ...grantArgs({readOnly:['/'], readWrite:[root, '/dev/null']}), '--', ...argv],
      enforcement:'full', denialSignatures:['permission denied', 'operation not permitted'],
      runnerFailureRules:[{allowedExitCodes:[LAUNCHER_FAILURE_EXIT], fatalSignatures:[`${LAUNCHER_BIN}: `],
        informationalLines:[`${LAUNCHER_BIN}: partial enforcement (older Landlock ABI)`]}]};
  }
}
