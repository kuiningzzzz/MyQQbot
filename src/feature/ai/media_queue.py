"""Persistent media enrichment, independent of QQ event and conversation queues."""
import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from src.feature.atom import MessageContext
from .media import IMAGE_SUFFIXES
from .workfiles import WorkFiles

logger = logging.getLogger(__name__)
PENDING = '媒体后台解析中'


def needs_media(segments):
    if not isinstance(segments, list):
        return False
    return any(isinstance(s, dict) and (s.get('type') in {'image', 'forward', 'node'} or
        s.get('type') == 'file' and Path(WorkFiles.attachment_name('file', s.get('data') or {})).suffix.lower() in IMAGE_SUFFIXES | {'.pdf'}) for s in segments)


class MediaQueue:
    CONCURRENCY = 2

    def __init__(self, feature):
        self.feature = feature
        self.path = feature.workspace.data_root / '.state/media-jobs.sqlite3'
        self.workers = []
        self.running = False
        self.closing = False
        self.bot = None
        self.connections = {}
        self.wake = asyncio.Event()
        self.errors = set()
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
                results TEXT NOT NULL DEFAULT '{}', forwards TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '')''')
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, ctx):
        self.bot = ctx.bot
        self.connections[str(ctx.event.get('self_id') or '')] = ctx.websocket

    def enqueue(self, seq, priority=False):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO jobs(seq,priority) VALUES (?,?)', (seq, int(priority)))
        self.ensure_workers()
        self.wake.set()

    def ensure_workers(self):
        if self.closing:
            return
        if not self.running:
            self.running = True
        for task in self.workers:
            if task.done() and not task.cancelled() and task.exception():
                logger.error('媒体工作协程异常退出：%s', type(task.exception()).__name__)
        self.workers = [t for t in self.workers if not t.done()]
        while len(self.workers) < self.CONCURRENCY:
            self.workers.append(asyncio.create_task(self._worker()))

    def job(self, seq):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE seq=?', (seq,)).fetchone()
            return dict(row) if row else None

    def statistics(self):
        with self.connect() as db:
            return {row['status']: row['count'] for row in db.execute('SELECT status,count(*) AS count FROM jobs GROUP BY status')}

    def partial(self, seq, identifier, description, complete=True):
        with self.connect() as db:
            row = db.execute('SELECT results FROM jobs WHERE seq=?', (seq,)).fetchone()
            if row:
                results = json.loads(row['results'])
                results[identifier] = {'status': 'completed' if complete else 'failed', 'description': str(description)}
                db.execute('UPDATE jobs SET results=? WHERE seq=?', (json.dumps(results, ensure_ascii=False), seq))
        if not complete:
            self.errors.add(seq)

    async def forward(self, seq, identifier, ctx):
        row = self.job(seq)
        snapshots = json.loads(row['forwards'])
        if identifier not in snapshots:
            result = await ctx.bot.call_action('get_forward_msg', {'message_id': identifier}, ctx.websocket)
            snapshots[identifier] = result.get('messages', [])
            with self.connect() as db:
                db.execute('UPDATE jobs SET forwards=? WHERE seq=?', (json.dumps(snapshots, ensure_ascii=False), seq))
        return snapshots[identifier]

    def _source(self, seq):
        with self.feature.journal.connect() as db:
            row = db.execute('SELECT * FROM messages WHERE seq=?', (seq,)).fetchone()
        if not row or row['timestamp'] <= time.time() - 30 * 86400:
            return None
        return dict(row)

    def _context(self, data):
        event = dict(data.get('event') or {})
        record = data['record']
        ws = self.connections.get(str(event.get('self_id') or ''))
        if hasattr(self.bot, 'connected_clients'):
            clients = self.bot.connected_clients
            if ws not in clients:
                accounts = getattr(self.bot, '_self_ids', {})
                ws = next((s for s in clients if not event.get('self_id') or accounts.get(s) == str(event['self_id'])), None)
            if ws is None:
                raise ConnectionError('等待NapCat连接恢复')
        event['message_id'] = record['id']
        return MessageContext(bot=self.bot, websocket=ws, event=event, msg='',
            message_type=record['scene_type'], user_id=int(record['qq']),
            group_id=int(record['scene_id']) if record['scene_type'] == 'group' else None)

    async def _worker(self):
        while self.running:
            selected = None
            try:
                if self.bot and self.feature.enabled:
                    with self.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        selected = db.execute("SELECT * FROM jobs WHERE status IN ('queued','retry') AND due<=? ORDER BY priority DESC,seq LIMIT 1", (time.time(),)).fetchone()
                        if selected:
                            db.execute("UPDATE jobs SET status='running' WHERE seq=?", (selected['seq'],))
                if selected is None:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), 1)
                    except TimeoutError:
                        pass
                    continue
                seq = selected['seq']
                source = self._source(seq)
                if source is None:
                    with self.connect() as db:
                        db.execute('DELETE FROM jobs WHERE seq=?', (seq,))
                    continue
                data = self.feature.journal.load(source)
                ctx = self._context(data)
                self.errors.discard(seq)
                content = await self.feature._render(ctx.event.get('message') or [], ctx, sequence=seq)
                if not self.feature.enabled:
                    with self.connect() as db:
                        db.execute("UPDATE jobs SET status='queued' WHERE seq=?", (seq,))
                    continue
                failed = seq in self.errors
                attempt = selected['attempts'] + 1
                if failed and attempt < 3:
                    with self.connect() as db:
                        db.execute("UPDATE jobs SET status='retry',attempts=?,due=? WHERE seq=?", (attempt, time.time() + 5 * 2 ** attempt, seq))
                    continue
                if failed:
                    content += '\n[部分媒体解析失败；OCR/已有结果已保留，可用read_media明确重试]'
                published = await asyncio.to_thread(self.feature.journal.annotate, seq, content, media_update=True)
                if published:
                    record = data['record']
                    await self.feature.context.update_content(f"{record['scene_type']}:{record['scene_id']}", record['id'], content)
                with self.connect() as db:
                    db.execute('UPDATE jobs SET status=?,attempts=?,error=? WHERE seq=?', ('failed' if failed else 'completed', attempt, '部分附件解析失败' if failed else '', seq))
                self.feature.runtime.trigger()
            except asyncio.CancelledError:
                if selected:
                    with self.connect() as db:
                        db.execute("UPDATE jobs SET status='queued' WHERE seq=? AND status='running'", (selected['seq'],))
                raise
            except Exception as exc:
                if selected:
                    attempt = selected['attempts'] + (0 if isinstance(exc, ConnectionError) else 1)
                    with self.connect() as db:
                        db.execute('UPDATE jobs SET status=?,attempts=?,due=?,error=? WHERE seq=?',
                            ('retry' if attempt < 3 else 'failed', attempt, time.time() + 10 * 2 ** min(attempt, 3), type(exc).__name__, selected['seq']))
                    logger.warning('媒体后台处理失败：seq=%s type=%s', selected['seq'], type(exc).__name__)
                    if attempt >= 3:
                        source = self._source(selected['seq'])
                        if source:
                            notice = self.feature.journal.load(source).get('rendered_content', '')
                            notice = notice.replace(PENDING, '媒体后台解析失败，可用read_media重试')
                            await asyncio.to_thread(self.feature.journal.annotate, source['seq'], notice, media_update=True)
                            self.feature.runtime.trigger()
                else:
                    logger.warning('媒体队列暂不可用：%s', type(exc).__name__)
                await asyncio.sleep(1)
            finally:
                if selected is not None:
                    self.errors.discard(selected['seq'])

    async def start(self, bot):
        self.closing = False
        self.bot = bot
        def recover_jobs():
            with self.connect() as db:
                existing = {row['seq'] for row in db.execute('SELECT seq FROM jobs')}
            for row in self.feature.journal.rows(limit=2 ** 31 - 1):
                if row['seq'] in existing:
                    continue
                data = self.feature.journal.load(row)
                if needs_media((data.get('event') or {}).get('message')) and (not data.get('rendered_content') or PENDING in data['rendered_content']):
                    with self.connect() as db:
                        db.execute('INSERT OR IGNORE INTO jobs(seq) VALUES (?)', (row['seq'],))
        await asyncio.to_thread(recover_jobs)
        self.ensure_workers()

    async def read(self, ctx, *, message_id=None, attachment_id=None, wait_seconds=0, retry=False, offset=0):
        if self.closing:
            return {'success': False, 'code': 'cancelled'}
        if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 30 or not isinstance(retry, bool):
            raise ValueError('wait_seconds须为0至30的整数，retry须为布尔值')
        if not isinstance(offset, int) or offset < 0:
            raise ValueError('offset须为非负整数')
        scene = self.feature.scene_key(ctx)
        if attachment_id:
            item = self.feature.workfiles.attachments.get(attachment_id)
            if not item or item['scene'] != scene:
                return {'success': False, 'code': 'attachment_not_found'}
            message_id = item['message_id']
        if not message_id:
            raise ValueError('请提供真实message_id或attachment_id')
        with self.feature.journal.connect() as db:
            source = db.execute('SELECT * FROM messages WHERE identity=?', (scene + ':' + str(message_id),)).fetchone()
        if not source or not self._source(source['seq']):
            return {'success': False, 'code': 'message_not_found'}
        seq = source['seq']
        row = self.job(seq)
        if row is None:
            return {'success': False, 'code': 'no_media_job'}
        self.bind(ctx)
        with self.connect() as db:
            if retry and row['status'] == 'failed':
                db.execute("UPDATE jobs SET status='queued',attempts=0,due=0 WHERE seq=?", (seq,))
            db.execute('UPDATE jobs SET priority=1,due=0 WHERE seq=?', (seq,))
        self.ensure_workers()
        self.wake.set()
        until = time.monotonic() + wait_seconds
        while True:
            row = self.job(seq)
            if row is None:
                return {'success': False, 'code': 'message_not_found'}
            result = json.loads(row['results']).get(attachment_id) if attachment_id else None
            if (result and result['status'] == 'completed') or row['status'] in {'completed', 'failed'} or time.monotonic() >= until:
                break
            await asyncio.sleep(.2)
        if not self._source(seq):
            return {'success': False, 'code': 'message_not_found'}
        path = (self.feature.workspace.memory_root / source['path']).with_suffix('.rendered.txt')
        description = result['description'] if result else path.read_text('utf-8') if path.exists() else ''
        return {'success': True, 'status': result['status'] if result else row['status'],
                'message_id': str(message_id), 'description': description[offset:offset + 12000],
                'truncated': len(description) > offset + 12000, 'next_offset': offset + 12000 if len(description) > offset + 12000 else None,
                'rendered_path': str(path) if row['status'] in {'completed', 'failed'} and path.exists() else None, 'error': row['error']}

    async def prune(self):
        def prune_jobs():
            with self.feature.journal.connect() as db:
                db.execute('BEGIN')
                counter = db.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
                highwater = counter[0] if counter else 0
                valid = {row['seq'] for row in db.execute('SELECT seq FROM messages WHERE timestamp>?', (time.time() - 30 * 86400,))}
            with self.connect() as db:
                # A message enqueued after the source snapshot is new, not an
                # orphan. Never delete it using the older snapshot.
                expired = [(row['seq'],) for row in db.execute('SELECT seq FROM jobs WHERE seq<=?', (highwater,)) if row['seq'] not in valid]
                db.executemany('DELETE FROM jobs WHERE seq=?', expired)
        await asyncio.to_thread(prune_jobs)
        await asyncio.to_thread(self.feature.files.cache.prune)
        if self.running:
            self.ensure_workers()

    async def stop(self):
        self.closing = True
        self.running = False
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
