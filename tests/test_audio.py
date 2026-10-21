"""Cloud audio contracts, asynchronous admission, QQ authority and bounded storage."""
import asyncio
import base64
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.feature.ai.admin import AiAdmin
from src.feature.ai.audio import AudioError, check_audio
from src.feature.ai.feature import AiFeature
from src.feature.atom import MessageContext
from src.backup.archive import environment


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\0\0' * 2400)
    return out.getvalue()


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'unit-audio-secret', 'AI_AUDIO_ENABLED': 'true', 'AI_VOICE_ENABLED': 'true'})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.f = AiFeature(Path(self.temp.name))
        self.f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999', 'nickname': '小鱼'})
        self.bot = SimpleNamespace(reply=AsyncMock(), call_action=AsyncMock(return_value={'message_id': 'voice-ack'}), master_service=SimpleNamespace(is_master=lambda qq: False))
        self.data = wav_bytes()
        self.f.audio._json_request = AsyncMock(return_value={'output': {'audio': {'url': 'https://example.com/output.wav'}}})
        self.f.files._download = AsyncMock(return_value=self.data)
        self.f.audio._understand = AsyncMock(return_value='【语音原文】小鱼，明天提醒我。\n【声音观察】背景有雨声。')
        self.f.engine._start_worker = lambda: None

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()
        self.env.stop()

    def ctx(self, *, identifier=1, group=12, private=False, segments=None):
        return MessageContext(bot=self.bot, websocket=None, msg='', message_type='private' if private else 'group',
            user_id=123, group_id=None if private else group, event={'post_type': 'message', 'message_type': 'private' if private else 'group',
            'user_id': 123, 'group_id': None if private else group, 'message_id': identifier, 'self_id': 999,
            'message': segments if segments is not None else [{'type': 'record', 'data': {'file': 'napcat-voice-id'}}]})

    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.02)

    def handler(self, ctx, *, worker=False, scheduled=False, silent=False, current=lambda: True):
        task = {'id': 'unit-task', 'attempts': {}, 'unknown': set(), 'sent': 0, 'worker': worker, 'scheduled': scheduled, 'silent': silent}
        return self.f.engine.make_handler(ctx, self.f.scene_key(ctx), True, task, current), task

    async def test_synthesis_is_scene_bound_cached_and_never_sends(self):
        ctx = self.ctx()
        a, b = await asyncio.gather(*[self.f.audio.synthesize(ctx, '明天见。', '轻松自然', lambda: True) for _ in range(2)])
        self.assertEqual(a['voice_id'], b['voice_id'])
        self.assertFalse(a['sent'])
        self.f.audio._json_request.assert_awaited_once()
        payload = self.f.audio._json_request.await_args.args[2]
        self.assertEqual(payload['input']['instruction'], '轻松自然')
        self.bot.call_action.assert_not_awaited()
        self.assertEqual((await self.f.audio.send(self.ctx(group=13), a['voice_id'], None, lambda: True))['code'], 'voice_not_found')
        self.bot.call_action.assert_not_awaited()

    async def test_main_sends_synthesized_voice_to_group_and_private_and_counts_only_delivery(self):
        for private in (False, True):
            ctx = self.ctx(private=private)
            synth, worker = self.handler(ctx, worker=True)
            result = await synth('synthesize_voice', {'text': '你好。'})
            self.assertEqual(worker['sent'], 0)
            self.assertEqual((await synth('send_voice', {'voice_id': result['voice_id']}))['code'], 'delegation_boundary')
            send, main = self.handler(ctx)
            self.assertEqual((await send('synthesize_voice', {'text': '不能在主对话合成'}))['code'], 'delegation_required')
            reply = await send('send_voice', {'voice_id': result['voice_id']})
            self.assertTrue(reply['success'])
            self.assertEqual(main['sent'], 1)
            action, params, _ = self.bot.call_action.await_args.args
            self.assertEqual(action, 'send_private_msg' if private else 'send_group_msg')
            self.assertEqual(params['user_id' if private else 'group_id'], '123' if private else '12')
            self.assertEqual(params['message'][0]['type'], 'record')
            self.assertNotIn('unit-audio-secret', json.dumps(params))
            self.assertIn('你好', self.f.context.recent(self.f.scene_key(ctx), 1)[0]['content'])

    async def test_cancel_disabled_silent_and_unknown_receipts_cannot_leak_or_repeat(self):
        ctx = self.ctx()
        voice = await self.f.audio.synthesize(ctx, '不要重复发送', None, lambda: True)
        silent, _ = self.handler(ctx, worker=True, scheduled=True, silent=True)
        self.assertEqual((await silent('send_voice', {'voice_id': voice['voice_id']}))['code'], 'silent_task')
        cancelled, _ = self.handler(ctx, current=lambda: False)
        self.assertEqual((await cancelled('send_voice', {'voice_id': voice['voice_id']}))['code'], 'cancelled')
        self.bot.call_action.assert_not_awaited()
        self.bot.call_action.side_effect = TimeoutError()
        send, _ = self.handler(ctx)
        self.assertEqual((await send('send_voice', {'voice_id': voice['voice_id']}))['code'], 'unknown')
        self.assertEqual((await send('send_voice', {'voice_id': voice['voice_id']}))['code'], 'unknown')
        self.bot.call_action.assert_awaited_once()
        with patch.dict(os.environ, {'AI_VOICE_ENABLED': 'false'}):
            with self.assertRaises(AudioError):
                await self.f.audio.synthesize(ctx, '关闭', None, lambda: True)

    async def test_send_workspace_audio_uses_existing_export_boundary(self):
        self.f.sandbox.request = AsyncMock(return_value={'success': True, 'data': base64.b64encode(self.data).decode()})
        self.assertTrue((await self.f.audio.send(self.ctx(), None, 'voice.wav', lambda: True))['success'])
        self.f.sandbox.request.assert_awaited_once_with('export', 'group:12', path='voice.wav')

    async def test_provider_http_oss_link_is_upgraded_to_https(self):
        self.f.audio._json_request.return_value = {'output': {'audio': {'url': 'http://example.com/voice.wav?signature=opaque'}}}
        await self.f.audio.synthesize(self.ctx(), '链接兼容', None, lambda: True)
        self.f.files._download.assert_awaited_once_with('https://example.com/voice.wav?signature=opaque')

    async def test_audio_parked_without_blocking_text_then_admitted_once_and_memory_supplemented(self):
        release = asyncio.Event()
        async def understand(*args):
            await release.wait()
            return '【语音原文】请记住明天开会。'
        self.f.audio._understand.side_effect = understand
        self.bot.call_action.return_value = {'base64': base64.b64encode(self.data).decode()}
        ctx = self.ctx(private=True)
        self.f.observe_arrival(ctx.event)
        await asyncio.wait_for(self.f.observe(ctx), 1)
        await self.f.engine.handle(ctx)
        self.assertTrue(self.f.engine.pending.empty())
        other = self.ctx(identifier=2, private=True, segments=[{'type': 'text', 'data': {'text': '其他文字照常受理'}}])
        await self.f.observe(other)
        await self.f.engine.handle(other)
        self.assertEqual(self.f.engine.pending.qsize(), 1)
        release.set()
        await self.until(lambda: self.f.engine.pending.qsize() == 2)
        row = self.f.journal.rows()[0]
        self.assertEqual(self.f.media.job(row['seq'])['audio_delivered'], 1)
        self.assertIn('请记住', self.f.journal.load(row)['rendered_content'])
        self.assertEqual(self.f.journal.pending_count(), 2)
        self.assertTrue(self.f.journal.media_rows())
        await self.f.engine.handle(ctx)
        self.assertEqual(self.f.engine.pending.qsize(), 2)
        self.bot.reply.assert_not_awaited()

    async def test_spoken_nickname_wakes_group_but_music_without_name_stays_passive(self):
        self.bot.call_action.return_value = {'base64': base64.b64encode(self.data).decode()}
        ctx = self.ctx()
        await self.f.observe(ctx)
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)
        self.assertEqual(self.f.engine.pending.qsize(), 1)
        await self.f.engine.pending.get()
        self.f.engine.active.clear()
        self.f.audio._understand.return_value = '【语音原文】无人说话。\n【声音观察】钢琴音乐与雨声。'
        self.f.files._download.return_value = self.data + b'changed'
        await self.f.observe(self.ctx(identifier=2))
        await self.until(lambda: self.f.media.statistics().get('completed') == 2)
        self.assertTrue(self.f.engine.pending.empty())

    async def test_repeated_audio_singleflight_and_forward_audio_are_understood(self):
        self.bot.call_action.return_value = {'base64': base64.b64encode(self.data).decode()}
        for i in range(2):
            await self.f.observe(self.ctx(identifier=i + 1))
        await self.until(lambda: self.f.media.statistics().get('completed') == 2)
        self.f.audio._understand.assert_awaited_once()
        forward = self.ctx(identifier=3, segments=[{'type': 'node', 'data': {'uin': '456', 'name': '作者', 'content': [{'type': 'record', 'data': {'file': 'forward-audio'}}]}}])
        await self.f.observe(forward)
        await self.until(lambda: self.f.media.statistics().get('completed') == 3)
        self.assertIn('雨声', self.f.journal.load(self.f.journal.rows()[-1])['rendered_content'])

    async def test_filename_sound_observations_and_quoted_speech_do_not_wake_group(self):
        self.f.audio._understand.return_value = '【语音原文】无人说话。\n【声音观察】小鱼在水中游动的声音。'
        ctx = self.ctx(segments=[
            {'type': 'file', 'data': {'file': 'https://example.com/audio.wav', 'name': '小鱼.wav'}},
            {'type': 'node', 'data': {'content': [{'type': 'record', 'data': {'file': 'quoted'}}]}}])
        self.bot.call_action.return_value = {'base64': base64.b64encode(self.data).decode()}
        await self.f.observe(ctx)
        await self.until(lambda: self.f.media.statistics().get('completed') == 1)
        self.assertTrue(self.f.engine.pending.empty())

    async def test_missing_key_fails_without_cloud_or_false_content_and_without_retries(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            await self.f.observe(self.ctx())
            await self.until(lambda: self.f.media.statistics().get('failed') == 1)
            row = self.f.journal.rows()[0]
            self.assertIn('audio_not_configured', self.f.journal.load(row)['rendered_content'])
            self.assertEqual(self.f.media.job(row['seq'])['attempts'], 1)
            self.f.audio._understand.assert_not_awaited()

    async def test_cloud_protocol_and_credentials_never_enter_status_or_error(self):
        # Restore actual methods and exercise the HTTP wire with a local transport.
        from src.feature.ai.audio import CloudAudio
        original = httpx.AsyncClient
        def service(request):
            body = json.loads(request.content)
            self.assertEqual(request.headers['authorization'], 'Bearer unit-audio-secret')
            if request.url.path.endswith('chat/completions'):
                self.assertEqual(body['messages'][1]['content'][0]['input_audio']['format'], 'wav')
                return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': {'content': '雨声，没有人声'}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n', headers={'Content-Type': 'text/event-stream'})
            self.assertEqual(body['input']['voice'], self.f.config.load().tts_voice)
            return httpx.Response(200, json={'output': {'audio': {'url': 'https://example.com/audio.wav'}}})
        with patch('src.feature.ai.audio.httpx.AsyncClient', side_effect=lambda **kwargs: original(transport=httpx.MockTransport(service), **kwargs)):
            result = await CloudAudio._understand(self.f.audio, self.f.config.load(), self.data, 'wav')
            self.assertIn('雨声', result)
            result = await CloudAudio._json_request(self.f.audio, self.f.config.load(), 'https://example.com/tts', {'input': {'voice': self.f.config.load().tts_voice}})
            self.assertIn('audio', result['output'])
        self.assertNotIn('unit-audio-secret', json.dumps(self.f.audio.status()))
        self.assertNotIn('audio_api_key', self.f.config.public())
        self.assertEqual(environment()['DASHSCOPE_API_KEY'], 'unit-audio-secret')

    async def test_admin_preview_analysis_and_bad_credentials_are_visible_without_qq_delivery(self):
        app = FastAPI()
        AiAdmin(self.f).register_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            status = await client.get('/api/ai/audio/status')
            self.assertNotIn('unit-audio-secret', status.text)
            preview = (await client.post('/api/ai/audio/preview', json={'text': '试听'})).json()
            self.assertTrue(preview['success'])
            audio = await client.get(preview['audio_url'])
            self.assertEqual(audio.content, self.data)
            result = await client.post('/api/ai/audio/analyze', content=self.data)
            self.assertIn('雨声', result.json()['description'])
            self.f.audio._json_request.side_effect = AudioError('audio_auth_failed', 'Key或地域不匹配')
            result = await client.post('/api/ai/audio/preview', json={'text': '另一个文本'})
            self.assertEqual(result.status_code, 502)
            self.assertEqual(result.json()['code'], 'audio_auth_failed')
            self.bot.call_action.assert_not_awaited()

    async def test_temporary_voice_expiry_and_count_bound_and_persistent_restart(self):
        voice = await self.f.audio.synthesize(self.ctx(), '持久到重启', None, lambda: True)
        from src.feature.ai.voice_store import VoiceStore
        store = VoiceStore(self.f.workspace)
        self.assertIsNotNone(store.get('group:12', identifier=voice['voice_id']))
        store.MAX_FILES = 1
        store.put('group:12', 'another-digest', '第二份', self.data)
        self.assertIsNone(store.get('group:12', identifier=voice['voice_id']))
        with store.connect() as db:
            db.execute('UPDATE voices SET expires=?', (time.time() - 1,))
        store.prune()
        self.assertFalse(list(store.root.glob('*.wav')))

    async def test_old_media_database_upgrades_without_losing_jobs_or_results(self):
        from src.feature.ai.media_queue import MediaQueue
        with self.f.media.connect() as db:
            db.execute('ALTER TABLE jobs DROP COLUMN audio_chat')
            db.execute('ALTER TABLE jobs DROP COLUMN audio_delivered')
            db.execute('INSERT INTO jobs(seq,status,attempts,results) VALUES (?,?,?,?)',
                (42, 'completed', 2, '{"old-image":{"status":"completed","description":"原有图片解析"}}'))
        self.f.media = MediaQueue(self.f)
        row = self.f.media.job(42)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['attempts'], 2)
        self.assertIn('原有图片解析', row['results'])
        self.assertEqual((row['audio_chat'], row['audio_delivered']), (0, 0))

    def test_format_rejects_silk_and_empty_and_long_wav_before_cloud(self):
        for data in (b'', b'#!SILK_V3' + b'0' * 20):
            with self.assertRaises(AudioError):
                check_audio(data)
        self.assertEqual(check_audio(self.data), 'wav')
        self.assertEqual(check_audio(b'\xff\xf1' + b'0' * 32), 'aac')
        self.assertEqual(check_audio(b'\xff\xfb' + b'0' * 32), 'mp3')

    def test_streaming_wav_length_sentinel_uses_actual_frames(self):
        audio = bytearray(self.data)
        audio[4:8] = (0x7fffffff).to_bytes(4, 'little')
        audio[40:44] = (0x7fffffff).to_bytes(4, 'little')
        self.assertEqual(check_audio(bytes(audio)), 'wav')
