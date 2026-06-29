from src.feature.atom import FeaturePlugin
from src.feature.summary.admin import SummaryAdmin
from src.feature.summary.feature import SummaryFeature


def create_plugin() -> FeaturePlugin:
    feature = SummaryFeature()
    return FeaturePlugin(feature=feature, admin=SummaryAdmin(feature))
