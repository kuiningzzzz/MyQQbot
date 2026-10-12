from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from src.feature.atom import Feature, MessageContext
from src.onebot_error import OneBotError
from .schedule import display_time, display_lunar_time
from .storage import TaskStore

logger = logging.getLogger(__name__)
USAGE = '''北京时间定时任务（群聊和私聊均可）：
/timing-task -y MM-DD-HH-MM-SS 文本：每年
/timing-task -m DD-HH-MM-SS 文本：每月
/timing-task -w 01至07-HH-MM-SS 文本：每周，01周一、07周日
/timing-task -d HH-MM-SS 文本：每天
/timing-task -o YY-MM-DD-HH-MM-SS 文本：仅一次，也支持四位年份
各项用-分隔，月、日、星期、时分秒无需补零，如-y 2-3-8-0-0；一至两位年份按2000至2099年解释。
中国农历给选项加-cn：-y-cn 每农历年，-m-cn 每农历月，-o-cn 农历指定日；时间数字格式不变。
-w-cn/-d-cn 与每周/每天相同，时分秒始终北京时间。农历支持1900至2100年。
农历年循环只用普通月份，不在闰月重复提醒；农历月循环包含闰月，没有三十的月份跳过。
将 /timing-task 换成 /timing-task-AI，后方内容即AI提示词。
/timing-task list 或 /timing-task-AI list：聊天记录形式列出任务和ID
/timing-task del ID 或 /timing-task-AI del ID：删除对应任务
保留内容中的空格、换行、群聊@；循环时间已过自动安排下一次。
每月不存在的日期跳过该月；2月29日按年循环跳到下个闰年。
任何群友可管理本会话任务；不可查看、修改或创建其他会话任务。
自然语言可管理本会话的固定消息和AI任务，由主对话spawn执行者调用schedule工具。
确定的文本使用/timing-task；需要执行时动态获取或AI生成的内容使用/timing-task-AI。
服务离线后恢复只执行一次到期任务，不补发全部错过的周期；结果未知不自动重试。'''


def normalize_segments(segments, private=False):
    if not isinstance(segments, list):
        raise ValueError('消息段须为列表')
    result = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            raise ValueError('消息段格式无效')
        kind, data = segment.get('type'), segment.get('data', {})
        if kind == 'text':
            value = data.get('text')
            if not isinstance(value, str):
                raise ValueError('文本消息格式无效')
            if value:
                result.append({'type': 'text', 'data': {'text': value}})
        elif kind == 'at':
            qq = str(data.get('qq', ''))
            if not re.fullmatch(r'[1-9][0-9]{0,19}', qq):
                raise ValueError('仅支持艾特具体QQ号，不支持@全体成员')
            result.append({'type': 'text', 'data': {'text': '@' + qq}} if private else {'type': 'at', 'data': {'qq': qq}})
        else:
            raise ValueError('定时文本仅支持文字和艾特，请移除图片、回复或其他消息段')
    if not result or not ''.join(s['data'].get('text', '@') for s in result).strip():
        raise ValueError('任务内容不能为空')
    if len(json.dumps(result, ensure_ascii=False)) > 24000:
        raise ValueError('任务内容过长（最多约12000字）')
    return result


def text_segments(value, private=False):
    """Admin and tools accept explicit at markup; other CQ markup stays literal."""
    if not isinstance(value, str):
        raise ValueError('消息内容须为文字')
    parts, position = [], 0
    for match in re.finditer(r'\[CQ:at,qq=([1-9][0-9]{0,19})\]', value):
        parts.append({'type': 'text', 'data': {'text': value[position:match.start()]}})
        parts.append({'type': 'at', 'data': {'qq': match[1]}})
        position = match.end()
    parts.append({'type': 'text', 'data': {'text': value[position:]}})
    return normalize_segments(parts, private)


def body_text(segments):
    return ''.join(s['data']['text'] if s['type'] == 'text' else '[CQ:at,qq=' + s['data']['qq'] + ']' for s in segments)


def split_command(ctx):
    segments = ctx.event.get('message')
    if not isinstance(segments, list):
        segments = [{'type': 'text', 'data': {'text': ctx.raw_msg or ctx.msg}}]
    # Prefix may span several text segments; at/nontext can only occur in body.
    prefix = ''
    for s in segments:
        if s.get('type') != 'text':
            break
        prefix += str(s.get('data', {}).get('text', ''))
    match = re.match(r'^\s*(/timing-task(?:-AI)?)(?=\s|$)', prefix, re.I)
    if not match:
        return None
    kind = 'ai' if match[1].lower().endswith('-ai') else 'fixed'
    rest = prefix[match.end():]
    control = re.fullmatch(r'\s*(list|del(?:\s+[a-f0-9]{32})?)\s*', rest, re.I)
    if control and all(s.get('type') == 'text' for s in segments):
        return kind, control[1].lower().split(), []
    header = re.match(r'\s+(-[ymwdo](?:-cn)?)\s+(\S+)\s', rest)
    if not header:
        raise ValueError(USAGE)
    consume = match.end() + header.end()
    body = []
    for segment in segments:
        if consume:
            if segment.get('type') != 'text':
                raise ValueError('请先填写循环选项和时间，再填写文本及艾特')
            value = segment['data']['text']
            if len(value) <= consume:
                consume -= len(value)
                continue
            segment = {'type': 'text', 'data': {'text': value[consume:]}}
            consume = 0
        body.append(segment)
    return kind, [header[1], header[2]], normalize_segments(body, ctx.message_type == 'private')


