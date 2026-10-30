from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from src.feature.atom import Feature, MessageContext
from .config import Config, Settings

logger = logging.getLogger(__name__)
USAGE = '请引用一条包含图片的消息，发送 /find-chicken；也可以把图片和指令一起发送。每次查第一张图。'
WORKER = Path(__file__).with_name('worker.mjs')


@dataclass
class SearchTask:
    ctx: MessageContext
    settings: Settings


def segments(event):
    message = event.get('message')
    return [s for s in message if isinstance(s, dict) and isinstance(s.get('data'), dict)] if isinstance(message, list) else []


def scene(ctx):
    return (ctx.message_type, str(ctx.group_id if ctx.message_type == 'group' else ctx.user_id))


class FindChickenFeature(Feature):
    name = 'find_chicken'
    title = '鱼鱼找鸡 / 图片溯源'
    description = '引用图片发送 /find-chicken，使用多引擎查询图源和候选账号，无需AI。图片会上传到所选搜图服务。'
    default_enabled = True

    def __init__(self, root: Path = Path('data/find_chicken')):
        self.root = Path(root).resolve()
        self.config = Config(self.root)
        self.enabled = True
        self.pending: deque[SearchTask] = deque()
        self.active: SearchTask | None = None
        self._worker: asyncio.Task | None = None
        self._admission = asyncio.Lock()
        self._last_use = {}

    def on_enabled_changed(self, enabled):
        self.enabled = bool(enabled)

    def agent_usage(self, owner=False):
        return {'commands': [USAGE], 'notes': '独立搜图指令，不经过AI。结果是相似图片页面线索，不保证人物身份；受网络、免费额度和风控影响。'}

    async def handle(self, ctx):
        if not re.match(r'^/find-chicken(?:\s|$)', ctx.msg):
            return False
        if ctx.msg != '/find-chicken' or ctx.message_type not in {'group', 'private'}:
            await ctx.bot.reply(ctx, USAGE)
            return True
        parts = segments(ctx.event)
        if not any(s['type'] in {'reply', 'image'} for s in parts):
            await ctx.bot.reply(ctx, USAGE)
            return True
        async with self._admission:
            settings = self.config.load()
            now = time.monotonic()
            # Bound the cooldown table without persisting QQ identifiers.
            self._last_use = {k: v for k, v in self._last_use.items() if now - v < 3600}
            key = (*scene(ctx), str(ctx.user_id))
            remaining = settings.cooldown_seconds - (now - self._last_use.get(key, float('-inf')))
            if remaining > 0:
                await ctx.bot.reply(ctx, f'刚查过，请 {int(remaining) + 1} 秒后再试。')
                return True
            if len(self.pending) + bool(self.active) >= 10:
                await ctx.bot.reply(ctx, '搜图队列已满，请稍后再试。')
                return True
            await ctx.bot.reply(ctx, '收到，开始搜图。图片会交给所选搜图引擎处理。' if not self.pending and not self.active else '已加入搜图队列，图片会交给所选搜图引擎处理。')
            self._last_use[key] = now
            self.pending.append(SearchTask(ctx, settings))
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._drain())
        return True

    async def _drain(self):
        while self.pending:
            self.active = self.pending.popleft()
            try:
                ctx = self.active.ctx
                if not self.enabled:
                    await ctx.bot.reply(ctx, '搜图功能已关闭，本次排队任务取消。')
                    continue
                async with asyncio.timeout(self.active.settings.total_timeout_seconds):
                    source = await self.image_source(ctx)
                    result = await self.search(source, self.active.settings)
                text = result.get('text') if result.get('ok') else result.get('error')
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('搜图进程未返回有效结果')
                # Treat external titles/URLs as literal text, never as QQ CQ code.
                await ctx.bot.reply(ctx, [{'type': 'text', 'data': {'text': text[:5000] + ('\n（复用缓存）' if result.get('cached') else '')}}])
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                await self._error(self.active.ctx, str(exc))
            except TimeoutError:
                await self._error(self.active.ctx, '搜图超时了，可能是网络或引擎风控，请稍后再试。')
            except Exception:
                logger.exception('图片溯源失败')
                await self._error(self.active.ctx, '搜图失败，请检查图片是否仍可访问，以及服务器网络和浏览器配置。')
            finally:
                self.active = None

    async def _error(self, ctx, text):
        try:
            await ctx.bot.reply(ctx, [{'type': 'text', 'data': {'text': text}}])
        except Exception:
            logger.warning('搜图反馈发送失败', exc_info=True)

    async def image_source(self, ctx):
        parts = segments(ctx.event)
        reply = next((s for s in parts if s['type'] == 'reply'), None)
        if reply:
            identifier = str(reply['data'].get('id') or '')
            if not re.fullmatch(r'-?\d+', identifier):
                raise ValueError('引用消息ID无效，请重新引用图片。')
            info = await ctx.bot.call_action('get_msg', {'message_id': identifier}, ctx.websocket)
            if not isinstance(info, dict) or not self.same_scene(ctx, info):
                raise ValueError('无法确认引用消息属于当前会话，请重新引用当前会话中的图片。')
            parts = segments(info)
        image = next((s['data'] for s in parts if s['type'] == 'image'), None)
        if not image:
            raise ValueError('引用的消息里没有图片。' + USAGE)
        for ref in (image.get('url'), image.get('file')):
            if isinstance(ref, str) and (re.match(r'^https?://', ref, re.I) or ref.startswith('base64://')):
                return ref
        # NapCat sometimes reports only an image resource ID. Never read its
        # server-local path, even if get_image returns one.
        ref = image.get('file')
        if isinstance(ref, str) and ref:
            info = await ctx.bot.call_action('get_image', {'file': ref}, ctx.websocket)
            url = info.get('url') if isinstance(info, dict) else None
            if isinstance(url, str) and re.match(r'^https?://', url, re.I):
                return url
        raise ValueError('没有可下载的QQ图片地址，请重新发送原图后再试。')

    @staticmethod
    def same_scene(ctx, info):
        if ctx.message_type == 'group':
            return str(info.get('group_id') or '') == str(ctx.group_id) and info.get('message_type') != 'private'
        if info.get('group_id') or info.get('message_type') == 'group':
            return False
        sender = str((info.get('sender') or {}).get('user_id') or info.get('user_id') or '')
        return sender == str(ctx.user_id) or (
            sender == str(ctx.event.get('self_id')) and str(info.get('target_id') or '') == str(ctx.user_id))

    async def search(self, source, settings):
        node = shutil.which('node')
        if not node:
            raise ValueError('搜图需要Node.js 24；请安装或重建项目Docker镜像。')
        self.root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env['FIND_CHICKEN_PROFILE_DIR'] = str(self.root / 'browser-profile')
        with tempfile.TemporaryDirectory(prefix='search-', dir=self.root) as temp_dir:
            proc = await asyncio.create_subprocess_exec(
                node, str(WORKER), str(self.root), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, start_new_session=os.name != 'nt',
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}),
            )
            job = None
            try:
                if os.name == 'nt':
                    from .process import WindowsJob
                    job = WindowsJob(proc.pid)
                payload = json.dumps({'source': source, 'settings': settings.model_dump(), 'temp_dir': temp_dir}).encode()
                output, _ = await asyncio.wait_for(proc.communicate(payload), settings.total_timeout_seconds)
                if not output or len(output) > 2 * 1024 * 1024:
                    raise RuntimeError('搜图进程未返回有效数据')
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise RuntimeError('搜图返回格式不正确')
                if not result.get('ok'):
                    logger.warning('搜图子进程失败（退出码%s）', proc.returncode)
                return result
            finally:
                # Always remove surviving Chromium descendants, including after
                # startup failures, shutdown cancellation and a hard deadline.
                if job is not None:
                    job.close()
                elif os.name == 'nt' and proc.returncode is None:
                    killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                    try:
                        await asyncio.wait_for(killer.wait(), 10)
                    except TimeoutError:
                        killer.kill()
                        await killer.wait()
                elif os.name != 'nt':
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()

    async def stop(self):
        self.pending.clear()
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    @property
    def has_active_jobs(self):
        return bool(self.pending) or self.active is not None
