import json
import logging
from pathlib import Path

from src.bot import MessageContext
from src.feature.base import Feature, FeatureInfo
from src.feature.help import HelpFeature
from src.feature.jm_pdf import JmPdfFeature

logger = logging.getLogger(__name__)


class FeatureManager:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.features: dict[str, Feature] = {}
        self.enabled: dict[str, bool] = {}
        self.help_feature: HelpFeature | None = None

    @classmethod
    def default(cls) -> "FeatureManager":
        manager = cls(Path("data/features.json"))
        manager.register(HelpFeature(Path("data/help.json")))
        manager.register(JmPdfFeature())
        manager.load_state()
        return manager

    def register(self, feature: Feature):
        self.features[feature.name] = feature
        self.enabled.setdefault(feature.name, feature.default_enabled)
        if isinstance(feature, HelpFeature):
            self.help_feature = feature

    def load_state(self):
        if not self.state_path.exists():
            self.save_state()
            return

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 feature 状态失败，将使用默认状态")
            return

        for name, value in data.items():
            if name in self.features:
                self.enabled[name] = bool(value)

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.enabled, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

    def get_help_messages(self) -> list[str]:
        if self.help_feature is None:
            return []
        return self.help_feature.messages.copy()

    def set_help_messages(self, messages: list[str]) -> list[str]:
        if self.help_feature is None:
            raise RuntimeError("help feature is not registered")
        self.help_feature.save_messages(messages)
        return self.help_feature.messages.copy()

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
