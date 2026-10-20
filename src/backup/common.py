from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath


class BackupError(ValueError):
    pass


def sync_directory(path: Path):
    if os.name != 'nt':
        directory = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def durable_replace(source: Path, destination: Path):
    os.replace(source, destination)
    sync_directory(source.parent)
    if destination.parent != source.parent:
        sync_directory(destination.parent)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.backup-')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            # A transaction journal must survive a power loss after rename.
            sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path, default=None):
    return json.loads(path.read_text('utf-8')) if path.exists() else default


def safe_name(value: str) -> PurePosixPath:
    # A package must also be safe to unpack on Windows, regardless of its origin.
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise BackupError('备份中含有无效路径')
    parts = value.split('/')
    if any(p in {'', '.', '..'} or p.endswith((' ', '.')) or any(c in p for c in ':<>"|?*') or
           re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p, re.I) for p in parts):
        raise BackupError(f'备份中含有不安全路径：{value}')
    return PurePosixPath(value)


def regular_tree(root: Path, *, file_links_within: Path | None = None):
    """Never follow links, including Windows junctions, or silently omit them."""
    if not root.exists():
        return
    if root.is_symlink() or root.resolve() != root.absolute():
        raise BackupError(f'备份目录不能经过符号链接：{root.name}')
    for path in sorted(root.iterdir()):
        info = path.lstat()
        if path.is_symlink() and file_links_within and path.is_file() and path.resolve().is_relative_to(file_links_within.resolve()):
            # HuggingFace caches use internal file symlinks. Store their actual
            # bytes so the package also works on Windows without link privileges.
            yield path, False
            continue
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise BackupError(f'备份发现链接，请先改为实际文件：{path.relative_to(root)}')
        if stat.S_ISDIR(info.st_mode):
            yield path, True
            yield from regular_tree(path, file_links_within=file_links_within)
        elif stat.S_ISREG(info.st_mode):
            yield path, False
        else:
            raise BackupError(f'无法备份特殊文件：{path.name}')


def remove(path: Path, boundary: Path):
    # All recursive deletion is centralized and confined to the checked store.
    absolute = path.absolute()
    base = boundary.resolve()
    if absolute == base or not absolute.is_relative_to(base) or path.parent.resolve() != path.parent.absolute():
        raise BackupError('清理路径越界')
    if path.is_symlink():
        path.unlink()
    elif getattr(path, 'is_junction', lambda: False)():
        # Remove the reparse point itself, never enumerate its target.
        os.rmdir(path)
    elif path.is_dir():
        path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for child in path.iterdir():
            remove(child, boundary)
        path.rmdir()
    else:
        if path.exists() and os.name == 'nt':
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        path.unlink(missing_ok=True)


def data_entries(data: Path):
    for top in sorted(data.iterdir()) if data.exists() else []:
        if top.name == 'backups':
            continue
        if top.is_symlink() or getattr(top, 'is_junction', lambda: False)():
            raise BackupError(f'数据根目录中含有链接：{top.name}')
        yield top, top.is_dir()
        if top.is_dir():
            yield from regular_tree(top, file_links_within=data)
