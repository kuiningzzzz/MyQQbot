"""Transactional memory maintenance in disposable forks of the main chat."""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .storage import atomic_write
from .transactions import MemoryTransaction, recover
from .self_context import self_context

logger = logging.getLogger(__name__)

MAINTENANCE_RULES = '''你是从主对话fork的临时记忆维护分支，继承主对话已完成的上下文，但只执行本轮维护指令，不继续历史请求。
先读取memory-management SKILL，涉及人格再读soul-management SKILL。当前cwd/memory是可修改的完整暂存副本；正式memory只读。
使用原生读写和search_memory；索引检索的是正式记忆，编辑前必须读暂存对应文件。需要删除已合并或过时文件时调用delete_memory_file，不能删除原始聊天记录。
不能执行命令、联网、发送QQ消息、spawn，或向主对话汇报。无需受理回执、维护日志或结果消息；最终仅向宿主输出JSON：{"completed":true,"batch":"本批编号"}。
只有完成本轮全部维护才能报告completed=true；发生失败、冲突或未完成须如实报告，宿主会保留正式数据和队列。
'''

COMPRESSION_PROMPT = '''[定时任务] 记忆压缩：平扫inventory.json列出的全部暂存记忆文件，逐个读取后维护。当前日期以宿主current_time为准，不沿用历史对话的旧日期。
合并重复事实和同主题事件，清理已明确过时的计划、失去价值的细节、无意义标签和重复时间标签；保留事实的有效时间、必要来源和关系，不为缩小体积牺牲仍有用的记忆。
合并后删除空壳和已迁移完有效内容的冗余文件；不确定是否过时则保留或明确不确定性，不臆测事实。不要修改或复制原始聊天档案来绕过30天保留期。
soul也要检查冗余与矛盾，依据soul-management SKILL谨慎精简、渐进更新；不是为完成定时任务强行改变人格，不把聊天事实和成长流水账塞入人格。
'''


