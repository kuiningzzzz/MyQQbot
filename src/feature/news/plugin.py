from pathlib import Path

from src.feature.atom import FeaturePlugin
from src.feature.news.admin import NewsAdmin
from src.feature.news.config import NewsConfig
from src.feature.news.feature import NewsFeature
from src.feature.news.store import NewsStore
from src.feature.summary.client import OpenAICompatibleClient
from src.feature.summary.config import SummaryConfig
from src.feature.web_search import WebSearchService


def create_plugin(
    ai_config: SummaryConfig,
    client: OpenAICompatibleClient,
    web_search: WebSearchService,
) -> FeaturePlugin:
    feature = NewsFeature(
        NewsConfig(Path("data/news/config.json")),
        NewsStore(Path("data/news/subscriptions.json"), Path("data/news/history.json")),
        ai_config,
        client,
        web_search,
    )
    return FeaturePlugin(feature=feature, admin=NewsAdmin(feature))
