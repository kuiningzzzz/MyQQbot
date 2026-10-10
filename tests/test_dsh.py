"""Exercise the installed official runtime against a local Messages API, never QQ/cloud."""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.feature.ai.feature import AiFeature
from src.feature.ai.journal import Journal
from src.feature.ai.migrate import migrate
from src.feature.ai.storage import atomic_write
from src.feature.ai.transactions import MemoryTransaction, recover
from src.feature.atom.context import MessageContext


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999'})

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def record(self, identifier, **kwargs):
        return await self.f.record('group', '12', '123', '甲', '事实', message_id=str(identifier), **kwargs)

    async def test_immutable_event_precedes_ocr_and_restart_queue_deduplicates(self):
        original = {'message_id': 1, 'self_id': 999, 'message': [{'type': 'image', 'data': {'url': 'https://example.org/a.png'}}]}
        async def render(*args):
            self.assertEqual(self.f.journal.pending_count(), 1)
            self.assertEqual(self.f.journal.load(self.f.journal.rows()[0])['event'], original)
            return 'OCR原文与详细描述'
        self.f._render = render
        ctx = MessageContext(bot=None, websocket=None, event=original, msg='', message_type='group', user_id=123, group_id=12)
        await self.f.observe(ctx)
        self.assertIn('OCR原文', self.f.journal_record(self.f.journal.rows()[0])['rendered_content'])
        again = Journal(self.f.workspace)
        self.assertEqual(again.pending_count(), 1)
        self.assertIn('OCR原文', again.load(again.rows()[0])['rendered_content'])
        await self.f.observe(ctx)
        self.assertEqual(again.pending_count(), 1)

    async def test_exact_100_human_messages_and_success_only_ack(self):
        for number in range(101):
            await self.record(number)
        await self.f.record('group', '12', '999', 'bot', '自身消息', outgoing=True)
        self.assertEqual(self.f.journal.pending_count(), 101)
        calls = []
        async def run(kind, prompt, **kwargs):
            data = json.loads(prompt)
            calls.append(data)
            self.assertEqual(kind, 'memory')
            self.assertEqual(data['count'], 100)
            atomic_write(kwargs['cwd'] / 'memory/user/123/PERSON.md', '- 大三学生\n')
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': data['batch']})}
        self.f.dsh.run = run
        result = await self.f.runtime.full()
        self.assertEqual(result['processed'], 100)
        self.assertEqual(self.f.journal.pending_count(), 1)
        self.assertIn('大三', self.f.workspace.read_memory('user/123/PERSON.md'))

    async def test_failed_batch_discards_stage_and_retries_same_rows(self):
        await self.record(1)
        async def run(kind, prompt, **kwargs):
            atomic_write(kwargs['cwd'] / 'memory/user/123/PREFERENCE.md', '- 未完成内容')
            return {'finish_reason': 'error', 'final_response': '{}'}
        self.f.dsh.run = run
        with self.assertRaises(RuntimeError):
            await self.f.runtime.full()
        self.assertEqual(self.f.journal.pending_count(), 1)
        self.assertEqual(self.f.workspace.read_memory('user/123/PREFERENCE.md'), '')
        self.assertFalse((self.f.workspace.root / 'tmp/memory-stage').exists())

    async def test_commit_recovery_and_conflicting_main_edit(self):
        transaction = MemoryTransaction(self.f.workspace, '1-100')
        atomic_write(transaction.root / 'memory/user/123/PERSON.md', 'staged')
        self.f.workspace.write_memory('user/123/PERSON.md', 'chat changed')
        with self.assertRaises(ValueError):
            transaction.commit()
        self.assertEqual(self.f.workspace.read_memory('user/123/PERSON.md'), 'chat changed')
        transaction.close()
        atomic_write(self.f.workspace.data_root / '.state/memory-commit.json', json.dumps({'batch': '1-100', 'changes': [{'path': 'user/123/PERSON.md', 'content': 'committed'}]}))
        recover(self.f.workspace)
        self.assertEqual(self.f.workspace.read_memory('user/123/PERSON.md'), 'committed')
        self.assertEqual(self.f.workspace.load_state('memory-batch-receipt', {})['batch'], '1-100')

    async def test_rolling_30_day_retention_preserves_memories(self):
        from datetime import datetime, timezone
        old = datetime.fromtimestamp(time.time() - 31 * 86400, timezone.utc).isoformat()
        await self.record(1, timestamp=old)
        await self.record(2)
        self.f.workspace.write_memory('user/123/PERSON.md', 'long term')
        self.assertEqual(self.f.journal.prune(), 1)
        await self.f.context.prune(time.time() - 30 * 86400)
        self.assertEqual(len(self.f.journal.rows()), 1)
        self.assertEqual(len(self.f.context.recent('group:12', 10)), 1)
        self.assertEqual(self.f.workspace.read_memory('user/123/PERSON.md'), 'long term')

    async def test_startup_migration_preserves_bytes_is_idempotent_and_conflicts_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = '# PERSON\r\n- 用户\r\n'.encode('utf-8')
            file = root / 'user/123/PERSON.md'
            file.parent.mkdir(parents=True)
            file.write_bytes(original)
            atomic_write(root / 'config.yaml', 'raw_retention_days: 7\n')
            first = migrate(root)
            self.assertEqual((root / 'workspace/memory/user/123/PERSON.md').read_bytes(), original)
            self.assertEqual(migrate(root), first)
            self.assertIn('30', (root / 'config.yaml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_write(root / 'user/123/PERSON.md', 'old')
            atomic_write(root / 'workspace/memory/user/123/PERSON.md', 'new')
            with self.assertRaises(ValueError):
                migrate(root)
            self.assertEqual((root / 'user/123/PERSON.md').read_text(), 'old')

    async def test_explicit_correction_cancels_stale_send_and_other_scene_queues(self):
        began, release = asyncio.Event(), asyncio.Event()
        results = []
        calls = 0
        async def run(kind, prompt, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                began.set()
                await release.wait()
            if kwargs['current']():
                await kwargs['handler']('send_message', {'message': json.loads(prompt)['request']})
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        self.f.dsh.cancel = AsyncMock()
        async def reply(ctx, message):
            results.append((ctx.group_id, message))
        bot = SimpleNamespace(reply=reply)
        def context(text, group=12):
            return MessageContext(bot=bot, event={'_ai_arrival_forced': True}, websocket=None, msg=text, message_type='group', user_id=123, group_id=group)
        await self.f.engine.handle(context('first'))
        await began.wait()
        await self.f.engine.handle(context('other scene', 34))
        self.f.dsh.cancel.assert_not_awaited()
        await self.f.engine.handle(context('等等，补充一下'))
        self.f.dsh.cancel.assert_awaited_once()
        release.set()
        await asyncio.gather(*self.f.engine.tasks.values())
        self.assertEqual(results, [(34, 'other scene'), (12, '等等，补充一下')])


    async def test_automatic_queue_resumes_after_restart_and_keeps_remainder(self):
        for number in range(101):
            await self.record(number)
        restarted = AiFeature(self.f.workspace.data_root)
        restarted.config.save({'memory_auto_learn': True})
        calls = []
        async def run(kind, prompt, **kwargs):
            batch = json.loads(prompt)['batch']
            calls.append(batch)
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': batch})}
        restarted.dsh.run = run
        try:
            await restarted.runtime.start()
            await restarted.runtime.worker
            self.assertEqual(len(calls), 1)
            self.assertEqual(restarted.journal.pending_count(), 1)
        finally:
            await restarted.stop()

    async def test_fast_disable_enable_resumes_new_requests(self):
        replies = []
        async def run(kind, prompt, **kwargs):
            await kwargs['handler']('send_message', {'message': json.loads(prompt)['request']})
            return {'finish_reason': 'completed'}
        async def reply(ctx, message):
            replies.append(message)
        self.f.dsh.run = run
        bot = SimpleNamespace(reply=reply)
        def context(text):
            return MessageContext(bot=bot, event={'_ai_arrival_forced': True}, websocket=None,
                msg=text, message_type='group', user_id=123, group_id=12)
        # No event-loop yield: cancellation happens before the worker starts.
        await self.f.engine.handle(context('discard'))
        self.f.on_enabled_changed(False)
        self.f.on_enabled_changed(True)
        await self.f.engine.handle(context('resume'))
        for _ in range(10):
            await asyncio.sleep(0)
            if not self.f.engine.tasks:
                break
        self.assertEqual(replies, ['resume'])
        self.assertTrue(self.f.engine.pending.empty())

    async def test_admin_settings_redact_key_and_manual_queue_api(self):
        import httpx
        from fastapi import FastAPI
        from src.feature.ai.admin import AiAdmin
        app = FastAPI()
        AiAdmin(self.f).register_routes(app)
        environment = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'do-not-expose'})
        environment.start()
        self.addCleanup(environment.stop)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/ai/config')
            self.assertNotIn('do-not-expose', response.text)
            self.assertEqual(response.json()['memory_batch_size'], 100)
            self.assertEqual(response.json()['raw_retention_days'], 30)
            self.assertIn('SKILL.md', response.json()['memory_skill_path'])
            self.assertEqual(response.json()['model'], 'deepseek-flash')
            self.assertNotIn('max_tokens', response.json())
            self.assertNotIn('api_url', response.json())
            rejected = await client.post('/api/ai/config', json={'model': 'other', 'api_key': 'bad'})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual((await client.get('/api/ai/search')).status_code, 404)
            result = await client.post('/memory/integrate', json={'mode': 'queue'})
            self.assertEqual(result.json()['processed'], 0)
            retired = await client.post('/memory/integrate', json={'mode': 'full', 'date': '2026-10-09'})
            self.assertEqual(retired.status_code, 400)

    async def test_config_upgrade_uses_environment_only_and_removes_retired_fields(self):
        from src.feature.ai.config import Config
        atomic_write(self.f.config.path, 'api_key: obsolete-secret\nmodel: other\nmax_tokens: 1\ncontext_token_limit: 2\nnickname: 保留名字\n')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'environment-secret', 'AI_MODEL': 'other', 'AI_MAX_TOKENS': '1'}):
            config = Config(self.f.workspace.data_root)
            settings = config.load()
            self.assertEqual(settings.api_key, 'environment-secret')
            self.assertEqual(settings.model, 'deepseek-flash')
            self.assertEqual(settings.vision_model, 'deepseek-flash')
            self.assertEqual(settings.nickname, '保留名字')
            saved = config.path.read_text('utf-8')
            self.assertNotIn('secret', saved)
            self.assertNotIn('max_tokens', saved)
            with self.assertRaises(ValueError):
                config.save({'dsh_timeout_seconds': 1})

    async def test_admin_memory_create_edit_conflict_delete_and_structured_list(self):
        import httpx
        from fastapi import FastAPI
        from src.feature.ai.admin import AiAdmin
        app = FastAPI()
        AiAdmin(self.f).register_routes(app)
        await self.record('nickname')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            created = await client.post('/api/ai/file/create', json={'qq': '123', 'category': 'EVENT', 'title': '摄影项目'})
            self.assertEqual(created.status_code, 200, created.text)
            data = created.json()
            path = data['path']
            self.assertEqual(path, 'user/123/memory/摄影项目.md')
            duplicate = await client.post('/api/ai/file/create', json={'qq': '123', 'category': 'EVENT', 'title': '摄影项目'})
            self.assertEqual(duplicate.status_code, 409)
            for qq, title in [('999', '自身'), ('123', '../越界')]:
                rejected = await client.post('/api/ai/file/create', json={'qq': qq, 'category': 'EVENT', 'title': title})
                self.assertEqual(rejected.status_code, 409)
            listing = (await client.get('/api/ai/files')).json()
            item = next(item for item in listing['items'] if item['path'] == path)
            self.assertEqual(item['nickname'], '甲')
            self.assertEqual(item['category'], 'EVENT')
            self.f.workspace.write_memory(path, 'Agent已更新')
            save = await client.post('/api/ai/file', json={'path': path, 'content': '旧编辑', 'expected_hash': data['hash']})
            self.assertEqual(save.status_code, 409)
            deleted = await client.post('/api/ai/file/delete', json={'path': path, 'expected_hash': data['hash']})
            self.assertEqual(deleted.status_code, 409)
            current = (await client.get('/api/ai/file', params={'path': path})).json()
            deleted = await client.post('/api/ai/file/delete', json={'path': path, 'expected_hash': current['hash']})
            self.assertEqual(deleted.status_code, 200)
            self.assertFalse(self.f.workspace.memory_path(path).exists())


