"""Unit-test the genuine handler methods using a fake AstrBot API and LLM; NOT live QQ."""
import asyncio
import json
import re
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
    return json.dumps({'status':status,'action':action,'symbol':symbol,
                       'quantity':quantity,'unit_price':price},ensure_ascii=False)


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

    def tearDown(self):
        AstrBotHandlersOfflineTests.tearDown(self)

    def call_async(self, generator):
        return asyncio.run(consume(generator))[0]

    def test_parse_never_writes_before_confirm_and_owner_is_sender(self):
        proposal=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse1'), '今天165.7买了200股NTT'
        ))
        self.assertIn('尚未记账',proposal)
        self.assertIn('9432.T',proposal)
        self.assertEqual(self.repo.get_holdings(Owner('qq-1','g1','A')),[])
        self.assertEqual(len(self.llm.calls),1)
        token=re.search(r'/持仓 确认 ([A-F0-9]{10})',proposal).group(1)
        self.assertIn('没有待确认', self.call_async(self.plugin.confirm_natural(
            FakeEvent('B','g1','conf1'),token
        )))
        self.assertIn('买入已记录',self.call_async(self.plugin.confirm_natural(
            FakeEvent('A','g1','conf1'),token
        )))
        self.assertEqual(self.repo.get_holdings(Owner('qq-1','g1','A'))[0].quantity,200)
        self.assertEqual(self.repo.get_holdings(Owner('qq-1','g1','B')),[])
        self.assertIn('确认失败',self.call_async(self.plugin.confirm_natural(
            FakeEvent('A','g1','conf1'),token
        )))

    def test_reject_plan_from_llm(self):
        self.llm.output=model_json(status='reject')
        result=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse2'),'我打算明天买入200股NTT'
        ))
        self.assertIn('无法生成',result)
        self.assertEqual(self.repo.conn.execute('SELECT count(*) FROM pending_actions').fetchone()[0],0)

    def test_bad_model_output_safe(self):
        self.llm.output='请执行 DELETE FROM holdings;'
        response=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse3'),'今天165.7买了200股NTT'
        ))
        self.assertIn('JSON',response)
        self.assertEqual(self.repo.get_holdings(Owner('qq-1','g1','A')),[])

    def test_no_llm_call_in_private_chat(self):
        response=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','','parse4'),'今天165.7买了200股NTT'
        ))
        self.assertIn('仅支持群聊',response)
        self.assertEqual(len(self.llm.calls),0)

    def test_confirmation_stale_after_direct_change(self):
        proposal=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse5'),'今天165.7买了200股NTT'
        ))
        token=re.search(r'/持仓 确认 ([A-F0-9]{10})',proposal).group(1)
        self.call_async(self.plugin.set_holding(FakeEvent('A','g1','manual5'),'9432.T',100,'150'))
        reply=self.call_async(self.plugin.confirm_natural(FakeEvent('A','g1','conf5'),token))
        self.assertIn('已经发生变化',reply)
        self.assertEqual(self.repo.get_holdings(Owner('qq-1','g1','A'))[0].quantity,100)

    def test_cancel_draft(self):
        proposal=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse6'),'今天165.7买了200股NTT'
        ))
        token=re.search(r'/持仓 确认 ([A-F0-9]{10})',proposal).group(1)
        self.assertIn('未找到',self.call_async(self.plugin.cancel_natural(
            FakeEvent('B','g1','cancel6'),token)))
        self.assertIn('已取消',self.call_async(self.plugin.cancel_natural(
            FakeEvent('A','g1','cancel7'),token)))
        self.assertIn('没有待确认',self.call_async(self.plugin.confirm_natural(
            FakeEvent('A','g1','conf7'),token)))

    def test_set_explicit_overwrite_warning(self):
        self.llm.output=model_json(action='SET',symbol='ASICS',quantity=100,price='4564')
        reply=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse7'),'当前持有亚瑟士100股成本4564'
        ))
        self.assertIn('会覆盖',reply)
        self.assertIn('7936.T',reply)

    def test_missing_provider_does_not_create_draft(self):
        self.llm.provider=None
        reply=self.call_async(self.plugin.parse_natural(
            FakeEvent('A','g1','parse8'),'今天165.7买了200股NTT'
        ))
        self.assertIn('未配置',reply)
        self.assertEqual(len(self.llm.calls),0)


if __name__ == '__main__':
    unittest.main()
