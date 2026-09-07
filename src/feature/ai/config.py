from __future__ import annotations

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from .storage import atomic_write


class Settings(BaseModel):
    api_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-v4-flash"
    vision_model: str = "deepseek-v4-flash-vision-exp"
    bot_qq: str = ""
    nickname: str = "奎大宁"
    account_description: str = "这是机器人自己的 QQ 账号，发言仅作对话背景，不是用户事实。"
    chat_enabled: bool = True
    summary_enabled: bool = True
    memory_enabled: bool = True
    memory_auto_learn: bool = True
    thinking_mode: str = "disabled"
    reasoning_effort: str = "high"
    max_tokens: int = Field(default=8192, ge=256, le=131072)
    context_token_limit: int = Field(default=1_000_000, ge=4096, le=1_000_000)
    context_recent_count: int = Field(default=20, ge=1, le=200)
    context_compress_threshold: float = Field(default=0.8, ge=0.1, le=0.9)
    raw_retention_days: int = Field(default=7, ge=1, le=365)
    vector_index_enabled: bool = True
    vector_merge_threshold: int = Field(default=7, ge=1, le=365)
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    timezone: str = "Asia/Shanghai"
    timeout_seconds: int = Field(default=60, ge=10, le=300)
    tool_round_limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def budget_valid(self):
        if self.max_tokens >= self.context_token_limit * 0.8:
            raise ValueError("最大生成Token必须小于上下文上限的80%")
        if not self.nickname.strip():
            raise ValueError("机器人网名不能为空")
        return self

    @field_validator("bot_qq")
    @classmethod
    def qq_valid(cls, value: str) -> str:
        if value and (not value.isascii() or not value.isdigit()):
            raise ValueError("机器人 QQ 号必须是数字")
        return value

    @field_validator("thinking_mode")
    @classmethod
    def thinking_valid(cls, value: str) -> str:
        if value not in {"inherit", "enabled", "disabled"}:
            raise ValueError("思考模式无效")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def effort_valid(cls, value: str) -> str:
        if value not in {"high", "max"}:
            raise ValueError("思考强度无效")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("时区名称无效") from exc
        return value

    @field_validator("api_url")
    @classmethod
    def url_valid(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("API URL 必须是 HTTP(S) 地址")
        return value.rstrip("/")


class Config:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "config.yaml"
        if not self.path.exists():
            # Only carry over connection credentials, never old memory or characters.
            legacy = root.parent / "summary/config.json"
            data = json.loads(legacy.read_text("utf-8")) if legacy.exists() else {}
            self.save({key: data[key] for key in ("api_url", "api_key") if key in data})

    def load(self) -> Settings:
        import yaml
        data = yaml.safe_load(self.path.read_text("utf-8")) or {}
        for name in Settings.model_fields:
            value = os.getenv("AI_" + name.upper())
            if value is None and name in {"raw_retention_days", "context_recent_count", "context_compress_threshold", "vector_index_enabled", "vector_merge_threshold", "timezone"}:
                value = os.getenv(name.upper())
            if value is not None:
                data[name] = value
        return Settings.model_validate(data)

    def save(self, changes: dict) -> Settings:
        import yaml
        current = yaml.safe_load(self.path.read_text("utf-8")) if self.path.exists() else {}
        data = {**(current or {}), **changes}
        if changes.get("api_key") is None:
            data["api_key"] = (current or {}).get("api_key", "")
        value = Settings.model_validate(data)
        atomic_write(self.path, yaml.safe_dump(value.model_dump(), allow_unicode=True, sort_keys=False))
        return value

    def public(self) -> dict:
        data = self.load().model_dump()
        data["api_key_configured"] = bool(data.pop("api_key"))
        return data
