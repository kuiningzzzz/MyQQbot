import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {grantArgs, launcherPath, probe} from '@deepseek-ai/node-addon-system/landlock-run';
import {shellPolicy, auditScene, liveShellTask} from '../shell-policy.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qqbot-shell-'));
const work = path.join(root, 'workspace');
const task = {role:'worker',kind:'chat',scene_dir:'one',expires_at:Date.now()+3600000};
process.on('exit', () => fs.rmSync(root, {recursive:true,force:true}));

test('each execution gets its current scene root, never the entire workspace', () => {
  assert.equal(shellPolicy(task,work,'a').workspaceRoot, path.join(work,'files/one'));
  assert.equal(shellPolicy({...task,scene_dir:'two'},work,'b').workspaceRoot, path.join(work,'files/two'));
  assert.equal(shellPolicy({...task,owner:true},work,'c').mode, 'danger-full-access');
  assert.throws(()=>shellPolicy({...task,scene_dir:'../escape'},work,'d'), /PERMISSION_DENIED/);
  assert.throws(()=>shellPolicy({...task,scene_dir:''},work,'d'), /PERMISSION_DENIED/);
  for(const patch of [{role:'main'},{role:'memory',kind:'memory'},{cancelled:true},{expires_at:1}]) {
    assert.equal(!!liveShellTask({...task,...patch}), false);
    assert.throws(()=>shellPolicy({...task,...patch},work,'x'), /TASK_EXPIRED/);
  }
});

test('pre-existing hardlinks cannot alias a protected file into the writable scene', async () => {
  const scene = shellPolicy({...task,scene_dir:'hardlinks'},work,'h').workspaceRoot;
  const outside = path.join(root,'protected.txt'); fs.writeFileSync(outside,'original');
  const link = path.join(scene,'alias'); fs.linkSync(outside,link);
  await assert.rejects(auditScene(scene), /硬链接/);
  assert.equal(fs.readFileSync(outside,'utf8'), 'original');
});

test('outside symlinks rejected; internal package links may stay inside the scene', {skip:process.platform==='win32'}, async () => {
  const scene = shellPolicy({...task,scene_dir:'links'},work,'l').workspaceRoot;
  fs.mkdirSync(path.join(scene,'bin'));
  fs.writeFileSync(path.join(scene,'target'),'safe');
  fs.symlinkSync('../target',path.join(scene,'bin/tool'));
  await auditScene(scene);
  fs.symlinkSync(root,path.join(scene,'outside'));
  await assert.rejects(auditScene(scene), /越界链接/);
});

const launcher = launcherPath();
const linuxReady = process.platform==='linux' && probe(launcher)==='full';
test('real Landlock permits external reads and local writes, blocks every outside write including /tmp', {skip:!linuxReady}, () => {
  const scene = shellPolicy({...task,scene_dir:'kernel'},work,'k').workspaceRoot;
  const outside=path.join(root,'outside.txt'), other=path.join(work,'files/other.txt');
  fs.writeFileSync(outside,'readable'); fs.writeFileSync(other,'other');
  const run = command => spawnSync(launcher,[...grantArgs({readOnly:['/'],readWrite:[scene,'/dev/null']}),'--','bash','-c',command], {cwd:scene,encoding:'utf8'});
  const result = run('printf inside > result.txt');
  assert.equal(result.status,0);
  // Paths are passed in environment, rather than interpolated shell text.
  const read = spawnSync(launcher,[...grantArgs({readOnly:['/'],readWrite:[scene,'/dev/null']}),'--','bash','-c','cat "$SOURCE"'], {cwd:scene,env:{...process.env,SOURCE:outside},encoding:'utf8'});
  assert.equal(read.stdout,'readable'); assert.equal(read.status,0);
  assert.equal(fs.readFileSync(path.join(scene,'result.txt'),'utf8'),'inside');
  for(const target of [outside,other,path.join(root,'new.txt')]) {
    const denied = spawnSync(launcher,[...grantArgs({readOnly:['/'],readWrite:[scene,'/dev/null']}),'--','bash','-c','printf overwrite > "$TARGET"'], {cwd:scene,env:{...process.env,TARGET:target},encoding:'utf8'});
    assert.notEqual(denied.status,0);
  }
  assert.equal(fs.readFileSync(outside,'utf8'),'readable');
  assert.equal(fs.readFileSync(other,'utf8'),'other');
});
