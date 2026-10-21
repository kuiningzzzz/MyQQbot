"""Versioned ZIP format. This module never imports or starts bot features."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from datetime import datetime, timezone, timedelta
from contextlib import closing
from pathlib import Path

from .common import BackupError, regular_tree, safe_name

FORMAT = 'myqqbot-backup'
VERSION = 1
MAX_MEMBERS = 500_000
MAX_BYTES = 100 * 1024**3
ENV_NAMES = {
    'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_SEARCH_BASE_URL',
    'VECTOR_INDEX_ENABLED', 'TIMEZONE', 'TZ', 'HF_ENDPOINT',
    'JM_CLEANUP_DELAY_SECONDS', 'PIXIV_CLEANUP_DELAY_SECONDS', 'PIXIV_PROXY',
    'MYQQBOT_WS_HOST', 'MYQQBOT_WS_PORT', 'MYQQBOT_ADMIN_HOST', 'MYQQBOT_ADMIN_PORT',
    'MYQQBOT_PUBLIC_BASE_URL',
    'DASHSCOPE_API_KEY', 'DASHSCOPE_BASE_URL', 'DASHSCOPE_HTTP_BASE_URL',
    'AI_AUDIO_MODEL', 'AI_TTS_MODEL', 'AI_TTS_VOICE', 'AI_TTS_INSTRUCTION', 'AI_AUDIO_ENABLED', 'AI_VOICE_ENABLED',
}
# Match Settings without importing it (that imports dotenv and changes os.environ).
ENV_NAMES |= {'AI_' + key.upper() for key in (
    'api_key', 'api_url', 'dsh_api_url', 'bot_qq', 'nickname', 'account_description',
    'chat_enabled', 'summary_enabled', 'memory_enabled', 'memory_auto_learn',
    'dsh_timeout_seconds', 'vector_index_enabled', 'embedding_model', 'timezone',
    'workspace_enabled', 'ocr_enabled', 'stickers_enabled')}
SOURCE_DIRS = {'src', 'dsh', 'deploy', 'docs', 'licenses', 'sandbox', 'scripts', 'tests', '.github'}
SOURCE_FILES = {'Dockerfile', 'docker-compose.yml', 'requirements.txt', '.dockerignore',
                '.env.example', '.gitignore', '.gitattributes', 'LICENSE', 'README.md',
                'THIRD_PARTY_NOTICES.md', 'memory_structure.md', 'tutorial.md'}
SKIP_DIRS = {'node_modules', '__pycache__', '.git', '.venv', '.venv-ai', '.pytest_cache'}


def environment():
    return {key: os.environ[key] for key in sorted(ENV_NAMES) if key in os.environ}


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sqlite_file(path):
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\0'


def _source_files(root):
    def walk(path):
        for child in sorted(path.iterdir()):
            if child.name in SKIP_DIRS:
                continue
            if child.is_symlink() or getattr(child, 'is_junction', lambda: False)():
                raise BackupError(f'项目源码中含有链接：{child.relative_to(root)}')
            if child.is_dir():
                yield child, True
                yield from walk(child)
            elif child.is_file():
                yield child, False
    for name in sorted(SOURCE_FILES | SOURCE_DIRS):
        path = root / name
        if path.is_symlink():
            raise BackupError('项目源码不能是链接')
        if path.is_dir():
            yield path, True
            yield from walk(path)
        elif path.is_file():
            yield path, False


def create(root: Path, destination: Path, *, env=None):
    """Caller guarantees the runtime process, its workers and DB handles exited."""
    root = root.resolve()
    stamp = datetime.now(timezone(timedelta(hours=8)))
    manifest = {'format': FORMAT, 'version': VERSION, 'created_at': stamp.isoformat(),
                'project_root': str(root), 'files': {}, 'directories': [], 'directory_metadata': {},
                'scope': 'project-data-workspace-source-environment',
                'excludes': ['data/backups', 'system-packages', 'NapCat', 'external-host-files']}
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix('.partial')
    try:
        # Restrict packages containing credentials on POSIX; use the host ACL on Windows.
        with part.open('xb') as handle:
            os.chmod(part, 0o600)
            with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=4, allowZip64=True) as archive:
                total_bytes = 0
                def add(name, *, path=None, content=None, directory=False):
                    nonlocal total_bytes
                    safe_name(name)
                    if directory:
                        manifest['directories'].append(name)
                        manifest['directory_metadata'][name] = {'mode': stat.S_IMODE(path.stat().st_mode) & 0o777,
                                                                 'mtime_ns': path.stat().st_mtime_ns}
                        return
                    mode = stat.S_IMODE(path.stat().st_mode) & 0o777 if path else 0o600
                    info = zipfile.ZipInfo(name)
                    info.date_time = stamp.timetuple()[:6]
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info, 'w', force_zip64=True) as output:
                        if path:
                            with path.open('rb') as stream:
                                while chunk := stream.read(1024 * 1024):
                                    output.write(chunk)
                                    digest.update(chunk)
                                    size += len(chunk)
                        else:
                            output.write(content)
                            digest.update(content)
                            size = len(content)
                    manifest['files'][name] = {'sha256': digest.hexdigest(), 'size': size, 'mode': mode,
                                              'mtime_ns': path.stat().st_mtime_ns if path else int(stamp.timestamp() * 1_000_000_000)}
                    total_bytes += size
                    if total_bytes > MAX_BYTES or len(manifest['files']) + len(manifest['directories']) >= MAX_MEMBERS:
                        raise BackupError('项目数据超过备份格式限制：50 万项、100 GiB')

                # Resolve the configured data root once (native deployments may
                # point it at another disk); never follow links *inside* it.
                data = (root / 'data').resolve()
                # Excluding the store here also avoids ever traversing imported archives.
                for top in sorted(data.iterdir()) if data.exists() else []:
                    if top.name == 'backups':
                        continue
                    if top.is_symlink() or getattr(top, 'is_junction', lambda: False)():
                        raise BackupError(f'数据目录中含有链接：{top.name}')
                    items = [(top, top.is_dir())]
                    if top.is_dir():
                        items.extend(regular_tree(top, file_links_within=data))
                    for path, directory in items:
                        # Checkpoint WAL before packaging, and validate every database.
                        # A genuine user file named *-wal is not omitted on its own.
                        if not directory and path.name.endswith(('-wal', '-shm', '-journal')):
                            base = path.with_name(path.name.rsplit('-', 1)[0])
                            if base.is_file() and sqlite_file(base):
                                continue
                        if not directory and sqlite_file(path):
                            with closing(sqlite3.connect(path)) as db:
                                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                                    raise BackupError(f'SQLite 完整性检查失败：{path.relative_to(data)}')
                        add('data/' + path.relative_to(data).as_posix(), path=path, directory=directory)
                for path, directory in _source_files(root):
                    add('project/' + path.relative_to(root).as_posix(), path=path, directory=directory)
                add('configuration/environment.json', content=json.dumps(env if env is not None else environment(), ensure_ascii=False).encode())
                for name in ('.env', '.env.host'):
                    path = root / name
                    if path.exists():
                        if path.is_symlink():
                            raise BackupError('环境配置文件不能是链接')
                        add('configuration/' + name, path=path)
                add('RESTORE.txt', content=(
                    'MyQQbot 项目备份（含密钥与聊天记录，请勿公开）\n'
                    '干净环境：先校验备份，再解压 project/ 作为项目目录；不要手工拼接或复制 data/。\n'
                    '运行 python -m src.backup.cli restore 备份.zip --mode overwrite 完成校验和配置恢复，随后按 README 部署。\n'
                    '联网安装依赖、NapCat 登录、系统软件、工作区以外的服务器修改需要另行准备。\n'
                    '也可在已部署的同版本项目 admin「备份管理」中导入还原。\n'
                    '备份不包含其他备份包。原始聊天记录仍按 30 天策略清理。\n').encode())
                encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                if len(encoded) > 64 * 1024**2:
                    raise BackupError('备份清单超过 64 MiB')
                archive.writestr('manifest.json', encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, destination)
        return manifest
    finally:
        part.unlink(missing_ok=True)


def inspect(path: Path):
    """Validate the entire archive, not just the JSON manifest or its CRC."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_BYTES:
                raise BackupError('备份超过限制：最多 50 万个文件、展开后 100 GiB')
            names = set()
            for info in infos:
                safe_name(info.filename)
                key = info.filename.casefold()
                if key in names or info.is_dir() or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG} or info.flag_bits & 1:
                    raise BackupError('备份含有重复路径、链接、特殊文件或加密成员')
                names.add(key)
            info = archive.getinfo('manifest.json')
            if info.file_size > 64 * 1024**2:
                raise BackupError('备份清单过大')
            manifest = json.loads(archive.read(info))
            if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('version') != VERSION:
                raise BackupError('不支持的备份格式或版本')
            if not isinstance(manifest.get('created_at'), str) or not isinstance(manifest.get('project_root'), str) or manifest.get('scope') != 'project-data-workspace-source-environment':
                raise BackupError('备份清单缺少时间或项目范围')
            try:
                if datetime.fromisoformat(manifest['created_at']).tzinfo is None:
                    raise ValueError('timezone required')
            except ValueError as error:
                raise BackupError('备份时间戳无效') from error
            files, directories = manifest['files'], manifest['directories']
            if not isinstance(files, dict) or not isinstance(directories, list):
                raise BackupError('备份清单结构无效')
            if len(files) + len(directories) >= MAX_MEMBERS:
                raise BackupError('备份目录清单超过限制')
            if archive.getinfo('configuration/environment.json').file_size > 1024 * 1024:
                raise BackupError('备份环境配置超过 1 MiB')
            if set(files) | {'manifest.json'} != {i.filename for i in infos}:
                raise BackupError('备份清单与文件不一致')
            declared = set(names)
            for directory in directories:
                safe_name(directory)
                if directory.casefold() in declared:
                    raise BackupError('备份目录与文件路径冲突')
                declared.add(directory.casefold())
            metadata = manifest.get('directory_metadata', {})
            if not isinstance(metadata, dict) or set(metadata) - set(directories):
                raise BackupError('目录元数据无效')
            if any(not isinstance(item, dict) or 'mtime_ns' not in item for item in metadata.values()):
                raise BackupError('目录时间戳缺失')
            for item in [*files.values(), *metadata.values()]:
                if (not isinstance(item, dict) or not isinstance(item.get('mode'), int) or not 0 <= item['mode'] <= 0o777 or
                        'mtime_ns' in item and (not isinstance(item['mtime_ns'], int) or not 0 <= item['mtime_ns'] < 2**63)):
                    raise BackupError('备份文件权限或时间戳无效')
            for name in list(files) + directories:
                if not name.startswith(('data/', 'project/', 'configuration/')) and name != 'RESTORE.txt':
                    raise BackupError('备份包含不支持的内容')
                if name.casefold().startswith('data/backups/') or name.casefold() == 'data/backups':
                    raise BackupError('备份不能覆盖备份管理目录')
                for parent in safe_name(name).parents:
                    if str(parent).casefold() in names:
                        raise BackupError('备份路径包含文件/目录冲突')
            for name, meta in files.items():
                info = archive.getinfo(name)
                if not isinstance(meta, dict) or info.file_size != meta.get('size'):
                    raise BackupError('备份文件大小不符')
                if not isinstance(meta.get('mode'), int) or not 0 <= meta['mode'] <= 0o777:
                    raise BackupError('备份文件权限无效')
                digest = hashlib.sha256()
                with archive.open(info) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != meta.get('sha256'):
                    raise BackupError(f'备份校验失败：{name}')
            env = json.loads(archive.read('configuration/environment.json'))
            if not isinstance(env, dict) or set(env) - ENV_NAMES or any(not isinstance(v, str) or '\0' in v for v in env.values()):
                raise BackupError('环境配置包含不支持的字段')
            return manifest
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, ValueError, RuntimeError, NotImplementedError) as exc:
        raise BackupError('备份无法读取或格式已损坏') from exc


