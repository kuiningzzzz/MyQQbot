"""Background media, cache singleflight, restart and late-memory delivery."""
import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from contextlib import contextmanager

from src.feature.ai.feature import AiFeature
from src.feature.ai.media_queue import PERMANENT_ATTEMPTS, TRANSIENT_ATTEMPTS
from src.feature.atom import MessageContext


class MediaQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.f = self.feature()
        self.bot = SimpleNamespace(reply=AsyncMock(), call_action=AsyncMock(), master_service=SimpleNamespace(is_master=lambda qq: False))

    def feature(self):
        f = AiFeature(self.root)
        f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999'})
        f.sandbox.analyze = AsyncMock(return_value={'success': True, 'pages': [{'page': 1, 'text': '识别文字', 'image': 'data:image/png;base64,eA=='}]})
        f.client.turn = AsyncMock(return_value={'content': '图片中的信息'})
        f.runtime.MEDIA_DEBOUNCE_SECONDS = .02
        return f

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    @staticmethod
    def image(value='eA=='):
        return {'type': 'image', 'data': {'url': 'data:image/png;base64,' + value}}

    def ctx(self, identifier=1, segments=None, group=12):
        return MessageContext(bot=self.bot, websocket=None,
            event={'self_id': 999, 'message_id': identifier, 'message': segments or [self.image()]},
            msg='', message_type='group', user_id=123, group_id=group)

    async def until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.02)

    async def test_many_images_do_not_hold_event_intake_and_identical_images_singleflight(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def vision(*args, **kwargs):
            started.set()
            await release.wait()
            return {'content': '表情包描述'}
        self.f.client.turn.side_effect = vision
        try:
            await asyncio.wait_for(self.f.observe(self.ctx(1, [self.image()] * 25)), 1)
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(self.f.observe(self.ctx(2)), 1)
            await asyncio.wait_for(self.f.observe(self.ctx(3, [{'type': 'text', 'data': {'text': '后续文字不排队等待图片'}}])), 1)
            self.assertEqual(self.f.journal.pending_count(), 3)
            self.assertEqual(self.f.client.turn.await_count, 1)
            self.assertIn('后台解析中', self.f.journal.load(self.f.journal.rows()[0])['rendered_content'])
            self.bot.reply.assert_not_awaited()
            release.set()
            await self.until(lambda: self.f.media.statistics().get('completed') == 2)
            self.assertEqual(self.f.client.turn.await_count, 1)
            self.assertEqual(self.f.sandbox.analyze.await_count, 1)
            self.assertEqual(len(self.f.workfiles.list('group:12')), 26)
        finally:
            release.set()

    async def test_distinct_images_have_bounded_parallelism(self):
        release = asyncio.Event()
        active = maximum = 0
        async def vision(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
                return {'content': '图片'}
            finally:
                active -= 1
        self.f.client.turn.side_effect = vision
        try:
            for identifier, value in enumerate(['YQ==', 'Yg==', 'Yw==', 'ZA=='], 1):
                await self.f.observe(self.ctx(identifier, [self.image(value)]))
            await self.until(lambda: active == 2)
            self.assertEqual(maximum, 2)
            release.set()
            await self.until(lambda: self.f.media.statistics().get('completed') == 4)
            self.assertEqual(maximum, 2)
        finally:
            release.set()

    async def test_forward_expansion_is_background_and_result_tool_is_scene_bound(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def forward(*args):
            started.set()
            await release.wait()
            return {'messages': [{'sender': {'user_id': 456, 'nickname': '转发作者'}, 'message': [self.image(), {'type': 'text', 'data': {'text': '转发文字'}}]}]}
        self.bot.call_action.side_effect = forward
        ctx = self.ctx(1, [{'type': 'forward', 'data': {'id': 'fwd'}}])
        try:
            await asyncio.wait_for(self.f.observe(ctx), 1)
            await asyncio.wait_for(started.wait(), 2)
            pending = await self.f.media.read(ctx, message_id='1')
            self.assertEqual(pending['status'], 'running')
            denied = await self.f.media.read(self.ctx(group=34), message_id='1')
            self.assertEqual(denied['code'], 'message_not_found')
            release.set()
            result = await self.f.media.read(ctx, message_id='1', wait_seconds=5)
            self.assertEqual(result['status'], 'completed')
            self.assertIn('QQ:456', result['description'])
            self.assertIn('转发文字', result['description'])
            item = self.f.workfiles.list('group:12')[0]
            single = await self.f.media.read(ctx, attachment_id=item['attachment_id'])
            self.assertIn('图片中的信息', single['description'])
            raw = self.f.journal.load(self.f.journal.rows()[0])
            self.assertEqual(raw['event']['message'], ctx.event['message'])
            self.assertNotIn('图片中的信息', raw['record']['content'])
            self.bot.reply.assert_not_awaited()
        finally:
            release.set()

    async def test_restart_resumes_interrupted_job_and_reuses_completed_parts(self):
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def vision(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                started.set()
                await release.wait()
            return {'content': '已识别图片' + str(count)}
        self.f.client.turn.side_effect = vision
        ctx = self.ctx(1, [self.image('YQ=='), self.image('Yg==')])
        await self.f.observe(ctx)
        await asyncio.wait_for(started.wait(), 3)
        source = self.f.journal.rows()[0]
        path = self.f.workspace.memory_root / source['path']
        original = path.read_bytes()
        await self.f.stop()
        self.f = self.feature()
        await self.f.media.start(self.bot)
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)
        self.assertEqual(self.f.client.turn.await_count, 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.f.journal.pending_count(), 1)
        # The process restart did not discard the content cache either.
        await self.f.files.understand('data:image/png;base64,YQ==')
        self.assertEqual(self.f.client.turn.await_count, 1)

    async def test_crash_between_raw_archive_and_enqueue_recovers(self):
        ctx = self.ctx()
        await self.f.record('group', '12', '123', '甲', '[image]', message_id=1, raw_event=ctx.event)
        self.assertEqual(self.f.media.statistics(), {})
        await self.f.media.start(self.bot)
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)
        self.assertIn('图片中的信息', self.f.journal.load(self.f.journal.rows()[0])['rendered_content'])

    async def test_failed_analysis_is_not_cached_and_cache_survives_restart(self):
        self.f.client.turn.side_effect = [RuntimeError('临时失败'), {'content': '恢复成功'}]
        first = await self.f.files.understand('data:image/png;base64,eA==')
        self.assertFalse(first.complete)
        self.assertIn('识别文字', first)
        second = await self.f.files.understand('data:image/png;base64,eA==')
        self.assertTrue(second.complete)
        await self.f.files.understand('data:image/png;base64,eA==')
        self.assertEqual(self.f.client.turn.await_count, 2)
        await self.f.stop()
        self.f = self.feature()
        cached = await self.f.files.understand('data:image/png;base64,eA==')
        self.assertIn('恢复成功', cached)
        self.f.client.turn.assert_not_awaited()
        self.f.config.save({'ocr_enabled': False})
        await self.f.files.understand('data:image/png;base64,eA==')
        self.f.client.turn.assert_awaited_once()

    async def test_late_media_during_memory_commit_gets_its_own_retryable_batch(self):
        for identifier in range(100):
            await self.f.record('group', '12', '123', '甲', '文字', message_id=identifier)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def memory(kind, prompt, **kwargs):
            data = json.loads(prompt)
            payload = json.loads(Path(data['batch_file']).read_text('utf-8'))
            calls.append(payload)
            if len(calls) == 1:
                started.set()
                await release.wait()
            if len(calls) == 2:
                raise RuntimeError('模拟整理失败')
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': data['batch']})}
        self.f.dsh.run = memory
        batch = asyncio.create_task(self.f.runtime.full())
        try:
            await asyncio.wait_for(started.wait(), 3)
            self.f.journal.annotate(1, '晚到的OCR事实', media_update=True)
            release.set()
            self.assertEqual((await batch)['processed'], 100)
            self.assertEqual(self.f.journal.pending_count(), 0)
            self.assertEqual(self.f.journal.pending_media_count(), 1)
            await self.f.record('group', '12', '123', '甲', '新文字', message_id=100)
            with self.assertRaises(RuntimeError):
                await self.f.runtime.full()
            self.assertEqual(self.f.journal.pending_media_count(), 1)
            result = await self.f.runtime.full()
            self.assertEqual((result['processed'], result['media_updated']), (0, 1))
            self.assertTrue(calls[-1][0]['media_supplement'])
            self.assertEqual(calls[-1][0]['rendered_content'], '晚到的OCR事实')
            self.assertEqual(self.f.journal.pending_count(), 1)
            self.assertEqual(self.f.journal.pending_media_count(), 0)
        finally:
            release.set()
            await asyncio.gather(batch, return_exceptions=True)

    async def test_consumed_media_revision_does_not_swallow_a_newer_revision(self):
        await self.f.record('group', '12', '123', '甲', '[image]', message_id=1)
        self.f.journal.annotate(1, '失败的OCR补充', media_update=True)
        old = self.f.journal.media_rows()[0]
        self.f.journal.annotate(1, '明确重试后成功', media_update=True)
        self.f.journal.acknowledge([1], {1: old['media_revision']})
        self.assertEqual(self.f.journal.pending_media_count(), 1)
        self.assertEqual(len(self.f.journal.media_rows(after=old['media_update_id'])), 1)
        self.f.journal.acknowledge([], {1: self.f.journal.media_revision(1)})
        self.assertEqual(self.f.journal.pending_media_count(), 0)

    async def test_late_media_is_automatically_integrated_below_100_messages(self):
        await self.f.record('group', '12', '123', '甲', '原消息', message_id=1)
        self.f.journal.acknowledge([1])
        await self.f.record('group', '12', '123', '甲', '未满100条的新消息', message_id=2)
        self.f.journal.annotate(1, '媒体补充', media_update=True)
        async def memory(kind, prompt, **kwargs):
            request = json.loads(prompt)
            payload = json.loads(Path(request['batch_file']).read_text('utf-8'))
            self.assertEqual(request['count'], 1)
            self.assertTrue(payload[0]['media_supplement'])
            return {'finish_reason': 'completed', 'final_response': json.dumps({'completed': True, 'batch': request['batch']})}
        self.f.dsh.run = AsyncMock(side_effect=memory)
        self.f.config.save({'memory_auto_learn': True})
        await self.f.runtime.start()
        await self.until(lambda: self.f.journal.pending_media_count() == 0)
        self.assertEqual(self.f.journal.pending_count(), 1)
        self.f.dsh.run.assert_awaited_once()

    async def test_failure_backoff_is_bounded_and_explicit_retry_can_recover(self):
        self.f.client.turn.side_effect = RuntimeError('模型暂不可用')
        ctx = self.ctx()
        await self.f.observe(ctx)
        for attempt in (1, 2):
            await self.until(lambda: self.f.media.job(1)['attempts'] == attempt)
            with self.f.media.connect() as db:
                db.execute('UPDATE jobs SET due=0 WHERE seq=1')
            self.f.media.wake.set()
        await self.until(lambda: self.f.media.job(1)['status'] == 'failed')
        self.assertEqual(self.f.client.turn.await_count, 3)
        old = self.f.journal.media_rows()[0]
        self.assertIn('识别文字', self.f.journal.load(self.f.journal.rows()[0])['rendered_content'])
        self.f.client.turn.side_effect = None
        result = await self.f.media.read(ctx, message_id='1', retry=True, wait_seconds=5)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.f.client.turn.await_count, 4)
        self.assertEqual(len(self.f.journal.media_rows(after=old['media_update_id'])), 1)

    async def test_chat_receives_late_media_once_without_new_user_message(self):
        await self.f.record('group', '12', '123', '甲', '[image]', message_id=1)
        feeds = []
        self.f.dsh.has_session = lambda kind: True
        async def chat(kind, prompt, **kwargs):
            data = json.loads(prompt)
            feeds.append((data, json.loads(Path(data['new_messages_file']).read_text('utf-8'))))
            return {'finish_reason': 'completed'}
        self.f.dsh.run = chat
        ctx = self.ctx(2, [{'type': 'text', 'data': {'text': '继续聊天'}}])
        await self.f.engine._turn(ctx, 'group:12', True)
        self.f.journal.annotate(1, '现在图片已解析', media_update=True)
        await self.f.engine._turn(ctx, 'group:12', True)
        self.assertEqual(feeds[-1][0]['new_message_count'], 0)
        self.assertEqual(feeds[-1][0]['media_update_count'], 1)
        self.assertTrue(feeds[-1][1][0]['media_supplement'])
        await self.f.engine._turn(ctx, 'group:12', True)
        self.assertEqual(feeds[-1][0]['media_update_count'], 0)
        self.bot.reply.assert_not_awaited()

    async def test_expired_sources_jobs_updates_and_cache_are_removed(self):
        old = datetime.fromtimestamp(time.time() - 31 * 86400, timezone.utc).isoformat()
        row = await self.f.record('group', '12', '123', '甲', '[image]', message_id=1, timestamp=old)
        self.assertFalse(self.f.journal.annotate(row['seq'], '过期补充', media_update=True))
        self.f.media.enqueue(row['seq'])
        self.f.journal.prune()
        await self.f.media.prune()
        self.assertEqual(self.f.media.statistics(), {})
        cache = self.f.files.cache
        cache.put('old', '测试', time.time() + 1)
        cache.prune(time.time() + 2)
        self.assertIsNone(cache.get('old'))
        self.assertEqual(self.f.journal.media_rows(), [])

    async def test_file_field_pdf_name_is_queued_and_pending_attachment_can_be_paged(self):
        pdf = {'type': 'file', 'data': {'file': 'scan.pdf', 'url': 'data:application/pdf;base64,eA=='}}
        await self.f.observe(self.ctx(1, [pdf]))
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)
        self.assertEqual(self.f.client.turn.await_count, 1)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def description(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                await release.wait()
            return '甲' * 15000
        self.f.files.understand = description
        ctx = self.ctx(2, [self.image('YQ=='), self.image('Yg==')])
        try:
            await self.f.observe(ctx)
            await asyncio.wait_for(started.wait(), 3)
            item = next(i for i in self.f.workfiles.list('group:12') if i['message_id'] == '2')
            first = await self.f.media.read(ctx, attachment_id=item['attachment_id'])
            self.assertEqual(first['next_offset'], 12000)
            self.assertIsNone(first['rendered_path'])
            rest = await self.f.media.read(ctx, attachment_id=item['attachment_id'], offset=first['next_offset'])
            self.assertEqual(len(rest['description']), 3000)
            self.assertFalse(rest['truncated'])
        finally:
            release.set()

    async def test_shutdown_cannot_be_reopened_by_an_inflight_tool(self):
        await self.f.record('group', '12', '123', '甲', '[image]', message_id=1, raw_event=self.ctx().event)
        await self.f.media.stop()
        self.f.media.enqueue(1)
        self.assertEqual(self.f.media.workers, [])
        self.assertEqual((await self.f.media.read(self.ctx(), message_id='1'))['code'], 'cancelled')
        await self.f.media.start(self.bot)
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)

    async def test_disabling_ai_pauses_remaining_images_and_resumes_without_redoing_parts(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def vision(*args, **kwargs):
            started.set()
            await release.wait()
            return {'content': '图片结果'}
        self.f.client.turn.side_effect = vision
        ctx = self.ctx(1, [self.image('YQ=='), self.image('Yg=='), self.image('Yw==')])
        try:
            await self.f.observe(ctx)
            await asyncio.wait_for(started.wait(), 3)
            self.f.on_enabled_changed(False)
            release.set()
            await self.until(lambda: self.f.media.job(1)['status'] in {'queued', 'retry'})
            self.assertEqual(self.f.client.turn.await_count, 1)
            self.assertEqual(self.f.journal.media_rows(), [])
            self.f.on_enabled_changed(True)
            result = await self.f.media.read(ctx, message_id='1', wait_seconds=5)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(self.f.client.turn.await_count, 3)
        finally:
            release.set()

    async def test_pruning_does_not_delete_jobs_enqueued_after_its_source_snapshot(self):
        await self.f.record('group', '12', '123', '甲', '第一条', message_id=1)
        original = self.f.media.connect
        injected = False
        @contextmanager
        def racing_connect():
            nonlocal injected
            if not injected:
                injected = True
                self.f.journal.append({'id': '2', 'scene_type': 'group', 'scene_id': '12', 'qq': '123',
                    'nickname': '甲', 'content': '[image]', 'timestamp': datetime.now(timezone.utc).isoformat(), 'is_self': False}, self.ctx(2).event)
                with original() as db:
                    db.execute('INSERT INTO jobs(seq) VALUES (2)')
            with original() as db:
                yield db
        with patch.object(self.f.media, 'connect', racing_connect):
            await self.f.media.prune()
        self.assertIsNotNone(self.f.media.job(2))


    async def test_connection_errors_are_bounded_and_never_retry_forever(self):
        """旧图片链接会永久失效：整段渲染抛 ConnectionError 时也必须有上限。"""
        original = self.f._render
        calls = {'n': 0}

        async def render(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] > 1:  # 第一次是 observe 的占位渲染，之后是队列的重试
                raise ConnectionError('链接已失效')
            return await original(*args, **kwargs)

        self.f._render = render
        await self.f.observe(self.ctx())
        for attempt in range(1, 6):
            await self.until(lambda: self.f.media.job(1)['attempts'] == attempt, timeout=20)
            with self.f.media.connect() as db:
                db.execute('UPDATE jobs SET due=0 WHERE seq=1')
            self.f.media.wake.set()
        await self.until(lambda: self.f.media.job(1)['status'] == 'failed', timeout=20)
        self.assertEqual(self.f.media.job(1)['attempts'], 6)
        self.assertEqual(self.f.media.job(1)['error'], 'ConnectionError')
        # 放弃后要把原文里的“解析中”改成失败提示（写盘在状态更新之后，需轮询）。
        await self.until(lambda: '媒体后台解析失败' in self.f.journal.load(self.f.journal.rows()[0]).get('rendered_content', ''), timeout=10)


    async def test_over_limit_job_is_finalized_without_retrying(self):
        """超过重试上限的任务必须直接收尾，不再解析、也不能让 worker 退出。"""
        await self.f.media.start(self.bot)
        self.f._render = AsyncMock(side_effect=ConnectionError('链接已失效'))
        with self.f.media.connect() as db:
            db.execute("INSERT INTO jobs(seq,status,attempts,due,priority) VALUES(1,'retry',?,0,0)", (TRANSIENT_ATTEMPTS,))
        self.f.media.wake.set()
        await self.until(lambda: self.f.media.job(1)['status'] == 'failed', timeout=10)
        self.assertEqual(self.f.media.job(1)['attempts'], TRANSIENT_ATTEMPTS)
        self.assertEqual(self.f._render.await_count, 0)
        self.assertFalse([worker for worker in self.f.media.workers if worker.done()])

    async def test_finalize_failed_swallows_annotation_errors(self):
        """收尾写盘再出错也必须吞掉：逃出去会杀死 worker 并让任务停在 running。"""
        self.f.media._source = lambda seq: {'seq': seq}
        self.f.journal.load = Mock(return_value={'rendered_content': '媒体后台解析中'})
        self.f.journal.annotate = Mock(side_effect=ConnectionError('写盘失败'))
        with self.f.media.connect() as db:
            db.execute("INSERT INTO jobs(seq,status,attempts,due,priority) VALUES(1,'running',?,0,0)", (PERMANENT_ATTEMPTS,))
        await self.f.media._finalize_failed(1, 'ConnectionError')  # 不应抛出
        self.assertEqual(self.f.media.job(1)['status'], 'failed')



    async def test_disconnected_napcat_waits_without_burning_retries(self):
        """重启窗口里 NapCat 还没连上：任务应等待，不消耗重试次数。"""
        self.bot.connected_clients = set()
        await self.f.observe(self.ctx())
        await asyncio.sleep(1.5)
        job = self.f.media.job(1)
        self.assertIsNotNone(job)
        self.assertEqual(job['status'], 'retry')
        self.assertEqual(job['attempts'], 0)


if __name__ == '__main__':
    unittest.main()

