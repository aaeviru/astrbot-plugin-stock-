import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import (
    DuplicateMessageError, SqliteRepository,
)


class GroupPublicReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'portfolio.db'
        self.repo = SqliteRepository(self.path)
        self.repo.open()
        self.a = Owner('qq-1', 'g1', 'A')
        self.b = Owner('qq-1', 'g1', 'B')
        self.c = Owner('qq-1', 'g1', 'C')
        self.a_in_other_group = Owner('qq-1', 'g2', 'A')
        self.a_on_other_platform = Owner('qq-2', 'g1', 'A')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def write(self, actor, msg, symbol, qty, price, action='SET', name=''):
        return self.repo.apply_trade(
            actor, msg, action, symbol, qty, price, display_name=name,
        )

    def test_alias_allocated_per_group_stable_after_close_and_reopen(self):
        self.write(self.a, 'm1', '9432.T', 100, '150', name='群友A')
        self.write(self.b, 'm2', '7936.T', 10, '5000', name='群友B')
        self.write(self.a_in_other_group, 'm1', 'NVDA', 1, '10', name='another')
        self.assertEqual(self.repo.get_member_no(self.a), 1)
        self.assertEqual(self.repo.get_member_no(self.b), 2)
        self.assertEqual(self.repo.get_member_no(self.a_in_other_group), 1)
        self.write(self.b, 'm3', '7936.T', 0, '0', name='群友B')
        self.write(self.c, 'm4', '9432.T', 5, '170', name='群友C')
        self.assertEqual(self.repo.get_member_no(self.c), 3)
        self.assertEqual(self.repo.count_active_members(self.a), 2)
        self.assertEqual(
            [m.member_no for m in self.repo.list_active_members(self.a)], [1, 3]
        )
        self.repo.close()
        self.repo.open()
        self.assertEqual(self.repo.get_member_no(self.b), 2)
        self.assertEqual(self.repo.get_member_by_no(self.a, 3), 'C')
        self.assertEqual(self.repo.get_member_by_no(self.a, 999), None)
        self.write(self.b, 'm5', '7936.T', 1, '5100', name='新名字')
        self.assertEqual(self.repo.get_member_no(self.b), 2)
        self.assertIn('新名字', self.repo.get_member_label(self.b))

    def test_group_summary_aggregates_exactly_and_isolates_scope(self):
        self.write(self.a, 'm1', '9432.T', 100, '150', name='Alice')
        self.write(self.a, 'm2', '9432.T', 200, '165.7', action='BUY', name='Alice')
        self.write(self.a, 'm3', '9432.T', 100, '170', action='SELL', name='Alice')
        self.write(self.b, 'm4', '9432.T', 100, '170', name='Bob')
        self.write(self.b, 'm5', 'NVDA', 2, '100', name='Bob')
        self.write(self.a_in_other_group, 'm1', '9432.T', 999, '100', name='Alice')
        self.write(self.a_on_other_platform, 'm1', '9432.T', 888, '100', name='Alice')
        rows = self.repo.get_group_summary(self.a)
        self.assertEqual([r.symbol for r in rows], ['9432.T', 'NVDA'])
        ntt = rows[0]
        self.assertEqual(ntt.holder_count, 2)
        self.assertEqual(ntt.quantity, 300)
        self.assertEqual(ntt.total_cost, D('49093.33333333'))
        self.assertEqual(ntt.currency, 'JPY')
        self.assertEqual(rows[1].currency, 'USD')
        self.assertEqual(self.repo.count_active_members(self.a), 2)
        self.assertEqual(self.repo.get_group_summary(self.a_in_other_group)[0].quantity, 999)
        self.assertEqual(self.repo.get_group_summary(self.a_on_other_platform)[0].quantity, 888)

    def test_reading_other_owner_does_not_allow_writing_their_positions(self):
        self.write(self.a, 'm1', '9432.T', 100, '150')
        # A public query may create a read-only target Owner for A.
        target = Owner(self.b.platform_id, self.b.group_id, self.a.user_id)
        self.assertEqual(self.repo.get_holdings(target)[0].quantity, 100)
        # The *write* path always receives the actor B from get_actor(event).
        self.write(self.b, 'm2', '9432.T', 5, '200')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 100)
        self.assertEqual(self.repo.get_holdings(self.b)[0].quantity, 5)

    def test_member_changes_roll_back_with_failed_trade(self):
        self.repo.conn.execute(
            """CREATE TRIGGER block_history BEFORE INSERT ON trades
               BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.write(self.a, 'm1', '9432.T', 100, '150', name='群友A')
        self.assertIsNone(self.repo.get_member_no(self.a))
        self.assertEqual(self.repo.count_active_members(self.a), 0)

    def test_duplicate_message_does_not_change_alias_or_nickname(self):
        self.write(self.a, 'm1', '9432.T', 100, '150', name='Alice')
        with self.assertRaises(DuplicateMessageError):
            self.write(self.a, 'm1', '9432.T', 100, '150', name='Other')
        self.assertIn('Alice', self.repo.get_member_label(self.a))
        self.assertEqual(self.repo.count_active_members(self.a), 1)

    def test_nickname_is_not_identity_and_is_sanitized(self):
        self.write(self.a, 'm1', '9432.T', 100, '150', name='@all\nAlice')
        member = self.repo.list_active_members(self.a)[0]
        self.assertEqual(member.display_name, '＠all Alice')
        self.assertEqual(member.user_id, 'A')

    def test_paging_only_in_current_group(self):
        for i in range(12):
            actor = Owner('qq-1', 'g1', f'U{i:02}')
            self.write(actor, f'msg-{i}', 'NVDA', 1, '50', name=f'User{i}')
        a = self.repo.list_active_members(self.a, limit=10, offset=0)
        b = self.repo.list_active_members(self.a, limit=10, offset=10)
        self.assertEqual(len(a), 10)
        self.assertEqual(len(b), 2)
        self.assertEqual(self.repo.count_active_members(self.a), 12)
        self.assertEqual(self.repo.count_active_members(self.a_in_other_group), 0)

    def test_v1_migrates_backfilling_and_preserving_holdings_and_history(self):
        self.write(self.a, 'm1', '9432.T', 100, '150', name='Alice')
        self.write(self.b, 'm2', '9432.T', 5, '160', name='Bob')
        self.write(self.b, 'm3', '9432.T', 0, '0', name='Bob')
        self.repo.close()
        conn = sqlite3.connect(self.path)
        conn.execute('DROP TABLE portfolio_members')
        conn.execute('PRAGMA user_version = 1')
        conn.close()
        self.repo.open()
        self.assertEqual(self.repo.conn.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(self.repo.get_member_no(self.a), 1)
        self.assertEqual(self.repo.get_member_no(self.b), 2)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 100)
        self.assertEqual(len(self.repo.get_history(self.b)), 2)
        self.assertEqual(self.repo.count_active_members(self.a), 1)

    def test_unknown_version_refused(self):
        self.repo.close()
        conn = sqlite3.connect(self.path)
        conn.execute('PRAGMA user_version = 99')
        conn.close()
        with self.assertRaisesRegex(RuntimeError, '不支持'):
            self.repo.open()


if __name__ == '__main__':
    unittest.main()
