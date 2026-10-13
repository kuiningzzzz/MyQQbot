"""Send generated text as a native QQ merged-forward card."""
import logging

from src.onebot_error import OneBotError

logger = logging.getLogger(__name__)

async def send_forward_message(feature, ctx, messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        raise ValueError("聊天记录必须包含1至20条文本")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in messages):
        raise ValueError("每条聊天记录必须为1至4000字符的非空文本")
    if sum(map(len, messages)) > 20000:
        raise ValueError("聊天记录总长度不能超过20000字符")
    if ctx.message_type not in {"group", "private"}:
        raise ValueError("只支持当前群聊或私聊")
    settings = feature.config.load()
    qq = str(ctx.event.get("self_id") or settings.bot_qq or "")
    if not qq.isdigit() or int(qq) <= 0:
        raise ValueError("无法确认机器人QQ账号，不能发送聊天记录")
    nodes = [{"type": "node", "data": {"uin": qq, "name": settings.nickname,
              "content": [{"type": "text", "data": {"text": item.strip()}}]}} for item in messages]
    group = ctx.message_type == "group"
    scene_id = ctx.group_id if group else ctx.user_id
    params = {"group_id" if group else "user_id": str(scene_id), "messages": nodes}
    try:
        result = await ctx.bot.call_action("send_group_forward_msg" if group else "send_private_forward_msg", params, ctx.websocket)
    except TimeoutError:
        return {"success": False, "code": "unknown", "error": "发送回执超时，结果未知；不要自动重发或声称成功"}
    except OneBotError as exc:
        return {"success": False, "code": "permission_denied" if exc.permission_denied else "send_failed", "retcode": exc.retcode, "error": exc.detail}
    # A local recording failure must not turn an acknowledged send into a retry.
    output = {"success": True, "message_id": result.get("message_id"), "count": len(nodes)}
    try:
        await feature.observe_system({"kind": "outgoing", "scene_type": ctx.message_type,
            "scene_id": str(scene_id), "self_id": qq, "message_id": result.get("message_id"),
            "content": "[发送聊天记录]\n" + "\n".join(f"[{i}] {item.strip()}" for i, item in enumerate(messages, 1))})
    except Exception:
        logger.exception("聊天记录已发送，但本地记录失败")
        output["warning"] = "消息已发送，本地记录失败；不要重发"
    return output
