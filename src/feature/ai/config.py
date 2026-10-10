from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from .storage import atomic_write

load_dotenv(Path(__file__).resolve().parents[3] / '.env', override=False)
CONNECTION_FIELDS = {'api_url', 'dsh_api_url', 'api_key', 'model', 'vision_model'}


class Settings(BaseModel):
    api_url: str = "https://api.deepseek.com"
    dsh_api_url: str = ""
    api_key: str = ""
    model: Literal['deepseek-flash'] = 'deepseek-flash'
    vision_model: Literal['deepseek-flash'] = 'deepseek-flash'
    bot_qq: str = ""
    nickname: str = "奎大宁"
    account_description: str = "这是机器人自己的 QQ 账号，发言仅作对话背景，不是用户事实。"
    chat_enabled: bool = True
    summary_enabled: bool = True
    memory_enabled: bool = True
    memory_auto_learn: bool = True
    raw_retention_days: int = Field(default=30, ge=30, le=30)
    memory_batch_size: int = Field(default=100, ge=100, le=100)
    vector_index_enabled: bool = True
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    timezone: str = "Asia/Shanghai"
    workspace_enabled: bool = True
    ocr_enabled: bool = True

    @model_validator(mode="after")
    def budget_valid(self):
        if not self.nickname.strip():
            raise ValueError("机器人网名不能为空")
        return self

    @field_validator("bot_qq")
    @classmethod
    def qq_valid(cls, value: str) -> str:
        if value and (not value.isascii() or not value.isdigit()):
            raise ValueError("机器人 QQ 号必须是数字")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("时区名称无效") from exc
        return value

    @field_validator("dsh_api_url")
    @classmethod
    def dsh_url_valid(cls, value):
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("dsh API URL 必须是 HTTP(S) 地址")
        return value.rstrip("/")

    @field_validator("api_url")
    @classmethod
    def url_valid(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("API URL 必须是 HTTP(S) 地址")
        return value.rstrip("/")


class Config:
    editable = set(Settings.model_fields) - CONNECTION_FIELDS - {'raw_retention_days', 'memory_batch_size'}

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "config.yaml"
        # Remove retired limits and YAML credentials on upgrade. Only the
        # process environment can configure model connections from now on.
        self._persist(self._preferences())

    def _preferences(self):
        import yaml
        data = yaml.safe_load(self.path.read_text('utf-8')) if self.path.exists() else {}
        return {key: value for key, value in (data or {}).items() if key in self.editable}

    def _persist(self, data):
        import yaml
        settings = Settings.model_validate(data)
        atomic_write(self.path, yaml.safe_dump({key: getattr(settings, key) for key in sorted(self.editable)}, allow_unicode=True, sort_keys=False))
        return settings

    def load(self) -> Settings:
        data = self._preferences()
        for name in self.editable:
            value = os.getenv("AI_" + name.upper())
            if value is None and name in {"vector_index_enabled", "timezone"}:
                value = os.getenv(name.upper())
            if value is not None:
                data[name] = value
        data.update(api_key=os.getenv('DEEPSEEK_API_KEY') or os.getenv('AI_API_KEY') or '',
            api_url=os.getenv('AI_API_URL') or 'https://api.deepseek.com',
            dsh_api_url=os.getenv('AI_DSH_API_URL') or os.getenv('DEEPSEEK_BASE_URL') or '')
        return Settings.model_validate(data)

    def save(self, changes: dict) -> Settings:
        unknown = set(changes) - self.editable
        if unknown:
            raise ValueError('以下设置已由环境或dsh管理，不能在管理界面修改：' + '、'.join(sorted(unknown)))
        return self._persist({**self._preferences(), **changes})

    def public(self) -> dict:
        settings = self.load()
        data = {key: getattr(settings, key) for key in self.editable}
        data.update(model='deepseek-flash', api_key_configured=bool(settings.api_key), raw_retention_days=30, memory_batch_size=100, search_provider='DeepSeek 原生搜索')
        return data
