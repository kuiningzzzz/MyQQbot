from dataclasses import dataclass
from typing import Any

import requests

from src.feature.summary.config import SummaryConfigData


class SummaryApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleClient:
    timeout_seconds: int = 90

    def summarize(self, config: SummaryConfigData, system_prompt: str, history: str) -> str:
        return self.complete(
            config,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请总结以下群聊记录：\n\n{history}"},
            ],
            temperature=0.3,
        )

    def complete(
        self,
        config: SummaryConfigData,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        if not config.api_key:
            raise SummaryApiError("尚未配置模型 API Key")
        url = config.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        try:
            payload: dict[str, Any] = {
                "model": config.model,
                "messages": messages,
                "max_tokens": max_tokens or config.max_tokens,
                "stream": False,
            }
            if config.thinking_mode != "inherit":
                payload["thinking"] = {"type": config.thinking_mode}
            if config.thinking_mode == "enabled":
                payload["reasoning_effort"] = config.reasoning_effort
            else:
                payload["temperature"] = temperature
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data: Any = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
            raise SummaryApiError("模型 API 请求失败，请检查配置和后端日志") from exc
        result = content.strip() if isinstance(content, str) else ""
        if not result:
            finish_reason = str(choice.get("finish_reason") or "unknown")
            reasoning_present = bool(str(message.get("reasoning_content") or "").strip())
            if finish_reason == "length":
                raise SummaryApiError(
                    "模型在生成最终回答前已耗尽最大生成 Token，请提高 max_tokens 或降低思考强度"
                )
            raise SummaryApiError(
                "模型返回了空的最终回答"
                f"（finish_reason={finish_reason}，思维链={'有' if reasoning_present else '无'}）"
            )
        return result
