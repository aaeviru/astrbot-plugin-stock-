"""MCP quotes and live portfolio valuation, tested entirely with fake MCP clients."""
import asyncio
import json
import types
import unittest
from decimal import Decimal

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.services.quotes import (
    AktoolsQuoteService, Quote, QuoteUnavailable, _extract_a_price,
    _unpack_mcp_response, compute_valuation,
)
from astrbot_plugin_portfolio.tests.test_handlers import (
    AstrBotHandlersOfflineTests, FakeEvent, consume,
)


def sample_response(code='600519', price='1600.50', kind='structured'):
    payload = {
        'name': 'stock_individual_info_em',
        'rows': [{'item': '股票代码', 'value': code},
                 {'item': '股票简称', 'value': '贵州茅台'},
                 {'item': '最新', 'value': price}],
        'returned_rows': 3,
        'total_rows': 3,
    }
    if kind == 'structured':
        return types.SimpleNamespace(isError=False, structuredContent={'result': payload}, content=[])
    if kind == 'text':
        return types.SimpleNamespace(isError=False, structuredContent=None,
                                     content=[types.SimpleNamespace(text=json.dumps(payload))])
    return types.SimpleNamespace(isError=True, structuredContent=None, content=[])


class FakeMCP:
    tools = [types.SimpleNamespace(name='get_data')]

    def __init__(self, response=None):
        self.response = response or sample_response()
        self.calls = []

    async def call_tool_with_reconnect(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class MCPContext:
    def __init__(self, mcp, server='AKShare'):
        self.mcp = mcp
        self.server = server
        self.llm_calls = 0

    def get_llm_tool_manager(self):
        return types.SimpleNamespace(mcp_client_dict={self.server: self.mcp})

    async def llm_generate(self, **kwargs):
        self.llm_calls += 1
        raise AssertionError('Quote requests must never call an LLM')


class QuoteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_quote_using_aktools_mcp_get_data(self):
        mcp = FakeMCP()
        service = AktoolsQuoteService(MCPContext(mcp))
        quote = await service.get_quote('600519.SH')
        self.assertEqual(quote.price, Decimal('1600.50'))
        self.assertEqual(quote.currency, 'CNY')
        self.assertIn('UTC', quote.fetched_at)
        self.assertEqual(mcp.calls[0]['tool_name'], 'get_data')
        self.assertEqual(mcp.calls[0]['arguments']['name'], 'stock_individual_info_em')
        self.assertEqual(mcp.calls[0]['arguments']['params'], {'symbol': '600519'})
        await service.get_quote('600519.SH')
        self.assertEqual(len(mcp.calls), 1, 'Quote must be cached for TTL')

    async def test_get_quote_from_text_content(self):
        service = AktoolsQuoteService(MCPContext(FakeMCP(sample_response(kind='text'))))
        self.assertEqual((await service.get_quote('600519.SH')).price, Decimal('1600.50'))

    async def test_unsupported_markets_never_call_mcp(self):
        mcp = FakeMCP()
        service = AktoolsQuoteService(MCPContext(mcp))
        out = await service.fetch_many(['9432.T', 'NVDA', '0700.HK', '600519.SH'])
        self.assertEqual(len(mcp.calls), 1)
        for name in ['9432.T', 'NVDA', '0700.HK']:
            self.assertIsNone(out[name].quote)
            self.assertIn('暂未接入', out[name].reason)
        self.assertIsNotNone(out['600519.SH'].quote)

    async def test_mismatched_market_suffix_is_rejected_without_request(self):
        mcp = FakeMCP()
        service = AktoolsQuoteService(MCPContext(mcp))
        result = await service.fetch_many(['600519.SZ'])
        self.assertIsNone(result['600519.SZ'].quote)
        self.assertIn('后缀不符', result['600519.SZ'].reason)
        self.assertEqual(mcp.calls, [])

    async def test_unavailable_mcp_is_a_missing_quote_not_fake_zero(self):
        result = await AktoolsQuoteService(None).fetch_many(['600519.SH'])
        self.assertIsNone(result['600519.SH'].quote)
        self.assertIn('MCP', result['600519.SH'].reason)

    async def test_mcp_error_fails_closed(self):
        service = AktoolsQuoteService(MCPContext(FakeMCP(sample_response(kind='error'))))
        with self.assertRaises(QuoteUnavailable):
            await service.get_quote('600519.SH')

    async def test_mismatch_missing_nan_and_zero_are_rejected(self):
        for code, price in [('000001', '1600'), ('600519', 'nan'), ('600519', '0'),
                            ('600519', None), ('600519', '-3')]:
            payload = _unpack_mcp_response(sample_response(code, price))
            with self.subTest(code=code, price=price), self.assertRaises(QuoteUnavailable):
                _extract_a_price(payload, '600519.SH')

    async def test_valuation_is_decimal_and_currency_specific(self):
        quote = Quote('600519.SH', Decimal('1600.50'), 'CNY', 'test', 'UTC')
        value, pnl, pct = compute_valuation(100, Decimal('150000'), quote)
        self.assertEqual(value, Decimal('160050.00'))
        self.assertEqual(pnl, Decimal('10050.00'))
        self.assertEqual(pct, Decimal('6.700'))

    async def test_error_then_success_does_not_cache_invalid_quote(self):
        mcp = FakeMCP(sample_response(kind='error'))
        service = AktoolsQuoteService(MCPContext(mcp))
        result = await service.fetch_many(['600519.SH'])
        self.assertIsNone(result['600519.SH'].quote)
        mcp.response = sample_response()
        result2 = await service.fetch_many(['600519.SH'])
        self.assertEqual(result2['600519.SH'].quote.price, Decimal('1600.50'))
        self.assertEqual(len(mcp.calls), 3)  # primary + fallback failure, then success

    async def test_other_mcp_server_cannot_be_mistaken_for_aktools(self):
        service = AktoolsQuoteService(MCPContext(FakeMCP(), server='unrelated'))
        result = await service.fetch_many(['600519.SH'])
        self.assertIn('AKTools', result['600519.SH'].reason)

    async def test_max_eight_quotes_per_request(self):
        mcp = FakeMCP()
        service = AktoolsQuoteService(MCPContext(mcp))
        names = [f'{600000+i}.SH' for i in range(9)]
        out = await service.fetch_many(names)
        self.assertIn('8', out[names[-1]].reason)
        self.assertEqual(len(mcp.calls), 8)


class QuoteHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AstrBotHandlersOfflineTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        AstrBotHandlersOfflineTests.tearDownClass.__func__(cls)

    def setUp(self):
        AstrBotHandlersOfflineTests.setUp(self)
        self.mcp = FakeMCP()
        self.ctx = MCPContext(self.mcp)
        self.plugin.context = self.ctx
        self.plugin.quotes = AktoolsQuoteService(self.ctx)
        self.owner = Owner('qq-1', 'g1', 'A')

    def tearDown(self):
        AstrBotHandlersOfflineTests.tearDown(self)

    def call(self, gen):
        return asyncio.run(consume(gen))[0]

    def test_view_stock_price_and_unrealized_profit_without_mutation(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'set1'), '600519.SH', 100, '1500'))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g1', 'view1'), '#1'))
        self.assertIn('600519.SH', reply)
        self.assertIn('1600.50 CNY', reply)
        self.assertIn('160050.00', reply)
        self.assertIn('+10050.00', reply)
        self.assertIn('+6.70%', reply)
        self.assertEqual(self.repo.get_holdings(self.owner)[0].total_cost, Decimal('150000.00000000'))
        self.assertEqual(self.ctx.llm_calls, 0)

    def test_group_summary_uses_mcp_price_and_same_currency(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'set1'), '600519.SH', 100, '1500'))
        self.call(self.plugin.set_holding(FakeEvent('B', 'g1', 'set2'), '600519.SH', 50, '1700'))
        reply = self.call(self.plugin.summary(FakeEvent('C', 'g1', 'sum1')))
        self.assertIn('150 股', reply)
        self.assertIn('最新可用价 1600.50', reply)
        self.assertIn('浮盈亏 +5075.00', reply)  # market 240075, cost 235000
        self.assertEqual(self.ctx.llm_calls, 0)

    def test_view_unsupported_japan_but_preserves_cost(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'set1'), '9432.T', 100, '150'))
        reply = self.call(self.plugin.view(FakeEvent('A', 'g1', 'view1')))
        self.assertIn('成本 150.00 JPY', reply)
        self.assertIn('行情暂不可用', reply)
        self.assertEqual(self.mcp.calls, [])

    def test_unavailable_quote_never_alters_holdings(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'set1'), '600519.SH', 100, '1500'))
        self.plugin.quotes = AktoolsQuoteService(None)
        reply = self.call(self.plugin.view(FakeEvent('A', 'g1', 'view1')))
        self.assertIn('行情暂不可用', reply)
        self.assertEqual(self.repo.get_holdings(self.owner)[0].quantity, 100)

    def test_cross_group_query_cannot_value_other_group(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'set1'), '600519.SH', 100, '1500'))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g2', 'view2'), 'A'))
        self.assertIn('尚未登记', reply)
        self.assertEqual(self.mcp.calls, [])


if __name__ == '__main__':
    unittest.main()
