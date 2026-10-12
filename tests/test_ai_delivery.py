"""QQ delivery requires explicit tools, including forced and background turns."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.feature.ai.feature import AiFeature
from src.feature.atom import MessageContext


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999', 'nickname': '大肥鱼'})
        self.bot = SimpleNamespace(reply=AsyncMock(), call_action=AsyncMock(return_value={'message_id':'sent-1'}),
                                   master_service=SimpleNamespace(is_master=lambda qq: qq == 123))

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    def ctx(self, *, private=False, text='请处理', forced=True):
        return MessageContext(bot=self.bot, websocket=None,
            event={'self_id':999, '_ai_arrival_forced':forced, 'message':[]}, msg=text,
            message_type='private' if private else 'group', user_id=123, group_id=None if private else 12)

    async def test_final_text_and_empty_outputs_never_become_qq_messages(self):
        for private, forced, completion in ((False,True,None), (True,True,None),
                                            (False,False,None), (False,True,{'result':'撤回完成'})):
            for text in ('（主人要求静默，本轮不发言。）', '', '  ', '这是一段最终回答'):
                with self.subTest(private=private, forced=forced, completion=completion, text=text):
                    self.f.dsh.run = AsyncMock(return_value={'finish_reason':'completed','final_response':text})
                    ctx = self.ctx(private=private, forced=forced)
                    await self.f.engine._turn(ctx, self.f.scene_key(ctx), forced, completion=completion)
                    self.bot.reply.assert_not_awaited()
                    self.bot.call_action.assert_not_awaited()
                    self.assertFalse(list((self.f.workspace.root / 'tmp').glob('chat-*.json')))

    async def test_explicit_text_and_forward_tools_still_send_without_filtering(self):
        # Parentheses are legitimate text; no keyword heuristic should reject it.
        visible = '（这是用户要求的括号内容。）'
        async def run(kind, prompt, **kwargs):
            sent = await kwargs['handler']('send_message', {'message':visible})
            forwarded = await kwargs['handler']('send_forward_message', {'messages':['第一条','第二条']})
            self.assertTrue(sent['success'])
            self.assertTrue(forwarded['success'])
            return {'finish_reason':'completed','final_response':'内部记录：发送完成，不要转述'}
        self.f.dsh.run = run
        await self.f.engine._turn(self.ctx(), 'group:12', True)
        self.bot.reply.assert_awaited_once()
        self.assertEqual(self.bot.reply.await_args.args[1], visible)
        self.bot.call_action.assert_awaited_once()
        self.assertEqual(self.bot.call_action.await_args.args[0], 'send_group_forward_msg')

    async def test_spawn_and_its_delivery_turn_can_both_remain_silent(self):
        child = None
        async def run(kind, prompt, **kwargs):
            nonlocal child
            if not kwargs['delivery']:
                child = await kwargs['spawn']('internal-task-uuid', '静默检查')
                return {'finish_reason':'completed','final_response':'（后台处理中，不发群消息。）'}
            data = json.loads(prompt)
            self.assertEqual(data['background_result']['task_id'], 'internal-task-uuid')
            self.assertFalse(data['owner'])
            return {'finish_reason':'completed','final_response':'（撤回回执 success=true，本轮保持沉默。）'}
        self.f.dsh.run = run
        # Drain the actual completion queue explicitly for deterministic assertions.
        self.f.engine._start_worker = lambda: None
        await self.f.engine._turn(self.ctx(), 'group:12', True)
        self.bot.reply.assert_not_awaited()
        self.assertIsNotNone(child)
        self.assertIn('internal-task-uuid', self.f.engine.background)
        await child['finish']({'status':'completed','result':'内部检查结果'})
        await self.f.engine._run()
        self.bot.reply.assert_not_awaited()
        self.bot.call_action.assert_not_awaited()
        self.assertFalse(self.f.engine.background)
        self.assertFalse(list((self.f.workspace.root / 'tmp').glob('worker-*.json')))

    async def test_only_real_explicit_request_failures_receive_fixed_error_notice(self):
        for private, forced, completion, attempted, expected in (
            (False,True,None,False,True), (True,True,None,False,True),
            (False,False,None,False,False), (False,True,{'result':'内部笔记'},False,False),
            (False,True,None,True,False)):
            with self.subTest(private=private, forced=forced, completion=completion, attempted=attempted):
                self.bot.reply.reset_mock()
                async def run(kind, prompt, **kwargs):
                    if attempted:
                        # An unknown send must never lead to a second fallback send.
                        self.bot.reply.side_effect = TimeoutError()
                        await kwargs['handler']('send_message', {'message':'明确回复'})
                    raise RuntimeError('内部异常细节不得发送')
                self.f.dsh.run = run
                self.bot.reply.side_effect = None
                await self.f.engine.pending.put((self.ctx(private=private, forced=forced), forced, completion))
                with self.assertLogs('src.feature.ai.engine', level='ERROR'):
                    await self.f.engine._run()
                self.assertEqual(self.bot.reply.await_count, int(expected or attempted))
                if expected:
                    self.assertEqual(self.bot.reply.await_args.args[1], '这次处理失败了，请稍后再试。')
                elif not attempted:
                    self.bot.reply.assert_not_awaited()

    async def test_disabled_or_cancelled_failed_turns_do_not_emit_error_notice(self):
        for mode in ('disabled', 'cancelled'):
            with self.subTest(mode=mode):
                self.f.enabled = True
                async def run(kind, prompt, **kwargs):
                    if mode == 'disabled':
                        self.f.enabled = False
                    else:
                        self.f.engine.current['valid'] = False
                    raise RuntimeError('不再有效的回合')
                self.f.dsh.run = run
                await self.f.engine.pending.put((self.ctx(), True, None))
                with self.assertLogs('src.feature.ai.engine', level='ERROR'):
                    await self.f.engine._run()
                self.bot.reply.assert_not_awaited()

    async def test_at_nickname_and_private_still_trigger_processing(self):
        cases = [
            {'message_type':'group','group_id':12,'message':[{'type':'at','data':{'qq':'999'}}]},
            {'message_type':'group','group_id':12,'message':[{'type':'text','data':{'text':'大肥鱼，先别说话'}}]},
            {'message_type':'private','message':[{'type':'text','data':{'text':'安静处理'}}]},
        ]
        self.f.dsh.run = AsyncMock(return_value={'finish_reason':'completed','final_response':'（不回复。）'})
        for event in cases:
            event.update(post_type='message', self_id=999, user_id=123)
            self.assertTrue(self.f.engine.note_arrival(event))
            self.assertTrue(event['_ai_arrival_forced'])
            ctx = self.ctx(private=event['message_type']=='private')
            ctx.event = event
            await self.f.engine.handle(ctx)
            await asyncio.gather(*self.f.engine.tasks.values())
        self.assertEqual(self.f.dsh.run.await_count, 3)
        self.bot.reply.assert_not_awaited()
