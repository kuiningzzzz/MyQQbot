import asyncio
import logging
import re
from pathlib import Path

from src.feature.atom import Feature, MessageContext
from src.feature.summary.client import OpenAICompatibleClient, SummaryApiError
from src.feature.summary.config import SummaryConfig
from src.feature.summary.history import GroupHistoryStore, message_from_context

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是群聊记录总结助手。你的职责是准确、客观地概括提供的聊天记录：
1. 尽量将总结控制在150个汉字以内；
2. 提炼主要话题、结论、争议和待办事项，不罗列无关寒暄；
3. 根据昵称和用户ID区分参与者，仅在有助于理解时指出人物观点或行动；
4. 不虚构记录中没有的信息，不把图片占位描述成已识别的图片内容；
5. 输出内容时应该输出为：群友们刚刚在聊：（总结的内容）。"""


class SummaryFeature(Feature):
    name = "summary"
    title = "AI 群聊总结"
    description = "使用 /short 10-200 总结当前群聊最近指定数量的消息。"
    default_enabled = True
    command_pattern = re.compile(r"^/short\s+(\d+)\s*$")

    def __init__(
        self,
        config: SummaryConfig | None = None,
        history: GroupHistoryStore | None = None,
        client: OpenAICompatibleClient | None = None,
    ):
        self.config = config or SummaryConfig(Path("data/summary/config.json"))
        self.history = history or GroupHistoryStore(Path("data/summary/history"))
        self.client = client or OpenAICompatibleClient()
        self._group_locks: dict[int, asyncio.Lock] = {}

    async def observe(self, ctx: MessageContext) -> None:
        if ctx.message_type != "group" or ctx.group_id is None:
            return
        if ctx.msg.startswith("/short"):
            return
        message = message_from_context(ctx)
        if message is not None:
            self.history.append(ctx.group_id, message)

    async def handle(self, ctx: MessageContext) -> bool:
        if not ctx.msg.startswith("/short"):
            return False
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
            history_text = "\n".join(message.as_prompt_line() for message in messages)
            try:
                result = await asyncio.to_thread(
                    self.client.summarize,
                    self.config.load(),
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
    def config_status(self) -> dict[str, str | bool]:
        config = self.config.load()
        return {
            "api_url": config.api_url,
            "model": config.model,
            "api_key_configured": bool(config.api_key),
        }
