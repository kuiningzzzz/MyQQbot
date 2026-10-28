"""Continuous life, sleep admission, consent, private observability and recovery."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
from src.admin import create_admin_app
from src.admin_auth import AdminAuth, COOKIE_NAME
from src.feature.ai.admin import AiAdmin
from src.feature.ai.autonomy import AutonomyRuntime, STATUS
from src.feature.ai.feature import AiFeature
from src.feature.atom import FeaturePlugin, MessageContext
from src.feature.manager import FeatureManager
from src.master import MasterService


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.f = AiFeature(self.root/'ai')
        self.f.config.save({'vector_index_enabled':False,'memory_auto_learn':False,'bot_qq':'999'})
        self.life = self.f.autonomy
        self.master = MasterService(self.root/'master.json')
        self.master.master_id = 123
        self.bot = SimpleNamespace(reply=AsyncMock(),master_service=self.master,connected_clients={'ws'},can_send_conversation=lambda:True)
        self.life.bot = self.bot
        self.life.running = True  # State tests never start the driver/cloud.
        self.f.engine._start_worker = lambda:None

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    def ctx(self, text='普通发言', qq=456, group=12, mention=False):
        return MessageContext(bot=self.bot,websocket='ws',msg=text,raw_msg=text,user_id=qq,group_id=group,
            message_type='group' if group else 'private',event={'post_type':'message','message_type':'group' if group else 'private',
                'user_id':qq,'group_id':group,'self_id':999,'message_id':str(time.time_ns()),
                'message':[{'type':'at','data':{'qq':'999'}}] if mention else [{'type':'text','data':{'text':text}}]})

    async def test_awake_observes_every_group_without_focus_window(self):
        c = self.ctx()
        self.assertTrue(self.f.engine.note_arrival(c.event))
        self.assertFalse(c.event['_ai_arrival_forced'])
        self.assertEqual(self.f.engine.active,{})
        await self.f.engine.handle(c)
        self.assertEqual(self.f.engine.pending.qsize(),1)

    async def test_normal_sleep_wake_filters_and_deep_sleep_archive_without_inference(self):
        self.life.sleep(3600,wake_on=['mention_self'])
        private = self.ctx(group=None)
        await self.f.observe(private)
        self.assertTrue(await self.f.engine.handle(private))
        self.assertEqual(self.life.state['mode'],'sleeping')
        self.assertTrue(self.f.engine.pending.empty())
        called = self.ctx(mention=True)
        await self.f.observe(called)
        await self.f.engine.handle(called)
        self.assertEqual(self.life.state['mode'],'awake')
        await self.f.engine.pending.get()
        self.life.sleep(3600,deep=True)
        for c in [self.ctx(group=None),self.ctx(mention=True),self.ctx(qq=123)]:
            await self.f.observe(c)
            await self.f.engine.handle(c)
        self.life.notify('task_completed')
        self.assertEqual(self.life.state['mode'],'deep_sleep')
        self.assertTrue(self.f.engine.pending.empty())
        self.assertEqual(self.f.journal.pending_count(),5)

    async def test_status_is_exact_and_only_owner_forces_wake(self):
        for mode in ['awake','sleeping','deep_sleep']:
            self.life.wake(force=True)
            if mode != 'awake': self.life.sleep(3600,deep=mode=='deep_sleep')
            await self.life.command(self.ctx('/ai-status'))
            self.assertEqual(self.bot.reply.await_args.args[1],STATUS[mode])
        await self.life.command(self.ctx('/ai-wakeup'))
        self.assertEqual(self.life.state['mode'],'deep_sleep')
        await self.life.command(self.ctx('/ai-wakeup',qq=123))
        self.assertEqual(self.life.state['mode'],'awake')
        self.assertEqual(self.bot.reply.await_args.args[1],STATUS['awake'])

    async def test_wake_filters_count_only_selected_scene_and_sender_and_fence_pending(self):
        filters={'scenes':['group:12'],'users':['456'],'message_threshold':2}
        self.life.sleep(3600,wake_on=['group_message'],filters=filters)
        self.assertFalse(self.life.receive(self.ctx(group=13)))
        self.assertFalse(self.life.receive(self.ctx(qq=789)))
        self.assertEqual(self.life.state['observed_count'],0)
        self.assertFalse(self.life.receive(self.ctx()))
        self.assertTrue(self.life.receive(self.ctx()))
        self.assertEqual(self.life.state['mode'],'awake')
        c=self.ctx(group=None)
        await self.f.engine.pending.put((c,True,None))
        result=self.life.sleep(3600,wake_on=['private_message'])
        self.assertEqual(result['code'],'observations_pending')
        result=self.life.sleep(3600,wake_on=['private_message'],filters={'users':['789']})
        self.assertTrue(result['success'])
        with self.assertRaises(ValueError):self.life.sleep(30,filters=[])

    async def test_active_batch_keeps_sources_and_cannot_inherit_owner_from_last_message(self):
        first,last,other=self.ctx(qq=456),self.ctx(qq=123),self.ctx(group=13)
        for c in [first,last,other]:
            self.f.engine.note_arrival(c.event)
            await self.f.engine.pending.put((c,c is last,None))
        ctx,forced,completion=await self.f.engine._next_turn()
        self.assertIs(ctx,last)
        self.assertTrue(forced)
        self.assertEqual([c.user_id for c,_ in ctx.event['_ai_batch_contexts']],[456,123])
        self.assertIs(self.f.engine.deferred[0],other)
        async def run(*args,handler,**kwargs):
            with self.assertRaises(PermissionError):await handler('__source',{})
            ordinary=await handler('__source',{'source_message_id':self.f.engine.input_id(first)})
            owner=await handler('__source',{'source_message_id':self.f.engine.input_id(last)})
            self.assertFalse(ordinary['owner'])
            self.assertTrue(owner['owner'])
            return {'finish_reason':'completed'}
        self.f.dsh.run=run
        await self.f.engine._turn(ctx,'group:12',forced,completion=completion)

    async def test_sleep_resumes_after_restart_and_merges_actual_four_plus_two_hours(self):
        clock=1000
        with patch('src.feature.ai.autonomy.time',SimpleNamespace(time=lambda:clock)):
            first=self.life.sleep(4*3600,wake_on=[])
            clock+=3600
            restored=AutonomyRuntime(self.f)
            self.assertEqual(restored.status()['mode'],'sleeping')
            self.assertEqual(restored.state['until']-clock,3*3600)
            clock+=3*3600
            self.assertEqual(self.life.status()['mode'],'awake')
            second=self.life.sleep(2*3600,wake_on=[])
            self.assertEqual(first['sleep_chain_id'],second['sleep_chain_id'])
            clock+=2*3600
            state=self.life.status()
            self.assertEqual(state['sleep_chain']['seconds'],6*3600)
            entries=self.f.activity.read(kind='sleep_summary')
            self.assertEqual(len(entries),1)
            self.assertEqual(entries[0]['data']['seconds'],6*3600)
            self.life.activity('write')
            third=self.life.sleep(60)
            self.assertNotEqual(first['sleep_chain_id'],third['sleep_chain_id'])

    async def test_interruption_counts_actual_time_and_old_sleep_cannot_override_force_wake(self):
        clock=1000
        with patch('src.feature.ai.autonomy.time',SimpleNamespace(time=lambda:clock)):
            revision=self.life.state['revision']
            self.life.sleep(4*3600)
            clock+=3600
            self.life.wake('owner',force=True)
            result=self.life.sleep(3600,revision=revision)
            self.assertEqual(result['code'],'sleep_interrupted')
            self.life.sleep(2*3600)
            clock+=2*3600
            self.assertEqual(self.life.status()['sleep_chain']['seconds'],3*3600)

    async def test_contact_policy_scope_and_beijing_windows(self):
        c=self.ctx(group=None)
        self.life.contact('set',policy={'start':'09:00','end':'23:00'},ctx=c)
        def stamp(hour,minute=0):return datetime(2026,10,28,hour,minute,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.assertFalse(self.life.permitted('private:456',stamp(8,59)))
        self.assertTrue(self.life.permitted('private:456',stamp(9)))
        self.assertFalse(self.life.permitted('private:456',stamp(23)))
        with self.assertRaises(PermissionError):self.life.contact('set','private:123',{},ctx=c)
        with self.assertRaises(PermissionError):self.life.contact('set','group:99',{},ctx=self.ctx())
        self.life.contact('set','group:12',{'start':'23:00','end':'09:00'},admin=True)
        self.assertTrue(self.life.permitted('group:12',stamp(2)))
        self.assertFalse(self.life.permitted('group:12',stamp(12)))
        auto=self.life.context('private:456')
        with self.assertRaises(PermissionError):self.life.contact('set','private:456',{},ctx=auto)
        self.life.contact('delete','private:456',ctx=auto)
        self.assertNotIn('private:456',self.life.contacts)

    async def test_driver_continues_without_messages_then_parks_and_owner_resumes(self):
        # Restore the real worker; the fake turn substitutes only inference.
        from src.feature.ai.engine import ChatEngine
        self.f.engine._start_worker = ChatEngine._start_worker.__get__(self.f.engine)
        seen=[]
        async def turn(ctx,key,forced,**kwargs):
            self.assertTrue(ctx.event['_ai_autonomous'])
            self.assertFalse(forced)
            seen.append(key)
            if len(seen)>=2:self.life.sleep(3600,deep=True)
        self.f.engine._turn=turn
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-autonomy-key'}):
            await self.life.start(self.bot)
            async with asyncio.timeout(3):
                while len(seen)<2:await asyncio.sleep(.01)
            await asyncio.sleep(.12)
            self.assertEqual(len(seen),2)
            await self.life.command(self.ctx('/ai-wakeup',qq=123))
            async with asyncio.timeout(3):
                while len(seen)<3:await asyncio.sleep(.01)
            await self.life.stop()

    async def test_autonomous_spawn_context_has_no_fabricated_human_requester(self):
        async def run(*args,spawn,**kwargs):
            child=await spawn('self-project','主动制作项目')
            self.assertFalse(child['owner'])
            self.assertIsNone(child['context']['requester_qq'])
            self.assertTrue(child['context']['self_initiated'])
            self.assertNotIn('observed_event',child['context'])
            await child['finish']({'finish_reason':'completed','final_response':'已制作'})
            return {'finish_reason':'completed'}
        self.f.dsh.run=run
        await self.f.engine._turn(self.life.context('private:123'),'private:123',False)
        self.assertFalse(self.f.engine.background)
        completion=await self.f.engine.pending.get()
        self.assertTrue(completion[0].event['_ai_autonomous'])
        self.assertEqual(completion[2]['title'],'主动制作项目')

    async def test_private_activity_endpoints_require_admin_and_redact_credentials(self):
        auth=AdminAuth(self.root/'admin-auth.json')
        manager=FeatureManager(self.root/'features.json',[self.f])
        with patch('src.admin.AdminAuth',return_value=auth):
            app=create_admin_app(manager,self.master,[FeaturePlugin(self.f,AiAdmin(self.f))])
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-private-key'}):
            self.f.activity.add('assistant/message',{'text':'秘密 test-private-key'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/ai/activity')).status_code,401)
            client.cookies.set(COOKIE_NAME,auth.issue())
            response=await client.get('/api/ai/activity')
            self.assertEqual(response.status_code,200)
            self.assertNotIn('test-private-key',response.text)
            self.assertIn('[REDACTED]',response.text)
            self.assertEqual((await client.post('/api/ai/life/contacts',json={'action':'set','scene':'private:456','policy':{'start':'09:00','end':'23:00'}})).status_code,200)
            self.life.sleep(100,deep=True)
            self.assertEqual((await client.post('/api/ai/life/wakeup')).json()['mode'],'awake')

    async def test_admin_sleep_fences_live_driver_before_cancelling_current_turn(self):
        from src.feature.ai.engine import ChatEngine
        self.f.engine._start_worker=ChatEngine._start_worker.__get__(self.f.engine)
        started=[]
        async def turn(*args,**kwargs):
            started.append(True)
            await asyncio.Event().wait()
        self.f.engine._turn=turn
        app=httpx.ASGITransport(app=self.admin_app())
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-autonomy-key'}):
            await self.life.start(self.bot)
            async with asyncio.timeout(3):
                while not started:await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=app,base_url='http://test') as client:
                client.cookies.set(COOKIE_NAME,self.admin_auth.issue())
                self.assertEqual((await client.post('/api/ai/life/sleep',json={'duration_seconds':100,'deep':True})).status_code,200)
                await asyncio.sleep(.15)
                self.assertEqual(len(started),1)
                self.assertFalse(self.life.may_run())
                self.assertFalse(self.life.suspended)
                self.assertEqual((await client.post('/api/ai/life/sleep',json={'duration_seconds':-1})).status_code,400)
                self.assertFalse(self.life.suspended)
                await client.post('/api/ai/life/wakeup')
                async with asyncio.timeout(3):
                    while len(started)<2:await asyncio.sleep(.01)
            await self.life.stop()

    def admin_app(self):
        self.admin_auth=AdminAuth(self.root/'test-admin-auth.json')
        manager=FeatureManager(self.root/'test-features.json',[self.f])
        with patch('src.admin.AdminAuth',return_value=self.admin_auth):
            return create_admin_app(manager,self.master,[FeaturePlugin(self.f,AiAdmin(self.f))])


if __name__=='__main__':unittest.main()
