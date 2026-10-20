"""QQ favourites, durable local images and background descriptions.

Only search/send are model tools. Mutations use the bundled SKILL's private,
task-bound bridge and re-check the live owner grant in ChatEngine.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import time
import tempfile
from pathlib import Path

from .sticker_store import StickerStore

MAX_IMAGE = 8 * 1024 * 1024
MAX_FAVOURITES = 10000


def inspect_image(data):
    from PIL import Image
    if not data or len(data) > MAX_IMAGE:
        raise ValueError('表情图片须为 1 字节至 8 MiB')
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'} or image.width * image.height > 20_000_000 or min(image.size) < 1:
            raise ValueError('仅支持 PNG/JPEG/GIF/WebP，单帧最多 2000 万像素')
        image.seek(0)
        image.load()
        return {'extension': {'PNG': '.png', 'JPEG': '.jpg', 'GIF': '.gif', 'WEBP': '.webp'}[image.format],
                'frames': getattr(image, 'n_frames', 1)}


class StickerLibrary:
    def __init__(self, feature):
        self.feature = feature
        self.store = StickerStore(feature.workspace.data_root)
        self.root = feature.workspace.root / 'stickers'
        self.assets = self.root / 'assets'
        self.assets.mkdir(parents=True, exist_ok=True)
        if self.assets.resolve() != self.assets.absolute():
            raise ValueError('表情目录不能经过符号链接')
        self.bot = None
        self.worker = None
        self.closing = False
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.sync_requests = set()
        self.next_sync = {}
        self.cooldowns = {}
        self.next_gc = 0

    @property
    def busy(self):
        return self.lock.locked()

    def accounts(self):
        return sorted({str(value) for ws, value in getattr(self.bot, '_self_ids', {}).items()
                       if str(value).isdigit() and ws in getattr(self.bot, 'connected_clients', set())})

    def connection(self, ctx=None, account=None):
        if self.closing:
            raise ValueError('服务正在关闭')
        if ctx is not None:
            account = str(ctx.event.get('self_id') or getattr(ctx.bot, '_self_ids', {}).get(ctx.websocket) or '')
            if not account.isdigit():
                raise ValueError('无法确定当前机器人的 QQ 账号')
            return ctx.bot, ctx.websocket, account
        pairs = [(ws, str(qq)) for ws, qq in getattr(self.bot, '_self_ids', {}).items()
                 if ws in getattr(self.bot, 'connected_clients', set())]
        if account:
            pairs = [pair for pair in pairs if pair[1] == str(account)]
        if len(pairs) != 1:
            raise ValueError('请选择已连接的机器人 QQ 账号' if pairs else 'NapCat 尚未连接或账号尚未识别')
        ws, account = pairs[0]
        return self.bot, ws, account

    def path(self, asset):
        if not isinstance(asset, str) or not re.fullmatch(r'[0-9a-f]{64}\.(png|jpg|gif|webp)', asset):
            raise ValueError('表情文件标识无效')
        path = self.assets / asset
        if path.resolve() != path.absolute() or path.is_file() and path.stat().st_nlink > 1:
            raise ValueError('表情文件不能是链接')
        return path

    def _publish(self, data):
        info = inspect_image(data)
        asset = hashlib.sha256(data).hexdigest() + info['extension']
        path = self.path(asset)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=self.assets, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != asset.split('.')[0]:
            raise ValueError('表情图片校验失败')
        path.touch()  # Protect a newly reused asset from delayed orphan cleanup.
        return asset

    async def collect_assets(self):
        """Keep tombstones for sync, release unreferenced bytes after a grace day."""
        async with self.lock:
            referenced = {r.get('asset') for r in self.store.list()}
            cutoff = time.time() - 86400
            def collect():
                for path in self.assets.iterdir():
                    if path.name in referenced or path.is_symlink() or not path.is_file():
                        continue
                    if not (re.fullmatch(r'[0-9a-f]{64}\.(png|jpg|gif|webp)', path.name) or path.suffix == '.tmp'):
                        continue
                    if path.stat().st_nlink == 1 and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
            await asyncio.to_thread(collect)
            self.next_gc = time.time() + 3600

    def _row(self, identifier, account, *, deleted=False):
        if not isinstance(identifier, str) or not re.fullmatch(r's-[0-9a-f]{24}', identifier):
            raise ValueError('请提供目录中的实际表情ID')
        row = self.store.get(identifier)
        if not row or row['account'] != account or row.get('deleted') and not deleted:
            raise ValueError('表情不存在或不属于当前机器人账号')
        return row

    @staticmethod
    def _metadata(body):
        patch = {}
        for name, limit in [('name', 100), ('description', 4000)]:
            if name in body:
                if not isinstance(body[name], str) or len(body[name]) > limit:
                    raise ValueError(f'{name} 必须是最多 {limit} 字的文本')
                patch[name] = body[name].strip()
        if 'tags' in body:
            if not isinstance(body['tags'], list) or len(body['tags']) > 30 or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in body['tags']):
                raise ValueError('标签最多 30 个，每个 1 至 40 字')
            patch['tags'] = list(dict.fromkeys(t.strip() for t in body['tags']))
        if 'enabled' in body:
            if not isinstance(body['enabled'], bool):
                raise ValueError('enabled 须为布尔值')
            patch['enabled'] = body['enabled']
        return patch

    async def rpc(self, bot, ws, action, args):
        result = await bot.call_action(action, args, ws)
        if isinstance(result, dict) and ('result' in result and result['result'] not in {0, '0'} or result.get('success') is False):
            raise ValueError(f'NapCat {action} 未成功，请检查 QQ 账号状态')
        return result

    async def _fetch(self, bot, ws):
        count = 48
        while True:
            items = await self.rpc(bot, ws, 'fetch_custom_face_detail', {'count': count})
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError('NapCat 未返回收藏详情列表，请检查版本及账号')
            if len(items) < count:
                return items, True
            if count == MAX_FAVOURITES:
                return items, False
            count = min(count * 2, MAX_FAVOURITES)

    async def sync(self, *, ctx=None, account=None, current=lambda: True):
        async with self.lock:
            bot, ws, account = self.connection(ctx, account)
            items, complete = await self._fetch(bot, ws)
            if not current() or self.closing:
                return {'success': False, 'code': 'cancelled'}
            grouped, skipped = {}, 0
            for item in items:
                md5 = str(item.get('md5') or '').lower()
                if not re.fullmatch('[0-9a-f]{32}', md5) or not item.get('resId'):
                    skipped += 1
                    continue
                grouped.setdefault(md5, []).append(item)
            # Missing items are not deleted from the local library. An incomplete
            # or failed fetch must never destroy the last working catalogue.
            for md5, remotes in grouped.items():
                identifier = self.store.identifier(account, md5)
                old = self.store.get(identifier)
                first = remotes[0]
                refs = [{'res_id': str(i['resId']), 'emoji_id': i.get('emoId'), 'desc': str(i.get('desc') or '')[:4000]} for i in remotes]
                patch = {'remote': refs, 'remote_present': True, 'source_url': str(first.get('url') or ''),
                         'remote_description': str(first.get('desc') or first.get('ocrWord') or first.get('modifyWord') or '')[:4000]}
                if old and old.get('pending'):
                    pending = old['pending']
                    if pending['action'] == 'add' or pending['action'] == 'update' and refs and all(ref['desc'] == pending.get('desc') for ref in refs):
                        patch['pending'] = None
                        patch.update(pending.get('patch', {}))
                    elif time.time() - pending['at'] > 300:
                        patch.update(pending=None, error='上次QQ修改未确认成功，已重新核对；需要时手动重试')
                await asyncio.to_thread(self.store.save, account, md5, patch)
            if complete and not skipped:
                for old in self.store.list(account, deleted=True):
                    if old['md5'] not in grouped:
                        patch = {'remote_present': False, 'remote': []}
                        if (old.get('pending') or {}).get('action') == 'delete':
                            if old['pending'].get('local_delete'):
                                patch.update(deleted=True, enabled=False)
                            patch['pending'] = None
                        elif old.get('pending') and time.time() - old['pending']['at'] > 300:
                            patch.update(pending=None, error='上次添加未在QQ收藏中出现；需要时手动重新添加')
                        await asyncio.to_thread(self.store.save, account, old['md5'], patch)
            state = {'at': time.time(), 'count': len(grouped), 'complete': complete and not skipped,
                     'skipped': skipped, 'error': ''}
            self.store.state('sync:' + account, state)
            self.next_sync[account] = time.time() + 3600
            self.wake.set()
            return {'success': True, **state, 'account': account}

    def request_sync(self, account):
        _, _, account = self.connection(account=account)
        self.sync_requests.add(account)
        self.wake.set()
        return {'success': True, 'state': 'queued', 'account': account}

    def search(self, account, query='', limit=8):
        if not isinstance(query, str) or len(query) > 200 or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError('查询最多 200 字，limit 为 1 至 20')
        terms = set(re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]{2}', query.casefold()))
        terms.add(query.strip().casefold())
        for group in [('赞同', '同意', '点赞', '支持', '赞'), ('开心', '高兴', '笑', '哈哈'), ('无语', '无奈', '沉默'), ('谢谢', '感谢'), ('疑惑', '问号', '不懂'), ('晚安', '睡觉')]:
            if any(word in query for word in group):
                terms.update(group)
        ranked = []
        for row in self.store.list(account):
            if not row.get('enabled') or not row.get('asset'):
                continue
            title = (row.get('name') or row.get('auto_name') or '') + ' ' + ' '.join(row.get('tags') or row.get('auto_tags') or [])
            description = ' '.join(str(row.get(k) or '') for k in ['description', 'auto_description', 'remote_description'])
            score = sum((8 if term in title.casefold() else 1 if term in description.casefold() else 0) for term in terms if term)
            if not query.strip() or score:
                ranked.append((score, -row.get('used_at', 0), row['id'], row))
        ranked.sort(key=lambda item: item[:3], reverse=True)
        return [self.store.public(row) for _, _, _, row in ranked[:limit]]

    async def send(self, ctx, identifier, current=lambda: True):
        if not self.feature.enabled or not self.feature.config.load().stickers_enabled:
            return {'success': False, 'code': 'stickers_disabled'}
        bot, ws, account = self.connection(ctx)
        row = self._row(identifier, account)
        if not row.get('enabled') or not row.get('asset'):
            return {'success': False, 'code': 'sticker_unavailable', 'error': '表情已停用或图片尚未下载'}
        scene = self.feature.scene_key(ctx)
        cooldown = self.cooldowns.get((account, scene), 0)
        if time.monotonic() < cooldown:
            return {'success': False, 'code': 'sticker_cooldown'}
        data = await asyncio.to_thread(self.path(row['asset']).read_bytes)
        if len(data) > MAX_IMAGE or hashlib.sha256(data).hexdigest() != row['asset'].split('.')[0]:
            raise ValueError('表情图片损坏，请重新同步')
        latest = self._row(identifier, account)
        if not current() or self.closing or not latest.get('enabled') or not self.feature.enabled or not self.feature.config.load().stickers_enabled:
            return {'success': False, 'code': 'cancelled'}
        if time.monotonic() < self.cooldowns.get((account, scene), 0):
            return {'success': False, 'code': 'sticker_cooldown'}
        # Reserve before the await, including unknown sends, to avoid concurrent
        # turns or a scheduled fork sending the same scene a burst of stickers.
        self.cooldowns[(account, scene)] = time.monotonic() + 10
        action = 'send_group_msg' if ctx.message_type == 'group' else 'send_private_msg'
        target = {'group_id': str(ctx.group_id)} if ctx.message_type == 'group' else {'user_id': str(ctx.user_id)}
        message = [{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(data).decode(), 'sub_type': 1}}]
        result = await bot.call_action(action, {**target, 'message': message}, ws)
        if not isinstance(result, dict) or result.get('message_id') is None:
            return {'success': False, 'code': 'unknown', 'error': '未获得消息ID，请在QQ核对，不要自动重发'}
        label = row.get('name') or row.get('description') or row.get('auto_description') or row.get('remote_description') or identifier
        recorded = True
        try:
            self.store.save(account, row['md5'], {'used_at': time.time()})
            await self.feature.record(ctx.message_type, str(ctx.group_id if ctx.message_type == 'group' else ctx.user_id),
                account, self.feature.config.load().nickname, f'[发送表情：{label[:300]}；ID:{identifier}]',
                message_id=result.get('message_id'), outgoing=True)
        except Exception:
            # An indexing/logging error cannot turn an acknowledged QQ send
            # into a failed delivery and encourage the model to duplicate it.
            recorded = False
        return {'success': True, 'message_id': result.get('message_id'), 'sticker_id': identifier, 'recorded': recorded}

    async def source(self, body, ctx=None):
        choices = [key for key in ['attachment_id', 'url', 'path', 'base64'] if body.get(key)]
        if len(choices) != 1:
            raise ValueError('请且只提供 attachment_id、url、path、base64 中的一项')
        kind = choices[0]
        if not isinstance(body[kind], str):
            raise ValueError('图片来源须为文本')
        if kind == 'attachment_id':
            if ctx is None:
                raise ValueError('附件来源需要当前 QQ 会话')
            item = self.feature.workfiles.attachments.get(body[kind])
            if not item or item['scene'] != self.feature.scene_key(ctx) or time.time() - item['created'] >= 7 * 86400:
                raise ValueError('附件不属于当前会话或已过期')
            return await self.feature.files._download(await self.feature.workfiles.resolve(ctx, item, refresh=True))
        if kind == 'path':
            path = Path(body[kind])
            if not path.is_absolute():
                path = self.feature.workspace.root / path
            if path.resolve() != path.absolute() or not path.resolve().is_relative_to(self.feature.workspace.root) or not path.is_file() or path.stat().st_nlink > 1 or path.stat().st_size > MAX_IMAGE:
                raise ValueError('只能添加工作区内的实际图片文件，最多 8 MiB')
            return await asyncio.to_thread(path.read_bytes)
        if kind == 'base64':
            value = body[kind]
            if not isinstance(value, str) or len(value) > MAX_IMAGE * 1.4:
                raise ValueError('图片过大')
            return base64.b64decode(value, validate=True)
        return await self.feature.files._download(body[kind])

    async def manage(self, action, body, *, ctx=None, account=None, current=lambda: True):
        if not isinstance(action, str):
            raise ValueError('操作须为文本')
        if not isinstance(body, dict):
            raise ValueError('参数须为 JSON 对象')
        try:
            bot, ws, account = self.connection(ctx, account)
        except ValueError:
            local = action in {'list', 'get', 'retry'} or action in {'add', 'update'} and body.get('qq', action == 'add') is False or action == 'delete' and body.get('scope') == 'local'
            if ctx is not None or not local or not str(account).isascii() or not str(account).isdigit():
                raise
            bot, ws, account = None, None, str(account)
        if action == 'sync':
            return await self.sync(ctx=ctx, account=account, current=current)
        if action == 'list':
            offset, limit = body.get('offset', 0), body.get('limit', 50)
            if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError('offset须为非负整数，limit须为1至100')
            rows = self.store.list(account)
            return {'success': True, 'stickers': [self.store.public(row, details=True) for row in rows[offset:offset + limit]],
                    'total': len(rows), 'next_offset': offset + limit if len(rows) > offset + limit else None}
        if action == 'get':
            return {'success': True, 'sticker': self.store.public(self._row(body.get('id'), account), details=True)}
        if action == 'retry':
            row = self._row(body.get('id'), account)
            self.store.save(account, row['md5'], {'state': 'queued', 'due': 0, 'attempts': 0, 'error': ''})
            self.wake.set()
            return {'success': True, 'state': 'queued'}
        if action not in {'add', 'update', 'delete'}:
            raise ValueError('未知表情操作')
        if 'qq' in body and not isinstance(body['qq'], bool):
            raise ValueError('qq 须为布尔值')
        if action == 'delete' and body.get('scope', 'both') not in {'both', 'local', 'qq'}:
            raise ValueError('删除范围为 local、qq 或 both')
        patch = self._metadata(body)
        if action == 'add':
            data = await self.source(body, ctx)
            asset = await asyncio.to_thread(self._publish, data)
            md5 = hashlib.md5(data).hexdigest()
        async with self.lock:
            if not current() or self.closing:
                return {'success': False, 'code': 'cancelled'}
            if action == 'add':
                row = self.store.get(self.store.identifier(account, md5))
                if row and row.get('pending'):
                    return {'success': False, 'code': 'unknown', 'error': '上次操作结果未知，先同步核对，不要重复添加'}
                if row and row.get('asset') and row['asset'] != asset:
                    raise ValueError('图片 MD5 与已有表情冲突，请使用其他图片')
                row = self.store.save(account, md5, {**patch, 'asset': asset, 'state': 'queued', 'attempts': 0, 'due': 0,
                    'deleted': False, 'enabled': body.get('enabled', True), 'error': ''})
            else:
                row = self._row(body.get('id'), account)
                md5 = row['md5']
                if body.get('expected_revision') is not None and body['expected_revision'] != row['revision']:
                    raise ValueError('表情已被更新，请刷新后重试')
                if row.get('pending'):
                    return {'success': False, 'code': 'unknown', 'error': '上次操作结果未知，请先同步核对'}
            remote = body.get('qq', action == 'add') if action != 'delete' else body.get('scope', 'both') in {'both', 'qq'}
            if action == 'delete' and body.get('scope', 'both') not in {'both', 'local', 'qq'}:
                raise ValueError('删除范围为 local、qq 或 both')
            if not isinstance(remote, bool):
                raise ValueError('qq 须为布尔值')
            if remote:
                # Resource IDs and cached presence can change in the QQ client.
                # Resolve the current snapshot before claiming any QQ mutation.
                observed, complete = await self._fetch(bot, ws)
                if not current() or self.closing:
                    return {'success': False, 'code': 'cancelled'}
                matches = [item for item in observed if str(item.get('md5', '')).lower() == md5]
                if any(not item.get('resId') for item in matches) or not matches and not complete:
                    raise ValueError('QQ 收藏目录不完整，无法确认修改范围，请稍后同步')
                refs = [{'res_id': str(item['resId']), 'emoji_id': item.get('emoId'),
                         'desc': str(item.get('desc') or '')} for item in matches]
                row = self.store.save(account, md5, {'remote': refs, 'remote_present': bool(matches)})
                if action == 'add' and matches or action == 'delete' and not matches:
                    remote = False
            if action == 'update' and remote and ('description' not in patch or not row.get('remote')):
                raise ValueError('同步修改 QQ 描述需要 description 和有效的 QQ 收藏记录')
            if action == 'delete' and remote and not row.get('remote'):
                if row.get('remote_present'):
                    raise ValueError('缺少 QQ 资源 ID，请先同步')
                remote = False
            if remote:
                pending = {'action': action, 'at': time.time(), 'desc': patch.get('description'), 'patch': patch,
                           'local_delete': action == 'delete' and body.get('scope', 'both') == 'both'}
                self.store.save(account, md5, {'pending': pending})
                try:
                    if action == 'add':
                        transfer = await self.rpc(bot, ws, 'download_file', {'base64': base64.b64encode(data).decode(), 'name': asset})
                        if not isinstance(transfer, dict) or not transfer.get('file'):
                            raise ValueError('NapCat 未返回临时文件路径')
                        if not current() or self.closing:
                            self.store.save(account, md5, {'pending': None})
                            return {'success': False, 'code': 'cancelled'}
                        await self.rpc(bot, ws, 'add_custom_face', {'file': transfer['file'], 'md5': md5, 'is_origin': True})
                    elif action == 'delete':
                        await self.rpc(bot, ws, 'delete_custom_face', {'ids': [ref['res_id'] for ref in row['remote']]})
                    else:
                        for ref in row['remote']:
                            if not current() or self.closing:
                                return {'success': False, 'code': 'cancelled', 'error': '任务已撤销，部分QQ描述可能已修改，请同步核对'}
                            if ref.get('emoji_id') is None:
                                raise ValueError('QQ 收藏记录缺少 emoji_id，请重新同步')
                            await self.rpc(bot, ws, 'set_custom_face_desc', {'emoji_id': ref['emoji_id'], 'res_id': ref['res_id'], 'md5': md5, 'desc': patch['description']})
                    observed, complete = await self._fetch(bot, ws)
                    matches = [item for item in observed if str(item.get('md5', '')).lower() == md5]
                    confirmed = bool(matches) if action == 'add' else complete and not matches if action == 'delete' else bool(matches) and all(item.get('desc') == patch['description'] for item in matches)
                    if not confirmed:
                        return {'success': False, 'code': 'unknown', 'sticker_id': row['id'], 'error': '已提交但未确认 QQ 收藏最终状态，请稍后同步核对，不要重复操作'}
                    refs = [{'res_id': str(item['resId']), 'emoji_id': item.get('emoId'), 'desc': str(item.get('desc') or '')} for item in matches]
                    patch.update(remote=refs, remote_present=bool(matches), pending=None)
                except TimeoutError:
                    return {'success': False, 'code': 'unknown', 'sticker_id': row['id'], 'error': 'QQ 回执超时，结果未知，请同步核对，不要自动重试'}
                except Exception:
                    # Keep an uncertainty receipt: an error after a successful
                    # write (or a partial multi-resource update) is not a safe retry.
                    self.wake.set()
                    raise
            if action == 'delete' and body.get('scope', 'both') in {'both', 'local'}:
                patch.update(deleted=True, enabled=False)
            result = self.store.save(account, md5, patch)
            self.wake.set()
            return {'success': True, 'sticker': self.store.public(result, details=True)}

    async def _enrich(self, row):
        account, md5 = row['account'], row['md5']
        self.store.save(account, md5, {'state': 'processing'})
        try:
            if row.get('asset') and self.path(row['asset']).is_file():
                data = await asyncio.to_thread(self.path(row['asset']).read_bytes)
            else:
                data = await self.feature.files._download(row.get('source_url', ''))
                if hashlib.md5(data).hexdigest() != md5:
                    raise ValueError('收藏图片 MD5 不符')
            asset = await asyncio.to_thread(self._publish, data)
            fresh = self.store.get(row['id'])
            if not fresh or fresh.get('deleted') or self.closing:
                return
            self.store.save(account, md5, {'asset': asset})
            if fresh.get('auto_description'):
                description = fresh['auto_description']
            else:
                description = await self.feature.files.describe_sticker(data)
                if not getattr(description, 'complete', True):
                    raise ValueError('图片理解尚未成功，可在后台重试')
            fresh = self.store.get(row['id'])
            if fresh and not fresh.get('deleted') and not self.closing:
                text = str(description).replace('**', '')
                name = re.search(r'(?m)^名称[：:]\s*(.+)', text)
                tags = re.search(r'(?m)^标签[：:]\s*(.+)', text)
                labels = [word.strip() for word in re.split(r'[,，、;；]', tags[1])] if tags else []
                self.store.save(account, md5, {'auto_description': str(description)[:6000],
                    'auto_name': name[1].strip()[:100] if name else '', 'auto_tags': [t[:40] for t in labels if t][:30],
                    'state': 'ready', 'error': ''})
        except asyncio.CancelledError:
            self.store.save(account, md5, {'state': 'queued'})
            raise
        except Exception as error:
            attempts = row.get('attempts', 0) + 1
            self.store.save(account, md5, {'state': 'queued' if attempts < 3 else 'failed', 'attempts': attempts,
                'due': time.time() + 30 * 2 ** attempts, 'error': f'后台处理失败（{type(error).__name__}），可重试'})

    async def _run(self):
        while not self.closing:
            try:
                active = self.feature.enabled and self.feature.config.load().stickers_enabled
                if time.time() >= self.next_gc:
                    await self.collect_assets()
                for account in self.accounts():
                    if account in self.sync_requests or active and time.time() >= self.next_sync.get(account, 0):
                        self.sync_requests.discard(account)
                        try:
                            await self.sync(account=account)
                        except Exception as error:
                            self.next_sync[account] = time.time() + 300
                            self.store.state('sync:' + account, {'at': time.time(), 'error': f'同步失败（{type(error).__name__}），保留上次目录'})
                if active:
                    row = next((item for item in self.store.list() if item['state'] == 'queued' and item.get('due', 0) <= time.time()), None)
                    if row:
                        await self._enrich(row)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Runtime status is available in the catalogue; do not spin on
                # a disk error or print remote URLs/credentials into logs.
                await asyncio.sleep(5)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 15)
            except TimeoutError:
                pass

    async def start(self, bot):
        self.bot = bot
        self.closing = False
        self.worker = asyncio.create_task(self._run())

    async def stop(self):
        self.closing = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
