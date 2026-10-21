// Official kernel + local fake model/callback, exercising real Linux commands.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {setTimeout as delay} from 'node:timers/promises';
import {launcherPath, probe} from '@deepseek-ai/node-addon-system/landlock-run';

const ready = process.platform==='linux' && probe(launcherPath())==='full';
const quote = value => "'"+value.replaceAll("'", "'\\''")+"'";

test('real Spawn and scheduled fork use scene sandbox; cancellation joins and removes fork', {skip:!ready,timeout:90000}, async () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'qqbot-shell-native-'));
  const workspace=path.join(root,'workspace'), home=path.join(root,'dsh-home');
  const scene=path.join(workspace,'files/scene');
  fs.mkdirSync(scene,{recursive:true});fs.mkdirSync(home,{recursive:true});
  const profile=path.join(home,'profiles/qqbot');fs.mkdirSync(profile,{recursive:true});
  fs.writeFileSync(path.join(profile,'package.json'),JSON.stringify({name:'qqbot-test',private:true,dsh:{profile:{bundles:[]}}}));
  fs.writeFileSync(path.join(profile,'cordis.patch.yml'),'[]\n');
  const outside=path.join(root,'outside.txt');fs.writeFileSync(outside,'readable-original');
  const calls=[], finishes=[];let workerSteps=0, scheduledSteps=0, cancelMode=false;
  const block=(name,input)=>({type:'tool_use',id:'call-'+Math.random().toString(36).slice(2),name,input});
  function stream(response, content) {
    const events=[{type:'message_start',message:{id:'msg-test',type:'message',role:'assistant',model:'test',content:[],usage:{input_tokens:100,output_tokens:0}}}];
    content.forEach((value,index)=>events.push({type:'content_block_start',index,content_block:value},{type:'content_block_stop',index}));
    events.push({type:'message_delta',delta:{stop_reason:content.some(b=>b.type==='tool_use')?'tool_use':'end_turn'},usage:{output_tokens:20}},{type:'message_stop'});
    response.writeHead(200,{'Content-Type':'text/event-stream'});
    response.end(events.map(event=>'event: '+event.type+'\ndata: '+JSON.stringify(event)+'\n\n').join(''));
  }
  const api=http.createServer(async (request,response)=>{
    try {
      let body='';for await(const chunk of request)body+=chunk;
      const data=JSON.parse(body);
      if(request.url==='/tool') {
        if(data.name==='__spawn')return response.end(JSON.stringify({ok:true,result:{context:{files_directory:scene},sticker_capability:''}}));
        if(data.name==='__finish')finishes.push(data.arguments);
        return response.end(JSON.stringify({ok:true,result:{success:true}}));
      }
      calls.push(data);
      if(calls.length>20)throw new Error('Unexpected model loop');
      const names=new Set(data.tools.map(t=>t.name));let content;
      if(names.has('spawn_task')) {
        const spawned=data.messages.some(m=>m.role==='assistant' && m.content.some(b=>b.type==='tool_use' && b.name==='spawn_task'));
        content=spawned?[{type:'text',text:'accepted'}]:[block('spawn_task',{title:'local file work',prompt:'Generate a file in the scene'})];
      } else if(names.has('send_message')) {
        scheduledSteps++;
        const command=cancelMode?'printf started > started; sleep 30; printf late > after-cancel.txt':'printf scheduled > scheduled.txt';
        content=scheduledSteps===1?[block('bash',{description:'Create scheduled scene output',command})]:[{type:'text',text:'scheduled complete'}];
      } else {
        workerSteps++;
        content=workerSteps===1?[block('bash',{description:'Read outside and write scene output',command:'cat '+quote(outside)+'; printf generated > generated.txt'})]:
          workerSteps===2?[block('bash',{description:'Check outside write protection',command:'printf forbidden > '+quote(outside)})]:[{type:'text',text:'worker complete'}];
      }
      stream(response,content);
    } catch(error) {response.writeHead(500);response.end(String(error));}
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+api.address().port;
  const child=spawn(process.execPath,[fileURLToPath(new URL('../node_modules/@deepseek-ai/dsh/lib/bin.js',import.meta.url)),
    '--profile','qqbot','--patch',fileURLToPath(new URL('../profile.yml',import.meta.url))],{cwd:workspace,
    env:{...process.env,DSH_HOME:home,QQBOT_WORKSPACE:workspace,QQBOT_DSH_TOKEN:'test-only-token',
      QQBOT_CALLBACK:base+'/tool',DEEPSEEK_API_KEY:'local-test-key',DEEPSEEK_BASE_URL:base+'/anthropic'}});
  let stdout='',stderr='';child.stdout.on('data',chunk=>{stdout+=chunk;});child.stderr.on('data',chunk=>{stderr+=chunk;});
  const until=async condition=>{for(let i=0;i<1800;i++){if(condition())return;if(child.exitCode!==null)throw new Error(stderr);await delay(25);}throw new Error('Timed out: '+stderr.slice(-3000));};
  let url;
  const post=async(endpoint,data)=>{
    const result=await (await fetch(url+endpoint,{method:'POST',signal:AbortSignal.timeout(20000),headers:{Authorization:'Bearer test-only-token','Content-Type':'application/json'},body:JSON.stringify(data)})).json();
    assert.equal(result.ok,true,result.error);return result.result;
  };
  const task=(role,id)=>({kind:'chat',role,task_id:id,owner:false,qq:'123',scene_dir:'scene',group_id:'12',self_ids:['999'],cwd:workspace,
    expires_at:Date.now()+3600000,system:'Execute only the current task',worker_system:'One-shot worker',prompt:'spawn now'});
  try {
    await until(()=>stdout.includes('QQBOT_READY '));url=stdout.match(/QQBOT_READY (\S+)/)[1];
    const result=await post('/run',task('main','main'));
    assert.equal(result.finish_reason,'completed');
    await until(()=>finishes.length>0);
    assert.equal(finishes[0].state,'completed');
    assert.equal(fs.readFileSync(path.join(scene,'generated.txt'),'utf8'),'generated');
    assert.equal(fs.readFileSync(outside,'utf8'),'readable-original');
    assert.match(JSON.stringify(calls),/readable-original/);
    assert.match(JSON.stringify(calls),/file access denied under workspace-write/);
    const scheduled=await post('/scheduled',{...task('scheduled','scheduled'),prompt:'[定时任务] create file'});
    assert.equal(scheduled.finish_reason,'completed');
    assert.equal(fs.readFileSync(path.join(scene,'scheduled.txt'),'utf8'),'scheduled');
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home,'qqbot-branches.json'),'utf8')),{});
    cancelMode=true;scheduledSteps=0;
    const pending=post('/scheduled',{...task('scheduled','cancelled'),prompt:'[定时任务] wait'});
    await until(()=>fs.existsSync(path.join(scene,'started')));
    await post('/cancel',{task_id:'cancelled'});
    assert.equal((await pending).cancelled,true);
    await delay(100);
    assert.equal(fs.existsSync(path.join(scene,'after-cancel.txt')),false);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home,'qqbot-branches.json'),'utf8')),{});
  } finally {
    if(process.env.QQBOT_TEST_DIAGNOSTICS)fs.writeFileSync(path.join(process.env.QQBOT_TEST_DIAGNOSTICS,'native-debug.json'),JSON.stringify({stdout,stderr,calls,finishes}));
    if(url)await post('/close',{}).catch(()=>{});
    if(child.exitCode===null){const exited=once(child,'exit');child.kill();await exited;}
    await new Promise(resolve=>api.close(resolve));
    fs.rmSync(root,{recursive:true,force:true});
  }
});
