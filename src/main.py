import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from src.admin import create_admin_app
from src.bot import QQBot
from src.feature.catalog import create_default_plugins
from src.feature.manager import FeatureManager
from src.logging_service import install_application_log_handler

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
install_application_log_handler()
logger = logging.getLogger(__name__)

plugins = create_default_plugins()
feature_manager = FeatureManager(
    state_path=Path("data/features.json"),
    features=(plugin.feature for plugin in plugins),
)
feature_manager.load_state()
bot = QQBot(feature_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("QQ Bot 启动成功，监听端口 8081")
    logger.info("WebSocket 端点: ws://0.0.0.0:8081/ws 或 ws://0.0.0.0:8081/ws/")
    yield
    logger.info("QQ Bot 正在关闭...")


bot_app = FastAPI(lifespan=lifespan, title="MyQQbot Backend")
for plugin in plugins:
    for resource in plugin.static_resources:
        resource.directory.mkdir(parents=True, exist_ok=True)
        bot_app.mount(
            resource.route,
            StaticFiles(directory=resource.directory),
            name=resource.name,
        )
admin_app = create_admin_app(feature_manager, plugins)


@bot_app.websocket("/ws")
@bot_app.websocket("/ws/")
async def websocket_endpoint(websocket: WebSocket):
    await bot.handle_websocket(websocket)


@bot_app.get("/")
async def root():
    return {
        "status": "running",
        "message": "QQ Bot is running",
        "websocket_endpoint": "/ws or /ws/",
        "connected_clients": len(bot.connected_clients),
        "admin": "http://127.0.0.1:10010",
    }


async def serve():
    bot_server = uvicorn.Server(
        uvicorn.Config(
            bot_app,
            host="0.0.0.0",
            port=8081,
            log_level="info",
            log_config=None,
        )
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            host="0.0.0.0",
            port=10010,
            log_level="info",
            log_config=None,
        )
    )
    await asyncio.gather(bot_server.serve(), admin_server.serve())


if __name__ == "__main__":
    asyncio.run(serve())
