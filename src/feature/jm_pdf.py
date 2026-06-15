import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.bot import MessageContext
from src.feature.base import Feature

logger = logging.getLogger(__name__)


@dataclass
class JmQueueTask:
    sequence: int
    album_id: str
    request_time: datetime
    ctx: MessageContext
    task_dir: Path
    announced_start: bool = False

    @property
    def download_dir(self) -> Path:
        return self.task_dir / "downloads"

    @property
    def output_dir(self) -> Path:
        return self.task_dir / "pdfs"


class JmPdfFeature(Feature):
    name = "jm_pdf"
    title = "JM 漫画 PDF"
    description = "收到 /jm 号码 后下载对应 JM 漫画，导出 PDF 并发送到当前会话。"
    default_enabled = False

    command_pattern = re.compile(r"^/jm\s+(\d+)$")

    def __init__(self):
        self.root_dir = Path("data/jm")
        self.tasks_dir = self.root_dir / "tasks"
        self.cleanup_delay_seconds = int(os.getenv("JM_CLEANUP_DELAY_SECONDS", "300"))
        self._next_sequence = 1
        self._active_task: JmQueueTask | None = None
        self._pending_tasks: list[JmQueueTask] = []
        self._worker_task: asyncio.Task | None = None

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg == "/jm status":
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
            await ctx.bot.reply(ctx, f"开始处理 JM{task.album_id}，下载和生成 PDF 可能需要一点时间。")
            task.announced_start = True
        else:
            await ctx.bot.reply(ctx, "已添加至处理队列。")
            await self.send_status(ctx)

        return True

    def create_task(self, album_id: str, ctx: MessageContext) -> JmQueueTask:
        sequence = self._next_sequence
        self._next_sequence += 1
        request_time = datetime.now()
        task_dir = self.tasks_dir / f"{request_time:%Y%m%d%H%M%S}-{sequence}-{album_id}"
        return JmQueueTask(
            sequence=sequence,
            album_id=album_id,
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

    async def process_task(self, task: JmQueueTask):
        try:
            if not task.announced_start:
                await task.ctx.bot.reply(
                    task.ctx,
                    f"开始处理 JM{task.album_id}，下载和生成 PDF 可能需要一点时间。",
                )
                task.announced_start = True

            pdf_path = await asyncio.to_thread(self.download_pdf, task)

            await task.ctx.bot.reply(task.ctx, f"JM{task.album_id} 已生成 PDF，正在发送文件。")
            await task.ctx.bot.send_file(task.ctx, pdf_path)
            self.schedule_task_cleanup(task)
        except ImportError:
            await task.ctx.bot.reply(
                task.ctx,
                "JM 功能依赖 jmcomic，请先安装 requirements.txt 里的依赖后再试。",
            )
        except Exception:
            logger.exception("下载 JM%s 失败", task.album_id)
            await task.ctx.bot.reply(task.ctx, f"JM{task.album_id} 下载或导出失败，请查看后端日志。")

    async def send_status(self, ctx: MessageContext):
        tasks = self.current_tasks()
        if not tasks:
            await ctx.bot.reply(ctx, "当前任务队列为空")
            return

        messages: list[str] = ["JM 当前任务队列"]
        for index, (status, task) in enumerate(tasks, start=1):
            messages.append(
                "\n".join(
                    [
                        f"序号：{index}",
                        f"状态：{status}",
                        f"请求时间：{task.request_time:%Y-%m-%d %H:%M:%S}",
                        f"漫画ID：{task.album_id}",
                    ]
                )
            )

        await ctx.bot.send_forward_msg(ctx, messages)

    def current_tasks(self) -> list[tuple[str, JmQueueTask]]:
        tasks: list[tuple[str, JmQueueTask]] = []
        if self._active_task is not None:
            tasks.append(("处理中", self._active_task))
        tasks.extend(("待处理", task) for task in self._pending_tasks)
        return tasks

    def download_pdf(self, task: JmQueueTask) -> Path:
        from jmcomic import Feature as JmFeature
        from jmcomic import create_option_by_str, download_album

        task.output_dir.mkdir(parents=True, exist_ok=True)
        task.download_dir.mkdir(parents=True, exist_ok=True)

        before = self._existing_pdfs(task.output_dir)
        option = create_option_by_str(
            f"""
dir_rule:
  base_dir: {task.download_dir.resolve().as_posix()}
"""
        )
        extra = JmFeature.export_pdf(
            pdf_dir=str(task.output_dir.resolve()),
            delete_original_file=True,
        )

        download_album(task.album_id, option, extra=extra)

        created = self._existing_pdfs(task.output_dir) - before
        if created:
            return max(created, key=lambda path: path.stat().st_mtime)

        candidates = list(task.output_dir.glob(f"*{task.album_id}*.pdf"))
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)

        raise FileNotFoundError(f"JM{task.album_id} PDF was not generated")

    def _existing_pdfs(self, output_dir: Path) -> set[Path]:
        if not output_dir.exists():
            return set()
        return {path for path in output_dir.glob("*.pdf") if path.is_file()}

    def schedule_task_cleanup(self, task: JmQueueTask):
        asyncio.create_task(self.cleanup_task_later(task))

    async def cleanup_task_later(self, task: JmQueueTask):
        try:
            if self.cleanup_delay_seconds > 0:
                await asyncio.sleep(self.cleanup_delay_seconds)
            await asyncio.to_thread(self.cleanup_task_dir, task.task_dir)
        except Exception:
            logger.exception("清理 JM 任务目录失败: %s", task.task_dir)

    def cleanup_task_dir(self, task_dir: Path):
        if task_dir.exists():
            shutil.rmtree(task_dir)
            logger.info("已清理 JM 任务目录: %s", task_dir)

    @property
    def has_active_jobs(self) -> bool:
        return self._active_task is not None

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

        logger.info("已清理 JM 空闲任务目录: %s", self.tasks_dir)
