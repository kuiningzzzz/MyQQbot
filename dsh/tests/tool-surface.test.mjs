import test from 'node:test';
import assert from 'node:assert/strict';
import {validateScheduledTools} from '../plugin.mjs';

const sends = ['send_message', 'send_forward_message', 'send_file'];

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
