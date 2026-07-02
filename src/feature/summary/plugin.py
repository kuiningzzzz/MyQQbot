from src.feature.atom import FeaturePlugin
from src.feature.summary.admin import SummaryAdmin
from src.feature.summary.feature import SummaryFeature
from src.master import MasterService
from src.feature.summary.client import OpenAICompatibleClient
from src.feature.summary.config import SummaryConfig
from src.feature.web_search import WebSearchService


def create_plugin(
    master_service: MasterService,
    config: SummaryConfig | None = None,
    client: OpenAICompatibleClient | None = None,
    web_search: WebSearchService | None = None,
) -> FeaturePlugin:
    feature = SummaryFeature(
        master_service, config=config, client=client, web_search=web_search
    )
    return FeaturePlugin(feature=feature, admin=SummaryAdmin(feature))
