from pathlib import Path

from src.feature.atom import FeaturePlugin
from src.feature.help.admin import HelpAdmin
from src.feature.help.feature import HelpFeature
from src.master import MasterService


def create_plugin(master_service: MasterService) -> FeaturePlugin:
    feature = HelpFeature(Path("data/help.json"), master_service)
    return FeaturePlugin(feature=feature, admin=HelpAdmin(feature))
