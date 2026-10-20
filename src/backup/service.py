from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archive
from .common import BackupError, data_entries, durable_replace, read_json, regular_tree, remove, safe_name, write_json
from .merge import deep_merge, merge_tree

PACKAGE_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}\.zip$')


class BackupStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data = (self.root / 'data').resolve()
        self.store = self.data / 'backups'
        if self.store.resolve() != self.store.absolute():
            raise BackupError('备份目录不能经过符号链接')
        self.packages = self.store / 'packages'
        if self.packages.resolve() != self.packages.absolute():
            raise BackupError('备份包目录不能经过符号链接')
        self.packages.mkdir(parents=True, exist_ok=True)
        self.pending = self.store / 'request.json'
        self.transaction = self.store / 'transaction.json'
        self.status_file = self.store / 'status.json'

    def package(self, name):
        if not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name):
            raise BackupError('备份包名称无效')
        path = self.packages / name
        if path.resolve() != path.absolute() or not path.is_file():
            raise BackupError('备份包不存在或路径无效')
        return path

    def status(self):
        return read_json(self.status_file, {'state': 'idle', 'message': '尚未执行备份或还原'})

    def set_status(self, state, message, **values):
        value = {'state': state, 'message': message, 'updated_at': datetime.now(timezone.utc).isoformat(), **values}
        write_json(self.status_file, value)
        return value

    def list(self):
        items = []
        for path in sorted(self.packages.glob('*.zip'), reverse=True):
            if path.is_symlink() or not PACKAGE_NAME.fullmatch(path.name):
                continue
            stat = path.stat()
            metadata = read_json(path.with_suffix('.metadata.json'), {})
            items.append({'name': path.name, 'size': stat.st_size,
                          'created_at': metadata.get('created_at'), 'imported': metadata.get('imported', False),
                          'files': metadata.get('files'), 'modified_at': stat.st_mtime})
        return items

    def metadata(self, path, manifest, *, imported=False):
        write_json(path.with_suffix('.metadata.json'), {
            'created_at': manifest['created_at'], 'files': len(manifest['files']),
            'imported': imported, 'sha256': archive.sha256(path)})

    def make_backup(self, prefix='backup', *, env=None):
        if env is None:
            from .supervisor import child_environment
            effective = child_environment(self)
            env = {key: effective[key] for key in archive.ENV_NAMES if key in effective}
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        name = f'{prefix}-{stamp}-{uuid.uuid4().hex[:8]}.zip'
        path = self.packages / name
        manifest = archive.create(self.root, path, env=env)
        self.metadata(path, manifest)
        return name

    def cleanup_incomplete(self):
        """Only while the service lock is held and no runtime is alive."""
        if self.transaction.exists():
            raise BackupError('存在未恢复的事务，不能清理暂存内容')
        for path in self.packages.iterdir():
            if re.fullmatch(r'(?:(?:backup|before-restore)-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.partial|import-[0-9a-f]{32}\.upload|\.backup-[a-z0-9_]{8})', path.name):
                remove(path, self.store)
        for path in self.store.iterdir():
            if re.fullmatch(r'(?:restore-[0-9a-f]{32}|\.backup-[a-z0-9_]{8})', path.name):
                remove(path, self.store)

    def preview(self, name):
        manifest = archive.inspect(self.package(name))
        return {'name': name, 'created_at': manifest['created_at'], 'files': len(manifest['files']),
                'bytes': sum(item['size'] for item in manifest['files'].values()),
                'has_environment': True, 'scope': manifest['scope'],
                'notice': '还原项目数据与环境配置；当前运行的项目代码不替换。旧备份的聊天原文仍受 30 天保留期限制。'}

    def request(self, action, *, name=None, mode=None, env=None):
        if self.pending.exists():
            raise BackupError('已有备份或还原任务正在执行')
        if action not in {'create', 'restore'}:
            raise BackupError('操作无效')
        if action == 'restore':
            self.package(name)
            if mode not in {'overwrite', 'incremental'}:
                raise BackupError('还原模式无效')
        identifier = uuid.uuid4().hex
        write_json(self.pending, {'id': identifier, 'action': action, 'name': name, 'mode': mode,
                                 'environment': env if env is not None else archive.environment()})
        return self.set_status('queued', '正在停止服务，完成后自动重新启动', id=identifier, action=action)

    def recover(self):
        """Run BEFORE any bot module opens data. Recovery is itself idempotent."""
        transaction = read_json(self.transaction)
        if not transaction:
            return False
        if (not isinstance(transaction, dict) or not re.fullmatch('[0-9a-f]{32}', str(transaction.get('id', ''))) or
                transaction.get('phase') not in {'installing', 'committed'} or
                not isinstance(transaction.get('originals'), list) or any(
                    not isinstance(name, str) or name in {'', '.', '..', 'backups'} or '/' in name or '\\' in name or ':' in name
                    for name in transaction['originals'])):
            raise BackupError('还原事务记录损坏，已停止启动以保护原数据，请检查 data/backups/transaction.json')
        workspace = self.store / ('restore-' + transaction['id'])
        old = workspace / 'old'
        if transaction['phase'] != 'committed':
            originals = transaction['originals']
            for child in list(self.data.iterdir()):
                if child.name != 'backups' and child.name not in originals:
                    remove(child, self.data)
            for name in originals:
                previous = old / name
                if previous.exists():
                    live = self.data / name
                    if live.exists():
                        remove(live, self.data)
                    durable_replace(previous, live)
            self.set_status('failed', '上一次还原未完成，已自动回滚原数据')
        self.transaction.unlink()
        if workspace.exists():
            remove(workspace, self.store)
        return transaction['phase'] != 'committed'

    def restore(self, name, mode):
        if mode not in {'overwrite', 'incremental'}:
            raise BackupError('还原模式无效')
        package = self.package(name)
        manifest = archive.inspect(package)
        needed = sum(m['size'] for m in manifest['files'].values())
        current_bytes = sum(p.stat().st_size for p, directory in data_entries(self.data) if not directory)
        # Staging and a safety ZIP coexist with the live data until commit.
        if shutil.disk_usage(self.store).free < needed + current_bytes * 2 + 64 * 1024**2:
            raise BackupError('磁盘空间不足，无法安全暂存与回滚')
        identifier = uuid.uuid4().hex
        workspace = self.store / ('restore-' + identifier)
        workspace.mkdir()
        staged = workspace / 'incoming'
        try:
            archive.extract(package, staged, manifest, writable=True)
            restored = staged / 'data'
            restored.mkdir(exist_ok=True)
            env = read_json(staged / 'configuration/environment.json')
            if mode == 'incremental':
                merge_tree(self.data, restored)
                from .supervisor import child_environment
                active = read_json(self.pending, {}).get('environment')
                if active is None:
                    effective = child_environment(self)
                    active = {key: effective[key] for key in archive.ENV_NAMES if key in effective}
                env = deep_merge(active, env)
            write_json(restored / '.deployment-environment.json', env)
            # Validate the complete staging tree and fail before removing any data.
            for path, directory in regular_tree(restored):
                if not directory and archive.sqlite_file(path):
                    with closing(sqlite3.connect(path)) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                            raise BackupError(f'备份数据库校验失败：{path.relative_to(restored)}')
                        if path.relative_to(restored).as_posix() == 'ai/.state/messages.sqlite3':
                            for (raw,) in db.execute('SELECT path FROM messages'):
                                relative = safe_name(raw)
                                if not raw.startswith('raw/') or relative.suffix != '.json':
                                    raise BackupError('消息数据库包含无效原文路径')
            safety = self.make_backup('before-restore', env=read_json(self.pending, {}).get('environment'))
            archive.restore_data_permissions(restored, manifest)
            originals = [p.name for p in self.data.iterdir() if p.name != 'backups']
            old = workspace / 'old'
            old.mkdir()
            journal = {'id': identifier, 'phase': 'installing', 'originals': originals}
            write_json(self.transaction, journal)
            for name in originals:
                durable_replace(self.data / name, old / name)
            for child in restored.iterdir():
                durable_replace(child, self.data / child.name)
            journal['phase'] = 'committed'
            write_json(self.transaction, journal)
            self.recover()
            return safety
        except BaseException:
            if self.transaction.exists():
                self.recover()
            raise
        finally:
            if workspace.exists() and not self.transaction.exists():
                remove(workspace, self.store)

    def execute_pending(self):
        request = read_json(self.pending)
        if not request:
            return
        action = request['action']
        self.set_status('running', '正在生成备份' if action == 'create' else '正在校验并还原数据', id=request['id'], action=action)
        try:
            if action == 'create':
                name = self.make_backup(env=request['environment'])
                self.set_status('succeeded', '备份完成', id=request['id'], package=name)
            else:
                safety = self.restore(request['name'], request['mode'])
                self.set_status('succeeded', '还原完成；管理密码恢复为备份时的密码',
                                id=request['id'], safety_backup=safety)
        except Exception as error:
            # No model inputs, passwords or environment values in logs/status.
            message = str(error) if isinstance(error, BackupError) else f'操作失败（{type(error).__name__}），原数据已保留或回滚，请检查磁盘和文件权限'
            self.set_status('failed', message, id=request['id'])
        finally:
            self.pending.unlink(missing_ok=True)
