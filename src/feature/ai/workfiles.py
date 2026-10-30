from __future__ import annotations

import base64
import json
import re
import time
import uuid
from pathlib import PurePosixPath

from .media import MediaUrlExpired


class WorkFiles:
    def __init__(self, feature):
        self.feature = feature
        self.attachments = feature.workspace.load_state("attachments", {})

    @staticmethod
    def attachment_name(kind, data):
        default = 'image.png' if kind == 'image' else 'voice.wav' if kind == 'record' else 'attachment.bin'
        name = str(data.get('name') or data.get('file_name') or data.get('file') or default)
        if name.startswith(('http:', 'https:', 'data:', 'base64:')):
            name = default
        return PurePosixPath(name.replace('\\', '/')).name[:180]

    def register(self, ctx, kind, data, *, position=None, persist=True):
        now = time.time()
        self.attachments = {key: item for key, item in self.attachments.items() if now - item["created"] < 7 * 86400}
        if position is not None:
            import hashlib
            identifier = hashlib.sha256(f'{self.feature.scene_key(ctx)}:{ctx.event.get("message_id")}:{position}'.encode()).hexdigest()[:24]
            if identifier in self.attachments:
                return identifier, self.attachments[identifier]
        else:
            identifier = uuid.uuid4().hex[:16]
        name = self.attachment_name(kind, data)
        item = {"scene": self.feature.scene_key(ctx), "sender_qq": str(ctx.user_id), "message_id": str(ctx.event.get("message_id", "")), "kind": kind, "name": name, "size": data.get("file_size", data.get("size")), "created": now, "data": {key: data[key] for key in ("url", "file", "file_id") if key in data}}
        self.attachments[identifier] = item
        while len(self.attachments) > 300 or len(json.dumps(self.attachments)) > 3 * 1024 * 1024:
            self.attachments.pop(next(iter(self.attachments)))
        if persist:
            self.flush()
        return identifier, item

    def flush(self):
        self.feature.workspace.save_state('attachments', self.attachments)

    def list(self, scene):
        return [{"attachment_id": key, **{k: v for k, v in item.items() if k not in {"data", "scene"}}} for key, item in self.attachments.items() if item["scene"] == scene and time.time() - item["created"] < 7 * 86400][-50:]

    async def resolve(self, ctx, item, refresh=False):
        data = item["data"]
        url = str(data.get("url") or data.get("file") or "")
        file_id = data.get("file_id")
        if file_id and (refresh or not url.startswith(("http://", "https://", "data:", "base64://"))):
            try:
                action = "get_group_file_url" if ctx.message_type == "group" else "get_private_file_url"
                params = {"file_id": file_id}
                if ctx.message_type == "group":
                    params["group_id"] = str(ctx.group_id)
                reply = await ctx.bot.call_action(action, params, ctx.websocket)
                url = str(reply.get("url") or "")
            except Exception:
                url = ""
            if not url.startswith(("http://", "https://", "data:", "base64://")):
                reply = await ctx.bot.call_action("get_file", {"file_id": file_id}, ctx.websocket)
                url = str(reply.get("url") or "")
                if not url.startswith(("http://", "https://")) and reply.get("base64"):
                    url = "base64://" + reply["base64"]
        return url

    async def refresh_media(self, ctx, item):
        """QQ 直链会过期（retmsg=download url has expired）；向 NapCat 重新索取。

        QQ 事件里带的下载地址带有时效；排队处理或稍后重试时经常已经失效，
        此时只能回到消息原文，让 NapCat 重新签发一个可用地址。
        """
        if not getattr(ctx, 'bot', None):
            return ''
        data = item.get('data') or {}
        name = str(data.get('file') or '')
        file_id = str(data.get('file_id') or '')
        calls = []
        if name:
            calls.append(('get_image', {'file': name}))
        if file_id:
            calls.append(('get_image', {'file_id': file_id}))
            calls.append(('get_file', {'file_id': file_id}))
        for action, params in calls:
            try:
                reply = await ctx.bot.call_action(action, params, ctx.websocket)
            except Exception:
                continue
            url = str((reply or {}).get('url') or '')
            if url.startswith(('http://', 'https://', 'base64://', 'data:')):
                return url
        # 兜底：重拉整条消息，消息段里自带新的直链。
        message_id = str(item.get('message_id') or ctx.event.get('message_id') or '')
        if not message_id:
            return ''
        try:
            reply = await ctx.bot.call_action('get_msg', {'message_id': message_id}, ctx.websocket)
        except Exception:
            return ''
        for segment in (reply or {}).get('message') or []:
            if not isinstance(segment, dict):
                continue
            if item.get('kind') and segment.get('type') != item['kind']:
                continue
            segment_data = segment.get('data') or {}
            if name and str(segment_data.get('file') or '') not in {'', name}:
                continue
            url = str(segment_data.get('url') or '')
            if url.startswith(('http://', 'https://')):
                return url
        return ''

    async def fetch(self, ctx, item):
        """下载附件字节；直链过期时换一个新链接再试一次。"""
        url = await self.resolve(ctx, item)
        try:
            return await self.feature.files._download(url)
        except MediaUrlExpired:
            fresh = await self.refresh_media(ctx, item)
            if not fresh or fresh == url:
                raise
            return await self.feature.files._download(fresh)

    async def download(self, ctx, identifier, path, is_current):
        item = self.attachments.get(identifier)
        if not item or item["scene"] != self.feature.scene_key(ctx) or time.time() - item["created"] >= 7 * 86400:
            raise ValueError("附件不存在、已过期或不属于当前会话")
        path = path or (identifier + "-" + re.sub(r"[^\w.\-]", "_", item["name"]))
        data = await self.feature.audio.attachment_bytes(ctx, item) if item['kind'] == 'record' else await self.fetch(ctx, item)
        if not is_current():
            return {"success": False, "error": "新消息已到达，取消本次下载入库"}
        return await self.feature.sandbox.put(item["scene"], path, data)

    async def send(self, ctx, path, name, is_current):
        exported = await self.feature.sandbox.request("export", self.feature.scene_key(ctx), path=path)
        if not exported.get("success"):
            return exported
        data = exported.get("data", "")
        if len(data) > 28 * 1024 * 1024 or len(base64.b64decode(data, validate=True)) > 20 * 1024 * 1024:
            raise ValueError("发送文件超过20MiB")
        filename = str(name or PurePosixPath(path).name)
        if not filename.strip() or len(filename) > 180 or re.search(r'[\\/\x00-\x1f]', filename):
            raise ValueError("发送文件名无效")
        if not is_current():
            return {"success": False, "error": "新消息已到达，取消过期文件发送"}
        action = "upload_group_file" if ctx.message_type == "group" else "upload_private_file"
        params = {"file": "base64://" + data, "name": filename}
        params["group_id" if ctx.message_type == "group" else "user_id"] = str(ctx.group_id if ctx.message_type == "group" else ctx.user_id)
        response = await ctx.bot.call_action(action, params, ctx.websocket)
        await self.feature.observe_system({"kind": "outgoing", "scene_type": ctx.message_type, "scene_id": str(ctx.group_id if ctx.message_type == "group" else ctx.user_id), "self_id": ctx.event.get("self_id"), "content": f"[发送文件：{filename}]"})
        return {"success": True, "name": filename, "bytes": exported.get("bytes"), "file_id": response.get("file_id")}
