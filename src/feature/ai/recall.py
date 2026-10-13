from __future__ import annotations

import asyncio
import re

from src.onebot_error import OneBotError


async def recall_message(feature, ctx, message_id, reason, is_current=lambda: True, inspect=False):
    def result(code, message, **extra):
        return {"success": code == "recalled", "code": code, "message": message, "message_id": str(message_id), **extra}

    if not re.fullmatch(r"-?\d+", str(message_id)) or not isinstance(reason, str) or not reason.strip():
        return result("invalid_request", "需要真实消息ID和撤回判断理由")
    key = feature.scene_key(ctx)
    records = feature.context.snapshot().get(key, {}).get("recent_messages", [])
    record = next((r for r in records if r["id"] == str(message_id)), None)
    segments = ctx.event.get("message", [])
    refs = {str((s.get("data") or {}).get("id")) for s in segments if isinstance(s, dict) and s.get("type") == "reply"} if isinstance(segments, list) else set()
    if record and record.get("recalled"):
        return result("already_recalled", "消息已撤回，无需重复操作")
    if not record and str(message_id) not in refs:
        return result("unknown_message", "目标不在当前会话上下文或当前引用中，请明确引用目标消息")
    try:
        if not record:
            info = await ctx.bot.call_action("get_msg", {"message_id": str(message_id)}, ctx.websocket)
            sender = str((info.get("sender") or {}).get("user_id") or info.get("user_id") or "")
            if ctx.message_type == "group":
                valid = str(info.get("group_id")) == str(ctx.group_id)
            else:
                valid = info.get("message_type") == "private" and (sender == str(ctx.user_id) or (sender in feature.identity.ids and str(info.get("target_id")) == str(ctx.user_id)))
            if not valid or not sender:
                return result("scene_mismatch", "无法确认目标属于当前会话，未执行撤回")
        else:
            sender = record["qq"]
        if inspect:
            return {"success": True, "code": "message_info", "message_id": str(message_id), "sender_qq": sender, "requested_by_sender": sender == str(ctx.user_id), "content": record["content"] if record else info.get("message", info.get("raw_message", ""))}
        own = sender in feature.identity.ids
        if ctx.message_type == "private" and not own:
            return result("permission_denied", "权限不足：不能撤回私聊中对方发送的消息", permission_denied=True)
        if not is_current():
            return result("cancelled", "新消息已到达，本次过期操作取消")
        try:
            await ctx.bot.call_action("delete_msg", {"message_id": str(message_id)}, ctx.websocket)
        except OneBotError as exc:
            denied = exc.permission_denied
            # NapCat may use a generic failure code. Query the actual role rather
            # than treating every failed deletion as a permissions error.
            if not denied and not own and ctx.message_type == "group":
                try:
                    bot_id = str(ctx.event.get("self_id") or feature.config.load().bot_qq)
                    member = await ctx.bot.call_action("get_group_member_info", {"group_id": str(ctx.group_id), "user_id": bot_id, "no_cache": True}, ctx.websocket)
                    denied = member.get("role") == "member"
                    if member.get("role") == "admin":
                        target = await ctx.bot.call_action("get_group_member_info", {"group_id": str(ctx.group_id), "user_id": sender, "no_cache": True}, ctx.websocket)
                        denied = target.get("role") in {"admin", "owner"}
                except Exception:
                    pass
            return result("permission_denied" if denied else "recall_failed", "权限不足：机器人无法撤回该消息" if denied else "撤回失败，原因见NapCat返回信息", permission_denied=denied, retcode=exc.retcode, detail=exc.detail)
        await feature.context.mark_recalled(key, message_id)
        return result("recalled", "消息已撤回", sender_qq=sender, requester_qq=str(ctx.user_id), requested_by_sender=sender == str(ctx.user_id), reason=reason)
    except (TimeoutError, asyncio.TimeoutError):
        return result("unknown", "NapCat回执超时，无法确认执行结果，请勿声称成功或自动重试")
    except Exception as exc:
        return result("recall_failed", "撤回处理失败", detail=str(exc))
