import hmac
import json
import logging
import re
import secrets
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.feature.atom import MessageContext, read_json, write_json

logger = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_lowercase + string.digits
SET_MASTER_PATTERN = re.compile(r"^/set-master\s+(\S+)$")


class MasterService:
    def __init__(self, state_path: Path, code_ttl_seconds: int = 600):
        if code_ttl_seconds <= 0:
            raise ValueError("code_ttl_seconds must be positive")
        self.state_path = state_path
        self.code_ttl_seconds = code_ttl_seconds
        self.master_id = self._load_master_id()
        self._verification_code: str | None = None
        self._code_expires_at: datetime | None = None

    async def handle(self, ctx: MessageContext) -> bool:
        if not ctx.msg.startswith("/set-master"):
            return False

        match = SET_MASTER_PATTERN.fullmatch(ctx.msg)
        if match is None:
            await ctx.bot.reply(ctx, "格式错误，请发送：/set-master 鉴别码")
            return True
        if ctx.user_id is None:
            await ctx.bot.reply(ctx, "无法识别当前用户 ID，绑定失败。")
            return True
        if not self.verify_code(match.group(1)):
            await ctx.bot.reply(ctx, "鉴别码无效或已过期，请在 WebUI 中重新生成。")
            return True

        self.bind_master(ctx.user_id)
        await ctx.bot.reply(ctx, f"绑定成功，QQ 用户 {ctx.user_id} 已成为机器人主人。")
        return True

    def generate_code(self) -> tuple[str, datetime]:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
        expires_at = datetime.now(UTC) + timedelta(seconds=self.code_ttl_seconds)
        self._verification_code = code
        self._code_expires_at = expires_at
        return code, expires_at

    def verify_code(self, candidate: str) -> bool:
        if self._verification_code is None or self._code_expires_at is None:
            return False
        if datetime.now(UTC) >= self._code_expires_at:
            self.clear_code()
            return False
        return hmac.compare_digest(candidate.casefold(), self._verification_code)

    def bind_master(self, user_id: int) -> None:
        self.master_id = user_id
        write_json(self.state_path, {"master_id": user_id})
        self.clear_code()
        logger.info("机器人主人已绑定为 QQ 用户 %s", user_id)

    def is_master(self, user_id: int | None) -> bool:
        """Return whether a QQ user may perform master-only chat operations."""
        return user_id is not None and user_id == self.master_id

    def clear_code(self) -> None:
        self._verification_code = None
        self._code_expires_at = None

    def _load_master_id(self) -> int | None:
        if not self.state_path.exists():
            return None
        try:
            data = read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取主人配置失败，将视为尚未绑定")
            return None
        if not isinstance(data, dict):
            return None
        master_id = data.get("master_id")
        if isinstance(master_id, int) and not isinstance(master_id, bool) and master_id > 0:
            return master_id
        return None
