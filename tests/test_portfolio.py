import unittest
from decimal import Decimal as D

from astrbot_plugin_portfolio.services.portfolio import (
    calculate_change, normalize_symbol, parse_price,
)


class PortfolioCalculationTests(unittest.TestCase):
    def test_ntt_three_steps(self):
        first = calculate_change("SET", 0, D("0"), 100, D("150"))
        self.assertEqual(first.quantity_after, 100)
        self.assertEqual(first.cost_after, D("15000"))

        second = calculate_change(
            "BUY", first.quantity_after, first.cost_after, 200, D("165.7")
        )
        self.assertEqual(second.quantity_after, 300)
        self.assertEqual(second.cost_after, D("48140"))

        third = calculate_change(
            "SELL", second.quantity_after, second.cost_after, 100, D("170")
        )
        self.assertEqual(third.quantity_after, 200)
        self.assertEqual(third.cost_after, D("32093.33333333"))
        self.assertEqual(third.realized_pnl, D("953.33333333"))

    def test_full_sell_removes_all_cost(self):
        result = calculate_change("SELL", 2, D("0.33333333"), 2, D("1"))
        self.assertEqual(result.quantity_after, 0)
        self.assertEqual(result.cost_after, D("0"))
        self.assertEqual(result.realized_pnl, D("1.66666667"))

    def test_set_is_not_a_sale(self):
        result = calculate_change("SET", 300, D("48140"), 100, D("150"))
        self.assertEqual(result.quantity_after, 100)
        self.assertEqual(result.realized_pnl, D("0"))

    def test_sell_more_than_owned_rejected(self):
        with self.assertRaisesRegex(ValueError, "超过"):
            calculate_change("SELL", 100, D("15000"), 200, D("170"))

    def test_zero_amount_buy_rejected(self):
        with self.assertRaises(ValueError):
            calculate_change("BUY", 100, D("15000"), 0, D("170"))

    def test_invalid_prices(self):
        for raw in ("nan", "-1", "inf", "1e5", "1.123456789", "abc"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_price(raw)

    def test_symbol_currency(self):
        self.assertEqual(normalize_symbol("9432.t"), ("9432.T", "JPY"))
        self.assertEqual(normalize_symbol("nvda"), ("NVDA", "USD"))
        self.assertEqual(normalize_symbol("600519.sh"), ("600519.SH", "CNY"))
        with self.assertRaises(ValueError):
            normalize_symbol("9432")
