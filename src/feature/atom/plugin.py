from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from src.feature.atom.base import Feature

if TYPE_CHECKING:
    from fastapi import FastAPI


class AdminExtension(Protocol):
    """Feature-owned contribution to the generic administration application."""

    def register_routes(self, app: "FastAPI") -> None: ...

    @property
    def panel_html(self) -> str: ...

    @property
    def javascript(self) -> str: ...


@dataclass(frozen=True)
class StaticResource:
    route: str
    directory: Path
    name: str


@dataclass(frozen=True)
class FeaturePlugin:
    feature: Feature
    admin: AdminExtension | None = None
    static_resources: tuple[StaticResource, ...] = field(default_factory=tuple)
