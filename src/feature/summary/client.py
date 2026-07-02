from dataclasses import dataclass
import json
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
                "stream": True,
            }
            if config.thinking_mode != "inherit":
                payload["thinking"] = {"type": config.thinking_mode}
            if config.thinking_mode == "enabled":
                payload["reasoning_effort"] = config.reasoning_effort
            else:
                payload["temperature"] = temperature
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=(10, self.timeout_seconds),
                    stream=True,
                )
                with response:
                    response.raise_for_status()
                    content, reasoning_present, finish_reason = (
                        self._read_response(response)
                    )
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
            raise SummaryApiError("模型 API 请求失败，请检查配置和后端日志") from exc
        result = content.strip()
        if not result:
            if finish_reason == "length":
                raise SummaryApiError(
                    "模型在生成最终回答前已耗尽最大生成 Token，请提高 max_tokens 或降低思考强度"
                )
            raise SummaryApiError(
                "模型返回了空的最终回答"
                f"（finish_reason={finish_reason}，思维链={'有' if reasoning_present else '无'}）"
            )
        return result

    def complete_with_tools(
        self,
        config: SummaryConfigData,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """Return one assistant turn, including any requested function calls."""
        if not config.api_key:
            raise SummaryApiError("尚未配置模型 API Key")
        url = config.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": config.max_tokens,
            "stream": True,
        }
        if config.thinking_mode != "inherit":
            payload["thinking"] = {"type": config.thinking_mode}
        if config.thinking_mode == "enabled":
            payload["reasoning_effort"] = config.reasoning_effort
        else:
            payload["temperature"] = temperature
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=(10, self.timeout_seconds),
                    stream=True,
                )
                with response:
                    response.raise_for_status()
                    return self._read_tool_response(response)
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
            raise SummaryApiError("模型工具调用请求失败，请检查配置和后端日志") from exc

    @staticmethod
    def _read_tool_response(response: Any) -> dict[str, Any]:
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            data: Any = response.json()
            message = dict(data["choices"][0]["message"])
            message.setdefault("role", "assistant")
            return message

        response.encoding = "utf-8"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            event: Any = json.loads(raw)
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for part in delta.get("tool_calls") or []:
                index = int(part.get("index", 0))
                call = calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if part.get("id"):
                    call["id"] += str(part["id"])
                function = part.get("function") or {}
                call["function"]["name"] += str(function.get("name") or "")
                call["function"]["arguments"] += str(function.get("arguments") or "")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return message

    @staticmethod
    def _read_response(response: Any) -> tuple[str, bool, str]:
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            data: Any = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            return (
                content if isinstance(content, str) else "",
                bool(str(reasoning or "").strip()),
                str(choice.get("finish_reason") or "unknown"),
            )

        response.encoding = "utf-8"
        content_parts: list[str] = []
        reasoning_present = False
        finish_reason = "unknown"
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            event_data = line[5:].strip()
            if event_data == "[DONE]":
                break
            event: Any = json.loads(event_data)
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            part = delta.get("content")
            if isinstance(part, str):
                content_parts.append(part)
            if str(delta.get("reasoning_content") or "").strip():
                reasoning_present = True
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
        return "".join(content_parts), reasoning_present, finish_reason
