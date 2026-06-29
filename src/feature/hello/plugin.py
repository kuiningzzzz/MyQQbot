from src.feature.atom import FeaturePlugin
from src.feature.hello.feature import HelloFeature


def create_plugin() -> FeaturePlugin:
    return FeaturePlugin(feature=HelloFeature())
