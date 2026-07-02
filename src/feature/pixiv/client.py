from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class PixivDownloadError(RuntimeError):
    """Raised when Pixiv metadata or image download fails."""


class PixivAuthenticationRequired(PixivDownloadError):
    """Raised when a work cannot be fetched without a Pixiv login."""


@dataclass(frozen=True)
class PixivWork:
    illust_id: str
    title: str
    author: str
    image_paths: list[Path]


class PixivClient:
    """Small adapter around PixivPy so the feature queue stays API-agnostic."""

    def __init__(
        self,
        refresh_token: str | None = None,
        proxy_url: str | None = None,
    ):
        self.refresh_token = refresh_token
        self.proxy_url = (
            proxy_url.strip()
            if proxy_url is not None
            else os.getenv("PIXIV_PROXY", "").strip()
        )
        self._api: Any = None

    def set_refresh_token(self, refresh_token: str | None) -> None:
        self.refresh_token = refresh_token.strip() if refresh_token else None
        self._api = None

    def download_work(self, illust_id: str, output_dir: Path) -> PixivWork:
        api = self._get_api()
        response = api.illust_detail(illust_id)
        error = getattr(response, "error", None)
        if error:
            message = self._error_message(error)
            if not self.refresh_token:
                raise PixivAuthenticationRequired(
                    f"{message}；该作品可能需要登录，请在管理 WebUI 配置 refresh token"
                )
            raise PixivDownloadError(message)

        illust = getattr(response, "illust", None)
        if illust is None:
            raise PixivDownloadError(f"Pixiv 作品 {illust_id} 不存在或无法访问")

        image_urls = self._original_image_urls(illust)
        if not image_urls:
            raise PixivDownloadError(f"Pixiv 作品 {illust_id} 没有可下载的图片")

        output_dir.mkdir(parents=True, exist_ok=True)
        image_paths: list[Path] = []
        for index, url in enumerate(image_urls):
            extension = Path(urlparse(url).path).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                extension = ".jpg"
            file_name = f"{illust_id}_p{index}{extension}"
            api.download(url, path=str(output_dir), name=file_name)
            image_path = output_dir / file_name
            if not image_path.is_file():
                raise PixivDownloadError(f"Pixiv 图片下载后未找到文件: {file_name}")
            image_paths.append(image_path)

        user = getattr(illust, "user", None)
        return PixivWork(
            illust_id=illust_id,
            title=str(getattr(illust, "title", illust_id)),
            author=str(getattr(user, "name", "未知作者")),
            image_paths=image_paths,
        )

    def _get_api(self):
        if self._api is not None:
            return self._api

        try:
            from pixivpy3 import AppPixivAPI
        except ImportError as exc:
            raise PixivDownloadError(
                "Pixiv 功能依赖 pixivpy3，请先安装 requirements.txt 里的依赖"
            ) from exc

        request_options = {}
        if self.proxy_url:
            request_options["proxies"] = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }
        api = AppPixivAPI(**request_options)
        api.requests.trust_env = False
        if self.refresh_token:
            api.auth(refresh_token=self.refresh_token)
        self._api = api
        return api

    @staticmethod
    def _original_image_urls(illust: Any) -> list[str]:
        pages = getattr(illust, "meta_pages", None) or []
        if pages:
            urls = []
            for page in pages:
                image_urls = getattr(page, "image_urls", None)
                url = getattr(image_urls, "original", None)
                if url:
                    urls.append(str(url))
            return urls

        single_page = getattr(illust, "meta_single_page", None)
        original = getattr(single_page, "original_image_url", None)
        if original:
            return [str(original)]

        image_urls = getattr(illust, "image_urls", None)
        fallback = getattr(image_urls, "large", None)
        return [str(fallback)] if fallback else []

    @staticmethod
    def _error_message(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("user_message") or error.get("message") or error)
        return str(
            getattr(error, "user_message", None)
            or getattr(error, "message", None)
            or error
        )
