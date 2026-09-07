from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.feature.atom import Feature, MessageContext
from src.feature.web_search import WebSearchConfig, WebSearchService

from .agents import AttentionAgent, Identity, IntegrationAgent
from .client import ModelClient
from .config import Config
from .context import ContextManager
from .engine import ChatEngine
from .index import VectorIndex
from .media import FileAgent
from .runtime import MemoryRuntime
from .storage import Workspace

logger = logging.getLogger(__name__)


class AiFeature(Feature):
    name = "ai"
    title = "AI 服务"
    description = "单一人格、跨场景上下文、Attention 检索与文件记忆整合。"

    def __init__(self, root: Path = Path("data/ai"), client=None, index=None):
        self.workspace = Workspace(root)
        self.config = Config(root)
        self.client = client or ModelClient()
        self.identity = Identity(self.config, self.workspace)
        self.index = index or VectorIndex(self.workspace, self.config)
        self.attention = AttentionAgent(self.config, self.client, self.index, self.identity)
        self.integration = IntegrationAgent(self.workspace, self.config, self.client, self.index, self.identity)
        self.context = ContextManager(self.workspace, self.config, self.client, self.identity.prompt)
        self.attention.context_provider = self.context.prompt_snapshot
        self.files = FileAgent(self.config, self.client, self.identity)
        self.runtime = MemoryRuntime(self.workspace, self.config, self.integration)
        self.search_config = WebSearchConfig(root.parent / "web_search/config.json")
        self.search = WebSearchService(self.search_config)
        self.engine = ChatEngine(self)
        self.enabled = True
        self._record_lock = asyncio.Lock()
        self._sent = []
        self._message_ids = set()
        self._summary_locks = {}

    def on_enabled_changed(self, enabled: bool) -> None:
        self.enabled = enabled
        self.runtime.set_enabled(enabled)
        if not enabled:
            self.engine.pending.clear()
            self.engine.active.clear()
            for key in self.engine.revisions:
                self.engine.revisions[key] += 1

    @staticmethod
    def scene_key(ctx) -> str:
        return f"group:{ctx.group_id}" if ctx.message_type == "group" else f"private:{ctx.user_id}"

    def observe_arrival(self, event: dict) -> None:
        self.identity.observe(event.get("self_id"))
        if self.enabled and self.config.load().chat_enabled:
            self.engine.note_arrival(event)

    async def record(self, scene_type: str, scene_id: str, qq: str, nickname: str, content: str, *, message_type="text", file_data=None, filename="", timestamp=None, message_id=None, outgoing=False) -> dict:
        if scene_type not in {"group", "private"} or not str(scene_id).isdigit() or not str(qq).isdigit():
            raise ValueError("场景和QQ号无效")
        settings = self.config.load()
        is_self = str(qq) in self.identity.ids or outgoing
        if message_type in {"image", "file"}:
            try:
                description = await self.files.understand(str(file_data or ""), message_type, filename)
                content += f"[{'图片' if message_type == 'image' else '文件'}: {description}]"
            except Exception as exc:
                logger.warning("文件理解失败：%s", exc)
                content += f"[{'图片' if message_type == 'image' else '文件'}: 无法解析，{str(exc)}]"
        now = datetime.now(ZoneInfo(settings.timezone))
        record = {"id": str(message_id or uuid.uuid4()), "scene_type": scene_type, "scene_id": str(scene_id), "qq": str(qq), "nickname": nickname, "content": content, "is_self": is_self, "timestamp": timestamp or now.isoformat()}
        async with self._record_lock:
            identity = (scene_type, str(scene_id), record["id"])
            if message_id and identity in self._message_ids:
                return {"success": True, "duplicate": True}
            clock = time.monotonic()
            self._sent = [item for item in self._sent if clock - item[0] < 10]
            if is_self and not outgoing:
                match = next((item for item in self._sent if item[1:] == (scene_type, str(scene_id), content)), None)
                if match:
                    self._sent.remove(match)
                    return {"success": True, "duplicate": True}
            result = await asyncio.to_thread(self.workspace.append_raw, record, settings.timezone)
            record["raw_line"] = result["raw_line"]
            await self.context.append(record)
            if message_id:
                self._message_ids.add(identity)
                if len(self._message_ids) > 10000:
                    self._message_ids = {identity}
            if outgoing:
                self._sent.append((clock, scene_type, str(scene_id), content))
        return {key: value for key, value in result.items() if key != "raw_line"}

    async def observe(self, ctx: MessageContext):
        self.identity.observe(ctx.event.get("self_id"))
        if ctx.message_type not in {"group", "private"}:
            return
        # Never store owner binding credentials or retired explicit-memory commands.
        if re.match(r"^/(set-master|remember|short)(?:\s|$)", ctx.msg):
            return
        segments = ctx.event.get("message") or []
        content = await self._render(segments, ctx)
        if not content.strip():
            return
        sender = ctx.event.get("sender") or {}
        timestamp = None
        if ctx.event.get("time"):
            timestamp = datetime.fromtimestamp(int(ctx.event["time"]), ZoneInfo(self.config.load().timezone)).isoformat()
        await self.record(ctx.message_type, str(ctx.group_id if ctx.message_type == "group" else ctx.user_id), str(ctx.user_id), str(sender.get("card") or sender.get("nickname") or ctx.user_id), content, timestamp=timestamp, message_id=ctx.event.get("message_id"))

    async def _render(self, segments, ctx, depth=0) -> str:
        if isinstance(segments, str):
            return segments
        if not isinstance(segments, list) or depth > 8:
            return "[转发内容层级过深或格式无效]"
        result = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            kind = segment.get("type")
            if kind == "text":
                result.append(str(data.get("text") or ""))
            elif kind == "at":
                result.append(f"@QQ:{data.get('qq', '')}")
            elif kind in {"image", "file"}:
                try:
                    url = data.get("url") or data.get("file") or ""
                    filename = str(data.get("name") or data.get("file_name") or data.get("file") or "")
                    if kind == "file" and data.get("file_id") and ctx.group_id and not str(url).startswith(("http://", "https://", "data:")):
                        try:
                            reply = await ctx.bot.call_action("get_group_file_url", {"group_id": str(ctx.group_id), "file_id": data["file_id"]}, ctx.websocket)
                            url = reply.get("url") or ""
                        except Exception:
                            logger.debug("群文件URL不可用，尝试get_file", exc_info=True)
                    if not str(url).startswith(("http://", "https://", "data:")) and kind == "file" and data.get("file_id"):
                        reply = await ctx.bot.call_action("get_file", {"file_id": data["file_id"]}, ctx.websocket)
                        url = reply.get("url") or reply.get("file") or ""
                        filename = str(reply.get("file_name") or filename)
                        if not str(url).startswith(("http://", "https://", "data:")) and reply.get("base64"):
                            url = "data:application/octet-stream;base64," + reply["base64"]
                    description = await self.files.understand(str(url), kind, filename)
                    result.append(f"[{'图片' if kind == 'image' else '文件'}: {description}]")
                except Exception as exc:
                    logger.warning("QQ附件解析失败：%s", exc)
                    result.append(f"[{'图片' if kind == 'image' else '文件'}: 未能解析]")
            elif kind in {"forward", "node"}:
                children = data.get("content") or data.get("messages")
                if children is None and data.get("id"):
                    try:
                        reply = await ctx.bot.call_action("get_forward_msg", {"message_id": data["id"]}, ctx.websocket)
                        children = reply.get("messages", [])
                    except Exception:
                        logger.exception("获取合并转发失败")
                if kind == "node":
                    rendered = await self._render(children or [], ctx, depth + 1)
                    result.append(f"[转发 QQ:{data.get('uin', '?')} 昵称:{data.get('name', '?')}] {rendered}")
                else:
                    result.append("[转发聊天记录]")
                    for child in children or []:
                        if not isinstance(child, dict):
                            continue
                        if child.get("type") == "node":
                            result.append("  " + await self._render([child], ctx, depth + 1))
                        else:
                            sender = child.get("sender") or {}
                            rendered = await self._render(child.get("message") or child.get("content") or [], ctx, depth + 1)
                            result.append(f"  [转发 QQ:{sender.get('user_id', '?')} 昵称:{sender.get('nickname', '?')}] {rendered}")
            else:
                result.append(f"[{kind or '未知消息'}]")
        return "\n".join(result)

    async def observe_system(self, event: dict):
        kind = event.get("kind")
        if event.get("notice_type") == "group_upload" and event.get("_bot"):
            attachment = event.get("file") or {}
            message = {**event, "message_type": "group", "message": [{"type": "file", "data": {**attachment, "file_id": attachment.get("id")}}]}
            ctx = MessageContext(bot=event["_bot"], websocket=event.get("_websocket"), event=message, msg="", message_type="group", user_id=event.get("user_id"), group_id=event.get("group_id"))
            await self.observe(ctx)
        elif kind == "outgoing":
            qq = str(event.get("self_id") or self.config.load().bot_qq or "0")
            self.identity.observe(qq)
            await self.record(event["scene_type"], str(event["scene_id"]), qq, self.config.load().nickname, event["content"], outgoing=True)
        elif kind == "disconnect":
            await asyncio.to_thread(self.workspace.append_system, "连接中断 | 消息丢失（断线期间可能有未接收消息）", self.config.load().timezone)
        elif event.get("notice_type") in {"group_recall", "friend_recall"}:
            original = "未在当前上下文找到原消息"
            target = f"group:{event['group_id']}" if event.get("group_id") else f"private:{event.get('user_id')}"
            for key, scene in self.context.snapshot().items():
                if key != target:
                    continue
                for record in scene["recent_messages"]:
                    if record["id"] == str(event.get("message_id")):
                        original = record["content"]
            await asyncio.to_thread(self.workspace.append_system, f"消息撤回 | 原消息:{original} | 消息ID:{event.get('message_id')} | 撤回者:{event.get('operator_id') or event.get('user_id')} | 群号:{event.get('group_id', '')}", self.config.load().timezone)

    async def handle(self, ctx):
        if ctx.msg.startswith("/short"):
            if not self.config.load().summary_enabled:
                await ctx.bot.reply(ctx, "群聊总结已关闭。")
                return True
            match = re.fullmatch(r"/short\s+(\d+)", ctx.msg)
            if ctx.message_type != "group" or not match or not 10 <= int(match[1]) <= 200:
                await ctx.bot.reply(ctx, "请在群聊发送 /short 10至200的整数")
                return True
            key = self.scene_key(ctx)
            lock = self._summary_locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                await ctx.bot.reply(ctx, "当前群正在总结，请稍候。")
                return True
            async with lock:
                records = [record for record in self.context.recent(key, int(match[1]) + 1) if not record["content"].startswith("/short")][-int(match[1]):]
                result = await self.client.complete(self.config.load(), self.identity.prompt() + "\n你是群聊总结助手，概括主要话题、结论、争议和待办，尽量150字以内，不虚构。", str(records))
                await ctx.bot.reply(ctx, result)
            return True
        if self.config.load().chat_enabled:
            return await self.engine.handle(ctx)
        return False

    async def start(self, bot):
        await self.runtime.start()
        ok, error = await asyncio.to_thread(self.index.preload)
        if ok:
            logger.info("AI 向量索引就绪：embedding 模型已加载")
        else:
            logger.warning("AI 向量索引预加载失败：%s（记忆检索已降级为纯文本检索，每 %s 秒自动重试）", error, self.index.MODEL_RETRY_SECONDS)

    async def stop(self):
        await self.engine.stop()
        await self.runtime.stop()
