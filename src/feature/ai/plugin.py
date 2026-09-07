from src.feature.atom import FeaturePlugin

from .admin import AiAdmin
from .feature import AiFeature


def create_plugin() -> FeaturePlugin:
    feature = AiFeature()
    return FeaturePlugin(feature=feature, admin=AiAdmin(feature))
