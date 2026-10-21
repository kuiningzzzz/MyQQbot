"""Host-owned, scene-bound, bounded temporary speech outputs. No model weights."""
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager


def junction(path):
    return getattr(path, 'is_junction', lambda: False)()


class VoiceStore:
    TTL = 86400
    MAX_BYTES = 128 * 1024 * 1024
    MAX_FILES = 200

    def __init__(self, workspace):
        self.root = workspace.data_root / '.state/voice-cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = workspace.data_root / '.state/voices.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS voices (id TEXT PRIMARY KEY, digest TEXT UNIQUE,
                scene TEXT NOT NULL, text TEXT NOT NULL, bytes INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL)''')
        self.prune()

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            with db:
                yield db

    def file(self, identifier):
        if not re.fullmatch(r'v-[a-f0-9]{32}', identifier or '') or self.root.is_symlink() or junction(self.root):
            raise ValueError('语音缓存路径无效')
        target = self.root / (identifier + '.wav')
        if target.is_symlink() or junction(target):
            raise ValueError('语音缓存不能使用链接')
        return target

    @staticmethod
    def digest(scene, config, text, instruction):
        import json
        return hashlib.sha256(json.dumps([scene, config, text, instruction], ensure_ascii=False).encode()).hexdigest()

    def get(self, scene, *, identifier=None, digest=None):
        with self.lock, self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM voices WHERE scene=? AND expires>? AND ' +
                ('id=?' if identifier else 'digest=?'), (scene, time.time(), identifier or digest)).fetchone()
            if row:
                file = self.file(row['id'])
                if file.is_file() and file.stat().st_size == row['bytes']:
                    return dict(row), file.read_bytes()

    def put(self, scene, digest, text, data):
        if not 0 < len(data) <= 20 * 1024 * 1024:
            raise ValueError('生成的语音须为1字节至20MiB')
        identifier = 'v-' + uuid.uuid4().hex
        with self.lock:
            self.prune()
            file = self.file(identifier)
            temporary = file.with_suffix('.tmp')
            try:
                with temporary.open('xb') as out:
                    out.write(data)
                os.replace(temporary, file)
                with self.connect() as db:
                    db.execute('INSERT INTO voices VALUES (?,?,?,?,?,?,?)',
                        (identifier, digest, scene, text, len(data), time.time(), time.time() + self.TTL))
            except BaseException:
                file.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
            self.prune()
        return identifier

    def prune(self):
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT id,bytes,expires FROM voices ORDER BY created DESC').fetchall()
            kept = set()
            size = 0
            for identifier, count, expires in rows:
                file = self.file(identifier)
                if expires <= time.time() or len(kept) >= self.MAX_FILES or size + count > self.MAX_BYTES or not file.is_file():
                    file.unlink(missing_ok=True)
                    db.execute('DELETE FROM voices WHERE id=?', (identifier,))
                else:
                    size += count
                    kept.add(file.name)
            # Recover files left between the atomic rename and database commit.
            for file in self.root.iterdir():
                if re.fullmatch(r'v-[a-f0-9]{32}\.(wav|tmp)', file.name) and file.name not in kept:
                    if not file.is_symlink() and not junction(file):
                        file.unlink(missing_ok=True)
