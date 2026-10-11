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
    "/short 10至200 - AI 总结当前群聊仍保留的最近消息\n@机器人或提到机器人网名 - 唤醒AI聊天，60秒内可连续交谈",
    "/hello - 回复 hello",
    "/timing-task 选项 时间 文本 - 北京时间定时消息，保留空格和群聊艾特\n/timing-task-AI 选项 时间 提示词 - 定时AI任务\n选项：-y每年 MM-DD-HH-MM-SS；-m每月 DD-HH-MM-SS；-w每周 01至07-HH-MM-SS；-d每天 HH-MM-SS；-o一次 YY-MM-DD-HH-MM-SS（也支持四位年份）\n两类指令均支持 list 查看、del ID 删除，只管理当前群聊或私聊；循环时间已过安排下一次。",
]

DEFAULT_MASTER_HELP_MESSAGES = [*DEFAULT_PUBLIC_HELP_MESSAGES, "AI账号、人设与记忆文件请在管理WebUI的AI服务中配置。"]


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

    def agent_usage(self, owner: bool = False) -> dict:
        # Use the same in-memory texts that /help and admin read; admin saves
        # and resets become visible next turn without restarting the service.
        usage = {'public_messages': self.public_messages.copy()}
        if owner:
            usage['master_messages'] = self.master_messages.copy()
        return usage

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
        retired = ("/character", "/change-character", "/add-character", "/del-character", "/remember", "/memory", "/forget", "/open-news", "/close-news", "/news")
        result = []
        for message in messages:
            text = "\n".join(line for line in str(message).splitlines() if not line.strip().startswith(retired)).strip()
            if text:
                result.append(text)
        return result

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
