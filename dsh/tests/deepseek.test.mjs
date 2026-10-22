import test from 'node:test';
import assert from 'node:assert/strict';
import {projectToolUpdates} from '@deepseek-ai/dsh-llm';
import {ensureImmediateTool, compatibleAdapter} from '../deepseek.mjs';

const schema = name => ({name, description:name, parameters:{type:'object',properties:{}}});
const freeze = value => { Object.freeze(value); for (const item of Object.values(value)) if (item && typeof item === 'object') freeze(item); return value; };

test('native fork projection with no remaining baseline gets a non-deferred tool', () => {
  const tools = ['delete_memory_file','edit','glob','grep','read','search_memory','skill','write'].map(schema);
  const addition = {id:'change',role:'developer',content:tools.map(tool=>({type:'tool-addition',toolName:tool.name}))};
  const history = {tools:[schema('send_message')],updates:[{messageId:'change',additions:tools}]};
  const projected = projectToolUpdates([addition,{id:'ask',role:'user',content:[{type:'text',text:'维护记忆'}]}], tools, 'addition-only', history);
  assert.ok(projected.tools.every(tool=>tool.deferLoading === true)); // Actual rc.2 regression.
  const options = freeze({...projected,provider:'deepseek-official',model:'deepseek-flash',toolHistory:history});
  const result = ensureImmediateTool(options);
  assert.equal(result.tools.find(tool=>tool.name === 'read').deferLoading, false);
  assert.deepEqual(result.tools.map(tool=>tool.name), tools.map(tool=>tool.name));
  assert.equal(result.tools.filter(tool=>tool.deferLoading === true).length, 7);
  assert.ok(options.tools.every(tool=>tool.deferLoading === true));
  assert.equal(result.toolHistory, options.toolHistory); // No durable history mutation.
  assert.equal(result.messages[0].content.some(block=>block.toolName === 'read'), false);
  assert.equal(result.messages[0].content.length, 7);
  assert.equal(ensureImmediateTool(result), result);
});

test('ordinary and tool-free requests remain unchanged', () => {
  for (const tools of [undefined, [], [schema('read')], [{...schema('read'),deferLoading:false},{...schema('write'),deferLoading:true}]]) {
    const options = freeze({messages:[],tools});
    assert.equal(ensureImmediateTool(options), options);
  }
});

test('fallback preserves developer text, tool removal/re-addition and other messages', () => {
  const user = {role:'user',content:[{type:'text',text:'original'}]};
  const options = freeze({tools:[{...schema('read'),deferLoading:true}],messages:[
    {role:'developer',content:[{type:'tool-addition',toolName:'read'}]},
    {role:'developer',content:[{type:'text',text:'keep instructions'},{type:'tool-addition',toolName:'read'}]},
    {role:'developer',content:[{type:'tool-removal',toolName:'read'}]},
    {role:'developer',content:[{type:'tool-addition',toolName:'read'}]},user,
  ]});
  const result = ensureImmediateTool(options);
  assert.equal(result.messages.length, 4);
  assert.deepEqual(result.messages[0].content, [{type:'text',text:'keep instructions'}]);
  assert.equal(result.messages[1].content[0].type, 'tool-removal');
  assert.equal(result.messages[2].content[0].type, 'tool-addition');
  assert.equal(result.messages[3], user);
});

test('official adapter keeps binding and metadata for prepared and direct streams', async () => {
  const model = {id:'deepseek-flash',toolUpdate:'addition-only'};
  class Adapter {
    #marker = 'bound';
    providerInfo() { return this.#marker; }
    async prepareCall() { return Object.freeze({model,stream:options=>this.stream(options)}); }
    *stream(options) { yield {marker:this.#marker,options}; }
  }
  const adapter = compatibleAdapter(new Adapter());
  const options = freeze({messages:[],tools:[{...schema('read'),deferLoading:true}]});
  const prepared = await adapter.prepareCall('deepseek-official','deepseek-flash');
  assert.equal(prepared.model, model);
  assert.equal(adapter.providerInfo(), 'bound');
  for (const stream of [adapter.stream(options),prepared.stream(options)]) {
    const [result] = [...stream];
    assert.equal(result.marker, 'bound');
    assert.equal(result.options.tools[0].deferLoading, false);
  }
});
