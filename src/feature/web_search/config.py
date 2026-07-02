import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json


@dataclass(frozen=True)
class WebSearchConfigData:
    enabled: bool = False
    provider: str = "tavily"
    api_url: str = "https://api.tavily.com/search"
    api_key: str = ""
    max_results: int = 5
    timeout_seconds: int = 20


class WebSearchConfig:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> WebSearchConfigData:
        defaults = WebSearchConfigData()
        if not self.path.exists():
            return defaults
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(data, dict):
            return defaults
        provider = str(data.get("provider") or defaults.provider).casefold()
        if provider not in {"tavily", "searxng"}:
            provider = defaults.provider
        return WebSearchConfigData(
            enabled=bool(data.get("enabled", defaults.enabled)),
            provider=provider,
            api_url=str(data.get("api_url") or defaults.api_url).strip(),
            api_key=str(data.get("api_key") or "").strip(),
            max_results=_bounded_int(data.get("max_results"), 1, 10, 5),
            timeout_seconds=_bounded_int(data.get("timeout_seconds"), 5, 60, 20),
        )

    def save(
        self,
        enabled: bool,
        provider: str,
        api_url: str,
        api_key: str | None,
        max_results: int,
        timeout_seconds: int,
    ) -> WebSearchConfigData:
        current = self.load()
        provider = provider.casefold().strip()
        if provider not in {"tavily", "searxng"}:
            raise ValueError("搜索服务只支持 tavily 或 searxng")
        if not api_url.strip().startswith(("http://", "https://")):
            raise ValueError("搜索 API URL 必须以 http:// 或 https:// 开头")
        value = WebSearchConfigData(
            enabled=enabled,
            provider=provider,
            api_url=api_url.strip(),
            api_key=current.api_key if api_key is None else api_key.strip(),
            max_results=_bounded_int(max_results, 1, 10, 5),
            timeout_seconds=_bounded_int(timeout_seconds, 5, 60, 20),
        )
        write_json(self.path, asdict(value))
        return value

    def clear_api_key(self) -> None:
        value = self.load()
        write_json(self.path, {**asdict(value), "api_key": ""})

    @property
    def status(self) -> dict[str, object]:
        value = self.load()
        return {
            "enabled": value.enabled,
            "provider": value.provider,
            "api_url": value.api_url,
            "has_api_key": bool(value.api_key),
            "max_results": value.max_results,
            "timeout_seconds": value.timeout_seconds,
        }


def _bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))
