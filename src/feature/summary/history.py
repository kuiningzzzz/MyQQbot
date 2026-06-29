import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.feature.atom import MessageContext, read_json, write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupMessage:
    timestamp: int
    sender: str
    user_id: int | None
    content: str

    def as_prompt_line(self) -> str:
        time_text = datetime.fromtimestamp(self.timestamp).strftime("%m-%d %H:%M")
        identity = f"{self.sender}({self.user_id})" if self.user_id else self.sender
        return f"[{time_text}] {identity}: {self.content}"


class GroupHistoryStore:
    def __init__(self, directory: Path, limit: int = 200):
        self.directory = directory
        self.limit = limit

    def append(self, group_id: int, message: GroupMessage) -> None:
        messages = self.load(group_id, self.limit)
        messages.append(message)
        write_json(
            self._path(group_id),
            {"group_id": group_id, "messages": [asdict(item) for item in messages[-self.limit :]]},
        )

    def load(self, group_id: int, limit: int) -> list[GroupMessage]:
        path = self._path(group_id)
        if not path.exists():
            return []
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取群 %s 的聊天历史失败", group_id)
            return []
        raw_messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(raw_messages, list):
            return []
        messages: list[GroupMessage] = []
        for raw in raw_messages[-limit:]:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            try:
                timestamp = int(raw.get("timestamp") or 0)
            except (TypeError, ValueError):
                timestamp = 0
            try:
                user_id = int(raw["user_id"]) if raw.get("user_id") is not None else None
            except (TypeError, ValueError):
                user_id = None
            messages.append(
                GroupMessage(
                    timestamp=timestamp,
                    sender=str(raw.get("sender") or user_id or "未知成员"),
                    user_id=user_id,
                    content=content,
                )
            )
        return messages

    def _path(self, group_id: int) -> Path:
        return self.directory / f"{int(group_id)}.json"


def message_from_context(ctx: MessageContext) -> GroupMessage | None:
    content = render_segments(ctx.event.get("message"))
    if not content:
        return None
    sender_data = ctx.event.get("sender")
    sender_data = sender_data if isinstance(sender_data, dict) else {}
    sender = str(sender_data.get("card") or sender_data.get("nickname") or ctx.user_id or "未知成员").strip()
    try:
        timestamp = int(ctx.event.get("time") or datetime.now().timestamp())
    except (TypeError, ValueError):
        timestamp = int(datetime.now().timestamp())
    return GroupMessage(timestamp, sender, ctx.user_id, content)


def render_segments(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for segment in value:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if segment_type == "text":
            parts.append(str(data.get("text") or ""))
        elif segment_type == "at":
            parts.append(f"@{data.get('name') or data.get('qq') or '成员'}")
        elif segment_type == "image":
            summary = str(data.get("summary") or "").strip()
            parts.append(f"[图片：{summary}]" if summary else "[图片]")
        elif segment_type == "face":
            parts.append("[表情]")
        elif segment_type == "reply":
            parts.append(f"[回复消息 {data.get('id') or ''}]".rstrip())
        elif segment_type in {"forward", "node"}:
            parts.append(_render_forward(data))
        elif segment_type:
            parts.append(f"[{segment_type}]")
    return "".join(parts).strip()


def _render_forward(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, list):
        lines: list[str] = []
        for node in content:
            if not isinstance(node, dict):
                continue
            node_data = node.get("data")
            node_data = node_data if isinstance(node_data, dict) else node
            name = str(node_data.get("name") or node_data.get("nickname") or node_data.get("uin") or "未知成员")
            rendered = render_segments(node_data.get("content") or node_data.get("message"))
            if rendered:
                lines.append(f"{name}: {rendered}")
        if lines:
            return "[聊天记录]\n" + "\n".join(lines)
    forward_id = data.get("id") or data.get("resid")
    return f"[聊天记录，ID: {forward_id}]" if forward_id else "[聊天记录]"
