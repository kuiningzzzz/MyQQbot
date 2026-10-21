import test from 'node:test';
import assert from 'node:assert/strict';
import {stickerShellEnv, validateScheduledTools} from '../plugin.mjs';

const sends = ['send_message', 'send_forward_message', 'send_file', 'send_sticker'];
test('only the current authorized execution receives its narrow sticker capability', () => {
  const task = {owner:true,role:'worker',expires_at:Date.now()+60000,sticker_capability:'task-a'};
  const url = 'http://127.0.0.1:1/tool';
  assert.deepEqual(stickerShellEnv(task, url), {DSH_QQBOT_STICKER_TOKEN:'task-a',DSH_QQBOT_CALLBACK:url});
  assert.equal(stickerShellEnv({...task,role:'scheduled',sticker_capability:'task-b'},url).DSH_QQBOT_STICKER_TOKEN, 'task-b');
  for (const value of [null,{...task,owner:false},{...task,role:'main'},{...task,role:'memory'},
    {...task,cancelled:true},{...task,expires_at:1},{...task,expires_at:undefined},{...task,sticker_capability:''}]) {
    assert.deepEqual(stickerShellEnv(value, url), {});
  }
});

test('a scheduled branch must retain every send tool, regardless of owner status', () => {
  for (const owner of [false, true]) {
    const task = {role: 'scheduled', owner};
    assert.doesNotThrow(() => validateScheduledTools(task, [...sends, 'read']));
    for (const missing of sends) {
      assert.throws(() => validateScheduledTools(task, sends.filter(name => name !== missing)),
        error => error.message.includes('SEND_TOOLS_MISSING') && error.message.includes(missing));
    }
  }
});

test('workers and memory tasks do not acquire sending capability through the check', () => {
  for (const role of ['main', 'worker', 'memory']) {
    const names = ['read'];
    validateScheduledTools({role}, names);
    assert.deepEqual(names, ['read']);
  }
});
