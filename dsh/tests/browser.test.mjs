import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import {denial} from '../policy.mjs';
import {BrowserSlot, browserExecutable} from '../browser.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qqbot-browser-'));
fs.mkdirSync(path.join(root, 'files/scene'), {recursive:true});
const task = {kind:'chat',role:'worker',browser_enabled:true,scene_dir:'scene',cwd:root,qq:'123'};
const check=(name,args={},overrides={})=>denial({...task,...overrides}, name==='open_browser'?name:'mcp__playwright-mcp__'+name,args,root);
test('browser tools belong to enabled live execution tasks, never main or memory',()=>{
  assert.equal(check('open_browser'),undefined);
  assert.equal(check('browser_click',{target:'e1'}),undefined);
  assert.equal(check('browser_click',{target:'e1'},{role:'scheduled'}),undefined);
  assert.match(check('open_browser',{}, {role:'main',owner:true}),/DELEGATION_REQUIRED/);
  assert.match(check('open_browser',{}, {kind:'memory',role:'memory',owner:true}),/PERMISSION_DENIED/);
  assert.match(check('open_browser',{}, {cancelled:true}),/TASK_EXPIRED/);
  assert.match(check('browser_navigate',{url:'https://example.com'},{browser_enabled:false}),/BROWSER_DISABLED/);
  for(const name of ['browser_run_code_unsafe','browser_run_code','browser_evaluate']){
    assert.match(check(name),/OWNER_REQUIRED/);
    assert.equal(check(name,{}, {owner:true}),undefined);
  }
  assert.match(check('browser_install',{}, {owner:true}),/PERMISSION_DENIED/);
});
test('browser navigation and artifacts respect the current task boundaries',()=>{
  for(const url of ['file:///etc/passwd','javascript:alert(1)','https://user:pass@example.com']) assert.match(check('browser_navigate',{url}),/PERMISSION_DENIED/);
  for(const name of ['browser_navigate','browser_tabs']) assert.match(check(name,{url:'http://127.0.0.1:10010',action:'new'}),/OWNER_REQUIRED/);
  for(const url of ['http://[::1]','http://[::ffff:127.0.0.1]','http://[fd00::1]','http://169.254.169.254']) assert.match(check('browser_navigate',{url}),/OWNER_REQUIRED/);
  for(const name of ['browser_file_upload','browser_drop']) assert.match(check(name,{paths:[path.join(root,'secret')]}),/PERMISSION_DENIED/);
  assert.match(check('browser_take_screenshot',{filename:'../secret'}),/PERMISSION_DENIED/);
  assert.equal(check('browser_take_screenshot',{filename:'screen.png'}),undefined);
  assert.equal(check('browser_file_upload',{paths:[path.join(root,'files/scene/screen.png')]}),undefined);
  assert.match(check('browser_snapshot',{filename:path.join(root,'files/another/snapshot.yml')}),/PERMISSION_DENIED/);
});
test('a queued browser task is cancellable and cannot strand the slot',async()=>{
  const slot=new BrowserSlot(), first=new AbortController(), second=new AbortController(), third=new AbortController();
  await slot.acquire('a',first.signal);
  const cancelled=slot.acquire('b',second.signal);
  second.abort(new Error('cancelled'));
  await assert.rejects(cancelled,/cancelled/);
  let started=false;
  const waiting=slot.acquire('c',third.signal).then(()=>started=true);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(started,false);
  slot.release('a');await waiting;
  assert.equal(slot.owner,'c');
  slot.release('c');assert.equal(slot.owner,null);
});
test('an explicit missing browser fails without runtime downloads',()=>{
  assert.throws(()=>browserExecutable({QQBOT_BROWSER_EXECUTABLE_PATH:path.join(root,'missing')}),/BROWSER_MISSING/);
});
process.on('exit',()=>fs.rmSync(root,{recursive:true,force:true}));
