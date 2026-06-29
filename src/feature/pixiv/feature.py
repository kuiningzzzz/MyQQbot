import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.bot import MessageContext
from src.feature.atom import Feature, batched
from src.feature.pixiv.client import (
    PixivAuthenticationRequired,
    PixivClient,
    PixivDownloadError,
    PixivWork,
)
from src.feature.pixiv.config import PixivConfig

logger = logging.getLogger(__name__)


@dataclass
class PixivQueueTask:
    sequence: int
    illust_id: str
    request_time: datetime
    ctx: MessageContext
    task_dir: Path
    announced_start: bool = False

    @property
    def image_dir(self) -> Path:
        return self.task_dir / "images"


class PixivFeature(Feature):
    name = "pixiv"
    title = "Pixiv 图片"
    description = "收到 /pixiv 作品ID 后下载对应图片，并以 QQ 聊天记录发送。"
    default_enabled = False

    command_pattern = re.compile(r"^/pixiv\s+(\d+)$")
    forward_batch_size = 100

    def __init__(
        self,
        client: PixivClient | None = None,
        config_path: Path | None = None,
    ):
        self.root_dir = Path("data/pixiv")
        self.tasks_dir = self.root_dir / "tasks"
        self.cleanup_delay_seconds = int(
            os.getenv("PIXIV_CLEANUP_DELAY_SECONDS", "300")
        )
        self.config = PixivConfig(config_path or self.root_dir / "config.json")
        self.client = client or PixivClient(self.config.load_refresh_token())
        self._next_sequence = 1
        self._active_task: PixivQueueTask | None = None
        self._pending_tasks: list[PixivQueueTask] = []
        self._worker_task: asyncio.Task | None = None

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg == "/pixiv status":
            await self.send_status(ctx)
            return True

        match = self.command_pattern.match(ctx.msg)
        if match is None:
            return False

        task = self.create_task(match.group(1), ctx)
        was_idle = self._active_task is None and not self._pending_tasks
        self._pending_tasks.append(task)
        self.ensure_worker()

        if was_idle:
            await ctx.bot.reply(
                ctx,
                f"开始处理 Pixiv {task.illust_id}，下载图片可能需要一点时间。",
            )
            task.announced_start = True
        else:
            await ctx.bot.reply(ctx, "已添加至处理队列。")
            await self.send_status(ctx)

        return True

    def create_task(self, illust_id: str, ctx: MessageContext) -> PixivQueueTask:
        sequence = self._next_sequence
        self._next_sequence += 1
        request_time = datetime.now()
        task_dir = self.tasks_dir / (
            f"{request_time:%Y%m%d%H%M%S}-{sequence}-{illust_id}"
        )
        return PixivQueueTask(
            sequence=sequence,
            illust_id=illust_id,
            request_time=request_time,
            ctx=ctx,
            task_dir=task_dir,
        )

    def ensure_worker(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self.run_queue())

    async def run_queue(self):
        while self._pending_tasks:
            task = self._pending_tasks.pop(0)
            self._active_task = task
            try:
                await self.process_task(task)
            finally:
                self._active_task = None

    async def process_task(self, task: PixivQueueTask):
        try:
            if not task.announced_start:
                await task.ctx.bot.reply(
                    task.ctx,
                    f"开始处理 Pixiv {task.illust_id}，下载图片可能需要一点时间。",
                )
                task.announced_start = True

            work = await asyncio.to_thread(
                self.client.download_work,
                task.illust_id,
                task.image_dir,
            )
            await self.send_work(task, work)
            self.schedule_task_cleanup(task)
        except PixivAuthenticationRequired as exc:
            logger.warning("访问 Pixiv %s 需要认证: %s", task.illust_id, exc)
            await task.ctx.bot.reply(
                task.ctx,
                "该作品不存在、不可见或需要登录访问。若要获取 R18 作品，请配置 "
                "管理 WebUI 中的 Pixiv refresh token。",
            )
            self.schedule_task_cleanup(task)
        except PixivDownloadError as exc:
            logger.error("下载 Pixiv %s 失败: %s", task.illust_id, exc)
            await task.ctx.bot.reply(task.ctx, f"Pixiv {task.illust_id} 获取失败：{exc}")
            self.schedule_task_cleanup(task)
        except Exception:
            logger.exception("处理 Pixiv %s 失败", task.illust_id)
            await task.ctx.bot.reply(
                task.ctx,
                f"Pixiv {task.illust_id} 下载或发送失败，请查看后端日志。",
            )
            self.schedule_task_cleanup(task)

    async def send_work(self, task: PixivQueueTask, work: PixivWork):
        image_refs = [
            task.ctx.bot.resolve_public_file_reference(
                image_path,
                self.root_dir,
                "/pixiv-files",
            )
            for image_path in work.image_paths
        ]
        batch_count = (len(image_refs) + self.forward_batch_size - 1) // self.forward_batch_size
        await task.ctx.bot.reply(
            task.ctx,
            f"Pixiv {work.illust_id}《{work.title}》/ {work.author}，共 "
            f"{len(image_refs)} 张图片，将发送 {batch_count} 组聊天记录。",
        )
        for index, image_batch in enumerate(
            batched(image_refs, self.forward_batch_size),
            start=1,
        ):
            await task.ctx.bot.send_image_forward_msg(task.ctx, image_batch)
            logger.info(
                "已发送 Pixiv %s 第 %s/%s 批，共 %s 张",
                work.illust_id,
                index,
                batch_count,
                len(image_batch),
            )

    async def send_status(self, ctx: MessageContext):
        tasks = self.current_tasks()
        if not tasks:
            await ctx.bot.reply(ctx, "当前 Pixiv 任务队列为空")
            return

        messages: list[str] = ["Pixiv 当前任务队列"]
        for index, (status, task) in enumerate(tasks, start=1):
            messages.append(
                "\n".join(
                    [
                        f"序号：{index}",
                        f"状态：{status}",
                        f"请求时间：{task.request_time:%Y-%m-%d %H:%M:%S}",
                        f"作品ID：{task.illust_id}",
                    ]
                )
            )
        await ctx.bot.send_forward_msg(ctx, messages)

    def current_tasks(self) -> list[tuple[str, PixivQueueTask]]:
        tasks: list[tuple[str, PixivQueueTask]] = []
        if self._active_task is not None:
            tasks.append(("处理中", self._active_task))
        tasks.extend(("待处理", task) for task in self._pending_tasks)
        return tasks

    def schedule_task_cleanup(self, task: PixivQueueTask):
        asyncio.create_task(self.cleanup_task_later(task))

    async def cleanup_task_later(self, task: PixivQueueTask):
        try:
            if self.cleanup_delay_seconds > 0:
                await asyncio.sleep(self.cleanup_delay_seconds)
            await asyncio.to_thread(self.cleanup_task_dir, task.task_dir)
        except Exception:
            logger.exception("清理 Pixiv 任务目录失败: %s", task.task_dir)

    def cleanup_task_dir(self, task_dir: Path):
        if task_dir.exists():
            shutil.rmtree(task_dir)
            logger.info("已清理 Pixiv 任务目录: %s", task_dir)

    @property
    def has_active_jobs(self) -> bool:
        return self._active_task is not None

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.client.refresh_token)

    def save_refresh_token(self, refresh_token: str) -> None:
        self.config.save_refresh_token(refresh_token)
        self.client.set_refresh_token(refresh_token)

    def clear_refresh_token(self) -> None:
        self.config.clear_refresh_token()
        self.client.set_refresh_token(None)

    def cleanup_dirs(self):
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        protected = {task.task_dir.resolve() for _, task in self.current_tasks()}
        for child in self.tasks_dir.iterdir():
            if not child.is_dir():
                child.unlink()
                continue
            if child.resolve() in protected:
                continue
            shutil.rmtree(child)
        logger.info("已清理 Pixiv 空闲任务目录: %s", self.tasks_dir)
