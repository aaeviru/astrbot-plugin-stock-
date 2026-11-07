import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.repositories.sqlite import (
    DuplicateMessageError, SqliteRepository,
)
from astrbot_plugin_portfolio.services.natural_language import ParsedAction


class PendingConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'portfolio.db'
        self.repo = SqliteRepository(self.path)
        self.repo.open()
        self.a = Owner('qq-1','group-A','alice')
        self.b = Owner('qq-1','group-A','bob')
        self.other_group = Owner('qq-1','group-B','alice')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    @staticmethod
    def parse(action='BUY',symbol='9432.T',qty=100,price='150'):
        return ParsedAction(action,symbol,'JPY',qty,Decimal(price))

    def test_preview_only_no_ledger_write(self):
        draft = self.repo.create_draft(self.a,'original-1',self.parse())
        self.assertEqual(draft.quantity_after,100)
        self.assertEqual(draft.cost_after,Decimal('15000'))
        self.assertEqual(len(draft.token),10)
        self.assertEqual(self.repo.get_holdings(self.a),[])
        self.assertEqual(self.repo.get_history(self.a),[])
        self.assertIsNone(self.repo.get_member_no(self.a))

    def test_confirm_writes_and_cannot_confirm_twice(self):
        draft = self.repo.create_draft(self.a,'original-1',self.parse())
        result = self.repo.confirm_draft(self.a,draft.token,'confirmation-1','Alice')
        self.assertEqual(result.holding.quantity,100)
        self.assertEqual(len(self.repo.get_history(self.a)),1)
        with self.assertRaises((ValueError,DuplicateMessageError)):
            self.repo.confirm_draft(self.a,draft.token,'confirmation-1')
        with self.assertRaises(ValueError):
            self.repo.confirm_draft(self.a,draft.token,'confirmation-2')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,100)

    def test_cross_user_and_cross_group_cannot_confirm_or_cancel(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        for actor in (self.b,self.other_group):
            with self.assertRaises(ValueError):
                self.repo.confirm_draft(actor,draft.token,'c1')
            self.assertFalse(self.repo.cancel_draft(actor,draft.token))
        self.assertEqual(self.repo.get_holdings(self.a),[])
        self.repo.confirm_draft(self.a,draft.token,'c1')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,100)

    def test_wrong_token_does_not_write(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        with self.assertRaisesRegex(ValueError, '不正确'):
            self.repo.confirm_draft(self.a,'BADTOKEN','c1')
        self.assertEqual(self.repo.get_holdings(self.a),[])
        self.assertTrue(self.repo.cancel_draft(self.a,draft.token))
        self.assertFalse(self.repo.cancel_draft(self.a,draft.token))

    def test_confirm_uses_latest_snapshot_check(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        self.repo.apply_trade(self.a,'manual-1','SET','9432.T',2,'100')
        with self.assertRaisesRegex(ValueError,'已经发生变化'):
            self.repo.confirm_draft(self.a,draft.token,'c1')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,2)
        self.assertEqual(len(self.repo.get_history(self.a)),1)

    def test_confirm_expired(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.repo.conn.execute(
            "UPDATE pending_actions SET expires_at=? WHERE token=?",(expired,draft.token)
        )
        with self.assertRaisesRegex(ValueError,'过期'):
            self.repo.confirm_draft(self.a,draft.token,'c1')
        self.assertEqual(self.repo.get_holdings(self.a),[])

    def test_new_proposal_replaces_old_and_old_cannot_confirm(self):
        old = self.repo.create_draft(self.a,'o1',self.parse())
        new = self.repo.create_draft(self.a,'o2',self.parse(qty=50,price='170'))
        with self.assertRaisesRegex(ValueError, '不正确'):
            self.repo.confirm_draft(self.a,old.token,'c1')
        self.repo.confirm_draft(self.a,new.token,'c2')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,50)

    def test_duplicate_original_message_id_rejected(self):
        self.repo.create_draft(self.a,'o1',self.parse())
        with self.assertRaises(DuplicateMessageError):
            self.repo.create_draft(self.a,'o1',self.parse())

    def test_duplicate_source_stays_blocked_after_confirm(self):
        draft = self.repo.create_draft(self.a, 'original-1', self.parse())
        self.repo.confirm_draft(self.a, draft.token, 'confirmed-1')
        with self.assertRaises(DuplicateMessageError):
            self.repo.create_draft(self.a, 'original-1', self.parse())
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity, 100)

    def test_invalid_sale_rejected_before_proposal(self):
        with self.assertRaises(ValueError):
            self.repo.create_draft(self.a,'o1',self.parse('SELL',qty=100))
        self.assertEqual(self.repo.conn.execute('SELECT count(*) FROM pending_actions').fetchone()[0],0)

    def test_pending_survives_restart(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        self.repo.close()
        self.repo.open()
        self.repo.confirm_draft(self.a,draft.token,'c1')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,100)

    def test_failed_history_insert_rolls_back_pending_and_holding(self):
        draft = self.repo.create_draft(self.a,'o1',self.parse())
        self.repo.conn.execute('''CREATE TRIGGER block_history BEFORE INSERT ON trades
             BEGIN SELECT RAISE(ABORT, 'fake failed transaction'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.confirm_draft(self.a,draft.token,'c1')
        self.assertEqual(self.repo.get_holdings(self.a),[])
        self.assertEqual(self.repo.conn.execute('SELECT count(*) FROM pending_actions').fetchone()[0],1)
        self.repo.conn.execute('DROP TRIGGER block_history')
        self.repo.confirm_draft(self.a,draft.token,'c2')
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,100)

    def test_migrate_existing_v2(self):
        self.repo.apply_trade(self.a,'m1','SET','9432.T',100,'150')
        self.repo.close()
        db=sqlite3.connect(self.path)
        db.execute('DROP TABLE pending_actions')
        db.execute('PRAGMA user_version=2')
        db.close()
        self.repo.open()
        self.assertEqual(self.repo.conn.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertEqual(self.repo.get_holdings(self.a)[0].quantity,100)
        self.assertEqual(len(self.repo.get_history(self.a)),1)
        self.assertIsNotNone(self.repo.create_draft(self.a,'o2',self.parse('SELL',qty=30,price='170')))


if __name__ == '__main__':
    unittest.main()
