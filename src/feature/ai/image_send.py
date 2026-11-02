"""Deliver workspace image bytes as a native QQ image segment."""
import asyncio
import base64
import io
import logging
from pathlib import PurePosixPath

from src.onebot_error import OneBotError

logger = logging.getLogger(__name__)
MAX_BYTES = 20 * 1024 * 1024
FORMATS = {'PNG': '.png', 'JPEG': '.jpg', 'GIF': '.gif', 'WEBP': '.webp', 'BMP': '.bmp'}


def validate_image(encoded):
    from PIL import Image
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError('图片超过20MiB或数据无效')
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_BYTES:
        raise ValueError('图片须为1字节至20MiB')
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = image.format
            if fmt not in FORMATS or min(image.size) < 1 or image.width * image.height > 20_000_000:
                raise ValueError('仅支持PNG/JPEG/GIF/WebP/BMP，单帧最多2000万像素；其他格式请先转换')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('文件不是有效的受支持图片，请先转换或重新生成') from exc
    return data, fmt


async def send_image(feature, ctx, path, current=lambda: True):
    if ctx.message_type not in {'group', 'private'}:
        return {'success': False, 'code': 'scene_mismatch'}
    if not current() or not feature.config.load().workspace_enabled:
        return {'success': False, 'code': 'cancelled'}
    scene = feature.scene_key(ctx)
    exported = await feature.sandbox.request('export', scene, path=path)
    if not exported.get('success'):
        return exported
    # Inspect the actual bytes, not a model-supplied extension. Keep the original
    # encoding and all GIF frames; do not thumbnail or re-encode for sending.
    data, fmt = await asyncio.to_thread(validate_image, exported.get('data'))
    if not current() or not feature.config.load().workspace_enabled:
        return {'success': False, 'code': 'cancelled'}
    filename = PurePosixPath(str(path).replace('\\', '/')).stem[:100] + FORMATS[fmt]
    payload = [{'type': 'image', 'data': {'file': 'base64://' + exported['data'], 'name': filename}}]
    group = ctx.message_type == 'group'
    scene_id = str(ctx.group_id if group else ctx.user_id)
    try:
        response = await ctx.bot.call_action('send_group_msg' if group else 'send_private_msg',
            {'group_id' if group else 'user_id': scene_id, 'message': payload}, ctx.websocket)
    except OneBotError as exc:
        return {'success': False, 'code': 'permission_denied' if exc.permission_denied else 'send_failed',
                'retcode': exc.retcode, 'error': exc.detail}
    except (TimeoutError, ConnectionError, RuntimeError):
        return {'success': False, 'code': 'unknown', 'error': '发送回执超时或连接中断，结果未知；禁止自动重发或改走文件通道重发'}
    if not isinstance(response, dict) or response.get('message_id') is None:
        return {'success': False, 'code': 'unknown', 'error': '图片发送未取得消息ID，结果未知；禁止自动重发或改走文件通道重发'}
    result = {'success': True, 'message_id': str(response['message_id']), 'name': filename, 'bytes': len(data), 'format': fmt}
    try:
        await feature.observe_system({'kind': 'outgoing', 'scene_type': ctx.message_type,
            'scene_id': scene_id, 'self_id': str(ctx.event.get('self_id') or feature.config.load().bot_qq),
            'message_id': response['message_id'], 'content': f'[发送图片：{filename}]'})
    except Exception:
        logger.exception('图片已发送，但本地记录失败')
        result['warning'] = '图片已发送，本地记录失败；不要重发'
    return result