def extract(path: Path, target: Path, manifest=None, *, writable=False):
    manifest = inspect(path) if manifest is None else manifest
    target.mkdir(parents=True, exist_ok=False)
    # target is a new private staging directory, never a live data directory.
    with zipfile.ZipFile(path) as archive:
        for name in manifest['directories']:
            (target / str(safe_name(name))).mkdir(parents=True, exist_ok=True)
        for name, meta in manifest['files'].items():
            dest = target / str(safe_name(name))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, dest.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            # Keep scripts executable; do not restore setuid/sticky bits.
            os.chmod(dest, (int(meta.get('mode', 0o600)) & 0o777) | (0o600 if writable else 0))
            if 'mtime_ns' in meta:
                os.utime(dest, ns=(meta['mtime_ns'], meta['mtime_ns']))
        # Apply directory permissions last, so read-only archived folders cannot
        # prevent extraction of their children. Preserve retention fallback times.
        for name, meta in sorted(manifest.get('directory_metadata', {}).items(), key=lambda item: len(safe_name(item[0]).parts), reverse=True):
            dest = target / str(safe_name(name))
            os.chmod(dest, meta['mode'] | (0o700 if writable else 0))
            os.utime(dest, ns=(meta['mtime_ns'], meta['mtime_ns']))
    return manifest


def restore_data_permissions(data: Path, manifest):
    for name, meta in manifest['files'].items():
        if name.startswith('data/'):
            dest = data / str(safe_name(name)).removeprefix('data/')
            if dest.is_file():
                os.chmod(dest, meta['mode'])
    for name, meta in sorted(manifest.get('directory_metadata', {}).items(), key=lambda item: len(safe_name(item[0]).parts), reverse=True):
        if name.startswith('data/'):
            dest = data / str(safe_name(name)).removeprefix('data/')
            if dest.is_dir():
                os.chmod(dest, meta['mode'])
