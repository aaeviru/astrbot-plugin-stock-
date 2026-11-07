import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import (
    DuplicateMessageError, SqliteRepository,
)


class SqliteRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "portfolio.db"
        self.repo = SqliteRepository(self.db_path)
        self.repo.open()
        self.a = Owner("qq-bot-1", "group-1", "user-A")
        self.b = Owner("qq-bot-1", "group-1", "user-B")
        self.other_group = Owner("qq-bot-1", "group-2", "user-A")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def write(self, actor, msg, action, qty, price, symbol="9432.T"):
        return self.repo.apply_trade(actor, msg, action, symbol, qty, price)

    def test_create_buy_sell_persist_after_reopen(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        self.write(self.a, "msg-2", "BUY", 200, "165.7")
        result = self.write(self.a, "msg-3", "SELL", 100, "170")
        self.assertEqual(result.realized_pnl, D("953.33333333"))
        self.assertEqual(result.holding.quantity, 200)
        self.assertEqual(result.holding.total_cost, D("32093.33333333"))
        self.assertEqual(len(self.repo.get_history(self.a)), 3)

        self.repo.close()
        self.repo.open()
        [holding] = self.repo.get_holdings(self.a)
        self.assertEqual(holding.quantity, 200)
        self.assertEqual(holding.total_cost, D("32093.33333333"))

    def test_message_deduplication_no_double_buy(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        self.write(self.a, "msg-2", "BUY", 200, "165.7")
        with self.assertRaises(DuplicateMessageError):
            self.write(self.a, "msg-2", "BUY", 200, "165.7")
        [holding] = self.repo.get_holdings(self.a)
        self.assertEqual(holding.quantity, 300)
        self.assertEqual(len(self.repo.get_history(self.a)), 2)

    def test_owner_and_group_isolation(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        self.assertEqual(len(self.repo.get_holdings(self.a)), 1)
        self.assertEqual(self.repo.get_holdings(self.b), [])
        self.assertEqual(self.repo.get_holdings(self.other_group), [])
        self.write(self.b, "msg-2", "SET", 10, "170")
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 100)
        self.assertEqual(self.repo.get_holdings(self.b)[0].quantity, 10)
        self.write(self.other_group, "msg-1", "SET", 5, "180")
        self.assertEqual(self.repo.get_holdings(self.other_group)[0].quantity, 5)

    def test_bad_sell_does_not_change_database(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        with self.assertRaises(ValueError):
            self.write(self.a, "msg-2", "SELL", 101, "170")
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 100)
        self.assertEqual(len(self.repo.get_history(self.a)), 1)

    def test_atomicity_if_history_insert_fails(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        self.repo.conn.execute(
            """CREATE TRIGGER block_history BEFORE INSERT ON trades
               BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.write(self.a, "msg-2", "BUY", 200, "165.7")
        [holding] = self.repo.get_holdings(self.a)
        self.assertEqual(holding.quantity, 100)
        self.assertEqual(holding.total_cost, D("15000"))
        self.assertEqual(len(self.repo.get_history(self.a)), 1)

    def test_clear_leaves_audit_trail(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        result = self.write(self.a, "msg-2", "SET", 0, "0")
        self.assertIsNone(result.holding)
        self.assertEqual(self.repo.get_holdings(self.a), [])
        self.assertEqual(len(self.repo.get_history(self.a)), 2)

    def test_full_sell_deletes_position_and_preserves_trade(self):
        self.write(self.a, "msg-1", "SET", 100, "150")
        result = self.write(self.a, "msg-2", "SELL", 100, "170")
        self.assertIsNone(result.holding)
        self.assertEqual(result.realized_pnl, D("2000.00000000"))
        self.assertEqual(self.repo.get_holdings(self.a), [])
        self.assertEqual(len(self.repo.get_history(self.a)), 2)

    def test_fail_closed_if_message_id_missing(self):
        with self.assertRaisesRegex(ValueError, "消息 ID"):
            self.write(self.a, "", "SET", 100, "150")
        self.assertEqual(self.repo.get_history(self.a), [])
