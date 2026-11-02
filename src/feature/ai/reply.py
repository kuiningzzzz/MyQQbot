"""Send a native QQ quote reply to a verified message in the bound scene."""
import logging
import re

from src.onebot_error import OneBotError
from .participation import ConversationEvidence

logger = logging.getLogger(__name__)


async def reply_message(feature, ctx, message_id, message, current=lambda: True):
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise ValueError('回复正文须为1至4000字符')
    identifier = str(message_id)
    if not re.fullmatch(r'-?[0-9]+', identifier) or int(identifier) == 0:
        return {'success': False, 'code': 'invalid_request', 'error': '需要上下文中的真实消息ID'}
    if ctx.message_type not in {'group', 'private'}:
        return {'success': False, 'code': 'scene_mismatch'}
    key = feature.scene_key(ctx)
    record = ConversationEvidence(feature).lookup(key, identifier)
    segments = ctx.event.get('message') or []
    refs = {str((s.get('data') or {}).get('id')) for s in segments
            if isinstance(s, dict) and s.get('type') == 'reply'} if isinstance(segments, list) else set()
    if not record and identifier != str(ctx.event.get('message_id')) and identifier not in refs:
        return {'success': False, 'code': 'unknown_message', 'error': '目标不在当前会话上下文或引用中，不能猜测消息ID'}
    if record and record.get('recalled'):
        return {'success': False, 'code': 'message_recalled', 'error': '目标消息已撤回，未发送回复'}
    # NapCat can silently omit a reply segment when its target is unavailable.
    # Resolve before sending so missing/stale IDs do not become plain messages.
    try:
        info = await ctx.bot.call_action('get_msg', {'message_id': identifier}, ctx.websocket)
    except (TimeoutError, ConnectionError, RuntimeError):
        return {'success': False, 'code': 'reply_target_unavailable', 'error': '无法读取目标消息，未发送回复'}
    if not isinstance(info, dict) or not info:
        return {'success': False, 'code': 'reply_target_unavailable', 'error': '目标消息不可用，未发送回复'}
    if info.get('message_id') is not None and str(info['message_id']) != identifier:
        return {'success': False, 'code': 'scene_mismatch', 'error': '目标消息ID不匹配，未发送回复'}
    sender = str((info.get('sender') or {}).get('user_id') or info.get('user_id') or '')
    if ctx.message_type == 'group':
        valid = str(info.get('group_id')) == str(ctx.group_id)
    else:
        own = sender in feature.identity.ids
        target = str(info.get('target_id') or '')
        valid = info.get('message_type') == 'private' and (
            sender == str(ctx.user_id) or own and (
                target == str(ctx.user_id) or not target and record and record.get('is_self')))
    if not valid:
        return {'success': False, 'code': 'scene_mismatch', 'error': '目标不属于当前会话，未发送回复'}
    if not current():
        return {'success': False, 'code': 'cancelled'}
    payload = [{'type': 'reply', 'data': {'id': identifier}},
               {'type': 'text', 'data': {'text': message}}]
    group = ctx.message_type == 'group'
    scene_id = str(ctx.group_id if group else ctx.user_id)
    try:
        result = await ctx.bot.call_action('send_group_msg' if group else 'send_private_msg',
            {'group_id' if group else 'user_id': scene_id, 'message': payload}, ctx.websocket)
    except OneBotError as exc:
        return {'success': False, 'code': 'permission_denied' if exc.permission_denied else 'send_failed',
                'retcode': exc.retcode, 'error': exc.detail}
    except (TimeoutError, ConnectionError, RuntimeError):
        return {'success': False, 'code': 'unknown', 'error': '发送回执超时或连接中断，结果未知；禁止自动重发'}
    output = {'success': True, 'message_id': result.get('message_id'), 'reply_to_message_id': identifier}
    try:
        await feature.observe_system({'kind': 'outgoing', 'scene_type': ctx.message_type,
            'scene_id': scene_id, 'self_id': str(ctx.event.get('self_id') or feature.config.load().bot_qq),
            'message_id': result.get('message_id'), 'content': f'[引用消息ID:{identifier}]\n{message}'})
    except Exception:
        logger.exception('引用回复已发送，但本地记录失败')
        output['warning'] = '消息已发送，本地记录失败；不要重发'
    return output
