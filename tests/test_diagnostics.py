import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI

from src.feature.atom import FeatureBusyError, FeaturePlugin
from src.feature.atom.diagnostics import DiagnosticReport, DiagnosticRunner, safe_error
from src.feature.jm.admin import JmAdmin
from src.feature.jm.feature import JmPdfFeature
from src.feature.jm.diagnostics import CHECKS as JM_CHECKS, probe as jm_probe
from src.feature.pixiv.admin import PixivAdmin
from src.feature.pixiv.client import PixivClient
from src.feature.pixiv.feature import PixivFeature
from src.feature.pixiv.diagnostics import CHECKS as PIXIV_CHECKS, probe as pixiv_probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def report(self, checks, secrets=()):
        return DiagnosticReport(self.root / 'report.json', {'run_id': 'test', 'status': 'running',
            'checks': [{'id': key, 'label': label, 'status': 'pending'} for key, label in checks]}, secrets)

    def execute(self, probe, payload, checks):
        report = self.report(checks, [payload.get('refresh_token', '')])
        try:
            probe(payload, report, self.root / 'sample')
            report.finish()
        except Exception as error:
            report.fail(error)
        return report.data

    def pixiv_api(self):
        from PIL import Image
        illust = SimpleNamespace(id=123, title='fixture', x_restrict=0, type='illust', user=SimpleNamespace(name='artist'),
            meta_pages=[SimpleNamespace(image_urls=SimpleNamespace(original=f'https://example.invalid/{n}.png')) for n in range(2)])
        api = Mock(access_token=None, requests=SimpleNamespace(trust_env=True))
        api.auth.side_effect = lambda **kw: setattr(api, 'access_token', 'new-access-token')
        api.illust_ranking.return_value = SimpleNamespace(error=None, illusts=[illust])
        api.illust_detail.return_value = SimpleNamespace(error=None, illust=illust)
        api.download.side_effect = lambda url, path, name: Image.new('RGB', (12, 12), 'white').save(Path(path) / name)
        return api

    def test_pixiv_refreshes_auth_and_downloads_only_one_real_image(self):
        api = self.pixiv_api()
        with patch('pixivpy3.AppPixivAPI', return_value=api) as factory:
            result = self.execute(pixiv_probe, {'refresh_token': 'current-secret', 'proxy_url': 'http://proxy.invalid:8080'}, PIXIV_CHECKS)
        self.assertEqual(result['status'], 'passed')
        api.auth.assert_called_once_with(refresh_token='current-secret')
        self.assertEqual(factory.call_args.kwargs['timeout'], 15)
        self.assertEqual(factory.call_args.kwargs['proxies']['https'], 'http://proxy.invalid:8080')
        api.download.assert_called_once()
        self.assertFalse(api.requests.trust_env)
        self.assertNotIn('current-secret', json.dumps(result))

    def test_pixiv_production_still_downloads_all_pages(self):
        api = self.pixiv_api()
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            work = PixivClient('secret').download_work('123', self.root)
        self.assertEqual(len(work.image_paths), 2)
        self.assertEqual(api.download.call_count, 2)

    def test_pixiv_no_token_and_expired_token_have_actionable_failures(self):
        result = self.execute(pixiv_probe, {}, PIXIV_CHECKS)
        self.assertEqual(result['checks'][1]['code'], 'not_configured')
        self.assertEqual(result['checks'][2]['status'], 'skipped')
        api = self.pixiv_api()
        api.auth.side_effect = RuntimeError('invalid_grant refresh_token=my-secret access_token=access-secret')
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            result = self.execute(pixiv_probe, {'refresh_token': 'my-secret'}, PIXIV_CHECKS)
        self.assertEqual(result['checks'][1]['code'], 'auth_failed')
        self.assertNotIn('my-secret', json.dumps(result))
        self.assertNotIn('access-secret', json.dumps(result))
        api.illust_detail.assert_not_called()

    def test_pixiv_metadata_and_bad_cdn_content_cannot_pass(self):
        api = self.pixiv_api()
        api.illust_detail.return_value = SimpleNamespace(error=None, illust=None)
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            result = self.execute(pixiv_probe, {'refresh_token': 'secret', 'sample_id': '123'}, PIXIV_CHECKS)
        self.assertEqual(result['checks'][2]['status'], 'failed')
        api.download.assert_not_called()
        api = self.pixiv_api()
        api.download.side_effect = lambda url, path, name: (Path(path) / name).write_text('<html>blocked</html>')
        with patch('pixivpy3.AppPixivAPI', return_value=api):
            result = self.execute(pixiv_probe, {'refresh_token': 'secret', 'sample_id': '123'}, PIXIV_CHECKS)
        self.assertEqual(result['checks'][3]['code'], 'image_download_failed')

    def jm_client(self):
        from jmcomic import JmAlbumDetail
        from PIL import Image
        album = JmAlbumDetail('123', '0', 'fixture', [('124', '1', 'first'), ('125', '2', 'second')],
            4, '', '', '0', '0', 0, [], [], ['test'], [])
        client = Mock()
        client.get_album_detail.return_value = album
        def check_photo(photo):
            photo.page_arr = ['00001.jpg', '00002.jpg']
            photo.data_original_domain = 'example.invalid'
        client.check_photo.side_effect = check_photo
        client.download_by_image_detail.side_effect = lambda image, path, **kw: Image.new('RGB', (20, 20), 'white').save(path)
        return client

    def test_jm_real_downloader_exports_one_page_pdf(self):
        from pypdf import PdfReader
        client = self.jm_client()
        with patch('jmcomic.JmOption.build_jm_client', return_value=client):
            result = self.execute(jm_probe, {'sample_id': '123'}, JM_CHECKS)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(client.check_photo.call_count, 1)
        client.download_by_image_detail.assert_called_once()
        self.assertTrue(client.download_by_image_detail.call_args.kwargs['decode_image'])
        pdfs = list((self.root / 'sample/pdfs').glob('*.pdf'))
        self.assertEqual(len(pdfs), 1)
        self.assertEqual(len(PdfReader(pdfs[0]).pages), 1)

    def test_jm_network_image_and_pdf_failures_are_distinguished(self):
        client = self.jm_client()
        client.get_album_detail.side_effect = RuntimeError('DNS lookup failed')
        with patch('jmcomic.JmOption.build_jm_client', return_value=client):
            result = self.execute(jm_probe, {'sample_id': '123'}, JM_CHECKS)
        self.assertEqual(result['checks'][1]['code'], 'network_error')
        self.assertEqual(result['checks'][3]['status'], 'skipped')
        client = self.jm_client()
        client.download_by_image_detail.side_effect = RuntimeError('CDN unavailable')
        with patch('jmcomic.JmOption.build_jm_client', return_value=client):
            result = self.execute(jm_probe, {'sample_id': '123'}, JM_CHECKS)
        self.assertEqual(result['checks'][2]['status'], 'failed')
        self.assertEqual(result['checks'][3]['status'], 'skipped')
        client = self.jm_client()
        with patch('jmcomic.JmOption.build_jm_client', return_value=client), patch('img2pdf.convert', side_effect=RuntimeError('broken PDF exporter')):
            result = self.execute(jm_probe, {'sample_id': '123'}, JM_CHECKS)
        self.assertEqual(result['checks'][2]['status'], 'passed')
        self.assertEqual(result['checks'][3]['code'], 'pdf_export_failed')

    def test_secrets_and_proxy_passwords_are_redacted(self):
        error = 'https://username:password@proxy.invalid/?refresh_token=known+secret access_token=generated-secret'
        result = safe_error(error, ('known secret',))
        for value in ('username', 'password@', 'known+secret', 'generated-secret'):
            self.assertNotIn(value, result)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'probe_fixture.py').write_text('''
import time
from src.feature.atom.diagnostics import run_worker
def probe(payload, report, directory):
    report.step('dependencies', 'passed', 'fixture ready')
    directory.mkdir()
    (directory / 'sample.bin').write_bytes(b'test')
    report.step('auth')
    if payload.get('hang'): time.sleep(60)
    assert payload['refresh_token'] == 'stdin-only-secret'
    report.step('auth', 'passed', 'fixture auth passed')
run_worker(probe)
''', 'utf-8')
        self.environment = patch.dict(os.environ, {'PYTHONPATH': str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runner = DiagnosticRunner('probe_fixture', PIXIV_CHECKS[:2], timeout=2)
        self.addAsyncCleanup(self.runner.stop)

    async def test_real_worker_stdin_result_and_temporary_cleanup(self):
        real_spawn = asyncio.create_subprocess_exec
        directories = []
        async def spawn(*args, **kwargs):
            self.assertNotIn('stdin-only-secret', str(args))
            directories.append(Path(kwargs['cwd']))
            return await real_spawn(*args, **kwargs)
        with patch('asyncio.create_subprocess_exec', side_effect=spawn):
            result = self.runner.start({'refresh_token': 'stdin-only-secret'})
            self.assertTrue(result['running'])
            await asyncio.wait_for(self.runner.task, 5)
        self.assertEqual(self.runner.snapshot()['status'], 'passed')
        self.assertFalse(self.runner.snapshot()['running'])
        self.assertNotIn('stdin-only-secret', json.dumps(self.runner.snapshot()))
        self.assertTrue(directories)
        self.assertFalse(directories[0].exists())

    async def test_hard_timeout_kills_process_and_allows_retry(self):
        self.runner.start({'refresh_token': 'stdin-only-secret', 'hang': True})
        with self.assertRaises(FeatureBusyError):
            self.runner.start({})
        await asyncio.wait_for(self.runner.task, 5)
        result = self.runner.snapshot()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(check.get('code') == 'timeout' for check in result['checks']))
        self.assertIsNone(self.runner.process)
        self.runner.start({'refresh_token': 'stdin-only-secret'})
        await asyncio.wait_for(self.runner.task, 5)
        self.assertEqual(self.runner.snapshot()['status'], 'passed')

    async def test_stop_before_start_and_during_worker_do_not_leave_process(self):
        self.runner.start({'refresh_token': 'stdin-only-secret', 'hang': True})
        await self.runner.stop()
        self.assertEqual(self.runner.snapshot()['status'], 'failed')
        self.runner.start({'refresh_token': 'stdin-only-secret', 'hang': True})
        for _ in range(40):
            if self.runner.process:
                break
            await asyncio.sleep(.02)
        process = self.runner.process
        self.assertIsNotNone(process)
        await self.runner.stop()
        self.assertIsNotNone(process.returncode)
        self.assertFalse(self.runner.running)


class AdminDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pixiv = PixivFeature(client=PixivClient('old-secret', 'http://proxy.invalid:8080'), config_path=self.root / 'pixiv.json')
        self.jm = JmPdfFeature()
        self.addAsyncCleanup(self.pixiv.stop)
        self.addAsyncCleanup(self.jm.stop)
        self.app = FastAPI()
        PixivAdmin(self.pixiv).register_routes(self.app)
        JmAdmin(self.jm).register_routes(self.app)
        self.payloads = []
        async def blocked(payload):
            self.payloads.append(payload)
            await asyncio.Event().wait()
        self.pixiv.diagnostics._execute = blocked
        self.jm.diagnostics._execute = blocked

    async def test_disabled_modules_test_latest_saved_config_and_reject_duplicate(self):
        self.assertFalse(self.pixiv.default_enabled)
        self.assertFalse(self.jm.default_enabled)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            await client.post('/api/pixiv/token', json={'refresh_token': 'new-secret'})
            response = await client.post('/api/pixiv/test', json={'sample_id': '123'})
            self.assertEqual(response.status_code, 202)
            self.assertNotIn('new-secret', response.text)
            await asyncio.sleep(0)
            self.assertEqual(self.payloads[-1]['refresh_token'], 'new-secret')
            self.assertEqual(self.payloads[-1]['proxy_url'], 'http://proxy.invalid:8080')
            self.assertEqual((await client.post('/api/pixiv/test')).status_code, 409)
            self.assertTrue((await client.get('/api/pixiv/test')).json()['running'])
            self.assertEqual((await client.post('/api/jm/test')).status_code, 202)
            self.assertEqual((await client.post('/api/jm/test')).status_code, 409)

    async def test_invalid_ids_are_rejected_without_starting_work(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            for kind in ('jm', 'pixiv'):
                for value in ('../x', '0', '1' * 21, 'https://example.com'):
                    response = await client.post(f'/api/{kind}/test', json={'sample_id': value})
                    self.assertEqual(response.status_code, 422)
        self.assertFalse(self.payloads)

    async def test_live_checks_require_admin_auth(self):
        from src.admin import create_admin_app
        from src.feature.manager import FeatureManager
        from src.master import MasterService
        manager = FeatureManager(self.root / 'features.json', [self.pixiv, self.jm])
        with patch('src.admin.AdminAuth', return_value=SimpleNamespace(validate=lambda cookie: False)):
            app = create_admin_app(manager, MasterService(self.root / 'master.json'),
                [FeaturePlugin(self.pixiv, PixivAdmin(self.pixiv)), FeaturePlugin(self.jm, JmAdmin(self.jm))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for kind in ('jm', 'pixiv'):
                self.assertEqual((await client.post(f'/api/{kind}/test')).status_code, 401)
                self.assertEqual((await client.get(f'/api/{kind}/test')).status_code, 401)
            for method, route in [('get', '/api/pixiv/account'), ('post', '/api/pixiv/account'),
                                  ('post', '/api/pixiv/account/login'), ('delete', '/api/pixiv/account')]:
                self.assertEqual((await getattr(client, method)(route)).status_code, 401)
        self.assertFalse(self.payloads)

    async def test_combined_panel_javascript_parses(self):
        source = 'const featureAdminControls = {};\n' + JmAdmin(self.jm).javascript + PixivAdmin(self.pixiv).javascript
        result = subprocess.run(['node', '--check', '-'], input=source, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
