from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Character:
    name: str
    trigger_name: str
    description: str
    prompt: str


DEFAULT_CHARACTER = Character(
    name="普通QQ群群友",
    trigger_name="奎大宁",
    description="自然、友善地参与日常群聊，不抢话也不过分正式。",
    prompt=(
        "你是一名普通的QQ群群友。你熟悉网络聊天语气，表达自然、友善、简洁，"
        "会结合上下文接话、回答问题或适度开玩笑。不要自称AI或助手，不要长篇说教，"
        "不了解的事情坦率说明，也不要编造事实。"
    ),
)


class CharacterStore:
    def __init__(self, path: Path):
        self.path = path

    def list(self) -> list[Character]:
        characters = self._load()
        if characters:
            return characters
        self._save([DEFAULT_CHARACTER])
        return [DEFAULT_CHARACTER]

    def get(self, name: str) -> Character | None:
        normalized = name.strip().casefold()
        return next(
            (character for character in self.list() if character.name.casefold() == normalized),
            None,
        )

    def save(self, character: Character, original_name: str | None = None) -> Character:
        value = Character(
            name=character.name.strip(),
            trigger_name=character.trigger_name.strip(),
            description=character.description.strip(),
            prompt=character.prompt.strip(),
        )
        if not value.name or not value.trigger_name or not value.description or not value.prompt:
            raise ValueError("人设名字、姓名、描述和正文提示词不能为空")
        characters = self.list()
        original_key = original_name.strip().casefold() if original_name else None
        duplicate = next(
            (
                item
                for item in characters
                if item.name.casefold() == value.name.casefold()
                and item.name.casefold() != original_key
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError(f"人设“{value.name}”已经存在")
        if original_key:
            characters = [
                value if item.name.casefold() == original_key else item
                for item in characters
            ]
            if all(item.name.casefold() != value.name.casefold() for item in characters):
                characters.append(value)
        else:
            characters.append(value)
        self._save(characters)
        return value

    def delete(self, name: str) -> list[Character]:
        key = name.strip().casefold()
        characters = [item for item in self.list() if item.name.casefold() != key]
        if not characters:
            characters = [DEFAULT_CHARACTER]
        self._save(characters)
        return characters

    def _load(self) -> list[Character]:
        if not self.path.exists():
            return []
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 AI 人设配置失败")
            return []
        raw_items = data.get("characters") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            return []
        result: list[Character] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            character = Character(
                name=str(item.get("name") or "").strip(),
                trigger_name=str(
                    item.get("trigger_name")
                    or (
                        DEFAULT_CHARACTER.trigger_name
                        if item.get("name") == DEFAULT_CHARACTER.name
                        else item.get("name")
                    )
                    or ""
                ).strip(),
                description=str(item.get("description") or "").strip(),
                prompt=str(item.get("prompt") or "").strip(),
            )
            if (
                character.name
                and character.trigger_name
                and character.description
                and character.prompt
            ):
                result.append(character)
        return result

    def _save(self, characters: list[Character]) -> None:
        write_json(self.path, {"characters": [asdict(item) for item in characters]})


class CharacterSelectionStore:
    def __init__(self, path: Path):
        self.path = path

    def get(self, conversation: str, characters: CharacterStore) -> Character:
        selections = self._load()
        selected = characters.get(str(selections.get(conversation) or ""))
        return selected or characters.list()[0]

    def set(self, conversation: str, character: Character) -> None:
        selections = self._load()
        selections[conversation] = character.name
        write_json(self.path, selections)

    def rename(self, old_name: str, new_name: str) -> None:
        selections = self._load()
        old_key = old_name.strip().casefold()
        changed = False
        for conversation, selected in selections.items():
            if selected.casefold() == old_key:
                selections[conversation] = new_name
                changed = True
        if changed:
            write_json(self.path, selections)

    def remove(self, name: str) -> None:
        selections = self._load()
        key = name.strip().casefold()
        filtered = {
            conversation: selected
            for conversation, selected in selections.items()
            if selected.casefold() != key
        }
        if filtered != selections:
            write_json(self.path, filtered)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 AI 人设选择失败")
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}
