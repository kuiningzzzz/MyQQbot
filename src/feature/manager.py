from collections.abc import Iterable
import json
import logging
from pathlib import Path

from src.feature.atom import Feature, FeatureInfo, MessageContext, read_json, write_json

logger = logging.getLogger(__name__)


class FeatureManager:
    def __init__(self, state_path: Path, features: Iterable[Feature] = ()):
        self.state_path = state_path
        self.features: dict[str, Feature] = {}
        self.enabled: dict[str, bool] = {}
        for feature in features:
            self.register(feature)

    def register(self, feature: Feature):
        if feature.name in self.features:
            raise ValueError(f"duplicate feature name: {feature.name}")
        self.features[feature.name] = feature
        self.enabled.setdefault(feature.name, feature.default_enabled)

    def load_state(self):
        if not self.state_path.exists():
            self.save_state()
            return

        try:
            data = read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 feature 状态失败，将使用默认状态")
            return

        if not isinstance(data, dict):
            logger.error("Feature 状态必须是 JSON 对象，将使用默认状态")
            return

        for name, value in data.items():
            if name in self.features:
                self.enabled[name] = bool(value)

    def save_state(self):
        write_json(self.state_path, self.enabled)

    def set_enabled(self, name: str, enabled: bool) -> FeatureInfo:
        if name not in self.features:
            raise KeyError(name)

        self.enabled[name] = enabled
        self.save_state()
        return self.info(name)

    def is_enabled(self, name: str) -> bool:
        return self.enabled.get(name, False)

    def info(self, name: str) -> FeatureInfo:
        feature = self.features[name]
        return FeatureInfo(
            name=feature.name,
            title=feature.title,
            description=feature.description,
            enabled=self.is_enabled(name),
        )

    def list_features(self) -> list[FeatureInfo]:
        return [self.info(name) for name in self.features]

    async def handle_event(self, ctx: MessageContext) -> bool:
        for name, feature in self.features.items():
            if not self.is_enabled(name):
                continue

            try:
                handled = await feature.handle(ctx)
            except Exception:
                logger.exception("Feature %s 处理消息失败", name)
                await ctx.bot.reply(ctx, f"功能 {feature.title} 执行失败，请查看后端日志。")
                return True

            if handled:
                return True

        return False
