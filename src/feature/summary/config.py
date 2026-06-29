import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummaryConfigData:
    api_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""
    summary_enabled: bool = True
    chat_enabled: bool = True
    chat_history_limit: int = 200


class SummaryConfig:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> SummaryConfigData:
        if not self.path.exists():
            return SummaryConfigData()
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取群聊总结配置失败")
            return SummaryConfigData()
        if not isinstance(data, dict):
            return SummaryConfigData()
        defaults = SummaryConfigData()
        return SummaryConfigData(
            api_url=str(data.get("api_url") or defaults.api_url).strip(),
            model=str(data.get("model") or defaults.model).strip(),
            api_key=str(data.get("api_key") or "").strip(),
            summary_enabled=bool(data.get("summary_enabled", defaults.summary_enabled)),
            chat_enabled=bool(data.get("chat_enabled", defaults.chat_enabled)),
            chat_history_limit=_normalize_history_limit(
                data.get("chat_history_limit", defaults.chat_history_limit)
            ),
        )

    def save(
        self,
        api_url: str,
        model: str,
        api_key: str | None = None,
        summary_enabled: bool | None = None,
        chat_enabled: bool | None = None,
        chat_history_limit: int | None = None,
    ) -> SummaryConfigData:
        current = self.load()
        value = SummaryConfigData(
            api_url=api_url.strip(),
            model=model.strip(),
            api_key=current.api_key if api_key is None else api_key.strip(),
            summary_enabled=current.summary_enabled if summary_enabled is None else summary_enabled,
            chat_enabled=current.chat_enabled if chat_enabled is None else chat_enabled,
            chat_history_limit=(
                current.chat_history_limit
                if chat_history_limit is None
                else _normalize_history_limit(chat_history_limit)
            ),
        )
        if not value.api_url or not value.model:
            raise ValueError("API URL 和模型名不能为空")
        write_json(self.path, asdict(value))
        return value

    def clear_api_key(self) -> None:
        current = self.load()
        write_json(
            self.path,
            asdict(
                SummaryConfigData(
                    api_url=current.api_url,
                    model=current.model,
                    summary_enabled=current.summary_enabled,
                    chat_enabled=current.chat_enabled,
                    chat_history_limit=current.chat_history_limit,
                )
            ),
        )


def _normalize_history_limit(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 200
    return min(200, max(10, number))
