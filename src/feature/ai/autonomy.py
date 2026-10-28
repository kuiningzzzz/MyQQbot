"""A durable life scheduler around finite native dsh turns."""
import asyncio
import logging
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from src.feature.atom import MessageContext

logger = logging.getLogger(__name__)
EVENTS = {'mention_self', 'reply_self', 'nickname_address', 'private_message', 'group_message', 'task_completed'}
STATUS = {'awake':'醒着醒着，有什么事？', 'sleeping':'zzz...（正在睡觉）', 'deep_sleep':'zzz...（深度睡眠，叫不醒的那种）'}


class AutonomyRuntime:
    def __init__(self, feature):
        self.f = feature
        self.state = feature.workspace.load_state('autonomy', {'mode':'awake','until':0,'wake_on':[], 'revision':0})
        self.contacts = feature.workspace.load_state('contact-policies', {})
        self.running = False
        self.suspended = False
        self.bot = self.driver = None
        self.signal = asyncio.Event()
        self.focus = 'private:0'
        self.error = ''
        self.retry_at = 0
        self.failures = 0

    @property
    def active(self):
        return self.running and self.f.config.load().autonomy_enabled

    def save(self):
        self.f.workspace.save_state('autonomy', self.state)

    def status(self):
        if self.state.get('mode') != 'awake' and self.state.get('until', 0) <= time.time():
            self.wake('sleep_expired')
        rendered = dict(self.state)
        if self.state.get('sleep_chain'):
            rendered['sleep_chain'] = dict(self.state['sleep_chain'])
            if self.state['mode'] != 'awake':
                rendered['sleep_chain']['seconds'] += max(0, min(time.time(),self.state['until']) - self.state['started'])
        return {**rendered, 'text':STATUS[self.state['mode']], 'running':self.running,
                'enabled':self.f.enabled, 'active':self.active, 'focus':self.focus, 'last_error':self.error,
                'retry_at':self.retry_at, 'contacts':self.contacts}

    def may_run(self):
        return not self.suspended and (not self.running or self.status()['mode'] == 'awake')

    def wake(self, reason='forced', *, force=False):
        if self.state['mode'] == 'deep_sleep' and not force and reason != 'sleep_expired':
            return False
        previous = self.state['mode']
        now = time.time()
        chain = self.state.get('sleep_chain')
        if previous != 'awake' and chain:
            chain['seconds'] += max(0, min(now, self.state['until']) - self.state['started'])
            chain['end'] = now
            self.f.activity.update(chain['log_id'], chain)
        self.state.update(mode='awake', until=0, wake_on=[], reason=reason,
                          revision=self.state.get('revision', 0) + 1)
        self.save()
        self.f.activity.add('wake', {'reason':reason, 'previous':previous})
        self.signal.set()
        return True

    def matches(self, event, *, scene=None, user=None):
        if self.state['mode'] == 'deep_sleep':
            return False
        filters = self.state.get('filters', {})
        if filters.get('scenes') and scene not in filters['scenes']:
            return False
        if filters.get('users') and str(user) not in filters['users']:
            return False
        return bool(set(event) & set(self.state.get('wake_on', [])))

    def notify(self, event, *, scene=None, user=None):
        if self.active and self.state['mode'] != 'awake' and self.matches({event}, scene=scene, user=user):
            self.wake(event)
        self.signal.set()

    def receive(self, ctx):
        """Original messages have already been archived before this admission."""
        if not self.running:
            return True
        self.status()
        hint = ctx.event.get('_ai_address_hint', '')
        events = {hint, 'private_message' if ctx.message_type == 'private' else 'group_message'}
        if self.state['mode'] != 'awake':
            matched = self.matches(events, scene=self.f.scene_key(ctx), user=ctx.user_id)
            if matched and events & set(self.state.get('wake_on', [])) == {'group_message'}:
                self.state['observed_count'] = self.state.get('observed_count',0) + 1
                matched = self.state['observed_count'] >= self.state.get('filters',{}).get('message_threshold',1)
                self.save()
            if not matched:
                self.f.activity.add('sleep_observation', {'message_id':ctx.event.get('message_id'), 'sender':ctx.user_id}, scene=self.f.scene_key(ctx))
                return False
            self.wake('message:' + hint)
        self.signal.set()
        return True

    def sleep(self, seconds, deep=False, wake_on=None, reason='', revision=None, filters=None, *, check_pending=True):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 365 * 86400:
            raise ValueError('睡眠时长须为1秒至365天')
        if not isinstance(deep, bool) or not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError('睡眠参数无效')
        events = list(EVENTS & {'mention_self', 'reply_self', 'nickname_address', 'private_message', 'task_completed'}) if wake_on is None else wake_on
        if not isinstance(events, list) or any(e not in EVENTS for e in events):
            raise ValueError('不支持的唤醒事件')
        filters = {} if filters is None else filters
        if not isinstance(filters, dict) or set(filters) - {'scenes','users','message_threshold'}:
            raise ValueError('睡眠筛选参数无效')
        scenes, users, threshold = filters.get('scenes',[]), filters.get('users',[]), filters.get('message_threshold',1)
        if not isinstance(scenes,list) or not isinstance(users,list) or isinstance(threshold,bool) or not isinstance(threshold,int) or threshold < 1:
            raise ValueError('睡眠筛选参数无效')
        for scene in scenes:self.valid_scene(scene)
        if any(not isinstance(user,str) or not re.fullmatch(r'[1-9][0-9]*',user) for user in users):
            raise ValueError('唤醒联系人使用QQ号字符串')
        # Check accepted but not yet consumed events atomically before parking.
        # Nonmatching observations may remain queued/archived until the wake.
        queued = list(self.f.engine.pending._queue)
        if self.f.engine.deferred is not None:
            queued.append(self.f.engine.deferred)
        if check_pending and not deep:
            for c, _, completion in queued:
                if c.event.get('_ai_autonomous') and completion is None:
                    continue
                types = {'task_completed'} if completion is not None else {
                    c.event.get('_ai_address_hint',''), 'private_message' if c.message_type=='private' else 'group_message'}
                if (types & set(events) and (not scenes or self.f.scene_key(c) in scenes)
                        and (not users or str(c.user_id) in users)):
                    return {'success':False,'code':'observations_pending','error':'已有符合唤醒条件的消息或任务结果待处理，请先观察。'}
        if revision is not None and revision != self.state.get('revision', 0):
            return {'success':False, 'code':'sleep_interrupted', 'error':'睡眠决定期间发生了唤醒，请重新判断'}
        if self.state['mode'] != 'awake':
            self.wake('reschedule_sleep', force=True)
        now = time.time()
        chain = self.state.get('sleep_chain')
        if not chain or not chain.get('mergeable'):
            chain = {'id':uuid.uuid4().hex, 'start':now, 'seconds':0, 'parts':0, 'mergeable':True}
            chain['log_id'] = self.f.activity.add('sleep_summary', chain)
        chain['parts'] += 1
        chain['planned_until'] = now + seconds
        self.state.update(mode='deep_sleep' if deep else 'sleeping', started=now, until=now + seconds,
                          wake_on=[] if deep else list(dict.fromkeys(events)), reason=reason, sleep_chain=chain,
                          filters=filters, observed_count=0)
        self.save()
        self.f.activity.update(chain['log_id'], chain)
        self.f.activity.add('sleep', {'seconds':seconds, 'deep':deep, 'wake_on':self.state['wake_on'], 'reason':reason})
        self.signal.set()
        return {'success':True, 'state':self.state['mode'], 'until':self.state['until'],
                'sleep_chain_id':chain['id'], 'combined_sleep_seconds':chain['seconds'], 'parts':chain['parts'],
                'instruction':'本轮已安排休息，宿主将暂停后续推理；无需发QQ确认。'}

    def activity(self, kind, at=None):
        chain = self.state.get('sleep_chain')
        if chain and chain.get('mergeable') and self.state['mode'] == 'awake' and (at is None or at >= chain.get('end',chain['start'])):
            chain['mergeable'] = False
            self.save()

    @staticmethod
    def valid_scene(scene):
        if not isinstance(scene, str) or not re.fullmatch(r'(group|private):[1-9][0-9]*', scene):
            raise ValueError('会话须为group:群号或private:QQ号')
        return scene

    @staticmethod
    def validate_policy(data):
        if not isinstance(data, dict) or set(data) - {'enabled','start','end','note'}:
            raise ValueError('主动联系设置只接受enabled/start/end/note')
        result = {'enabled':True, 'start':'09:00', 'end':'23:00', 'note':'', **data}
        if not isinstance(result['enabled'], bool) or not isinstance(result['note'], str) or len(result['note']) > 2000:
            raise ValueError('主动联系参数无效')
        for key in ('start', 'end'):
            value = result[key]
            if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
                raise ValueError('时段使用北京时间HH:MM')
        return result

    def permitted(self, scene, now=None):
        policy = self.contacts.get(scene)
        if not policy or not policy['enabled']:
            return False
        local = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo('Asia/Shanghai')).strftime('%H:%M')
        start, end = policy['start'], policy['end']
        return start == end or (start <= local < end if start < end else local >= start or local < end)

    def contact(self, action='list', scene=None, policy=None, *, ctx=None, admin=False):
        if action == 'list':
            if admin or ctx and ctx.event.get('_ai_autonomous'):
                return {'success':True, 'contacts':self.contacts}
            key = self.f.scene_key(ctx)
            return {'success':True, 'contacts':{key:self.contacts[key]} if key in self.contacts else {}}
        scene = self.valid_scene(scene or self.f.scene_key(ctx))
        if not admin:
            if ctx.event.get('_ai_autonomous'):
                if action != 'delete':
                    raise PermissionError('自主回合可查看或放弃联系许可；扩大许可须由该会话的真实用户提出')
            elif scene != self.f.scene_key(ctx):
                raise PermissionError('只能管理当前群或自己的私聊联系许可')
        if action == 'delete':
            self.contacts.pop(scene, None)
        elif action == 'set':
            self.contacts[scene] = self.validate_policy(policy or {})
        else:
            raise ValueError('action须为list/set/delete')
        self.f.workspace.save_state('contact-policies', self.contacts)
        self.activity('contact_policy')
        self.f.activity.add('contact_policy', {'action':action, 'scene':scene, 'policy':self.contacts.get(scene)})
        self.signal.set()
        return {'success':True, 'scene':scene, 'policy':self.contacts.get(scene)}

    def context(self, scene=None):
        scene = scene or self.focus
        if scene != 'private:0':
            self.valid_scene(scene)
        kind, identifier = scene.split(':')
        clients = getattr(self.bot, 'connected_clients', set())
        return MessageContext(bot=self.bot, websocket=next(iter(clients), None),
            event={'_ai_autonomous':True, '_ai_arrival_forced':False, 'message_id':'life-' + uuid.uuid4().hex, 'message':[]},
            msg='[自主活动] 观察当前变化，继续感兴趣的事情，或安排睡眠。', raw_msg='',
            message_type=kind, user_id=int(identifier) if kind == 'private' else 0,
            group_id=int(identifier) if kind == 'group' else None)

    def focus_scene(self, scene):
        self.valid_scene(scene)
        if not self.permitted(scene):
            raise PermissionError('该会话没有许可或当前不在允许主动联系的时段')
        self.focus = scene
        self.f.activity.add('focus_scene', {'scene':scene})
        return {'success':True, 'scene':scene, 'instruction':'下一自主回合将绑定该会话；本轮不要发送到旧会话。'}

    async def command(self, ctx):
        command = ctx.msg.strip()
        if command not in {'/ai-status', '/ai-wakeup'}:
            return False
        if command == '/ai-status':
            await ctx.bot.reply(ctx, self.status()['text'])
        elif not ctx.bot.master_service.is_master(ctx.user_id):
            await ctx.bot.reply(ctx, '只有主人可以强制叫醒。')
        else:
            self.wake('owner_command', force=True)
            self.f.engine._start_worker()
            await ctx.bot.reply(ctx, STATUS['awake'])
        return True

    async def start(self, bot):
        if self.driver and not self.driver.done():
            return
        self.bot = bot
        self.running = True
        self.driver = asyncio.create_task(self.drive())

    async def wait(self, seconds):
        self.signal.clear()
        try:
            await asyncio.wait_for(self.signal.wait(), max(.01, seconds))
        except TimeoutError:
            pass

    async def drive(self):
        while self.running:
            try:
                if self.suspended or not self.active or not self.f.enabled or not self.f.config.load().chat_enabled:
                    await self.wait(5); continue
                state = self.status()
                if state['mode'] != 'awake':
                    await self.wait(min(60, max(.01, state['until'] - time.time()))); continue
                ready = getattr(self.bot, 'can_send_conversation', lambda:False)()
                if not ready or not self.f.config.load().api_key:
                    await self.wait(5); continue
                if self.f.engine.tasks or self.f.engine.current:
                    await self.wait(60); continue
                if self.f.engine.has_pending():
                    self.f.engine._start_worker(); await self.wait(60); continue
                if self.retry_at > time.time():
                    await self.wait(min(60, self.retry_at - time.time())); continue
                await self.f.engine.pending.put((self.context(), False, None))
                self.f.engine._start_worker()
                await self.wait(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed(exc)
                await self.wait(5)

    def failed(self, exc):
        self.failures += 1
        self.error = str(exc)
        self.retry_at = time.time() + min(1800, 60 * 2 ** min(self.failures - 1, 5))
        self.f.activity.add('autonomy_error', {'error':self.error, 'retry_at':self.retry_at})
        logger.exception('自主活动失败，将退避重试')

    async def stop(self):
        self.running = False
        self.signal.set()
        if self.driver:
            self.driver.cancel()
            await asyncio.gather(self.driver, return_exceptions=True)
