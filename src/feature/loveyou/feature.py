import base64
import re
from pathlib import Path

from src.feature.atom import Feature
from .storage import Store


class LoveyouFeature(Feature):
    name = 'loveyou'
    title = '赞助打榜'
    description = '/loveyou 查看收款码和赞助榜；主人可用 /loveyou-add 人名 数目 增减金额。'
    default_enabled = True

    def __init__(self, master_service, root=Path('data/loveyou')):
        self.master_service = master_service
        self.store = Store(Path(root))

    def agent_usage(self, owner=False):
        return {'commands': ['/loveyou：发送收款码与全局赞助榜，金额降序、隐藏零金额。'] +
                (['/loveyou-add 人名 数目：主人手动增减金额，支持负数。'] if owner else []),
                'notes': '赞助金额由主人或admin手工登记，不自动识别支付到账。'}

    async def handle(self, ctx):
        if re.match(r'^/loveyou-add(?:\s|$)', ctx.msg):
            if not self.master_service.is_master(ctx.user_id):
                await ctx.bot.reply(ctx, '只有主人可以修改赞助金额。')
                return True
            # ctx.msg is casefolded by QQBot; preserve the actual display name.
            raw = (ctx.raw_msg or ctx.msg).strip()
            match = re.fullmatch(r'/loveyou-add\s+(.+)\s+(\S+)', raw, re.I)
            if not match:
                await ctx.bot.reply(ctx, '格式：/loveyou-add 人名 数目，例如 /loveyou-add 小鱼 -1.50')
                return True
            try:
                total = self.store.add(match[1], match[2])
            except ValueError as exc:
                await ctx.bot.reply(ctx, str(exc))
                return True
            await ctx.bot.reply(ctx, [{'type': 'text', 'data': {'text': f'{match[1].strip()} 喂饭数目 {total}'}}])
            return True
        if ctx.msg != '/loveyou':
            return False
        entries, image = self.store.snapshot()
        if not image:
            await ctx.bot.reply(ctx, '还没有设置收款码，请管理员在“赞助打榜”中上传图片。')
            return True
        messages = [[{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(image).decode()}}]]
        messages.extend([{'type': 'text', 'data': {'text': f"{entry['name']} 喂饭数目 {entry['amount']}"}}]
                        for entry in entries if entry['cents'] != 0)
        for start in range(0, len(messages), 99):
            await ctx.bot.send_forward_msg(ctx, messages[start:start + 99])
        return True
