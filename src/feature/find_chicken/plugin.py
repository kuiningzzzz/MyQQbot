from src.feature.atom import FeaturePlugin
from .feature import FindChickenFeature
from .admin import FindChickenAdmin


def create_plugin():
    feature = FindChickenFeature()
    return FeaturePlugin(feature=feature, admin=FindChickenAdmin(feature))
