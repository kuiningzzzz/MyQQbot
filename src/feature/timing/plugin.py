from src.feature.atom import FeaturePlugin
from .feature import TimingFeature
from .admin import TimingAdmin


def create_plugin(ai, master):
    feature = TimingFeature(ai, master)
    ai.timing = feature
    return FeaturePlugin(feature, TimingAdmin(feature))
