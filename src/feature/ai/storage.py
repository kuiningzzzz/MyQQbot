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
        self.root = root.resolve()
        for directory in ("raw", "user", "common", ".vector_cache", ".state"):
            path = self.root / directory
            if path.resolve() != path.absolute():
                raise ValueError("记忆目录不能是符号链接")
            path.mkdir(parents=True, exist_ok=True)
        if not (self.root / "AGENT.md").exists():
            atomic_write(self.root / "AGENT.md", "你是一位自然、友善的 QQ 聊天成员。回答简洁，依据事实，不编造记忆。\n")
        if not (self.root / "common/GLOSSARY.md").exists():
            atomic_write(self.root / "common/GLOSSARY.md", "# 网络黑话与通用知识\n")

    def memory_path(self, relative: str) -> Path:
        if "\\" in relative or not re.fullmatch(r"(?:user/[0-9]+/(?:PERSON|PREFERENCE|RECENTNESS)\.md|user/[0-9]+/memory/[^/]+\.md|common/GLOSSARY\.md)", relative):
            raise ValueError("只允许访问 user 用户画像和 common/GLOSSARY.md")
        path = self.root / relative
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(self.root) or any(part in {"..", "."} for part in Path(relative).parts) or any(char in relative for char in '<>:"|?*'):
            raise ValueError("路径越界")
        return path

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)).replace("\\", "/") for folder in ("user", "common") for p in (self.root / folder).rglob("*.md") if p.resolve().is_relative_to(self.root))

    def read_memory(self, relative: str) -> str:
        path = self.memory_path(relative)
        with FileLock(str(self.root / ".state/memory.lock")):
            return path.read_text("utf-8") if path.exists() else ""

    def write_memory(self, relative: str, content: str, expected_hash: str | None = None) -> None:
        path = self.memory_path(relative)
        with FileLock(str(self.root / ".state/memory.lock")):
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
        (self.root / f"user/{qq}/memory").mkdir(exist_ok=True)

    def append_raw(self, record: dict, timezone: str) -> dict:
        now = datetime.fromisoformat(record["timestamp"]).astimezone(ZoneInfo(timezone))
        path = self.root / "raw" / (period_date(now) + ".md")
        line = self.format_raw(record, timezone)
        self._append(path, line + "\n")
        return {"success": True, "raw_path": f"raw/{path.name}", "recorded_at": now.strftime("%Y-%m-%d %H:%M:%S"), "raw_line": line}

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

    def append_system(self, content: str, timezone: str) -> None:
        now = datetime.now(ZoneInfo(timezone))
        self._append(self.root / "raw" / (period_date(now) + ".md"), f"[{now:%Y-%m-%d %H:%M:%S}] | [系统] | {single_line(content)}\n")

    def _append(self, path: Path, text: str) -> None:
        for attempt in range(3):
            try:
                with FileLock(str(path) + ".lock"):
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(1)

    def read_raw(self, date: str) -> list[str]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("日期格式应为 YYYY-MM-DD")
        path = self.root / "raw" / (date + ".md")
        with FileLock(str(path) + ".lock"):
            rows = path.read_text("utf-8").splitlines() if path.exists() else []
            records = []
            for row in rows:
                if row.startswith("  ") and records:
                    records[-1] += "\n" + row
                else:
                    records.append(row)
            return records

    def load_state(self, name: str, default):
        path = self.root / ".state" / (name + ".json")
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    def save_state(self, name: str, value) -> None:
        atomic_write(self.root / ".state" / (name + ".json"), json.dumps(value, ensure_ascii=False))
