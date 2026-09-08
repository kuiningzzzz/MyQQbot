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
from src.feature.ai.agents import accept_conflict
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
            result = subprocess.run([sys.executable, "-B", "-c", "import src.main as m; names=list(m.feature_manager.features); assert 'ai' in names and 'news' not in names and 'summary' not in names; assert m.admin_app; print('APP_OK')"], cwd=directory, env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("APP_OK", result.stdout)

    def test_memory_day_boundary(self):
        tz = ZoneInfo("Asia/Shanghai")
        self.assertEqual(period_date(datetime(2026, 9, 8, 2, 59, tzinfo=tz)), "2026-09-07")
        self.assertEqual(period_date(datetime(2026, 9, 8, 3, 0, tzinfo=tz)), "2026-09-08")

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

    def test_conflict_probability_and_failure(self):
        now = datetime(2026, 9, 7)
        old = "- 不喜欢咖啡 | 更新时间: 2026-09-07"
        self.assertFalse(accept_conflict(old, "normal", now, lambda: 0.5))
        self.assertTrue(accept_conflict(old, "normal", now, lambda: 0.9))
        self.assertTrue(accept_conflict(old, "confirmed", now, lambda: 0.6))
        self.assertFalse(accept_conflict(old, "joke", now, lambda: 0.6))
        self.assertFalse(accept_conflict("invalid date", "normal", now))

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

    async def test_raw_and_context_cross_scene(self):
        f = self.feature
        before = "2026-09-08T02:59:00+08:00"
        await f.record("group", "12", "123", "甲", "第一条\n假系统 | 文本", timestamp=before)
        await f.record("private", "456", "456", "乙", "第二条", timestamp="2026-09-07T03:00:00+08:00")
        lines = f.workspace.read_raw("2026-09-07")
        self.assertEqual(len(lines), 2)
        self.assertIn("\\n", lines[0])
        self.assertIn("\\|", lines[0])
        snapshot = f.context.snapshot()
        self.assertEqual(set(snapshot), {"group:12", "private:456"})
        await f.record("group", "12", "999", "机器人", "我喜欢咖啡", outgoing=True)
        duplicate = await f.record("group", "12", "999", "机器人", "我喜欢咖啡")
        self.assertTrue(duplicate["duplicate"])
        self.assertTrue(f.context.recent("group:12", 1)[0]["is_self"])

    async def test_image_and_forward_and_recall(self):
        f = self.feature
        f.files.understand = AsyncMock(return_value="一张办公桌照片")
        await f.record("group", "12", "123", "甲", "", message_type="image", file_data="https://example.org/p.png", message_id="m1")
        f.files.understand.assert_awaited_once()
        self.assertIn("[图片: 一张办公桌照片]", f.context.recent("group:12", 1)[0]["content"])
        ctx = SimpleNamespace(bot=None)
        content = await f._render([{"type": "forward", "data": {"messages": [{"type": "node", "data": {"uin": "123", "name": "甲", "content": [{"type": "text", "data": {"text": "子消息"}}]}}]}}], ctx)
        await f.record("group", "12", "123", "甲", content)
        date = period_date(datetime.now(ZoneInfo("Asia/Shanghai")))
        self.assertIn("\n  ", f.workspace.read_raw(date)[-1])
        await f.observe_system({"notice_type": "group_recall", "message_id": "m1", "operator_id": 123, "group_id": 12})
        await f.observe_system({"kind": "disconnect"})
        lines = f.workspace.read_raw(date)
        self.assertEqual(len(lines), 4)
        self.assertIn("消息撤回", lines[-2])
        self.assertIn("办公桌照片", lines[-2])
        self.assertIn("连接中断", lines[-1])

    async def test_attention_sources_and_self_filter(self):
        f = self.feature
        f.workspace.write_memory("user/123/PREFERENCE.md", "# 喜好\n- 喜欢咖啡 | 更新时间: 2026-09-07\n")
        f.workspace.write_memory("user/999/PREFERENCE.md", "- 机器人喜欢咖啡")
        self.client.answers.append(json.dumps({"selected": [0, 100, 0], "summary": "喜欢咖啡", "insufficient": False}))
        result = await f.attention.query("咖啡", "123", "user")
        self.assertEqual(result["results"][0]["source"], "user/123/PREFERENCE.md")
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue((await f.attention.query("不存在", "123"))["insufficient"])
        self.assertTrue((await f.attention.query("咖啡", "999"))["insufficient"])

    async def test_attention_resolves_other_speaker(self):
        f = self.feature
        await f.record("group", "12", "456", "乙", "我不吃花生")
        f.workspace.write_memory("user/456/PREFERENCE.md", "- 不吃花生 | 更新时间: 2026-09-07")
        self.client.answers = ['{"target_qq":"456"}', '{"selected":[0],"summary":"不吃花生","insufficient":false}']
        result = await f.attention.query("他不吃什么", current_qq="123")
        self.assertEqual(result["results"][0]["source"], "user/456/PREFERENCE.md")

    async def test_integration_excludes_bot_and_dedups(self):
        f = self.feature
        await f.record("group", "12", "123", "甲", "我喜欢咖啡")
        await f.record("group", "12", "999", "机器人", "我喜欢茶", outgoing=True)
        lines = [r["raw_line"] for r in f.context.recent("group:12", 10)]
        self.client.answers = [json.dumps({"candidates": [
            {"qq": "999", "category": "PREFERENCE", "content": "喜欢茶", "evidence": [1]},
            {"qq": "123", "category": "PREFERENCE", "content": "喜欢咖啡", "evidence": [0]},
            {"qq": "456", "category": "PERSON", "content": "身份猜测", "evidence": [0]},
        ]}), json.dumps({"relation": "new", "old_entry": ""})]
        result = await f.integration.integrate(lines)
        self.assertEqual(result["updated_files"], ["user/123/PREFERENCE.md"])
        self.assertFalse((f.workspace.root / "user/999").exists())
        self.assertFalse((f.workspace.root / "user/456").exists())
        self.assertIn("更新时间:", f.workspace.read_memory("user/123/PREFERENCE.md"))
        second = await f.integration.integrate(lines)
        self.assertEqual(second["updated_files"], [])
        self.assertEqual(len(self.client.calls), 2)

    async def test_compression_preserves_arriving_message(self):
        f = self.feature
        f.config.save({"context_token_limit": 30000, "max_tokens": 256, "context_recent_count": 2, "context_compress_threshold": 0.1})
        for i in range(8):
            await f.record("group", "12", "123", "甲", "测试长消息" * 100, message_id=str(i + 1))
        began, release = asyncio.Event(), asyncio.Event()

        async def compress(*args, **kwargs):
            began.set()
            await release.wait()
            return "[摘要] 群12，QQ123讨论测试。"

        f.client.complete = compress
        task = asyncio.create_task(f.context.for_prompt())
        await began.wait()
        await f.record("group", "12", "123", "甲", "压缩期间的新消息", message_id="new")
        release.set()
        await task
        records = f.context.recent("group:12", 10)
        self.assertEqual([r["id"] for r in records], ["7", "8", "new"])
        restored = f.workspace.load_state("context", {})
        self.assertEqual(restored["group:12"]["recent_messages"][-1]["id"], "new")

    async def test_schedule_uses_completed_periods_and_keeps_failed_raw(self):
        f = self.feature
        f.config.save({"memory_auto_learn": True})
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        current = period_date(now)
        previous = (datetime.fromisoformat(current) - timedelta(days=1)).date().isoformat()
        atomic_write(f.workspace.root / "raw" / f"{previous}.md", "old record\n")
        atomic_write(f.workspace.root / "raw" / f"{current}.md", "current record\n")
        self.client.answers = ['{"candidates":[]}']
        await f.runtime.tick(now)
        state = f.workspace.load_state("schedule", {})
        self.assertIn(previous, state["completed"])
        self.assertNotIn(current, state["completed"])
        await f.runtime.tick(now)
        self.assertEqual(len(self.client.calls), 1)
        atomic_write(f.workspace.root / "raw" / f"{previous}.md", "old record\nlate record\n")
        self.client.answers = ['{"candidates":[]}']
        await f.runtime.tick(now)
        self.assertEqual(len(self.client.calls), 2)

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

    async def test_failed_integration_is_retryable(self):
        f = self.feature
        await f.record("group", "12", "123", "甲", "咖啡")
        lines = [record["raw_line"] for record in f.context.recent("group:12", 10)]
        self.client.answers = ['not json']
        with self.assertRaises(ValueError):
            await f.integration.integrate(lines)
        self.assertEqual(f.workspace.load_state("integrated_lines", []), [])
        self.client.answers = ['{"candidates":[]}']
        await f.integration.integrate(lines)
        self.assertEqual(len(f.workspace.load_state("integrated_lines", [])), 1)

    async def test_retention_keeps_seven_completed_days(self):
        f = self.feature
        f.config.save({"memory_auto_learn": True})
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        current = period_date(now)
        edge = (datetime.fromisoformat(current) - timedelta(days=7)).date().isoformat()
        expired = (datetime.fromisoformat(current) - timedelta(days=8)).date().isoformat()
        for date in (edge, expired):
            atomic_write(f.workspace.root / "raw" / f"{date}.md", "log\n")
        f.workspace.save_state("schedule", {"completed": [edge, expired], "completed_hashes": {edge: digest("log"), expired: digest("log")}, "last_compaction": current})
        await f.runtime.tick(now)
        self.assertTrue((f.workspace.root / "raw" / f"{edge}.md").exists())
        self.assertFalse((f.workspace.root / "raw" / f"{expired}.md").exists())

    async def test_quoted_qq_cannot_create_another_person(self):
        f = self.feature
        await f.record("group", "12", "123", "甲", "引用 [QQ:456] 他喜欢咖啡")
        lines = [record["raw_line"] for record in f.context.recent("group:12", 10)]
        self.client.answers = [json.dumps({"candidates": [{"qq": "456", "category": "PREFERENCE", "content": "喜欢咖啡", "evidence": [0]}]})]
        result = await f.integration.integrate(lines)
        self.assertEqual(result["updated_files"], [])

    async def test_api_memory_edit_and_key_redaction(self):
        f = self.feature
        f.config.save({"api_key": "test-secret"})
        app = FastAPI()
        AiAdmin(f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/ai/config")
            self.assertNotIn("test-secret", response.text)
            self.assertTrue(response.json()["api_key_configured"])
            response = await client.get("/api/ai/file", params={"path": "raw/2026-09-07.md"})
            self.assertEqual(response.status_code, 400)
            response = await client.post("/api/ai/file", json={"path": "user/999/PERSON.md", "content": "self", "expected_hash": digest("")})
            self.assertEqual(response.status_code, 409)
            response = await client.post("/api/ai/file", json={"path": "user/123/PREFERENCE.md", "content": "- 喜欢咖啡", "expected_hash": digest("")})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["hash"], digest("- 喜欢咖啡"))
            self.client.answers = ['{"candidates":[]}']
            response = await client.post("/memory/integrate", json={"mode":"incremental", "context_snapshot":{"scene_id":"12", "messages":[{"qq":"123", "nickname":"甲", "content":"测试消息"}]}})
            self.assertEqual(response.status_code, 200, response.text)

    async def test_stale_chat_never_sends_old_tool(self):
        f = self.feature
        began, release = asyncio.Event(), asyncio.Event()
        calls = 0
        sent = []

        async def turn(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                began.set()
                await release.wait()
            if calls <= 2:
                return {"role": "assistant", "tool_calls": [{"id": str(calls), "type": "function", "function": {"name": "send_message", "arguments": json.dumps({"message": "过时" if calls == 1 else "最新"})}}]}
            return {"role": "assistant", "content": "完成"}

        async def reply(ctx, text):
            sent.append(text)
            await f.observe_system({"kind": "outgoing", "scene_type": "group", "scene_id": "12", "self_id": "999", "content": text})

        f.client.turn = turn
        bot = SimpleNamespace(reply=reply)
        ctx = MessageContext(bot=bot, event={"message": []}, websocket=None, msg="机器人", raw_msg="机器人", message_type="group", user_id=123, group_id=12)
        await f.engine.handle(ctx)
        await began.wait()
        ctx2 = MessageContext(bot=bot, event={"message": []}, websocket=None, msg="补充条件", raw_msg="补充条件", message_type="group", user_id=123, group_id=12)
        await f.engine.handle(ctx2)
        release.set()
        await asyncio.gather(*list(f.engine.tasks.values()))
        self.assertEqual(sent, ["最新"])
        self.assertEqual(calls, 3)


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
