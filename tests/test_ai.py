import asyncio
import json
import tempfile
import unittest
import subprocess
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, WebSocketDisconnect

from src.bot import QQBot
from src.feature.ai.admin import AiAdmin
from src.feature.ai.client import ModelClient, ModelError
from src.feature.ai.config import Settings
from src.feature.ai.feature import AiFeature
from src.feature.ai.storage import Workspace, atomic_write, digest, period_date
from src.feature.atom.context import MessageContext
from src.feature.manager import FeatureManager


class StubModel:
    def __init__(self):
        self.answers = []
        self.calls = []

    async def complete(self, settings, system, content, model=None):
        self.calls.append((system, content))
        return self.answers.pop(0)

    async def turn(self, settings, messages, tools=None, model=None):
        self.calls.append((messages, tools))
        return self.answers.pop(0)


class StorageTests(unittest.TestCase):
    def test_full_application_composes_without_retired_modules(self):
        root = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", "import src.main as m; names=list(m.feature_manager.features); assert 'ai' in names and 'news' not in names and 'summary' not in names; assert m.admin_app; print('APP_OK')"], cwd=directory, env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("APP_OK", result.stdout)


    def test_paths_atomic_and_optimistic_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for path in ("raw/2026-09-07.md", "AGENT.md", "../secret.md", "user/123/memory/../../secret.md", "user/123/memory/..\\secret.md"):
                with self.assertRaises(ValueError):
                    workspace.read_memory(path)
            path = "user/123/PREFERENCE.md"
            workspace.write_memory(path, "old", digest(""))
            with self.assertRaises(ValueError):
                workspace.write_memory(path, "lost update", digest(""))
            self.assertEqual(workspace.read_memory(path), "old")
            with patch("src.feature.ai.storage.os.replace", side_effect=OSError("disk error")), patch("src.feature.ai.storage.time.sleep"):
                with self.assertRaises(OSError):
                    workspace.write_memory(path, "new")
            self.assertEqual(workspace.read_memory(path), "old")
            with self.assertRaises(ValueError):
                workspace.init_user("123", {"123"})


    def test_concurrent_atomic_writers_preserve_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            contents = [f"- writer {index} " * 1000 for index in range(12)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda content: workspace.write_memory("user/123/PERSON.md", content), contents))
            self.assertIn(workspace.read_memory("user/123/PERSON.md"), contents)


class AsyncAiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = StubModel()
        self.feature = AiFeature(Path(self.temp.name) / "ai", client=self.client)
        self.feature.config.save({"vector_index_enabled": False, "bot_qq": "999", "nickname": "机器人", "memory_auto_learn": False})

    async def asyncTearDown(self):
        await self.feature.stop()
        self.temp.cleanup()








    async def test_group_upload_defers_download_and_uses_base64_fallback(self):
        f = self.feature
        action = AsyncMock(side_effect=[{}, {"file": "C:/napcat/document.txt", "file_name": "document.txt", "base64": "aGVsbG8="}])
        f.files.understand = AsyncMock(return_value="文档描述")
        await f.observe_system({"notice_type": "group_upload", "self_id": 999, "group_id": 12, "user_id": 123, "file": {"id": "file1", "name": "document.txt"}, "_bot": SimpleNamespace(call_action=action), "_websocket": None})
        f.files.understand.assert_not_awaited()
        action.assert_not_awaited()
        item = f.workfiles.list("group:12")[0]
        f.sandbox.put = AsyncMock(return_value={"success": True})
        ctx = MessageContext(bot=SimpleNamespace(call_action=action), websocket=None, event={}, msg="", message_type="group", user_id=123, group_id=12)
        await f.workfiles.download(ctx, item["attachment_id"], "document.txt", lambda: True)
        f.sandbox.put.assert_awaited_once_with("group:12", "document.txt", b"hello")
        self.assertEqual(action.await_args_list[0].args[0], "get_group_file_url")
        self.assertIn("document.txt", f.context.recent("group:12", 1)[0]["content"])







class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_read_ack_does_not_deadlock_receiver(self):
        queue = asyncio.Queue()
        notices = []
        manager = SimpleNamespace(observe_system=AsyncMock(side_effect=lambda event: notices.append(event)), observe_arrival=lambda event: None)
        bot = QQBot(manager, None)

        class Socket:
            client = SimpleNamespace(host="test")

            async def accept(self):
                pass

            async def receive_text(self):
                event = await queue.get()
                if event is None:
                    raise WebSocketDisconnect()
                return json.dumps(event)

            async def send_text(self, text):
                request = json.loads(text)
                await queue.put({"echo": request["echo"], "status": "ok", "data": {"messages": ["forwarded"]}})

        async def handle(event, ws):
            result = await bot.call_action("get_forward_msg", {"message_id": "forward1"}, ws)
            self.assertEqual(result, {"messages": ["forwarded"]})
            await queue.put(None)

        bot.handle_event = handle
        await queue.put({"post_type": "message", "self_id": 999})
        await asyncio.wait_for(bot.handle_websocket(Socket()), timeout=2)
        self.assertEqual(notices[-1]["kind"], "disconnect")

    async def test_sse_tool_fragments_and_reasoning(self):
        fragments = [
            {"choices": [{"delta": {"reasoning_content": "思考", "tool_calls": [{"index": 0, "id": "call1", "function": {"name": "query_", "arguments": '{"query":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "memory", "arguments": '"咖啡"}'}}]}, "finish_reason": "tool_calls"}]},
        ]
        body = "\n\n".join("data: " + json.dumps(part) for part in fragments) + "\n\ndata: [DONE]\n"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
        original = httpx.AsyncClient
        with patch("src.feature.ai.client.httpx.AsyncClient", side_effect=lambda **kwargs: original(transport=transport, **kwargs)):
            result = await ModelClient().turn(Settings(api_key="test"), [{"role": "user", "content": "hi"}])
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "query_memory")
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"query": "咖啡"})
        self.assertEqual(result["reasoning_content"], "思考")

    async def test_timeout_is_reported(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")
        original = httpx.AsyncClient
        with patch("src.feature.ai.client.httpx.AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            with self.assertRaises(ModelError):
                await ModelClient().turn(Settings(api_key="test"), [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
