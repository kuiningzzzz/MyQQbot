from __future__ import annotations

import asyncio
import copy
import json


def token_count(value) -> int:
    # Conservative Unicode estimate; includes JSON/tool/schema overhead at call sites.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return sum(1 if ord(char) < 128 else 2 for char in text)


class ContextManager:
    def __init__(self, workspace, config, client, identity):
        self.workspace, self.config, self.client, self.identity = workspace, config, client, identity
        self.scenes = workspace.load_state("context", {})
        self._lock = asyncio.Lock()

    async def append(self, record: dict) -> None:
        async with self._lock:
            key = f"{record['scene_type']}:{record['scene_id']}"
            scene = self.scenes.setdefault(key, {"recent_messages": [], "compressed_summary": "", "last_activity": ""})
            if any(item["id"] == record["id"] for item in scene["recent_messages"]):
                return
            scene["recent_messages"].append(record)
            scene["recent_messages"] = scene["recent_messages"][-1000:]
            scene["last_activity"] = record["timestamp"]
            await asyncio.to_thread(self.workspace.save_state, "context", self.scenes)

    def snapshot(self) -> dict:
        return copy.deepcopy(self.scenes)

    def prompt_snapshot(self) -> dict:
        snapshot = self.snapshot()
        for scene in snapshot.values():
            for record in scene["recent_messages"]:
                record.pop("raw_line", None)
        return snapshot

    async def for_prompt(self) -> str:
        # Model context/compaction belongs exclusively to dsh. This cache is
        # only for QQ recall lookup and the legacy /short command.
        return json.dumps(self.prompt_snapshot(), ensure_ascii=False)

    async def prune(self, cutoff):
        from datetime import datetime
        async with self._lock:
            for key in list(self.scenes):
                scene = self.scenes[key]
                scene["recent_messages"] = [r for r in scene["recent_messages"] if datetime.fromisoformat(r["timestamp"]).timestamp() >= cutoff][-1000:]
                scene["compressed_summary"] = ""
                if not scene["recent_messages"]:
                    del self.scenes[key]
            await asyncio.to_thread(self.workspace.save_state, "context", self.scenes)

    def recent(self, scene: str, count: int) -> list[dict]:
        return copy.deepcopy(self.scenes.get(scene, {}).get("recent_messages", [])[-count:])

    async def mark_recalled(self, scene, message_id):
        async with self._lock:
            for record in self.scenes.get(scene, {}).get("recent_messages", []):
                if record["id"] == str(message_id):
                    record["recalled"] = True
            await asyncio.to_thread(self.workspace.save_state, "context", self.scenes)

    async def update_content(self, scene, message_id, content):
        async with self._lock:
            for record in self.scenes.get(scene, {}).get('recent_messages', []):
                if record['id'] == message_id:
                    record['content'] = content
            await asyncio.to_thread(self.workspace.save_state, 'context', self.scenes)
