from pathlib import Path

from src.feature.atom import FeaturePlugin
from src.feature.help.admin import HelpAdmin
from src.feature.help.feature import HelpFeature


def create_plugin() -> FeaturePlugin:
    feature = HelpFeature(Path("data/help.json"))
    return FeaturePlugin(feature=feature, admin=HelpAdmin(feature))
