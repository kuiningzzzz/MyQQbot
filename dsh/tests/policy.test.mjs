import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {denial} from '../policy.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qqbot-policy-'));
const work = path.join(root, 'workspace');
fs.mkdirSync(path.join(work, 'memory/user/123'), {recursive:true});
fs.mkdirSync(path.join(work, 'files/scene'), {recursive:true});
fs.mkdirSync(path.join(work, 'tmp/memory-stage/memory/user/123'), {recursive:true});
const task = {kind:'chat', role:'worker', cwd:work, qq:'123', scene_dir:'scene', self_ids:['999']};
const check = (name, file, override={}) => denial({...task,...override}, name, {file_path:file}, work);

test('ordinary user can use native own-memory and scene writes', () => {
  assert.equal(check('write', 'memory/user/123/PERSON.md'), undefined);
  assert.equal(check('edit', 'files/scene/result.txt'), undefined);
  assert.match(check('write', 'memory/user/456/PERSON.md'), /PERMISSION_DENIED/);
  assert.match(check('write', 'files/another/result.txt'), /PERMISSION_DENIED/);
  assert.match(check('write', '.agents/skills/memory-management/SKILL.md'), /PERMISSION_DENIED/);
});
test('group memory writes require the actual current group, private chats cannot borrow it', () => {
  assert.equal(check('write', 'memory/group/158132790/群规.md', {group_id:'158132790'}), undefined);
  assert.equal(check('edit', 'memory/group/158132790/共同项目.md', {group_id:'158132790',role:'scheduled'}), undefined);
  assert.match(check('write', 'memory/group/42/群规.md', {group_id:'158132790'}), /PERMISSION_DENIED/);
  assert.match(check('write', 'memory/group/158132790/群规.md'), /PERMISSION_DENIED/);
  for (const file of ['memory/group/158132790/topic/细节.md','memory/group/158132790/群规.txt','memory/group/abc/群规.md']) {
    assert.match(check('write', file, {group_id:'158132790'}), /PERMISSION_DENIED/);
  }
  assert.equal(check('write', 'memory/group/42/群规.md', {owner:true}), undefined);
});
test('main delegates every execution even for owner; children cannot spawn or chat', () => {
  assert.match(check('write', 'files/scene/a', {role:'main', owner:true}), /DELEGATION_REQUIRED/);
  assert.match(check('web_search', '.', {role:'main', owner:true}), /DELEGATION_REQUIRED/);
  assert.equal(check('spawn_task', '.', {role:'main'}), undefined);
  assert.match(check('spawn_task', '.', {owner:true}), /PERMISSION_DENIED/);
  assert.match(check('send_message', '.', {owner:true}), /PERMISSION_DENIED/);
  assert.match(check('read', '.', {expires_at:1}), /TASK_EXPIRED/);
});
test('owner status belongs only to current task and expired tasks never run', () => {
  assert.match(check('bash', '.'), /OWNER_REQUIRED/);
  assert.equal(check('bash', '.', {owner:true}), undefined);
  assert.match(check('bash', '.', {owner:true,cancelled:true}), /TASK_EXPIRED/);
  assert.match(denial(null,'read',{},work), /TASK_EXPIRED/);
  assert.match(check('read', '../secret'), /OWNER_REQUIRED/);
});
test('sticker lookup is lightweight and sticker sending respects conversation roles', () => {
  for (const name of ['find_sticker','send_sticker']) {
    assert.equal(check(name, '.', {role:'main'}), undefined);
    assert.equal(check(name, '.', {role:'scheduled'}), undefined);
    assert.match(check(name, '.', {kind:'memory',role:'memory'}), /PERMISSION_DENIED/);
  }
  assert.match(check('send_sticker', '.', {role:'worker',owner:true}), /PERMISSION_DENIED/);
});
test('scheduled forks can execute and deliver, but do not inherit owner or spawn authority', () => {
  for (const name of ['send_message','send_file','read_media','web_search','get_bot_help','schedule_create','schedule_list','schedule_update','schedule_delete','integrate_memory']) {
    assert.equal(check(name, '.', {role:'scheduled'}), undefined);
    assert.match(check(name, '.', {kind:'memory',role:'memory'}), /PERMISSION_DENIED/);
    if (name === 'send_message') assert.equal(check(name, '.', {role:'main',owner:true}), undefined);
    else assert.match(check(name, '.', {role:'main',owner:true}), /DELEGATION_REQUIRED/);
  }
  assert.match(check('bash','.',{role:'scheduled'}), /OWNER_REQUIRED/);
  assert.match(check('spawn_task','.',{role:'scheduled',owner:true}), /PERMISSION_DENIED/);
});
test('maintenance writes only staged memory, cannot send or execute', () => {
  const maintenance = {...task,kind:'memory',cwd:path.join(work,'tmp/memory-stage')};
  assert.equal(denial(maintenance,'write',{file_path:'memory/user/123/PERSON.md'},work), undefined);
  assert.equal(denial(maintenance,'write',{file_path:'memory/group/42/群规.md'},work), undefined);
  assert.match(denial(maintenance,'write',{file_path:path.join(work,'memory/group/42/群规.md')},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'write',{file_path:'memory/group/42/nested/群规.md'},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'write',{file_path:path.join(work,'memory/user/123/PERSON.md')},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'write',{file_path:'memory/user/999/PERSON.md'},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'send_message',{},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'web_search',{},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'web_fetch',{},work), /PERMISSION_DENIED/);
  assert.match(denial(maintenance,'bash',{},work), /OWNER_REQUIRED/);
  assert.equal(denial(maintenance,'search_memory',{},work), undefined);
});
test('search path traversal and absolute glob patterns rejected', () => {
  assert.match(denial(task,'glob',{pattern:'../**'},work), /PERMISSION_DENIED/);
  assert.match(denial(task,'grep',{pattern:'secret',path:root},work), /OWNER_REQUIRED/);
  assert.equal(denial(task,'grep',{pattern:'foo.*',path:'memory'},work), undefined);
});
test('hardlinks cannot expose host files through the workspace', () => {
  const secret = path.join(root, 'secret'); fs.writeFileSync(secret, 'secret');
  const link = path.join(work, 'files/scene/hardlink'); fs.linkSync(secret, link);
  assert.match(check('read', link), /PERMISSION_DENIED/);
  assert.match(denial(task,'grep',{pattern:'secret',path:'files'},work), /PERMISSION_DENIED/);
});
process.on('exit', () => fs.rmSync(root, {recursive:true,force:true}));
