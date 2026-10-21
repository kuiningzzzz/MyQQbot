"""SQLite claims advance the schedule before delivery: unknown sends never retry."""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

from .schedule import next_run, parse_time, base_flag, BJT


class Conflict(ValueError):
    pass


class TaskStore:
    def __init__(self, path=Path('data/timing/tasks.sqlite3')):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, scene TEXT NOT NULL,
                    data TEXT NOT NULL, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_run REAL, created REAL NOT NULL,
                    updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS task_due ON tasks(enabled,next_run);
                CREATE INDEX IF NOT EXISTS task_scene ON tasks(scene,kind);
                CREATE TABLE IF NOT EXISTS installations (id TEXT PRIMARY KEY, installed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    scheduled REAL NOT NULL, started REAL NOT NULL, finished REAL,
                    status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, scheduled));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        return {**json.loads(row['data']), **{k: row[k] for k in ('id', 'kind', 'scene', 'revision', 'next_run', 'created', 'updated')}, 'enabled': bool(row['enabled'])}

    def get(self, identifier, *, scene=None, kind=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if row is None or (scene is not None and row['scene'] != scene) or (kind is not None and row['kind'] != kind):
                return None
            return self.decode(row)

    def list(self, *, scene=None, kind=None):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM tasks ORDER BY created,id').fetchall()
            results = []
            for row in rows:
                if (scene is not None and row['scene'] != scene) or (kind is not None and row['kind'] != kind):
                    continue
                item = self.decode(row)
                last = db.execute('SELECT * FROM runs WHERE task_id=? ORDER BY started DESC LIMIT 1', (item['id'],)).fetchone()
                item['last_run'] = dict(last) if last else None
                results.append(item)
            return results

    def save(self, data, *, identifier=None, expected=None, now=None, installation=None):
        now = time.time() if now is None else now
        data = dict(data)
        data.pop('last_run', None)
        parse_time(data['flag'], data['time'])
        identifier = identifier or uuid.uuid4().hex
        enabled = bool(data.get('enabled', True))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if installation and db.execute('SELECT 1 FROM installations WHERE id=?', (installation,)).fetchone():
                return self.get(identifier)
            previous = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if expected is not None and (previous is None or previous['revision'] != expected):
                raise Conflict('任务已被其他人修改或删除，请刷新后重试')
            if previous is None and db.execute('SELECT count(*) FROM tasks WHERE scene=?', (data['scene'],)).fetchone()[0] >= 1000:
                raise ValueError('当前会话最多保留1000个定时任务，请先删除不用的任务')
            revision = previous['revision'] + 1 if previous else 1
            created = previous['created'] if previous else now
            try:
                due = next_run(data['flag'], data['time'], datetime.fromtimestamp(now, BJT))
            except ValueError:
                old = json.loads(previous['data']) if previous else {}
                if base_flag(data['flag']) != '-o' or enabled or old.get('flag') != data['flag'] or old.get('time') != data['time']:
                    raise
                due = previous['next_run']
            # Keep timestamp on pause/unpause or content-only edits. A past
            # completed one-shot can be edited/paused, but cannot be re-enabled.
            if previous and json.loads(previous['data'])['flag'] == data['flag'] and json.loads(previous['data'])['time'] == data['time']:
                due = previous['next_run'] if previous['next_run'] and previous['next_run'] > now else due
            payload = json.dumps(data, ensure_ascii=False)
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,scene=excluded.scene,data=excluded.data,revision=excluded.revision,enabled=excluded.enabled,next_run=excluded.next_run,updated=excluded.updated',
                       (identifier, data['kind'], data['scene'], payload, revision, int(enabled), due, created, now))
            if installation:
                db.execute('INSERT INTO installations VALUES (?,?)', (installation, now))
        return self.get(identifier)

    def delete(self, identifier, *, scene=None, kind=None, expected=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if not row or (scene is not None and row['scene'] != scene) or (kind is not None and row['kind'] != kind):
                return False
            if expected is not None and row['revision'] != expected:
                raise Conflict('任务已被修改，请刷新后重试')
            db.execute('DELETE FROM tasks WHERE id=?', (identifier,))
            return True

    def claim(self, identifier, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=? AND enabled=1 AND next_run<=?', (identifier, now)).fetchone()
            if not row or db.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (identifier,)).fetchone():
                return None
            item = self.decode(row)
            run = uuid.uuid4().hex
            db.execute('INSERT INTO runs(id,task_id,scheduled,started,status) VALUES (?,?,?,?,?)', (run, identifier, row['next_run'], now, 'running'))
            once = base_flag(item['flag']) == '-o'
            due = None if once else next_run(item['flag'], item['time'], datetime.fromtimestamp(now, BJT))
            db.execute('UPDATE tasks SET next_run=?,enabled=? WHERE id=?', (due, int(not once), identifier))
            return {**item, 'run_id': run, 'scheduled_at': row['next_run']}

    def due(self, now):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM tasks WHERE enabled=1 AND next_run<=? ORDER BY next_run,id', (now,))]

    def finish(self, run, status, result=''):
        with self.connect() as db:
            db.execute("UPDATE runs SET finished=?,status=?,result=? WHERE id=? AND status='running'", (time.time(), status, str(result)[:24000], run))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='unknown',finished=?,result='服务上次异常退出，执行结果未知，不自动重试' WHERE status='running'", (time.time(),))
        self.prune()

    def prune(self):
        with self.connect() as db:
            db.execute('DELETE FROM runs WHERE started<?', (time.time() - 30 * 86400,))
