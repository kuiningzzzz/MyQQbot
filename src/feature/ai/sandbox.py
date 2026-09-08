from __future__ import annotations

import asyncio
import base64
import json
import os


class SandboxClient:
    """No local-shell fallback, even when running outside Docker."""
    def __init__(self):
        self.socket = os.getenv("AI_SANDBOX_SOCKET", "/control/sandbox.sock")
        self.lock = asyncio.Lock()

    async def request(self, op, scene="", **values):
        async with self.lock:
            writer = None
            try:
                if not hasattr(asyncio, "open_unix_connection"):
                    raise OSError("当前平台不支持Unix socket")
                async with asyncio.timeout(150):
                    reader, writer = await asyncio.open_unix_connection(self.socket, limit=30 * 1024 * 1024)
                    payload = json.dumps({**values, "op": op, "scene": scene}, ensure_ascii=False).encode() + b"\n"
                    if len(payload) > 30 * 1024 * 1024:
                        raise ValueError("工作区请求过大")
                    writer.write(payload)
                    await writer.drain()
                    raw = await reader.readline()
                    if not raw:
                        raise ValueError("工作容器连接中断")
                    response = json.loads(raw)
                    if not isinstance(response, dict):
                        raise ValueError("工作容器返回格式错误")
                    return response
            except (OSError, NotImplementedError) as exc:
                raise RuntimeError("独立工作容器不可用，请启动 ai-sandbox；禁止回退到服务器本机执行") from exc
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()

    async def put(self, scene, path, data):
        return await self.request("put", scene, path=path, data=base64.b64encode(data).decode())

    async def analyze(self, data, pdf=False):
        # OCR is separated from model-controlled command workspaces.
        return await self.request("analyze", "private:0", data=base64.b64encode(data).decode(), pdf=pdf)
