"""A separate, account-scoped sticker catalogue; all keys survive backup merges."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class StickerStore:
    def __init__(self, root):
        self.path = root / '.state/stickers.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS stickers(
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, md5 TEXT NOT NULL,
                  data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                  UNIQUE(account,md5));
                CREATE TABLE IF NOT EXISTS sticker_state(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
            for row in db.execute('SELECT id,data FROM stickers').fetchall():
                data = json.loads(row['data'])
                if data.get('state') == 'processing':
                    data['state'] = 'queued'
                    db.execute('UPDATE stickers SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), row['id']))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def identifier(account, md5):
        return 's-' + hashlib.sha256((account + ':' + md5).encode()).hexdigest()[:24]

    def get(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT * FROM stickers WHERE id=?', (identifier,)).fetchone()
        return {**json.loads(row['data']), 'id': row['id'], 'account': row['account'], 'md5': row['md5'], 'revision': row['revision']} if row else None

    def list(self, account=None, *, deleted=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM stickers' + (' WHERE account=?' if account else '') + ' ORDER BY id', (account,) if account else ()).fetchall()
        result = [{**json.loads(row['data']), 'id': row['id'], 'account': row['account'], 'md5': row['md5'], 'revision': row['revision']} for row in rows]
        return [row for row in result if deleted or not row.get('deleted')]

    def save(self, account, md5, patch, *, expected=None):
        identifier = self.identifier(account, md5)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data,revision FROM stickers WHERE id=?', (identifier,)).fetchone()
            if expected is not None and (not row or row['revision'] != expected):
                raise ValueError('表情已被更新，请刷新后重试')
            data = json.loads(row['data']) if row else {'name': '', 'tags': [], 'description': '', 'auto_description': '',
                'enabled': True, 'deleted': False, 'state': 'queued', 'attempts': 0, 'due': 0,
                'remote': [], 'remote_present': False, 'asset': '', 'error': '', 'pending': None, 'used_at': 0}
            data.update(patch)
            data['updated_at'] = time.time()
            revision = row['revision'] + 1 if row else 1
            db.execute('INSERT INTO stickers VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,revision=excluded.revision',
                       (identifier, account, md5, json.dumps(data, ensure_ascii=False), revision))
        return self.get(identifier)

    def state(self, key, value=None):
        with self.connect() as db:
            if value is None:
                row = db.execute('SELECT data FROM sticker_state WHERE key=?', (key,)).fetchone()
                return json.loads(row[0]) if row else {}
            db.execute('INSERT INTO sticker_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data', (key, json.dumps(value, ensure_ascii=False)))
        return value

    @staticmethod
    def public(row, *, details=False):
        keys = ['id', 'account', 'name', 'tags', 'description', 'state', 'enabled', 'remote_present', 'revision', 'error', 'used_at', 'deleted']
        result = {key: row.get(key) for key in keys}
        result['name'] = row.get('name') or row.get('auto_name') or ''
        result['tags'] = row.get('tags') or row.get('auto_tags') or []
        result['description'] = (row.get('description') or row.get('auto_description') or row.get('remote_description') or '')[:4000 if details else 300]
        result['has_image'] = bool(row.get('asset'))
        result['pending_operation'] = (row.get('pending') or {}).get('action')
        return result
