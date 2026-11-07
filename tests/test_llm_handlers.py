"""LLM handlers: simulated AstrBot events, NOT a live QQ integration."""
import asyncio
import json
import types
import unittest

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.tests.test_handlers import AstrBotHandlersOfflineTests, FakeEvent, consume


class MockLLM:
    def __init__(self, output=None, provider='test-llm'):
        self.output = output
        self.provider = provider
        self.calls = []

    async def get_current_chat_provider_id(self, umo):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(completion_text=self.output)


def model_json(status='valid', action='BUY', symbol='NTT', quantity=200, price='165.7'):
    if status != 'valid':
        action = symbol = quantity = price = None
    return json.dumps({'status': status, 'action': action, 'symbol': symbol,
                       'quantity': quantity, 'unit_price': price}, ensure_ascii=False)


class NaturalLanguageHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AstrBotHandlersOfflineTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        AstrBotHandlersOfflineTests.tearDownClass.__func__(cls)

    def setUp(self):
        AstrBotHandlersOfflineTests.setUp(self)
        self.llm = MockLLM(model_json())
        self.plugin.context = self.llm
        self.a = Owner('qq-1', 'g1', 'A')
        self.b = Owner('qq-1', 'g1', 'B')

    def tearDown(self):
        AstrBotHandlersOfflineTests.tearDown(self)

    def call_async(self, generator):
        return asyncio.run(consume(generator))[0]

    def test_parse_writes_immediately_and_undo_redo(self):
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse1'), '今天165.7买了200股NTT'))
        self.assertIn('买入已记录', reply)
        self.assertIn('撤回', reply)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 200)
        self.assertEqual(self.repo.get_holdings(self.b), [])
        self.assertEqual(len(self.llm.calls), 1)
        reply = self.call_async(self.plugin.toggle_latest(FakeEvent('A', 'g1', 't1')))
        self.assertIn('已撤回', reply)
        self.assertEqual(self.repo.get_holdings(self.a), [])
        reply = self.call_async(self.plugin.toggle_latest(FakeEvent('A', 'g1', 't2')))
        self.assertIn('已恢复', reply)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 200)
        self.assertEqual(len(self.llm.calls), 1)

    def test_duplicate_parse_skips_second_llm_call(self):
        self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse2'), '今天165.7买了200股NTT'))
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse2'), '今天165.7买了200股NTT'))
        self.assertIn('已处理过', reply)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 200)

    def test_reject_plan_even_if_llm_would_claim_valid(self):
        response = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse3'), '我打算明天买入200股NTT'))
        self.assertIn('计划', response)
        self.assertEqual(len(self.llm.calls), 0)
        self.assertEqual(self.repo.get_holdings(self.a), [])

    def test_model_rejects_vague_trade(self):
        self.llm.output = model_json(status='reject')
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse4'), '昨天买了一些NTT'))
        self.assertIn('无法记账', reply)
        self.assertEqual(self.repo.get_holdings(self.a), [])

    def test_bad_model_output_no_write(self):
        self.llm.output = '```sql\nDELETE FROM holdings;\n```'
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse5'), '今天165.7买了200股NTT'))
        self.assertIn('JSON', reply)
        self.assertEqual(self.repo.get_holdings(self.a), [])

    def test_no_llm_call_in_private_chat(self):
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', '', 'parse6'), '今天165.7买了200股NTT'))
        self.assertIn('仅支持群聊', reply)
        self.assertEqual(len(self.llm.calls), 0)

    def test_set_overwrites_and_can_undo_without_confirmation(self):
        self.llm.output = model_json(action='SET', symbol='ASICS', quantity=100, price='4564')
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse7'), '当前持有亚瑟士100股成本4564'))
        self.assertIn('持仓已校准', reply)
        self.assertEqual(self.repo.get_holdings(self.a)[0].symbol, '7936.T')
        self.call_async(self.plugin.toggle_latest(FakeEvent('A', 'g1', 't7')))
        self.assertEqual(self.repo.get_holdings(self.a), [])

    def test_missing_provider_does_not_write(self):
        self.llm.provider = None
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse8'), '今天165.7买了200股NTT'))
        self.assertIn('未配置', reply)
        self.assertEqual(len(self.llm.calls), 0)

    def test_llm_failure_does_not_write(self):
        self.llm.output = None
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse9'), '今天165.7买了200股NTT'))
        self.assertIn('无法记账', reply)
        self.assertEqual(self.repo.get_holdings(self.a), [])

    def test_other_user_cannot_undo_your_trade(self):
        self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse10'), '今天165.7买了200股NTT'))
        reply = self.call_async(self.plugin.toggle_latest(FakeEvent('B', 'g1', 't10')))
        self.assertIn('没有可撤回', reply)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 200)

    def test_invalid_sale_remains_unwritten(self):
        self.llm.output = model_json(action='SELL', quantity=200, price='170')
        reply = self.call_async(self.plugin.parse_natural(
            FakeEvent('A', 'g1', 'parse11'), '今天170卖了200股NTT'))
        self.assertIn('不能卖出', reply)
        self.assertEqual(self.repo.get_holdings(self.a), [])


if __name__ == '__main__':
    unittest.main()
