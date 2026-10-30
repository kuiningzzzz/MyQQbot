from pathlib import Path

from fastapi import FastAPI

from .config import Settings


class FindChickenAdmin:
    def __init__(self, feature):
        self.feature = feature

    def register_routes(self, app: FastAPI):
        @app.get('/api/find-chicken')
        async def get_config():
            return self.feature.config.load().model_dump()

        @app.post('/api/find-chicken')
        async def save_config(body: Settings):
            return self.feature.config.save(body.model_dump()).model_dump()

    @property
    def panel_html(self):
        return Path(__file__).with_name('panel.html').read_text(encoding='utf-8')

    @property
    def javascript(self):
        return Path(__file__).with_name('panel.js').read_text(encoding='utf-8')
