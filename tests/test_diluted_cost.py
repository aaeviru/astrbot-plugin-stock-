"""Current-cycle realized P&L and adjusted cost; no database schema changes."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import SqliteRepository
from astrbot_plugin_portfolio.services.portfolio import diluted_average_cost


class CyclePnLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SqliteRepository(Path(self.tmp.name) / 'test.db')
        self.repo.open()
        self.alice = Owner('qq', 'g1', 'alice')
        self.bob = Owner('qq', 'g1', 'bob')
        self.other_group = Owner('qq', 'g2', 'alice')
        self.other_platform = Owner('qq-official', 'g1', 'alice')
        self.seq = 0

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def trade(self, owner=None, action='BUY', qty=100, price='10',
              symbol='600519.SH'):
        self.seq += 1
        return self.repo.apply_trade(
            owner or self.alice, f'order-{self.seq}', action,
            symbol, qty, price,
        )

    def pnl(self, owner=None, symbol='600519.SH'):
        return self.repo.get_period_realized_for_holdings(owner or self.alice).get(
            symbol, Decimal('0')
        )

    def test_moving_average_diluted_cost_and_combined_pnl(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.trade(action='BUY', qty=20, price='9')
        holding = self.repo.get_holdings(self.alice)[0]
        self.assertEqual(holding.quantity, 100)
        self.assertEqual(holding.total_cost, Decimal('980.00000000'))
        self.assertEqual(self.pnl(), Decimal('40.00000000'))
        self.assertEqual(diluted_average_cost(holding.quantity, holding.total_cost, self.pnl()), Decimal('9.40000000'))
        mark_to_market = Decimal('11') * holding.quantity - holding.total_cost
        self.assertEqual(mark_to_market, Decimal('120.00000000'))
        self.assertEqual(mark_to_market + self.pnl(), Decimal('160.00000000'))

    def test_loss_increases_diluted_cost(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='8')
        self.trade(action='BUY', qty=20, price='9')
        h = self.repo.get_holdings(self.alice)[0]
        self.assertEqual(self.pnl(), Decimal('-40.00000000'))
        self.assertEqual(diluted_average_cost(h.quantity, h.total_cost, self.pnl()), Decimal('10.20000000'))

    def test_large_profit_allows_negative_diluted_cost(self):
        self.trade(action='SET', qty=10, price='10')
        self.trade(action='SELL', qty=9, price='100')
        h = self.repo.get_holdings(self.alice)[0]
        self.assertEqual(self.pnl(), Decimal('810.00000000'))
        self.assertEqual(diluted_average_cost(h.quantity, h.total_cost, self.pnl()), Decimal('-800.00000000'))

    def test_history_not_limited_to_ten_trades(self):
        self.trade(action='SET', qty=100, price='10')
        for _ in range(15):
            self.trade(action='SELL', qty=1, price='12')
        self.assertEqual(len(self.repo.get_history(self.alice)), 10)
        self.assertEqual(self.pnl(), Decimal('30.00000000'))

    def test_full_sale_and_new_buy_starts_fresh_cycle(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=50, price='12')
        self.trade(action='SELL', qty=50, price='15')
        self.assertFalse(self.repo.get_holdings(self.alice))
        self.trade(action='BUY', qty=100, price='12')
        self.assertEqual(self.pnl(), Decimal('0'))
        h = self.repo.get_holdings(self.alice)[0]
        self.assertEqual(diluted_average_cost(h.quantity, h.total_cost, self.pnl()), Decimal('12'))

    def test_set_nonzero_resets_cycle(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.assertEqual(self.pnl(), Decimal('40'))
        self.trade(action='SET', qty=80, price='11')
        self.assertEqual(self.pnl(), Decimal('0'))
        self.trade(action='SELL', qty=10, price='13')
        self.assertEqual(self.pnl(), Decimal('20'))

    def test_set_zero_resets_cycle(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.trade(action='SET', qty=0, price='0')
        self.trade(action='BUY', qty=100, price='9')
        self.assertEqual(self.pnl(), Decimal('0'))

    def test_undo_redo_of_partial_sell_updates_cycle(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.assertEqual(self.pnl(), Decimal('40'))
        self.repo.toggle_latest(self.alice, 'undo-a')
        self.assertEqual(self.pnl(), Decimal('0'))
        self.repo.toggle_latest(self.alice, 'redo-a')
        self.assertEqual(self.pnl(), Decimal('40'))
        self.assertFalse(any(not t.is_active for t in self.repo.get_history(self.alice)))

    def test_undo_and_redo_of_set_restores_prior_cycle(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.trade(action='SET', qty=80, price='11')
        self.assertEqual(self.pnl(), Decimal('0'))
        self.repo.toggle_latest(self.alice, 'undo-set')
        self.assertEqual(self.pnl(), Decimal('40'))
        self.repo.toggle_latest(self.alice, 'redo-set')
        self.assertEqual(self.pnl(), Decimal('0'))

    def test_undo_full_close_restores_old_partial_sales(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.trade(action='SELL', qty=80, price='15')
        self.assertEqual(self.pnl(), Decimal('0'))
        self.repo.toggle_latest(self.alice, 'undo-close')
        self.assertEqual(self.repo.get_holdings(self.alice)[0].quantity, 80)
        self.assertEqual(self.pnl(), Decimal('40'))
        self.repo.toggle_latest(self.alice, 'redo-close')
        self.assertEqual(self.pnl(), Decimal('0'))
        self.assertEqual(self.repo.get_holdings(self.alice), [])

    def test_new_trade_after_undo_ignores_inactive_profit(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.repo.toggle_latest(self.alice, 'undo-sell')
        self.trade(action='BUY', qty=10, price='9')
        self.assertEqual(self.pnl(), Decimal('0'))

    def test_independent_symbols(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        self.trade(action='SET', qty=50, price='20', symbol='000001.SZ')
        self.trade(action='SELL', qty=10, price='18', symbol='000001.SZ')
        self.assertEqual(self.pnl(symbol='600519.SH'), Decimal('40'))
        self.assertEqual(self.pnl(symbol='000001.SZ'), Decimal('-20'))

    def test_group_summary_adds_only_active_holders_own_cycles(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')  # +40, alice: 80 at 10
        self.trade(owner=self.bob, action='SET', qty=50, price='20')
        self.trade(owner=self.bob, action='SELL', qty=10, price='18')  # -20, bob: 40 at 20
        g = self.repo.get_group_summary(self.alice)
        self.assertEqual(len(g), 1)
        h = g[0]
        self.assertEqual((h.holder_count, h.quantity), (2, 120))
        self.assertEqual(h.total_cost, Decimal('1600'))
        self.assertEqual(h.realized_pnl, Decimal('20'))
        self.assertEqual(diluted_average_cost(h.quantity, h.total_cost, h.realized_pnl), Decimal('1580') / 120)
        self.assertEqual(len(self.repo.get_history(self.alice)), 2)
        self.assertEqual(len(self.repo.get_history(self.bob)), 2)

    def test_isolation_across_group_platform_and_user(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        for other in (self.bob, self.other_group, self.other_platform):
            self.trade(owner=other, action='SET', qty=100, price='10')
            self.trade(owner=other, action='SELL', qty=20, price='20')
        self.assertEqual(self.pnl(self.alice), Decimal('40'))
        for other in (self.bob, self.other_group, self.other_platform):
            self.assertEqual(self.pnl(other), Decimal('200'))
        self.assertEqual(self.repo.get_group_summary(self.alice)[0].realized_pnl, Decimal('240'))
        self.assertEqual(self.repo.get_group_summary(self.other_group)[0].realized_pnl, Decimal('200'))
        self.assertEqual(self.repo.get_group_summary(self.other_platform)[0].realized_pnl, Decimal('200'))

    def test_database_version_unchanged_and_all_calculations_read_only(self):
        self.trade(action='SET', qty=100, price='10')
        self.trade(action='SELL', qty=20, price='12')
        before = self.repo.conn.total_changes
        self.assertEqual(self.repo.conn.execute('PRAGMA user_version').fetchone()[0], 4)
        self.assertEqual(self.pnl(), Decimal('40'))
        self.assertEqual(self.repo.get_group_summary(self.alice)[0].realized_pnl, Decimal('40'))
        self.assertEqual(self.repo.conn.total_changes, before)


class DilutedMathTests(unittest.TestCase):
    def test_no_zero_quantity_or_nonfinite_input(self):
        with self.assertRaises(ValueError):
            diluted_average_cost(0, Decimal('100'), Decimal('0'))
        with self.assertRaises(ValueError):
            diluted_average_cost(100, Decimal('NaN'), Decimal('0'))
        with self.assertRaises(ValueError):
            diluted_average_cost(100, Decimal('100'), Decimal('Infinity'))


if __name__ == '__main__':
    unittest.main()
