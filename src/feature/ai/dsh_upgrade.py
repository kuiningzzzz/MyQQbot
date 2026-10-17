"""Versioned, restart-safe protection for native dsh checkpoints.

The rc.2 kernel reads the existing v4 JSONL format directly. Only runtime
checkpoints need a pre-upgrade snapshot; Markdown, queues, indexes and business
databases stay in place. No automatic rollback can overwrite newer user data.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from filelock import FileLock

from .storage import atomic_write

DSH_VERSION = "0.2.1-rc.2"
LEGACY_VERSION = "0.2.0-rc.2"


class RuntimeUpgrade:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "dsh-home"
        self.directory = root / ".state/dsh-upgrades"
        self.marker = self.directory / "runtime.json"

    def prepare(self):
        """Snapshot once, before the new kernel or retention cleanup can write."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.directory / 'upgrade.lock')):
            state = json.loads(self.marker.read_text("utf-8")) if self.marker.exists() else {}
            version = state.get("version", LEGACY_VERSION)
            if version not in {LEGACY_VERSION, DSH_VERSION}:
                raise RuntimeError(f"不支持自动迁移 dsh {version}；原数据未修改")
            if state.get("target", DSH_VERSION) != DSH_VERSION:
                raise RuntimeError("发现其他版本尚未完成的 dsh 迁移；原数据未修改")
            if state.get("status") == "ready" and version == DSH_VERSION:
                return state
            if state.get("status") == "pending":
                # A failed boot reuses its completed snapshot, never snapshots
                # already-upgraded logs over the original rollback point.
                return state
            # A process killed while copying never publishes a partial backup.
            # Discard only our own unfinished copies before retrying.
            for partial in self.directory.glob(f'{LEGACY_VERSION}-to-{DSH_VERSION}-*.partial'):
                if partial.is_dir() and not partial.is_symlink():
                    shutil.rmtree(partial)
            snapshot = self._snapshot() if self.home.exists() else None
            state = {"version": version, "target": DSH_VERSION, "status": "pending",
                     "prepared_at": time.time(), "backup": snapshot}
            atomic_write(self.marker, json.dumps(state, ensure_ascii=False))
            return state

    def complete(self):
        """Only a successful QQBOT_READY handshake commits the new version."""
        with FileLock(str(self.directory / 'upgrade.lock')):
            state = json.loads(self.marker.read_text("utf-8"))
            if state["status"] != "ready":
                state.update(version=DSH_VERSION, status="ready", completed_at=time.time())
                atomic_write(self.marker, json.dumps(state, ensure_ascii=False))

    def _snapshot(self):
        if self.home.is_symlink():
            raise RuntimeError("dsh-home 是符号链接，无法安全备份")
        now = time.time()
        retained_path = self.home / "qqbot-retention.json"
        retained = json.loads(retained_path.read_text("utf-8")) if retained_path.exists() else {}
        manifest = self.home / "qqbot-sessions.json"
        if manifest.exists():
            for item in json.loads(manifest.read_text("utf-8")).values():
                retained.setdefault(item["id"], item["expires"])
        name = f"{LEGACY_VERSION}-to-{DSH_VERSION}-{uuid.uuid4().hex}"
        temporary = self.directory / (name + ".partial")
        destination = self.directory / name
        runtime = temporary / "runtime"
        runtime.mkdir(parents=True)
        hashes, deadlines, skipped = {}, [], []

        def digest(file):
            # Session logs can be large; verification must not load a whole
            # checkpoint (or two copies of it) into the bot process's memory.
            with file.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()

        def copy(source):
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"无法备份非常规 dsh 文件：{source.name}")
            relative = source.relative_to(self.home)
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            original = digest(source)
            if digest(target) != original:
                raise RuntimeError("dsh 备份校验失败，迁移已停止")
            hashes[relative.as_posix()] = original

        try:
            # tmp may contain large downloads and process artifacts; profiles
            # are generated deployment configuration. Neither is checkpoint data.
            for source in self.home.glob("qqbot-*.json"):
                copy(source)
            sessions = self.home / "sessions"
            if sessions.is_symlink():
                raise RuntimeError("dsh sessions 是符号链接，无法安全备份")
            if sessions.exists():
                for project in sessions.iterdir():
                    if project.is_symlink() or not project.is_dir():
                        raise RuntimeError("dsh 会话项目目录无效")
                    for session in project.iterdir():
                        if session.is_symlink() or not session.is_dir():
                            raise RuntimeError("dsh 会话目录无效")
                        files = list(session.rglob("*"))
                        if any(file.is_symlink() for file in files):
                            raise RuntimeError("dsh 会话中存在符号链接")
                        files = [file for file in files if file.is_file()]
                        # Do not resurrect already-expired raw conversation text.
                        fallback = min((file.stat().st_mtime for file in files), default=now) + 30 * 86400
                        deadline = min(retained.get(session.name, fallback * 1000) / 1000, fallback)
                        if deadline <= now:
                            skipped.append(session.name)
                            continue
                        deadlines.append(deadline)
                        for source in files:
                            copy(source)
            metadata = {"from": LEGACY_VERSION, "to": DSH_VERSION, "created_at": now,
                        "expires_at": min(deadlines, default=now + 30 * 86400),
                        "sha256": hashes, "expired_sessions_excluded": skipped}
            atomic_write(temporary / "snapshot.json", json.dumps(metadata, ensure_ascii=False))
            temporary.rename(destination)
            return name
        except BaseException:
            shutil.rmtree(temporary)
            raise

    def prune(self):
        """Snapshots cannot extend the original chat-history retention period."""
        if not self.directory.exists():
            return
        for metadata in self.directory.glob("*/snapshot.json"):
            if metadata.is_symlink() or metadata.parent.is_symlink():
                continue
            state = json.loads(metadata.read_text("utf-8"))
            if state["expires_at"] <= time.time():
                shutil.rmtree(metadata.parent)
