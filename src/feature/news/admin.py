from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.news.feature import NewsFeature


class NewsConfigRequest(BaseModel):
    enabled: bool
    broadcast_hour: int
    news_count: int
    requirements: str


class NewsAdmin:
    def __init__(self, feature: NewsFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/news/config")
        async def get_news_config():
            value = feature.config.load()
            return {
                "enabled": value.enabled,
                "broadcast_hour": value.broadcast_hour,
                "news_count": value.news_count,
                "requirements": value.requirements,
                "subscription_count": len(feature.store.targets()),
                "recent_count": len(feature.store.recent_news(5)),
                "last_attempt_date": feature.store.last_attempt_date(),
            }

        @app.post("/api/news/config")
        async def save_news_config(body: NewsConfigRequest):
            try:
                feature.config.save(
                    body.enabled,
                    body.broadcast_hour,
                    body.news_count,
                    body.requirements,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"ok": True}

    @property
    def panel_html(self) -> str:
        return ""

    @property
    def javascript(self) -> str:
        return ""
