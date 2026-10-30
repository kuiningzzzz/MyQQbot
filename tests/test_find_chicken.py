import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.feature.atom.context import MessageContext
from src.feature.find_chicken.admin import FindChickenAdmin
from src.feature.find_chicken.config import Settings
from src.feature.find_chicken.feature import FindChickenFeature, SearchTask
from src.feature.manager import FeatureManager


def part(kind, **data):
    return {'type': kind, 'data': data}


class FindChickenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.feature = FindChickenFeature(Path(self.temp.name))
        self.feature.search = AsyncMock(return_value={'ok': True, 'text': '图源 https://example.com/a'})
        self.bot = SimpleNamespace(reply=AsyncMock(), call_action=AsyncMock())

    async def asyncTearDown(self):
        await self.feature.stop()

    def ctx(self, message=None, *, private=False, text='/find-chicken', user=123, group=456):
        return MessageContext(bot=self.bot, websocket='socket', event={
            'self_id': 999, 'message': message if message is not None else [part('reply', id='12'), part('text', text=text)],
        }, msg=text, raw_msg=text, message_type='private' if private else 'group', user_id=user, group_id=None if private else group)

    def quoted(self, group=456, **extra):
        return {'group_id': group, 'message_type': 'group',
                'message': [part('image', url='https://example.com/img.png')], **extra}

    async def finish(self):
        if self.feature._worker:
            await asyncio.wait_for(self.feature._worker, 2)

    def texts(self):
        return [call.args[1] if isinstance(call.args[1], str) else call.args[1][0]['data']['text']
                for call in self.bot.reply.await_args_list]

    async def test_replied_image_background_and_literal_result(self):
        self.bot.call_action.return_value = self.quoted()
        gate = asyncio.Event()
        async def search(*args):
            await gate.wait()
            return {'ok': True, 'text': '[CQ:at,qq=all] https://example.com/a'}
        self.feature.search.side_effect = search
        ctx = self.ctx([part('at', qq=999), part('text', text='/find-chicken'), part('reply', id=12)])
        self.assertTrue(await asyncio.wait_for(self.feature.handle(ctx), 1))
        await asyncio.sleep(0)
        self.assertTrue(self.feature.has_active_jobs)
        self.assertIn('开始搜图', self.texts()[0])
        self.bot.call_action.assert_awaited_once_with('get_msg', {'message_id': '12'}, 'socket')
        gate.set()
        await self.finish()
        payload = self.bot.reply.await_args_list[-1].args[1]
        self.assertEqual(payload, [part('text', text='[CQ:at,qq=all] https://example.com/a')])
        self.assertFalse(self.feature.has_active_jobs)

    async def test_direct_first_image_and_quoted_precedence(self):
        ctx = self.ctx([part('image', file='base64://aGVsbG8='), part('image', url='https://example.com/2')])
        self.assertEqual(await self.feature.image_source(ctx), 'base64://aGVsbG8=')
        self.bot.call_action.assert_not_awaited()
        self.bot.call_action.return_value = self.quoted()
        ctx.event['message'].append(part('reply', id=12))
        self.assertEqual(await self.feature.image_source(ctx), 'https://example.com/img.png')

    async def test_cross_scene_missing_image_or_malformed_quote_never_search(self):
        cases = [(self.ctx(), self.quoted(group=457)),
                 (self.ctx(), {'message': [part('image', url='https://example.com/a')]}),
                 (self.ctx(), self.quoted(message=[part('text', text='没有图')])),
                 (self.ctx([part('reply', id='../../file')]), self.quoted()),
                 (self.ctx(private=True), self.quoted())]
        for ctx, info in cases:
            self.bot.call_action.return_value = info
            with self.assertRaises(ValueError):
                await self.feature.image_source(ctx)
        self.feature.search.assert_not_awaited()

    async def test_private_incoming_and_outgoing_quoted_messages(self):
        ctx = self.ctx(private=True)
        for sender, target in [(123, 999), (999, 123)]:
            self.bot.call_action.return_value = {
                'message_type': 'private', 'sender': {'user_id': sender}, 'target_id': target,
                'message': [part('image', url='https://example.com/p.png')],
            }
            self.assertEqual(await self.feature.image_source(ctx), 'https://example.com/p.png')
        self.bot.call_action.return_value['target_id'] = 321
        with self.assertRaises(ValueError):
            await self.feature.image_source(ctx)

    async def test_resource_id_resolution_never_reads_local_path(self):
        ctx = self.ctx([part('image', file='resource-id')])
        self.bot.call_action.return_value = {'file': 'D:/secrets.png', 'url': 'https://example.com/resolved.png'}
        self.assertEqual(await self.feature.image_source(ctx), 'https://example.com/resolved.png')
        self.bot.call_action.assert_awaited_once_with('get_image', {'file': 'resource-id'}, 'socket')
        self.bot.call_action.return_value = {'file': 'D:/secrets.png'}
        with self.assertRaises(ValueError):
            await self.feature.image_source(ctx)

    async def test_cooldown_scoped_to_person_and_scene_and_exact_command(self):
        self.bot.call_action.return_value = self.quoted()
        self.assertFalse(await self.feature.handle(self.ctx(text='/find-chickens')))
        self.assertTrue(await self.feature.handle(self.ctx(text='/find-chicken D:/secrets.png')))
        self.feature.search.assert_not_awaited()
        self.assertTrue(await self.feature.handle(self.ctx()))
        await self.finish()
        self.assertTrue(await self.feature.handle(self.ctx()))
        self.assertIn('秒后再试', self.texts()[-1])
        await self.feature.handle(self.ctx([part('image', url='https://example.com/a')], user=124))
        await self.finish()
        await self.feature.handle(self.ctx([part('image', url='https://example.com/a')], group=457))
        await self.finish()
        self.assertEqual(self.feature.search.await_count, 3)

    async def test_queue_failure_does_not_lose_later_task_and_no_ai_dispatch(self):
        self.feature.search.side_effect = [TimeoutError(), {'ok': True, 'text': '第二项完成'}]
        ai = SimpleNamespace(name='ai', title='AI', description='', show_in_admin=True,
            default_enabled=True, on_enabled_changed=lambda value: None,
            bind_application_context=lambda provider: None, observe=AsyncMock(), handle=AsyncMock(return_value=True))
        manager = FeatureManager(Path(self.temp.name) / 'features.json', [self.feature, ai])
        for user in (123, 124):
            await manager.handle_event(self.ctx([part('image', url='https://example.com/a')], user=user))
        await self.finish()
        self.assertEqual(self.feature.search.await_count, 2)
        ai.handle.assert_not_awaited()
        self.assertTrue(any('超时' in text for text in self.texts()))
        self.assertEqual(self.texts()[-1], '第二项完成')

    async def test_queue_limit_disable_and_shutdown(self):
        gate = asyncio.Event()
        self.feature.search.side_effect = lambda *args: None
        async def block(*args):
            await gate.wait()
            return {'ok': True, 'text': '完成'}
        self.feature.search.side_effect = block
        direct = [part('image', url='https://example.com/a')]
        for user in range(100, 111):
            await self.feature.handle(self.ctx(direct, user=user))
        self.assertIn('队列已满', self.texts()[-1])
        self.assertEqual(len(self.feature.pending) + bool(self.feature.active), 10)
        await self.feature.stop()
        self.assertFalse(self.feature.has_active_jobs)
        self.feature.on_enabled_changed(False)
        self.feature.pending.append(SearchTask(self.ctx(direct), Settings()))
        await self.feature._drain()
        self.assertIn('已关闭', self.texts()[-1])

    async def test_bad_input_and_send_failure_do_not_kill_queue(self):
        self.bot.call_action.return_value = self.quoted(message=[])
        await self.feature.handle(self.ctx())
        await self.finish()
        self.assertIn('没有图片', self.texts()[-1])
        self.bot.reply.side_effect = RuntimeError('offline')
        await self.feature._error(self.ctx(), '错误')  # No exception escapes the consumer.

    async def test_admin_validation_config_and_backup_busy(self):
        app = FastAPI()
        FindChickenAdmin(self.feature).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/api/find-chicken')
            self.assertEqual(response.json()['engines'], ['lens', 'saucenao', 'tracemoe'])
            for settings in ({'engines': []}, {'engines': ['unknown']}, {'timeout_seconds': 0},
                             {'proxy': 'http://name:password@localhost:7890'}, {'extra': True}):
                self.assertEqual((await client.post('/api/find-chicken', json=settings)).status_code, 422)
            saved = await client.post('/api/find-chicken', json={'engines': ['tracemoe'], 'cooldown_seconds': 0})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(self.feature.config.load().engines, ['tracemoe'])
        from src.backup.control import BackupControl
        from src.backup.common import BackupError
        control = object.__new__(BackupControl)
        control.shutdown = lambda: None
        control.requested = False
        control.manager = SimpleNamespace(features={'find_chicken': self.feature})
        self.feature.pending.append(SearchTask(self.ctx(), Settings()))
        with patch.dict('os.environ', {'MYQQBOT_BACKUP_SUPERVISED': '1'}):
            with self.assertRaisesRegex(BackupError, '正在执行任务'):
                control.check()

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    async def test_real_worker_rejects_local_input_without_network(self):
        feature = FindChickenFeature(Path(self.temp.name))
        # Call the actual subprocess implementation; no search service receives data.
        result = await feature.search('file:///etc/passwd', Settings())
        self.assertFalse(result['ok'])
        self.assertIn('失败', result['error'])
        self.assertFalse(list(feature.root.glob('search-*')))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    async def test_real_hung_process_is_killed_on_timeout_and_shutdown(self):
        feature = FindChickenFeature(Path(self.temp.name))
        script = feature.root / 'hang.mjs'
        script.write_text('process.stdin.resume(); setInterval(() => {}, 1000);', encoding='utf-8')
        settings = SimpleNamespace(total_timeout_seconds=0.15, model_dump=lambda: Settings().model_dump())
        with patch('src.feature.find_chicken.feature.WORKER', script):
            with self.assertRaises(TimeoutError):
                await feature.search('https://example.com/not-requested', settings)
            job = asyncio.create_task(feature.search('https://example.com/not-requested', Settings()))
            await asyncio.sleep(0.1)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertFalse(list(feature.root.glob('search-*')))

    @unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Windows process tree test')
    async def test_exited_launcher_cannot_leave_detached_browser_descendants(self):
        import ctypes
        from ctypes import wintypes
        feature = FindChickenFeature(Path(self.temp.name))
        script = feature.root / 'launcher.mjs'
        script.write_text("""import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {detached: true, stdio: 'ignore'});
child.unref();
fs.writeFileSync(path.join(process.argv[2], 'child-pid.txt'), String(child.pid));
console.log(JSON.stringify({ok: true, text: 'finished'}));
process.exit(0);
""", encoding='utf-8')
        with patch('src.feature.find_chicken.feature.WORKER', script):
            self.assertTrue((await feature.search('unused', Settings()))['ok'])
        pid = int((feature.root / 'child-pid.txt').read_text())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for _ in range(50):
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                break
            try:
                code = wintypes.DWORD()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                if code.value != 259:  # STILL_ACTIVE
                    break
            finally:
                kernel.CloseHandle(handle)
            await asyncio.sleep(0.02)
        else:
            self.fail('Search launcher left a detached descendant alive')

    async def test_get_image_allowed_by_actual_bot_dispatch(self):
        from src.bot import QQBot
        bot = QQBot(None, None)
        class Socket:
            async def send_text(inner, text):
                payload = json.loads(text)
                self.assertEqual(payload['action'], 'get_image')
                bot._responses[payload['echo']][1].set_result({'status': 'ok', 'retcode': 0,
                    'data': {'url': 'https://example.com/test.png'}})
        self.assertEqual(await bot.call_action('get_image', {'file': 'id'}, Socket()), {'url': 'https://example.com/test.png'})
        self.assertFalse(bot._responses)
