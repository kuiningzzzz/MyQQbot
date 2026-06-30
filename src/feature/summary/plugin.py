from src.feature.atom import FeaturePlugin
from src.feature.summary.admin import SummaryAdmin
from src.feature.summary.feature import SummaryFeature
from src.master import MasterService


def create_plugin(master_service: MasterService) -> FeaturePlugin:
    feature = SummaryFeature(master_service)
    return FeaturePlugin(feature=feature, admin=SummaryAdmin(feature))
