import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from src.bot import MessageContext
from src.feature.base import Feature

logger = logging.getLogger(__name__)


class JmPdfFeature(Feature):
    name = "jm_pdf"
    title = "JM 漫画 PDF"
    description = "收到 /jm 号码 后下载对应 JM 漫画，导出 PDF 并发送到当前会话。"
    default_enabled = False

    command_pattern = re.compile(r"^/jm\s+(\d+)$")

    def __init__(self):
        self.output_dir = Path("data/jm/pdfs")
        self.download_dir = Path("data/jm/downloads")
        self.cleanup_delay_seconds = int(os.getenv("JM_CLEANUP_DELAY_SECONDS", "300"))
        self._active_jobs = 0
        self._cleanup_task: asyncio.Task | None = None

    async def handle(self, ctx: MessageContext) -> bool:
        match = self.command_pattern.match(ctx.msg)
        if match is None:
            return False

        album_id = match.group(1)
        await ctx.bot.reply(ctx, f"开始处理 JM{album_id}，下载和生成 PDF 可能需要一点时间。")
        self._active_jobs += 1

        try:
            pdf_path = await asyncio.to_thread(self.download_pdf, album_id)

            await ctx.bot.reply(ctx, f"JM{album_id} 已生成 PDF，正在发送文件。")
            await ctx.bot.send_file(ctx, pdf_path)
            self.schedule_cleanup()
            return True
        except ImportError:
            await ctx.bot.reply(
                ctx,
                "JM 功能依赖 jmcomic，请先安装 requirements.txt 里的依赖后再试。",
            )
            return True
        except Exception:
            logger.exception("下载 JM%s 失败", album_id)
            await ctx.bot.reply(ctx, f"JM{album_id} 下载或导出失败，请查看后端日志。")
            return True
        finally:
            self._active_jobs -= 1

    def download_pdf(self, album_id: str) -> Path:
        from jmcomic import Feature as JmFeature
        from jmcomic import create_option_by_str, download_album

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        before = self._existing_pdfs()
        option = create_option_by_str(
            f"""
dir_rule:
  base_dir: {self.download_dir.resolve().as_posix()}
"""
        )
        extra = JmFeature.export_pdf(
            pdf_dir=str(self.output_dir.resolve()),
            delete_original_file=True,
        )

        download_album(album_id, option, extra=extra)

        created = self._existing_pdfs() - before
        if created:
            return max(created, key=lambda path: path.stat().st_mtime)

        candidates = list(self.output_dir.glob(f"*{album_id}*.pdf"))
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)

        raise FileNotFoundError(f"JM{album_id} PDF was not generated")

    def _existing_pdfs(self) -> set[Path]:
        if not self.output_dir.exists():
            return set()
        return {path for path in self.output_dir.glob("*.pdf") if path.is_file()}

    def schedule_cleanup(self):
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()

        self._cleanup_task = asyncio.create_task(self.cleanup_later())

    @property
    def has_active_jobs(self) -> bool:
        return self._active_jobs > 0

    async def cleanup_later(self):
        try:
            while True:
                if self.cleanup_delay_seconds > 0:
                    await asyncio.sleep(self.cleanup_delay_seconds)
                elif self._active_jobs > 0:
                    await asyncio.sleep(1)

                if self._active_jobs == 0:
                    break

            await asyncio.to_thread(self.cleanup_dirs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("清理 JM 临时目录失败")

    def cleanup_dirs(self):
        for directory in (self.download_dir, self.output_dir):
            directory.mkdir(parents=True, exist_ok=True)
            for child in directory.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

        logger.info("已清空 JM 临时目录: %s, %s", self.download_dir, self.output_dir)
