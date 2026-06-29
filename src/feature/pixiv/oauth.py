from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"


class PixivOAuthError(RuntimeError):
    """Raised when a Pixiv PKCE authorization flow cannot be completed."""


@dataclass(frozen=True)
class PixivOAuthStart:
    flow_id: str
    login_url: str
    expires_in: int


@dataclass(frozen=True)
class _PendingFlow:
    code_verifier: str
    expires_at: float


class PixivOAuth:
    """Short-lived PKCE flow used only to obtain and persist a refresh token."""

    def __init__(
        self,
        lifetime_seconds: int = 300,
        request_timeout: int = 20,
        post: Callable[..., Any] = requests.post,
    ):
        self.lifetime_seconds = lifetime_seconds
        self.request_timeout = request_timeout
        self._post = post
        self._pending: dict[str, _PendingFlow] = {}
        self._lock = Lock()

    def start(self) -> PixivOAuthStart:
        flow_id = token_urlsafe(24)
        code_verifier = token_urlsafe(32)
        code_challenge = urlsafe_b64encode(
            sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        with self._lock:
            self._discard_expired()
            self._pending[flow_id] = _PendingFlow(
                code_verifier=code_verifier,
                expires_at=monotonic() + self.lifetime_seconds,
            )
        login_params = {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "client": "pixiv-android",
        }
        login_url = f"{LOGIN_URL}?{urlencode(login_params)}"
        return PixivOAuthStart(
            flow_id=flow_id,
            login_url=login_url,
            expires_in=self.lifetime_seconds,
        )

    def exchange(self, flow_id: str, callback_or_code: str) -> str:
        with self._lock:
            flow = self._pending.pop(flow_id, None)
        if flow is None or flow.expires_at <= monotonic():
            raise PixivOAuthError("授权流程不存在或已过期，请重新生成授权链接")

        code = self.extract_code(callback_or_code)
        try:
            response = self._post(
                AUTH_TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "code": code,
                    "code_verifier": flow.code_verifier,
                    "grant_type": "authorization_code",
                    "include_policy": "true",
                    "redirect_uri": REDIRECT_URI,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=self.request_timeout,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PixivOAuthError("连接 Pixiv OAuth 服务失败") from exc

        if not isinstance(data, dict):
            raise PixivOAuthError("Pixiv OAuth 服务返回了无法识别的数据")
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            error = data.get("error_description") or data.get("error")
            message = str(error or "Pixiv 未返回 refresh token")
            raise PixivOAuthError(f"授权交换失败：{message}")
        return refresh_token

    @staticmethod
    def extract_code(callback_or_code: str) -> str:
        value = callback_or_code.strip()
        if not value:
            raise PixivOAuthError("callback URL 或授权 code 不能为空")

        if "://" in value or "?" in value:
            query = parse_qs(urlparse(value).query)
            code = str((query.get("code") or [""])[0]).strip()
        else:
            code = value
        if not code:
            raise PixivOAuthError("没有在 callback URL 中找到授权 code")
        return code

    def _discard_expired(self) -> None:
        now = monotonic()
        self._pending = {
            flow_id: flow
            for flow_id, flow in self._pending.items()
            if flow.expires_at > now
        }
