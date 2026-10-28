import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeSleepContext} from '../sleep-context.mjs';
const text = value=>[{type:'text',text:value}];
function exchange(id, chain='rest', automatic=true, extraCall=null) {
  const calls=[{type:'tool-call',id,name:'sleep',arguments:'{"duration_seconds":3600}'}];
  if(extraCall)calls.push({type:'tool-call',id:id+'other',name:extraCall,arguments:'{}'});
  return [{role:'user',content:text(automatic?JSON.stringify({input_kind:'autonomous_cycle'}):'请休息一会儿')},
    {role:'assistant',content:calls}, {role:'tool',toolCallId:id,content:text(JSON.stringify({success:true,sleep_chain_id:chain}))},
    ...(extraCall?[{role:'tool',toolCallId:id+'other',content:text('done')}]:[])];
}
test('adjacent sleep-only exchanges are projected to one complete exchange',()=>{
  const messages=[...exchange('first'),...exchange('second'),{role:'user',content:text('最新累计已休息六小时')}];
  const snapshot=JSON.stringify(messages);
  const result=mergeSleepContext({messages});
  assert.equal(JSON.stringify(messages),snapshot);
  assert.equal(result.messages.filter(m=>m.role==='assistant').length,1);
  assert.equal(result.messages.find(m=>m.role==='assistant').content[0].id,'second');
  assert.equal(result.messages.find(m=>m.role==='tool').toolCallId,'second');
  assert.equal(result.messages.at(-1),messages.at(-1));
});
test('human instructions are preserved and never left as an unhandled request',()=>{
  const messages=[...exchange('human','rest',false),...exchange('next')];
  const result=mergeSleepContext({messages});
  assert.equal(result.messages[0],messages[0]);
  assert.match(result.messages[1].content[0].text,/宿主休息摘要.*原请求已受理/);
  assert.equal(result.messages.filter(m=>m.role==='tool').length,1);
});
test('substantive work, different rest spans and unfinished tool calls are retained',()=>{
  for(const messages of [[...exchange('a','one'),...exchange('b','two')],
    [...exchange('a','rest',true,'web_search'),...exchange('b')],
    [...exchange('a').slice(0,2),...exchange('b')]]) {
    const options={messages};assert.equal(mergeSleepContext(options),options);
  }
});
test('invalid historical tool arguments cannot break a later model request',()=>{
  const earlier=exchange('a','rest',true,'contact_policy');
  earlier[1].content[1].arguments='invalid json';
  const options={messages:[...earlier,...exchange('b')]};
  assert.equal(mergeSleepContext(options),options);
});
