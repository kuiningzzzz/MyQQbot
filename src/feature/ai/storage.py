from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

logger = logging.getLogger(__name__)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        temporary = None
        try:
            with FileLock(str(path) + ".lock"):
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                    temporary = stream.name
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def period_date(now: datetime) -> str:
    """File label is the START date of a 03:00–03:00 memory day."""
    return (now - timedelta(hours=3)).date().isoformat()


def single_line(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("|", "\\|")


class Workspace:
    def __init__(self, root: Path):
        self.data_root = root.resolve()
        self.root = self.data_root / "workspace"
        self.memory_root = self.root / "memory"
        (self.data_root / ".state").mkdir(parents=True, exist_ok=True)
        for directory in ("memory/raw", "memory/user", "memory/group", "memory/common", "files", "tmp"):
            path = self.root / directory
            if path.resolve() != path.absolute():
                raise ValueError("记忆目录不能是符号链接")
            path.mkdir(parents=True, exist_ok=True)
        if not (self.root / "AGENT.md").exists():
            atomic_write(self.root / "AGENT.md", "你是一位自然、友善的 QQ 聊天成员。回答简洁，依据事实，不编造记忆。\n")
        if not (self.memory_root / "common/GLOSSARY.md").exists():
            atomic_write(self.memory_root / "common/GLOSSARY.md", "# 网络黑话与通用知识\n")

    def memory_path(self, relative: str) -> Path:
        if "\\" in relative or not re.fullmatch(r"(?:user/[0-9]+/(?:PERSON|PREFERENCE|RECENTNESS)\.md|user/[0-9]+/memory/[^/]+\.md|group/[0-9]+/[^/\r\n]+\.md|common/GLOSSARY\.md)", relative):
            raise ValueError("只允许访问合法用户记忆、group/<群号>/<主题>.md 和 common/GLOSSARY.md")
        path = self.memory_root / relative
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(self.root) or any(part in {"..", "."} for part in Path(relative).parts) or any(char in relative for char in '<>:"|?*'):
            raise ValueError("路径越界")
        return path

    def files(self) -> list[str]:
        result = []
        for folder in ('user', 'group', 'common'):
            for file in (self.memory_root / folder).rglob('*.md'):
                relative = file.relative_to(self.memory_root).as_posix()
                try:
                    self.memory_path(relative)
                except ValueError:
                    continue
                result.append(relative)
        return sorted(result)

    def read_memory(self, relative: str) -> str:
        with FileLock(str(self.data_root / ".state/memory.lock")):
            path = self.memory_path(relative)
            return path.read_text("utf-8") if path.exists() else ""

    def write_memory(self, relative: str, content: str, expected_hash: str | None = None) -> None:
        with FileLock(str(self.data_root / ".state/memory.lock")):
            path = self.memory_path(relative)
            previous = path.read_text("utf-8") if path.exists() else ""
            if expected_hash is not None and digest(previous) != expected_hash:
                raise ValueError("文件已被更新，请重新读取后保存")
            atomic_write(path, content)

    def init_user(self, qq: str, self_ids: set[str]) -> None:
        if not qq.isascii() or not qq.isdigit() or qq in self_ids:
            raise ValueError("不能为机器人自身或无效 QQ 建立用户画像")
        for name in ("PERSON", "PREFERENCE", "RECENTNESS"):
            relative = f"user/{qq}/{name}.md"
            if not self.memory_path(relative).exists():
                self.write_memory(relative, f"# {name}\n", digest(""))
        (self.memory_root / f"user/{qq}/memory").mkdir(exist_ok=True)

    def delete_memory(self, relative: str, expected_hash: str) -> None:
        with FileLock(str(self.data_root / '.state/memory.lock')):
            path = self.memory_path(relative)
            previous = path.read_text('utf-8') if path.exists() else ''
            if digest(previous) != expected_hash:
                raise ValueError('文件已被更新，请重新读取后删除')
            path.unlink(missing_ok=True)

    @staticmethod
    def format_raw(record: dict, timezone: str) -> str:
        now = datetime.fromisoformat(record["timestamp"]).astimezone(ZoneInfo(timezone))
        scene = "群聊" if record["scene_type"] == "group" else "私聊"
        location = f"[群号:{record['scene_id']}]" if record["scene_type"] == "group" else f"[私聊QQ:{record['scene_id']}]"
        content = single_line(record['content'])
        if "[转发聊天记录]" in record['content']:
            content = "\n  ".join(single_line(part) for part in record['content'].splitlines())
        fields = [f"[{now:%Y-%m-%d %H:%M:%S}]", f"[{scene}]", location, f"[QQ:{record['qq']}]", f"[昵称:{single_line(record['nickname'])}]", "[机器人自己]" if record.get("is_self") else "[人类用户]", f"说：{content}"]
        return " | ".join(fields)

    def load_state(self, name: str, default):
        path = self.data_root / '.state' / (name + '.json')
        return json.loads(path.read_text('utf-8')) if path.exists() else default

    def save_state(self, name: str, value) -> None:
        atomic_write(self.data_root / ".state" / (name + ".json"), json.dumps(value, ensure_ascii=False))