class MemoryRuntime:
    MEDIA_DEBOUNCE_SECONDS = 10

    def __init__(self, feature):
        self.feature = feature
        self.workspace = feature.workspace
        self.enabled = True
        self.running = False
        self.worker = self.scheduler = None
        self.last_error = ''
        self.lock = asyncio.Lock()

    def set_enabled(self, enabled):
        self.enabled = enabled

    def trigger(self):
        settings = self.feature.config.load()
        if self.running and self.enabled and settings.memory_enabled and settings.memory_auto_learn and self.ready() and (not self.worker or self.worker.done()):
            self.worker = asyncio.create_task(self._drain())

    def ready(self):
        return self.feature.journal.pending_count() >= 100 or self.feature.journal.pending_media_count() > 0

    async def _drain(self):
        try:
            while self.running and self.enabled and self.feature.config.load().memory_auto_learn and self.ready():
                if self.feature.journal.pending_count() < 100 and self.feature.journal.pending_media_count() < 100:
                    # Coalesce late results rather than paying for one memory
                    # model turn per emoji. Manual full() does not wait here.
                    await asyncio.sleep(self.MEDIA_DEBOUNCE_SECONDS)
                    if not self.running or not self.enabled or not self.feature.config.load().memory_auto_learn:
                        break
                await self.full()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('记忆整理失败，保留队列，稍后重试')

    async def full(self, date=None, current=None):
        if date:
            raise ValueError('每日整理已移除；请整理当前消息队列')
        f = self.feature
        allowed = lambda: self.enabled and f.enabled and f.config.load().memory_enabled and (current is None or current())
        async with self.lock:
            if not allowed():
                raise PermissionError('记忆整理已取消或关闭')
            rows = f.journal.rows(pending=True, limit=100)
            supplements = f.journal.media_rows(pending=True, limit=100)
            media_only = bool(supplements and len(rows) < 100)
            if media_only:
                rows = supplements
            if not rows:
                return {'success': True, 'processed': 0, 'updated_files': []}
            batch = f"{rows[0]['seq']}-{rows[-1]['seq']}"
            # Capture revisions before reading content. A result arriving during
            # this transaction remains pending after its old revision is acked.
            revisions = {r['seq']: f.journal.media_revision(r['seq']) for r in rows}
            if any(revisions.values()) or media_only:
                from .storage import digest
                batch += '-media-' + digest(json.dumps(revisions, sort_keys=True))[:16]
            processed = 0 if media_only else len(rows)
            media_updated = len(rows) if media_only else 0
            receipt = self.workspace.load_state('memory-batch-receipt', {})
            if receipt.get('batch') == batch:
                f.journal.acknowledge([] if media_only else [r['seq'] for r in rows], revisions)
                return {'success': True, **receipt, 'processed': processed, 'media_updated': media_updated}
            transaction = MemoryTransaction(self.workspace, batch, f.identity.ids)
            try:
                payload = []
                for row in rows:
                    data = f.journal_record(row)
                    payload.append({'source': str(self.workspace.memory_root / row['path']), 'record': data['record'], 'rendered_content': data.get('rendered_content', ''), 'media_supplement': media_only, 'note': '媒体解析补充，不是新的用户发言；只补充原消息的可验证信息，表情包不等于长期偏好。' if media_only else '原始QQ事件完整保存在source；媒体后台解析未完成不应猜测，完成后会另行补充；长消息应按需read原文'})
                atomic_write(transaction.root / 'batch.json', json.dumps(payload, ensure_ascii=False))
                prompt = {'batch': batch, 'count': len(rows), 'batch_file': str(transaction.root / 'batch.json'), 'memory_directory': str(transaction.root / 'memory'),
                          'instructions': '读取batch.json，覆盖本批所有人及群聊的可记忆事实，按SKILL分类；普通闲聊不必产生记忆。媒体待解析不应猜测。可从交流中自主反思人格，但无需每批修改soul。'}
                changed = await self._maintain(transaction, prompt, allowed, min(row['timestamp'] for row in rows) + 30 * 86400)
                f.journal.acknowledge([] if media_only else [r['seq'] for r in rows], revisions)
                self.workspace.save_state('index', {'dirty': True})
                self.last_error = ''
                return {'success': True, 'processed': processed, 'media_updated': media_updated, 'batch': batch, 'updated_files': changed}
            finally:
                transaction.close()

    async def _maintain(self, transaction, prompt, allowed, expires_at=None):
        f = self.feature
        prompt = {**prompt, 'current_time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
        system = f.identity.prompt() + '\n' + MAINTENANCE_RULES + '\n' + self_context(f, 'memory', cwd=transaction.root)

        async def handler(name, args):
            if not allowed():
                raise PermissionError('维护任务已撤销')
            if name == 'search_memory':
                return await f.query_memory(**args)
            if name == 'delete_memory_file':
                relative = args['path']
                formal = self.workspace.memory_path(relative)  # Validate the exact same layout.
                target = transaction.root / 'memory' / formal.relative_to(self.workspace.memory_root)
                if target.resolve() != target.absolute() or target.exists() and target.stat().st_nlink > 1:
                    raise ValueError('拒绝删除链接文件')
                if any(relative.startswith('user/' + qq + '/') for qq in f.identity.ids):
                    raise ValueError('不能为机器人维护用户画像')
                existed = target.exists()
                target.unlink(missing_ok=True)
                return {'success': True, 'deleted': existed, 'path': relative, 'staged': True}
            raise PermissionError('维护分支只能检索或删除暂存记忆')

        result = await f.dsh.run('memory', json.dumps(prompt, ensure_ascii=False), task_id=uuid.uuid4().hex,
                                 handler=handler, current=allowed, cwd=transaction.root, system=system,
                                 expires_at=expires_at, role='memory')
        if result.get('cancelled') or result.get('finish_reason') != 'completed':
            raise RuntimeError('dsh维护未完成：' + str(result.get('error') or result.get('finish_reason')))
        output = result.get('final_response', '').strip()
        if output.startswith('```'):
            output = '\n'.join(output.splitlines()[1:-1])
        done = json.loads(output)
        if not allowed() or done.get('completed') is not True or done.get('batch') != transaction.batch:
            raise RuntimeError('维护未完整完成，暂存修改不发布，队列保留')
        for file in (transaction.root / 'memory/user').glob('*'):
            if file.name in f.identity.ids:
                raise ValueError('维护尝试建立机器人自身画像')
        return transaction.commit()

    async def compact(self, current=None, instructions=COMPRESSION_PROMPT):
        f = self.feature
        allowed = lambda: self.enabled and f.enabled and f.config.load().memory_enabled and (current is None or current())
        async with self.lock:
            if not allowed():
                raise PermissionError('记忆压缩已取消或关闭')
            transaction = MemoryTransaction(self.workspace, 'compact-' + uuid.uuid4().hex, f.identity.ids, receipt='memory-compression-receipt')
            try:
                inventory = sorted(transaction.hashes)
                atomic_write(transaction.root / 'inventory.json', json.dumps(inventory, ensure_ascii=False))
                changed = await self._maintain(transaction, {'batch': transaction.batch, 'mode': 'compression',
                    'inventory_file': str(transaction.root / 'inventory.json'), 'memory_directory': str(transaction.root / 'memory'),
                    'instructions': instructions}, allowed)
                self.workspace.save_state('index', {'dirty': True})
                self.last_error = ''
                return {'success': True, 'scanned_files': len(inventory), 'updated_files': changed}
            except Exception as exc:
                self.last_error = str(exc)
                raise
            finally:
                transaction.close()

    async def tick(self):
        await asyncio.to_thread(self.feature.journal.prune)
        await self.feature.context.prune(time.time() - 30 * 86400)
        await self.feature.media.prune()
        await self.feature.dsh.prune()
        self.trigger()

    async def _schedule(self):
        while self.running:
            await asyncio.sleep(60)
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception('AI保留期清理失败')

    async def start(self):
        recover(self.workspace)
        # A hard shutdown may leave raw excerpts in temporary files. They are
        # reproducible from the archive and are never used as queue authority.
        import shutil
        stage = self.workspace.root / 'tmp/memory-stage'
        if stage.is_dir() and not stage.is_symlink() and stage.resolve().is_relative_to(self.workspace.root.resolve()):
            shutil.rmtree(stage)
        temporary = self.workspace.root / 'tmp'
        if temporary.is_dir() and not temporary.is_symlink() and temporary.resolve().is_relative_to(self.workspace.root.resolve()):
            for pattern in ('chat-*.json', 'worker-*.json'):
                for file in temporary.glob(pattern):
                    if file.is_file() and not file.is_symlink():
                        file.unlink()
        self.running = True
        await self.tick()
        self.scheduler = asyncio.create_task(self._schedule())
        self.watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self):
        # 独立于清理循环：清理本身也可能被泄漏的锁拖住。
        while self.running:
            await asyncio.sleep(30)
            try:
                self.feature.engine.watchdog()
            except Exception:
                logger.exception('聊天看门狗执行失败')

    async def stop(self):
        self.running = False
        tasks = [t for t in (self.worker, self.scheduler, getattr(self, 'watchdog_task', None)) if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
