import test from 'node:test';
import assert from 'node:assert/strict';
import {mountTelemetry} from '../telemetry.mjs';

test('local audit frames preserve large multilingual output without opaque replay data',()=>{
  const hooks=new Map(),lines=[];
  const task={task_id:'audit-test',role:'worker',scene:'group:12'};
  mountTelemetry({on:(name,fn)=>hooks.set(name,fn)},()=>task,()=>task);
  const write=process.stderr.write;
  try {
    process.stderr.write=line=>{lines.push(line);return true;};
    hooks.get('session/event')({header:{id:'session'}},{type:'assistant/message',data:{
      text:'中文🙂'.repeat(12000),signature:'opaque',replayState:{private:'opaque'},
      content:[{type:'image',data:'binary payload'}],reasoning:'模型返回的推理'}});
  } finally {process.stderr.write=write;}
  assert.ok(lines.length>1);
  const packets=lines.map(line=>{
    assert.ok(Buffer.byteLength(line)<65536);
    assert.ok(line.startsWith('QQBOT_AI_EVENT '));
    return JSON.parse(line.slice('QQBOT_AI_EVENT '.length));
  });
  const data=JSON.parse(Buffer.concat(packets.map(p=>Buffer.from(p.data,'base64'))).toString('utf8'));
  assert.equal(data.text,'中文🙂'.repeat(12000));
  assert.equal(data.reasoning,'模型返回的推理');
  assert.equal(data.signature,undefined);
  assert.equal(data.replayState,undefined);
  assert.deepEqual(data.content,[{type:'image',note:'binary omitted'}]);
  assert.ok(packets.every(p=>p.task===task.task_id && p.parts===packets.length));
});
