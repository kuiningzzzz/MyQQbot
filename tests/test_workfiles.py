import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.feature.ai.feature import AiFeature
from src.feature.atom.context import MessageContext


class WorkFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({"bot_qq": "999", "vector_index_enabled": False, "memory_auto_learn": False})
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={"file_id": "sent1"}), reply=AsyncMock())
        self.ctx = MessageContext(bot=self.bot, event={"self_id": 999, "message_id": 101}, websocket=None, msg="处理文件", message_type="group", user_id=123, group_id=12)

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_ocr_text_and_visuals_reach_vision_and_context(self):
        self.f.sandbox.analyze = AsyncMock(return_value={"success": True, "pages": [{"page": 1, "text": "总价88.50", "image": "data:image/png;base64,eA=="}]})
        self.f.client.turn = AsyncMock(return_value={"content": "订单截图，金额88.50。"})
        result = await self.f.files.understand("data:image/png;base64,eA==")
        messages = self.f.client.turn.await_args.args[1]
        self.assertIn("详细", messages[0]["content"])
        self.assertIn("总价88.50", json.dumps(messages, ensure_ascii=False))
        self.assertIn("总价88.50", result)
        self.assertIn("订单截图", result)

    async def test_pdf_ocr_failure_does_not_invent_content(self):
        self.f.sandbox.analyze = AsyncMock(return_value={"success": False, "error": "超时"})
        self.f.client.turn = AsyncMock()
        result = await self.f.files.understand("data:application/pdf;base64,eA==", "file", "scan.pdf")
        self.assertIn("未能提取正文", result)
        self.f.client.turn.assert_not_awaited()

    async def test_vision_failure_preserves_ocr_evidence(self):
        self.f.sandbox.analyze = AsyncMock(return_value={"success": True, "pages": [{"page": 1, "text": "重要原文123", "image": "data:image/png;base64,eA=="}]})
        self.f.client.turn = AsyncMock(side_effect=RuntimeError("API暂不可用"))
        result = await self.f.files.understand("data:image/png;base64,eA==")
        self.assertIn("重要原文123", result)
        self.assertIn("视觉转述失败", result)

    async def test_passive_file_is_metadata_only_and_scene_bound(self):
        self.ctx.event["message"] = [{"type": "file", "data": {"name": "table.xlsx", "file_id": "abc", "file_size": 100}}]
        await self.f.observe(self.ctx)
        self.bot.call_action.assert_not_awaited()
        item = self.f.workfiles.list("group:12")[0]
        self.assertEqual(item["name"], "table.xlsx")
        self.assertNotIn("data", item)
        self.ctx.group_id = 13
        with self.assertRaisesRegex(ValueError, "不属于"):
            await self.f.workfiles.download(self.ctx, item["attachment_id"], "table.xlsx", lambda: True)

    async def test_export_send_uses_bytes_current_scene_and_ack(self):
        self.f.sandbox.request = AsyncMock(return_value={"success": True, "data": "aGVsbG8=", "bytes": 5})
        output = await self.f.workfiles.send(self.ctx, "out/result.txt", None, lambda: True)
        self.assertTrue(output["success"])
        args = self.bot.call_action.await_args.args
        self.assertEqual(args[0], "upload_group_file")
        self.assertEqual(args[1], {"group_id": "12", "file": "base64://aGVsbG8=", "name": "result.txt"})
        self.assertIn("发送文件", self.f.context.recent("group:12", 1)[0]["content"])
        self.bot.call_action.reset_mock()
        await self.f.workfiles.send(self.ctx, "out/result.txt", None, lambda: False)
        self.bot.call_action.assert_not_awaited()

    async def test_downloader_blocks_private_targets_and_redirects(self):
        dns = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch("src.feature.ai.media.socket.getaddrinfo", return_value=dns):
            with self.assertRaisesRegex(ValueError, "禁止"):
                await self.f.files._download("http://example.com/secret")
        original = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}))
        with patch("src.feature.ai.media.socket.getaddrinfo", side_effect=[[(2, 1, 6, "", ("8.8.8.8", 80))], dns]), patch("src.feature.ai.media.httpx.AsyncClient", side_effect=lambda **kwargs: original(transport=transport, **kwargs)):
            with self.assertRaisesRegex(ValueError, "禁止"):
                await self.f.files._download("http://example.com/file")

    async def test_download_acknowledged_before_processing(self):
        identifier, _ = self.f.workfiles.register(self.ctx, "file", {"name": "a.txt", "url": "https://example.com/a"})
        async def run(kind, prompt, **kwargs):
            await kwargs['handler']('send_message', {'message': '好的，我下载下来看看能不能处理。'})
            child = await kwargs['spawn']('download-test', '下载附件')
            await child['handler']('download_attachment', {'attachment_id': identifier})
            child['revoke']()
            await child['finish']({'state': 'cancelled'})
            return {'finish_reason': 'completed'}
        async def download(*args):
            self.bot.reply.assert_awaited_once()
            return {'success': True}
        self.f.dsh.run = run
        self.f.workfiles.download = download
        await self.f.engine._turn(self.ctx, 'group:12', True)
        self.assertIn('下载', self.bot.reply.await_args.args[1])

    async def test_arbitrary_commands_require_native_owner_tool(self):
        with self.assertRaises(ValueError):
            await self.f.sandbox.request('run', 'group:12', command='echo hi')

    def test_invalid_paths(self):
        for name in ('../x', '/etc/passwd', 'x/../../y', ''):
            with self.assertRaises(ValueError):
                self.f.sandbox.resolve('group:12', name)
        own = self.f.sandbox.resolve('group:12', 'output/report.txt')
        self.assertTrue(own.is_relative_to(self.f.sandbox.scene_dir('group:12')))
