"""Tencent daily-close backup and QQ output: offline tests, no network calls."""
import asyncio
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from astrbot_plugin_portfolio.services.quotes import (
    AktoolsQuoteService, QuoteUnavailable, _extract_tx_close,
)
from astrbot_plugin_portfolio.tests.test_quotes import sample_response
from astrbot_plugin_portfolio.tests.test_quotes import MCPContext
from astrbot_plugin_portfolio.tests.test_handlers import (
    AstrBotHandlersOfflineTests, FakeEvent, consume,
)


class SourceAwareMCP:
    tools = [types.SimpleNamespace(name="get_data")]

    def __init__(self, fallback_rows=None, fail_both=False):
        self.calls = []
        self.fail_both = fail_both
        self.fallback_rows = fallback_rows

    async def call_tool_with_reconnect(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["arguments"]["name"]
        if name == "stock_individual_info_em" or self.fail_both:
            return types.SimpleNamespace(
                isError=True, structuredContent=None,
                content=[types.SimpleNamespace(text="东方财富返回空响应")],
            )
        if name == "stock_zh_a_hist_tx":
            rows = self.fallback_rows
            if rows is None:
                # Avoid using a fixed date in tests.
                today = datetime.now(timezone(timedelta(hours=8))).date()
                rows = [{"date": (today - timedelta(days=1)).isoformat(),
                         "close": "17.07"}]
            return types.SimpleNamespace(
                isError=False,
                structuredContent={
                    "result": {
                        "name": name,
                        "rows": rows,
                        "returned_rows": len(rows),
                    }
                },
                content=[],
            )
        raise AssertionError("unexpected interface " + name)


class QuoteBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_uses_correct_symbol_and_date(self):
        mcp = SourceAwareMCP()
        service = AktoolsQuoteService(MCPContext(mcp))
        quote = await service.get_quote("000833.SZ")
        self.assertEqual(quote.price, Decimal("17.07"))
        self.assertEqual(quote.price_kind, "最近收盘价")
        self.assertIn("腾讯", quote.source)
        self.assertIsNotNone(quote.trade_date)
        self.assertEqual(len(mcp.calls), 2)
        args = mcp.calls[1]["arguments"]
        self.assertEqual(args["name"], "stock_zh_a_hist_tx")
        self.assertEqual(args["params"]["symbol"], "sz000833")
        self.assertEqual(args["offset"], -5)
        self.assertEqual(args["params"]["adjust"], "")
        await service.get_quote("000833.SZ")
        self.assertEqual(len(mcp.calls), 2, "fallback is cached")

    async def test_shenzhen_and_shanghai_are_not_mixed(self):
        mcp = SourceAwareMCP()
        await AktoolsQuoteService(MCPContext(mcp)).get_quote("600519.SH")
        self.assertEqual(mcp.calls[1]["arguments"]["params"]["symbol"], "sh600519")

    async def test_failed_both_sources_does_not_create_price(self):
        mcp = SourceAwareMCP(fail_both=True)
        results = await AktoolsQuoteService(MCPContext(mcp)).fetch_many(["000833.SZ"])
        self.assertIsNone(results["000833.SZ"].quote)
        self.assertIn("备用行情", results["000833.SZ"].reason)

    async def test_do_not_accept_old_or_future_daily_prices(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for day in [today - timedelta(days=60), today + timedelta(days=3)]:
            payload = {
                "name": "stock_zh_a_hist_tx",
                "rows": [{"date": day.isoformat(), "close": "17.07"}],
            }
            with self.subTest(day=day), self.assertRaises(QuoteUnavailable):
                _extract_tx_close(payload, "000833.SZ", today)

    async def test_select_latest_valid_row_not_position_or_zero(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        rows = [
            {"date": (today - timedelta(days=3)).isoformat(), "close": "12.34"},
            {"date": today.isoformat(), "close": "0"},
            {"date": (today - timedelta(days=1)).isoformat(), "close": "17.07"},
        ]
        payload = {"name": "stock_zh_a_hist_tx", "rows": rows}
        price, day = _extract_tx_close(payload, "000833.SZ", today)
        self.assertEqual(price, Decimal("17.07"))
        self.assertEqual(day, (today - timedelta(days=1)).isoformat())


class FallbackHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AstrBotHandlersOfflineTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        AstrBotHandlersOfflineTests.tearDownClass.__func__(cls)

    def setUp(self):
        AstrBotHandlersOfflineTests.setUp(self)
        self.mcp = SourceAwareMCP()
        self.ctx = MCPContext(self.mcp)
        self.plugin.context = self.ctx
        self.plugin.quotes = AktoolsQuoteService(self.ctx)

    def tearDown(self):
        AstrBotHandlersOfflineTests.tearDown(self)

    def call(self, generator):
        return asyncio.run(consume(generator))[0]

    def test_qq_reply_marks_tencent_as_daily_close(self):
        self.call(self.plugin.set_holding(
            FakeEvent("A", "g1", "set-qq"), "000833.SZ", 3000, "16.62"
        ))
        reply = self.call(self.plugin.view(FakeEvent("B", "g1", "view-qq"), "#1"))
        self.assertIn("📊", reply)
        self.assertIn("000833.SZ", reply)
        self.assertIn("收盘价 17.07", reply)
        self.assertIn("综合盈亏", reply)
        self.assertNotIn("东方财富返回空响应", reply)
        self.assertEqual(self.ctx.llm_calls, 0)


    def test_invalid_exchange_suffix_is_not_hidden(self):
        self.call(self.plugin.set_holding(
            FakeEvent("A", "g1", "set-invalid"), "002158.SH", 100, "23.74"
        ))
        reply = self.call(self.plugin.view(FakeEvent("B", "g1", "view-invalid"), "#1"))
        self.assertIn("股票代码后缀有误", reply)
        self.assertEqual(len(self.mcp.calls), 0)
