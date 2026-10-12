import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.feature.atom import MessageContext
from src.feature.ai.feature import AiFeature
from src.feature.timing.schedule import BJT, PAST, next_run, parse_time, display_lunar_time
from src.feature.timing.storage import TaskStore, Conflict
from src.feature.timing.feature import TimingFeature, split_command, body_text
from src.feature.timing.admin import TimingAdmin


class CalendarTests(unittest.TestCase):
    def assert_next(self, flag, text, now, expected):
        stamp = next_run(flag, text, datetime.fromisoformat(now).replace(tzinfo=BJT))
        self.assertEqual(datetime.fromtimestamp(stamp, BJT).strftime('%Y-%m-%d %H:%M:%S'), expected)

    def test_recurring_past_goes_forward_and_seconds_are_preserved(self):
        self.assert_next('-d', '08-00-01', '2026-10-11T10:00:00', '2026-10-12 08:00:01')
        self.assert_next('-w', '07-08-00-00', '2026-10-11T10:00:00', '2026-10-18 08:00:00')
        self.assert_next('-w', '01-08-00-00', '2026-10-11T10:00:00', '2026-10-12 08:00:00')
        self.assert_next('-m', '31-00-00-00', '2026-04-01T10:00:00', '2026-05-31 00:00:00')
        self.assert_next('-y', '02-29-00-00-00', '2026-10-11T10:00:00', '2028-02-29 00:00:00')
        self.assert_next('-y', '10-11-00-00-00', '2026-10-11T10:00:00', '2027-10-11 00:00:00')
        self.assert_next('-o', '27-01-01-00-00-01', '2026-10-11T10:00:00', '2027-01-01 00:00:01')

    def test_past_once_exact_error_and_invalid_calendar(self):
        now = datetime(2026, 10, 11, 10, tzinfo=BJT)
        for text in ('26-10-11-09-59-59', '2026-10-11-10-00-00'):
            with self.assertRaisesRegex(ValueError, PAST):
                next_run('-o', text, now)
        for flag, text in [('-o', '26-02-29-00-00-00'), ('-w', '08-00-00-00'), ('-m', '00-00-00-00'), ('-d', '24-00-00'), ('-y', '04-31-00-00-00'), ('-d', '8-00-00')]:
            with self.assertRaises(ValueError):
                next_run(flag, text, now)

    def test_lunar_annual_and_once_match_calendar_and_cross_lunar_new_year(self):
        # Fixed solar/lunar fixtures from Hong Kong Observatory 2025-2027 tables:
        # https://www.hko.gov.hk/tc/gts/time/calendar/pdf/files/2026.pdf
        self.assert_next('-y-cn', '01-01-00-00-01', '2026-01-01T10:00:00', '2026-02-17 00:00:01')
        self.assert_next('-y-cn', '01-01-00-00-00', '2026-02-17T00:00:00', '2027-02-06 00:00:00')
        self.assert_next('-y-cn', '12-01-08-00-00', '2026-01-01T00:00:00', '2026-01-19 08:00:00')
        self.assert_next('-y-cn', '08-15-00-00-00', '2026-09-01T00:00:00', '2026-09-25 00:00:00')
        self.assert_next('-o-cn', '26-01-01-08-02-03', '2026-01-01T00:00:00', '2026-02-17 08:02:03')
        self.assert_next('-o-cn', '2027-01-01-00-00-00', '2026-10-12T00:00:00', '2027-02-06 00:00:00')

    def test_lunar_leap_month_and_missing_thirtieth(self):
        self.assert_next('-m-cn', '01-08-00-00', '2025-06-25T08:00:00', '2025-07-25 08:00:00')
        self.assert_next('-m-cn', '01-08-00-00', '2025-07-25T08:00:00', '2025-08-23 08:00:00')
        # Annual June 1 does not fire twice in 2025's leap June.
        self.assert_next('-y-cn', '06-01-08-00-00', '2025-06-25T08:00:00', '2026-07-14 08:00:00')
        self.assert_next('-m-cn', '30-08-00-00', '2025-07-24T08:00:00', '2025-09-21 08:00:00')
        self.assert_next('-y-cn', '02-30-08-00-00', '2025-01-01T00:00:00', '2027-04-06 08:00:00')

    def test_lunar_validation_and_week_day_compatibility(self):
        now = datetime(2026, 10, 12, tzinfo=BJT)
        for flag, value in [('-o-cn','2026-02-30-00-00-00'), ('-o-cn','1899-01-01-00-00-00'),
                            ('-o-cn','2101-01-01-00-00-00'), ('-y-cn','13-01-00-00-00'),
                            ('-y-cn','01-31-00-00-00'), ('-m-cn','31-00-00-00'),
                            ('-o-cn','2026-00-01-00-00-00'), ('-y-cn-cn','01-01-00-00-00')]:
            with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                parse_time(flag, value)
        with self.assertRaisesRegex(ValueError, PAST):
            next_run('-o-cn', '26-01-01-00-00-00', now)
        for flag, value in [('-w','01-08-00-00'), ('-d','08-00-00')]:
            self.assertEqual(next_run(flag, value, now), next_run(flag+'-cn', value, now))
        utc = now.astimezone(timezone.utc)
        self.assertEqual(next_run('-y-cn', '01-01-08-00-00', now), next_run('-y-cn', '01-01-08-00-00', utc))
        self.assertEqual(display_lunar_time(next_run('-o-cn','2027-01-01-08-00-00',now)), '2027年正月初一 08:00:00')


class TimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ai = AiFeature(root / 'ai')
        self.ai.config.save({'vector_index_enabled': False, 'memory_auto_learn': False})
        self.master = SimpleNamespace(is_master=lambda qq: int(qq or 0) == 123)
        self.f = TimingFeature(self.ai, self.master, root / 'timing.sqlite3')
        self.ai.timing = self.f
        self.ws = object()
        self.bot = SimpleNamespace(reply=AsyncMock(), call_action=AsyncMock(), master_service=self.master,
                                   connected_clients={self.ws}, _self_ids={self.ws:'999'})
        self.f.bot = self.bot

    async def asyncTearDown(self):
        await self.f.stop()
        await self.ai.stop()
        self.temp.cleanup()

    def ctx(self, text='', *, group=12, qq=456, segments=None):
        return MessageContext(bot=self.bot, websocket=self.ws, event={'self_id':999, 'message': segments or [{'type':'text','data':{'text':text}}]}, msg=text.casefold(),raw_msg=text,
            message_type='group' if group else 'private',user_id=qq,group_id=group)

    def create(self, **extra):
        return self.f.save({'kind':'ai','scene':'group:12','flag':'-d','time':'08-00-00','prompt':'核实新闻并播报', **extra})

    async def test_commands_preserve_case_spaces_newlines_and_real_at(self):
        segments = [{'type':'text','data':{'text':'/timing-task -d 08-00-00 Hello World\nHappy Birthday '}}, {'type':'at','data':{'qq':'789'}}, {'type':'text','data':{'text':'  End'}}]
        await self.f.handle(self.ctx('/timing-task -d 08-00-00 Hello World\nHappy Birthday   End',segments=segments))
        task = self.f.store.list()[0]
        self.assertEqual(task['segments'][1], segments[1])
        self.assertEqual(body_text(task['segments']), 'Hello World\nHappy Birthday [CQ:at,qq=789]  End')
        self.assertFalse(task['owner_grant'])
        await self.f.handle(self.ctx('/timing-task-AI -d 09-00-00 Write Docker Guide with Spaces\nSecond Line'))
        self.assertEqual(self.f.store.list(kind='ai')[0]['prompt'], 'Write Docker Guide with Spaces\nSecond Line')

    async def test_scene_and_kind_boundaries_in_commands_and_tools(self):
        group = self.create()
        fixed = self.create(kind='fixed', content='固定', scene='group:12')
        for ctx in (self.ctx(group=34), self.ctx(group=None)):
            for task in (group, fixed):
                self.assertEqual((await self.f.tool(ctx,'schedule_delete',{'task_id':task['id']}))['code'], 'task_not_found')
                self.assertEqual((await self.f.tool(ctx,'schedule_update',{'task_id':task['id'],'time':'09-00-00'}))['code'], 'task_not_found')
            self.assertEqual((await self.f.tool(ctx,'schedule_list',{}))['tasks'], [])
            await self.f.handle(self.ctx('/timing-task-AI del '+group['id'],group=ctx.group_id))
        await self.f.handle(self.ctx('/timing-task-AI del '+fixed['id']))
        await self.f.handle(self.ctx('/timing-task del '+group['id']))
        self.assertIsNotNone(self.f.store.get(fixed['id']))
        self.assertTrue((await self.f.tool(self.ctx(),'schedule_delete',{'task_id':fixed['id']}))['success'])
        rejected = await self.f.tool(self.ctx(),'schedule_create',{'scene':'group:34','flag':'-d','time':'08-00-00','prompt':'发送'})
        self.assertEqual(rejected['code'],'scene_boundary')
        self.assertIsNotNone(self.f.store.get(group['id']))
        self.assertTrue((await self.f.tool(self.ctx(qq=789),'schedule_delete',{'task_id':group['id']}))['success'])

    async def test_tools_fixed_crud_preserves_exact_text_mentions_and_direct_delivery(self):
        content = '  Hello World\n[CQ:at,qq=789]  带钥匙 '
        created = await self.f.tool(self.ctx(qq=123), 'schedule_create',
            {'kind':'fixed','flag':'-d','time':'08-00-00','content':content})
        self.assertTrue(created['success'], created)
        task = created['task']
        self.assertEqual(task['content'], content)
        self.assertFalse(task['owner_grant'])
        self.assertEqual(task['scene'], 'group:12')
        self.assertEqual(task['bot_qq'], '999')
        self.assertEqual(task['command'], '/timing-task -d 08-00-00 ' + content)
        updated = await self.f.tool(self.ctx(), 'schedule_update',
            {'task_id':task['id'],'time':'09-00-00','enabled':False})
        self.assertTrue(updated['success'], updated)
        self.assertEqual(updated['task']['segments'], task['segments'])
        self.assertFalse(updated['task']['enabled'])
        updated = await self.f.tool(self.ctx(), 'schedule_update',
            {'task_id':task['id'],'content':'新正文\n[CQ:at,qq=123]','enabled':True})
        self.assertTrue(updated['success'], updated)
        self.assertEqual(updated['task']['content'], '新正文\n[CQ:at,qq=123]')
        self.assertEqual(updated['task']['kind'], 'fixed')
        self.assertEqual(updated['task']['requester'], '456')
        self.assertFalse(updated['task']['owner_grant'])
        self.ai.enabled = False
        self.ai.dsh.run = AsyncMock(side_effect=AssertionError('固定消息不应运行AI'))
        job = self.f.store.claim(task['id'], updated['task']['next_run'])
        await self.f.execute(job)
        self.assertEqual(self.bot.reply.call_args.args[1], updated['task']['segments'])
        self.ai.dsh.run.assert_not_awaited()
        self.assertTrue((await self.f.tool(self.ctx(), 'schedule_delete', {'task_id':task['id']}))['success'])
        self.assertIsNone(self.f.store.get(task['id']))

    async def test_tools_list_both_kinds_filter_and_private_scope(self):
        fixed = (await self.f.tool(self.ctx(), 'schedule_create',
            {'kind':'fixed','flag':'-d','time':'08-00-00','content':'固定'}))['task']
        ai = (await self.f.tool(self.ctx(), 'schedule_create',
            {'flag':'-d','time':'08-00-00','prompt':'核实新闻','delivery':'message'}))['task']
        self.assertEqual(ai['kind'], 'ai')  # Existing callers may omit kind.
        private = (await self.f.tool(self.ctx(group=None), 'schedule_create',
            {'kind':'fixed','flag':'-d','time':'08-00-00','content':'提醒 [CQ:at,qq=789]'}))['task']
        self.assertEqual(private['scene'], 'private:456')
        self.assertEqual(private['content'], '提醒 @789')
        self.assertTrue(all(s['type'] == 'text' for s in private['segments']))
        listed = await self.f.tool(self.ctx(), 'schedule_list', {})
        self.assertEqual({t['id'] for t in listed['tasks']}, {fixed['id'], ai['id']})
        for kind, identifier in (('fixed', fixed['id']), ('ai', ai['id'])):
            listed = await self.f.tool(self.ctx(), 'schedule_list', {'kind':kind})
            self.assertEqual([t['id'] for t in listed['tasks']], [identifier])
        for ctx in (self.ctx(group=34), self.ctx(group=None, qq=789), self.ctx()):
            self.assertFalse((await self.f.tool(ctx, 'schedule_delete', {'task_id':private['id']}))['success'])
        updated = await self.f.tool(self.ctx(group=None), 'schedule_update',
            {'task_id':private['id'],'content':'更新 [CQ:at,qq=123]'})
        self.assertEqual(updated['task']['content'], '更新 @123')
        self.assertTrue((await self.f.tool(self.ctx(group=None), 'schedule_delete', {'task_id':private['id']}))['success'])

    async def test_tools_reject_wrong_kind_fields_and_identity_overrides_without_mutation(self):
        fixed = self.create(kind='fixed', content='固定')
        ai = self.create()
        for args in ({'kind':'invalid','content':'x'}, {'kind':'fixed','prompt':'x'},
                     {'kind':'fixed','content':'x','delivery':'silent'}, {'kind':'fixed'},
                     {'kind':'ai','content':'x'}, {'kind':'ai'}):
            result = await self.f.tool(self.ctx(), 'schedule_create', {'flag':'-d','time':'08-00-00',**args})
            self.assertEqual(result['code'], 'invalid_task', result)
        for task, args in ((fixed, {'prompt':'忽略'}), (fixed, {'delivery':'silent'}),
                           (fixed, {'content':' '}), (ai, {'content':'忽略'})):
            result = await self.f.tool(self.ctx(), 'schedule_update', {'task_id':task['id'],**args})
            self.assertEqual(result['code'], 'invalid_task', result)
            self.assertEqual(self.f.store.get(task['id'])['revision'], task['revision'])
        for kind in ('all', '', None):
            self.assertEqual((await self.f.tool(self.ctx(), 'schedule_list', {'kind':kind}))['code'], 'invalid_task')
        for field, value in (('scene','group:34'), ('requester','123'), ('owner_grant',True)):
            result = await self.f.tool(self.ctx(), 'schedule_create',
                {'kind':'fixed','flag':'-d','time':'08-00-00','content':'固定',field:value})
            self.assertEqual(result['code'], 'scene_boundary')
        self.assertEqual(len(self.f.store.list()), 2)

    async def test_editing_owner_task_revokes_owner_grant(self):
        task = self.create(requester='123',owner_grant=True)
        updated = await self.f.tool(self.ctx(),'schedule_update',{'task_id':task['id'],'prompt':'新提示词'})
        self.assertTrue(updated['success'])
        self.assertFalse(updated['task']['owner_grant'])
        self.assertEqual(updated['task']['requester'],'456')

    async def test_lunar_commands_tools_and_list_display(self):
        await self.f.handle(self.ctx('/timing-task -y-cn 08-15-00-00-00 农历生日 Hello World'))
        task = self.f.store.list()[0]
        self.assertEqual(task['flag'], '-y-cn')
        self.assertEqual(body_text(task['segments']), '农历生日 Hello World')
        self.assertIn('对应农历', self.bot.reply.call_args.args[1])
        await self.f.handle(self.ctx('/timing-task list'))
        self.assertIn('对应农历', json.dumps(self.bot.call_action.call_args.args[1], ensure_ascii=False))
        await self.f.handle(self.ctx('/timing-task-AI -m-cn 01-08-00-00 初一祝福'))
        self.assertEqual(self.f.store.list(kind='ai')[0]['flag'], '-m-cn')
        created = await self.f.tool(self.ctx(group=None), 'schedule_create',
            {'kind':'fixed','flag':'-o-cn','time':'2099-01-01-00-00-00','content':'新春 [CQ:at,qq=789]'})
        self.assertTrue(created['success'], created)
        self.assertEqual(created['task']['calendar'], 'lunar')
        self.assertIn('正月初一', created['task']['next_run_lunar_text'])
        updated = await self.f.tool(self.ctx(), 'schedule_update',
            {'task_id':task['id'],'flag':'-y','time':'08-15-00-00-00'})
        self.assertEqual(updated['task']['calendar'], 'solar')
        self.assertEqual(updated['task']['next_run_lunar_text'], '')
        restored = await self.f.tool(self.ctx(), 'schedule_update', {'task_id':task['id'],'flag':'-y-cn'})
        self.assertEqual(restored['task']['calendar'], 'lunar')

    async def test_admin_lunar_create_edit_and_validation(self):
        app = FastAPI()
        TimingAdmin(self.f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/timing/tasks', json={
                'kind':'fixed','scene':'group:12','flag':'-y-cn','time':'08-15-00-00-00','content':'生日快乐'})
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            self.assertEqual(task['calendar'], 'lunar')
            self.assertIn('八月十五', task['next_run_lunar_text'])
            response = await client.put('/api/timing/tasks/'+task['id'], json={
                'revision':task['revision'],'flag':'-m-cn','time':'01-08-00-00'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['content'], '生日快乐')
            listed = (await client.get('/api/timing/tasks')).json()['tasks'][0]
            self.assertEqual(listed['flag'], '-m-cn')
            self.assertIn('初一', listed['next_run_lunar_text'])
            response = await client.post('/api/timing/tasks', json={
                'kind':'ai','scene':'group:12','flag':'-o-cn','time':'2026-02-30-00-00-00','prompt':'新闻'})
            self.assertEqual(response.status_code, 400)

    async def test_forward_lists_paginate_99_nodes(self):
        for n in range(101):
            self.create(prompt='Task '+str(n))
        await self.f.handle(self.ctx('/timing-task-AI list'))
        self.assertEqual([len(c.args[1]['messages']) for c in self.bot.call_action.call_args_list], [99,2])
        self.assertIn('/timing-task-AI -d 08-00-00 Task',json.dumps(self.bot.call_action.call_args_list[0].args[1],ensure_ascii=False))

    async def test_claim_is_durable_nonoverlapping_and_recovers_unknown(self):
        task = self.create()
        due = task['next_run']
        claimed = self.f.store.claim(task['id'], due + 86400 * 3)
        self.assertIsNotNone(claimed)
        other = TaskStore(self.f.store.path)
        self.assertIsNone(other.claim(task['id'], due + 86400 * 4))
        self.assertGreater(other.get(task['id'])['next_run'], due + 86400 * 3)
        other.recover()
        self.assertEqual(other.list()[0]['last_run']['status'],'unknown')
        self.assertIsNone(other.claim(task['id'], due + 86400 * 3))

    async def test_fixed_delivery_works_while_ai_disabled_and_keeps_at(self):
        task = self.create(kind='fixed',content='Hello [CQ:at,qq=789]')
        job = self.f.store.claim(task['id'], task['next_run'])
        self.ai.enabled = False
        await self.f.execute(job)
        self.assertEqual(self.bot.reply.call_args.args[1][1], {'type':'at','data':{'qq':'789'}})
        self.assertEqual(self.f.store.list()[0]['last_run']['status'],'completed')

    async def test_unknown_send_no_repeat_and_private_mentions_are_text(self):
        task = self.create(kind='fixed',content='Hello [CQ:at,qq=789]',scene='private:456')
        self.assertTrue(all(s['type']=='text' for s in task['segments']))
        self.bot.reply.side_effect = TimeoutError()
        job = self.f.store.claim(task['id'],task['next_run'])
        await self.f.execute(job)
        self.assertEqual(self.f.store.list()[0]['last_run']['status'],'unknown')
        self.assertEqual(self.bot.reply.await_count,1)

    async def test_running_task_cancelled_after_edit_and_other_bot_not_used(self):
        task = self.create(bot_qq='1000')
        self.assertIsNone(self.f.connection(task))
        task = self.create()
        job = self.f.store.claim(task['id'],task['next_run'])
        self.assertTrue(self.f.valid(job))
        await self.f.tool(self.ctx(),'schedule_update',{'task_id':task['id'],'enabled':False})
        self.assertFalse(self.f.valid(job))

    async def test_admin_crud_revision_and_past_validation(self):
        app = FastAPI()
        TimingAdmin(self.f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            data={'kind':'fixed','scene':'group:34','flag':'-d','time':'08-00-00','content':'Hello [CQ:at,qq=789]'}
            r=await client.post('/api/timing/tasks',json=data)
            self.assertEqual(r.status_code,200,r.text)
            task=r.json()
            self.assertEqual(task['segments'][1]['type'],'at')
            r=await client.put('/api/timing/tasks/'+task['id'],json={'revision':1,'content':'新的内容','enabled':False})
            self.assertEqual(r.status_code,200,r.text)
            self.assertFalse(r.json()['enabled'])
            self.assertEqual((await client.put('/api/timing/tasks/'+task['id'],json={'revision':1,'content':'过时编辑'})).status_code,409)
            self.assertEqual((await client.delete('/api/timing/tasks/'+task['id']+'?revision=1')).status_code,409)
            self.assertEqual((await client.delete('/api/timing/tasks/'+task['id']+'?revision=2')).status_code,200)
            r=await client.post('/api/timing/tasks',json={**data,'flag':'-o','time':'20-01-01-00-00-00'})
            self.assertEqual(r.status_code,400)
            self.assertEqual(r.json()['detail'],PAST)
            r=await client.post('/api/timing/tasks',json={**data,'kind':'ai','prompt':'维护','owner_grant':True,'requester':'456'})
            self.assertEqual(r.status_code,400)

    async def test_scheduled_prompt_silent_and_memory_bridge(self):
        captured={}
        async def run(kind,prompt,**kw):
            captured.update(prompt=prompt,**kw)
            denied=await kw['handler']('send_message',{'message':'不应发送'})
            self.assertEqual(denied['code'],'silent_task')
            self.ai.runtime.full=AsyncMock(return_value={'processed':3})
            r=await kw['handler']('integrate_memory',{'batches':1})
            self.assertEqual(r['processed'],3)
            return {'finish_reason':'completed','final_response':'维护完成'}
        self.ai.dsh.run=run
        task=self.create(prompt='维护记忆',delivery='silent')
        job=self.f.store.claim(task['id'],task['next_run'])
        await self.f.execute(job)
        self.assertTrue(captured['prompt'].startswith('[定时任务]'))
        self.assertEqual(captured['role'],'scheduled')
        self.assertFalse(captured['owner'])
        self.bot.reply.assert_not_awaited()
        handler=captured['handler']
        r=await handler('integrate_memory',{'batches':1})
        self.assertEqual(r['code'],'cancelled')

    async def test_admin_auth_protects_all_timing_routes(self):
        from src.admin import create_admin_app
        from src.feature.atom import FeaturePlugin
        from src.feature.manager import FeatureManager
        from src.master import MasterService
        root = Path(self.temp.name)
        manager = FeatureManager(root / 'features.json', [self.f])
        with patch('src.admin.AdminAuth', return_value=SimpleNamespace(validate=lambda cookie: False)):
            app = create_admin_app(manager, MasterService(root / 'master.json'), [FeaturePlugin(self.f, TimingAdmin(self.f))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for method, path in [('get','/api/timing/tasks'),('post','/api/timing/tasks'),('put','/api/timing/tasks/unknown'),('delete','/api/timing/tasks/unknown?revision=1')]:
                self.assertEqual((await getattr(client, method)(path)).status_code,401)

    async def test_completed_once_stays_disabled_and_cannot_reenable_in_past(self):
        for flag in ('-o', '-o-cn'):
            with self.subTest(flag=flag):
                task = self.create(flag=flag,time='2099-01-01-00-00-00')
                claimed = self.f.store.claim(task['id'],task['next_run'])
                self.f.store.finish(claimed['run_id'], 'completed')
                self.assertFalse(self.f.store.get(task['id'])['enabled'])
                self.assertIsNone(self.f.store.claim(task['id'],task['next_run']+1))
                current = self.f.store.get(task['id'])
                data = {k:v for k,v in current.items() if k not in {'id','revision','next_run','created','updated'}}
                saved = self.f.store.save(data,identifier=task['id'],expected=1,now=task['next_run']+1)
                self.assertFalse(saved['enabled'])
                with self.assertRaisesRegex(ValueError,PAST):
                    self.f.store.save({**data,'enabled':True},identifier=task['id'],expected=2,now=task['next_run']+2)

    async def test_ai_capacity_does_not_starve_fixed_reminders(self):
        began = asyncio.Event()
        release = asyncio.Event()
        async def long(*args):
            began.set()
            await release.wait()
            return {'finish_reason':'completed'}
        self.ai.engine.run_scheduled = long
        with patch('src.feature.timing.feature.time.time',return_value=time.time()+86400*2):
            for _ in range(4):
                self.create()
            self.create(kind='fixed',content='Fixed reminder')
            # Save computes due using the same patched clock: move once more.
        with patch('src.feature.timing.feature.time.time',return_value=time.time()+86400*4):
            await self.f.tick()
            await asyncio.wait_for(began.wait(),2)
            await asyncio.sleep(0)
            self.bot.reply.assert_awaited()
            release.set()
            await asyncio.gather(*(task for _,task in self.f.running.values()))
