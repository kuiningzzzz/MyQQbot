"""Sticker catalogue, NapCat protocol and task/scene boundaries without real QQ."""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from PIL import Image

from src.feature.ai.feature import AiFeature
from src.feature.ai.media import MediaDescription
from src.feature.ai.sticker_admin import register
from src.feature.ai.sticker_store import StickerStore
from src.feature.ai.stickers import inspect_image
from src.feature.atom.context import MessageContext


def png(color='red'):
    stream = io.BytesIO()
    Image.new('RGB', (12, 12), color).save(stream, 'PNG')
    return stream.getvalue()


class StickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'bot_qq': '999', 'memory_auto_learn': False, 'vector_index_enabled': False})
        self.lib = self.f.stickers
        self.favs, self.calls = [], []
        self.transferred = b''
        self.owner = True
        self.bot = SimpleNamespace(_self_ids={'ws': '999'}, connected_clients={'ws'},
            call_action=AsyncMock(side_effect=self.rpc), reply=AsyncMock(),
            master_service=SimpleNamespace(is_master=lambda qq: qq == 123 and self.owner))
        self.lib.bot = self.bot
        self.ctx = MessageContext(bot=self.bot, websocket='ws', event={'self_id': 999, 'message_id': 10},
            msg='收藏图片', message_type='group', group_id=12, user_id=123)
        self.data = png()
        self.file = self.f.workspace.root / 'test.png'
        self.file.write_bytes(self.data)

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def rpc(self, action, args, ws):
        self.calls.append((action, args, ws))
        if action == 'fetch_custom_face_detail':
            return [dict(item) for item in self.favs[:args['count']]]
        if action == 'download_file':
            self.transferred = base64.b64decode(args['base64'])
            return {'file': '/only-in-napcat/upload.png'}
        if action == 'add_custom_face':
            self.assertEqual(args['file'], '/only-in-napcat/upload.png')
            self.assertEqual(args['md5'], hashlib.md5(self.transferred).hexdigest())
            self.favs.append({'md5': args['md5'], 'resId': 'resource1', 'emoId': 7, 'desc': '', 'url': 'https://example.com/img'})
            return {'result': 0}
        if action == 'set_custom_face_desc':
            self.assertEqual(args['emoji_id'], 7)
            for item in self.favs:
                if item['resId'] == args['res_id']:
                    item['desc'] = args['desc']
            return {'result': 0}
        if action == 'delete_custom_face':
            self.favs[:] = [i for i in self.favs if i['resId'] not in args['ids']]
            return {'result': 0}
        if action in {'send_group_msg', 'send_private_msg'}:
            return {'message_id': 500}
        raise AssertionError(action)

    async def add(self, **kwargs):
        result = await self.lib.manage('add', {'path': str(self.file), 'qq': False, **kwargs}, ctx=self.ctx)
        self.assertTrue(result['success'], result)
        return result['sticker']

    def handler(self, **kwargs):
        task = {'attempts': {}, 'sent': 0, 'unknown': set(), **kwargs}
        return self.f.engine.make_handler(self.ctx, 'group:12', True, task, lambda: True), task

    async def test_native_crud_uses_resource_ids_and_cross_container_transfer(self):
        item = await self.add(qq=True, name='点赞', tags=['赞同'])
        self.assertTrue(item['remote_present'])
        self.assertEqual(self.transferred, self.data)
        updated = await self.lib.manage('update', {'id': item['id'], 'description': '表示赞同', 'qq': True,
            'expected_revision': item['revision']}, ctx=self.ctx)
        self.assertTrue(updated['success'])
        self.assertEqual(self.favs[0]['desc'], '表示赞同')
        result = await self.lib.manage('delete', {'id': item['id'], 'scope': 'qq'}, ctx=self.ctx)
        self.assertTrue(result['success'])
        self.assertFalse(self.favs)
        self.assertTrue(self.lib.store.get(item['id'])['asset'])
        self.assertFalse(self.lib.store.get(item['id'])['deleted'])

    async def test_duplicate_account_scope_revision_and_local_tombstone(self):
        item = await self.add(name='手工名称')
        again = await self.add(tags=['猫'])
        self.assertEqual(item['id'], again['id'])
        self.assertEqual(again['name'], '手工名称')
        with self.assertRaisesRegex(ValueError, '更新'):
            await self.lib.manage('update', {'id': item['id'], 'expected_revision': item['revision']}, ctx=self.ctx)
        self.ctx.event['self_id'] = 888
        with self.assertRaisesRegex(ValueError, '不属于'):
            await self.lib.send(self.ctx, item['id'])
        self.ctx.event['self_id'] = 999
        await self.lib.manage('delete', {'id': item['id'], 'scope': 'local'}, ctx=self.ctx)
        self.favs = [{'md5': hashlib.md5(self.data).hexdigest(), 'resId': 'resource1', 'emoId': 7}]
        await self.lib.sync(ctx=self.ctx)
        self.assertTrue(self.lib.store.get(item['id'])['deleted'])
        self.assertFalse(self.lib.search('999'))
        self.assertTrue((await self.add())['enabled'])

    async def test_sync_expands_count_and_preserves_catalogue_on_failure(self):
        self.favs = [{'md5': f'{i:032x}', 'resId': f'res{i}', 'emoId': i, 'desc': '原始描述'} for i in range(60)]
        await self.lib.sync(ctx=self.ctx)
        self.assertEqual([a[1]['count'] for a in self.calls], [48, 96])
        self.assertEqual(len(self.lib.store.list('999')), 60)
        self.bot.call_action.side_effect = ValueError('network')
        with self.assertRaises(ValueError):
            await self.lib.sync(ctx=self.ctx)
        self.assertTrue(all(r['remote_present'] for r in self.lib.store.list('999')))
        self.bot.call_action.side_effect = self.rpc
        self.favs = [{'md5': 'bad', 'resId': 'broken'}]
        result = await self.lib.sync(ctx=self.ctx)
        self.assertFalse(result['complete'])
        self.assertTrue(all(r['remote_present'] for r in self.lib.store.list('999')))

    async def test_unknown_write_no_replay_and_sync_reconciles(self):
        original = self.rpc
        async def timeout_after_add(action, args, ws):
            result = await original(action, args, ws)
            if action == 'add_custom_face':
                raise TimeoutError()
            return result
        self.bot.call_action.side_effect = timeout_after_add
        first = await self.lib.manage('add', {'path': str(self.file)}, ctx=self.ctx)
        self.assertEqual(first['code'], 'unknown')
        again = await self.lib.manage('add', {'path': str(self.file)}, ctx=self.ctx)
        self.assertEqual(again['code'], 'unknown')
        self.assertEqual(sum(a[0] == 'add_custom_face' for a in self.calls), 1)
        self.bot.call_action.side_effect = original
        await self.lib.sync(ctx=self.ctx)
        self.assertIsNone(self.lib.store.get(first['sticker_id'])['pending'])
        self.assertTrue(self.lib.store.get(first['sticker_id'])['remote_present'])

    async def test_remote_mutations_refresh_stale_catalogue(self):
        item = await self.add(qq=True)
        self.favs.clear()  # Removed through the QQ client, local cache still says present.
        await self.add(qq=True)
        self.assertEqual(len(self.favs), 1)
        await self.lib.sync(ctx=self.ctx)
        self.favs[0]['resId'] = 'new-native-id'
        deleted = await self.lib.manage('delete', {'id': item['id'], 'scope': 'qq'}, ctx=self.ctx)
        self.assertTrue(deleted['success'])
        self.assertEqual(next(args for name, args, _ in reversed(self.calls) if name == 'delete_custom_face')['ids'], ['new-native-id'])
        self.assertFalse(self.favs)

    async def test_bad_fetch_response_is_not_an_empty_favourites_list(self):
        item = await self.add(qq=True)
        self.bot.call_action.side_effect = None
        self.bot.call_action.return_value = {}
        with self.assertRaisesRegex(ValueError, '列表'):
            await self.lib.sync(ctx=self.ctx)
        self.assertTrue(self.lib.store.get(item['id'])['remote_present'])

    async def test_onebot_bridge_preserves_empty_array_and_empty_sync(self):
        from src.bot import QQBot
        bot = QQBot.__new__(QQBot)
        bot._responses = {}
        async def answer(text):
            request = json.loads(text)
            bot._responses[request['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': []})
        ws = SimpleNamespace(send_text=AsyncMock(side_effect=answer))
        self.assertEqual(await bot.call_action('fetch_custom_face_detail', {'count': 48}, ws), [])
        item = await self.add(qq=True)
        self.favs.clear()
        result = await self.lib.sync(ctx=self.ctx)
        self.assertTrue(result['complete'])
        self.assertFalse(self.lib.store.get(item['id'])['remote_present'])
        self.assertTrue(self.lib.store.get(item['id'])['asset'])

    async def test_background_enrichment_keeps_manual_fields_and_can_resume(self):
        item = await self.add(name='自定名称', description='自定描述', tags=['手工'])
        started, release = asyncio.Event(), asyncio.Event()
        async def describe(data):
            started.set()
            await release.wait()
            return MediaDescription('名称：猫猫点赞\n标签：赞同，开心\n一只猫竖起拇指')
        self.f.files.describe_sticker = describe
        task = asyncio.create_task(self.lib._enrich(self.lib.store.get(item['id'])))
        await asyncio.wait_for(started.wait(), 3)
        handler, _ = self.handler()
        self.assertTrue((await handler('send_message', {'message': '后台处理中也可正常聊天'}))['success'])
        release.set()
        await task
        row = self.lib.store.get(item['id'])
        self.assertEqual((row['name'], row['description'], row['tags']), ('自定名称', '自定描述', ['手工']))
        self.assertEqual(row['auto_tags'], ['赞同', '开心'])
        self.assertEqual(row['state'], 'ready')
        self.lib.store.save('999', row['md5'], {'state': 'processing'})
        self.assertEqual(StickerStore(self.f.workspace.data_root).get(item['id'])['state'], 'queued')
        self.assertEqual(self.lib.search('999', '赞同')[0]['id'], item['id'])

    async def test_failed_analysis_retains_sendable_asset_and_retries(self):
        item = await self.add()
        self.f.files.describe_sticker = AsyncMock(side_effect=RuntimeError('unavailable'))
        for i in range(3):
            await self.lib._enrich(self.lib.store.get(item['id']))
        row = self.lib.store.get(item['id'])
        self.assertEqual(row['state'], 'failed')
        self.assertTrue(row['asset'])
        await self.lib.manage('retry', {'id': item['id']}, ctx=self.ctx)
        self.assertEqual(self.lib.store.get(item['id'])['attempts'], 0)

    async def test_send_ack_scope_limits_and_logging_failure(self):
        item = await self.add(name='猫猫')
        self.f.record = AsyncMock(side_effect=OSError('disk'))
        handler, task = self.handler()
        result = await handler('send_sticker', {'sticker_id': item['id']})
        self.assertTrue(result['success'])
        self.assertFalse(result['recorded'])
        self.assertEqual(task['sent'], 1)
        action, body, ws = self.calls[-1]
        self.assertEqual((action, body['group_id'], ws), ('send_group_msg', '12', 'ws'))
        self.assertEqual(base64.b64decode(body['message'][0]['data']['file'][9:]), self.data)
        self.assertEqual((await handler('send_sticker', {'sticker_id': item['id']}))['code'], 'send_limit')
        self.assertEqual((await self.lib.send(self.ctx, item['id']))['code'], 'sticker_cooldown')
        self.ctx.message_type, self.ctx.group_id, self.ctx.user_id = 'private', None, 123
        self.assertTrue((await self.lib.send(self.ctx, item['id']))['success'])
        self.assertEqual(self.calls[-1][0], 'send_private_msg')

    async def test_concurrent_sends_cannot_bypass_cooldown(self):
        item = await self.add()
        results = await asyncio.gather(*(self.lib.send(self.ctx, item['id']) for _ in range(5)))
        self.assertEqual(sum(r['success'] for r in results), 1)
        self.assertEqual(sum(a[0] == 'send_group_msg' for a in self.calls), 1)

    async def test_unknown_delivery_and_role_owner_boundaries(self):
        item = await self.add()
        self.bot.call_action.side_effect = None
        self.bot.call_action.return_value = {}
        handler, task = self.handler()
        self.assertEqual((await handler('send_sticker', {'sticker_id': item['id']}))['code'], 'unknown')
        self.assertEqual(task['sent'], 0)
        self.assertEqual(task['unknown'], {'send_sticker'})
        for flags, code in [({'worker': True, 'owner': True}, 'delegation_boundary'),
                            ({'worker': True, 'scheduled': True, 'silent': True}, 'silent_task')]:
            guarded, _ = self.handler(**flags)
            self.assertEqual((await guarded('send_sticker', {'sticker_id': item['id']}))['code'], code)
        body = {'action': 'update', 'body': {'id': item['id'], 'name': '修改'}}
        for flags in [{}, {'worker': True}, {'worker': True, 'owner': False}]:
            guarded, _ = self.handler(**flags)
            self.assertFalse((await guarded('__stickers', body))['success'])
        authorized, _ = self.handler(worker=True, owner=True)
        self.assertTrue((await authorized('__stickers', body))['success'])
        self.owner = False
        self.assertEqual((await authorized('__stickers', body))['code'], 'owner_required')

    async def test_attachment_scope_workspace_paths_and_image_validation(self):
        identifier, _ = self.f.workfiles.register(self.ctx, 'image', {'url': 'https://example.com/image'})
        self.f.files._download = AsyncMock(return_value=self.data)
        self.assertEqual(await self.lib.source({'attachment_id': identifier}, self.ctx), self.data)
        self.ctx.group_id = 42
        with self.assertRaisesRegex(ValueError, '不属于'):
            await self.lib.source({'attachment_id': identifier}, self.ctx)
        with self.assertRaisesRegex(ValueError, '工作区'):
            await self.lib.source({'path': '../outside.png'})
        with self.assertRaises(ValueError):
            await self.lib.source({'path': str(self.file), 'url': 'https://example.com/a'})
        with self.assertRaises(Exception):
            inspect_image(b'not an image')
        self.assertEqual(inspect_image(self.data)['extension'], '.png')

    async def test_offline_local_edits_and_garbage_collection(self):
        item = await self.add()
        row = self.lib.store.get(item['id'])
        self.bot.connected_clients.clear()
        result = await self.lib.manage('update', {'id': item['id'], 'name': '离线修改'}, account='999')
        self.assertTrue(result['success'])
        with self.assertRaises(ValueError):
            await self.lib.manage('delete', {'id': item['id'], 'scope': 'qq'}, account='999')
        path = self.lib.path(row['asset'])
        old = time.time() - 90000
        os.utime(path, (old, old))
        await self.lib.collect_assets()
        self.assertTrue(path.exists())
        await self.lib.manage('delete', {'id': item['id'], 'scope': 'local'}, account='999')
        await self.lib.collect_assets()
        self.assertFalse(path.exists())

    async def test_static_cache_reused_and_animation_preserved(self):
        self.f.files._describe = AsyncMock(return_value=MediaDescription('名称：红色\n标签：开心'))
        uri = 'base64://' + base64.b64encode(self.data).decode()
        await self.f.files.understand(uri)
        await self.f.files.describe_sticker(self.data)
        self.f.files._describe.assert_awaited_once()
        stream = io.BytesIO()
        frames = [Image.new('RGB', (12, 12), color) for color in ['red', 'green', 'blue', 'yellow']]
        frames[0].save(stream, 'GIF', save_all=True, append_images=frames[1:], duration=100, loop=0)
        data = stream.getvalue()
        self.f.sandbox.analyze = AsyncMock(return_value={'success': True, 'pages': [{'text': '文字'}]})
        self.f.client.turn = AsyncMock(return_value={'content': '名称：变色\n标签：惊讶\n抽样画面'})
        await self.f.files.describe_sticker(data)
        await self.f.files.describe_sticker(data)
        self.f.client.turn.assert_awaited_once()
        blocks = self.f.client.turn.await_args.args[1][1]['content']
        self.assertEqual(sum(b['type'] == 'image_url' for b in blocks), 3)
        asset = self.lib._publish(data)
        self.assertEqual(self.lib.path(asset).read_bytes(), data)

    async def test_skill_cli_real_bridge_checks_task_authority(self):
        item = await self.add()
        script = self.f.workspace.root / '.agents/skills/sticker-management/scripts/manage.py'
        self.assertTrue(script.exists())
        handler, _ = self.handler(worker=True, owner=True)
        self.f.dsh.active['owner-task'] = {'handler': handler, 'current': lambda: True}
        server = await asyncio.start_server(self.f.dsh._callback, '127.0.0.1', 0)
        request = self.f.workspace.root / 'request.json'
        request.write_text(json.dumps({'id': item['id'], 'name': '通过脚本修改'}), 'utf-8')
        env = {**os.environ, 'QQBOT_CALLBACK': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/tool',
               'QQBOT_DSH_TOKEN': self.f.dsh.token, 'PYTHONIOENCODING': 'utf-8'}
        async def call():
            return await asyncio.to_thread(subprocess.run, [sys.executable, str(script), '--task-id', 'owner-task',
                'update', '--input', str(request)], env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
        try:
            result = await call()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.lib.store.get(item['id'])['name'], '通过脚本修改')
            self.owner = False
            result = await call()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['code'], 'owner_required')
            self.f.dsh.active.clear()
            result = await call()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.f.dsh.token, result.stdout + result.stderr)
        finally:
            self.f.dsh.active.clear()
            server.close()
            await server.wait_closed()

    async def test_admin_routes_upload_metadata_preview_and_local_delete(self):
        # Auth is provided by the parent app; these ASGI calls isolate route logic.
        app = FastAPI()
        register(app, self.f)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/api/ai/stickers/upload?account=999&qq=false&name=cat', content=self.data)
            self.assertEqual(result.status_code, 200, result.text)
            item = result.json()['sticker']
            listing = await client.get('/api/ai/stickers?account=999&limit=1')
            self.assertEqual(listing.json()['total'], 1)
            image = await client.get(f"/api/ai/stickers/{item['id']}/image")
            self.assertEqual(image.content, self.data)
            self.assertEqual(image.headers['cache-control'], 'no-store')
            result = await client.post('/api/ai/stickers/manage', json={'account': '999', 'action': 'delete',
                'body': {'id': item['id'], 'scope': 'local'}})
            self.assertTrue(result.json()['success'])
            self.assertEqual((await client.get(f"/api/ai/stickers/{item['id']}/image")).status_code, 404)
            self.assertFalse(any(a[0] == 'delete_custom_face' for a in self.calls))

    async def test_parent_admin_protects_all_sticker_endpoints(self):
        from src.admin import create_admin_app
        from src.admin_auth import AdminAuth, COOKIE_NAME
        from src.feature.ai.admin import AiAdmin
        from src.feature.atom import FeaturePlugin
        from src.feature.atom.json_file import write_json
        from src.feature.manager import FeatureManager
        from src.master import MasterService
        previous = Path.cwd()
        os.chdir(self.temp.name)
        try:
            write_json(Path('data/admin/auth.json'), {'password': 'sticker-test-password'})
            manager = FeatureManager(Path('data/features.json'), [self.f])
            app = create_admin_app(manager, MasterService(Path('data/master.json')),
                [FeaturePlugin(self.f, AiAdmin(self.f))])
            item = await self.add()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for method, url in [('GET', '/api/ai/stickers'), ('GET', f"/api/ai/stickers/{item['id']}/image"),
                    ('POST', '/api/ai/stickers/manage'), ('POST', '/api/ai/stickers/upload?account=999'),
                    ('POST', '/api/ai/stickers/test-send'), ('POST', '/api/ai/stickers/sync')]:
                    self.assertEqual((await client.request(method, url)).status_code, 401)
                client.cookies.set(COOKIE_NAME, AdminAuth().issue())
                self.assertEqual((await client.get('/api/ai/stickers')).status_code, 200)
                html = (await client.get('/')).text
                self.assertIn('data-sticker-grid', html)
                self.assertIn('function initStickerPanel', html)
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
