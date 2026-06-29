from src.feature.atom import FeaturePlugin, StaticResource
from src.feature.jm.admin import JmAdmin
from src.feature.jm.feature import JmPdfFeature


def create_plugin() -> FeaturePlugin:
    feature = JmPdfFeature()
    return FeaturePlugin(
        feature=feature,
        admin=JmAdmin(feature),
        static_resources=(
            StaticResource(
                route=feature.public_route,
                directory=feature.root_dir,
                name="jm-files",
            ),
        ),
    )
