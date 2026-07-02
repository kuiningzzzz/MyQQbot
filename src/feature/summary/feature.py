import asyncio
import logging
import re
from pathlib import Path

from src.feature.atom import Feature, MessageContext
from src.feature.summary.character import Character, CharacterSelectionStore, CharacterStore
from src.feature.summary.chat import AiChatService
from src.feature.summary.client import OpenAICompatibleClient, SummaryApiError
from src.feature.summary.config import SummaryConfig
from src.feature.summary.history import (
    GroupHistoryStore,
    conversation_key,
    format_prompt_history,
    message_from_context,
)
from src.feature.summary.memory_service import MemoryService
from src.feature.summary.memory_store import MemoryStore
from src.master import MasterService
from src.feature.web_search import WebSearchConfig, WebSearchService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是群聊记录总结助手。你的职责是准确、客观地概括提供的聊天记录：
1. 尽量将总结控制在150个汉字以内；
2. 提炼主要话题、结论、争议和待办事项，不罗列无关寒暄；
3. 根据昵称和用户ID区分参与者，仅在有助于理解时指出人物观点或行动；
4. 不虚构记录中没有的信息，不把图片占位描述成已识别的图片内容；
5. 输出内容时应该输出为：群友们刚刚在聊：（总结的内容）。"""

ADD_CHARACTER_PATTERN = re.compile(r"^/?add-character(?:\s|$)")
DELETE_CHARACTER_PATTERN = re.compile(r"^/?del-character(?:\s|$)")


class SummaryFeature(Feature):
    name = "summary"
    title = "AI 功能"
    description = "共用模型配置的群聊总结与多角色 AI 聊天。"
    default_enabled = True
    command_pattern = re.compile(r"^/short\s+(\d+)\s*$")

    def __init__(
        self,
        master_service: MasterService,
        config: SummaryConfig | None = None,
        history: GroupHistoryStore | None = None,
        client: OpenAICompatibleClient | None = None,
        characters: CharacterStore | None = None,
        selections: CharacterSelectionStore | None = None,
        memory_store: MemoryStore | None = None,
        memory_service: MemoryService | None = None,
        web_search: WebSearchService | None = None,
    ):
        self.master_service = master_service
        self.config = config or SummaryConfig(Path("data/summary/config.json"))
        self.history = history or GroupHistoryStore(Path("data/summary/history"))
        self.client = client or OpenAICompatibleClient()
        self.characters = characters or CharacterStore(Path("data/summary/characters.json"))
        self.characters.list()
        self.selections = selections or CharacterSelectionStore(
            Path("data/summary/selections.json")
        )
        self.memory_store = memory_store or MemoryStore(
            Path("data/summary/memory.db")
        )
        self.memory = memory_service or MemoryService(
            self.memory_store,
            self.config,
            self.client,
            self.master_service,
        )
        self.web_search = web_search or WebSearchService(
            WebSearchConfig(Path("data/web_search/config.json"))
        )
        self.chat = AiChatService(
            self.config,
            self.history,
            self.characters,
            self.selections,
            self.client,
            self.memory,
            self.web_search,
        )
        self._group_locks: dict[int, asyncio.Lock] = {}

    async def observe(self, ctx: MessageContext) -> None:
        key = conversation_key(ctx)
        if key is None:
            return
        if (
            ctx.msg.startswith(("/short", "/character", "/change-character"))
            or ADD_CHARACTER_PATTERN.match(ctx.msg)
            or DELETE_CHARACTER_PATTERN.match(ctx.msg)
            or self.memory.is_command(ctx)
        ):
            return
        message = message_from_context(ctx)
        if message is not None:
            self_id = ctx.event.get("self_id")
            if (
                self_id is not None
                and ctx.user_id is not None
                and str(self_id) == str(ctx.user_id)
            ):
                recent = self.history.load_conversation(key, 1)
                if recent:
                    previous = recent[-1]
                    if (
                        previous.user_id == message.user_id
                        and previous.content == message.content
                        and abs(previous.timestamp - message.timestamp) <= 10
                    ):
                        return
            if ctx.message_type == "group" and ctx.group_id is not None:
                self.history.append(ctx.group_id, message)
            elif ctx.user_id is not None:
                self.history.append_private(ctx.user_id, message)
            self_id = ctx.event.get("self_id")
            is_self = (
                self_id is not None
                and ctx.user_id is not None
                and str(self_id) == str(ctx.user_id)
            )
            if not ctx.msg.startswith("/"):
                self.memory.observe(ctx, message, is_self=is_self)

    async def handle(self, ctx: MessageContext) -> bool:
        if self.memory.is_command(ctx):
            return await self.memory.handle_command(ctx)
        if ADD_CHARACTER_PATTERN.match(ctx.msg):
            return await self._handle_add_character(ctx)
        if DELETE_CHARACTER_PATTERN.match(ctx.msg):
            return await self._handle_delete_character(ctx)

        settings = self.config.load()
        if ctx.msg.startswith("/short"):
            if not settings.summary_enabled:
                await ctx.bot.reply(ctx, "AI 群聊总结当前已关闭。")
                return True
            return await self._handle_summary(ctx)
        is_chat_command = ctx.msg == "/character" or ctx.msg.startswith(
            "/change-character"
        )
        if not settings.chat_enabled:
            if is_chat_command:
                await ctx.bot.reply(ctx, "AI 聊天当前已关闭。")
                return True
            return False
        return await self.chat.handle(ctx)

    async def _handle_add_character(self, ctx: MessageContext) -> bool:
        if not self.master_service.is_master(ctx.user_id):
            await ctx.bot.reply(ctx, "只有机器人主人可以添加 AI 人设。")
            return True
        raw_command = ctx.raw_msg or ctx.msg
        parts = raw_command.split(maxsplit=4)
        if len(parts) != 5:
            await ctx.bot.reply(
                ctx,
                "格式错误，请发送：/add-character 名字 触发词 描述 系统提示词正文",
            )
            return True
        _, name, trigger_name, description, prompt = parts
        try:
            character = self.characters.save(
                Character(name, trigger_name, description, prompt)
            )
        except ValueError as exc:
            await ctx.bot.reply(ctx, str(exc))
            return True
        await ctx.bot.reply(ctx, f"AI 人设“{character.name}”已添加。")
        return True

    async def _handle_delete_character(self, ctx: MessageContext) -> bool:
        if not self.master_service.is_master(ctx.user_id):
            await ctx.bot.reply(ctx, "只有机器人主人可以删除 AI 人设。")
            return True
        raw_command = ctx.raw_msg or ctx.msg
        parts = raw_command.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await ctx.bot.reply(ctx, "格式错误，请发送：/del-character 名字")
            return True
        name = parts[1].strip()
        character = self.characters.get(name)
        if character is None:
            await ctx.bot.reply(ctx, f"没有找到 AI 人设“{name}”。")
            return True
        self.characters.delete(character.name)
        self.selections.remove(character.name)
        await ctx.bot.reply(ctx, f"AI 人设“{character.name}”已删除。")
        return True

    async def _handle_summary(self, ctx: MessageContext) -> bool:
        if ctx.message_type != "group" or ctx.group_id is None:
            await ctx.bot.reply(ctx, "群聊总结只能在群聊中使用。")
            return True
        match = self.command_pattern.fullmatch(ctx.msg)
        if match is None or not 10 <= int(match.group(1)) <= 200:
            await ctx.bot.reply(ctx, "请输入 /short 10至200的整数，例如：/short 50")
            return True
        count = int(match.group(1))
        messages = self.history.load(ctx.group_id, count)
        if not messages:
            await ctx.bot.reply(ctx, "当前群聊还没有可供总结的历史消息。")
            return True

        lock = self._group_locks.setdefault(ctx.group_id, asyncio.Lock())
        if lock.locked():
            await ctx.bot.reply(ctx, "这个群的总结任务正在处理中，请稍后再试。")
            return True
        async with lock:
            await ctx.bot.reply(ctx, f"正在总结最近 {len(messages)} 条群聊消息...")
            config = self.config.load()
            history_text = format_prompt_history(
                messages,
                config.context_token_limit,
            )
            try:
                result = await asyncio.to_thread(
                    self.client.summarize,
                    config,
                    SYSTEM_PROMPT,
                    history_text,
                )
            except SummaryApiError as exc:
                logger.exception("群 %s 的 AI 总结失败", ctx.group_id)
                await ctx.bot.reply(ctx, str(exc))
                return True
            await ctx.bot.reply(ctx, result)
        return True

    @property
    def config_status(self) -> dict[str, str | bool | int]:
        config = self.config.load()
        return {
            "api_url": config.api_url,
            "model": config.model,
            "api_key_configured": bool(config.api_key),
            "summary_enabled": config.summary_enabled,
            "chat_enabled": config.chat_enabled,
            "chat_window_min": config.chat_window_min,
            "chat_window_max": config.chat_window_max,
            "thinking_mode": config.thinking_mode,
            "reasoning_effort": config.reasoning_effort,
            "context_token_limit": config.context_token_limit,
            "max_tokens": config.max_tokens,
            "memory_enabled": config.memory_enabled,
            "memory_auto_learn": config.memory_auto_learn,
            "memory_batch_size": config.memory_batch_size,
            "memory_retrieval_limit": config.memory_retrieval_limit,
            "memory_min_confidence": config.memory_min_confidence,
        }
