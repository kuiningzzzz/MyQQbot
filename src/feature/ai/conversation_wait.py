"""Event-only conversation standby. A reply opens a lease; observations do not."""
import math
import time

from .participation import ConversationEvidence

DIRECT = {'mention_self', 'reply_self', 'nickname_address', 'request_correction'}
GROUP_SECONDS, PRIVATE_SECONDS, JOIN_SECONDS = 90, 180, 20


class ConversationWait:
    def __init__(self, life):
        self.life = life

    def leases(self):
        return {scene: value for scene, value in self.life.state.get('conversations', {}).items()
                if value['until'] > time.time()}

    def record(self, ctx, task, seconds=None):
        """Called only after successful main-chat delivery (or explicit wait)."""
        scene = self.life.f.scene_key(ctx)
        if scene == 'private:0':
            return
        self.life.valid_scene(scene)
        now = time.time()
        leases = self.leases()
        previous = leases.get(scene, {})
        participants = set(previous.get('participants', []))
        identifiers = list(previous.get('message_ids', []))
        inputs = task.get('inputs', {}).values() or [(ctx, False)]
        for item, _ in inputs:
            if not item.event.get('_ai_autonomous') and self.life.f.scene_key(item) == scene:
                participants.add(str(item.user_id))
                identifier = item.event.get('message_id')
                if identifier is not None:
                    identifiers.append(str(identifier))
        leases[scene] = {'until':now + (seconds if seconds is not None else PRIVATE_SECONDS if ctx.message_type == 'private' else GROUP_SECONDS),
            'join_until':now + JOIN_SECONDS, 'participants':sorted(participants)[-64:],
            'message_ids':list(dict.fromkeys(identifiers))[-64:]}
        self.life.state['conversations'] = leases
        self.life.save()

    def replied(self, ctx, task):
        if not self.life.active or task.get('worker') or self.life.state['mode'] != 'awake' or not task.get('valid',True):
            return
        self.record(ctx, task)
        self.life.f.activity.add('conversation_reply', {'waiting':self.leases()},
            task=task.get('id', ''), role='main', scene=self.life.f.scene_key(ctx))

    def accepts(self, ctx):
        if ctx.event.get('_ai_autonomous'):
            return False
        hint = ctx.event.get('_ai_address_hint', '')
        if ctx.message_type == 'private' or hint in DIRECT:
            return True
        scene = self.life.f.scene_key(ctx)
        lease = self.leases().get(scene)
        if not lease or hint == 'addressed_to_others':
            return False
        facts = ConversationEvidence(self.life.f).facts(ctx.event, scene)
        if facts['mention_qq'] and not set(facts['mention_qq']) & self.life.f.identity.ids:
            return False
        replies = facts['replies']
        if replies:
            return any(r['message_id'] in lease['message_ids'] or r.get('is_self') for r in replies)
        # A proactive opening has no human participant yet; anyone in this
        # scene may answer during its lease. It still grants no send authority.
        return not lease['participants'] or str(ctx.user_id) in lease['participants'] or time.time() < lease['join_until']

    def pending(self):
        engine = self.life.f.engine
        items = list(engine.pending._queue)
        if engine.deferred is not None:
            items.append(engine.deferred)
        return any(result is not None or self.accepts(ctx) for ctx, _, result in items)

    def start(self, ctx=None, task=None, seconds=None):
        if self.life.state['mode'] in {'sleeping', 'deep_sleep'}:
            return {'success':False, 'code':'sleeping'}
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 1 <= seconds <= 3600):
            raise ValueError('待聊时长须为1至3600秒')
        # Explicit waiting may open a conversation without a reply. A silent
        # observation with an existing lease never extends its deadline.
        if ctx is not None and (not ctx.event.get('_ai_autonomous') or self.life.f.scene_key(ctx) in self.leases()):
            if seconds is not None or self.life.f.scene_key(ctx) not in self.leases():
                self.record(ctx, task or {}, seconds)
        leases = self.leases()
        if not leases:
            return {'success':False, 'code':'no_conversation', 'error':'没有可等待的交谈；可以选择行动或sleep。'}
        if self.pending():
            return {'success':False, 'code':'observations_pending', 'error':'已有续话或后台结果待处理，请先观察。'}
        was_waiting = self.life.state['mode'] == 'waiting'
        self.life.state.update(mode='waiting', until=max(item['until'] for item in leases.values()),
                               wake_on=[], reason='conversation_wait')
        self.life.save()
        if not was_waiting:
            self.life.f.activity.add('conversation_wait', {'scenes':leases, 'until':self.life.state['until']})
        self.life.signal.set()
        return {'success':True, 'state':'waiting', 'until':self.life.state['until'], 'scenes':leases,
                'instruction':'已进入会话待机，本轮结束；宿主等续话或到期，不调用模型，不算睡眠，也未安排额外长睡眠。'}

    def settle(self, task):
        if (self.life.active and task.get('valid') and not task.get('skip_chat_wait')
                and self.life.state['mode'] == 'awake' and self.leases()):
            self.start()

    def finish(self):
        if self.life.state['mode'] in {'sleeping', 'deep_sleep'}:
            return {'success':False, 'code':'sleeping'}
        scenes = list(self.leases())
        self.clear()
        self.life.state.update(mode='awake', until=0, reason='conversation_ended')
        self.life.save()
        self.life.f.activity.add('conversation_wait_end', {'reason':'agent_finished', 'scenes':scenes})
        self.life.signal.set()
        return {'success':True, 'state':'awake', 'instruction':'已结束当前接话等待；下一回合可继续自主工作，不算睡眠。'}

    def clear(self):
        self.life.state.pop('conversations', None)
