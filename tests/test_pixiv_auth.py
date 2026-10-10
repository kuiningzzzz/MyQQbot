import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.feature.atom import FeatureBusyError, MessageContext
from src.feature.pixiv.admin import PixivAdmin
from src.feature.pixiv.auth import browser_login
from src.feature.pixiv.client import PixivClient, PixivDownloadError, PixivTokenInvalid
from src.feature.pixiv.config import PixivConfig
from src.feature.pixiv.feature import PixivFeature
from src.feature.pixiv.login_worker import login


class CredentialTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_preserves_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = PixivConfig(Path(directory) / 'config.json')
            config.save_refresh_token('manual-token')
            config.save_credentials('test@example.invalid', 'fixture-password', 'fixture-totp')
            saved = config.credentials_path.read_bytes()
            self.assertNotIn(b'fixture-password', saved)
            self.assertNotIn(b'fixture-totp', saved)
            self.assertNotIn(b'test@example.invalid', saved)
            self.assertEqual(config.load_credentials()['password'], 'fixture-password')
            self.assertEqual(config.load_refresh_token(), 'manual-token')
            config.key_path.write_bytes(b'invalid')
            with self.assertRaisesRegex(ValueError, '无法解密'):
                config.load_credentials()
            config.save_credentials('test@example.invalid', 'replacement')
            self.assertEqual(config.load_credentials()['password'], 'replacement')
            config.clear_credentials()
            self.assertEqual(config.load_credentials(), {})
            self.assertEqual(config.load_refresh_token(), 'manual-token')


class ClientRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = PixivClient('refresh-old', '', request_timeout=30)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def api(self):
        api = Mock(requests=SimpleNamespace(trust_env=True))
        api.refresh_token = 'refresh-old'
        api.illust_detail.return_value = SimpleNamespace(error=None, illust=SimpleNamespace(
            title='test', user=SimpleNamespace(name='test'), meta_pages=[],
            meta_single_page=SimpleNamespace(original_image_url='https://example.invalid/1.png')))
        api.download.side_effect = lambda url, path, name: (Path(path) / name).write_bytes(b'test')
        return api

    def test_expired_access_refreshes_then_downloads_without_password_login(self):
        cached, fresh = self.api(), self.api()
        cached.illust_detail.return_value = SimpleNamespace(error={'message': 'Error occurred at the OAuth process. invalid_grant'})
        self.client._api = cached
        with patch('pixivpy3.AppPixivAPI', return_value=fresh) as factory:
            work = self.client.download_work('123', self.root)
        self.assertEqual(len(work.image_paths), 1)
        factory.assert_called_once()
        fresh.auth.assert_called_once_with(refresh_token='refresh-old')

    def test_rejected_refresh_token_is_typed_and_redacted(self):
        api = self.api()
        api.auth.side_effect = RuntimeError('invalid_grant refresh_token=refresh-old')
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            with self.assertRaises(PixivTokenInvalid) as error:
                self.client.download_work('123', self.root)
        self.assertNotIn('refresh-old', str(error.exception))
        api.illust_detail.assert_not_called()

    def test_network_and_missing_work_are_not_auth_failures(self):
        api = self.api()
        api.auth.side_effect = RuntimeError('HTTP 503 auth() failed! check refresh_token')
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            with self.assertRaises(PixivDownloadError) as error:
                self.client.download_work('123', self.root)
        self.assertNotIsInstance(error.exception, PixivTokenInvalid)
        api.auth.side_effect = None
        api.illust_detail.return_value = SimpleNamespace(error={'message': 'work not found'})
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            with self.assertRaises(PixivDownloadError) as error:
                self.client.download_work('123', self.root)
        self.assertNotIsInstance(error.exception, PixivTokenInvalid)

    def test_rotated_refresh_token_is_persisted(self):
        api = self.api()
        api.refresh_token = 'refresh-rotated'
        saved = Mock()
        self.client.on_token_refresh = saved
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            self.client.download_work('123', self.root)
        saved.assert_called_once_with('refresh-rotated')

    def test_inflight_refresh_does_not_overwrite_admin_credential(self):
        api = self.api()
        api.refresh_token = 'refresh-rotated'
        api.auth.side_effect = lambda **kw: self.client.set_refresh_token('admin-new-token')
        self.client.on_token_refresh = Mock()
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            self.client.download_work('123', self.root)
        self.assertEqual(self.client.refresh_token, 'admin-new-token')
        self.assertIsNone(self.client._api)
        self.client.on_token_refresh.assert_not_called()


class AuthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feature = PixivFeature(client=PixivClient('old-token', ''), config_path=self.root / 'config.json')
        self.addAsyncCleanup(self.feature.stop)
        self.feature.config.save_credentials('test@example.invalid', 'fixture-password')
        self.bot = SimpleNamespace(reply=AsyncMock(), send_image_forward_msg=AsyncMock(),
            resolve_public_file_reference=lambda *args: 'test.png')
        ctx = MessageContext(self.bot, {}, None, '/pixiv 123', 'private', 1, None)
        self.task = self.feature.create_task('123', ctx)
        self.work = SimpleNamespace(illust_id='123', title='test', author='test', image_paths=[self.root / 'test.png'])

    async def test_auth_failure_logs_in_updates_config_and_retries_only_once(self):
        self.feature.client.download_work = Mock(side_effect=[PixivTokenInvalid('expired'), self.work])
        with patch('src.feature.pixiv.auth.browser_login', AsyncMock(return_value={'ok': True, 'refresh_token': 'new-token'})) as worker:
            result = await self.feature.download_with_recovery(self.task)
        self.assertIs(result, self.work)
        self.assertEqual(self.feature.client.download_work.call_count, 2)
        worker.assert_awaited_once()
        self.assertEqual(self.feature.client.refresh_token, 'new-token')
        self.assertEqual(self.feature.config.load_refresh_token(), 'new-token')
        self.assertNotIn('new-token', json.dumps(self.feature.auth.snapshot()))

    async def test_no_login_on_network_work_or_send_errors(self):
        self.feature.schedule_task_cleanup = Mock()
        self.feature.client.download_work = Mock(side_effect=PixivDownloadError('CDN timeout'))
        with patch('src.feature.pixiv.auth.browser_login', AsyncMock()) as worker:
            with self.assertRaises(PixivDownloadError):
                await self.feature.download_with_recovery(self.task)
            self.feature.client.download_work.side_effect = None
            self.feature.client.download_work.return_value = self.work
            self.feature.send_work = AsyncMock(side_effect=RuntimeError('QQ failed'))
            with self.assertLogs('src.feature.pixiv.feature', level='ERROR'):
                await self.feature.process_task(self.task)
        worker.assert_not_awaited()
        self.feature.send_work.assert_awaited_once()

    async def test_failed_login_keeps_old_token_and_cooldown_prevents_repeated_login(self):
        with patch('src.feature.pixiv.auth.browser_login', AsyncMock(return_value={'ok': False, 'code': 'login_failed', 'message': '人工验证'})) as worker:
            self.assertFalse(await self.feature.auth.recover())
            self.assertFalse(await self.feature.auth.recover())
            worker.assert_awaited_once()
        self.assertEqual(self.feature.client.refresh_token, 'old-token')
        self.assertEqual(self.feature.auth.snapshot()['code'], 'login_failed')

    async def test_same_login_shared_and_manual_retry_allowed(self):
        gate = asyncio.Event()
        async def blocked(payload):
            await gate.wait()
            return {'ok': True, 'refresh_token': 'new-token'}
        with patch('src.feature.pixiv.auth.browser_login', blocked):
            first = self.feature.auth.start()
            self.assertIs(first, self.feature.auth.start(automatic=True))
            with self.assertRaises(FeatureBusyError):
                self.feature.auth.start()
            gate.set()
            self.assertTrue(await first)
            self.assertTrue(await self.feature.auth.start())

    async def test_second_auth_failure_does_not_loop_or_send(self):
        self.feature.client.download_work = Mock(side_effect=PixivTokenInvalid('expired'))
        with patch('src.feature.pixiv.auth.browser_login', AsyncMock(return_value={'ok': True, 'refresh_token': 'new-token'})) as worker:
            with self.assertRaises(PixivTokenInvalid):
                await self.feature.download_with_recovery(self.task)
            worker.assert_awaited_once()
        self.assertEqual(self.feature.client.download_work.call_count, 2)
        self.bot.send_image_forward_msg.assert_not_awaited()

    async def test_admin_routes_no_secret_return_and_block_mutations_during_login(self):
        app = FastAPI()
        PixivAdmin(self.feature).register_routes(app)
        with patch('src.feature.pixiv.auth.browser_login', AsyncMock(side_effect=lambda payload: None)):
            # Separate blocked worker keeps job in flight until cancelled at teardown.
            async def blocked(payload):
                await asyncio.Event().wait()
            with patch('src.feature.pixiv.auth.browser_login', blocked):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/api/pixiv/account', json={'username': 'another@example.invalid', 'password': 'new-password'})
                    self.assertEqual(response.status_code, 202)
                    await asyncio.sleep(0)
                    status = await client.get('/api/pixiv/account')
                    self.assertNotIn('new-password', status.text)
                    self.assertNotIn('old-token', status.text)
                    self.assertTrue(status.json()['running'])
                    self.assertEqual((await client.post('/api/pixiv/account/login')).status_code, 409)
                    self.assertEqual((await client.delete('/api/pixiv/account')).status_code, 409)
                    self.assertEqual((await client.post('/api/pixiv/token', json={'refresh_token': 'manual'})).status_code, 409)
                    self.assertEqual((await client.delete('/api/pixiv/token')).status_code, 409)
                    await self.feature.auth.stop()
                    self.assertEqual((await client.delete('/api/pixiv/account')).status_code, 200)
                    self.assertFalse(self.feature.config.load_credentials())
                    self.assertEqual(self.feature.client.refresh_token, 'old-token')

    async def test_static_download_route_does_not_expose_config_or_credentials(self):
        from src.feature.pixiv.plugin import create_plugin
        with patch('src.feature.pixiv.plugin.PixivFeature', return_value=self.feature):
            plugin = create_plugin()
        self.feature.tasks_dir = self.root / 'tasks'
        # Check the production declaration, then use isolated paths for the HTTP test.
        self.assertEqual(plugin.static_resources[0].directory, Path('data/pixiv/tasks'))
        self.feature.tasks_dir.mkdir()
        (self.feature.tasks_dir / 'test.png').write_bytes(b'image')
        self.feature.config.save_refresh_token('private-token')
        app = FastAPI()
        app.mount('/pixiv-files', StaticFiles(directory=self.feature.tasks_dir))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.get('/pixiv-files/test.png')).status_code, 200)
            for name in ('config.json', 'config.account', 'config.key', '../config.json', '%2e%2e/config.key'):
                self.assertEqual((await client.get('/pixiv-files/' + name)).status_code, 404)


