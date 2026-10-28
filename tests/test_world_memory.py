"""Global public knowledge is durable, searchable and maintained as memory."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.feature.ai.admin import AiAdmin
from src.feature.ai.feature import AiFeature
from src.feature.ai.self_context import self_context
from src.feature.ai.storage import MEMORY_AREAS, Workspace, atomic_write
from src.feature.ai.transactions import MemoryTransaction
from tests.test_group_memory import Collection


class WorldStorageTests(unittest.TestCase):
    def test_nested_knowledge_discovery_assets_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path='world/科技/人工智能/模型进展.md'
            atomic_write(root/'workspace/memory'/path,'- 公开知识与来源')
            w=Workspace(root)
            self.assertIn(path,w.files())
            w.write_memory('world/历史/航海.md','- 航海知识')
            atomic_write(w.memory_root/'world/资料/data.csv','value\n1')
            self.assertNotIn('world/资料/data.csv',w.files())
            self.assertNotIn('公开知识',w.soul_prompt())
            for bad in ['world/../soul/坏.md','world/a/../../坏.md','world//坏.md',
                        'world/坏.txt','world/坏\n名.md','world/..\\坏.md','common/自建.md']:
                with self.subTest(path=bad),self.assertRaises(ValueError):w.memory_path(bad)
            self.assertIn(path,Workspace(root).files())

    def test_world_maintenance_merge_delete_conflict_and_assets_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Workspace(Path(temp))
            first,old='world/科技/资料.md/知识.md','world/科技/旧知识.md'
            w.write_memory(first,'- 原有知识')
            w.write_memory(old,'- 重复知识')
            asset=w.memory_root/'world/科技/source.csv'
            atomic_write(asset,'source\nexample')
            t=MemoryTransaction(w,'world-merge')
            self.assertTrue((t.root/'memory'/first).exists())
            self.assertFalse((t.root/'memory/world/科技/source.csv').exists())
            atomic_write(t.root/'memory'/first,'- 合并的知识；保留公开来源和核验日期')
            (t.root/'memory'/old).unlink()
            self.assertEqual(set(t.commit()),{first,old})
            t.close()
            self.assertFalse(w.memory_path(old).exists())
            self.assertEqual(asset.read_text('utf-8'),'source\nexample')
            t=MemoryTransaction(w,'world-conflict')
            atomic_write(t.root/'memory'/first,'- 过期副本')
            w.write_memory(first,'- 并发更新')
            with self.assertRaises(ValueError):t.commit()
            self.assertEqual(w.read_memory(first),'- 并发更新')
            t.close()

    def test_standalone_search_helper_finds_nested_world_markdown_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for path in ['world/科技/模型.md','world/资料.csv','raw/原文.md','common/非法.md']:
                atomic_write(root/path,'知识条目')
            script=Path('src/feature/ai/skills/memory-management/scripts/search.py').resolve()
            result=subprocess.run([sys.executable,'-X','utf8',str(script),str(root),'知识'],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual([r['path'] for r in json.loads(result.stdout)],['world/科技/模型.md'])


class WorldFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.f=AiFeature(Path(self.temp.name))
        self.f.config.save({'vector_index_enabled':False,'memory_auto_learn':False})

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_world_vector_text_search_scope_updates_and_deletion(self):
        f=self.f
        world='world/科技/人工智能/模型.md'
        other='user/123/PERSON.md'
        f.workspace.write_memory(world,'- 知识：公开模型资料，核验日期2026-10-28')
        f.workspace.write_memory(other,'- 知识：用户的个人背景')
        f.workspace.save_state('index',{'dirty':False,'model':f.config.load().embedding_model,'hashes':{}})
        self.assertEqual({h['source'] for h in (await f.query_memory('知识'))['memories']},{world,other})
        self.assertEqual([h['source'] for h in (await f.query_memory('知识',scope='world',target_qq='999'))['memories']],[world])
        collections={area:Collection() for area in MEMORY_AREAS}
        f.config.save({'vector_index_enabled':True})
        f.index._model_name=f.config.load().embedding_model
        with patch.object(f.index,'_collection',side_effect=lambda area:collections[area]),patch.object(f.index,'_encode',side_effect=lambda texts,**kw:[[1.,0.] for _ in texts]):
            self.assertEqual([h['source'] for h in (await f.query_memory('知识',scope='world'))['memories']],[world])
            self.assertTrue(collections['world'].count())
            self.assertEqual({m['qq'] for m in collections['world'].rows.values()},{''})
            self.assertEqual({h['source'] for h in (await f.query_memory('知识'))['memories']},{world,other})
            f.workspace.write_memory(world,'- 知识已核实更新')
            self.assertEqual((await f.query_memory('知识',scope='world'))['memories'][0]['content'],'- 知识已核实更新')
            f.workspace.memory_path(world).unlink()
            self.assertEqual((await f.query_memory('知识',scope='world'))['memories'],[])
            self.assertEqual(collections['world'].count(),0)

    async def test_admin_world_nested_crud_search_and_invalid_titles(self):
        app=FastAPI()
        AiAdmin(self.f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/api/ai/file/create',json={'category':'WORLD','title':'科技/人工智能/模型.md'})
            self.assertEqual(response.status_code,200,response.text)
            data=response.json()
            self.assertEqual(data['path'],'world/科技/人工智能/模型.md')
            self.assertEqual(data['content'],'# 模型\n\n')
            listing=(await client.get('/api/ai/files')).json()['items']
            item=next(item for item in listing if item['path']==data['path'])
            self.assertEqual((item['category'],item['qq'],item['group_id']),('WORLD','',''))
            for title in ['', '../知识','科技/../../知识','科技//知识','/知识','坏\\知识','坏\n知识','.md','..']:
                bad=await client.post('/api/ai/file/create',json={'category':'WORLD','title':title})
                self.assertEqual(bad.status_code,409,(title,bad.text))
            saved=await client.post('/api/ai/file',json={'path':data['path'],'content':'- 世界知识笔记','expected_hash':data['hash']})
            self.assertEqual(saved.status_code,200,saved.text)
            query=(await client.post('/memory/query',json={'query':'世界知识','scope':'world'})).json()
            self.assertEqual(query['memories'][0]['source'],data['path'])
            conflict=await client.post('/api/ai/file/delete',json={'path':data['path'],'expected_hash':data['hash']})
            self.assertEqual(conflict.status_code,409)
            deleted=await client.post('/api/ai/file/delete',json={'path':data['path'],'expected_hash':saved.json()['hash']})
            self.assertEqual(deleted.status_code,200)
            self.assertNotIn(data['path'],(await client.get('/api/ai/files')).json()['files'])

    async def test_custom_skill_upgrade_teaches_world_without_injecting_knowledge(self):
        skill=self.f.workspace.root/'.agents/skills/memory-management/SKILL.md'
        atomic_write(skill,'# 自定义规则\n保留这段内容\n')
        self.f.install_memory_skill()
        content=skill.read_text('utf-8')
        self.assertIn('保留这段内容',content)
        self.assertIn('memory/world/',content)
        self.assertIn('scope="world"',content)
        self.f.workspace.write_memory('world/自学.md','UNIQUE_WORLD_KNOWLEDGE_BODY')
        self.assertNotIn('UNIQUE_WORLD_KNOWLEDGE_BODY',self_context(self.f,'chat'))
