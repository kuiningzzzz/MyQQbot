from src.feature.atom import FeaturePlugin, StaticResource
from src.feature.pixiv.admin import PixivAdmin
from src.feature.pixiv.feature import PixivFeature


def create_plugin() -> FeaturePlugin:
    feature = PixivFeature()
    return FeaturePlugin(
        feature=feature,
        admin=PixivAdmin(feature),
        static_resources=(
            StaticResource(
                route=feature.public_route,
                directory=feature.root_dir,
                name="pixiv-files",
            ),
        ),
    )
