import json
import logging
from pathlib import Path

from src.feature.atom import Feature, MessageContext, read_json, write_json

logger = logging.getLogger(__name__)


DEFAULT_HELP_MESSAGES = [
    "/help - 查看当前支持的指令",
    "/set-master 鉴别码 - 将当前 QQ 用户绑定为机器人主人",
    "/jm 号码 - 下载对应 JM 漫画，导出 PDF 并发送到当前会话\n/jm status - 查看 JM 当前处理队列",
    "/pixiv 作品ID - 获取对应 Pixiv 图片或图片集\n/pixiv status - 查看 Pixiv 当前处理队列",
    "/short 10至200 - AI 总结当前群聊最近指定数量的消息（暂时不支持图片和语音等多模态信息的解析读取）",
    "/character - 查看可用的 AI 聊天人设\n/change-character 名字 - 切换当前会话的 AI 人设\n/add-character 名字 触发词 描述 系统提示词正文 - 主人添加 AI 人设\n/del-character 名字 - 主人删除 AI 人设",
    "/hello - 回复 hello",
]


class HelpFeature(Feature):
    name = "help"
    title = "帮助菜单"
    description = "收到 /help 后，以 QQ 聊天记录形式发送当前支持的指令合集。"
    default_enabled = True

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.messages = self.load_messages()

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg != "/help":
            return False

        await ctx.bot.send_forward_msg(ctx, self.messages)
        return True

    def load_messages(self) -> list[str]:
        if not self.state_path.exists():
            self.save_messages(DEFAULT_HELP_MESSAGES)
            return DEFAULT_HELP_MESSAGES.copy()

        try:
            data = read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 help 配置失败，将使用默认帮助文本")
            return DEFAULT_HELP_MESSAGES.copy()

        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return DEFAULT_HELP_MESSAGES.copy()

        normalized = [str(message).strip() for message in messages if str(message).strip()]
        return normalized or DEFAULT_HELP_MESSAGES.copy()

    def save_messages(self, messages: list[str]):
        normalized = [str(message).strip() for message in messages if str(message).strip()]
        if not normalized:
            normalized = DEFAULT_HELP_MESSAGES.copy()

        write_json(self.state_path, {"messages": normalized})
        self.messages = normalized

    def reset_messages(self) -> list[str]:
        self.save_messages(DEFAULT_HELP_MESSAGES)
        return self.messages.copy()
