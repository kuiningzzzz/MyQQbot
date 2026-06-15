import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from src.feature.manager import FeatureManager

logger = logging.getLogger(__name__)


@dataclass
class MessageContext:
    bot: "QQBot"
    event: dict[str, Any]
    websocket: WebSocket
    msg: str
    message_type: str | None
    user_id: int | None
    group_id: int | None


class QQBot:
    def __init__(self, feature_manager: "FeatureManager"):
        self.connected_clients: set[WebSocket] = set()
        self.feature_manager = feature_manager

    def extract_text_message(self, event: dict[str, Any]) -> str:
        """Extract plain text from NapCat's message=array format."""
        message_segments = event.get("message", [])
        if not isinstance(message_segments, list):
            return ""

        text_parts: list[str] = []
        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") != "text":
                continue
            data = seg.get("data") or {}
            text_parts.append(str(data.get("text", "")))

        return "".join(text_parts).strip().lower()

    async def handle_websocket(self, websocket: WebSocket):
        logger.info("收到来自 %s 的 WebSocket 连接请求", websocket.client.host)
        try:
            await websocket.accept()
            logger.info("WebSocket 连接已建立")
            self.connected_clients.add(websocket)

            while True:
                message = await websocket.receive_text()
                logger.debug("收到消息: %s...", message[:100])
                event = json.loads(message)
                await self.handle_event(event, websocket)
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开")
        except Exception:
            logger.exception("处理 WebSocket 时发生错误")
        finally:
            self.connected_clients.discard(websocket)

    async def handle_event(self, event: dict[str, Any], ws: WebSocket):
        if event.get("post_type") != "message":
            return

        ctx = MessageContext(
            bot=self,
            event=event,
            websocket=ws,
            msg=self.extract_text_message(event),
            user_id=event.get("user_id"),
            message_type=event.get("message_type"),
            group_id=event.get("group_id"),
        )

        logger.info(
            "收到来自 %s 的消息 - 用户 %s: %s",
            ctx.message_type,
            ctx.user_id,
            ctx.msg,
        )

        handled = await self.feature_manager.handle_event(ctx)
        if handled:
            return

        if ctx.msg == "hello":
            await self.reply(ctx, "hello")

    async def reply(self, ctx: MessageContext, message: str):
        if ctx.message_type == "group":
            await self.send_group_msg(ctx.group_id, message, ctx.websocket)
        else:
            await self.send_private_msg(ctx.user_id, message, ctx.websocket)

    async def send_private_msg(self, user_id: int | None, message: str, ws: WebSocket):
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message,
            },
        }
        logger.info("发送私聊消息给用户 %s: %s", user_id, message)
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_group_msg(self, group_id: int | None, message: str, ws: WebSocket):
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message,
            },
        }
        logger.info("发送群聊消息到群 %s: %s", group_id, message)
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_forward_msg(self, ctx: MessageContext, messages: list[str]):
        nodes = [
            {
                "type": "node",
                "data": {
                    "name": "MyQQbot",
                    "uin": str(ctx.event.get("self_id") or ctx.user_id or 0),
                    "content": message,
                },
            }
            for message in messages
        ]

        if ctx.message_type == "group":
            payload = {
                "action": "send_group_forward_msg",
                "params": {
                    "group_id": ctx.group_id,
                    "messages": nodes,
                },
            }
        else:
            payload = {
                "action": "send_private_forward_msg",
                "params": {
                    "user_id": ctx.user_id,
                    "messages": nodes,
                },
            }

        logger.info("发送帮助聊天记录，共 %s 条", len(nodes))
        await ctx.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_forward_file_msg(
        self, ctx: MessageContext, file_path: Path, name: str | None = None
    ):
        file_name = name or file_path.name
        file_ref = self.resolve_file_reference(file_path)
        nodes = [
            {
                "type": "node",
                "data": {
                    "name": "MyQQbot",
                    "uin": str(ctx.event.get("self_id") or ctx.user_id or 0),
                    "content": [
                        {
                            "type": "file",
                            "data": {
                                "file": file_ref,
                                "name": file_name,
                            },
                        }
                    ],
                },
            }
        ]

        if ctx.message_type == "group":
            payload = {
                "action": "send_group_forward_msg",
                "params": {
                    "group_id": ctx.group_id,
                    "messages": nodes,
                },
            }
        else:
            payload = {
                "action": "send_private_forward_msg",
                "params": {
                    "user_id": ctx.user_id,
                    "messages": nodes,
                },
            }

        logger.info("发送文件聊天记录 %s", file_path)
        await ctx.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_file(self, ctx: MessageContext, file_path: Path, name: str | None = None):
        file_name = name or file_path.name
        file_ref = self.resolve_file_reference(file_path)
        if ctx.message_type == "group":
            payload = {
                "action": "upload_group_file",
                "params": {
                    "group_id": ctx.group_id,
                    "file": file_ref,
                    "name": file_name,
                },
            }
        else:
            payload = {
                "action": "upload_private_file",
                "params": {
                    "user_id": ctx.user_id,
                    "file": file_ref,
                    "name": file_name,
                },
            }

        logger.info("发送文件 %s", file_path)
        await ctx.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    def resolve_file_reference(self, file_path: Path) -> str:
        public_base_url = os.getenv("MYQQBOT_PUBLIC_BASE_URL")
        if not public_base_url:
            return str(file_path.resolve())

        return f"{public_base_url.rstrip('/')}/files/{quote(file_path.name, safe='')}"
