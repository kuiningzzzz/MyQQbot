from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from dataclasses import dataclass

from src.feature.atom.context import MessageContext

if TYPE_CHECKING:
    from src.feature.atom.context import BotActions


@dataclass(frozen=True)
class FeatureInfo:
    name: str
    title: str
    description: str
    enabled: bool


class Feature(ABC):
    name: str
    title: str
    description: str
    default_enabled = True
    show_in_admin = True

    async def observe(self, ctx: MessageContext) -> None:
        """Observe an event even while the feature is disabled."""

    async def start(self, bot: "BotActions") -> None:
        """Start optional background work after the bot server is ready."""

    async def stop(self) -> None:
        """Stop optional background work during application shutdown."""

    def on_enabled_changed(self, enabled: bool) -> None:
        """Receive the persisted WebUI feature switch state."""

    @abstractmethod
    async def handle(self, ctx: MessageContext) -> bool:
        """Return True when this feature handled the message."""
