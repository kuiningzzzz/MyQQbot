"""Group memories participate in the same storage, retrieval and commit lifecycle."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.feature.ai.admin import AiAdmin
from src.feature.ai.feature import AiFeature
from src.feature.ai.storage import Workspace, atomic_write
from src.feature.ai.transactions import MemoryTransaction
from src.feature.atom.context import MessageContext


class Collection:
    """Small in-memory Chroma double; exercises the real indexing pipeline."""
    def __init__(self):
        self.rows = {}

    def get(self):
        return {'ids': list(self.rows)}

    def delete(self, ids=None, where=None):
        for identifier, row in list(self.rows.items()):
            if identifier in (ids or []) or (where and all(row[k] == v for k, v in where.items())):
                del self.rows[identifier]

    def upsert(self, ids, documents, metadatas, embeddings):
        for identifier, metadata in zip(ids, metadatas):
            self.rows[identifier] = metadata

    def count(self):
        return len(self.rows)

    def query(self, query_embeddings, n_results, where=None):
        rows = [row for row in self.rows.values() if not where or all(row[k] == v for k, v in where.items())][:n_results]
        return {'metadatas': [rows], 'distances': [[0.05] * len(rows)]}


class GroupStorageTests(unittest.TestCase):
    def test_existing_group_files_and_invalid_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            path = 'group/158132790/群规.md'
            # Files written by older owner tasks must be discovered in place.
            atomic_write(workspace.memory_root / path, '- 每周讨论项目')
            self.assertIn(path, workspace.files())
            self.assertIn('项目', workspace.read_memory(path))
            for bad in ['group/not-a-number/群规.md', 'group/１２/群规.md', 'group/12/nested/群规.md',
                        'group/12/../群规.md', 'group/12/群规.txt', 'group/12/..\\群规.md',
                        'group/12/坏\n名.md', 'common/自建.md']:
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    workspace.memory_path(bad)

    def test_transaction_copies_commits_and_protects_group_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            path = 'group/12/项目.md'
            workspace.write_memory(path, '旧进展')
            transaction = MemoryTransaction(workspace, '1-100')
            self.assertEqual((transaction.root / 'memory' / path).read_text('utf-8'), '旧进展')
            atomic_write(transaction.root / 'memory' / path, '新进展')
            self.assertEqual(transaction.commit(), [path])
            self.assertEqual(workspace.read_memory(path), '新进展')
            transaction.close()
            transaction = MemoryTransaction(workspace, '101-200')
            atomic_write(transaction.root / 'memory' / path, '暂存更改')
            workspace.write_memory(path, '并发更改')
            with self.assertRaises(ValueError):
                transaction.commit()
            self.assertEqual(workspace.read_memory(path), '并发更改')
            transaction.close()
            transaction = MemoryTransaction(workspace, '101-200')
            atomic_write(transaction.root / 'memory' / path, '不得部分提交')
            atomic_write(transaction.root / 'memory/group/12/nested/非法.md', '非法')
            with self.assertRaises(ValueError):
                transaction.commit()
            self.assertEqual(workspace.read_memory(path), '并发更改')
            transaction.close()

    def test_search_helper_includes_only_formal_group_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ['group/12/群规.md', 'group/12/nested/漏网.md', 'raw/原文.md', 'common/自建.md']:
                atomic_write(root / path, '项目')
            script = Path('src/feature/ai/skills/memory-management/scripts/search.py').resolve()
            result = subprocess.run([sys.executable, '-X', 'utf8', str(script), str(root), '项目'], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([row['path'] for row in json.loads(result.stdout)], ['group/12/群规.md'])


class GroupFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999'})

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_vector_and_text_search_filter_update_delete_and_fallback(self):
        f = self.f
        paths = ['group/12/项目.md', 'group/34/项目.md', 'user/123/PREFERENCE.md', 'common/GLOSSARY.md']
        for path in paths:
            f.workspace.write_memory(path, '- 项目背景 ' + path)
        for scope, target, expected in [('auto', None, set(paths)), ('group', '12', {paths[0]}), ('group', '34', {paths[1]})]:
            result = await f.query_memory('项目', scope=scope, target_group=target)
            self.assertEqual({hit['source'] for hit in result['memories']}, expected)
        for target in ['../12', '１２', '']:
            with self.assertRaises(ValueError):
                await f.query_memory('项目', scope='group', target_group=target)
        f.config.save({'vector_index_enabled': True})
        collections = {scope: Collection() for scope in ('user', 'group', 'common', 'world')}
        f.index._model_name = f.config.load().embedding_model
        with patch.object(f.index, '_collection', side_effect=lambda scope: collections[scope]), patch.object(f.index, '_encode', side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts]):
            result = await f.query_memory('项目', scope='group', target_group='12')
            self.assertEqual({hit['source'] for hit in result['memories']}, {paths[0]})
            self.assertEqual({row['group_id'] for row in collections['group'].rows.values()}, {'12', '34'})
            self.assertEqual({hit['source'] for hit in (await f.query_memory('项目'))['memories']}, set(paths))
            f.workspace.write_memory(paths[0], '- 项目最新进展')
            self.assertEqual((await f.query_memory('项目', scope='group', target_group='12'))['memories'][0]['content'], '- 项目最新进展')
            f.workspace.memory_path(paths[1]).unlink()
            self.assertEqual((await f.query_memory('项目', scope='group', target_group='34'))['memories'], [])
            self.assertEqual({row['group_id'] for row in collections['group'].rows.values()}, {'12'})
        with self.assertLogs('src.feature.ai.index', level='ERROR'), patch.object(f.index, '_encode', side_effect=RuntimeError('unavailable')):
            self.assertEqual({hit['source'] for hit in (await f.query_memory('项目', scope='group', target_group='12'))['memories']}, {paths[0]})

    async def test_host_binds_group_authority_to_actual_event_for_chat_and_schedule(self):
        groups = []
        async def run(kind, prompt, **kwargs):
            groups.append(kwargs['group_id'])
            return {'finish_reason': 'completed'}
        self.f.dsh.run = run
        bot = SimpleNamespace(master_service=SimpleNamespace(is_master=lambda qq: False))
        for scene_type, group, key in [('group', 12, 'group:12'), ('private', None, 'private:123')]:
            ctx = MessageContext(bot=bot, websocket=None, event={}, msg='测试', message_type=scene_type, user_id=123, group_id=group)
            await self.f.engine._turn(ctx, key, True)
            await self.f.engine.run_scheduled(ctx, {'id': 'job', 'run_id': 'run', 'scheduled_at': 1,
                'prompt': '检查群记忆', 'owner_grant': False, 'delivery': 'silent'}, lambda: True)
        self.assertEqual(groups, ['12', '12', '', ''])

    async def test_admin_group_crud_and_query(self):
        app = FastAPI()
        AiAdmin(self.f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/ai/file/create', json={'category': 'GROUP', 'group_id': '158132790', 'title': '群规'})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data['path'], 'group/158132790/群规.md')
            item = next(item for item in (await client.get('/api/ai/files')).json()['items'] if item['path'] == data['path'])
            self.assertEqual((item['category'], item['group_id'], item['title'], item['qq']), ('GROUP', '158132790', '群规', ''))
            for group, title in [('bad', '群规'), ('１２', '群规'), ('12', 'nested/群规'), ('12', '坏\n名'), ('12', '')]:
                rejected = await client.post('/api/ai/file/create', json={'category': 'GROUP', 'group_id': group, 'title': title})
                self.assertEqual(rejected.status_code, 409, rejected.text)
            saved = await client.post('/api/ai/file', json={'path': data['path'], 'content': '- 项目每周交流', 'expected_hash': data['hash']})
            self.assertEqual(saved.status_code, 200, saved.text)
            query = await client.post('/memory/query', json={'query': '项目', 'scope': 'group', 'target_group': '158132790'})
            self.assertEqual(query.json()['memories'][0]['source'], data['path'])
            conflict = await client.post('/api/ai/file/delete', json={'path': data['path'], 'expected_hash': data['hash']})
            self.assertEqual(conflict.status_code, 409)
            deleted = await client.post('/api/ai/file/delete', json={'path': data['path'], 'expected_hash': saved.json()['hash']})
            self.assertEqual(deleted.status_code, 200)
            self.assertNotIn(data['path'], (await client.get('/api/ai/files')).json()['files'])

    async def test_maintenance_publishes_group_memory_and_acknowledges_queue(self):
        await self.f.record('group', '12', '123', '甲', '周末集体读书', message_id='1')
        async def run(kind, prompt, **kwargs):
            payload = json.loads(prompt)
            self.assertIn('先读取memory-management SKILL', kwargs['system'])
            self.assertNotIn('├─ group/', kwargs['system'])
            atomic_write(kwargs['cwd'] / 'memory/group/12/共同活动.md', '- 周末集体读书')
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': payload['batch']})}
        self.f.dsh.run = run
        result = await self.f.runtime.full()
        self.assertEqual(result['updated_files'], ['group/12/共同活动.md'])
        self.assertEqual(self.f.journal.pending_count(), 0)
        self.assertIn('读书', self.f.workspace.read_memory('group/12/共同活动.md'))

    async def test_upgrade_preserves_custom_skill_and_refreshes_managed_rules(self):
        from src.feature.ai.self_context import self_context
        skill = self.f.workspace.root / '.agents/skills/memory-management/SKILL.md'
        script = skill.parent / 'scripts/search.py'
        atomic_write(skill, '# 我的自定义证据规则\n- 保留这条微调\n')
        atomic_write(script, '# 我的旧搜索脚本\n')
        self.f.install_memory_skill()
        upgraded = skill.read_text('utf-8')
        self.assertIn('保留这条微调', upgraded)
        self.assertIn('group/<群号>', upgraded)
        self.assertEqual(script.with_name('search.py.pre-upgrade.bak').read_text('utf-8'), '# 我的旧搜索脚本\n')
        self.f.install_memory_skill()
        self.assertEqual(skill.read_text('utf-8'), upgraded)
        self.assertEqual(upgraded.count('qqbot:memory-layout:start'), 1)
        # The full layout stays in the SKILL, rather than repeating every turn.
        self.assertIn('不会每轮注入SKILL全文', upgraded)
        for kind in ('chat', 'memory'):
            context = self_context(self.f, kind)
            self.assertNotIn('【记忆目录规范】', context)
            self.assertNotIn('├─ group/', context)
            self.assertNotIn('不会进入向量索引', context)


if __name__ == '__main__':
    unittest.main()
