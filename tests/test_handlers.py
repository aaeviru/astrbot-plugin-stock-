"""Offline handler tests with AstrBot API shims; NOT a real QQ integration test."""

import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import SqliteRepository


class FakeAt:
    def __init__(self, qq):
        self.qq = qq


class GroupRegistrar:
    def command(self, name):
        return lambda function: function


class FilterShim:
    def command_group(self, name):
        return lambda function: GroupRegistrar()


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeEvent:
    def __init__(self, user='A', group='g1', message_id='m1', mentions=None,
                 nickname='', self_id='bot-id'):
        self.user = user
        self.group = group
        self.message_obj = types.SimpleNamespace(
            message_id=message_id,
            message=[FakeAt(x) for x in (mentions or [])],
        )
        self.nickname = nickname
        self.self_id = self_id
        self.unified_msg_origin = f'qq-1:{group}:{user}'

    def get_platform_id(self):
        return 'qq-1'

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return self.nickname

    def get_self_id(self):
        return self.self_id

    def plain_result(self, message):
        return message


async def consume(handler):
    return [item async for item in handler]


class AstrBotHandlersOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Inject minimal API shims to import the real main.py and identity.py
        # in a pure-Python test environment without the AstrBot runtime.
        astrbot = types.ModuleType('astrbot')
        api = types.ModuleType('astrbot.api')
        event = types.ModuleType('astrbot.api.event')
        star = types.ModuleType('astrbot.api.star')
        comp = types.ModuleType('astrbot.api.message_components')
        core = types.ModuleType('astrbot.core')
        core_star = types.ModuleType('astrbot.core.star')
        core_filter = types.ModuleType('astrbot.core.star.filter')
        core_cmd = types.ModuleType('astrbot.core.star.filter.command')
        core.__path__ = []
        core_star.__path__ = []
        core_filter.__path__ = []
        core_cmd.GreedyStr = str
        api.__path__ = []
        astrbot.__path__ = []
        api.logger = logging.getLogger('astrbot-test')
        event.filter = FilterShim()
        event.AstrMessageEvent = FakeEvent
        star.Star = FakeStar
        star.Context = object
        star.StarTools = type('StarTools', (), {'get_data_dir': lambda _: None})
        comp.At = FakeAt
        cls._shims = {
            'astrbot': astrbot,
            'astrbot.api': api,
            'astrbot.api.event': event,
            'astrbot.api.star': star,
            'astrbot.api.message_components': comp,
            'astrbot.core': core,
            'astrbot.core.star': core_star,
            'astrbot.core.star.filter': core_filter,
            'astrbot.core.star.filter.command': core_cmd,
        }
        cls._patch = patch.dict(sys.modules, cls._shims)
        cls._patch.start()
        cls._identity = importlib.import_module('astrbot_plugin_portfolio.identity')
        cls._main = importlib.import_module('astrbot_plugin_portfolio.main')

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('astrbot_plugin_portfolio.main', None)
        sys.modules.pop('astrbot_plugin_portfolio.identity', None)
        cls._patch.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SqliteRepository(Path(self.tmp.name) / 'portfolio.db')
        self.repo.open()
        self.plugin = self._main.PortfolioPlugin(None)
        self.plugin.repo = self.repo

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def call(self, handler):
        return asyncio.run(consume(handler))[0]

    def test_commands_public_read_private_write_and_group_scope(self):
        a = FakeEvent('A', 'g1', 'm1', nickname='Alice')
        self.assertIn('持仓已校准', self.call(self.plugin.set_holding(a, '9432.T', 100, '150')))
        self.assertIn('#1', self.call(self.plugin.members(FakeEvent('B', 'g1', 'list1'))))
        self.assertIn('100 股', self.call(self.plugin.view(FakeEvent('B', 'g1'), '#1')))
        # B changes B's own holdings, never A's holdings.
        self.call(self.plugin.buy(FakeEvent('B', 'g1', 'm2', nickname='Bob'), '9432.T', 5, '200'))
        self.assertEqual(self.repo.get_holdings(Owner('qq-1', 'g1', 'A'))[0].quantity, 100)
        self.assertEqual(self.repo.get_holdings(Owner('qq-1', 'g1', 'B'))[0].quantity, 5)
        self.assertIn('2 人', self.call(self.plugin.summary(FakeEvent('C', 'g1'))))
        self.assertIn('105 股', self.call(self.plugin.summary(FakeEvent('C', 'g1'))))
        self.assertIn('尚未登记', self.call(self.plugin.view(FakeEvent('C', 'g2'), 'A')))

    def test_structured_at_onebot_and_official_fallback(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'm1'), 'NVDA', 3, '100'))
        # OneBot-like: @bot and @A in same message; bot mention ignored.
        self.assertIn('NVDA', self.call(self.plugin.view(
            FakeEvent('B', 'g1', mentions=['bot-id', 'A']), '@Alice'
        )))
        # QQ official may NOT expose an @target; tell the user to use # number.
        self.assertIn('无法识别', self.call(self.plugin.view(
            FakeEvent('B', 'g1'), '@Alice'
        )))
        self.assertIn('NVDA', self.call(self.plugin.view(
            FakeEvent('B', 'g1'), '#1'
        )))

    def test_private_messages_do_not_read_or_write(self):
        self.assertIn('仅支持群聊', self.call(self.plugin.set_holding(
            FakeEvent('A', '', 'm1'), 'NVDA', 1, '100'
        )))
        self.assertIn('仅支持群聊', self.call(self.plugin.members(FakeEvent('A', ''))))
        self.assertIn('仅支持群聊', self.call(self.plugin.summary(FakeEvent('A', ''))))
        self.assertEqual(self.repo.get_history(Owner('qq-1', '', 'A')), [])


if __name__ == '__main__':
    unittest.main()
