import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.feature.ai.feature import AiFeature
from src.feature.ai.forward import send_forward_message
from src.feature.atom.context import MessageContext
from src.onebot_error import OneBotError


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'bot_qq': '999', 'nickname': '机器人', 'vector_index_enabled': False, 'memory_auto_learn': False})
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={'message_id': 456}), reply=AsyncMock())
        self.ctx = MessageContext(bot=self.bot, event={'self_id': 999}, websocket=None, msg='用聊天记录回复', message_type='group', user_id=123, group_id=12)

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_group_and_private_identity_text_and_record(self):
        for scene in ('group', 'private'):
            self.ctx.message_type = scene
            result = await send_forward_message(self.f, self.ctx, ['第一段', '[CQ:at,qq=all]'])
            self.assertTrue(result['success'])
            action, params, _ = self.bot.call_action.await_args.args
            self.assertEqual(action, f'send_{scene}_forward_msg')
            self.assertEqual(params['group_id' if scene == 'group' else 'user_id'], '12' if scene == 'group' else '123')
            node = params['messages'][1]['data']
            self.assertEqual(node['uin'], '999')
            self.assertEqual(node['name'], '机器人')
            self.assertEqual(node['content'], [{'type': 'text', 'data': {'text': '[CQ:at,qq=all]'}}])
            record = self.f.context.recent(f'{scene}:12' if scene == 'group' else 'private:123', 1)[0]
            self.assertTrue(record['is_self'])
            self.assertEqual(record['id'], '456')

    async def test_invalid_payload_never_sends(self):
        for value in (None, 'text', [], [' '], [{}], ['x'] * 21, ['x' * 4001], ['x' * 4000] * 6):
            with self.assertRaises(ValueError):
                await send_forward_message(self.f, self.ctx, value)
        self.bot.call_action.assert_not_awaited()

    async def test_failed_and_unknown_not_recorded(self):
        for error, code in ((OneBotError('send_group_forward_msg', {'retcode': 1200, 'message': '权限不足'}), 'permission_denied'), (TimeoutError(), 'unknown')):
            self.bot.call_action.side_effect = error
            result = await send_forward_message(self.f, self.ctx, ['内容'])
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], code)
        self.assertEqual(self.f.context.recent('group:12', 10), [])

    async def test_agent_forward_satisfies_direct_call_and_limits_retries(self):
        call = {'id': '1', 'type': 'function', 'function': {'name': 'send_forward_message', 'arguments': json.dumps({'messages': ['段落一', '段落二']})}}
        self.f.client.turn = AsyncMock(side_effect=[{'role': 'assistant', 'tool_calls': [call, dict(call, id='2')]}, {'role': 'assistant', 'content': ''}])
        self.f.engine.revisions['group:12'] = 1
        await self.f.engine._turn(self.ctx, 'group:12', True, 1)
        self.bot.call_action.assert_awaited_once()
        self.bot.reply.assert_not_awaited()
        self.assertEqual(self.f.client.turn.await_count, 2)
        self.assertTrue(self.f.engine._did_send['group:12'])

    async def test_real_action_dispatch_waits_for_ack(self):
        from src.bot import QQBot
        bot = QQBot(SimpleNamespace(observe_system=AsyncMock()), None)
        class Socket:
            async def send_text(inner, text):
                payload = json.loads(text)
                self.assertIn(payload['action'], {'send_group_forward_msg', 'send_private_forward_msg'})
                bot._responses[payload['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': 789}})
        self.ctx.bot = bot
        self.ctx.websocket = Socket()
        for scene in ('group', 'private'):
            self.ctx.message_type = scene
            result = await send_forward_message(self.f, self.ctx, ['测试正文'])
            self.assertEqual(result['message_id'], 789)
            self.assertEqual(bot._responses, {})
