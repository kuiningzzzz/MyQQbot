import base64
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from PIL import Image

from src.feature.atom import MessageContext
from src.feature.loveyou.feature import LoveyouFeature
from src.feature.loveyou.admin import LoveyouAdmin
from src.feature.loveyou.storage import Store, cents_value


def png():
    stream = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(stream, format='PNG')
    return stream.getvalue()


class LoveyouTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feature = LoveyouFeature(SimpleNamespace(is_master=lambda user: user == 123), self.root)
        self.bot = SimpleNamespace(reply=AsyncMock(), send_forward_msg=AsyncMock())

    def ctx(self, text='/loveyou', user=123, private=False):
        return MessageContext(bot=self.bot, websocket=None, event={'self_id': 999},
            msg=text.casefold(), raw_msg=text, user_id=user, group_id=None if private else 456,
            message_type='private' if private else 'group')

    async def test_add_preserves_case_spaces_signed_amount_and_owner(self):
        for text in ['/loveyou-add Alice Chen 0.10', '/loveyou-add Alice Chen 0.20', '/loveyou-add Alice Chen -0.05']:
            self.assertTrue(await self.feature.handle(self.ctx(text)))
        entries, _ = self.feature.store.snapshot()
        self.assertEqual([(e['name'], e['amount']) for e in entries], [('Alice Chen', '0.25')])
        await self.feature.handle(self.ctx('/loveyou-add Alice Chen 999', user=124))
        self.assertEqual(self.feature.store.snapshot()[0][0]['amount'], '0.25')
        self.assertIn('只有主人', self.bot.reply.await_args.args[1])
        self.assertFalse(await self.feature.handle(self.ctx('/loveyou-addition')))

    async def test_ranking_negative_zero_and_literal_segments_both_scenes(self):
        self.feature.store.set_image(png())
        for name, amount in [('负数', '-2'), ('零', '0'), ('少', '1.2'), ('多[CQ:at,qq=all]', '15')]:
            self.feature.store.add(name, amount)
        for private in (False, True):
            ctx = self.ctx(user=9999, private=private)
            self.assertTrue(await self.feature.handle(ctx))
            self.assertIs(self.bot.send_forward_msg.await_args.args[0], ctx)
            messages = self.bot.send_forward_msg.await_args.args[1]
            self.assertEqual(messages[0][0]['type'], 'image')
            self.assertEqual([m[0]['data']['text'] for m in messages[1:]], [
                '多[CQ:at,qq=all] 喂饭数目 15.00', '少 喂饭数目 1.20', '负数 喂饭数目 -2.00'])

    async def test_page_boundaries_have_no_missing_or_duplicate_people(self):
        self.feature.store.set_image(png())
        for count in (0, 98, 99, 198, 205):
            with self.feature.store.connect() as db:
                db.execute('DELETE FROM sponsors')
                db.executemany('INSERT INTO sponsors(name,cents) VALUES (?,?)', [(f'人{i}', i + 1) for i in range(count)])
            self.bot.send_forward_msg.reset_mock()
            await self.feature.handle(self.ctx())
            pages = [call.args[1] for call in self.bot.send_forward_msg.await_args_list]
            self.assertEqual(len(pages), (count + 99) // 99)
            self.assertTrue(all(len(page) <= 99 for page in pages))
            messages = [item for page in pages for item in page]
            self.assertEqual(len(messages), count + 1)
            self.assertEqual(sum(m[0]['type'] == 'image' for m in messages), 1)
            names = [m[0]['data']['text'].split(' 喂饭数目 ')[0] for m in messages[1:]]
            self.assertEqual(names, [f'人{i}' for i in reversed(range(count))])

    async def test_missing_image_and_invalid_amount_never_change_records(self):
        await self.feature.handle(self.ctx())
        self.bot.send_forward_msg.assert_not_awaited()
        self.assertIn('还没有设置', self.bot.reply.await_args.args[1])
        for amount in ('NaN', 'Infinity', '1e2', '0.001', '1000000000000', '--1'):
            await self.feature.handle(self.ctx('/loveyou-add 测试 ' + amount))
        self.assertEqual(self.feature.store.snapshot()[0], [])
        self.assertEqual(cents_value('-0.00'), 0)

    async def test_persistence_atomic_add_and_stale_admin_edits(self):
        store = self.feature.store
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: store.add('同一人', '0.01'), range(50)))
        entry = store.snapshot()[0][0]
        self.assertEqual(entry['amount'], '0.50')
        store.add('同一人', '0.20')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            store.edit('改名', '5', '同一人', entry['version'])
        with self.assertRaisesRegex(ValueError, '发生变化'):
            store.delete('同一人', entry['version'])
        store.set_image(png())
        restored = Store(self.root)
        self.assertEqual(restored.snapshot()[0][0]['amount'], '0.70')
        self.assertEqual(restored.snapshot()[1], png())

    async def test_admin_crud_image_validation_and_conflicts(self):
        app = FastAPI()
        LoveyouAdmin(self.feature).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertFalse((await client.get('/api/loveyou')).json()['has_image'])
            self.assertEqual((await client.get('/api/loveyou/image')).status_code, 404)
            for value in ('wrong', 'data:image/png;base64,bad!', 'data:image/svg+xml;base64,AAA='):
                self.assertEqual((await client.post('/api/loveyou/image', json={'data_url': value})).status_code, 400)
            response = await client.post('/api/loveyou/image', json={'data_url': 'data:image/png;base64,' + base64.b64encode(png()).decode()})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await client.get('/api/loveyou/image')).headers['content-type'], 'image/png')
            self.assertEqual((await client.post('/api/loveyou/entries', json={'name': 'AA', 'amount': '-1.20'})).status_code, 200)
            self.assertEqual((await client.post('/api/loveyou/entries', json={'name': 'AA', 'amount': '2'})).status_code, 409)
            entry = (await client.get('/api/loveyou')).json()['entries'][0]
            response = await client.post('/api/loveyou/entries', json={'previous_name': 'AA', 'name': 'BB', 'amount': '3.40', 'version': entry['version']})
            self.assertEqual(response.status_code, 200)
            current = (await client.get('/api/loveyou')).json()['entries'][0]
            self.assertEqual((current['name'], current['amount']), ('BB', '3.40'))
            self.assertEqual((await client.post('/api/loveyou/entries/delete', json={'name': 'BB', 'version': current['version']})).status_code, 200)
            self.assertEqual((await client.delete('/api/loveyou/image')).status_code, 200)
            self.assertFalse((await client.get('/api/loveyou')).json()['has_image'])

    async def test_backup_incremental_merge_uses_name_and_backup_value(self):
        from src.backup.merge import merge_database
        old = Store(self.root / 'current')
        incoming = Store(self.root / 'incoming')
        old.add('仅当前', '1')
        old.add('同名', '2')
        incoming.add('仅备份', '3')
        incoming.add('同名', '4')
        incoming.set_image(png())
        merge_database(old.path, incoming.path)
        entries, image = incoming.snapshot()
        self.assertEqual({e['name']: e['amount'] for e in entries}, {'仅当前': '1.00', '仅备份': '3.00', '同名': '4.00'})
        self.assertEqual(image, png())
