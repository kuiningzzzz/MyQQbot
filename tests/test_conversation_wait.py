"""Standby must preserve natural follow-ups without polling the model."""
import asyncio
import os
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src.feature.ai.autonomy import AutonomyRuntime, STATUS
from src.feature.ai.engine import ChatEngine
from tests import test_autonomy as fixtures


class ConversationWaitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AutonomyTests.asyncSetUp
    asyncTearDown = fixtures.AutonomyTests.asyncTearDown
    ctx = fixtures.AutonomyTests.ctx

    @contextmanager
    def clock(self):
        value=SimpleNamespace(now=time.time())
        clock=SimpleNamespace(time=lambda:value.now)
        with patch('src.feature.ai.autonomy.time',clock), patch('src.feature.ai.conversation_wait.time',clock):
            yield value

    def task(self,ctx):
        return {'id':'chat-test','valid':True,'inputs':{ctx.event['message_id']:(ctx,False)},
                'attempts':{},'sent':0,'unknown':set()}

    async def reply(self,ctx):
        task=self.task(ctx)
        handler=self.f.engine.make_handler(ctx,self.f.scene_key(ctx),False,task,lambda:True)
        result=await handler('send_message',{'message':'可以这样做。'})
        self.assertTrue(result['success'])
        self.life.chat.settle(task)
        return task

    async def test_followup_wakes_without_address_and_silent_observation_does_not_extend(self):
        with self.clock() as clock:
            await self.reply(self.ctx())
            deadline=clock.now+90
            self.assertEqual(self.life.status()['mode'],'waiting')
            self.assertEqual(self.life.status()['text'],STATUS['awake'])
            clock.now+=10
            c=self.ctx('然后呢？')
            self.f.engine.note_arrival(c.event)
            self.assertFalse(c.event['_ai_arrival_forced'])
            self.assertTrue(self.life.receive(c))
            self.assertTrue(c.event['_ai_conversation_followup'])
            self.life.chat.settle(self.task(c))  # Model chose silence.
            self.assertEqual(self.life.state['mode'],'waiting')
            self.assertEqual(self.life.state['until'],deadline)
            clock.now=deadline
            self.assertEqual(self.life.status()['mode'],'awake')
            self.assertEqual(self.life.context().event['_ai_wait_resume']['reason'],'conversation_wait_expired')
            self.assertNotIn('sleep_chain',self.life.state)

    async def test_group_scene_participants_join_grace_and_real_quote_boundaries(self):
        with self.clock() as clock:
            original=self.ctx()
            await self.reply(original)
            self.assertFalse(self.life.chat.accepts(self.ctx(group=13)))
            self.assertTrue(self.life.chat.accepts(self.ctx(qq=789)))
            clock.now+=21
            self.assertFalse(self.life.chat.accepts(self.ctx(qq=789)))
            self.assertTrue(self.life.chat.accepts(self.ctx()))
            addressed=self.ctx()
            addressed.event['message']=[{'type':'at','data':{'qq':'789'}}]
            self.f.engine.note_arrival(addressed.event)
            self.assertFalse(self.life.chat.accepts(addressed))
            quote=self.ctx(qq=789)
            quote.event['message']=[{'type':'reply','data':{'id':original.event['message_id']}}]
            self.assertTrue(self.life.chat.accepts(quote))
            quote.event['message'][0]['data']['id']='unrelated-message'
            self.assertFalse(self.life.chat.accepts(quote))
            called=self.ctx(group=13,mention=True)
            self.f.engine.note_arrival(called.event)
            self.assertTrue(self.life.chat.accepts(called))
            self.assertTrue(self.life.chat.accepts(self.ctx(group=None,qq=789)))

    async def test_private_wait_is_longer_and_restart_keeps_remaining_deadline(self):
        with self.clock() as clock:
            await self.reply(self.ctx(group=None))
            deadline=clock.now+180
            clock.now+=45
            restored=AutonomyRuntime(self.f)
            self.assertEqual(restored.status()['mode'],'waiting')
            self.assertEqual(restored.state['until'],deadline)
            self.assertEqual(restored.state['until']-clock.now,135)
            await restored.command(self.ctx('/ai-status'))
            self.assertEqual(self.bot.reply.await_args.args[1],STATUS['awake'])
            self.assertEqual(restored.state['until'],deadline)

    async def test_multiple_scenes_expire_separately_and_proactive_opening_accepts_first_participant(self):
        with self.clock() as clock:
            await self.reply(self.ctx())
            self.life.receive(self.ctx(group=None))
            await self.reply(self.ctx(group=None))
            clock.now+=100
            self.assertEqual(self.life.status()['mode'],'waiting')
            self.assertFalse(self.life.chat.accepts(self.ctx()))
            self.assertTrue(self.life.chat.accepts(self.ctx(group=None)))
            self.life.wake('test',force=True)
            auto=self.life.context('group:12')
            self.life.chat.replied(auto,self.task(auto))
            self.life.chat.settle(self.task(auto))
            clock.now+=21
            self.assertTrue(self.life.chat.accepts(self.ctx(qq=789)))
            self.assertFalse(self.life.chat.accepts(self.ctx(group=13,qq=789)))

    async def test_real_reply_renews_but_failed_sends_and_files_do_not_open_wait(self):
        with self.clock() as clock:
            ctx=self.ctx()
            await self.reply(ctx)
            first=self.life.state['until']
            clock.now+=10
            self.life.wake('conversation_followup')
            await self.reply(ctx)
            self.assertEqual(self.life.state['until'],first+10)
            self.life.wake('admin',force=True)
            task=self.task(ctx)
            self.bot.reply.side_effect=TimeoutError()
            handler=self.f.engine.make_handler(ctx,'group:12',False,task,lambda:True)
            self.assertEqual((await handler('send_message',{'message':'失败消息'}))['code'],'unknown')
            self.life.chat.settle(task)
            self.assertFalse(self.life.chat.leases())
            self.life.chat.replied(ctx,{**self.task(ctx),'worker':True})
            self.assertFalse(self.life.chat.leases())

    async def test_pending_followup_prevents_parking_and_explicit_rest_and_deep_win(self):
        with self.clock():
            ctx=self.ctx()
            self.life.chat.replied(ctx,self.task(ctx))
            c=self.ctx('还没说完')
            await self.f.engine.pending.put((c,False,None))
            self.assertEqual(self.life.chat.start()['code'],'observations_pending')
            self.assertEqual(self.life.state['mode'],'awake')
            self.f.engine.pending.get_nowait()
            result=self.life.sleep(3600,end_conversation=False)
            self.assertEqual(result['state'],'waiting')
            self.assertNotIn('sleep_chain_id',result)
            self.assertTrue(self.life.chat.finish()['success'])
            self.assertEqual(self.life.state['mode'],'awake')
            self.assertFalse(self.life.chat.leases())
            self.life.chat.replied(ctx,self.task(ctx))
            self.assertEqual(self.life.sleep(3600,end_conversation=True)['state'],'sleeping')
            self.assertFalse(self.life.chat.leases())
            self.life.wake('test',force=True)
            self.life.chat.replied(ctx,self.task(ctx))
            self.assertEqual(self.life.sleep(3600,deep=True,end_conversation=False)['state'],'deep_sleep')
            self.assertFalse(self.life.receive(self.ctx(mention=True)))
            self.assertEqual(self.life.chat.start()['code'],'sleeping')

    async def test_standby_is_not_counted_as_sleep_and_task_result_can_resume(self):
        with self.clock() as clock:
            self.life.sleep(100)
            clock.now+=10
            self.life.wake('owner',force=True)
            await self.reply(self.ctx())
            clock.now+=30
            self.life.notify('task_completed',scene='group:12')
            self.assertEqual(self.life.state['mode'],'awake')
            self.assertEqual(self.life.status()['sleep_chain']['seconds'],10)

    async def test_live_driver_makes_no_empty_calls_then_resumes_followup_and_timeout(self):
        self.f.engine._start_worker=ChatEngine._start_worker.__get__(self.f.engine)
        seen=[]
        async def run(kind,prompt,handler,**kwargs):
            import json
            seen.append(json.loads(prompt))
            if len(seen)==1:await handler('send_message',{'message':'第一句答复'})
            if len(seen)==3:await handler('sleep',{'duration_seconds':3600,'deep':True})
            return {'finish_reason':'completed'}
        self.f.dsh.run=run
        with self.clock() as clock, patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-wait-key'}):
            await self.f.engine.pending.put((self.ctx(mention=True),True,None))
            await self.life.start(self.bot)
            async with asyncio.timeout(3):
                while self.life.state['mode']!='waiting':await asyncio.sleep(.01)
            deadline=self.life.state['until']
            await asyncio.sleep(.15)
            self.assertEqual(len(seen),1)
            unrelated=self.ctx(group=13)
            await self.f.observe(unrelated)
            await self.f.engine.handle(unrelated)
            await asyncio.sleep(.12)
            self.assertEqual(len(seen),1)
            await self.f.engine.handle(self.ctx('那数据放哪里？'))
            async with asyncio.timeout(3):
                while len(seen)<2 or self.life.state['mode']!='waiting':await asyncio.sleep(.01)
            self.assertEqual(self.life.state['until'],deadline)
            self.assertTrue(seen[1]['observed_event']['conversation_followup'])
            await asyncio.sleep(.12)
            self.assertEqual(len(seen),2)
            clock.now=deadline+1
            self.life.signal.set()
            async with asyncio.timeout(3):
                while self.life.state['mode']!='deep_sleep':await asyncio.sleep(.01)
            self.assertEqual(len(seen),3)
            self.assertIn('conversation_wait_ended',seen[2])
            await self.life.stop()


if __name__=='__main__':unittest.main()
