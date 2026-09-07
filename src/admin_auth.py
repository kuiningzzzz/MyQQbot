"""MyQQbot 管理界面登录鉴权。

- 密码存储于 gitignore 的配置文件 data/admin/auth.json（{"password": "..."}）。
  文件不存在时自动创建并写入默认初始密码 zjy051104，请首次登录后尽快修改。
- 登录成功后签发 HMAC 签名的 HttpOnly Cookie，无状态校验，重启后会话仍有效。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

COOKIE_NAME = "myqqbot_admin"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 天
DEFAULT_PASSWORD = "zjy051104"


class AdminAuth:
    def __init__(self, path: str = "data/admin/auth.json"):
        self.path = Path(path)
        self._password = self._load_or_create()

    def _load_or_create(self) -> str:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text("utf-8"))
                password = str(data.get("password") or "").strip()
                if password:
                    return password
                logger.error("管理密码配置为空：%s，管理界面将拒绝所有登录", self.path)
                return ""
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"password": DEFAULT_PASSWORD}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            logger.warning("管理密码配置文件不存在，已创建 %s（初始密码 %s，请尽快修改）", self.path, DEFAULT_PASSWORD)
            return DEFAULT_PASSWORD
        except Exception as exc:
            logger.error("读取管理密码配置失败：%s", exc)
            return ""

    def verify(self, password: str) -> bool:
        expected = self._password
        return bool(expected) and hmac.compare_digest(str(password or ""), expected)

    def _secret(self) -> bytes:
        return hashlib.sha256(("myqqbot-admin:" + self._password).encode("utf-8")).digest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_TTL_SECONDS
        payload = str(expires).encode("ascii")
        signature = hmac.new(self._secret(), payload, hashlib.sha256).hexdigest()
        return f"{expires}.{signature}"

    def validate(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        raw_expires, _, signature = token.partition(".")
        try:
            expires = int(raw_expires)
        except ValueError:
            return False
        if expires <= time.time():
            return False
        expected = hmac.new(self._secret(), raw_expires.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
