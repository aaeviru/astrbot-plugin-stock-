"""QQ public read visibility, aggregate realized P&L and quote fallbacks."""
import asyncio
import unittest
from decimal import Decimal

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.services.quotes import AktoolsQuoteService
from astrbot_plugin_portfolio.tests.test_handlers import (
    AstrBotHandlersOfflineTests, FakeEvent, consume,
)
from astrbot_plugin_portfolio.tests.test_quotes import FakeMCP, MCPContext


class DilutedCostHandlerTests(unittest.TestCase):
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

    def tearDown(self):
        AstrBotHandlersOfflineTests.tearDown(self)

    def call(self, handler):
        return asyncio.run(consume(handler))[0]

    def test_group_member_sees_diluted_cost_but_not_other_history(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '600519.SH', 100, '1500'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '600519.SH', 20, '1600'))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g1', 'viewB'), '#1'))
        self.assertIn('600519.SH', reply)
        self.assertIn('成本 1500.00 CNY', reply)
        self.assertIn('摊薄成本 1475.00 CNY', reply)
        self.assertIn('周期已实现 +2000.00 CNY', reply)
        self.assertIn('浮盈亏 +8040.00', reply)
        self.assertIn('综合盈亏 +10040.00 CNY', reply)
        self.assertIn('没有交易或调整记录', self.call(self.plugin.history(FakeEvent('B', 'g1', 'historyB'))))
        self.assertIn('卖出', self.call(self.plugin.history(FakeEvent('A', 'g1', 'historyA'))))
        self.assertEqual(self.ctx.llm_calls, 0)

    def test_group_summary_aggregates_individual_periods(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '600519.SH', 100, '1500'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '600519.SH', 20, '1600'))
        self.call(self.plugin.set_holding(FakeEvent('B', 'g1', 'setB'), '600519.SH', 50, '1700'))
        self.call(self.plugin.sell(FakeEvent('B', 'g1', 'sellB'), '600519.SH', 10, '1600'))
        reply = self.call(self.plugin.summary(FakeEvent('C', 'g1', 'summaryC')))
        self.assertIn('2 人 / 120 股', reply)
        self.assertIn('摊薄成本 1558.33 CNY', reply)  # (188000 - 1000) / 120
        self.assertIn('周期已实现 +1000.00 CNY', reply)
        self.assertIn('浮盈亏 +4060.00', reply)
        self.assertIn('综合盈亏 +5060.00 CNY', reply)
        self.assertIn('已报价 A 股', reply)
        self.assertIn('综合盈亏 +5060.00 CNY', reply)
        self.assertEqual(self.ctx.llm_calls, 0)

    def test_no_quote_still_shows_dilution_but_not_comprehensive(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '9432.T', 100, '150'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '9432.T', 20, '160'))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g1', 'viewB'), '#1'))
        self.assertIn('摊薄成本 147.50 JPY', reply)
        self.assertIn('周期已实现 +200.00 JPY', reply)
        self.assertIn('综合盈亏暂不可计算', reply)
        self.assertNotIn('综合盈亏 +', reply)
        self.assertEqual(self.mcp.calls, [])

    def test_mcp_unavailable_preserves_ledger_and_pnl(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '600519.SH', 100, '1500'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '600519.SH', 20, '1600'))
        self.plugin.quotes = AktoolsQuoteService(None)
        before = self.repo.conn.total_changes
        reply = self.call(self.plugin.view(FakeEvent('A', 'g1', 'viewA')))
        self.assertIn('摊薄成本 1475.00 CNY', reply)
        self.assertIn('周期已实现 +2000.00 CNY', reply)
        self.assertIn('综合盈亏暂不可计算', reply)
        self.assertEqual(self.repo.conn.total_changes, before)

    def test_latest_sell_undo_redo_reflected_in_public_view(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '600519.SH', 100, '1500'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '600519.SH', 20, '1600'))
        self.assertIn('周期已实现 +2000.00', self.call(self.plugin.view(FakeEvent('B', 'g1', 'v1'), '#1')))
        self.call(self.plugin.toggle_latest(FakeEvent('A', 'g1', 'undoA')))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g1', 'v2'), '#1'))
        self.assertIn('周期已实现 +0.00', reply)
        self.assertIn('摊薄成本 1500.00', reply)
        self.call(self.plugin.toggle_latest(FakeEvent('A', 'g1', 'redoA')))
        self.assertIn('周期已实现 +2000.00', self.call(self.plugin.view(FakeEvent('B', 'g1', 'v3'), '#1')))

    def test_no_cross_group_pnl_disclosure(self):
        self.call(self.plugin.set_holding(FakeEvent('A', 'g1', 'setA'), '600519.SH', 100, '1500'))
        self.call(self.plugin.sell(FakeEvent('A', 'g1', 'sellA'), '600519.SH', 20, '1600'))
        reply = self.call(self.plugin.view(FakeEvent('B', 'g2', 'viewB'), 'A'))
        self.assertIn('尚未登记', reply)
        self.assertNotIn('周期已实现', reply)


if __name__ == '__main__':
    unittest.main()
