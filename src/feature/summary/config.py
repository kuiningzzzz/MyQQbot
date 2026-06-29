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
    chat_window_min: int = 150
    chat_window_max: int = 200
    thinking_mode: str = "inherit"
    reasoning_effort: str = "high"
    context_token_limit: int = 16000
    max_tokens: int = 8192


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
            chat_window_min=_normalize_history_limit(
                data.get("chat_window_min", defaults.chat_window_min)
            ),
            chat_window_max=_normalize_history_limit(
                data.get("chat_window_max", defaults.chat_window_max)
            ),
            thinking_mode=_normalize_thinking_mode(
                data.get("thinking_mode", defaults.thinking_mode)
            ),
            reasoning_effort=_normalize_reasoning_effort(
                data.get("reasoning_effort", defaults.reasoning_effort)
            ),
            context_token_limit=_normalize_int(
                data.get("context_token_limit", defaults.context_token_limit),
                1024,
                128000,
                defaults.context_token_limit,
            ),
            max_tokens=_normalize_int(
                data.get("max_tokens", defaults.max_tokens),
                256,
                65536,
                defaults.max_tokens,
            ),
        )

    def save(
        self,
        api_url: str,
        model: str,
        api_key: str | None = None,
        summary_enabled: bool | None = None,
        chat_enabled: bool | None = None,
        chat_window_min: int | None = None,
        chat_window_max: int | None = None,
        thinking_mode: str | None = None,
        reasoning_effort: str | None = None,
        context_token_limit: int | None = None,
        max_tokens: int | None = None,
    ) -> SummaryConfigData:
        current = self.load()
        value = SummaryConfigData(
            api_url=api_url.strip(),
            model=model.strip(),
            api_key=current.api_key if api_key is None else api_key.strip(),
            summary_enabled=current.summary_enabled if summary_enabled is None else summary_enabled,
            chat_enabled=current.chat_enabled if chat_enabled is None else chat_enabled,
            chat_window_min=(
                current.chat_window_min
                if chat_window_min is None
                else _normalize_history_limit(chat_window_min)
            ),
            chat_window_max=(
                current.chat_window_max
                if chat_window_max is None
                else _normalize_history_limit(chat_window_max)
            ),
            thinking_mode=(
                current.thinking_mode
                if thinking_mode is None
                else _normalize_thinking_mode(thinking_mode)
            ),
            reasoning_effort=(
                current.reasoning_effort
                if reasoning_effort is None
                else _normalize_reasoning_effort(reasoning_effort)
            ),
            context_token_limit=(
                current.context_token_limit
                if context_token_limit is None
                else _normalize_int(context_token_limit, 1024, 128000, 16000)
            ),
            max_tokens=(
                current.max_tokens
                if max_tokens is None
                else _normalize_int(max_tokens, 256, 65536, 8192)
            ),
        )
        if not value.api_url or not value.model:
            raise ValueError("API URL 和模型名不能为空")
        if value.chat_window_min > value.chat_window_max:
            raise ValueError("动态窗口起始消息数不能大于最大消息数")
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
                    chat_window_min=current.chat_window_min,
                    chat_window_max=current.chat_window_max,
                    thinking_mode=current.thinking_mode,
                    reasoning_effort=current.reasoning_effort,
                    context_token_limit=current.context_token_limit,
                    max_tokens=current.max_tokens,
                )
            ),
        )


def _normalize_history_limit(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 200
    return min(200, max(10, number))


def _normalize_thinking_mode(value: object) -> str:
    mode = str(value or "inherit").strip().lower()
    return mode if mode in {"inherit", "enabled", "disabled"} else "inherit"


def _normalize_reasoning_effort(value: object) -> str:
    effort = str(value or "high").strip().lower()
    return effort if effort in {"high", "max"} else "high"


def _normalize_int(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(maximum, max(minimum, number))
