"""Keep mandatory prompts small while preserving on-demand information access."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.feature.ai.engine import CHAT_RULES, WORKER_RULES, SCHEDULED_RULES
from src.feature.ai.feature import AiFeature
from src.feature.ai.self_context import self_context
from src.feature.atom import MessageContext
from src.feature.help.feature import HelpFeature
from src.feature.manager import FeatureManager


class PromptContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.f = AiFeature(self.root / 'ai')
        self.f.config.save({'vector_index_enabled': False, 'memory_auto_learn': False, 'bot_qq': '999'})
        self.master = SimpleNamespace(is_master=lambda qq: qq == 123)
        self.help = HelpFeature(self.root / 'help.json', self.master)
        FeatureManager(self.root / 'features.json', [self.f, self.help])

    async def asyncTearDown(self):
        await self.f.stop()
        self.temp.cleanup()

    async def test_large_help_and_full_guide_are_not_reinjected(self):
        self.help.save_messages(['PUBLIC_HELP_MARKER_' * 1000], ['MASTER_HELP_MARKER_' * 1000])
        for role, rules in [('main', CHAT_RULES), ('worker', WORKER_RULES), ('scheduled', SCHEDULED_RULES)]:
            system = self.f.identity.prompt() + rules + self_context(self.f, 'chat', role=role, owner=True)
            self.assertNotIn('PUBLIC_HELP_MARKER_', system)
            self.assertNotIn('MASTER_HELP_MARKER_', system)
            self.assertNotIn('## 整个 QQ 机器人是做什么的', system)
            self.assertLess(len(system), 4000)
        memory = self_context(self.f, 'memory')
        self.assertNotIn('PUBLIC_HELP_MARKER_', memory)
        state = json.loads(memory.split('【当轮宿主状态】\n', 1)[1])
        guide = Path(state['bot_guide']).read_text('utf-8')
        self.assertIn('name: qqbot-guide', guide)
        self.assertIn('## 整个 QQ 机器人是做什么的', guide)
        self.assertIn('/pixiv', guide)
        self.assertIn('-y-cn', guide)

    async def test_help_query_uses_frozen_owner_grant_and_live_binding(self):
        self.help.save_messages(['PUBLIC'], ['PRIVATE'])
        bot = SimpleNamespace(master_service=self.master)
        async def query(qq, **task):
            ctx = MessageContext(bot=bot, websocket=None, event={}, msg='help', message_type='private', user_id=qq, group_id=None)
            handler = self.f.engine.make_handler(ctx, f'private:{qq}', True, task, lambda: True)
            return await handler('get_bot_help', {'owner': True})
        def usage(result):
            return next(item['usage'] for item in result['application']['features'] if item['name'] == 'help')
        self.assertEqual((await query(123, owner=True))['code'], 'delegation_required')
        self.assertEqual(usage(await query(456, worker=True, owner=True)), {'public_messages': ['PUBLIC']})
        self.assertEqual(usage(await query(123, worker=True, owner=False)), {'public_messages': ['PUBLIC']})
        self.assertEqual(usage(await query(123, worker=True, owner=True))['master_messages'], ['PRIVATE'])
        self.master.is_master = lambda qq: False
        self.assertNotIn('master_messages', usage(await query(123, worker=True, owner=True)))
        self.assertNotIn('PRIVATE', '\n'.join(p.read_text('utf-8') for p in (self.f.workspace.root / '.agents').rglob('*.md')))


if __name__ == '__main__':
    unittest.main()
