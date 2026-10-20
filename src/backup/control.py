"""Runtime admission and graceful shutdown, separate from cold filesystem work."""
import os

from .common import BackupError
from .service import BackupStore


class BackupControl:
    def __init__(self, root, manager):
        self.store = BackupStore(root)
        self.manager = manager
        self.shutdown = None
        self.requested = False

    def check(self):
        if not os.environ.get('MYQQBOT_BACKUP_SUPERVISED') or self.shutdown is None:
            raise BackupError('当前启动方式不支持自动维护，请使用 python -m src.main 启动服务')
        if self.requested:
            raise BackupError('已有备份或还原任务正在执行')
        for feature in self.manager.features.values():
            queue = getattr(feature, 'queue', None)
            diagnostics = getattr(feature, 'diagnostics', None)
            auth = getattr(feature, 'auth', None)
            if (queue and queue.busy) or (diagnostics and diagnostics.running) or (auth and auth.running):
                raise BackupError(f'{feature.title} 正在执行任务，请完成后再备份或还原')
            if feature.name == 'ai':
                engine = feature.engine
                memory = feature.runtime.worker
                stickers = getattr(feature, 'stickers', None)
                if stickers and stickers.busy:
                    raise BackupError('表情收藏正在同步或修改，请完成后再备份或还原')
                if feature.dsh.active or engine.current or not engine.pending.empty() or engine.background or (memory and not memory.done()):
                    raise BackupError('AI 正在聊天、执行任务或整理记忆，请完成后再备份或还原')
            if feature.name == 'timing' and feature.running:
                raise BackupError('定时任务正在执行，请完成后再备份或还原')

    def request(self, action, *, name=None, mode=None):
        self.check()
        result = self.store.request(action, name=name, mode=mode)
        self.requested = True
        self.manager.maintenance = True
        return result

    async def stop(self):
        # Called after the HTTP response is sent. Uvicorn drains the current
        # requests and lifespan stops all feature workers before process exit.
        self.shutdown()