class TimingFeature(Feature):
    name = 'timing'
    title = '定时任务'
    description = '北京时间固定消息与 AI 分支任务，支持年/月/周/日/一次性循环及会话内管理。'

    def __init__(self, ai, master, path=Path('data/timing/tasks.sqlite3')):
        self.ai, self.master = ai, master
        self.store = TaskStore(path)
        self.enabled = True
        self.bot = None
        self.loop = None
        self.running = {}
        self.wake = asyncio.Event()
        self.last_error = ''
        self.last_prune = 0

    def agent_usage(self, owner=False):
        return {'help': USAGE, 'tools': ['schedule_create', 'schedule_list', 'schedule_update', 'schedule_delete'],
                'boundary': '工具可管理当前QQ会话固定消息和AI任务，不接受目标会话；其他群和私聊隔离。主对话须spawn执行者调用。'}

    @staticmethod
    def scene(ctx):
        if ctx.message_type not in {'group', 'private'}:
            raise ValueError('仅支持群聊和私聊')
        identifier = ctx.group_id if ctx.message_type == 'group' else ctx.user_id
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(identifier)):
            raise ValueError('会话ID无效')
        return f'{ctx.message_type}:{identifier}'

    def validate(self, body):
        allowed = {'kind', 'scene', 'flag', 'time', 'content', 'segments', 'prompt', 'enabled', 'delivery', 'requester', 'owner_grant', 'bot_qq'}
        if set(body) - allowed:
            raise ValueError('不支持的任务字段：' + ','.join(sorted(set(body) - allowed)))
        data = dict(body)
        if data.get('kind') not in {'fixed', 'ai'} or not re.fullmatch(r'(group|private):[1-9][0-9]{0,19}', str(data.get('scene', ''))):
            raise ValueError('任务类型或会话ID无效')
        if not isinstance(data.get('enabled', True), bool) or not isinstance(data.get('owner_grant', False), bool):
            raise ValueError('启用和主人授权须为布尔值')
        if data.get('delivery', 'auto') not in {'auto', 'message', 'silent'}:
            raise ValueError('AI发送方式须为auto/message/silent')
        if data['kind'] == 'fixed':
            data['segments'] = normalize_segments(data['segments'], data['scene'].startswith('private:')) if 'segments' in data else text_segments(data.get('content', ''), data['scene'].startswith('private:'))
            data.pop('content', None)
            data.pop('prompt', None)
            data['owner_grant'] = False
        elif not isinstance(data.get('prompt'), str) or not data['prompt'].strip() or len(data['prompt']) > 12000:
            raise ValueError('AI提示词须为1至12000字')
        else:
            data.pop('segments', None)
            data.pop('content', None)
        data.setdefault('delivery', 'auto')
        data.setdefault('enabled', True)
        data.setdefault('requester', '0')
        data.setdefault('owner_grant', False)
        data.setdefault('bot_qq', '')
        if not str(data['requester']).isdigit() or (data['bot_qq'] and not str(data['bot_qq']).isdigit()):
            raise ValueError('请求者或机器人QQ无效')
        if data['owner_grant'] and not self.master.is_master(int(data['requester'])):
            raise ValueError('越界操作只能授权给当前绑定的主人QQ；请先绑定主人')
        if data['owner_grant'] and data['scene'].startswith('private:') and data['scene'].split(':')[1] != str(data['requester']):
            raise ValueError('主人授权的私聊任务只能绑定主人自己的私聊')
        return data

    def save(self, body, identifier=None, expected=None):
        result = self.store.save(self.validate(body), identifier=identifier, expected=expected)
        self.wake.set()
        return self.describe(result)

    @staticmethod
    def describe(task):
        command = '/timing-task-AI' if task['kind'] == 'ai' else '/timing-task'
        content = task['prompt'] if task['kind'] == 'ai' else body_text(task['segments'])
        lunar = task['flag'].endswith('-cn')
        return {**task, 'content': content, 'command': f"{command} {task['flag']} {task['time']} {content}",
                'calendar': 'lunar' if lunar else 'solar', 'next_run_text': display_time(task['next_run']),
                'next_run_lunar_text': display_lunar_time(task['next_run']) if lunar else ''}

    async def handle(self, ctx):
        if not (ctx.raw_msg or ctx.msg).lstrip().lower().startswith('/timing-task'):
            return False
        try:
            parsed = split_command(ctx)
            if parsed is None:
                return False
            kind, args, body = parsed
            scene = self.scene(ctx)
            if args[0] == 'list':
                items = self.store.list(scene=scene, kind=kind)
                if not items:
                    await ctx.bot.reply(ctx, '当前会话没有这类定时任务。')
                for offset in range(0, len(items), 99):
                    nodes = []
                    for item in items[offset:offset + 99]:
                        data = self.describe(item)
                        lunar_info = f"\n对应农历：{data['next_run_lunar_text']}" if data['calendar'] == 'lunar' else ''
                        nodes.append({'type': 'node', 'data': {'name': '定时任务', 'uin': str(ctx.event.get('self_id') or 0),
                            'content': [{'type': 'text', 'data': {'text': f"ID：{data['id']}\n{'启用' if data['enabled'] else '暂停/已完成'} · 北京时间 {data['next_run_text']}{lunar_info}\n{data['command']}"}}]}})
                    action = 'send_group_forward_msg' if ctx.message_type == 'group' else 'send_private_forward_msg'
                    target = {'group_id': ctx.group_id} if ctx.message_type == 'group' else {'user_id': ctx.user_id}
                    await ctx.bot.call_action(action, {**target, 'messages': nodes}, ctx.websocket)
            elif args[0] == 'del':
                if len(args) != 2:
                    raise ValueError('用法：/timing-task del ID 或 /timing-task-AI del ID')
                success = self.store.delete(args[1], scene=scene, kind=kind)
                self.wake.set()
                await ctx.bot.reply(ctx, '已删除定时任务。' if success else '当前会话没有这个类型或ID的任务。')
            else:
                owner = self.master.is_master(ctx.user_id)
                data = {'kind': kind, 'scene': scene, 'flag': args[0], 'time': args[1], 'requester': str(ctx.user_id), 'owner_grant': owner, 'bot_qq': str(ctx.event.get('self_id') or '')}
                data.update({'segments': body} if kind == 'fixed' else {'prompt': body_text(body)})
                item = self.save(data)
                lunar_info = f"\n对应农历：{item['next_run_lunar_text']}" if item['calendar'] == 'lunar' else ''
                await ctx.bot.reply(ctx, f"已创建{'AI' if kind == 'ai' else '固定消息'}定时任务。\nID：{item['id']}\n下次：北京时间 {item['next_run_text']}{lunar_info}")
        except (ValueError, KeyError) as exc:
            await ctx.bot.reply(ctx, str(exc))
        except TimeoutError:
            logger.warning('定时任务指令发送回执未知，不重试')
        except OneBotError as exc:
            await ctx.bot.reply(ctx, '发送失败：' + ('权限不足' if exc.permission_denied else exc.detail))
        return True

    async def tool(self, ctx, name, args):
        if not self.enabled:
            return {'success': False, 'code': 'feature_disabled'}
        scene = self.scene(ctx)
        permitted = {'schedule_list': {'kind'}, 'schedule_delete': {'task_id'},
                     'schedule_create': {'kind', 'flag', 'time', 'content', 'prompt', 'delivery'},
                     'schedule_update': {'task_id', 'flag', 'time', 'content', 'prompt', 'delivery', 'enabled'}}
        if name not in permitted or set(args) - permitted[name]:
            return {'success': False, 'code': 'scene_boundary', 'error': '仅管理本会话固定消息和AI任务，不能指定会话、身份或授权'}
        try:
            if name == 'schedule_list':
                kind = args.get('kind')
                if 'kind' in args and kind not in ('fixed', 'ai'):
                    raise ValueError('任务类型须为fixed或ai；省略kind列出两类任务')
                return {'success': True, 'tasks': [self.describe(t) for t in self.store.list(scene=scene, kind=kind)]}
            previous = None
            if name != 'schedule_create':
                previous = self.store.get(args.get('task_id'), scene=scene)
                if not previous:
                    return {'success': False, 'code': 'task_not_found'}
            if name == 'schedule_delete':
                self.store.delete(previous['id'], scene=scene, kind=previous['kind'], expected=previous['revision'])
                self.wake.set()
                return {'success': True}
            kind = previous['kind'] if previous else args.get('kind', 'ai')
            if kind not in ('fixed', 'ai'):
                raise ValueError('任务类型须为fixed或ai')
            if kind == 'fixed' and ('prompt' in args or 'delivery' in args):
                raise ValueError('固定消息使用content正文，不接受AI prompt或delivery；到时直接发送原文')
            if kind == 'ai' and 'content' in args:
                raise ValueError('AI任务使用prompt提示词，不接受固定消息content')
            data = {k: v for k, v in (previous or {}).items() if k in {'flag', 'time', 'segments', 'prompt', 'delivery', 'enabled', 'bot_qq'}}
            if 'content' in args:
                data.pop('segments', None)
            data.update({k: v for k, v in args.items() if k != 'task_id'})
            data.update(kind=kind, scene=scene, requester=str(ctx.user_id), owner_grant=kind == 'ai' and self.master.is_master(ctx.user_id), bot_qq=str(ctx.event.get('self_id') or data.get('bot_qq') or ''))
            saved = self.save(data, previous['id'] if previous else None, previous['revision'] if previous else None)
            return {'success': True, 'task': saved}
        except (ValueError, KeyError) as exc:
            return {'success': False, 'code': 'invalid_task', 'error': str(exc)}

    def on_enabled_changed(self, enabled):
        self.enabled = enabled
        self.wake.set()

    def connection(self, job):
        clients = getattr(self.bot, 'connected_clients', set())
        accounts = getattr(self.bot, '_self_ids', {})
        return next((ws for ws in clients if not job.get('bot_qq') or accounts.get(ws) == job['bot_qq']), None)

    def valid(self, job):
        latest = self.store.get(job['id'])
        # claim disables a once-only task, so compare revision rather than enabled.
        return bool(self.enabled and latest and latest['revision'] == job['revision'] and self.connection(job) is not None
                    and (not job.get('owner_grant') or self.master.is_master(int(job['requester'])))
                    and (job['kind'] == 'fixed' or self.ai.enabled and self.ai.config.load().chat_enabled))

    async def execute(self, job):
        status, detail = 'failed', ''
        try:
            if not self.valid(job):
                status = 'cancelled'
                return
            scene_type, scene_id = job['scene'].split(':')
            ws = self.connection(job)
            qq = int(job['requester']) if scene_type == 'group' else int(scene_id)
            ctx = MessageContext(bot=self.bot, websocket=ws, event={'self_id': getattr(self.bot, '_self_ids', {}).get(ws), 'message': []}, msg=job.get('prompt', ''), raw_msg=job.get('prompt', ''), message_type=scene_type, user_id=qq, group_id=int(scene_id) if scene_type == 'group' else None)
            if job['kind'] == 'fixed':
                await self.bot.reply(ctx, job['segments'])
                status, detail = 'completed', '固定消息发送成功'
            else:
                result = await self.ai.engine.run_scheduled(ctx, job, lambda: self.valid(job))
                status = 'cancelled' if result.get('cancelled') else 'completed' if result.get('finish_reason') == 'completed' else 'failed'
                detail = result.get('final_response') or result.get('error') or result.get('finish_reason', '')
                if result.get('delivery_unknown'):
                    status = 'unknown'
        except asyncio.CancelledError:
            status, detail = 'cancelled', '任务被删除、修改、暂停，或服务/功能已停止；文件修改不会回滚'
            raise
        except TimeoutError:
            status, detail = 'unknown', '发送回执超时，结果未知，不自动重试'
        except OneBotError as exc:
            detail = '权限不足' if exc.permission_denied else exc.detail
        except Exception as exc:
            detail = str(exc)
            logger.exception('定时任务执行失败：%s', job['id'])
        finally:
            self.store.finish(job['run_id'], status, detail)

    async def tick(self):
        if time.time() - self.last_prune > 3600:
            self.store.prune()
            self.last_prune = time.time()
        for identifier, (job, task) in list(self.running.items()):
            if not task.done() and not self.valid(job):
                task.cancel()
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                if task.cancelled():
                    self.store.finish(job['run_id'], 'cancelled', '执行前任务已撤销')
                self.running.pop(identifier, None)
        if not self.enabled or not self.bot:
            return
        now = time.time()
        for job in self.store.due(now):
            if sum(1 for running, _ in self.running.values() if running['kind'] == job['kind']) >= 4:
                continue
            if not job['enabled'] or not job['next_run'] or job['next_run'] > now or job['id'] in self.running or not self.connection(job):
                continue
            if job['kind'] == 'ai' and (not self.ai.enabled or not self.ai.config.load().chat_enabled):
                continue
            claimed = self.store.claim(job['id'], now)
            if claimed:
                self.running[job['id']] = (claimed, asyncio.create_task(self.execute(claimed)))

    async def _loop(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
                self.last_error = ''
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception('定时任务调度失败')
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def start(self, bot):
        self.bot = bot
        self.store.recover()
        self.loop = asyncio.create_task(self._loop())

    async def stop(self):
        tasks = ([self.loop] if self.loop else []) + [task for _, task in self.running.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job, task in self.running.values():
            if task.cancelled():
                self.store.finish(job['run_id'], 'cancelled', '服务停止，任务已取消')
        self.running.clear()
        self.loop = None
