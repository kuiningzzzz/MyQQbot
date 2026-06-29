import json
import logging
from pathlib import Path

from src.feature.atom import read_json, write_json

logger = logging.getLogger(__name__)


class PixivConfig:
    def __init__(self, path: Path):
        self.path = path

    def load_refresh_token(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 Pixiv 配置失败")
            return None

        if not isinstance(data, dict):
            return None
        token = str(data.get("refresh_token") or "").strip()
        return token or None

    def save_refresh_token(self, refresh_token: str) -> None:
        token = refresh_token.strip()
        if not token:
            raise ValueError("refresh token cannot be empty")
        write_json(self.path, {"refresh_token": token})

    def clear_refresh_token(self) -> None:
        if self.path.exists():
            self.path.unlink()
