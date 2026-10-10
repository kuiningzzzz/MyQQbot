"""Stage automatic memory edits; publish only a completed, conflict-free batch."""
import json
import shutil
import uuid
from pathlib import Path

from filelock import FileLock

from .storage import atomic_write, digest


class MemoryTransaction:
    def __init__(self, workspace, batch, self_ids=()):
        self.workspace = workspace
        self.batch = batch
        self.root = workspace.root / "tmp" / "memory-stage"
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(workspace.root.resolve()):
            raise ValueError("暂存目录越界")
        if self.root.exists():
            shutil.rmtree(self.root)
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(workspace.root.resolve()):
            raise ValueError("暂存目录越界")
        self.root.mkdir(parents=True)
        self.hashes = {}
        for path in workspace.files():
            if any(path.startswith('user/' + qq + '/') for qq in self_ids):
                continue
            text = workspace.read_memory(path)
            self.hashes[path] = digest(text)
            atomic_write(self.root / "memory" / path, text)

    def commit(self):
        changes = []
        for file in (self.root / "memory").rglob("*.md"):
            path = file.relative_to(self.root / "memory").as_posix()
            self.workspace.memory_path(path)
            if file.is_symlink():
                raise ValueError("拒绝提交符号链接")
            text = file.read_text("utf-8")
            if len(text) > 500000:
                raise ValueError("记忆文件过大")
            if digest(text) != self.hashes.get(path, digest("")):
                changes.append({"path": path, "content": text})
        manifest = self.workspace.data_root / ".state/memory-commit.json"
        with FileLock(str(self.workspace.data_root / ".state/memory.lock")):
            for change in changes:
                path = self.workspace.memory_path(change["path"])
                current = path.read_text("utf-8") if path.exists() else ""
                if digest(current) != self.hashes.get(change["path"], digest("")):
                    raise ValueError("记忆已被其他写入修改，整批重新读取后再整理")
            atomic_write(manifest, json.dumps({"batch": self.batch, "changes": changes}, ensure_ascii=False))
            recover(self.workspace)
        return [change["path"] for change in changes]

    def close(self):
        if self.root.exists():
            shutil.rmtree(self.root)


def recover(workspace):
    manifest = workspace.data_root / ".state/memory-commit.json"
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text("utf-8"))
    for change in data["changes"]:
        atomic_write(workspace.memory_path(change["path"]), change["content"])
    workspace.save_state("memory-batch-receipt", {"batch": data["batch"], "updated_files": [c["path"] for c in data["changes"]]})
    manifest.unlink()
