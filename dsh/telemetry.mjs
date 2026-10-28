// Private AI observability through the existing child stderr pipe only.
// No network destination. The Python host persists these packets for admin.
import {randomUUID} from 'node:crypto';
export function mountTelemetry(ctx, taskForSession, taskForAgent) {
  const emit = (task, kind, data) => {
    if (!task) return;
    const clean = JSON.stringify(data, (key, value) => {
      if (['replayState','replay_state','encrypted_content','signature'].includes(key)) return undefined;
      if (value && typeof value === 'object' && value.type === 'image') return {type:'image',note:'binary omitted'};
      return value;
    }) || 'null';
    const bytes=Buffer.from(clean,'utf8');
    const packet = randomUUID(), parts = Math.max(1, Math.ceil(bytes.length / 16000));
    for (let i=0;i<parts;i++) process.stderr.write('QQBOT_AI_EVENT ' + JSON.stringify({packet,part:i,parts,kind,
      task:task.task_id,role:task.role,time:Date.now()/1000,scene:task.scene || task.scene_dir || '',data:bytes.subarray(i*16000,(i+1)*16000).toString('base64')})+'\n');
  };
  ctx.on('session/event',(session,event)=>emit(taskForSession(session.header.id),event.type,event.data));
  ctx.on('agent/assistant-stream',({agent,frame})=>emit(taskForAgent(agent),'model_stream',frame));
}
