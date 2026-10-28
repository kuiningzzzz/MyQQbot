// Request-only projection. Never edit native session logs or unbalance tool
// calls/results: remove only complete, otherwise-idle sleep exchanges.
export function mergeSleepContext(options) {
  const groups = [];
  let group;
  for (let i=0;i<options.messages.length;i++) {
    const message = options.messages[i];
    if (message.role === 'user' && message.source?.kind !== 'runtime-context') {
      group = {indices:[],calls:[],results:new Set(),chain:null,automatic:false};groups.push(group);
      try { group.automatic = JSON.parse(message.content.filter(b=>b.type==='text').map(b=>b.text).join('')).input_kind === 'autonomous_cycle'; } catch {}
    }
    if (!group) continue;
    group.indices.push(i);
    if (message.role === 'assistant') for (const block of message.content) if (block.type === 'tool-call') group.calls.push(block);
    if (message.role === 'tool') {
      group.results.add(message.toolCallId);
      for (const block of message.content) if (block.type === 'text') {
        try { const data=JSON.parse(block.text);if(data.success && data.sleep_chain_id) group.chain=data.sleep_chain_id; } catch {}
      }
    }
  }
  const candidates = groups.filter(g=>g.chain && g.calls.length && g.calls.every(call=>{
    if (!['sleep','autonomy_status','contact_policy'].includes(call.name) || !g.results.has(call.id)) return false;
    if (call.name !== 'contact_policy') return true;
    try { return JSON.parse(call.arguments || '{}').action === 'list'; } catch { return false; }
  }));
  const last = new Map(candidates.map(g=>[g.chain,g]));
  const drop = new Set(), notices = new Map();
  for (const g of candidates) if (last.get(g.chain) !== g) {
    for (const i of g.indices) {
      const role=options.messages[i].role;
      if (role === 'assistant' || role === 'tool' || role === 'user' && g.automatic) drop.add(i);
    }
    if (!g.automatic) notices.set(g.indices.at(-1), {role:'user',content:[{type:'text',text:'【宿主休息摘要】本轮休息已并入后续累计记录；原请求已受理，不要再次执行。sleep_chain_id='+g.chain}]});
  }
  if (!drop.size) return options;
  const messages=[];
  for(let i=0;i<options.messages.length;i++) {
    if(!drop.has(i))messages.push(options.messages[i]);
    if(notices.has(i))messages.push(notices.get(i));
  }
  return {...options,messages};
}
