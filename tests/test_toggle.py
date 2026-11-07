"""The latest successful input is ONE reversible switch per owner/group."""
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import (
    DuplicateMessageError, SqliteRepository,
)


class LatestInputToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'portfolio.db'
        self.repo = SqliteRepository(self.path)
        self.repo.open()
        self.a = Owner('qq-1', 'group-A', 'alice')
        self.b = Owner('qq-1', 'group-A', 'bob')
        self.group_b = Owner('qq-1', 'group-B', 'alice')
        self.platform_b = Owner('qq-2', 'group-A', 'alice')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def write(self, owner, mid, action='BUY', symbol='9432.T', qty=100, price='150'):
        return self.repo.apply_trade(owner, mid, action, symbol, qty, price)

    def quantity(self, owner, symbol='9432.T'):
        return next((h.quantity for h in self.repo.get_holdings(owner)
                     if h.symbol == symbol), 0)

    def test_undo_redo_is_a_switch_not_stack(self):
        self.write(self.a, 'set', 'SET', qty=100)
        self.write(self.a, 'buy', 'BUY', qty=200, price='165.7')
        self.assertEqual(self.quantity(self.a), 300)
        r = self.repo.toggle_latest(self.a, 'undo1')
        self.assertFalse(r.now_active)
        self.assertEqual(self.quantity(self.a), 100)
        self.assertEqual(self.repo.get_holdings(self.a)[0].total_cost, Decimal('15000'))
        self.assertFalse(self.repo.get_history(self.a)[0].is_active)
        r = self.repo.toggle_latest(self.a, 'undo2')
        self.assertTrue(r.now_active)
        self.assertEqual(self.quantity(self.a), 300)
        self.assertEqual(self.repo.get_holdings(self.a)[0].total_cost, Decimal('48140'))
        self.assertTrue(self.repo.get_history(self.a)[0].is_active)
        self.repo.toggle_latest(self.a, 'undo3')
        self.assertEqual(self.quantity(self.a), 100)
        # The previous SET stays in effect, the second toggle did NOT undo it.
        self.assertEqual(len(self.repo.get_history(self.a)), 2)

    def test_only_last_input_across_all_stocks(self):
        self.write(self.a, 'a1', symbol='9432.T')
        self.write(self.a, 'a2', symbol='7936.T', qty=10, price='5000')
        self.repo.toggle_latest(self.a, 'toggle1')
        self.assertEqual(self.quantity(self.a, '9432.T'), 100)
        self.assertEqual(self.quantity(self.a, '7936.T'), 0)
        self.repo.toggle_latest(self.a, 'toggle2')
        self.assertEqual(self.quantity(self.a, '7936.T'), 10)

    def test_new_input_replaces_toggle_target_after_undo(self):
        self.write(self.a, 'a1', symbol='9432.T')
        self.repo.toggle_latest(self.a, 'toggle1')
        self.write(self.a, 'a2', symbol='7936.T', qty=10, price='5000')
        self.repo.toggle_latest(self.a, 'toggle2')
        self.assertEqual(self.quantity(self.a, '9432.T'), 0)
        self.assertEqual(self.quantity(self.a, '7936.T'), 0)
        self.repo.toggle_latest(self.a, 'toggle3')
        self.assertEqual(self.quantity(self.a, '9432.T'), 0)
        self.assertEqual(self.quantity(self.a, '7936.T'), 10)
        # A stale undone trade remains visibly undone in history.
        self.assertEqual([r.is_active for r in self.repo.get_history(self.a)], [True, False])

    def test_first_input_can_be_undone_and_redone(self):
        self.write(self.a, 'first', 'SET', qty=7, price='123')
        r = self.repo.toggle_latest(self.a, 't1')
        self.assertIsNone(r.holding)
        self.assertEqual(self.repo.count_active_members(self.a), 0)
        self.repo.toggle_latest(self.a, 't2')
        self.assertEqual(self.quantity(self.a), 7)
        self.assertEqual(self.repo.count_active_members(self.a), 1)
        self.assertEqual(self.repo.get_member_no(self.a), 1)

    def test_full_sale_undo_restores_cost_and_realized_history(self):
        self.write(self.a, 'm1', 'SET', qty=100, price='150')
        self.write(self.a, 'm2', 'SELL', qty=100, price='170')
        self.assertEqual(self.quantity(self.a), 0)
        self.assertEqual(self.repo.get_history(self.a)[0].realized_pnl, Decimal('2000'))
        self.repo.toggle_latest(self.a, 't1')
        self.assertEqual(self.quantity(self.a), 100)
        self.assertEqual(self.repo.get_holdings(self.a)[0].total_cost, Decimal('15000'))
        self.assertFalse(self.repo.get_history(self.a)[0].is_active)
        self.repo.toggle_latest(self.a, 't2')
        self.assertEqual(self.quantity(self.a), 0)

    def test_per_owner_per_group_per_platform(self):
        for who in (self.a, self.group_b, self.platform_b):
            self.write(who, 'same-msg-id', qty=100)
        self.write(self.b, 'other-msg-id', qty=100)
        self.repo.toggle_latest(self.b, 'toggle-b')
        self.assertEqual(self.quantity(self.a), 100)
        self.assertEqual(self.quantity(self.b), 0)
        self.assertEqual(self.quantity(self.group_b), 100)
        self.assertEqual(self.quantity(self.platform_b), 100)
        self.repo.toggle_latest(self.group_b, 'toggle-g')
        self.assertEqual(self.quantity(self.group_b), 0)
        self.assertEqual(self.quantity(self.a), 100)

    def test_duplicate_toggle_does_not_toggle_twice(self):
        self.write(self.a, 'm1')
        self.repo.toggle_latest(self.a, 'toggle-1')
        with self.assertRaises(DuplicateMessageError):
            self.repo.toggle_latest(self.a, 'toggle-1')
        with self.assertRaises(DuplicateMessageError):
            self.write(self.a, 'toggle-1')
        with self.assertRaises(DuplicateMessageError):
            self.repo.toggle_latest(self.a, 'm1')
        self.assertEqual(self.quantity(self.a), 0)
        # Another user in the same group cannot reuse the same event ID.
        with self.assertRaises(DuplicateMessageError):
            self.repo.toggle_latest(self.b, 'toggle-1')

    def test_failure_to_record_toggle_rolls_back_position_and_flag(self):
        self.write(self.a, 'm1')
        self.repo.conn.execute('''CREATE TRIGGER fail_toggle BEFORE INSERT ON toggle_events
            BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.toggle_latest(self.a, 'toggle-1')
        self.assertEqual(self.quantity(self.a), 100)
        self.assertTrue(self.repo.get_history(self.a)[0].is_active)
        self.assertEqual(self.repo.conn.execute('SELECT active FROM latest_input').fetchone()[0], 1)
        self.repo.conn.execute('DROP TRIGGER fail_toggle')
        self.repo.toggle_latest(self.a, 'toggle-1')
        self.assertEqual(self.quantity(self.a), 0)

    def test_persistence_across_reopen(self):
        self.write(self.a, 'm1')
        self.repo.toggle_latest(self.a, 't1')
        self.repo.close()
        self.repo.open()
        self.assertEqual(self.quantity(self.a), 0)
        self.repo.toggle_latest(self.a, 't2')
        self.assertEqual(self.quantity(self.a), 100)

    def test_snapshot_protects_against_manual_db_edit(self):
        self.write(self.a, 'm1')
        self.repo.conn.execute("UPDATE holdings SET total_cost='9999'")
        with self.assertRaisesRegex(ValueError, '快照'):
            self.repo.toggle_latest(self.a, 't1')
        self.assertEqual(self.quantity(self.a), 100)
        self.assertEqual(self.repo.conn.execute('SELECT count(*) FROM toggle_events').fetchone()[0], 0)

    def test_no_previous_input(self):
        with self.assertRaisesRegex(ValueError, '没有可撤回'):
            self.repo.toggle_latest(self.a, 't1')

    def test_migration_v3_keeps_old_data_but_does_not_undo_legacy(self):
        self.write(self.a, 'm1', 'SET', qty=100)
        self.repo.close()
        old = sqlite3.connect(self.path)
        old.execute('DROP TABLE latest_input')
        old.execute('DROP TABLE toggle_events')
        old.execute('ALTER TABLE trades DROP COLUMN is_active')
        old.execute('''CREATE TABLE pending_actions (platform_id TEXT)''')
        old.execute('PRAGMA user_version = 3')
        old.close()
        self.repo.open()
        self.assertEqual(self.repo.conn.execute('PRAGMA user_version').fetchone()[0], 4)
        self.assertIsNone(self.repo.conn.execute("SELECT name FROM sqlite_master WHERE name='pending_actions'").fetchone())
        self.assertEqual(self.quantity(self.a), 100)
        self.assertEqual(len(self.repo.get_history(self.a)), 1)
        with self.assertRaisesRegex(ValueError, '旧版'):
            self.repo.toggle_latest(self.a, 't1')
        self.write(self.a, 'm2', 'BUY', qty=10, price='160')
        self.repo.toggle_latest(self.a, 't2')
        self.assertEqual(self.quantity(self.a), 100)


if __name__ == '__main__':
    unittest.main()
