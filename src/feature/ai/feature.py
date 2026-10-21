from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.feature.atom import Feature, MessageContext

from .identity import Identity
from .migrate import migrate
from .journal import Journal
from .dsh_runtime import DshRuntime
from .local_workspace import LocalWorkspace
from .client import ModelClient
from .config import Config
from .context import ContextManager
from .engine import ChatEngine
from .index import VectorIndex
from .media import MediaReader
from .runtime import MemoryRuntime
from .storage import Workspace
from .workfiles import WorkFiles
from .stickers import StickerLibrary
from .media import IMAGE_SUFFIXES
from .media_queue import MediaQueue, needs_media, PENDING

logger = logging.getLogger(__name__)


class AiFeature(Feature):
    name = "ai"
    title = "AI 服务"
    description = "dsh 共享对话、后台 Spawn 执行、记忆维护分支与 QQ 工具。"

    def __init__(self, root: Path = Path("data/ai"), client=None, index=None):
        self.migration = migrate(root)
        self.workspace = Workspace(root)
        self.journal = Journal(self.workspace)
        self.config = Config(root)
        self.client = client or ModelClient()
        self.identity = Identity(self.config, self.workspace)
        self.index = index or VectorIndex(self.workspace, self.config)
        self.context = ContextManager(self.workspace, self.config, self.client, self.identity.prompt)
        self.sandbox = LocalWorkspace(self.workspace.root)
        self.files = MediaReader(self.config, self.client, self.identity, self.sandbox)
        self.workfiles = WorkFiles(self)
        self.stickers = StickerLibrary(self)
        self.media = MediaQueue(self)
        self.dsh = DshRuntime(self)
        self.runtime = MemoryRuntime(self)
        self.install_memory_skill()
        self.engine = ChatEngine(self)
        self.enabled = True
        self.application_context = None
        self._record_lock = asyncio.Lock()
        self._sent = []
        self._message_ids = set()
        self._summary_locks = {}

    def bind_application_context(self, provider):
        self.application_context = provider

    def on_enabled_changed(self, enabled: bool) -> None:
        self.enabled = enabled
        self.runtime.set_enabled(enabled)
        if not enabled:
            self.engine.reset_transient()
            self.engine.active.clear()
            for key in self.engine.revisions:
                self.engine.revisions[key] += 1
            # Revoke native writes/shell too, rather than just QQ callbacks.
            for task in self.engine.tasks.values():
                task.cancel()
            if self.runtime.worker:
                self.runtime.worker.cancel()

    @staticmethod
    def scene_key(ctx) -> str:
        return f"group:{ctx.group_id}" if ctx.message_type == "group" else f"private:{ctx.user_id}"

    def observe_arrival(self, event: dict) -> None:
        self.identity.observe(event.get("self_id"))
        if self.enabled and self.config.load().chat_enabled:
            self.engine.note_arrival(event)

    async def record(self, scene_type: str, scene_id: str, qq: str, nickname: str, content: str, *, message_type="text", file_data=None, filename="", timestamp=None, message_id=None, outgoing=False, raw_event=None, defer_trigger=False) -> dict:
        if scene_type not in {"group", "private"} or not str(scene_id).isdigit() or not str(qq).isdigit():
            raise ValueError("场景和QQ号无效")
        settings = self.config.load()
        is_self = str(qq) in self.identity.ids or outgoing
        if message_type in {"image", "file"}:
            raw_event = raw_event or {'message': ([{'type': 'text', 'data': {'text': content}}] if content else []) + [{'type': message_type, 'data': {'url': str(file_data or ''), 'name': filename or ('image.png' if message_type == 'image' else 'attachment.bin')}}]}
        now = datetime.now(ZoneInfo(settings.timezone))
        record = {"id": str(message_id or uuid.uuid4()), "scene_type": scene_type, "scene_id": str(scene_id), "qq": str(qq), "nickname": nickname, "content": content, "is_self": is_self, "timestamp": timestamp or now.isoformat()}
        async with self._record_lock:
            identity = (scene_type, str(scene_id), record["id"])
            if message_id and identity in self._message_ids:
                return {"success": True, "duplicate": True}
            clock = time.monotonic()
            self._sent = [item for item in self._sent if clock - item[0] < 10]
            if is_self and not outgoing:
                match = next((item for item in self._sent if item[1:] == (scene_type, str(scene_id), content)), None)
                if match:
                    self._sent.remove(match)
                    return {"success": True, "duplicate": True}
            seq, added = self.journal.append(record, raw_event)
            if not added:
                return {"success": True, "duplicate": True}
            result = {"success": True, "seq": seq, "id": record["id"]}
            record["raw_line"] = self.workspace.format_raw(record, settings.timezone)
            await self.context.append(record)
            if message_id:
                self._message_ids.add(identity)
                if len(self._message_ids) > 10000:
                    self._message_ids = {identity}
            if outgoing:
                self._sent.append((clock, scene_type, str(scene_id), content))
        if message_type in {'image', 'file'}:
            ctx = MessageContext(bot=self.media.bot, websocket=None, event={**raw_event, 'message_id': record['id']}, msg='',
                message_type=scene_type, user_id=int(qq), group_id=int(scene_id) if scene_type == 'group' else None)
            await self._capture_media(ctx, result)
        if not defer_trigger:
            self.runtime.trigger()
        return result

    async def observe(self, ctx: MessageContext):
        self.identity.observe(ctx.event.get("self_id"))
        if ctx.message_type not in {"group", "private"}:
            return
        # Never store owner binding credentials or retired explicit-memory commands.
        if re.match(r"^/(set-master|remember|short)(?:\s|$)", ctx.msg):
            return
        segments = ctx.event.get("message") or []
        sender = ctx.event.get("sender") or {}
        timestamp = None
        if ctx.event.get("time"):
            timestamp = datetime.fromtimestamp(int(ctx.event["time"]), ZoneInfo(self.config.load().timezone)).isoformat()
        original = {k: v for k, v in ctx.event.items() if not k.startswith('_')}
        # Persist the unmodified original BEFORE network/OCR work, so a crash
        # cannot lose the event or publish a paraphrase as its original text.
        text = ctx.event.get('raw_message') or (segments if isinstance(segments, str) else '\n'.join(str(s.get('data', {}).get('text', '')) if s.get('type') == 'text' else '[' + str(s.get('type')) + ']' for s in segments))
        result = await self.record(ctx.message_type, str(ctx.group_id if ctx.message_type == "group" else ctx.user_id), str(ctx.user_id), str(sender.get("card") or sender.get("nickname") or ctx.user_id), str(text), timestamp=timestamp, message_id=ctx.event.get("message_id"), raw_event=original, defer_trigger=True)
        if result.get('duplicate'):
            return
        ctx.event.setdefault('message_id', result['id'])
        await self._capture_media(ctx, result)

    async def _capture_media(self, ctx, result):
        segments = ctx.event.get('message') or []
        try:
            content = await self._render(segments, ctx, pending=True, sequence=result['seq'])
            self.workfiles.flush()
            await asyncio.to_thread(self.journal.annotate, result['seq'], content)
            await self.context.update_content(self.scene_key(ctx), str(ctx.event.get('message_id') or result['id']), content)
            if needs_media(segments):
                if ctx.bot:
                    self.media.bind(ctx)
                self.media.enqueue(result['seq'], bool(ctx.event.get('_ai_arrival_forced')))
        finally:
            self.runtime.trigger()

    async def _render(self, segments, ctx, depth=0, *, pending=False, sequence=None, trail='') -> str:
        if isinstance(segments, str):
            return segments
        if not isinstance(segments, list) or depth > 8:
            return "[转发内容层级过深或格式无效]"
        result = []
        for index, segment in enumerate(segments):
            # Cached emoji / large forwards must also yield to incoming QQ work.
            await asyncio.sleep(0)
            if sequence is not None and not pending and not self.enabled:
                raise ConnectionError('媒体解析已暂停')
            source = self.media._source(sequence) if sequence is not None and not pending else None
            if sequence is not None and not pending and source is None:
                raise ConnectionError('媒体原文已过保留期')
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            kind = segment.get("type")
            position = trail + '/' + str(index)
            if kind == "text":
                result.append(str(data.get("text") or ""))
            elif kind == "at":
                result.append(f"@QQ:{data.get('qq', '')}")
            elif kind == "reply":
                result.append(f"[引用消息ID:{data.get('id', '')}]")
            elif kind in {"image", "file"}:
                identifier, item = self.workfiles.register(ctx, kind, data, position=position, persist=not pending)
                label = f"[{'图片' if kind == 'image' else '文件'}: {item['name']}；大小:{item['size'] or '未知'}；附件ID:{identifier}]"
                result.append(label)
                if kind == "image" or Path(item["name"]).suffix.casefold() in IMAGE_SUFFIXES | {".pdf"}:
                    if pending:
                        result.append(f'[{PENDING}；用read_media读取结果，禁止声称已看过]')
                        continue
                    try:
                        previous = json.loads(self.media.job(sequence)['results']).get(identifier) if sequence else None
                        if previous and previous['status'] == 'completed':
                            description = previous['description']
                        else:
                            url = await self.workfiles.resolve(ctx, item)
                            async with asyncio.timeout(240):
                                description = await self.files.understand(url, kind, item["name"], expires_at=source['timestamp'] + 30 * 86400 if source else None)
                        result.append(description)
                        if sequence:
                            self.media.partial(sequence, identifier, description, getattr(description, 'complete', True))
                    except Exception as exc:
                        logger.warning("QQ附件解析失败：%s", exc)
                        description = '[附件内容未能解析，可在用户要求时下载处理]'
                        result.append(description)
                        if sequence:
                            self.media.partial(sequence, identifier, description, False)
            elif kind in {"forward", "node"}:
                if pending:
                    result.append(f'[转发聊天记录；{PENDING}；消息ID:{ctx.event.get("message_id")}；可用read_media读取展开结果]')
                    continue
                children = data.get("content") or data.get("messages")
                if children is None and data.get("id"):
                    try:
                        if sequence:
                            children = await self.media.forward(sequence, str(data['id']), ctx)
                        else:
                            reply = await ctx.bot.call_action("get_forward_msg", {"message_id": data["id"]}, ctx.websocket)
                            children = reply.get("messages", [])
                    except Exception:
                        logger.exception("获取合并转发失败")
                        if sequence:
                            self.media.errors.add(sequence)
                if kind == "node":
                    rendered = await self._render(children or [], ctx, depth + 1, sequence=sequence, trail=position)
                    result.append(f"[转发 QQ:{data.get('uin', '?')} 昵称:{data.get('name', '?')}] {rendered}")
                else:
                    result.append("[转发聊天记录]")
                    for child_index, child in enumerate(children or []):
                        if not isinstance(child, dict):
                            continue
                        if child.get("type") == "node":
                            result.append("  " + await self._render([child], ctx, depth + 1, sequence=sequence, trail=position + '/' + str(child_index)))
                        else:
                            sender = child.get("sender") or {}
                            rendered = await self._render(child.get("message") or child.get("content") or [], ctx, depth + 1, sequence=sequence, trail=position + '/' + str(child_index))
                            result.append(f"  [转发 QQ:{sender.get('user_id', '?')} 昵称:{sender.get('nickname', '?')}] {rendered}")
            else:
                result.append(f"[{kind or '未知消息'}]")
        return "\n".join(result)

    async def observe_system(self, event: dict):
        kind = event.get("kind")
        if event.get("notice_type") == "group_upload" and event.get("_bot"):
            attachment = event.get("file") or {}
            message = {**event, "message_type": "group", "message": [{"type": "file", "data": {**attachment, "file_id": attachment.get("id")}}]}
            ctx = MessageContext(bot=event["_bot"], websocket=event.get("_websocket"), event=message, msg="", message_type="group", user_id=event.get("user_id"), group_id=event.get("group_id"))
            await self.observe(ctx)
        elif kind == "outgoing":
            qq = str(event.get("self_id") or self.config.load().bot_qq or "0")
            self.identity.observe(qq)
            await self.record(event["scene_type"], str(event["scene_id"]), qq, self.config.load().nickname, event["content"], outgoing=True, message_id=event.get("message_id"))
        elif kind == "disconnect":
            self.record_system("连接中断 | 消息丢失（断线期间可能有未接收消息）", event)
        elif event.get("notice_type") in {"group_recall", "friend_recall"}:
            original = "未在当前上下文找到原消息"
            target = f"group:{event['group_id']}" if event.get("group_id") else f"private:{event.get('user_id')}"
            await self.context.mark_recalled(target, event.get("message_id"))
            for key, scene in self.context.snapshot().items():
                if key != target:
                    continue
                for record in scene["recent_messages"]:
                    if record["id"] == str(event.get("message_id")):
                        original = record["content"]
            self.record_system(f"消息撤回 | 原消息:{original} | 消息ID:{event.get('message_id')}", event)

    async def handle(self, ctx):
        if ctx.msg.startswith("/short"):
            if not self.config.load().summary_enabled:
                await ctx.bot.reply(ctx, "群聊总结已关闭。")
                return True
            match = re.fullmatch(r"/short\s+(\d+)", ctx.msg)
            if ctx.message_type != "group" or not match or not 10 <= int(match[1]) <= 200:
                await ctx.bot.reply(ctx, "请在群聊发送 /short 10至200的整数")
                return True
            key = self.scene_key(ctx)
            lock = self._summary_locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                await ctx.bot.reply(ctx, "当前群正在总结，请稍候。")
                return True
            async with lock:
                records = [record for record in self.context.recent(key, int(match[1]) + 1) if not record["content"].startswith("/short")][-int(match[1]):]
                result = await self.client.complete(self.config.load(), self.identity.prompt() + "\n你是群聊总结助手，概括主要话题、结论、争议和待办，尽量150字以内，不虚构。", str(records))
                await ctx.bot.reply(ctx, result)
            return True
        if self.config.load().chat_enabled:
            return await self.engine.handle(ctx)
        return False

    async def start(self, bot):
        await self.runtime.start()
        await self.media.start(bot)
        await self.stickers.start(bot)
        if not self.config.load().vector_index_enabled:
            return
        ok, error = await asyncio.to_thread(self.index.preload)
        if ok:
            logger.info("AI 向量索引就绪：embedding 模型已加载")
        else:
            logger.warning("AI 向量索引预加载失败：%s（记忆检索已降级为纯文本检索，每 %s 秒自动重试）", error, self.index.MODEL_RETRY_SECONDS)

    async def stop(self):
        await self.stickers.stop()
        await self.media.stop()
        await self.engine.stop()
        await self.runtime.stop()
        await self.dsh.stop()

    def record_system(self, content, event):
        self.journal.append({"id": uuid.uuid4().hex, "scene_type": "system", "scene_id": "0", "qq": "0", "nickname": "system", "content": content, "is_self": True, "timestamp": datetime.now(ZoneInfo(self.config.load().timezone)).isoformat()}, {k: v for k, v in event.items() if not k.startswith("_")})

    def install_memory_skill(self):
        from .storage import atomic_write
        source = Path(__file__).with_name('skills') / 'memory-management'
        target = self.workspace.root / '.agents/skills/memory-management'
        for file in source.rglob('*'):
            if file.is_file() and file.suffix in {".md", ".py"}:
                dest = target / file.relative_to(source)
                # Ship fixes to the optional search helper, keeping the previous
                # (possibly customized) script for the administrator to recover.
                if file.relative_to(source).as_posix() == 'scripts/search.py' and dest.exists() and dest.read_text('utf-8') != file.read_text('utf-8'):
                    backup = dest.with_name('search.py.pre-upgrade.bak')
                    if not backup.exists():
                        atomic_write(backup, dest.read_text('utf-8'))
                    atomic_write(dest, file.read_text('utf-8'))
                if not dest.exists():
                    atomic_write(dest, file.read_text('utf-8'))
        # Preserve custom SKILL content; replace only the host-owned appendix.
        skill = target / 'SKILL.md'
        start, end = '<!-- qqbot:memory-layout:start -->', '<!-- qqbot:memory-layout:end -->'
        content = skill.read_text('utf-8')
        appendix = start + '\n' + Path(__file__).with_name('memory_layout.md').read_text('utf-8').rstrip() + '\n' + end
        if start in content and end in content and content.index(start) < content.index(end):
            content = content[:content.index(start)] + appendix + content[content.index(end) + len(end):]
        else:
            content = content.rstrip() + '\n\n' + appendix + '\n'
        if content != skill.read_text('utf-8'):
            atomic_write(skill, content)
        # This is shipped documentation, separate from the editable persona and
        # memory SKILL. It is visible through native read/skill, never injected.
        guide = self.workspace.root / '.agents/skills/qqbot-guide/SKILL.md'
        guide_content = ('---\nname: qqbot-guide\ndescription: QQ机器人架构、模块命令、admin功能、定时任务和工具详细使用指南；需要功能细节时按需读取。\n---\n\n'
                         + Path(__file__).with_name('self_description.md').read_text('utf-8'))
        if not guide.exists() or guide.read_text('utf-8') != guide_content:
            atomic_write(guide, guide_content)
        soul = Path(__file__).with_name('skills') / 'soul-management/SKILL.md'
        dest = self.workspace.root / '.agents/skills/soul-management/SKILL.md'
        if not dest.exists() or dest.read_text('utf-8') != soul.read_text('utf-8'):
            atomic_write(dest, soul.read_text('utf-8'))
        # Built-in operational SKILL: not a permanently injected mutation tool.
        source = Path(__file__).with_name('skills') / 'sticker-management'
        for file in source.rglob('*'):
            if file.is_file() and file.suffix in {'.md', '.py'}:
                dest = self.workspace.root / '.agents/skills/sticker-management' / file.relative_to(source)
                content = file.read_text('utf-8')
                if not dest.exists() or dest.read_text('utf-8') != content:
                    atomic_write(dest, content)

    async def query_memory(self, query, target_qq=None, scope='auto', target_group=None):
        if scope not in {'auto', 'user', 'group', 'common'} or any(value is not None and (not str(value).isascii() or not str(value).isdigit()) for value in (target_qq, target_group)):
            raise ValueError('检索范围、QQ号或群号无效')
        if not self.config.load().memory_enabled:
            return {'success': False, 'error': '记忆已关闭'}
        hits = await asyncio.to_thread(self.index.search, query, scope, str(target_qq) if target_qq is not None else None, group_id=str(target_group) if target_group is not None else None)
        hits = [h for h in hits if not any(str(h.get('source') or h.get('path') or '').startswith('user/' + qq + '/') for qq in self.identity.ids)]
        return {'success': True, 'memories': hits}

    def journal_record(self, row):
        data = self.journal.load(row)
        record = data['record']
        scene = record['scene_type'] + ':' + record['scene_id']
        cached = next((r for r in self.context.scenes.get(scene, {}).get('recent_messages', []) if r['id'] == record['id']), None)
        if cached and 'rendered_content' not in data:
            data['rendered_content'] = cached['content']
        return data
