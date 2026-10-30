from src.feature.atom import FeaturePlugin
from src.feature.help.plugin import create_plugin as create_help_plugin
from src.feature.hello.plugin import create_plugin as create_hello_plugin
from src.feature.jm.plugin import create_plugin as create_jm_plugin
from src.feature.pixiv.plugin import create_plugin as create_pixiv_plugin
from src.feature.ai.plugin import create_plugin as create_ai_plugin
from src.feature.timing.plugin import create_plugin as create_timing_plugin
from src.feature.find_chicken.plugin import create_plugin as create_find_chicken_plugin
from src.master import MasterService


def create_default_plugins(master_service: MasterService) -> list[FeaturePlugin]:
    """The single composition root where application features are selected."""
    ai = create_ai_plugin()
    return [
        create_help_plugin(master_service),
        create_hello_plugin(),
        create_jm_plugin(),
        create_pixiv_plugin(),
        create_find_chicken_plugin(),
        ai,
        create_timing_plugin(ai.feature, master_service),
    ]
