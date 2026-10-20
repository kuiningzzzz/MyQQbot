"""Authenticated by the parent admin app; no public image URLs or write tokens."""
import asyncio
import base64
from pathlib import Path

from fastapi import Body, HTTPException, Request
from fastapi.responses import FileResponse

from src.onebot_error import OneBotError
from src.feature.atom.context import MessageContext
from .stickers import MAX_IMAGE


def register(app, feature):
    library = feature.stickers

    async def operation(action, body, account):
        try:
            return await library.manage(action, body, account=account)
        except TimeoutError:
            return {'success': False, 'code': 'unknown', 'error': '操作超时，请同步核对QQ状态，不要自动重试'}
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error
        except OneBotError as error:
            raise HTTPException(502, error.detail) from error

    @app.get('/api/ai/stickers')
    async def catalogue(account: str | None = None, offset: int = 0, limit: int = 100, query: str = ''):
        if offset < 0 or not 1 <= limit <= 200 or len(query) > 200:
            raise HTTPException(400, '分页或查询参数无效')
        rows = await asyncio.to_thread(library.store.list, account)
        if query:
            rows = [row for row in rows if query.casefold() in ' '.join(str(row.get(k) or '') for k in ['name', 'tags', 'description', 'auto_description', 'remote_description', 'id']).casefold()]
        known = {row['account'] for row in library.store.list(deleted=True)} | set(library.accounts())
        return {'stickers': [library.store.public(row, details=True) for row in rows[offset:offset + limit]],
                'total': len(rows), 'next_offset': offset + limit if len(rows) > offset + limit else None,
                'accounts': sorted(known), 'connected_accounts': library.accounts(),
                'sync': {qq: library.store.state('sync:' + qq) for qq in known},
                'enabled': feature.enabled and feature.config.load().stickers_enabled}

    @app.get('/api/ai/stickers/{identifier}/image')
    async def image(identifier: str):
        row = library.store.get(identifier)
        if not row or row.get('deleted') or not row.get('asset'):
            raise HTTPException(404, '图片尚未就绪或表情已移除')
        try:
            path = library.path(row['asset'])
            if not path.is_file():
                raise ValueError('表情文件不存在，请重新同步')
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return FileResponse(path, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/api/ai/stickers/sync')
    async def sync(body: dict = Body(...)):
        try:
            return library.request_sync(body.get('account'))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post('/api/ai/stickers/manage')
    async def manage(body: dict = Body(...)):
        return await operation(body.get('action'), body.get('body', {}), body.get('account'))

    @app.post('/api/ai/stickers/upload')
    async def upload(request: Request, account: str, qq: bool = True, name: str = ''):
        if len(name) > 100:
            raise HTTPException(400, '名称最多100字')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_IMAGE:
                raise HTTPException(413, '表情最多8MiB')
        return await operation('add', {'base64': base64.b64encode(data).decode(), 'qq': qq, 'name': name}, account)

    @app.post('/api/ai/stickers/test-send')
    async def test_send(body: dict = Body(...)):
        try:
            bot, ws, account = library.connection(account=body.get('account'))
            kind, target = body.get('scene_type'), str(body.get('scene_id', ''))
            if kind not in {'group', 'private'} or not target.isascii() or not target.isdigit() or int(target) <= 0:
                raise ValueError('请输入群号或私聊QQ号')
            ctx = MessageContext(bot=bot, websocket=ws, event={'self_id': account}, msg='', message_type=kind,
                                 group_id=int(target) if kind == 'group' else None, user_id=int(target) if kind == 'private' else 0)
            if not feature.enabled or not feature.config.load().stickers_enabled:
                raise ValueError('AI或表情发送已关闭')
            return await library.send(ctx, body.get('id'))
        except TimeoutError:
            return {'success': False, 'code': 'unknown', 'error': '回执超时，结果未知，不要自动重发'}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OneBotError as error:
            raise HTTPException(502, error.detail) from error


def panel_html():
    return Path(__file__).with_name('sticker_panel.html').read_text('utf-8')


def javascript():
    return Path(__file__).with_name('sticker_panel.js').read_text('utf-8')
