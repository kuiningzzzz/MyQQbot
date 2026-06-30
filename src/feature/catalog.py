from src.feature.atom import FeaturePlugin
from src.feature.help.plugin import create_plugin as create_help_plugin
from src.feature.hello.plugin import create_plugin as create_hello_plugin
from src.feature.jm.plugin import create_plugin as create_jm_plugin
from src.feature.pixiv.plugin import create_plugin as create_pixiv_plugin
from src.feature.summary.plugin import create_plugin as create_summary_plugin
from src.master import MasterService


def create_default_plugins(master_service: MasterService) -> list[FeaturePlugin]:
    """The single composition root where application features are selected."""
    return [
        create_help_plugin(master_service),
        create_hello_plugin(),
        create_jm_plugin(),
        create_pixiv_plugin(),
        create_summary_plugin(master_service),
    ]
