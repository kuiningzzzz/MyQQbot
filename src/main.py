import sys

# Keep the supervisor free of bot imports: those constructors create/open data.
# Restore and crash recovery must finish before a single feature is initialized.
if __name__ == '__main__' and '--runtime' not in sys.argv:
    from src.backup.supervisor import run
    raise SystemExit(run())

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from src.admin import create_admin_app
from src.backup.control import BackupControl
from src.backup.supervisor import MAINTENANCE_EXIT
from src.bot import QQBot
from src.feature.catalog import create_default_plugins
from src.feature.manager import FeatureManager
from src.logging_service import install_application_log_handler
from src.master import MasterService

# 原生部署时用环境变量把两个监听口收到回环，容器部署沿用原默认值。
WS_HOST = os.getenv("MYQQBOT_WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("MYQQBOT_WS_PORT", "8081"))
ADMIN_HOST = os.getenv("MYQQBOT_ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.getenv("MYQQBOT_ADMIN_PORT", "10010"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
install_application_log_handler()
logger = logging.getLogger(__name__)

master_service = MasterService(Path("data/master.json"))
plugins = create_default_plugins(master_service)
feature_manager = FeatureManager(
    state_path=Path("data/features.json"),
    features=(plugin.feature for plugin in plugins),
)
feature_manager.load_state()
bot = QQBot(feature_manager, master_service)
backup_control = BackupControl(Path.cwd(), feature_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("QQ Bot 启动成功，监听端口 8081")
    logger.info("WebSocket 端点: ws://0.0.0.0:8081/ws 或 ws://0.0.0.0:8081/ws/")
    await feature_manager.start(bot)
    try:
        yield
    finally:
        logger.info("QQ Bot 正在关闭...")
        await feature_manager.stop()


bot_app = FastAPI(lifespan=lifespan, title="MyQQbot Backend")
for plugin in plugins:
    for resource in plugin.static_resources:
        resource.directory.mkdir(parents=True, exist_ok=True)
        bot_app.mount(
            resource.route,
            StaticFiles(directory=resource.directory),
            name=resource.name,
        )
admin_app = create_admin_app(feature_manager, master_service, plugins, backup_control=backup_control)


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
        "admin": f"http://127.0.0.1:{ADMIN_PORT}",
    }


async def serve():
    class ManagedServer(uvicorn.Server):
        # Both servers share one process. A signal must stop BOTH, rather than
        # whichever server installed Uvicorn's handler last (0.27/newer APIs).
        def install_signal_handlers(self):
            pass

        @contextmanager
        def capture_signals(self):
            yield

    bot_server = ManagedServer(
        uvicorn.Config(
            bot_app,
            host=WS_HOST,
            port=WS_PORT,
            # A 20MiB attachment encoded as Base64 needs about 27MiB per frame.
            ws_max_size=32 * 1024 * 1024,
            ws_max_queue=4,
            log_level="info",
            log_config=None,
            timeout_graceful_shutdown=15,
        )
    )
    admin_server = ManagedServer(
        uvicorn.Config(
            admin_app,
            host=ADMIN_HOST,
            port=ADMIN_PORT,
            log_level="info",
            log_config=None,
            timeout_graceful_shutdown=15,
        )
    )
    def shutdown_for_backup():
        bot_server.should_exit = admin_server.should_exit = True

    backup_control.shutdown = shutdown_for_backup
    previous = {sig: signal.signal(sig, lambda *_: shutdown_for_backup()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        await asyncio.gather(bot_server.serve(), admin_server.serve())
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    asyncio.run(serve())
    if backup_control.requested:
        raise SystemExit(MAINTENANCE_EXIT)
