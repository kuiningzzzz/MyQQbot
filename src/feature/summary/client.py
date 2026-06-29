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
        if not config.api_key:
            raise SummaryApiError("尚未配置模型 API Key")
        url = config.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"请总结以下群聊记录：\n\n{history}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                    "stream": False,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data: Any = response.json()
            content = data["choices"][0]["message"]["content"]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            raise SummaryApiError("模型 API 请求失败，请检查配置和后端日志") from exc
        result = str(content).strip()
        if not result:
            raise SummaryApiError("模型返回了空内容")
        return result
