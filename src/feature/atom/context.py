from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class BotActions(Protocol):
    async def reply(self, ctx: "MessageContext", message: str) -> None: ...

    async def send_forward_msg(
        self,
        ctx: "MessageContext",
        messages: list[str | list[dict[str, Any]]],
    ) -> None: ...

    async def send_image_forward_msg(
        self,
        ctx: "MessageContext",
        image_refs: list[str],
    ) -> None: ...

    async def send_file(
        self,
        ctx: "MessageContext",
        file_path: Path,
        name: str | None = None,
        public_file_root: Path | None = None,
        public_route: str | None = None,
    ) -> None: ...

    def resolve_public_file_reference(
        self,
        file_path: Path,
        public_file_root: Path,
        public_route: str,
    ) -> str: ...


@dataclass
class MessageContext:
    bot: BotActions
    event: dict[str, Any]
    websocket: Any
    msg: str
    message_type: str | None
    user_id: int | None
    group_id: int | None
    raw_msg: str = ""
