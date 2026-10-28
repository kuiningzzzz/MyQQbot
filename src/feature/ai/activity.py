"""Private, persistent AI activity journal; never delivered to QQ."""
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager


class ActivityLog:
    def __init__(self, workspace):
        self.path = workspace.data_root / '.state/ai-activity.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, kind TEXT NOT NULL, task TEXT NOT NULL, role TEXT NOT NULL, scene TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_time ON activity(time)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        for key, secret in os.environ.items():
            if re.search(r'KEY|TOKEN|PASSWORD|SECRET', key, re.I) and len(secret) >= 4:
                text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'Bearer\s+[^\s"\\]+', 'Bearer [REDACTED]', text, flags=re.I)
        text = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', text)
        return text

    def add(self, kind, data=None, *, task='', role='', scene=''):
        with self.connect() as db:
            cursor = db.execute('INSERT INTO activity(time,kind,task,role,scene,data) VALUES(?,?,?,?,?,?)',
                (time.time(), kind, str(task), role, scene, self.clean(data)))
            return cursor.lastrowid

    def update(self, identifier, data):
        with self.connect() as db:
            db.execute("UPDATE activity SET data=? WHERE id=? AND kind='sleep_summary'", (self.clean(data), identifier))

    def read(self, after=0, limit=100, kind='', task='', before=0):
        if not 1 <= limit <= 300 or min(after, before) < 0:
            raise ValueError('日志分页参数无效')
        where, values = ['id>?'], [after]
        for field, value in [('kind', kind), ('task', task)]:
            if value:
                where.append(field + '=?')
                values.append(value)
        if before:
            where.append('id<?'); values.append(before)
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            # Initial/history pages show the latest entries, live polling goes forward.
            order = 'ASC' if after else 'DESC'
            rows = [dict(r) for r in db.execute('SELECT * FROM activity WHERE ' + ' AND '.join(where) + f' ORDER BY id {order} LIMIT ?', (*values, limit))]
        for row in rows:
            row['data'] = json.loads(row['data'])
        return sorted(rows, key=lambda r: r['id'])

    def prune(self):
        with self.connect() as db:
            db.execute('DELETE FROM activity WHERE time<?', (time.time() - 30 * 86400,))
