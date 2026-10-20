"""Backup wins conflicts; preserve current-only keys, rows and files."""
from __future__ import annotations

import json
import heapq
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .archive import sqlite_file
from .common import BackupError, read_json, regular_tree, remove, safe_name, write_json


def deep_merge(current, incoming):
    if isinstance(current, dict) and isinstance(incoming, dict):
        return {**current, **{k: deep_merge(current.get(k), v) for k, v in incoming.items()}}
    return incoming


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def merge_database(current: Path, incoming: Path, *, skip=()):
    """Insert current-only rows into the backup copy; all unique keys win there.

    Unknown databases require identical schema and an explicit primary key. We
    reject ambiguous merges before touching live data instead of guessing keys.
    """
    with closing(sqlite3.connect(current)) as source, closing(sqlite3.connect(incoming)) as target:
        def schemas(db):
            return {r[0]: (r[1] or '').strip() for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        old, new = schemas(source), schemas(target)
        if old != new or any('VIRTUAL TABLE' in sql.upper() for sql in old.values()):
            raise BackupError(f'数据库结构不同或为虚拟表，不能增量合并：{current.name}；可使用覆盖还原')
        target.execute('PRAGMA foreign_keys=OFF')
        with target:
            for table in old:
                if table in skip:
                    continue
                fields = source.execute(f'PRAGMA table_info({quote(table)})').fetchall()
                if not any(r[5] for r in fields):
                    raise BackupError(f'数据表没有主键，无法可靠增量合并：{current.name}/{table}')
                columns = ','.join(quote(r[1]) for r in fields)
                placeholders = ','.join('?' for _ in fields)
                # ON CONFLICT DO NOTHING handles every unique index; unlike
                # INSERT OR IGNORE it does not suppress malformed NOT NULL rows.
                sql = f'INSERT INTO {quote(table)} ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
                target.executemany(sql, source.execute(f'SELECT {columns} FROM {quote(table)}'))
            if target.execute('PRAGMA foreign_key_check').fetchone():
                raise BackupError(f'数据库合并后存在无效关联：{current.name}')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise BackupError(f'数据库合并校验失败：{current.name}')


def merge_journal(current: Path, incoming: Path):
    """Preserve backup seq IDs; remap current-only messages and their media IDs."""
    mapping = {}
    added = set()
    with closing(sqlite3.connect(current)) as old, closing(sqlite3.connect(incoming)) as new:
        old.row_factory = new.row_factory = sqlite3.Row
        with new:
            for row in old.execute('SELECT * FROM messages ORDER BY seq'):
                known = new.execute('SELECT seq FROM messages WHERE identity=?', (row['identity'],)).fetchone()
                if known:
                    mapping[row['seq']] = known['seq']
                else:
                    cursor = new.execute('INSERT INTO messages(identity,timestamp,path,human,processed) VALUES(?,?,?,?,?)',
                                         tuple(row[k] for k in ('identity', 'timestamp', 'path', 'human', 'processed')))
                    mapping[row['seq']] = cursor.lastrowid
                    added.add(row['seq'])
            for row in old.execute('SELECT * FROM media_updates ORDER BY id'):
                if row['seq'] not in mapping:
                    continue
                seq = mapping[row['seq']]
                # Conflicts include the message identity, not only queue row ID.
                # Never attach an old result to the backup's newer message.
                if row['seq'] in added or not new.execute('SELECT 1 FROM media_updates WHERE seq=?', (seq,)).fetchone():
                    new.execute('INSERT OR IGNORE INTO media_updates(seq,revision,consumed) VALUES(?,?,?)',
                                (seq, row['revision'], row['consumed']))
    return mapping


def merge_jobs(current: Path, incoming: Path, mapping):
    with closing(sqlite3.connect(current)) as old, closing(sqlite3.connect(incoming)) as new:
        old.row_factory = sqlite3.Row
        fields = [r[1] for r in old.execute('PRAGMA table_info(jobs)')]
        if fields != [r[1] for r in new.execute('PRAGMA table_info(jobs)')]:
            raise BackupError('媒体任务数据库结构不兼容')
        with new:
            for row in old.execute('SELECT * FROM jobs'):
                if row['seq'] in mapping:
                    values = dict(row)
                    values['seq'] = mapping[row['seq']]
                    new.execute(f'INSERT INTO jobs ({",".join(quote(f) for f in fields)}) VALUES ({",".join("?" for _ in fields)}) ON CONFLICT DO NOTHING',
                                [values[f] for f in fields])


def merge_tree(current: Path, incoming: Path):
    """Merge into an already-extracted backup tree, never the running data root."""
    if any((root / 'ai/.state/memory-commit.json').exists() for root in (current, incoming)):
        raise BackupError('发现未完成的记忆提交，不能增量合并；请先在原环境启动完成恢复后重新备份，或选择覆盖还原')
    journal = Path('ai/.state/messages.sqlite3')
    jobs = Path('ai/.state/media-jobs.sqlite3')
    mapping = None
    context_records = {}
    for data in (current, incoming):
        for scene, value in read_json(data / 'ai/.state/context.json', {}).items():
            for record in value.get('recent_messages', []):
                identity = (scene, str(record['id']))
                if 'recalled' in record:
                    context_records[identity] = {'recalled': record['recalled']}
    # The Pixiv ciphertext and its key form ONE configuration value. A backup
    # may retain an unused old key after credentials were cleared; combining
    # it with a current-only account would make that account undecryptable.
    account = Path('pixiv/config.account')
    key = Path('pixiv/config.key')
    paired_credentials = (incoming / account).exists() or (current / account).exists()
    if (incoming / account).exists() and not (incoming / key).is_file():
        raise BackupError('备份 Pixiv 账号缺少配套的加密密钥')
    if not (incoming / account).exists() and (current / account).exists():
        if not (current / key).is_file():
            raise BackupError('当前 Pixiv 账号缺少配套的加密密钥')
        for relative in (account, key):
            (incoming / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current / relative, incoming / relative)
    # Preserve backup-side cursors. Current-side integer cursors cannot be
    # carried into a rekeyed journal if that cursor was absent in the backup.
    cursors = {name: read_json(incoming / ('ai/.state/' + name + '.json'), {field: 0})
               for name, field in [('chat-cursor', 'seq'), ('chat-media-cursor', 'id')]}
    if (current / journal).exists() and (incoming / journal).exists():
        mapping = merge_journal(current / journal, incoming / journal)
    # The cache is derived from all memory files, and cannot be row-merged with
    # its binary HNSW segments. Rebuild it from the merged source files instead.
    chroma = incoming / 'ai/.vector_cache/chroma'
    if chroma.exists():
        remove(chroma, incoming)
    for top in sorted(current.iterdir()) if current.exists() else []:
        if top.name == 'backups':
            continue
        items = [(top, top.is_dir())]
        if top.is_dir():
            items.extend(regular_tree(top, file_links_within=current))
        for source, directory in items:
            rel = source.relative_to(current)
            if paired_credentials and rel in {account, key}:
                continue
            if rel == Path('ai/.vector_cache/chroma') or Path('ai/.vector_cache/chroma') in rel.parents:
                continue
            dest = incoming / rel
            if directory:
                if dest.exists() and not dest.is_dir():
                    # A backup file wins over the entire old directory.
                    continue
                if any(p.is_file() for p in dest.parents if p.is_relative_to(incoming)):
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if any(p.is_file() for p in dest.parents if p.is_relative_to(incoming)):
                continue
            if dest.is_dir():
                continue
            if source.name.endswith(('-wal', '-shm', '-journal')):
                base = source.with_name(source.name.rsplit('-', 1)[0])
                if base.is_file() and sqlite_file(base):
                    continue
            if rel == journal and mapping is not None:
                continue
            if rel == jobs and mapping is not None:
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(dest)) as target:
                        # Retain schema, but IDs must be rekeyed against the journal.
                        db.backup(target)
                        target.execute('DELETE FROM jobs')
                        target.commit()
                merge_jobs(source, dest, mapping)
                continue
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if sqlite_file(source):
                    with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(dest)) as target:
                        db.backup(target)
                else:
                    shutil.copy2(source, dest)
                continue
            if sqlite_file(source) and sqlite_file(dest):
                merge_database(source, dest)
            # Only known host-owned structured configuration/state is merged.
            # User projects and JSONL session logs remain whole-file conflicts.
            elif rel.suffix == '.json' and (len(rel.parts) == 1 or rel.parts[0] == 'admin' or
                  rel.as_posix().startswith(('ai/.state/', 'pixiv/'))):
                # Crypto key+encrypted account are deliberately never field-merged.
                write_json(dest, deep_merge(read_json(source), read_json(dest)))
            elif rel.as_posix() == 'ai/config.yaml':
                import yaml
                value = deep_merge(yaml.safe_load(source.read_text('utf-8')), yaml.safe_load(dest.read_text('utf-8')))
                dest.write_text(yaml.safe_dump(value, allow_unicode=True), 'utf-8')
    if (incoming / 'ai').exists():
        write_json(incoming / 'ai/.state/index.json', {'dirty': True})
        # Batch receipts are keyed by seq ranges. After combining histories the
        # same range can refer to different messages; never use an old receipt
        # to acknowledge a batch which has not actually been integrated.
        (incoming / 'ai/.state/memory-batch-receipt.json').unlink(missing_ok=True)
        if mapping is not None:
            for name, value in cursors.items():
                write_json(incoming / ('ai/.state/' + name + '.json'), value)
    # The short QQ lookup context is a cache of journal records. Reconstruct it
    # so retained current-only messages do not disappear from /short or recall.
    if (incoming / journal).exists():
        context = {}
        recent = {}
        with closing(sqlite3.connect(incoming / journal)) as db:
            for path, sequence in db.execute('SELECT path,seq FROM messages ORDER BY seq'):
                relative = safe_name(path)
                if not path.startswith('raw/') or relative.suffix != '.json':
                    raise BackupError('消息数据库包含无效原文路径')
                file = incoming / 'ai/workspace/memory' / str(relative)
                if not file.exists():
                    continue
                if file.stat().st_size > 64 * 1024**2:
                    raise BackupError('消息原文文件超过 64 MiB，不能安全读取')
                data = read_json(file)
                record = data['record']
                key = record['scene_type'] + ':' + str(record['scene_id'])
                rendered = file.with_suffix('.rendered.txt')
                content = rendered.read_text('utf-8') if rendered.exists() else data.get('rendered_content', record['content'])
                cached = context_records.get((key, str(record['id'])), {})
                record = {**record, 'content': content, **({'recalled': cached['recalled']} if 'recalled' in cached else {})}
                heap = recent.setdefault(key, [])
                heapq.heappush(heap, (datetime.fromisoformat(record['timestamp']).timestamp(), sequence, record))
                if len(heap) > 1000:
                    heapq.heappop(heap)
        for key, heap in recent.items():
            records = [entry[2] for entry in sorted(heap)]
            context[key] = {'recent_messages': records, 'compressed_summary': '', 'last_activity': records[-1]['timestamp']}
        write_json(incoming / 'ai/.state/context.json', context)
