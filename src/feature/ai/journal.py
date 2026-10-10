"""Durable raw archive and monotonic 100-human-message queue."""
from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import time
from datetime import datetime, timedelta

from .storage import atomic_write


class Journal:
    def __init__(self, workspace):
        self.workspace = workspace
        self.path = workspace.data_root / ".state/messages.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT UNIQUE,
                  timestamp REAL NOT NULL, path TEXT NOT NULL, human INTEGER NOT NULL,
                  processed INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS queue ON messages(processed,human,seq);
            """)

        # Recover files published just before a crash interrupted SQLite commit.
        for file in (workspace.memory_root / 'raw').glob('*/*.json'):
            data = json.loads(file.read_text('utf-8'))
            record = data['record']
            identity = f"{record['scene_type']}:{record['scene_id']}:{record['id']}"
            with self.connect() as db:
                db.execute('INSERT OR IGNORE INTO messages(identity,timestamp,path,human) VALUES(?,?,?,?)', (identity, datetime.fromisoformat(record['timestamp']).timestamp(), file.relative_to(workspace.memory_root).as_posix(), int(not record.get('is_self') and record['scene_type'] != 'system')))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(self, record, event=None):
        stamp = datetime.fromisoformat(record["timestamp"]).timestamp()
        identity = f"{record['scene_type']}:{record['scene_id']}:{record['id']}"
        # Commit the archive file before publishing its queue row.
        import hashlib
        relative = "raw/" + datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") + "/" + hashlib.sha256(identity.encode()).hexdigest() + ".json"
        path = self.workspace.memory_root / relative
        with self.connect() as db:
            existing = db.execute("SELECT seq FROM messages WHERE identity=?", (identity,)).fetchone()
            if existing:
                return existing["seq"], False
            if path.exists():
                # A previous DB commit may have failed after publishing the
                # original. Never overwrite that immutable first observation.
                record = json.loads(path.read_text('utf-8'))['record']
                stamp = datetime.fromisoformat(record['timestamp']).timestamp()
            else:
                atomic_write(path, json.dumps({"record": record, "event": event}, ensure_ascii=False, indent=2))
            cursor = db.execute("INSERT INTO messages(identity,timestamp,path,human) VALUES(?,?,?,?)", (identity, stamp, relative, int(not record.get("is_self") and record.get("scene_type") != "system")))
            return cursor.lastrowid, True

    def rows(self, *, after=0, pending=False, limit=1000):
        query = "SELECT * FROM messages WHERE seq>?"
        if pending:
            query += " AND human=1 AND processed=0"
        with self.connect() as db:
            return [dict(r) for r in db.execute(query + " ORDER BY seq LIMIT ?", (after, limit))]

    def load(self, row):
        path = self.workspace.memory_root / row['path']
        data = json.loads(path.read_text('utf-8'))
        rendered = path.with_suffix('.rendered.txt')
        if rendered.exists():
            data['rendered_content'] = rendered.read_text('utf-8')
        return data

    def annotate(self, sequence, content):
        with self.connect() as db:
            row = db.execute('SELECT path FROM messages WHERE seq=?', (sequence,)).fetchone()
        if row:
            path = self.workspace.memory_root / row['path']
            atomic_write(path.with_suffix('.rendered.txt'), content)

    def acknowledge(self, sequences):
        with self.connect() as db:
            db.executemany("UPDATE messages SET processed=1 WHERE seq=?", [(seq,) for seq in sequences])

    def pending_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM messages WHERE human=1 AND processed=0").fetchone()[0]

    def oldest_timestamp(self):
        with self.connect() as db:
            value = db.execute('SELECT min(timestamp) FROM messages').fetchone()[0]
        for file in (self.workspace.memory_root / 'raw/legacy').glob('*.md'):
            try:
                # Conservative by one day for legacy timezone/day boundaries.
                stamp = datetime.fromisoformat(file.stem).timestamp() - 86400
                value = min(value, stamp) if value is not None else stamp
            except ValueError:
                pass
        return value

    def prune(self, now=None):
        cutoff = (now or time.time()) - 30 * 86400
        lost = 0
        with self.connect() as db:
            for row in db.execute("SELECT * FROM messages WHERE timestamp<?", (cutoff,)).fetchall():
                lost += int(row["human"] and not row["processed"])
                (self.workspace.memory_root / row["path"]).unlink(missing_ok=True)
                (self.workspace.memory_root / row['path']).with_suffix('.rendered.txt').unlink(missing_ok=True)
            db.execute("DELETE FROM messages WHERE timestamp<?", (cutoff,))
        # Imported legacy Markdown lacks per-message metadata. Its filename is
        # the old 03:00 period start; remove before its earliest record expires.
        for path in (self.workspace.memory_root / "raw/legacy").glob("*.md"):
            try:
                if datetime.fromisoformat(path.stem).timestamp() < cutoff:
                    path.unlink()
            except ValueError:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
        # Orphans can result from a crash between file publication and DB commit.
        for path in (self.workspace.memory_root / "raw").glob("*/*.json"):
            try:
                stamp = datetime.fromisoformat(json.loads(path.read_text("utf-8"))["record"]["timestamp"]).timestamp()
                if stamp < cutoff:
                    path.unlink(missing_ok=True)
                    path.with_suffix('.rendered.txt').unlink(missing_ok=True)
            except (KeyError, ValueError):
                continue
        for rendered in (self.workspace.memory_root / 'raw').glob('*/*.rendered.txt'):
            if not rendered.with_name(rendered.name.removesuffix('.rendered.txt') + '.json').exists():
                rendered.unlink(missing_ok=True)
        if lost:
            self.workspace.save_state("retention_warning", {"expired_unprocessed": lost, "at": now or time.time()})
        return lost
