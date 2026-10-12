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


def host_state(system):
    return json.JSONDecoder().raw_decode(system.rsplit('【当轮宿主状态】\n', 1)[1].lstrip())[0]


def model_instructions(request):
    # DeepSeek can receive subsequent system-prompt replacements in history
    # to preserve its prompt cache. Inspect the latest effective update too.
    return request['system'] + '\n' + '\n'.join(
        block['text'] for message in request['messages'] for block in message['content']
        if block['type'] == 'text')


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

    async def test_self_context_refreshes_identity_paths_and_feature_gates(self):
        # One logical conversation must not retain the last request's authority
        # or attachment destination, including after settings are changed.
        calls = []
        async def run(kind, prompt, **kwargs):
            calls.append(host_state(kwargs['system']))
            await kwargs['handler']('send_message', {'message': '已检查当前权限'})
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: qq == 123))
        for qq, group, explicit in ((123, 12, True), (456, 34, False)):
            if qq == 456:
                self.f.config.save({'workspace_enabled': False, 'ocr_enabled': False, 'memory_enabled': False})
            ctx = MessageContext(bot=bot, websocket=None, event={}, msg='检查权限', message_type='group', user_id=qq, group_id=group)
            await self.f.engine._turn(ctx, f'group:{group}', explicit)
        first, second = calls
        self.assertTrue(first['owner'])
        self.assertFalse(first['shell_allowed'])
        self.assertTrue(first['background_spawn_available'])
        self.assertTrue(first['file_transfer_allowed'])
        self.assertEqual(first['files_directory'], str(self.f.sandbox.scene_dir('group:12')))
        self.assertEqual(second['requester_qq'], '456')
        self.assertEqual(second['current_scene'], 'group:34')
        self.assertFalse(second['owner'])
        self.assertFalse(second['shell_allowed'])
        self.assertFalse(second['file_transfer_allowed'])
        self.assertFalse(second['file_transfer_enabled'])
        self.assertFalse(second['ocr_enabled'])
        self.assertFalse(second['memory_enabled'])
        self.assertEqual(second['files_directory'], str(self.f.sandbox.scene_dir('group:34')))

    async def test_admin_help_and_module_switches_refresh_chat_and_memory_context(self):
        import httpx
        from fastapi import FastAPI
        from src.feature.help.admin import HelpAdmin
        from src.feature.help.feature import HelpFeature, DEFAULT_PUBLIC_HELP_MESSAGES
        from src.feature.hello.feature import HelloFeature
        from src.feature.manager import FeatureManager
        master = SimpleNamespace(is_master=lambda qq: qq == 123)
        help_feature = HelpFeature(Path(self.temp.name) / 'help.json', master)
        manager = FeatureManager(Path(self.temp.name) / 'features.json', [self.f, help_feature, HelloFeature()])
        app = FastAPI()
        HelpAdmin(help_feature).register_routes(app)
        calls = []
        async def run(kind, prompt, **kwargs):
            calls.append((kwargs['system'], host_state(kwargs['system'])))
            if kind == 'memory':
                return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': json.loads(prompt)['batch']})}
            await kwargs['handler']('send_message', {'message': '当前用法已说明'})
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        bot = SimpleNamespace(reply=AsyncMock(), master_service=master)
        async def chat(qq):
            ctx = MessageContext(bot=bot, websocket=None, event={}, msg='介绍机器人', message_type='private', user_id=qq, group_id=None)
            await self.f.engine._turn(ctx, f'private:{qq}', True)
            return calls[-1]
        def modules(state):
            return {item['name']: item for item in state['application']['features']}
        _, initial = await chat(456)
        self.assertEqual(modules(initial)['help']['usage']['public_messages'], DEFAULT_PUBLIC_HELP_MESSAGES)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            saved = await client.post('/api/help', json={
                'public_messages': ['在线修改的普通帮助：发送 /hello 测试'],
                'master_messages': ['仅主人可见的自定义帮助标记'],
            })
            self.assertEqual(saved.status_code, 200)
            manager.set_enabled('hello', False)
            _, owner_state = await chat(123)
            self.assertEqual(modules(owner_state)['help']['usage']['master_messages'], saved.json()['master_messages'])
            system, state = await chat(456)
            self.assertEqual(modules(state)['help']['usage']['public_messages'], saved.json()['public_messages'])
            self.assertNotIn('master_messages', modules(state)['help']['usage'])
            self.assertNotIn('仅主人可见的自定义帮助标记', system)
            self.assertFalse(modules(state)['hello']['enabled'])
            await self.record(1)
            await self.f.runtime.full()
            memory_system, memory_state = calls[-1]
            self.assertEqual(modules(memory_state)['help']['usage']['public_messages'], saved.json()['public_messages'])
            self.assertNotIn('仅主人可见的自定义帮助标记', memory_system)
            reset = await client.post('/api/help/reset')
            self.assertEqual(reset.status_code, 200)
            _, restored = await chat(456)
            self.assertEqual(modules(restored)['help']['usage']['public_messages'], reset.json()['public_messages'])

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
            state = host_state(kwargs['system'])
            self.assertEqual(state['conversation'], 'memory')
            self.assertEqual(state['editable_memory_directory'], str(kwargs['cwd'] / 'memory'))
            self.assertEqual(state['formal_memory_directory'], str(self.f.workspace.memory_root))
            self.assertIsNone(state['requester_qq'])
            self.assertIsNone(state['current_scene'])
            for permission in ('owner', 'shell_allowed', 'qq_tools_allowed', 'web_tools_allowed', 'file_transfer_allowed'):
                self.assertFalse(state[permission])
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

    async def test_startup_discards_interrupted_worker_input_but_preserves_artifacts(self):
        temporary = self.f.workspace.root / 'tmp/worker-interrupted.json'
        artifact = self.f.sandbox.scene_dir('group:12') / 'report.md'
        atomic_write(temporary, '["raw excerpts"]')
        atomic_write(artifact, 'persistent output')
        await self.f.runtime.start()
        self.assertFalse(temporary.exists())
        self.assertEqual(artifact.read_text('utf-8'), 'persistent output')

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
            response = self.dispatcher if hasattr(self, 'dispatcher') else self.responses.pop(0)
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
        except (ConnectionResetError, BrokenPipeError):
            pass  # Cancellation deliberately closes a live model stream.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    @staticmethod
    def tool(name, **args):
        import uuid
        return [{'type': 'tool_use', 'id': 'call-' + uuid.uuid4().hex, 'name': name, 'input': args}]

    async def native(self, kind='chat', **kwargs):
        return await self.f.dsh.run(kind, kwargs.pop('prompt', 'native test'), task_id=kwargs.pop('task_id', 'test'), current=lambda: True, handler=kwargs.pop('handler', AsyncMock(return_value={'success': True})), qq='123', scene_dir='scene', system='必读分类规则', role=kwargs.pop('role', 'worker' if kind == 'chat' else 'memory'), **kwargs)

    async def test_native_schedule_tools_manage_fixed_messages(self):
        from src.feature.timing.feature import TimingFeature
        master = SimpleNamespace(is_master=lambda qq: qq == 123)
        timing = TimingFeature(self.f, master, Path(self.temp.name) / 'timing.sqlite3')
        ctx = MessageContext(bot=SimpleNamespace(master_service=master), websocket=None,
            event={'self_id':999}, msg='每天提醒我', message_type='group', user_id=456, group_id=12)
        results = []
        async def handler(name, args):
            result = await timing.tool(ctx, name, args)
            self.assertTrue(result['success'], result)
            results.append((name, result))
            return result
        def respond(request):
            tools = {t['name']:t for t in request['tools']}
            create = tools['schedule_create']['input_schema']
            self.assertIn('kind', create['properties'])
            self.assertIn('content', create['properties'])
            self.assertNotIn('prompt', create.get('required', []))
            self.assertIn('content', tools['schedule_update']['input_schema']['properties'])
            self.assertIn('kind', tools['schedule_list']['input_schema']['properties'])
            if not results:
                return self.tool('schedule_create', kind='fixed', flag='-d', time='08-00-00', content='带钥匙 [CQ:at,qq=789]')
            identifier = results[0][1]['task']['id']
            if len(results) == 1:
                return self.tool('schedule_update', task_id=identifier, content='带钥匙\n[CQ:at,qq=789] 新正文')
            if len(results) == 2:
                return self.tool('schedule_list', kind='fixed')
            if len(results) == 3:
                return self.tool('schedule_delete', task_id=identifier)
            return '固定消息已创建、修改、查询和删除'
        self.dispatcher = respond
        result = await self.native(handler=handler)
        self.assertEqual(result['finish_reason'], 'completed')
        self.assertEqual([name for name, _ in results], ['schedule_create','schedule_update','schedule_list','schedule_delete'])
        self.assertEqual(results[2][1]['tasks'][0]['content'], '带钥匙\n[CQ:at,qq=789] 新正文')
        self.assertEqual(timing.store.list(), [])

    async def test_scheduled_native_fork_inherits_history_without_blocking_and_deletes_logs(self):
        self.responses = ['父对话已完成标记']
        await self.native(prompt='PARENT_COMPLETED_7931', role='main')
        began, release = asyncio.Event(), asyncio.Event()
        target = self.f.workspace.root / 'files/scene/scheduled.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        calls = []
        count = 0
        async def respond(request):
            nonlocal count
            names = {t['name'] for t in request.get('tools', [])}
            if 'write' not in names:
                return '主对话继续响应'
            count += 1
            calls.append(request)
            self.assertNotIn('spawn_task', names)
            self.assertIn('send_message', names)
            self.assertIn('schedule_create', names)
            if count == 1:
                began.set()
                await release.wait()
                return self.tool('write', file_path=str(target), content='文件效果持久保留')
            if count == 2:
                return self.tool('send_message', message='定时消息')
            return '分支完成'
        self.dispatcher = respond
        handler = AsyncMock(return_value={'success': True})
        branch = asyncio.create_task(self.native(prompt='[定时任务]\nCURRENT_SCHEDULE_1973', role='scheduled', task_id='scheduled-test', handler=handler))
        try:
            await asyncio.wait_for(began.wait(), 20)
            files = list((self.f.dsh.home / 'sessions').glob('*/*'))
            branch_dirs = [p for p in files if not p.name.startswith(('chat-', 'memory-'))]
            self.assertEqual(len(branch_dirs), 1)
            main = await asyncio.wait_for(self.native(prompt='LATER_MAIN_3927', role='main', task_id='next'), 10)
            self.assertEqual(main['finish_reason'], 'completed')
            release.set()
            result = await asyncio.wait_for(branch, 20)
            self.assertEqual(result['finish_reason'], 'completed')
            all_text = model_instructions(calls[-1])
            self.assertIn('PARENT_COMPLETED_7931', all_text)
            self.assertIn('CURRENT_SCHEDULE_1973', all_text)
            self.assertNotIn('LATER_MAIN_3927', all_text)
            self.assertEqual(target.read_text('utf-8'), '文件效果持久保留')
            handler.assert_awaited_once_with('send_message', {'message': '定时消息'})
            self.assertTrue(all(not p.exists() for p in branch_dirs))
            self.assertEqual(json.loads((self.f.dsh.home / 'qqbot-branches.json').read_text('utf-8')), {})
            await self.f.dsh.stop()
            self.assertTrue(all(not p.exists() for p in branch_dirs))
        finally:
            release.set()
            if not branch.done():
                branch.cancel()
            await asyncio.gather(branch, return_exceptions=True)

    async def test_scheduled_fork_cancellation_deletes_branch(self):
        began, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            began.set()
            await release.wait()
            return '迟到的结果'
        self.dispatcher = respond
        branch = asyncio.create_task(self.native(prompt='[定时任务]\n可取消任务', role='scheduled', task_id='scheduled-cancel'))
        try:
            await asyncio.wait_for(began.wait(), 20)
            await asyncio.wait_for(self.f.dsh.cancel('scheduled-cancel'), 10)
            result = await asyncio.wait_for(branch, 10)
            self.assertTrue(result.get('cancelled'))
            self.assertEqual(list((self.f.dsh.home / 'sessions').glob('*/*')), list((self.f.dsh.home / 'sessions').glob('*/chat-*')))
        finally:
            release.set()
            if not branch.done():
                branch.cancel()
            await asyncio.gather(branch, return_exceptions=True)

    async def test_scheduled_integrates_pending_memory_without_blocking_main(self):
        from src.feature.timing.feature import TimingFeature
        master = SimpleNamespace(is_master=lambda qq: False)
        timing = TimingFeature(self.f, master, Path(self.temp.name) / 'timing.sqlite3')
        self.f.timing = timing
        await self.f.record('group', '12', '123', '甲', '我住在苏州', message_id='schedule-memory')
        began, release = asyncio.Event(), asyncio.Event()
        counts = {'scheduled': 0, 'memory': 0}
        async def respond(request):
            instructions = model_instructions(request)
            state = host_state(instructions)
            if state['agent_role'] == 'main':
                return '主对话仍然可用'
            role = state['agent_role']
            counts[role] += 1
            if role == 'scheduled':
                if counts[role] == 1:
                    return self.tool('integrate_memory', batches=1)
                return '整理完成'
            self.assertEqual(role, 'memory')
            prompt = next(b['text'] for m in reversed(request['messages']) if m['role'] == 'user' for b in m['content'] if b['type'] == 'text' and 'batch_file' in b['text'])
            batch = json.loads(prompt)['batch']
            if counts[role] == 1:
                began.set()
                await release.wait()
                return self.tool('write', file_path=str(Path(state['editable_memory_directory']) / 'user/123/PERSON.md'), content='- 居住在苏州\n')
            return json.dumps({'completed': True, 'batch': batch})
        self.dispatcher = respond
        bot = SimpleNamespace(reply=AsyncMock(), master_service=master)
        ctx = MessageContext(bot=bot, websocket=None, event={'message': []}, msg='整理新增记忆', raw_msg='整理新增记忆', message_type='group', user_id=456, group_id=12)
        job = {'id': 'memory-maintenance', 'run_id': 'integration', 'scheduled_at': time.time(), 'prompt': '整理新增记忆', 'owner_grant': False, 'delivery': 'silent'}
        branch = asyncio.create_task(self.f.engine.run_scheduled(ctx, job, lambda: True))
        try:
            await asyncio.wait_for(began.wait(), 20)
            await asyncio.wait_for(self.f.engine._turn(ctx, 'group:12', True), 10)
            release.set()
            result = await asyncio.wait_for(branch, 20)
            self.assertEqual(result['finish_reason'], 'completed')
            self.assertEqual(self.f.journal.pending_count(), 0)
            self.assertIn('苏州', self.f.workspace.read_memory('user/123/PERSON.md'))
            self.assertEqual(counts, {'scheduled': 2, 'memory': 2})
        finally:
            release.set()
            if not branch.done():
                branch.cancel()
            await asyncio.gather(branch, return_exceptions=True)

    async def test_cancelling_schedule_also_cancels_memory_transaction(self):
        await self.f.record('group','12','123','甲','我的资料',message_id='cancel-memory')
        began, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            state = host_state(model_instructions(request))
            if state['agent_role'] == 'scheduled':
                return self.tool('integrate_memory', batches=1)
            began.set()
            await release.wait()
            return '不能提交'
        self.dispatcher = respond
        bot = SimpleNamespace(reply=AsyncMock(),master_service=SimpleNamespace(is_master=lambda qq:False))
        ctx=MessageContext(bot=bot,websocket=None,event={'message':[]},msg='整理',message_type='group',user_id=456,group_id=12)
        job={'id':'cancel-integration','run_id':'cancel-integration','scheduled_at':time.time(),'prompt':'整理记忆','owner_grant':False,'delivery':'silent'}
        branch=asyncio.create_task(self.f.engine.run_scheduled(ctx,job,lambda:True))
        try:
            await asyncio.wait_for(began.wait(),20)
            branch.cancel()
            await asyncio.wait_for(asyncio.gather(branch,return_exceptions=True),10)
            async with asyncio.timeout(5):
                while self.f.runtime.lock.locked():
                    await asyncio.sleep(.05)
            self.assertEqual(self.f.journal.pending_count(),1)
            self.assertFalse((self.f.workspace.root/'tmp/memory-stage').exists())
        finally:
            release.set()
            if not branch.done():
                branch.cancel()
            await asyncio.gather(branch,return_exceptions=True)

    async def test_crashed_scheduled_branch_is_removed_on_next_kernel_start(self):
        began, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            if 'write' not in {t['name'] for t in request.get('tools',[])}:
                return '重启后聊天恢复'
            began.set()
            await release.wait()
            return '已中断'
        self.dispatcher=respond
        await self.native(prompt='已保存的父对话历史',role='main')
        branch=asyncio.create_task(self.native(prompt='[定时任务]\n异常退出清理',role='scheduled',task_id='crash-fork'))
        try:
            await asyncio.wait_for(began.wait(),20)
            orphan=list((self.f.dsh.home/'sessions').glob('*/*'))
            orphan=[p for p in orphan if not p.name.startswith(('chat-','memory-'))]
            self.assertEqual(len(orphan),1)
            self.f.dsh.process.kill()
            await self.f.dsh.process.wait()
            await asyncio.wait_for(asyncio.gather(branch,return_exceptions=True),10)
            self.assertTrue(orphan[0].exists())
            result=await self.native(prompt='重启',role='main')
            self.assertEqual(result['finish_reason'],'completed')
            self.assertFalse(orphan[0].exists())
        finally:
            release.set()
            if not branch.done():
                branch.cancel()
            await asyncio.gather(branch,return_exceptions=True)

    async def test_spawn_runs_in_background_and_delivers_files_to_original_scene(self):
        began, release = asyncio.Event(), asyncio.Event()
        counts = {'main': 0, 'worker': 0}
        target = self.f.sandbox.scene_dir('group:12') / 'docker.md'
        async def respond(request):
            tools = {t['name'] for t in request.get('tools', [])}
            worker = 'write' in tools
            role = 'worker' if worker else 'main'
            counts[role] += 1
            if worker:
                self.assertNotIn('spawn_task', tools)
                self.assertNotIn('send_message', tools)
                self.assertIn('必读记忆规则', request['system'])
                self.assertIn('999', request['system'])
                if counts[role] == 1:
                    began.set()
                    await release.wait()
                    return self.tool('write', file_path=str(target), content='# Docker 使用说明')
                if counts[role] == 2:
                    return self.tool('send_file', path=str(target))
                return '已生成docker.md并发送成功。'
            self.assertNotIn('read', tools)
            self.assertNotIn('web_search', tools)
            self.assertNotIn('pwsh', tools)
            if counts[role] == 1:
                return self.tool('spawn_task', title='制作Docker手册', prompt='生成详细说明并发送文件')
            messages = {2:'任务已启动，稍后发给你。', 4:'可以继续聊天。', 6:'手册已完成并发送。'}
            if counts[role] in messages:
                return self.tool('send_message', message=messages[counts[role]])
            return '内部执行记录，不应发送到QQ。'
        self.dispatcher = respond
        bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: qq == 123))
        self.f.workfiles.send = AsyncMock(return_value={'success': True, 'message_id': 'file-1'})
        def context(group, text):
            return MessageContext(bot=bot, event={}, websocket=None, msg=text, message_type='group', user_id=123, group_id=group)
        try:
            await asyncio.wait_for(self.f.engine._turn(context(12, '制作Docker手册'), 'group:12', True), 25)
            await asyncio.wait_for(began.wait(), 10)
            self.assertEqual(len(self.f.engine.background), 1)
            feeds = list((self.f.workspace.root / 'tmp').glob('worker-*.json'))
            self.assertEqual(len(feeds), 1)
            self.assertTrue(feeds[0].exists())
            await asyncio.wait_for(self.f.engine._turn(context(34, '还能聊天吗'), 'group:34', True), 10)
            self.assertFalse(target.exists())
            release.set()
            async with asyncio.timeout(20):
                while self.f.engine.background or self.f.engine.tasks:
                    await asyncio.sleep(.05)
            self.assertEqual(target.read_text('utf-8'), '# Docker 使用说明')
            self.assertEqual(self.f.workfiles.send.await_args.args[0].group_id, 12)
            self.assertEqual([call.args[0].group_id for call in bot.reply.await_args_list], [12, 34, 12])
            self.assertEqual([call.args[1] for call in bot.reply.await_args_list],
                ['任务已启动，稍后发给你。','可以继续聊天。','手册已完成并发送。'])
            self.assertFalse(feeds[0].exists())
            # Background results are data; their delivery turn cannot spawn.
            self.assertNotIn('spawn_task', {t['name'] for t in self.requests[-1]['tools']})
            await self.f.dsh.stop()
            retention = self.f.dsh.home / 'qqbot-retention.json'
            entries = json.loads(retention.read_text('utf-8'))
            self.assertEqual(len(entries), 2)  # parent and one-shot child logs
            atomic_write(retention, json.dumps({identity: 0 for identity in entries}))
            await self.f.dsh.prune()
            self.assertEqual(list((self.f.dsh.home / 'sessions').glob('*/*/*.jsonl')), [])
            self.assertTrue(target.exists())
        finally:
            release.set()

    async def test_main_cannot_execute_even_with_owner_authority(self):
        outside = self.f.workspace.data_root / 'forbidden.txt'
        self.responses = [self.tool('write', file_path=str(outside), content='bad'), 'blocked']
        result = await self.native(owner=True, role='main')
        self.assertEqual(result['finish_reason'], 'completed')
        self.assertFalse(outside.exists())
        self.assertNotIn('write', {t['name'] for t in self.requests[0]['tools']})

    async def test_native_spawn_and_background_delivery_do_not_leak_final_text(self):
        began, release = asyncio.Event(), asyncio.Event()
        main_steps = 0
        async def respond(request):
            nonlocal main_steps
            if 'write' in {t['name'] for t in request.get('tools', [])}:
                began.set()
                await release.wait()
                return '内部结果：检查完毕，按要求不向QQ发言。'
            main_steps += 1
            if main_steps == 1:
                return self.tool('spawn_task', title='静默检查', prompt='检查后返回结果，不发文件或消息')
            return '（主人要求静默，本轮不再发言。）'
        self.dispatcher = respond
        bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: qq == 123))
        ctx = MessageContext(bot=bot, websocket=None, event={}, msg='安静地检查一下',
                             message_type='group', user_id=123, group_id=12)
        try:
            await asyncio.wait_for(self.f.engine._turn(ctx, 'group:12', True), 20)
            await asyncio.wait_for(began.wait(), 10)
            bot.reply.assert_not_awaited()
            release.set()
            async with asyncio.timeout(20):
                while self.f.engine.background or self.f.engine.tasks:
                    await asyncio.sleep(.05)
            self.assertEqual(main_steps, 3)  # Spawn, initial final, result delivery final.
            bot.reply.assert_not_awaited()
            self.assertFalse(list((self.f.workspace.root / 'tmp').glob('worker-*.json')))
        finally:
            release.set()

    async def test_spawn_cancellation_is_scoped_and_disable_stops_native_writes(self):
        for mode in ('cancel', 'disable'):
            began, release = asyncio.Event(), asyncio.Event()
            commands = [self.tool('spawn_task', title='后台测试', prompt='写入测试文件')]
            target = self.f.sandbox.scene_dir('group:12') / (mode + '.txt')
            main_steps = 0
            async def respond(request):
                nonlocal main_steps
                if 'write' in {t['name'] for t in request.get('tools', [])}:
                    began.set()
                    await release.wait()
                    return self.tool('write', file_path=str(target), content='must not execute')
                main_steps += 1
                return commands.pop(0) if commands else '任务状态已说明。'
            self.dispatcher = respond
            bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: qq == 123))
            def context(qq=123, group=12):
                return MessageContext(bot=bot, event={}, websocket=None, msg='任务操作', message_type='group', user_id=qq, group_id=group)
            try:
                await asyncio.wait_for(self.f.engine._turn(context(), 'group:12', True), 20)
                await asyncio.wait_for(began.wait(), 10)
                identifier = next(iter(self.f.engine.background))
                if mode == 'cancel':
                    commands.append(self.tool('cancel_task', task_id=identifier))
                    await self.f.engine._turn(context(456, 34), 'group:34', True)
                    self.assertIn('task_not_found', json.dumps(self.requests[-1]['messages']))
                    self.assertIn(identifier, self.f.engine.background)
                    commands.append(self.tool('cancel_task', task_id=identifier))
                    await self.f.engine._turn(context(), 'group:12', True)
                else:
                    self.f.on_enabled_changed(False)
                    self.f.on_enabled_changed(True)
                    await asyncio.gather(*self.f.engine.cleanup_tasks)
                async with asyncio.timeout(10):
                    while self.f.engine.background:
                        await asyncio.sleep(.05)
                self.assertTrue(self.f.engine.pending.empty())
                release.set()
                await self.f.dsh.stop()
                self.assertFalse(target.exists())
                self.assertEqual(list((self.f.workspace.root / 'tmp').glob('worker-*.json')), [])
            finally:
                release.set()

    async def test_spawn_runtime_crash_reports_failure_without_repeating_work(self):
        began, release = asyncio.Event(), asyncio.Event()
        dispatched = False
        worker_requests = 0
        failure_delivery = None
        async def respond(request):
            nonlocal dispatched, worker_requests, failure_delivery
            if 'write' in {t['name'] for t in request.get('tools', [])}:
                worker_requests += 1
                began.set()
                await release.wait()
                return 'late result'
            if not dispatched:
                dispatched = True
                return self.tool('spawn_task', title='进程中断测试', prompt='执行一次后台工作')
            if failure_delivery is None:
                block = request['messages'][-1]['content'][-1]
                if block.get('type') == 'text':
                    data = json.loads(block['text'])
                    if 'background_result' in data:
                        failure_delivery = data
                        return self.tool('send_message', message='后台任务执行失败，未重复执行。')
            return '（内部状态已处理，不额外发送。）'
        self.dispatcher = respond
        bot = SimpleNamespace(reply=AsyncMock())
        ctx = MessageContext(bot=bot, event={}, websocket=None, msg='后台任务', message_type='group', user_id=123, group_id=12)
        try:
            await self.f.engine._turn(ctx, 'group:12', True)
            await asyncio.wait_for(began.wait(), 10)
            bot.reply.assert_not_awaited()
            process = self.f.dsh.process
            process.kill()
            await process.wait()
            async with asyncio.timeout(20):
                while self.f.engine.background or self.f.engine.tasks:
                    await asyncio.sleep(.05)
            self.assertEqual(worker_requests, 1)
            self.assertIsNotNone(failure_delivery)
            self.assertEqual(failure_delivery['background_result']['state'], 'failed')
            self.assertIn('进程意外退出', failure_delivery['background_result']['output'])
            bot.reply.assert_awaited_once()
            self.assertEqual(bot.reply.await_args.args[0].group_id, 12)
            self.assertEqual(bot.reply.await_args.args[1], '后台任务执行失败，未重复执行。')
            self.assertEqual(list((self.f.workspace.root / 'tmp').glob('worker-*.json')), [])
        finally:
            release.set()

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
        system = model_instructions(self.requests[-1])
        self.assertEqual(host_state(system)['conversation'], 'memory')
        self.assertIn('机器人自身架构、能力与边界', system)

    async def test_native_search_and_global_chat_scene_routing(self):
        self.f.workspace.write_memory('user/123/PERSON.md', '- 大学生')
        self.responses = [self.tool('grep', pattern='大学生', path='memory'), 'found']
        await self.native()
        results = [b for m in self.requests[-1]['messages'] for b in m['content'] if b.get('type') == 'tool_result']
        self.assertFalse(results[-1].get('is_error'))
        self.assertIn('PERSON.md', json.dumps(results[-1]['content'], ensure_ascii=False))
        bot = SimpleNamespace(reply=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: False))
        from src.feature.help.feature import HelpFeature
        from src.feature.manager import FeatureManager
        help_feature = HelpFeature(Path(self.temp.name) / 'help.json', bot.master_service)
        FeatureManager(Path(self.temp.name) / 'features.json', [self.f, help_feature])
        for group in (12, 34):
            help_feature.save_messages([f'群{group}当轮实时帮助'], ['仅主人帮助'])
            await self.f.record('group', str(group), '123', '甲', 'group ' + str(group))
            self.responses = [self.tool('send_message', message='reply ' + str(group)), 'done']
            ctx = MessageContext(bot=bot, event={'self_id': 999}, websocket=None, msg='reply here', message_type='group', user_id=123, group_id=group)
            await self.f.engine._turn(ctx, 'group:' + str(group), True)
            system = model_instructions(self.requests[-1])
            self.assertEqual(host_state(system)['current_scene'], f'group:{group}')
            self.assertIn('send_file(path, name?)', system)
            registered = {item['name']: item for item in host_state(system)['application']['features']}
            self.assertEqual(registered['help']['usage']['public_messages'], [f'群{group}当轮实时帮助'])
            self.assertNotIn('master_messages', registered['help']['usage'])
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
