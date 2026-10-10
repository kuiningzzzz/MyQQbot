"""Idempotent, startup migration. Never overwrite an existing destination."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from filelock import FileLock


def migrate(root: Path) -> dict:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".state"
    state.mkdir(exist_ok=True)
    marker = state / "dsh-migration.json"
    with FileLock(str(state / "migration.lock")):
        if marker.exists():
            return json.loads(marker.read_text("utf-8"))
        work = root / "workspace"
        memory = work / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        moved = []
        for folder, target in (("user", "user"), ("common", "common"), ("raw", "raw/legacy")):
            source = root / folder
            if not source.exists():
                continue
            if source.is_symlink():
                raise ValueError("拒绝迁移符号链接记忆目录")
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError("拒绝迁移符号链接记忆文件")
                if not path.is_file() or path.suffix == ".lock":
                    continue
                dest = memory / target / path.relative_to(source)
                if dest.resolve() != dest.absolute() or not dest.resolve().is_relative_to(root):
                    raise ValueError("迁移目标路径越界")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    if dest.read_bytes() != path.read_bytes():
                        raise ValueError(f"迁移冲突，未覆盖：{dest}")
                    path.unlink()
                else:
                    shutil.move(str(path), str(dest))
                moved.append(str(dest.relative_to(root)))
        if (root / "AGENT.md").exists() and not (work / "AGENT.md").exists():
            shutil.copy2(root / "AGENT.md", work / "AGENT.md")
        config = root / "config.yaml"
        if config.exists():
            import yaml
            data = yaml.safe_load(config.read_text("utf-8")) or {}
            backup = state / "pre-dsh-config.yaml"
            if not backup.exists():
                shutil.copy2(config, backup)
            data["raw_retention_days"] = 30
            from .storage import atomic_write
            atomic_write(config, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        result = {"version": 2, "moved": moved, "note": "旧记忆原样迁移；旧整合游标不复用，旧原文按30天清理。"}
        from .storage import atomic_write
        atomic_write(marker, json.dumps(result, ensure_ascii=False))
        return result
