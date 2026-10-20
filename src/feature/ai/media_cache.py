"""Bounded, expiring content cache; no URLs, filenames or QQ authority in keys."""
import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager, asynccontextmanager


class MediaCache:
    VERSION = 'ocr-vision-2'

    def __init__(self, root):
        self.path = root / '.state/media-cache.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.locks = {}
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, content TEXT NOT NULL, expires REAL NOT NULL, accessed REAL NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def key(self, data, settings, identity, pdf):
        config = json.dumps([self.VERSION, settings.vision_model, settings.api_url,
                             settings.ocr_enabled, identity, pdf], ensure_ascii=False)
        return hashlib.sha256(config.encode() + b'\0' + data).hexdigest()

    @asynccontextmanager
    async def singleflight(self, key):
        slot = self.locks.setdefault(key, [asyncio.Lock(), 0])
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if not slot[1]:
                self.locks.pop(key, None)

    def get(self, key):
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT content FROM cache WHERE key=? AND expires>?', (key, now)).fetchone()
            if row:
                db.execute('UPDATE cache SET accessed=? WHERE key=?', (now, key))
                return row[0]

    def put(self, key, content, expires):
        if expires <= time.time():
            return
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)', (key, str(content), expires, time.time()))
        self.prune()

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('DELETE FROM cache WHERE expires<=?', (now,))
            # Bound both entry count and disk payload. Reuse never extends the
            # original deadline, so repeated emoji cannot retain an old result forever.
            rows = db.execute('SELECT key,length(CAST(content AS BLOB)) FROM cache ORDER BY accessed DESC').fetchall()
            size = 0
            evict = []
            for number, (key, length) in enumerate(rows):
                size += length
                if number >= 5000 or size > 128 * 1024 * 1024:
                    evict.append((key,))
            db.executemany('DELETE FROM cache WHERE key=?', evict)
