import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.feature.ai.dsh_upgrade import DSH_VERSION, RuntimeUpgrade
from src.feature.ai.storage import atomic_write


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upgrade = RuntimeUpgrade(self.root)
        self.log = self.upgrade.home / 'sessions/project/chat-test/session.v4.jsonl'
        atomic_write(self.log, 'old checkpoint\n')
        self.deadline = time.time() + 3600
        atomic_write(self.upgrade.home / 'qqbot-retention.json', json.dumps({'chat-test': self.deadline * 1000}))
        self.unrelated = self.root / 'workspace/memory/user/123/PERSON.md'
        atomic_write(self.unrelated, '保留用户数据\n')

    def test_snapshot_is_verified_idempotent_and_commits_only_after_ready(self):
        state = self.upgrade.prepare()
        snapshot = self.upgrade.directory / state['backup']
        metadata = json.loads((snapshot / 'snapshot.json').read_text('utf-8'))
        key = self.log.relative_to(self.upgrade.home).as_posix()
        self.assertEqual(metadata['sha256'][key], hashlib.sha256(self.log.read_bytes()).hexdigest())
        self.assertEqual((snapshot / 'runtime' / key).read_bytes(), self.log.read_bytes())
        # Millisecond persistence and conversion back to seconds can round by
        # one floating-point ULP; the retention deadline is unchanged in use.
        self.assertAlmostEqual(metadata['expires_at'], self.deadline, delta=1e-6)
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state, self.upgrade.prepare())
        self.upgrade.complete()
        self.assertEqual(self.upgrade.prepare()['version'], DSH_VERSION)
        self.assertEqual(self.upgrade.prepare()['status'], 'ready')
        self.assertEqual(self.unrelated.read_text('utf-8'), '保留用户数据\n')
        with patch('src.feature.ai.dsh_upgrade.time.time', return_value=self.deadline + 1):
            self.upgrade.prune()
        self.assertFalse(snapshot.exists())
        self.assertTrue(self.log.exists())

    def test_failed_snapshot_stops_upgrade_without_changing_source(self):
        with patch('src.feature.ai.dsh_upgrade.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.upgrade.prepare()
        self.assertFalse(self.upgrade.marker.exists())
        self.assertEqual(self.log.read_text('utf-8'), 'old checkpoint\n')
        self.assertEqual(list(self.upgrade.directory.glob('*.partial')), [])
        self.assertEqual(self.upgrade.prepare()['status'], 'pending')

    def test_expired_sessions_not_resurrected_in_backup(self):
        atomic_write(self.upgrade.home / 'qqbot-retention.json', json.dumps({'chat-test': (time.time() - 1) * 1000}))
        state = self.upgrade.prepare()
        snapshot = self.upgrade.directory / state['backup']
        metadata = json.loads((snapshot / 'snapshot.json').read_text('utf-8'))
        self.assertEqual(metadata['expired_sessions_excluded'], ['chat-test'])
        self.assertFalse((snapshot / 'runtime/sessions').exists())
        self.assertTrue(self.log.exists())

    def test_unknown_version_fails_before_snapshot_or_data_changes(self):
        atomic_write(self.upgrade.marker, json.dumps({'version': 'future-version', 'status': 'ready'}))
        with self.assertRaisesRegex(RuntimeError, '不支持自动迁移'):
            self.upgrade.prepare()
        self.assertEqual(list(self.upgrade.directory.glob('*/snapshot.json')), [])
        self.assertEqual(self.log.read_text('utf-8'), 'old checkpoint\n')
