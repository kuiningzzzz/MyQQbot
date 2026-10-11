import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import WebSocket, WebSocketDisconnect

from src.feature.atom.context import MessageContext
from src.onebot_error import OneBotError

if TYPE_CHECKING:
    from src.feature.manager import FeatureManager
    from src.master import MasterService

logger = logging.getLogger(__name__)


class QQBot:
    def __init__(
        self,
        feature_manager: "FeatureManager",
        master_service: "MasterService",
    ):
        self.connected_clients: set[WebSocket] = set()
        self.feature_manager = feature_manager
        self.master_service = master_service
        self._responses: dict[str, tuple[WebSocket, asyncio.Future]] = {}
        self._self_ids: dict[WebSocket, str] = {}

    def extract_text_message(
        self,
        event: dict[str, Any],
        *,
        normalize: bool = True,
    ) -> str:
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

        message = "".join(text_parts).strip()
        return message.casefold() if normalize else message

    async def handle_websocket(self, websocket: WebSocket):
        logger.info("收到来自 %s 的 WebSocket 连接请求", websocket.client.host)
        queue = asyncio.Queue()

        async def consume():
            while True:
                event = await queue.get()
                try:
                    await self.handle_event(event, websocket)
                except Exception:
                    logger.exception("处理 QQ 事件失败")
                finally:
                    queue.task_done()

        consumer = asyncio.create_task(consume())
        try:
            await websocket.accept()
            logger.info("WebSocket 连接已建立")
            self.connected_clients.add(websocket)

            while True:
                message = await websocket.receive_text()
                logger.debug("收到消息: %s...", message[:100])
                event = json.loads(message)
                if event.get("self_id"):
                    self._self_ids[websocket] = str(event["self_id"])
                echo = str(event.get("echo") or "")
                pending = self._responses.get(echo)
                if pending and pending[0] is websocket:
                    if not pending[1].done():
                        pending[1].set_result(event)
                else:
                    self.feature_manager.observe_arrival(event)
                    queue.put_nowait(event)
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开")
        except Exception:
            logger.exception("处理 WebSocket 时发生错误")
        finally:
            self.connected_clients.discard(websocket)
            for owner, future in list(self._responses.values()):
                if owner is websocket and not future.done():
                    future.set_exception(RuntimeError("NapCat连接已断开"))
            try:
                await asyncio.wait_for(queue.join(), timeout=5)
            except TimeoutError:
                logger.warning("断线时仍有未处理事件，将记录可能的消息丢失")
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self._self_ids.pop(websocket, None)
            await self.feature_manager.observe_system({"kind": "disconnect"})

    async def handle_event(self, event: dict[str, Any], ws: WebSocket):
        if event.get("self_id"):
            self._self_ids[ws] = str(event["self_id"])
        if event.get("post_type") == "notice":
            await self.feature_manager.observe_system({**event, "_bot": self, "_websocket": ws})
            return
        if event.get("post_type") != "message":
            return

        raw_msg = self.extract_text_message(event, normalize=False)
        ctx = MessageContext(
            bot=self,
            event=event,
            websocket=ws,
            msg=raw_msg.casefold(),
            user_id=event.get("user_id"),
            message_type=event.get("message_type"),
            group_id=event.get("group_id"),
            raw_msg=raw_msg,
        )

        logger.info(
            "收到来自 %s 的消息 - 用户 %s: %s",
            ctx.message_type,
            ctx.user_id,
            ctx.msg,
        )

        if await self.master_service.handle(ctx):
            return
        await self.feature_manager.handle_event(ctx)

    async def call_action(self, action: str, params: dict, ws: WebSocket) -> dict:
        if action not in {"get_forward_msg", "get_file", "get_group_file_url", "get_private_file_url", "get_msg", "get_group_member_info", "delete_msg", "send_group_msg", "send_private_msg", "upload_group_file", "upload_private_file", "send_group_forward_msg", "send_private_forward_msg"}:
            raise ValueError("不允许的动作")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._responses[echo] = (ws, future)
        try:
            await ws.send_text(json.dumps({"action": action, "params": params, "echo": echo}))
            result = await asyncio.wait_for(future, timeout=120 if action in {"upload_group_file", "upload_private_file", "send_group_forward_msg", "send_private_forward_msg"} else 20)
            if result.get("status") == "failed" or result.get("retcode", 0) != 0:
                raise OneBotError(action, result)
            return result.get("data") or {}
        finally:
            self._responses.pop(echo, None)

    async def _observe_outgoing(self, scene_type, scene_id, message, ws, message_id=None):
        if isinstance(message, list):
            message = ''.join(str(s.get('data', {}).get('text', '')) if s.get('type') == 'text' else '@' + str(s.get('data', {}).get('qq', '')) if s.get('type') == 'at' else '[' + s.get('type', '消息') + ']' for s in message)
        await self.feature_manager.observe_system({"kind": "outgoing", "scene_type": scene_type, "scene_id": str(scene_id), "content": message, "self_id": self._self_ids.get(ws), "message_id": message_id})

    async def reply(self, ctx: MessageContext, message: str | list[dict[str, Any]]):
        if ctx.message_type == "group":
            await self.send_group_msg(ctx.group_id, message, ctx.websocket)
        else:
            await self.send_private_msg(ctx.user_id, message, ctx.websocket)

    async def send_private_msg(self, user_id: int | None, message: str | list[dict[str, Any]], ws: WebSocket):
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message,
            },
        }
        logger.info("发送私聊消息给用户 %s: %s", user_id, message)
        result = await self.call_action(payload["action"], payload["params"], ws)
        await self._observe_outgoing("private", user_id, message, ws, result.get("message_id"))

    async def send_group_msg(self, group_id: int | None, message: str | list[dict[str, Any]], ws: WebSocket):
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message,
            },
        }
        logger.info("发送群聊消息到群 %s: %s", group_id, message)
        result = await self.call_action(payload["action"], payload["params"], ws)
        await self._observe_outgoing("group", group_id, message, ws, result.get("message_id"))

    async def send_conversation(
        self, conversation_type: str, conversation_id: int, message: str
    ):
        if not self.connected_clients:
            raise RuntimeError("当前没有可用的 NapCat WebSocket 连接")
        websocket = next(iter(self.connected_clients))
        if conversation_type == "group":
            await self.send_group_msg(conversation_id, message, websocket)
        elif conversation_type == "private":
            await self.send_private_msg(conversation_id, message, websocket)
        else:
            raise ValueError("conversation_type must be group or private")

    def can_send_conversation(self) -> bool:
        return bool(self.connected_clients)

    async def send_forward_msg(
        self,
        ctx: MessageContext,
        messages: list[str | list[dict[str, Any]]],
    ):
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

        logger.info("发送合并转发消息，共 %s 条", len(nodes))
        await ctx.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_image_forward_msg(self, ctx: MessageContext, image_refs: list[str]):
        messages = [
            [{"type": "image", "data": {"file": image_ref}}]
            for image_ref in image_refs
        ]
        await self.send_forward_msg(ctx, messages)

    async def send_file(
        self,
        ctx: MessageContext,
        file_path: Path,
        name: str | None = None,
        public_file_root: Path | None = None,
        public_route: str | None = None,
    ):
        file_name = name or file_path.name
        if public_file_root is not None and public_route is not None:
            file_ref = self.resolve_public_file_reference(
                file_path,
                public_file_root,
                public_route,
            )
        else:
            file_ref = str(file_path.resolve())
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

    def resolve_public_file_reference(
        self,
        file_path: Path,
        public_file_root: Path,
        public_route: str,
    ) -> str:
        public_base_url = os.getenv("MYQQBOT_PUBLIC_BASE_URL")
        if not public_base_url:
            return str(file_path.resolve())

        public_file_root = public_file_root.resolve()
        resolved_path = file_path.resolve()
        try:
            relative_path = resolved_path.relative_to(public_file_root)
        except ValueError:
            relative_path = Path(file_path.name)

        encoded_path = "/".join(quote(part, safe="") for part in relative_path.parts)
        return f"{public_base_url.rstrip('/')}/{public_route.strip('/')}/{encoded_path}"
