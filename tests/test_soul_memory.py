"""Persona migration, maintenance transactions and the built-in daily schedule."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.feature.ai.admin import AiAdmin
from src.feature.ai.feature import AiFeature
from src.feature.ai.self_context import self_context
from src.feature.ai.storage import Workspace, atomic_write, digest
from src.feature.ai.transactions import MemoryTransaction, recover
from src.feature.timing.admin import TimingAdmin
from src.feature.timing.feature import TimingFeature, MEMORY_COMPRESSION_ID, MEMORY_SCENE
from src.feature.timing.schedule import BJT


class SoulMigrationTests(unittest.TestCase):
    def test_custom_persona_moves_once_and_deletion_does_not_recreate_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / 'workspace/AGENT.md'
            atomic_write(legacy, '旧的自定义人格\n')
            w = Workspace(root)
            self.assertEqual(w.read_memory('soul/AGENT.md'), '旧的自定义人格\n')
            self.assertEqual((root / '.state/legacy-AGENT.md').read_text('utf-8'), '旧的自定义人格\n')
            self.assertFalse(legacy.exists())
            w.delete_memory('soul/AGENT.md', digest('旧的自定义人格\n'))
            self.assertEqual(Workspace(root).soul_prompt(), '')

    def test_existing_soul_wins_and_legacy_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_write(root / 'workspace/AGENT.md', 'old')
            atomic_write(root / 'workspace/memory/soul/AGENT.md', 'new')
            w = Workspace(root)
            self.assertEqual(w.read_memory('soul/AGENT.md'), 'new')
            self.assertEqual((root / '.state/legacy-AGENT.md').read_text('utf-8'), 'old')

    def test_tracked_deletions_conflict_and_recover_without_touching_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            w = Workspace(Path(temporary))
            name = 'group/12/重复主题.md'
            w.write_memory(name, 'still valuable')
            raw = w.memory_root / 'raw/original.json'
            atomic_write(raw, 'original')
            tx = MemoryTransaction(w, 'delete-test')
            (tx.root / 'memory' / name).unlink()
            w.write_memory(name, 'concurrent update')
            with self.assertRaisesRegex(ValueError, '其他写入'):
                tx.commit()
            tx.close()
            self.assertEqual(w.read_memory(name), 'concurrent update')
            tx = MemoryTransaction(w, 'delete-test-2')
            (tx.root / 'memory' / name).unlink()
            self.assertIn(name, tx.commit())
            tx.close()
            self.assertFalse(w.memory_path(name).exists())
            atomic_write(w.data_root / '.state/memory-commit.json', json.dumps({'batch': 'recover-delete',
                'changes': [{'path': 'soul/AGENT.md', 'delete': True}]}))
            recover(w)
            recover(w)
            self.assertFalse(w.persona_path.exists())
            self.assertEqual(raw.read_text('utf-8'), 'original')


class SoulRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ai = AiFeature(self.root / 'ai')
        self.ai.config.save({'vector_index_enabled': False, 'memory_auto_learn': False})
        self.timing = TimingFeature(self.ai, SimpleNamespace(is_master=lambda qq: False), self.root / 'timing.sqlite3')

    async def asyncTearDown(self):
        await self.timing.stop()
        await self.ai.stop()
        self.temp.cleanup()

    async def test_automatic_memory_failure_backs_off_and_manual_retry_recovers(self):
        runtime = self.ai.runtime
        await self.ai.record('group', '12', '123', '甲', '待整理内容', message_id='retry')
        self.ai.config.save({'memory_auto_learn': True})
        runtime.running = True
        runtime.MEDIA_DEBOUNCE_SECONDS = 0
        # Exercise automatic scheduling without generating 100 raw records.
        runtime.ready = lambda: self.ai.journal.pending_count() > 0
        self.ai.dsh.run = AsyncMock(side_effect=RuntimeError('MEMORY_STAGE_INIT_TEST'))
        clock = 100.0
        for attempt, delay in enumerate([60, 120, 240, 480, 960, 1800, 1800], 1):
            with patch('src.feature.ai.runtime.time', SimpleNamespace(monotonic=lambda: clock)):
                runtime.trigger()
                with self.assertLogs('src.feature.ai.runtime', level='ERROR'):
                    await runtime.worker
                self.assertEqual(runtime.failure_count, attempt)
                self.assertEqual(runtime.retry_after, clock + delay)
                self.assertEqual(self.ai.journal.pending_count(), 1)
                previous = runtime.worker
                for _ in range(5):
                    runtime.trigger()
                self.assertIs(runtime.worker, previous)
            clock += delay
        self.assertEqual(self.ai.dsh.run.await_count, 7)
        # An explicit admin/tool retry bypasses the automatic cooldown.
        self.ai.dsh.run.side_effect = None
        self.ai.dsh.run.return_value = {'finish_reason': 'completed', 'final_response': '{"completed":true,"batch":"1-1"}'}
        result = await runtime.full()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(self.ai.journal.pending_count(), 0)
        self.assertEqual((runtime.failure_count, runtime.retry_after, runtime.last_error), (0, 0.0, ''))

    async def test_all_roles_reload_all_soul_files_without_vector_indexing(self):
        w = self.ai.workspace
        w.write_memory('soul/notes/相处.md', 'SOUL_MARKER_ONE')
        self.assertNotIn('soul/notes/相处.md', w.files())
        self.assertIn('soul/notes/相处.md', w.files(include_soul=True))
        for role in ['main', 'worker', 'scheduled', 'memory']:
            self.assertIn('SOUL_MARKER_ONE', self_context(self.ai, 'memory' if role == 'memory' else 'chat', role=role))
        w.write_memory('soul/notes/相处.md', 'SOUL_MARKER_TWO')
        context = self_context(self.ai, 'chat')
        self.assertIn('SOUL_MARKER_TWO', context)
        self.assertNotIn('SOUL_MARKER_ONE', context)
        for path in ['soul/../../outside.md', 'soul/../evil.md', 'soul/a.txt', 'soul/a\n.md']:
            with self.assertRaises(ValueError):
                w.write_memory(path, 'bad')

    async def test_compression_scans_even_with_empty_queue_and_publishes_deletes(self):
        w = self.ai.workspace
        name = 'group/12/冗余.md'
        w.write_memory(name, 'old')
        w.save_state('memory-batch-receipt', {'batch': 'unacknowledged-batch'})
        async def run(kind, prompt, **kwargs):
            data = json.loads(prompt)
            self.assertEqual(kind, 'memory')
            self.assertEqual(kwargs['role'], 'memory')
            self.assertIn(name, json.loads(Path(data['inventory_file']).read_text('utf-8')))
            self.assertIn('或向主对话汇报', kwargs['system'])
            self.assertIn('soul/AGENT.md', kwargs['system'])
            for path in ['raw/original.json', '../AGENT.md']:
                with self.assertRaises(ValueError):
                    await kwargs['handler']('delete_memory_file', {'path': path})
            result = await kwargs['handler']('delete_memory_file', {'path': name})
            self.assertTrue(result['staged'])
            self.assertTrue(w.memory_path(name).exists())
            atomic_write(kwargs['cwd'] / 'memory/soul/AGENT.md', '成长后的人格')
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': data['batch']})}
        self.ai.dsh.run = run
        result = await self.ai.runtime.compact()
        self.assertIn(name, result['updated_files'])
        self.assertFalse(w.memory_path(name).exists())
        self.assertEqual(w.read_memory('soul/AGENT.md'), '成长后的人格')
        self.assertEqual(self.ai.journal.pending_count(), 0)
        self.assertEqual(w.load_state('memory-batch-receipt', {})['batch'], 'unacknowledged-batch')
        self.assertFalse((w.root / 'tmp/memory-stage').exists())

    async def test_default_bjt_schedule_runs_offline_and_admin_delete_persists(self):
        f = self.timing
        now = datetime(2026, 10, 21, 1, 59, tzinfo=BJT).timestamp()
        with patch('src.feature.timing.storage.time.time', return_value=now):
            f.install_memory_task()
        job = f.store.get(MEMORY_COMPRESSION_ID)
        self.assertEqual(job['scene'], MEMORY_SCENE)
        self.assertEqual(datetime.fromtimestamp(job['next_run'], BJT).strftime('%H:%M:%S'), '02:00:00')
        self.ai.config.save({'chat_enabled': False})
        self.ai.runtime.compact = AsyncMock(return_value={'success': True, 'updated_files': []})
        f.bot = None
        with patch('src.feature.timing.feature.time.time', return_value=job['next_run'] + 1):
            await f.tick()
            await asyncio.gather(*(task for _, task in f.running.values()))
        self.ai.runtime.compact.assert_awaited_once()
        self.assertEqual(f.store.list()[0]['last_run']['status'], 'completed')
        app = FastAPI()
        TimingAdmin(f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = (await client.get('/api/timing/tasks')).json()['tasks'][0]
            self.assertTrue(result['system_maintenance'])
            revision = result['revision']
            response = await client.put('/api/timing/tasks/' + result['id'], json={'revision': revision, 'enabled': False, 'time': '3-0-0'})
            self.assertEqual(response.status_code, 200)
            f.install_memory_task()
            paused = f.store.get(result['id'])
            self.assertFalse(paused['enabled'])
            self.assertEqual(paused['time'], '3-0-0')
            rejected = await client.put('/api/timing/tasks/' + result['id'], json={'revision': paused['revision'], 'delivery': 'message'})
            self.assertEqual(rejected.status_code, 400)
            deleted = await client.delete('/api/timing/tasks/' + result['id'], params={'revision': paused['revision']})
            self.assertEqual(deleted.status_code, 200)
        f.install_memory_task()
        self.assertIsNone(f.store.get(MEMORY_COMPRESSION_ID))

    async def test_admin_soul_management_and_persona_share_one_source(self):
        app = FastAPI()
        AiAdmin(self.ai).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/ai/file/create', json={'category': 'SOUL', 'title': '边界'})
            self.assertEqual(response.status_code, 200)
            items = (await client.get('/api/ai/files')).json()['items']
            self.assertEqual({item['category'] for item in items if item['path'].startswith('soul/')}, {'SOUL'})
            response = await client.post('/api/ai/config', json={'agent_prompt': 'admin人格'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.ai.workspace.read_memory('soul/AGENT.md'), 'admin人格')
            self.ai.workspace.write_memory('soul/AGENT.md', '自主成长后的人格')
            self.assertEqual((await client.get('/api/ai/config')).json()['agent_prompt'], '自主成长后的人格')
            response = await client.post('/api/ai/config', json={'agent_prompt': 'stale', 'agent_prompt_hash': digest('admin人格')})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.ai.workspace.read_memory('soul/AGENT.md'), '自主成长后的人格')
            self.ai.workspace.delete_memory('soul/AGENT.md', digest('自主成长后的人格'))
            response = await client.post('/api/ai/config', json={'agent_prompt': '', 'agent_prompt_hash': digest('')})
            self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
