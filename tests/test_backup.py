import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import httpx

from src.backup import archive
from src.backup.common import BackupError, read_json, remove, write_json
from src.backup.control import BackupControl
from src.backup.merge import merge_database, merge_tree
from src.backup.service import BackupStore
from src.backup.supervisor import child_environment


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = BackupStore(self.root)
        self.put('src/example.py', 'print("hello")')
        self.put('README.md', '部署说明')

    def put(self, relative, text):
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, 'utf-8')
        return file

    def database(self, relative, rows):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, name TEXT UNIQUE, content TEXT NOT NULL)')
            db.executemany('INSERT INTO items VALUES(?,?,?)', rows)
            db.commit()
        return path

    def backup(self):
        return self.store.make_backup(env={'DEEPSEEK_API_KEY': 'test-secret'})

    def test_complete_archive_roundtrip_excludes_other_backups_and_dependencies(self):
        self.put('data/ai/workspace/memory/group/123/主题.md', '群记忆')
        self.put('data/ai/workspace/memory/world/科技/专题.md', '世界知识')
        self.put('data/ai/workspace/memory/world/科技/资料.csv', 'source,value\nexample,1')
        self.put('data/ai/workspace/files/项目.txt', '文件内容')
        self.put('data/ai/workspace/empty/.keep', '')
        self.put('data/pixiv/config.key', 'encrypted-key')
        self.put('data/admin/auth.json', '{"password":"private"}')
        self.put('.env', 'DEEPSEEK_API_KEY=file-secret')
        self.put('dsh/node_modules/huge.bin', 'excluded')
        self.database('data/timing/tasks.sqlite3', [('1', 'task', 'data')])
        (self.root / 'data/empty-dir').mkdir()
        old = self.backup()
        name = self.backup()
        path = self.store.package(name)
        manifest = archive.inspect(path)
        files = manifest['files']
        self.assertIn('data/ai/workspace/memory/group/123/主题.md', files)
        self.assertIn('data/ai/workspace/memory/world/科技/专题.md', files)
        self.assertIn('data/ai/workspace/memory/world/科技/资料.csv', files)
        self.assertIn('data/pixiv/config.key', files)
        self.assertIn('configuration/.env', files)
        self.assertIn('project/src/example.py', files)
        self.assertFalse(any('node_modules' in f or 'data/backups' in f for f in files))
        archive.extract(path, self.root / 'unpacked')
        self.assertEqual((self.root/'unpacked/data/ai/workspace/memory/world/科技/专题.md').read_text('utf-8'),'世界知识')
        self.assertEqual((self.root/'unpacked/data/ai/workspace/memory/world/科技/资料.csv').read_text('utf-8'),'source,value\nexample,1')
        self.assertEqual(read_json(self.root / 'unpacked/configuration/environment.json')['DEEPSEEK_API_KEY'], 'test-secret')
        self.assertTrue((self.root / 'unpacked/data/empty-dir').is_dir())
        self.assertEqual(self.store.list()[0]['files'], len(files))
        self.assertTrue(self.store.package(old).exists())

    def test_overwrite_removes_current_only_data_and_keeps_backup_store(self):
        self.put('data/ai/workspace/memory/group/123/event.md', 'before')
        self.put('data/master.json', '{"master_id":1}')
        name = self.backup()
        self.put('data/master.json', '{"master_id":2}')
        self.put('data/extra/current.txt', 'current-only')
        safety = self.store.restore(name, 'overwrite')
        self.assertEqual(read_json(self.root / 'data/master.json')['master_id'], 1)
        self.assertFalse((self.root / 'data/extra').exists())
        self.assertTrue(self.store.package(name).exists())
        self.assertTrue(self.store.package(safety).exists())
        self.assertEqual(read_json(self.root / 'data/.deployment-environment.json')['DEEPSEEK_API_KEY'], 'test-secret')
        self.assertFalse(self.store.transaction.exists())
        # The pre-restore ZIP can independently roll the operation back.
        self.store.restore(safety, 'overwrite')
        self.assertEqual(read_json(self.root / 'data/master.json')['master_id'], 2)
        self.assertTrue((self.root / 'data/extra/current.txt').exists())

    def test_sticker_catalogue_and_assets_survive_incremental_and_overwrite_restore(self):
        from src.feature.ai.sticker_store import StickerStore
        catalogue = StickerStore(self.root / 'data/ai')
        first = catalogue.save('999', 'a' * 32, {'name': 'backup', 'asset': 'a' * 64 + '.png'})
        asset = self.put('data/ai/workspace/stickers/assets/' + 'a' * 64 + '.png', 'image-bytes')
        name = self.backup()
        catalogue.save('999', 'a' * 32, {'name': 'current'})
        second = catalogue.save('999', 'b' * 32, {'name': 'current-only'})
        asset.unlink()
        self.store.restore(name, 'incremental')
        self.assertEqual(catalogue.get(first['id'])['name'], 'backup')
        self.assertIsNotNone(catalogue.get(second['id']))
        self.assertEqual(asset.read_text('utf-8'), 'image-bytes')
        self.store.restore(name, 'overwrite')
        self.assertIsNone(catalogue.get(second['id']))
        self.assertEqual(catalogue.get(first['id'])['name'], 'backup')

    def test_incremental_merges_configuration_and_database_rows(self):
        self.put('data/features.json', '{"common":{"backup":1,"shared":"backup"},"backup-only":true}')
        self.put('data/ai/config.yaml', 'nickname: 备份名字\n')
        self.put('data/ai/workspace/note.md', 'backup text')
        self.database('data/timing/tasks.sqlite3', [('1', 'one', 'backup'), ('2', 'two', 'backup-only')])
        name = self.backup()
        self.put('data/features.json', '{"common":{"current":2,"shared":"current"},"current-only":true}')
        self.put('data/ai/config.yaml', 'nickname: 当前名字\nocr_enabled: false\n')
        self.put('data/ai/workspace/note.md', 'current text')
        self.put('data/ai/workspace/extra.md', 'keep')
        db = self.root / 'data/timing/tasks.sqlite3'
        db.unlink()
        self.database('data/timing/tasks.sqlite3', [('1', 'one', 'current'), ('3', 'three', 'current-only')])
        self.store.restore(name, 'incremental')
        self.assertEqual(read_json(self.root / 'data/features.json'), {'common': {'backup': 1, 'shared': 'backup', 'current': 2}, 'backup-only': True, 'current-only': True})
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(dict(connection.execute('SELECT id,content FROM items')), {'1': 'backup', '2': 'backup-only', '3': 'current-only'})
        self.assertEqual((self.root / 'data/ai/workspace/note.md').read_text(), 'backup text')
        self.assertTrue((self.root / 'data/ai/workspace/extra.md').exists())
        import yaml
        self.assertEqual(yaml.safe_load((self.root / 'data/ai/config.yaml').read_text('utf-8')), {'nickname': '备份名字', 'ocr_enabled': False})
        self.assertTrue(read_json(self.root / 'data/ai/.state/index.json')['dirty'])

    def journal(self, relative, identities):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT UNIQUE, timestamp REAL, path TEXT, human INTEGER, processed INTEGER);
                CREATE TABLE media_updates(id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER UNIQUE, revision INTEGER, consumed INTEGER);''')
            for seq, identity in identities:
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (seq, identity, 100, 'raw/missing.json', 1, 0))
                db.execute('INSERT INTO media_updates(seq,revision,consumed) VALUES(?,?,?)', (seq, seq, 0))
            db.commit()
        return path

    def test_incremental_remaps_message_and_media_job_identity_not_integer_id(self):
        old = self.root / 'current'
        new = self.root / 'incoming'
        self.journal('current/ai/.state/messages.sqlite3', [(1, 'group:1:a'), (2, 'group:1:current')])
        merged = self.journal('incoming/ai/.state/messages.sqlite3', [(1, 'group:1:backup'), (5, 'group:1:a')])
        for folder, seq, value in [('current', 1, 'old-a'), ('current', 2, 'old-only'), ('incoming', 1, 'backup-only'), ('incoming', 5, 'backup-a')]:
            path = self.root / folder / 'ai/.state/media-jobs.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS jobs(seq INTEGER PRIMARY KEY, results TEXT)')
                db.execute('INSERT INTO jobs VALUES(?,?)', (seq, value))
                db.commit()
        merge_tree(old, new)
        with closing(sqlite3.connect(merged)) as db:
            ids = dict(db.execute('SELECT identity,seq FROM messages'))
            self.assertEqual(ids, {'group:1:backup': 1, 'group:1:a': 5, 'group:1:current': 6})
            self.assertEqual(db.execute('SELECT revision FROM media_updates WHERE seq=5').fetchone()[0], 5)
        with closing(sqlite3.connect(new / 'ai/.state/media-jobs.sqlite3')) as db:
            self.assertEqual(dict(db.execute('SELECT seq,results FROM jobs')), {1: 'backup-only', 5: 'backup-a', 6: 'old-only'})

    def test_incompatible_database_aborts_incremental_before_mutating_live_data(self):
        self.database('data/custom.sqlite3', [('1', 'one', 'backup')])
        name = self.backup()
        with closing(sqlite3.connect(self.root / 'data/custom.sqlite3')) as db:
            db.execute('ALTER TABLE items ADD COLUMN current_only TEXT')
            db.commit()
        before = (self.root / 'data/custom.sqlite3').read_bytes()
        with self.assertRaises(BackupError):
            self.store.restore(name, 'incremental')
        self.assertEqual((self.root / 'data/custom.sqlite3').read_bytes(), before)
        self.assertFalse(self.store.transaction.exists())

    def test_install_failure_rolls_back_data(self):
        self.put('data/master.json', '{"master_id":1}')
        name = self.backup()
        self.put('data/master.json', '{"master_id":2}')
        original = os.replace

        def fail_once(source, destination):
            if str(source).endswith('incoming\\data\\master.json') or str(source).endswith('incoming/data/master.json'):
                raise OSError('simulated disk failure')
            return original(source, destination)

        with patch('src.backup.service.os.replace', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.store.restore(name, 'overwrite')
        self.assertEqual(read_json(self.root / 'data/master.json'), {'master_id': 2})
        self.assertFalse(self.store.transaction.exists())

    def test_crash_recovery_is_idempotent_after_partial_install(self):
        identifier = 'a' * 32
        old = self.store.store / ('restore-' + identifier) / 'old'
        old.mkdir(parents=True)
        write_json(old / 'master.json', {'master_id': 'old'})
        write_json(self.root / 'data/master.json', {'master_id': 'new'})
        self.put('data/new-only.txt', 'remove')
        write_json(self.store.transaction, {'id': identifier, 'phase': 'installing', 'originals': ['master.json']})
        self.assertTrue(self.store.recover())
        self.assertEqual(read_json(self.root / 'data/master.json'), {'master_id': 'old'})
        self.assertFalse((self.root / 'data/new-only.txt').exists())
        self.assertFalse(self.store.recover())

    def test_restore_rejects_low_disk_space_without_removing_live_data(self):
        self.put('data/keep.txt', 'untouched')
        name = self.backup()
        with patch('src.backup.service.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(BackupError):
                self.store.restore(name, 'overwrite')
        self.assertEqual((self.root / 'data/keep.txt').read_text(), 'untouched')

    def rewrite_archive(self, name, changes=None, extra=None):
        original = self.store.package(name)
        result = self.root / 'bad.zip'
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(result, 'w') as target:
            for info in source.infolist():
                data = source.read(info)
                if changes and info.filename in changes:
                    data = changes[info.filename]
                target.writestr(info, data)
            if extra:
                target.writestr(*extra)
        return result

    def test_corrupt_payload_fails_hash_verification(self):
        self.put('data/test.txt', 'aaaa')
        bad = self.rewrite_archive(self.backup(), changes={'data/test.txt': b'bbbb'})
        with self.assertRaises(BackupError):
            archive.inspect(bad)

    def test_traversal_absolute_duplicate_symlink_and_reserved_members_are_rejected(self):
        name = self.backup()
        for malicious in ['../outside', '/absolute', 'C:/windows', 'data/../escape', 'data\\escape', 'data/CON.txt', 'manifest.json']:
            with self.subTest(malicious=malicious):
                bad = self.rewrite_archive(name, extra=(malicious, b'bad'))
                with self.assertRaises(BackupError):
                    archive.inspect(bad)
        info = zipfile.ZipInfo('data/link')
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(BackupError):
            archive.inspect(self.rewrite_archive(name, extra=(info, b'/etc/passwd')))

    def test_size_limit_applies_before_decompression(self):
        name = self.backup()
        with patch.object(archive, 'MAX_BYTES', 1), self.assertRaises(BackupError):
            archive.inspect(self.store.package(name))

    def test_package_names_cannot_escape_store(self):
        for name in ['../../.env', 'C:\\secret.zip', '/tmp/file.zip', '../other.zip']:
            with self.assertRaises(BackupError):
                self.store.package(name)

    def test_environment_snapshot_only_uses_project_allowlist(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test', 'AWS_SECRET_ACCESS_KEY': 'never-backup'}):
            values = archive.environment()
        self.assertEqual(values['DEEPSEEK_API_KEY'], 'test')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', values)

    def test_restored_environment_wins_and_does_not_read_unrelated_host_secrets(self):
        write_json(self.root / 'data/.deployment-environment.json', {'DEEPSEEK_API_KEY': 'backup-key'})
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'current-key'}):
            values = child_environment(self.store)
        self.assertEqual(values['DEEPSEEK_API_KEY'], 'backup-key')
        self.assertEqual(values['MYQQBOT_BACKUP_SUPERVISED'], '1')

    def test_overwrite_environment_also_restores_absent_values(self):
        write_json(self.root / 'data/.deployment-environment.json', {})
        self.put('.env', 'AI_BOT_QQ=1234\nDEEPSEEK_API_KEY=current-key\n')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'host-key', 'PIXIV_PROXY': 'host-proxy'}):
            values = child_environment(self.store)
        self.assertNotIn('AI_BOT_QQ', values)
        self.assertNotIn('DEEPSEEK_API_KEY', values)
        self.assertNotIn('PIXIV_PROXY', values)

    def test_offline_environment_reads_native_host_config(self):
        self.put('.env.host', 'AI_BOT_QQ=12345\nDEEPSEEK_API_KEY=host-file-key\n')
        with patch.dict(os.environ, {}, clear=True):
            values = child_environment(self.store)
        self.assertEqual(values['AI_BOT_QQ'], '12345')
        self.assertEqual(values['DEEPSEEK_API_KEY'], 'host-file-key')

    def test_incremental_preserves_pixiv_current_only_account_with_matching_key(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        self.put('incoming/pixiv/config.key', Fernet.generate_key().decode())
        self.put('current/pixiv/config.key', key.decode())
        self.put('current/pixiv/config.account', Fernet(key).encrypt(b'{"username":"test"}').decode())
        merge_tree(self.root / 'current', self.root / 'incoming')
        cipher = Fernet((self.root / 'incoming/pixiv/config.key').read_bytes())
        self.assertEqual(cipher.decrypt((self.root / 'incoming/pixiv/config.account').read_bytes()), b'{"username":"test"}')

    def test_file_mtime_survives_roundtrip_for_retention_fallbacks(self):
        file = self.put('data/ai/workspace/memory/raw/legacy/unknown.md', 'old raw')
        stamp = 1_700_000_000_000_000_000
        os.utime(file, ns=(stamp, stamp))
        name = self.backup()
        self.store.restore(name, 'overwrite')
        self.assertEqual(file.stat().st_mtime_ns, stamp)

    def test_missing_backup_cursor_is_not_replaced_by_unrelated_current_sequence(self):
        self.journal('current/ai/.state/messages.sqlite3', [(100, 'group:1:current')])
        self.journal('incoming/ai/.state/messages.sqlite3', [(1, 'group:1:backup')])
        self.put('current/ai/.state/chat-cursor.json', '{"seq":100}')
        self.put('current/ai/.state/chat-media-cursor.json', '{"id":100}')
        self.put('current/ai/.state/memory-batch-receipt.json', '{"batch":"1-100"}')
        merge_tree(self.root / 'current', self.root / 'incoming')
        self.assertEqual(read_json(self.root / 'incoming/ai/.state/chat-cursor.json'), {'seq': 0})
        self.assertEqual(read_json(self.root / 'incoming/ai/.state/chat-media-cursor.json'), {'id': 0})
        self.assertFalse((self.root / 'incoming/ai/.state/memory-batch-receipt.json').exists())

    def test_incremental_rebuilds_chronological_context_with_media_and_recall_flags(self):
        for folder, identifier, stamp in [('current', 'old', '2026-10-20T08:00:00+08:00'), ('incoming', 'new', '2026-10-20T09:00:00+08:00')]:
            db_path = self.journal(f'{folder}/ai/.state/messages.sqlite3', [(1, 'group:1:' + identifier)])
            relative = f'raw/day/{identifier}.json'
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('UPDATE messages SET path=?', (relative,))
                db.commit()
            record = {'id': identifier, 'scene_type': 'group', 'scene_id': '1', 'qq': '123', 'nickname': 'user',
                      'content': '[image]', 'timestamp': stamp, 'is_self': False}
            write_json(self.root / folder / 'ai/workspace/memory' / relative, {'record': record})
            if folder == 'current':
                self.put(f'{folder}/ai/workspace/memory/raw/day/{identifier}.rendered.txt', '已解析的图片内容')
                write_json(self.root / folder / 'ai/.state/context.json', {'group:1': {'recent_messages': [{**record, 'recalled': True}]}})
        merge_tree(self.root / 'current', self.root / 'incoming')
        scene = read_json(self.root / 'incoming/ai/.state/context.json')['group:1']
        self.assertEqual([row['id'] for row in scene['recent_messages']], ['old', 'new'])
        self.assertEqual(scene['recent_messages'][0]['content'], '已解析的图片内容')
        self.assertTrue(scene['recent_messages'][0]['recalled'])
        self.assertEqual(scene['last_activity'], '2026-10-20T09:00:00+08:00')

    def test_successful_pending_operation_status_and_catalog(self):
        result = self.store.request('create', env={})
        self.assertEqual(result['state'], 'queued')
        with self.assertRaises(BackupError):
            self.store.request('create')
        self.store.execute_pending()
        self.assertEqual(self.store.status()['state'], 'succeeded')
        self.assertTrue(self.store.list())
        self.assertFalse(self.store.pending.exists())

    def test_cold_start_cleans_only_known_incomplete_work_and_keeps_packages(self):
        name = self.backup()
        partial = self.store.packages / 'backup-20261020-080000-1234abcd.partial'
        partial.write_bytes(b'incomplete')
        work = self.store.store / ('restore-' + '1' * 32)
        work.mkdir()
        (work / 'large.tmp').write_bytes(b'incomplete')
        (self.store.store / 'import.zip').write_bytes(b'user-kept')
        self.store.cleanup_incomplete()
        self.assertFalse(partial.exists())
        self.assertFalse(work.exists())
        self.assertTrue(self.store.package(name).exists())
        self.assertTrue((self.store.store / 'import.zip').exists())

    def test_invalid_manifest_type_is_a_user_facing_backup_error(self):
        bad = self.rewrite_archive(self.backup(), changes={'manifest.json': b'[]'})
        with self.assertRaises(BackupError):
            archive.inspect(bad)


class BackupAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.admin import create_admin_app
        from src.admin_auth import AdminAuth, COOKIE_NAME
        from src.feature.manager import FeatureManager
        from src.master import MasterService
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.root = Path.cwd()
        write_json(self.root / 'data/admin/auth.json', {'password': 'backup-test-password'})
        self.manager = FeatureManager(self.root / 'data/features.json')
        self.control = BackupControl(self.root, self.manager)
        self.app = create_admin_app(self.manager, MasterService(self.root / 'data/master.json'), backup_control=self.control)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.client.cookies.set(COOKIE_NAME, AdminAuth().issue())

    async def test_all_backup_routes_require_login(self):
        self.client.cookies.clear()
        for method, url in [('GET', '/api/backups'), ('POST', '/api/backups'), ('POST', '/api/backups/import'), ('GET', '/api/backups/x.zip/download'), ('POST', '/api/backups/restore')]:
            result = await self.client.request(method, url)
            self.assertEqual(result.status_code, 401)

    async def test_admin_contains_dedicated_backup_view(self):
        response = await self.client.get('/')
        self.assertIn('id="view-backups"', response.text)
        self.assertIn('data-view-link="backups"', response.text)
        self.assertNotIn('/* BACKUP_SCRIPT */', response.text)

    async def test_upload_validate_download_delete(self):
        name = self.control.store.make_backup(env={})
        package = self.control.store.package(name).read_bytes()
        response = await self.client.post('/api/backups/import', content=package)
        self.assertEqual(response.status_code, 200, response.text)
        imported = response.json()['name']
        preview = await self.client.get(f'/api/backups/{imported}/preview')
        self.assertEqual(preview.status_code, 200)
        download = await self.client.get(f'/api/backups/{imported}/download')
        self.assertEqual(download.content, package)
        self.assertEqual(download.headers['cache-control'], 'no-store')
        self.assertEqual((await self.client.delete(f'/api/backups/{imported}')).status_code, 200)
        self.assertEqual((await self.client.get(f'/api/backups/{imported}/download')).status_code, 409)

    async def test_corrupt_upload_is_not_published(self):
        response = await self.client.post('/api/backups/import', content=b'not-a-zip')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(self.control.store.packages.iterdir()), [])

    async def test_request_requires_supervisor_then_enters_maintenance_and_shuts_down(self):
        called = []
        self.control.shutdown = lambda: called.append(True)
        with patch.dict(os.environ, {}, clear=True):
            response = await self.client.post('/api/backups')
            self.assertEqual(response.status_code, 409)
        with patch.dict(os.environ, {'MYQQBOT_BACKUP_SUPERVISED': '1'}):
            response = await self.client.post('/api/backups')
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(self.manager.maintenance)
        self.assertEqual(called, [True])
        self.assertEqual((await self.client.post('/api/features/ai', json={'enabled': True})).status_code, 503)
        self.assertEqual((await self.client.get('/api/backups/status')).json()['state'], 'queued')

    async def test_restore_requires_confirmation_and_busy_task_blocks_request(self):
        name = self.control.store.make_backup(env={})
        self.control.shutdown = lambda: None
        body = {'name': name, 'mode': 'overwrite', 'confirmation': 'wrong'}
        self.assertEqual((await self.client.post('/api/backups/restore', json=body)).status_code, 409)
        self.manager.features['jm'] = SimpleNamespace(name='jm', title='JM', queue=SimpleNamespace(busy=True))
        with patch.dict(os.environ, {'MYQQBOT_BACKUP_SUPERVISED': '1'}):
            response = await self.client.post('/api/backups')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.manager.maintenance)


class BackupProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_process_backup_restart_restore_restart(self):
        """Actual supervisor + two Uvicorn servers; no QQ or remote model calls."""
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(project / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            write_json(root / 'data/admin/auth.json', {'password': 'process-backup-test'})
            (root / 'data/ai').mkdir()
            (root / 'data/ai/config.yaml').write_text('vector_index_enabled: false\nmemory_auto_learn: false\n', 'utf-8')
            probe = root / 'data/restore-probe.txt'
            probe.write_text('original', 'utf-8')
            sockets = [socket.socket(), socket.socket()]
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            bot_port, admin_port = [sock.getsockname()[1] for sock in sockets]
            for sock in sockets:
                sock.close()
            env = {**os.environ, 'MYQQBOT_WS_HOST': '127.0.0.1', 'MYQQBOT_ADMIN_HOST': '127.0.0.1',
                   'MYQQBOT_WS_PORT': str(bot_port), 'MYQQBOT_ADMIN_PORT': str(admin_port),
                   'AI_VECTOR_INDEX_ENABLED': 'false', 'AI_MEMORY_AUTO_LEARN': 'false',
                   'DEEPSEEK_API_KEY': '', 'AI_API_KEY': '', 'PYTHONUTF8': '1'}
            with (root / 'process-test.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, '-m', 'src.main'], cwd=root, env=env, stdout=log, stderr=log,
                                           start_new_session=os.name != 'nt')
                try:
                    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{admin_port}', timeout=2, trust_env=False) as client:
                        async def wait_ready(identifier=None):
                            deadline = asyncio.get_running_loop().time() + 40
                            while asyncio.get_running_loop().time() < deadline:
                                if process.poll() is not None:
                                    self.fail(f'服务进程意外退出：{process.returncode}')
                                try:
                                    response = await client.post('/api/login', json={'password': 'process-backup-test'})
                                    if response.status_code == 200:
                                        state = (await client.get('/api/backups/status')).json()
                                        if identifier is None or state.get('id') == identifier and state['state'] not in {'queued', 'running'}:
                                            return state
                                except (httpx.HTTPError, ValueError):
                                    pass
                                await asyncio.sleep(0.15)
                            self.fail('等待服务重新启动超时')

                        await wait_ready()
                        response = await client.post('/api/backups')
                        self.assertEqual(response.status_code, 202, response.text)
                        result = await wait_ready(response.json()['id'])
                        self.assertEqual(result['state'], 'succeeded', result)
                        name = result['package']
                        archive.inspect(root / 'data/backups/packages' / name)
                        probe.write_text('changed', 'utf-8')
                        (root / 'data/current-only.txt').write_text('keep only for incremental', 'utf-8')
                        for mode, confirmation in [('incremental', '增量还原'), ('overwrite', '覆盖还原')]:
                            response = await client.post('/api/backups/restore', json={'name': name, 'mode': mode, 'confirmation': confirmation})
                            self.assertEqual(response.status_code, 202, response.text)
                            result = await wait_ready(response.json()['id'])
                            self.assertEqual(result['state'], 'succeeded', result)
                            self.assertEqual(probe.read_text('utf-8'), 'original')
                            self.assertEqual((root / 'data/current-only.txt').exists(), mode == 'incremental')
                        # One package carries its matching code and data. Prove
                        # the shipped CLI works from the extracted clean project.
                        package = root / 'data/backups/packages' / name
                        portable = root / 'portable'
                        archive.extract(package, portable)
                        clean = portable / 'project'
                        result = await asyncio.to_thread(subprocess.run,
                            [sys.executable, '-m', 'src.backup.cli', 'restore', str(package), '--mode', 'overwrite'],
                            cwd=clean, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((clean / 'data/restore-probe.txt').read_text('utf-8'), 'original')
                        self.assertTrue((clean / 'data/ai/workspace/memory/soul/AGENT.md').is_file())
                finally:
                    if os.name == 'nt':
                        # This PID is the test's own supervisor; reap only its
                        # process tree (Windows terminate bypasses signal handlers).
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    elif process.poll() is None:
                        import signal
                        os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.to_thread(process.wait, 30)


if __name__ == '__main__':
    unittest.main()
