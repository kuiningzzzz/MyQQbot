from __future__ import annotations

import json

import httpx


class ModelError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型必须返回 JSON 对象")
    return value


class ModelClient:
    """Auxiliary vision/summary transport; output size uses server defaults."""

    async def turn(self, settings, messages: list[dict], tools: list[dict] | None = None, model: str | None = None) -> dict:
        if not settings.api_key:
            raise ModelError("尚未配置模型 API Key")
        url = settings.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = {"model": settings.model, "messages": messages, "stream": True}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(300, connect=10)) as client:
                async with client.stream("POST", url, headers={"Authorization": f"Bearer {settings.api_key}"}, json=payload) as response:
                    response.raise_for_status()
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        data = json.loads(await response.aread())
                        if data["choices"][0].get("finish_reason") == "length":
                            raise ModelError("模型响应达到服务端输出上限，请缩小请求范围")
                        message = data["choices"][0]["message"]
                    else:
                        content, reasoning, calls = [], [], {}
                        finish = None
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            event = json.loads(raw)
                            if event.get("error"):
                                raise ModelError("模型在流式生成期间返回错误")
                            if not event.get("choices"):
                                continue
                            choice = event["choices"][0]
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta") or {}
                            content.append(delta.get("content") or "")
                            reasoning.append(delta.get("reasoning_content") or "")
                            for part in delta.get("tool_calls") or []:
                                call = calls.setdefault(part["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                call["id"] += part.get("id") or ""
                                for key in ("name", "arguments"):
                                    call["function"][key] += (part.get("function") or {}).get(key) or ""
                        if finish == "length":
                            raise ModelError("模型响应达到服务端输出上限，请缩小请求范围")
                        message = {"role": "assistant", "content": "".join(content)}
                        if any(reasoning):
                            message["reasoning_content"] = "".join(reasoning)
                        if calls:
                            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise ModelError("模型请求失败或超时，请检查 API 配置、额度和网络") from exc
        message.setdefault("role", "assistant")
        if not message.get("content") and not message.get("tool_calls"):
            raise ModelError("模型没有返回最终内容或工具调用")
        return message

    async def complete(self, settings, system: str, content: str, model: str | None = None) -> str:
        result = await self.turn(settings, [{"role": "system", "content": system}, {"role": "user", "content": content}], model=model)
        return result.get("content") or ""
