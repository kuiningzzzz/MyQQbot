import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.feature.ai.feature import AiFeature
from src.feature.ai.reply import reply_message
from src.feature.atom.context import MessageContext
from src.onebot_error import OneBotError


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'bot_qq': '999', 'vector_index_enabled': False, 'memory_auto_learn': False})
        self.info = {'message_id': -42, 'message_type': 'group', 'group_id': 12, 'sender': {'user_id': 123}}
        self.bot = SimpleNamespace(call_action=AsyncMock(side_effect=self.action), reply=AsyncMock())
        self.ctx = MessageContext(bot=self.bot, event={'self_id': 999, 'message_id': -42},
            websocket='ws', msg='回复一下', message_type='group', user_id=123, group_id=12)

    async def action(self, action, params, ws):
        return self.info if action == 'get_msg' else {'message_id': 456}

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_group_private_and_own_messages_use_native_segments_and_record(self):
        for scene, own in [('group', False), ('private', False), ('private', True)]:
            with self.subTest(scene=scene, own=own):
                self.ctx.message_type = scene
                self.info.update(message_type=scene, sender={'user_id': 999 if own else 123}, target_id=123)
                output = await reply_message(self.f, self.ctx, '-42', '[CQ:at,qq=all] 正文')
                self.assertEqual(output, {'success': True, 'message_id': 456, 'reply_to_message_id': '-42'})
                action, args, ws = self.bot.call_action.await_args.args
                self.assertEqual(action, f'send_{scene}_msg')
                self.assertEqual(args['group_id' if scene == 'group' else 'user_id'], '12' if scene == 'group' else '123')
                self.assertEqual(args['message'], [{'type': 'reply', 'data': {'id': '-42'}},
                    {'type': 'text', 'data': {'text': '[CQ:at,qq=all] 正文'}}])
                record = self.f.context.recent('group:12' if scene == 'group' else 'private:123', 1)[0]
                self.assertTrue(record['is_self'])
                self.assertIn('[引用消息ID:-42]', record['content'])
        self.bot.reply.assert_not_awaited()

    async def test_unknown_recalled_and_invalid_targets_never_send(self):
        for identifier in [None, '', '0', '123x', '41', '1.5']:
            self.assertFalse((await reply_message(self.f, self.ctx, identifier, '正文'))['success'])
        for content in ['', ' ', None, 'x' * 4001]:
            with self.assertRaises(ValueError):
                await reply_message(self.f, self.ctx, '-42', content)
        await self.f.record('group', '12', '123', '甲', '内容', message_id='-42')
        await self.f.context.mark_recalled('group:12', '-42')
        self.assertEqual((await reply_message(self.f, self.ctx, '-42', '正文'))['code'], 'message_recalled')
        self.bot.call_action.assert_not_awaited()

    async def test_current_reference_and_cached_target_can_be_selected(self):
        self.ctx.event['message_id'] = 50
        self.ctx.event['message'] = [{'type': 'reply', 'data': {'id': '-42'}}]
        self.assertTrue((await reply_message(self.f, self.ctx, '-42', '引用的消息'))['success'])
        self.ctx.event['message'] = []
        await self.f.record('group', '12', '123', '甲', '原文', message_id='-42')
        self.assertTrue((await reply_message(self.f, self.ctx, '-42', '缓存的消息'))['success'])
        self.f.context.scenes['group:12']['recent_messages'] = []
        self.assertTrue((await reply_message(self.f, self.ctx, '-42', '从保留原文找出的消息'))['success'])
        self.ctx.group_id = 34
        self.assertEqual((await reply_message(self.f, self.ctx, '-42', '不能跨群引用'))['code'], 'unknown_message')

    async def test_scene_and_resolved_id_mismatch_never_send(self):
        for info in [{**self.info, 'group_id': 34}, {**self.info, 'message_id': 43},
                     {'message_type': 'private', 'sender': {'user_id': 456}},
                     {'message_type': 'private', 'sender': {'user_id': 999}, 'target_id': 456}]:
            self.info = info
            self.ctx.message_type = info['message_type']
            self.bot.call_action.reset_mock()
            result = await reply_message(self.f, self.ctx, '-42', '正文')
            self.assertEqual(result['code'], 'scene_mismatch')
            self.assertEqual(self.bot.call_action.await_count, 1)

    async def test_lookup_failure_does_not_claim_ambiguous_send(self):
        for error in [TimeoutError(), RuntimeError('NapCat连接已断开'), OneBotError('get_msg', {'retcode': 1, 'message': 'not found'})]:
            self.bot.call_action.side_effect = error
            self.assertEqual((await reply_message(self.f, self.ctx, '-42', '正文'))['code'], 'reply_target_unavailable')
        self.bot.call_action.side_effect = None
        self.bot.call_action.return_value = {}
        self.assertEqual((await reply_message(self.f, self.ctx, '-42', '正文'))['code'], 'reply_target_unavailable')
        self.assertEqual(self.f.context.recent('group:12', 1), [])

    async def test_cancel_before_send_and_successful_send_survives_recording_error(self):
        self.assertEqual((await reply_message(self.f, self.ctx, '-42', '正文', lambda: False))['code'], 'cancelled')
        self.assertEqual(self.bot.call_action.await_count, 1)
        with patch.object(self.f, 'observe_system', AsyncMock(side_effect=RuntimeError('disk error'))):
            with self.assertLogs('src.feature.ai.reply', level='ERROR'):
                result = await reply_message(self.f, self.ctx, '-42', '正文')
        self.assertTrue(result['success'])
        self.assertIn('warning', result)

    def handler(self, **extra):
        task = {'attempts': {}, 'sent': 0, 'unknown': set(), **extra}
        return self.f.engine.make_handler(self.ctx, 'group:12', True, task, lambda: True), task

    async def test_roles_silent_and_shared_budget(self):
        for extra, code in [({'worker': True}, 'delegation_boundary'),
                            ({'worker': True, 'scheduled': True, 'silent': True}, 'silent_task'),
                            ({'worker': True, 'scheduled': True, 'autonomous': True}, 'proactive_contact_denied')]:
            handler, _ = self.handler(**extra)
            self.assertEqual((await handler('reply_message', {'message_id': '-42', 'message': '正文'}))['code'], code)
        self.bot.call_action.assert_not_awaited()
        handler, task = self.handler()
        for tool in ['send_message', 'reply_message', 'reply_message', 'send_message']:
            self.assertTrue((await handler(tool, {'message_id': '-42', 'message': '正文'}))['success'])
        self.assertEqual(task['sent'], 4)
        self.assertEqual((await handler('reply_message', {'message_id': '-42', 'message': '多余'}))['code'], 'send_limit')
        self.assertEqual((await handler('send_message', {'message': '多余'}))['code'], 'send_limit')

    async def test_send_errors_and_unknown_block_plain_and_quote_retries(self):
        for error, code in [(TimeoutError(), 'unknown'),
                            (RuntimeError('NapCat连接已断开'), 'unknown'),
                            (OneBotError('send_group_msg', {'retcode': 1200, 'message': '权限不足'}), 'permission_denied'),
                            (OneBotError('send_group_msg', {'retcode': 1, 'message': 'failed'}), 'send_failed')]:
            async def action(name, params, ws):
                if name == 'get_msg':
                    return self.info
                raise error
            self.bot.call_action.side_effect = action
            handler, task = self.handler()
            self.assertEqual((await handler('reply_message', {'message_id': '-42', 'message': '正文'}))['code'], code)
            self.assertEqual(task['sent'], 0)
            if code == 'unknown':
                for tool in ['send_message', 'reply_message']:
                    self.assertEqual((await handler(tool, {'message_id': '-42', 'message': '重发'}))['code'], 'unknown')
                self.bot.reply.assert_not_awaited()
        self.bot.reply.side_effect = TimeoutError()
        handler, _ = self.handler()
        self.assertEqual((await handler('send_message', {'message': '正文'}))['code'], 'unknown')
        self.assertEqual((await handler('reply_message', {'message_id': '-42', 'message': '重发'}))['code'], 'unknown')

    async def test_scheduled_reply_counts_as_delivery(self):
        async def run(kind, prompt, **kwargs):
            self.assertTrue((await kwargs['handler']('reply_message', {'message_id': '-42', 'message': '提醒'}))['success'])
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        result = await self.f.engine.run_scheduled(self.ctx, {'id': 'job', 'run_id': 'run', 'scheduled_at': time.time(),
            'delivery': 'message', 'prompt': '回复指定消息'}, lambda: True)
        self.assertIsNone(result['delivery_error'])
        self.assertFalse(result['delivery_unknown'])

    async def test_actual_onebot_dispatch_and_ack(self):
        from src.bot import QQBot
        bot = QQBot(SimpleNamespace(observe_system=AsyncMock()), None)
        class Socket:
            async def send_text(inner, text):
                payload = json.loads(text)
                data = self.info if payload['action'] == 'get_msg' else {'message_id': 789}
                bot._responses[payload['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': data})
        self.ctx.bot, self.ctx.websocket = bot, Socket()
        self.assertEqual((await reply_message(self.f, self.ctx, '-42', '收到'))['message_id'], 789)
        self.assertEqual(bot._responses, {})