class LoginWorkerTests(unittest.TestCase):
    def test_worker_uses_headless_login_no_runtime_install_and_converts_browser_proxy(self):
        import gppt.browser as browser
        with patch.dict(os.environ, {'ALL_PROXY': 'http://unrelated.invalid'}), \
             patch.object(browser, 'is_chromium_installed', return_value=True), \
             patch.object(browser, 'install'), patch.object(browser, 'PROXIES', {}), \
             patch('gppt.login', return_value=SimpleNamespace(refresh_token='new', access_token='access')) as sdk:
            self.assertTrue(login({'username': 'test', 'password': 'fixture', 'proxy_url': 'socks5h://localhost:1234'})['ok'])
            sdk.assert_called_once_with(username='test', password='fixture', totp_secret='', headless=True)
            self.assertEqual(browser.PROXIES, {'https': 'socks5://localhost:1234'})
            browser.install('ignored')  # replaced by a no-op, never runs an installer

    def test_worker_fails_for_missing_browser_and_two_factor_without_exposing_errors(self):
        import gppt.browser as browser
        with patch.dict(os.environ, {}, clear=False), patch.object(browser, 'is_chromium_installed', return_value=False):
            self.assertEqual(login({'username': 'test', 'password': 'fixture'})['code'], 'browser_missing')
        with patch.dict(os.environ, {}, clear=False), patch.object(browser, 'install'), patch.object(browser, 'PROXIES', {}), \
             patch.object(browser, 'is_chromium_installed', return_value=True), \
             patch('gppt.login', side_effect=RuntimeError('two-factor fixture-secret')):
            result = login({'username': 'test', 'password': 'fixture'})
        self.assertEqual(result['code'], 'two_factor_required')
        self.assertNotIn('fixture-secret', json.dumps(result))


class LoginProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_pipe_redaction_and_hard_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'gppt'
            root.mkdir()
            (root / '__init__.py').write_text('''
from types import SimpleNamespace
import time
def login(**kwargs):
    print('secret log: ' + kwargs['password'])
    if kwargs['username'] == 'hang': time.sleep(60)
    return SimpleNamespace(refresh_token='new-token', access_token='access')
''', 'utf-8')
            (root / 'browser.py').write_text('is_chromium_installed = lambda: True\ninstall = lambda *args, **kwargs: None\nPROXIES = {}\n', 'utf-8')
            with patch.dict(os.environ, {'PYTHONPATH': directory}):
                result = await browser_login({'username': 'test', 'password': 'private-fixture'}, timeout=5)
                self.assertEqual(result, {'ok': True, 'refresh_token': 'new-token'})
                result = await browser_login({'username': 'hang', 'password': 'private-fixture'}, timeout=.5)
                self.assertEqual(result['code'], 'timeout')
                self.assertNotIn('private-fixture', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
