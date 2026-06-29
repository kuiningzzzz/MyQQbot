from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.feature.atom.context import MessageContext


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

    @abstractmethod
    async def handle(self, ctx: MessageContext) -> bool:
        """Return True when this feature handled the message."""
