import test from 'node:test';
import assert from 'node:assert/strict';
import {activationError, observeActivationErrors} from '../activation-errors.mjs';

test('activation diagnostics preserve causes, bound output and redact credentials', () => {
  const error = new Error('MEMORY_INIT: unit-private-secret', {cause:new Error('missing directory')});
  const message = activationError(error, {DEEPSEEK_API_KEY:'unit-private-secret'});
  assert.match(message, /MEMORY_INIT: \[REDACTED\].*missing directory/);
  assert.doesNotMatch(activationError(new Error('Authorization: Bearer abcdefg\nstack')), /abcdefg|\n/);
  assert.doesNotMatch(activationError(new Error('invalid request body: {messages: private content}')), /private content/);
  assert.doesNotMatch(activationError(new Error('invalid schema: {"messages": "private content"}')), /private content/);
  assert.ok(activationError(new Error('x'.repeat(3000))).length <= 1500);
  assert.match(activationError(undefined), /DSH_ACTIVATION_ERROR/);
});

test('a scoped agent error stays with its own task, including admission-time lookup', () => {
  let listener;
  const ctx = {on(event, callback) { assert.equal(event, 'agent/error'); listener = callback; }};
  const first = {}, second = {}, tasks = new Map([['one', first], ['two', second]]);
  observeActivationErrors(ctx, agent => tasks.get(agent?.id));
  listener({agent:{id:'one'},error:new Error('memory init failure')});
  assert.equal(first.activation_error, 'memory init failure');
  assert.equal(second.activation_error, undefined);
  assert.doesNotThrow(() => listener({agent:{id:'unknown'},error:new Error('unrelated')}));
});
