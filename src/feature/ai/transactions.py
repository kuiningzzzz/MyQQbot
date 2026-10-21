"""Stage automatic memory edits; publish only a completed, conflict-free batch."""
import json
import shutil

from filelock import FileLock

from .storage import atomic_write, digest


class MemoryTransaction:
    def __init__(self, workspace, batch, self_ids=(), *, receipt='memory-batch-receipt'):
        if receipt not in {'memory-batch-receipt', 'memory-compression-receipt'}:
            raise ValueError('无效维护回执')
        self.workspace = workspace
        self.batch = batch
        self.receipt = receipt
        self.root = workspace.root / "tmp" / "memory-stage"
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(workspace.root.resolve()):
            raise ValueError("暂存目录越界")
        if self.root.exists():
            shutil.rmtree(self.root)
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(workspace.root.resolve()):
            raise ValueError("暂存目录越界")
        self.root.mkdir(parents=True)
        self.hashes = {}
        for path in workspace.files(include_soul=True):
            if any(path.startswith('user/' + qq + '/') for qq in self_ids):
                continue
            text = workspace.read_memory(path)
            self.hashes[path] = digest(text)
            atomic_write(self.root / "memory" / path, text)

    def commit(self):
        changes = []
        present = set()
        for file in (self.root / "memory").rglob("*.md"):
            path = file.relative_to(self.root / "memory").as_posix()
            self.workspace.memory_path(path)
            if file.is_symlink() or file.resolve() != file.absolute() or file.stat().st_nlink > 1:
                raise ValueError("拒绝提交符号链接")
            present.add(path)
            text = file.read_text("utf-8")
            if len(text) > 500000:
                raise ValueError("记忆文件过大")
            if digest(text) != self.hashes.get(path, digest("")):
                changes.append({"path": path, "content": text})
        changes.extend({'path': path, 'delete': True} for path in self.hashes if path not in present)
        manifest = self.workspace.data_root / ".state/memory-commit.json"
        with FileLock(str(self.workspace.data_root / ".state/memory.lock")):
            for change in changes:
                path = self.workspace.memory_path(change["path"])
                current = path.read_text("utf-8") if path.exists() else ""
                if digest(current) != self.hashes.get(change["path"], digest("")):
                    raise ValueError("记忆已被其他写入修改，整批重新读取后再整理")
            atomic_write(manifest, json.dumps({"batch": self.batch, "changes": changes, 'receipt': self.receipt}, ensure_ascii=False))
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
    receipt = data.get('receipt', 'memory-batch-receipt')
    if receipt not in {'memory-batch-receipt', 'memory-compression-receipt'}:
        raise ValueError('无效维护回执')
    for change in data["changes"]:
        target = workspace.memory_path(change['path'])
        if change.get('delete'):
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, change['content'])
    workspace.save_state(receipt, {"batch": data["batch"], "updated_files": [c["path"] for c in data["changes"]]})
    manifest.unlink()
