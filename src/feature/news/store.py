import json
import threading
from dataclasses import dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json


@dataclass(frozen=True, order=True)
class NewsTarget:
    conversation_type: str
    conversation_id: int


class NewsStore:
    def __init__(self, subscriptions_path: Path, history_path: Path):
        self.subscriptions_path = subscriptions_path
        self.history_path = history_path
        self._lock = threading.RLock()

    def targets(self) -> list[NewsTarget]:
        with self._lock:
            if not self.subscriptions_path.exists():
                return []
            try:
                data = read_json(self.subscriptions_path)
            except (OSError, json.JSONDecodeError):
                return []
            items = data.get("targets") if isinstance(data, dict) else None
            result: set[NewsTarget] = set()
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("conversation_type") or "")
                try:
                    identifier = int(item.get("conversation_id"))
                except (TypeError, ValueError):
                    continue
                if kind in {"group", "private"}:
                    result.add(NewsTarget(kind, identifier))
            return sorted(result)

    def subscribe(self, target: NewsTarget) -> bool:
        with self._lock:
            targets = set(self.targets())
            added = target not in targets
            targets.add(target)
            self._save_targets(targets)
            return added

    def unsubscribe(self, target: NewsTarget) -> bool:
        with self._lock:
            targets = set(self.targets())
            removed = target in targets
            targets.discard(target)
            self._save_targets(targets)
            return removed

    def recent_news(self, limit: int = 5) -> list[dict[str, str]]:
        with self._lock:
            data = self._history_data()
            items = data.get("items")
            return list(items[-limit:]) if isinstance(items, list) else []

    def last_attempt_date(self) -> str | None:
        value = self._history_data().get("last_attempt_date")
        return str(value) if value else None

    def mark_attempt(self, date_text: str) -> None:
        with self._lock:
            data = self._history_data()
            data["last_attempt_date"] = date_text
            write_json(self.history_path, data)

    def add_news(self, generated_at: str, content: str) -> None:
        with self._lock:
            data = self._history_data()
            items = data.get("items") if isinstance(data.get("items"), list) else []
            data["items"] = [*items, {"generated_at": generated_at, "content": content}][-5:]
            write_json(self.history_path, data)

    def _save_targets(self, targets: set[NewsTarget]) -> None:
        write_json(
            self.subscriptions_path,
            {"targets": [target.__dict__ for target in sorted(targets)]},
        )

    def _history_data(self) -> dict:
        if not self.history_path.exists():
            return {"last_attempt_date": None, "items": []}
        try:
            data = read_json(self.history_path)
        except (OSError, json.JSONDecodeError):
            return {"last_attempt_date": None, "items": []}
        return data if isinstance(data, dict) else {"last_attempt_date": None, "items": []}
