import json
import logging
import os
import threading
from pathlib import Path

from src.feature.atom import read_json

logger = logging.getLogger(__name__)


class PixivConfig:
    def __init__(self, path: Path):
        self.path = path
        self.credentials_path = path.with_suffix('.account')
        self.key_path = path.with_suffix('.key')
        self.lock = threading.RLock()

    @staticmethod
    def _private_write(path: Path, content: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        with temporary.open('wb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(content)
        os.replace(temporary, path)

    def load_credentials(self) -> dict:
        with self.lock:
            if not self.credentials_path.exists():
                return {}
            from cryptography.fernet import Fernet
            try:
                data = Fernet(self.key_path.read_bytes()).decrypt(self.credentials_path.read_bytes())
                return json.loads(data)
            except Exception:
                raise ValueError('无法解密 Pixiv 账号配置，请重新保存账号密码。') from None

    def save_credentials(self, username: str, password: str, totp_secret: str = ''):
        from cryptography.fernet import Fernet
        with self.lock:
            try:
                cipher = Fernet(self.key_path.read_bytes())
            except (OSError, ValueError):
                # A lost/corrupt key makes old credentials unusable; saving a
                # confirmed account is the recovery path advertised by the UI.
                self._private_write(self.key_path, Fernet.generate_key())
                cipher = Fernet(self.key_path.read_bytes())
            self._private_write(self.credentials_path, cipher.encrypt(json.dumps({
                'username': username, 'password': password, 'totp_secret': totp_secret,
            }, ensure_ascii=False).encode('utf-8')))

    def clear_credentials(self):
        with self.lock:
            self.credentials_path.unlink(missing_ok=True)

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
        with self.lock:
            self._private_write(self.path, json.dumps({'refresh_token': token}).encode('utf-8'))

    def clear_refresh_token(self) -> None:
        with self.lock:
            self.path.unlink(missing_ok=True)
