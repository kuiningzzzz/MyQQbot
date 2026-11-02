import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.feature.ai.feature import AiFeature
from src.feature.ai.image_send import MAX_BYTES, send_image, validate_image
from src.feature.atom.context import MessageContext
from src.onebot_error import OneBotError


def image_bytes(fmt='PNG'):
    stream = io.BytesIO()
    image = Image.new('RGB', (8, 8), 'blue')
    if fmt == 'GIF':
        image.save(stream, fmt, save_all=True, append_images=[Image.new('RGB', (8, 8), 'red')], duration=100, loop=0)
    else:
        image.save(stream, fmt)
    return stream.getvalue()


class ImageSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'bot_qq': '999', 'vector_index_enabled': False, 'memory_auto_learn': False})
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={'message_id': 456}), reply=AsyncMock())
        self.ctx = MessageContext(bot=self.bot, event={'self_id': 999}, websocket='ws', msg='把图片发给我',
            message_type='group', user_id=123, group_id=12)
        self.png = image_bytes()
        await self.f.sandbox.put('group:12', 'chart.png', self.png)

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    def handler(self, *, forced=True, current=lambda: True, **extra):
        task = {'worker': True, 'attempts': {}, 'sent': 0, 'unknown': set(), **extra}
        return self.f.engine.make_handler(self.ctx, self.f.scene_key(self.ctx), forced, task, current), task

    async def test_group_private_actual_segments_bytes_receipt_and_archive(self):
        for scene in ['group', 'private']:
            self.ctx.message_type = scene
            await self.f.sandbox.put(self.f.scene_key(self.ctx), 'chart.png', self.png)
            handler, task = self.handler()
            result = await handler('send_image', {'path': 'chart.png'})
            self.assertEqual(result['message_id'], '456')
            self.assertEqual(result['bytes'], len(self.png))
            self.assertEqual(task['sent'], 1)
            action, args, ws = self.bot.call_action.await_args.args
            self.assertEqual(action, f'send_{scene}_msg')
            self.assertEqual(args['group_id' if scene == 'group' else 'user_id'], '12' if scene == 'group' else '123')
            segment, = args['message']
            self.assertEqual(segment['type'], 'image')
            self.assertEqual(base64.b64decode(segment['data']['file'].removeprefix('base64://')), self.png)
            record = self.f.context.recent(self.f.scene_key(self.ctx), 1)[0]
            self.assertTrue(record['is_self'])
            self.assertEqual(record['id'], '456')
            self.assertIn('发送图片', record['content'])
        self.bot.reply.assert_not_awaited()

    async def test_formats_are_checked_by_bytes_and_animated_gif_is_preserved(self):
        for fmt in ['PNG', 'JPEG', 'GIF', 'WEBP', 'BMP']:
            raw = image_bytes(fmt)
            await self.f.sandbox.put('group:12', 'picture.txt', raw)
            output = await send_image(self.f, self.ctx, 'picture.txt')
            self.assertTrue(output['success'])
            self.assertEqual(output['format'], fmt)
            sent = self.bot.call_action.await_args.args[1]['message'][0]['data']['file']
            self.assertEqual(base64.b64decode(sent[9:]), raw)
        gif = image_bytes('GIF')
        with Image.open(io.BytesIO(gif)) as image:
            self.assertEqual(image.n_frames, 2)

    async def test_invalid_corrupt_unsupported_oversized_never_send(self):
        for data in [b'not an image', b'<svg xmlns="http://www.w3.org/2000/svg"/>', image_bytes('TIFF'), self.png[:40]]:
            await self.f.sandbox.put('group:12', 'bad.png', data)
            with self.assertRaises(ValueError):
                await send_image(self.f, self.ctx, 'bad.png')
        for encoded in ['', 'invalid base64!!', 'A' * (4 * ((MAX_BYTES + 2) // 3) + 1)]:
            with self.assertRaises(ValueError):
                validate_image(encoded)
        fake = SimpleNamespace(format='PNG', size=(5000, 5000), width=5000, height=5000)
        with patch('PIL.Image.open') as opened:
            opened.return_value.__enter__.return_value = fake
            with self.assertRaisesRegex(ValueError, '2000万'):
                validate_image(base64.b64encode(self.png).decode())
        self.bot.call_action.assert_not_awaited()

    async def test_path_cannot_access_other_scenes_or_host_and_accepts_absolute_own(self):
        other = self.f.sandbox.scene_dir('group:34') / 'else.png'
        other.write_bytes(self.png)
        for path in [str(other), '../chart.png', str(self.f.workspace.memory_root / 'soul/AGENT.md'),
                     'https://example.com/a.png', 'base64://abcd', 'missing.png', None]:
            with self.assertRaises(ValueError):
                await send_image(self.f, self.ctx, path)
        self.bot.call_action.assert_not_awaited()
        own = self.f.sandbox.scene_dir('group:12') / 'chart.png'
        self.assertTrue((await send_image(self.f, self.ctx, str(own)))['success'])

    async def test_existing_attachment_download_then_native_image_send(self):
        identifier, _ = self.f.workfiles.register(self.ctx, 'image', {'name': 'received.png', 'url': 'https://example.com/a.png'})
        self.f.workfiles.fetch = AsyncMock(return_value=self.png)
        downloaded = await self.f.workfiles.download(self.ctx, identifier, 'received.png', lambda: True)
        self.assertTrue((await send_image(self.f, self.ctx, downloaded['path']))['success'])

    async def test_roles_workspace_explicit_silent_contact_and_limit(self):
        for options, code in [({'worker': False}, 'delegation_required'),
                              ({'silent': True, 'scheduled': True}, 'silent_task'),
                              ({'autonomous': True}, 'proactive_contact_denied')]:
            handler, _ = self.handler(**options)
            self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], code)
        handler, _ = self.handler(forced=False)
        self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'explicit_request_required')
        self.f.config.save({'workspace_enabled': False})
        handler, _ = self.handler()
        self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'explicit_request_required')
        self.bot.call_action.assert_not_awaited()
        self.f.config.save({'workspace_enabled': True})
        handler, task = self.handler()
        for _ in range(3):
            self.assertTrue((await handler('send_image', {'path': 'chart.png'}))['success'])
        self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'send_limit')
        self.assertEqual(task['sent'], 3)

    async def test_cancellation_and_disable_after_export_prevent_send(self):
        handler, _ = self.handler(current=lambda: False)
        self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'cancelled')
        async def export(*args, **kwargs):
            self.f.config.save({'workspace_enabled': False})
            return {'success': True, 'data': base64.b64encode(self.png).decode()}
        self.f.sandbox.request = export
        self.assertEqual((await send_image(self.f, self.ctx, 'chart.png'))['code'], 'cancelled')
        self.bot.call_action.assert_not_awaited()

    async def test_autonomous_contact_revocation_during_export_prevents_send(self):
        allowed = True
        async def export(*args, **kwargs):
            nonlocal allowed
            allowed = False
            return {'success': True, 'data': base64.b64encode(self.png).decode()}
        self.f.sandbox.request = export
        with patch.object(self.f.autonomy, 'permitted', side_effect=lambda key: allowed):
            handler, task = self.handler(autonomous=True)
            self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'cancelled')
        self.bot.call_action.assert_not_awaited()
        self.assertEqual(task['sent'], 0)

    async def test_failures_and_unknown_prevent_cross_channel_retries(self):
        for error, code in [(TimeoutError(), 'unknown'), (RuntimeError('NapCat连接已断开'), 'unknown'),
                            (OneBotError('send_group_msg', {'retcode': 1, 'message': '权限不足'}), 'permission_denied'),
                            (OneBotError('send_group_msg', {'retcode': 1, 'message': '图片不可发送'}), 'send_failed')]:
            self.bot.call_action.side_effect = error
            handler, task = self.handler()
            self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], code)
            self.assertEqual(task['sent'], 0)
            if code == 'unknown':
                for tool in ['send_image', 'send_file']:
                    self.assertEqual((await handler(tool, {'path': 'chart.png'}))['code'], 'unknown')
        self.bot.call_action.side_effect = None
        self.bot.call_action.return_value = {}
        self.assertEqual((await send_image(self.f, self.ctx, 'chart.png'))['code'], 'unknown')
        handler, task = self.handler()
        task['unknown'].add('send_file')
        self.assertEqual((await handler('send_image', {'path': 'chart.png'}))['code'], 'unknown')
        self.assertEqual(self.f.context.recent('group:12', 1), [])

    async def test_acknowledged_send_survives_recording_failure(self):
        with patch.object(self.f, 'observe_system', AsyncMock(side_effect=OSError('disk error'))):
            with self.assertLogs('src.feature.ai.image_send', level='ERROR'):
                result = await send_image(self.f, self.ctx, 'chart.png')
        self.assertTrue(result['success'])
        self.assertIn('warning', result)
        self.bot.call_action.assert_awaited_once()

    async def test_scheduled_image_counts_as_delivery(self):
        async def run(kind, prompt, **kwargs):
            self.assertTrue((await kwargs['handler']('send_image', {'path': 'chart.png'}))['success'])
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        result = await self.f.engine.run_scheduled(self.ctx, {'id': 'job', 'run_id': 'run', 'scheduled_at': time.time(),
            'delivery': 'message', 'prompt': '发送图表'}, lambda: True)
        self.assertIsNone(result['delivery_error'])

    async def test_actual_onebot_action_and_receipt(self):
        from src.bot import QQBot
        bot = QQBot(SimpleNamespace(observe_system=AsyncMock()), None)
        class Socket:
            async def send_text(inner, text):
                payload = json.loads(text)
                self.assertEqual(payload['action'], 'send_group_msg')
                self.assertEqual(payload['params']['message'][0]['type'], 'image')
                bot._responses[payload['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': 789}})
        self.ctx.bot, self.ctx.websocket = bot, Socket()
        self.assertEqual((await send_image(self.f, self.ctx, 'chart.png'))['message_id'], '789')
        self.assertEqual(bot._responses, {})
