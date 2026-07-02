import json
import logging
from pathlib import Path

from src.feature.atom import Feature, MessageContext, read_json, write_json
from src.master import MasterService

logger = logging.getLogger(__name__)


DEFAULT_PUBLIC_HELP_MESSAGES = [
    "/help - 查看当前支持的指令",
    "/set-master 鉴别码 - 将当前 QQ 用户绑定为机器人主人",
    "/jm 号码 - 下载对应 JM 漫画，导出 PDF 并发送到当前会话\n/jm status - 查看 JM 当前处理队列",
    "/pixiv 作品ID - 获取对应 Pixiv 图片或图片集\n/pixiv status - 查看 Pixiv 当前处理队列",
    "/short 10至200 - AI 总结当前群聊最近指定数量的消息（暂时不支持图片和语音等多模态信息的解析读取）",
    "/character - 查看可用的 AI 聊天人设\n/change-character 名字 - 切换当前会话的 AI 人设",
    "/open-news - 为当前群聊或私聊开启每日新闻播报\n/close-news - 关闭当前会话的每日新闻播报",
    "/hello - 回复 hello",
]

DEFAULT_MASTER_HELP_MESSAGES = [
    *DEFAULT_PUBLIC_HELP_MESSAGES[:-3],
    DEFAULT_PUBLIC_HELP_MESSAGES[-3]
    + "\n/add-character 名字 触发词 描述 系统提示词正文 - （主人权限）添加 AI 人设"
    + "\n/del-character 名字 - （主人权限）删除 AI 人设"
    + "\n/remember [--global] 内容 - （主人权限）显式添加长期记忆"
    + "\n/memory [关键词] - （主人权限）查看当前范围的长期记忆"
    + "\n/forget 记忆ID或关键词 - （主人权限）遗忘长期记忆",
    DEFAULT_PUBLIC_HELP_MESSAGES[-2] + "\n/news - （主人权限）立即联网生成一次新闻播报",
    *DEFAULT_PUBLIC_HELP_MESSAGES[-1:],
]


class HelpFeature(Feature):
    name = "help"
    title = "帮助菜单"
    description = "收到 /help 后，以 QQ 聊天记录形式发送当前支持的指令合集。"
    default_enabled = True

    def __init__(self, state_path: Path, master_service: MasterService):
        self.state_path = state_path
        self.master_service = master_service
        self.public_messages, self.master_messages = self.load_messages()

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg != "/help":
            return False

        messages = (
            self.master_messages
            if self.master_service.is_master(ctx.user_id)
            else self.public_messages
        )
        await ctx.bot.send_forward_msg(ctx, messages)
        return True

    def load_messages(self) -> tuple[list[str], list[str]]:
        if not self.state_path.exists():
            self.save_messages(DEFAULT_PUBLIC_HELP_MESSAGES, DEFAULT_MASTER_HELP_MESSAGES)
            return self.public_messages.copy(), self.master_messages.copy()

        try:
            data = read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 help 配置失败，将使用默认帮助文本")
            return DEFAULT_PUBLIC_HELP_MESSAGES.copy(), DEFAULT_MASTER_HELP_MESSAGES.copy()

        if not isinstance(data, dict):
            return DEFAULT_PUBLIC_HELP_MESSAGES.copy(), DEFAULT_MASTER_HELP_MESSAGES.copy()
        public_messages = self._normalize(data.get("public_messages"))
        master_messages = self._normalize(data.get("master_messages"))
        if public_messages and master_messages:
            return public_messages, master_messages

        legacy_messages = self._normalize(data.get("messages"))
        if legacy_messages:
            self.save_messages(
                self._without_master_commands(legacy_messages),
                legacy_messages,
            )
            return self.public_messages.copy(), self.master_messages.copy()
        return DEFAULT_PUBLIC_HELP_MESSAGES.copy(), DEFAULT_MASTER_HELP_MESSAGES.copy()

    def save_messages(
        self,
        public_messages: list[str],
        master_messages: list[str],
    ) -> None:
        public = self._normalize(public_messages) or DEFAULT_PUBLIC_HELP_MESSAGES.copy()
        master = self._normalize(master_messages) or DEFAULT_MASTER_HELP_MESSAGES.copy()

        write_json(self.state_path, {"public_messages": public, "master_messages": master})
        self.public_messages = public
        self.master_messages = master

    def reset_messages(self) -> tuple[list[str], list[str]]:
        self.save_messages(DEFAULT_PUBLIC_HELP_MESSAGES, DEFAULT_MASTER_HELP_MESSAGES)
        return self.public_messages.copy(), self.master_messages.copy()

    @staticmethod
    def _normalize(messages: object) -> list[str]:
        if not isinstance(messages, list):
            return []
        return [str(message).strip() for message in messages if str(message).strip()]

    @staticmethod
    def _without_master_commands(messages: list[str]) -> list[str]:
        result: list[str] = []
        for message in messages:
            lines = [
                line
                for line in message.splitlines()
                if not line.strip().casefold().startswith(
                    (
                        "/add-character ",
                        "/del-character ",
                        "/remember ",
                        "/memory",
                        "/forget ",
                    )
                )
            ]
            normalized = "\n".join(lines).strip()
            if normalized:
                result.append(normalized)
        return result or DEFAULT_PUBLIC_HELP_MESSAGES.copy()
