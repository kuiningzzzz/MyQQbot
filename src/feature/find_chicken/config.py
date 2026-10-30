import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.feature.atom import write_json

logger = logging.getLogger(__name__)
ENGINES = ('lens', 'saucenao', 'tracemoe', 'iqdb', 'baidu')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    engines: list[str] = Field(default_factory=lambda: ['lens', 'saucenao', 'tracemoe'])
    timeout_seconds: int = Field(default=45, ge=5, le=90)
    total_timeout_seconds: int = Field(default=240, ge=30, le=600)
    max_accounts: int = Field(default=3, ge=1, le=10)
    strict_consensus: bool = True
    cooldown_seconds: int = Field(default=30, ge=0, le=3600)
    proxy: str = Field(default='', max_length=2048)
    browser_executable: str = Field(default='', max_length=2048)

    @field_validator('engines')
    @classmethod
    def engines_valid(cls, value):
        if not value or any(engine not in ENGINES for engine in value):
            raise ValueError('至少选择一个支持的引擎')
        return list(dict.fromkeys(value))

    @field_validator('proxy')
    @classmethod
    def proxy_valid(cls, value):
        from urllib.parse import urlsplit
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {'http', 'https', 'socks5'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('浏览器代理应为不带账号密码的 http/https/socks5 地址')
            if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
                raise ValueError('代理地址不能包含路径、查询参数或片段')
            parsed.port  # Validate malformed/out-of-range port numbers.
        return value


class Config:
    def __init__(self, root: Path):
        self.path = root / 'config.json'

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            return Settings.model_validate_json(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            logger.exception('搜图配置读取失败，使用默认配置')
            return Settings()

    def save(self, values: dict) -> Settings:
        settings = Settings.model_validate(values)
        write_json(self.path, settings.model_dump())
        return settings