@unittest.skipUnless(shutil.which('node') and Path('dsh/node_modules/@deepseek-ai/dsh/lib/bin.js').exists(), 'run npm ci in dsh to enable real-kernel integration tests')
class NativeDshTests(unittest.IsolatedAsyncioTestCase):
    # Do not inherit the state tests twice.
    async def asyncSetUp(self):
        await StateTests.asyncSetUp(self)
        self.responses = []
        self.requests = []
        self.model_started = asyncio.Event()
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        base = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        environment = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'local-test-key', 'AI_DSH_API_URL': base, 'DEEPSEEK_SEARCH_BASE_URL': base + '/v1'})
        environment.start()
        self.addCleanup(environment.stop)

    async def asyncTearDown(self):
        await StateTests.asyncTearDown(self)
        self.server.close()
        await self.server.wait_closed()

    async def serve(self, reader, writer):
        try:
            line = await reader.readline()
            headers = {}
            while (line := await reader.readline()) not in (b'\r\n', b''):
                key, value = line.decode().split(':', 1)
                headers[key.lower()] = value.strip()
            if 'content-length' not in headers:
                return
            request = json.loads(await reader.readexactly(int(headers['content-length'])))
            self.requests.append(request)
            self.model_started.set()
            response = self.responses.pop(0)
            if callable(response):
                response = response(request)
            if asyncio.iscoroutine(response):
                response = await response
            if request.get('stream') is not True:
                data = json.dumps(response).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data)
                await writer.drain()
                return
            blocks = response if isinstance(response, list) else [{'type': 'text', 'text': response}]
            events = [{'type': 'message_start', 'message': {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'model': 'test', 'content': [], 'usage': {'input_tokens': 100, 'output_tokens': 0}}}]
            for index, block in enumerate(blocks):
                events.append({'type': 'content_block_start', 'index': index, 'content_block': block})
                events.append({'type': 'content_block_stop', 'index': index})
            events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if any(b['type'] == 'tool_use' for b in blocks) else 'end_turn'}, 'usage': {'output_tokens': max(10, sum(len(b.get('text', '')) for b in blocks) // 4)}}, {'type': 'message_stop'}]
            data = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e, ensure_ascii=False) + '\n\n' for e in events).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def tool(name, **args):
        import uuid
        return [{'type': 'tool_use', 'id': 'call-' + uuid.uuid4().hex, 'name': name, 'input': args}]

    async def native(self, kind='chat', **kwargs):
        return await self.f.dsh.run(kind, kwargs.pop('prompt', 'native test'), task_id=kwargs.pop('task_id', 'test'), current=lambda: True, handler=kwargs.pop('handler', AsyncMock(return_value={'success': True})), qq='123', scene_dir='scene', system='必读分类规则', **kwargs)

    async def test_native_write_guard_owner_downgrade_and_session_resume(self):
        target = self.f.workspace.root / 'memory/user/123/PERSON.md'
        self.responses = [self.tool('write', file_path=str(target), content='- 身份：大学生'), 'done']
        result = await self.native()
        self.assertEqual(result['finish_reason'], 'completed')
        self.assertEqual(target.read_text('utf-8'), '- 身份：大学生')
        self.assertIn('PERSON.md', json.dumps(self.requests[0]['system'], ensure_ascii=False))
        outside = self.f.workspace.data_root / 'outside.txt'
        self.responses = [self.tool('write', file_path=str(outside), content='owner'), 'owner done']
        await self.native(owner=True)
        self.assertEqual(outside.read_text(), 'owner')
        await self.f.dsh.stop()
        self.responses = [self.tool('write', file_path=str(outside), content='intruder'), 'denied']
        await self.native()
        self.assertEqual(outside.read_text(), 'owner')
        self.assertIn('OWNER_REQUIRED', json.dumps(self.requests[-1]['messages']))
        self.assertIn('owner done', json.dumps(self.requests[-1]))

    async def test_native_qq_callback_and_memory_forbidden_send(self):
        callback = AsyncMock(return_value={'success': False, 'code': 'permission_denied'})
        self.responses = [self.tool('recall_message', message_id='42', reason='本人申请'), '权限不足']
        await self.native(handler=callback)
        callback.assert_awaited_once()
        self.assertIn('permission_denied', json.dumps(self.requests[-1]))
        stage = self.f.workspace.root / 'tmp/memory-stage'
        stage.mkdir(parents=True)
        self.responses = [self.tool('send_message', message='forbidden'), 'blocked']
        callback.reset_mock()
        await self.native('memory', cwd=stage, handler=callback)
        callback.assert_not_awaited()
        self.assertIn('PERMISSION_DENIED', json.dumps(self.requests[-1]['messages']))

    async def test_native_memory_batch_edits_markdown_and_acknowledges(self):
        await self.f.record('group', '12', '123', '甲', '大三学生，喜欢摄影', message_id='1')
        stage = self.f.workspace.root / 'tmp/memory-stage'
        self.responses = [self.tool('read', file_path=str(stage / 'batch.json')),
            self.tool('write', file_path=str(stage / 'memory/user/123/PERSON.md'), content='- 大三学生'),
            self.tool('write', file_path=str(stage / 'memory/user/123/PREFERENCE.md'), content='- 喜欢摄影'),
            '{"completed":true,"batch":"1-1"}']
        result = await self.f.runtime.full()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(self.f.journal.pending_count(), 0)
        self.assertIn('大三', self.f.workspace.read_memory('user/123/PERSON.md'))

        self.assertIn('摄影', self.f.workspace.read_memory('user/123/PREFERENCE.md'))

    async def test_native_search_and_global_chat_scene_routing(self):
        self.f.workspace.write_memory('user/123/PERSON.md', '- 大学生')
        self.responses = [self.tool('grep', pattern='大学生', path='memory'), 'found']
        await self.native()
        results = [b for m in self.requests[-1]['messages'] for b in m['content'] if b.get('type') == 'tool_result']
        self.assertFalse(results[-1].get('is_error'))
        self.assertIn('PERSON.md', json.dumps(results[-1]['content'], ensure_ascii=False))
        bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: False))
        for group in (12, 34):
            await self.f.record('group', str(group), '123', '甲', 'group ' + str(group))
            self.responses = [self.tool('send_message', message='reply ' + str(group)), 'done']
            ctx = MessageContext(bot=bot, event={'self_id': 999}, websocket=None, msg='reply here', message_type='group', user_id=123, group_id=group)
            await self.f.engine._turn(ctx, 'group:' + str(group), True)
        self.assertEqual([call.args[0].group_id for call in bot.reply.await_args_list], [12, 34])
        history = json.dumps(self.requests[-1]['messages'], ensure_ascii=False)
        self.assertIn('group:12', history)
        self.assertIn('group:34', history)

    async def test_official_automatic_compaction(self):
        # Exercise the upstream defaults rather than reintroducing app budgets.
        self.responses = [lambda request: 'Retained checkpoint of the older background.' if 'acting as a compaction engine' in json.dumps(request['messages'][-1]) else 'older background detail ' * 30000] * 30
        for _ in range(12):
            first = await self.native()
            self.assertEqual(first['finish_reason'], 'completed', first.get('error'))
            if any('acting as a compaction engine' in json.dumps(request['messages'][-1]) for request in self.requests):
                break
        self.responses = ['New reply.'] * 8
        result = await self.native(prompt='Continue the conversation.')
        self.assertEqual(result['finish_reason'], 'completed', result.get('error'))
        self.assertEqual(result['final_response'], 'New reply.')
        self.assertTrue(any('acting as a compaction engine' in json.dumps(request['messages'][-1]) for request in self.requests))
        self.assertIn('Retained checkpoint', json.dumps(self.requests[-1]['messages']))

    async def test_native_deepseek_web_search_and_fetch_guards(self):
        self.responses = [self.tool('web_search', queries=['QQ bot architecture']),
            {'content': [{'type': 'web_search_tool_result', 'content': [
                {'type': 'web_search_result', 'url': 'https://example.org/source', 'title': 'Official source'}]}]}, 'done']
        result = await self.native()
        self.assertEqual(result['finish_reason'], 'completed', result.get('error'))
        search_request = next(r for r in self.requests if r.get('tools', [{}])[0].get('type') == 'web_search_20250305')
        self.assertEqual(search_request['model'], 'deepseek-flash')
        self.assertIn('Official source', json.dumps(self.requests[-1]['messages']))
        self.responses = [self.tool('web_fetch', url='http://127.0.0.1/private'), 'done']
        await self.native()
        self.assertIn('non-public', json.dumps(self.requests[-1]['messages']).lower())

    async def test_native_cancellation_revokes_callback_and_expired_logs_are_deleted(self):
        async def slow(_):
            await asyncio.sleep(2)
            return 'late'
        self.responses = [slow]
        run = asyncio.create_task(self.native(task_id='cancel-me'))
        await asyncio.wait_for(self.model_started.wait(), 10)
        await self.f.dsh.cancel('cancel-me')
        self.assertTrue((await asyncio.wait_for(run, 10))['cancelled'])
        await self.f.dsh.stop()
        manifest = self.f.dsh.home / 'qqbot-retention.json'
        data = json.loads(manifest.read_text())
        atomic_write(manifest, json.dumps({key: 0 for key in data}))
        await self.f.dsh.prune()
        self.assertFalse(self.f.dsh.has_session('chat'))
        self.assertEqual(list((self.f.dsh.home / 'sessions').glob('*/*/*.jsonl')), [])
