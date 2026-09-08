import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot import QQBot
from src.onebot_error import OneBotError
from src.feature.ai.feature import AiFeature
from src.feature.ai.recall import recall_message
from src.feature.atom.context import MessageContext


class RecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({"bot_qq": "999", "vector_index_enabled": False, "memory_auto_learn": False})
        self.action = AsyncMock(return_value={})
        self.ctx = MessageContext(bot=SimpleNamespace(call_action=self.action), event={"self_id": 999}, websocket=None, msg="撤回", message_type="group", user_id=123, group_id=12)
        await self.f.record("group", "12", "123", "甲", "误发", message_id="101")
        await self.f.record("group", "12", "999", "机器人", "回答", message_id="102", outgoing=True)

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_self_request_and_bot_message(self):
        result = await recall_message(self.f, self.ctx, "101", "发送者本人申请")
        self.assertTrue(result["requested_by_sender"])
        self.assertTrue(result["success"])
        self.assertTrue(self.f.context.recent("group:12", 2)[0]["recalled"])
        duplicate = await recall_message(self.f, self.ctx, "101", "重复")
        self.assertEqual(duplicate["code"], "already_recalled")
        result = await recall_message(self.f, self.ctx, "102", "机器人纠正误发")
        self.assertTrue(result["success"])
        self.assertEqual(self.action.await_count, 2)
        self.assertIn('"id": "102"', await self.f.context.for_prompt())

    async def test_other_request_is_not_restricted_to_admin_requesters(self):
        self.ctx.user_id = 456
        result = await recall_message(self.f, self.ctx, "101", "根据上下文判断内容含隐私，批准撤回")
        self.assertTrue(result["success"])
        self.assertFalse(result["requested_by_sender"])

    async def test_permission_errors_and_generic_role_failure(self):
        for responses in ([OneBotError("delete_msg", {"retcode": 1200, "message": "权限不足"})], [OneBotError("delete_msg", {"retcode": 1200, "message": "失败"}), {"role": "member"}], [OneBotError("delete_msg", {"retcode": 1200}), {"role": "admin"}, {"role": "owner"}]):
            self.action.side_effect = responses
            result = await recall_message(self.f, self.ctx, "101", "本人申请")
            self.assertEqual(result["code"], "permission_denied")
            self.assertTrue(result["permission_denied"])
            self.assertIn("权限不足", result["message"])
            self.assertEqual(result["retcode"], 1200)

    async def test_expired_and_timeout_are_not_permission_errors(self):
        self.action.side_effect = [OneBotError("delete_msg", {"retcode": 1200, "wording": "消息已过期"})]
        result = await recall_message(self.f, self.ctx, "102", "纠正")
        self.assertEqual(result["code"], "recall_failed")
        self.assertFalse(result["permission_denied"])
        self.assertEqual(result["detail"], "消息已过期")
        self.action.side_effect = TimeoutError()
        result = await recall_message(self.f, self.ctx, "102", "纠正")
        self.assertEqual(result["code"], "unknown")

    async def test_scope_and_stale_guards(self):
        await self.f.record("group", "34", "123", "甲", "其他群", message_id="103")
        self.assertEqual((await recall_message(self.f, self.ctx, "103", "请求"))["code"], "unknown_message")
        self.assertEqual((await recall_message(self.f, self.ctx, "101", "请求", lambda: False))["code"], "cancelled")
        self.action.assert_not_awaited()

    async def test_reply_lookup_and_private_permissions(self):
        self.ctx.event["message"] = [{"type": "reply", "data": {"id": "104"}}]
        self.action.return_value = {"group_id": 12, "sender": {"user_id": 456}, "message": "引用原文"}
        result = await recall_message(self.f, self.ctx, "104", "查看", inspect=True)
        self.assertEqual(result["sender_qq"], "456")
        self.assertEqual(result["content"], "引用原文")
        self.assertEqual(self.action.await_args.args[0], "get_msg")
        self.action.return_value = {"group_id": 34, "sender": {"user_id": 456}}
        self.assertEqual((await recall_message(self.f, self.ctx, "104", "请求"))["code"], "scene_mismatch")
        self.ctx.message_type = "private"
        await self.f.record("private", "123", "123", "甲", "私聊", message_id="105")
        self.assertEqual((await recall_message(self.f, self.ctx, "105", "本人申请"))["code"], "permission_denied")

    async def test_tool_failure_is_returned_to_agent(self):
        responses = []
        async def turn(settings, messages, tools):
            responses.append(json.loads(json.dumps(messages)))
            if len(responses) == 1:
                self.assertIn("非发送者请求", messages[0]["content"])
                return {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "recall_message", "arguments": '{"message_id":"101","reason":"本人申请"}'}}]}
            return {"role": "assistant", "content": ""}
        self.f.client.turn = turn
        self.action.side_effect = OneBotError("delete_msg", {"retcode": 1200, "message": "权限不足"})
        self.f.engine.revisions["group:12"] = 1
        await self.f.engine._turn(self.ctx, "group:12", False, 1)
        output = json.loads(responses[1][-1]["content"])
        self.assertEqual(output["code"], "permission_denied")

    async def test_outgoing_ack_records_real_message_id_and_errors(self):
        manager = SimpleNamespace(observe_system=AsyncMock())
        bot = QQBot(manager, None)
        class Socket:
            response = {"status": "ok", "retcode": 0, "data": {"message_id": 202}}
            async def send_text(self, text):
                payload = json.loads(text)
                bot._responses[payload["echo"]][1].set_result(self.response)
        socket = Socket()
        bot._self_ids[socket] = "999"
        await bot.send_group_msg(12, "hello", socket)
        self.assertEqual(manager.observe_system.await_args.args[0]["message_id"], 202)
        socket.response = {"status": "failed", "retcode": 1200, "wording": "权限不足"}
        with self.assertRaises(OneBotError) as caught:
            await bot.call_action("delete_msg", {"message_id": "202"}, socket)
        self.assertTrue(caught.exception.permission_denied)
        self.assertEqual(bot._responses, {})
