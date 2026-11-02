from src.feature.atom import FeaturePlugin
from .feature import LoveyouFeature
from .admin import LoveyouAdmin


def create_plugin(master_service):
    feature = LoveyouFeature(master_service)
    return FeaturePlugin(feature=feature, admin=LoveyouAdmin(feature))
