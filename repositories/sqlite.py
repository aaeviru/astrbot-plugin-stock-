import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from ..models import GroupMember, GroupPosition, Holding, Owner, ToggleResult, Trade, WriteResult
from ..services.portfolio import calculate_change, normalize_symbol, parse_price

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    total_cost TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform_id, group_id, user_id, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT NOT NULL PRIMARY KEY,
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('SET', 'BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    unit_price TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    quantity_after INTEGER NOT NULL CHECK (quantity_after >= 0),
    cost_after TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    UNIQUE (platform_id, group_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_owner_date
ON trades (platform_id, group_id, user_id, recorded_at DESC);
"""


# Version 2: stable per-group aliases for people who have entered positions.
MEMBER_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_members (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    member_no INTEGER NOT NULL CHECK(member_no > 0),
    display_name TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (platform_id, group_id, user_id),
    UNIQUE (platform_id, group_id, member_no)
)
"""


PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_parse_requests (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (platform_id, group_id, source_message_id)
)
"""

# Latest submitted input only: no undo stack. Previous records remain in trades.
LATEST_INPUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS latest_input (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    before_quantity INTEGER NOT NULL CHECK(before_quantity >= 0),
    before_cost TEXT NOT NULL,
    before_updated_at TEXT NOT NULL,
    after_quantity INTEGER NOT NULL CHECK(after_quantity >= 0),
    after_cost TEXT NOT NULL,
    after_updated_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    PRIMARY KEY (platform_id, group_id, user_id),
    FOREIGN KEY (trade_id) REFERENCES trades(id)
)
"""

# Undo itself is not a new trade. Keep only idempotency/audit events.
TOGGLE_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS toggle_events (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    active_after INTEGER NOT NULL CHECK(active_after IN (0, 1)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (platform_id, group_id, source_message_id),
    FOREIGN KEY (trade_id) REFERENCES trades(id)
)
"""


def clean_display_name(raw: str) -> str:
    """A nickname is display-only, never an identity or authorization key."""
    value = " ".join(str(raw or "").split()).replace("@", "＠")
    return "".join(ch for ch in value if ch.isprintable())[:32]


class DuplicateMessageError(Exception):
    """Same platform/group/message has already been recorded."""


class SqliteRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(
            self.db_path, timeout=5, isolation_level=None
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        self.conn = db
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise RuntimeError(f"不支持的数据库版本：{version}")
            # executescript may implicitly commit, so run it BEFORE our migration
            # transaction, never in the middle of a business transaction.
            db.executescript(SCHEMA)
            with self.transaction() as conn:
                conn.execute(MEMBER_SCHEMA)
                if version in (0, 1):
                    self._backfill_members(conn)
                # Keep v3 seen-message table for legacy deduplication, but do
                # not create new pending confirmation drafts.
                conn.execute(PENDING_SCHEMA)
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(trades)')}
                if 'is_active' not in columns:
                    conn.execute('''ALTER TABLE trades ADD COLUMN is_active INTEGER NOT NULL
                                    DEFAULT 1 CHECK(is_active IN (0, 1))''')
                conn.execute(LATEST_INPUT_SCHEMA)
                conn.execute(TOGGLE_EVENTS_SCHEMA)
                # The old v3 confirmation workflow is retired. Pending proposals
                # were never committed trades and must NOT become executable.
                conn.execute("DROP TABLE IF EXISTS pending_actions")
                conn.execute("PRAGMA user_version = 4")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("数据库尚未打开")
        return self.conn

    @contextmanager
    def transaction(self):
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    @staticmethod
    def _holding(row: sqlite3.Row) -> Holding:
        return Holding(
            symbol=row["symbol"],
            currency=row["currency"],
            quantity=row["quantity"],
            total_cost=Decimal(row["total_cost"]),
            updated_at=row["updated_at"],
        )

    def get_holdings(self, owner: Owner) -> list[Holding]:
        rows = self._db().execute(
            """SELECT symbol, currency, quantity, total_cost, updated_at
               FROM holdings
               WHERE platform_id=? AND group_id=? AND user_id=?
               ORDER BY symbol""",
            (owner.platform_id, owner.group_id, owner.user_id),
        ).fetchall()
        return [self._holding(row) for row in rows]

    def get_history(self, owner: Owner, limit: int = 10) -> list[Trade]:
        rows = self._db().execute(
            """SELECT id, action, symbol, currency, quantity, unit_price,
                      realized_pnl, quantity_after, cost_after, recorded_at, is_active
               FROM trades
               WHERE platform_id=? AND group_id=? AND user_id=?
               ORDER BY recorded_at DESC, id DESC LIMIT ?""",
            (owner.platform_id, owner.group_id, owner.user_id, limit),
        ).fetchall()
        return [
            Trade(
                id=r["id"], action=r["action"], symbol=r["symbol"],
                currency=r["currency"], quantity=r["quantity"],
                unit_price=Decimal(r["unit_price"]),
                realized_pnl=Decimal(r["realized_pnl"]),
                quantity_after=r["quantity_after"],
                cost_after=Decimal(r["cost_after"]),
                recorded_at=r["recorded_at"],
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    @staticmethod
    def _ensure_member(conn: sqlite3.Connection, actor: Owner,
                       display_name: str, now: str) -> int:
        """Allocate group-local stable alias inside the same write transaction."""
        scope = (actor.platform_id, actor.group_id, actor.user_id)
        row = conn.execute(
            """SELECT member_no, display_name FROM portfolio_members
               WHERE platform_id=? AND group_id=? AND user_id=?""", scope
        ).fetchone()
        name = clean_display_name(display_name)
        if row is not None:
            if name and name != row["display_name"]:
                conn.execute(
                    """UPDATE portfolio_members SET display_name=?
                       WHERE platform_id=? AND group_id=? AND user_id=?""",
                    (name, *scope),
                )
            return row["member_no"]

        next_no = conn.execute(
            """SELECT COALESCE(MAX(member_no), 0) + 1 FROM portfolio_members
               WHERE platform_id=? AND group_id=?""",
            (actor.platform_id, actor.group_id),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO portfolio_members
               (platform_id, group_id, user_id, member_no, display_name, joined_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (*scope, next_no, name, now),
        )
        return next_no

    def _backfill_members(self, conn: sqlite3.Connection) -> None:
        """V1 -> V2: preserve all holdings/trades; aliases start deterministically."""
        rows = conn.execute(
            """SELECT platform_id, group_id, user_id FROM holdings
               UNION SELECT platform_id, group_id, user_id FROM trades
               ORDER BY platform_id, group_id, user_id"""
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        for row in rows:
            actor = Owner(row["platform_id"], row["group_id"], row["user_id"])
            self._ensure_member(conn, actor, "", now)

    def get_member_no(self, actor: Owner) -> int | None:
        row = self._db().execute(
            """SELECT member_no FROM portfolio_members
               WHERE platform_id=? AND group_id=? AND user_id=?""",
            (actor.platform_id, actor.group_id, actor.user_id),
        ).fetchone()
        return row["member_no"] if row is not None else None

    def get_member_by_no(self, actor: Owner, member_no: int) -> str | None:
        if member_no < 1:
            raise ValueError("群友编号必须大于 0")
        row = self._db().execute(
            """SELECT user_id FROM portfolio_members
               WHERE platform_id=? AND group_id=? AND member_no=?""",
            (actor.platform_id, actor.group_id, member_no),
        ).fetchone()
        return row["user_id"] if row is not None else None

    def get_member_label(self, actor: Owner) -> str:
        row = self._db().execute(
            """SELECT member_no, display_name FROM portfolio_members
               WHERE platform_id=? AND group_id=? AND user_id=?""",
            (actor.platform_id, actor.group_id, actor.user_id),
        ).fetchone()
        if row is None:
            return f"用户 {actor.user_id}"
        return (f"#{row['member_no']} {row['display_name']}".strip()
                if row['display_name'] else f"#{row['member_no']} 用户 {actor.user_id}")

    def count_active_members(self, actor: Owner) -> int:
        row = self._db().execute(
            """SELECT COUNT(DISTINCT user_id) FROM holdings
               WHERE platform_id=? AND group_id=?""",
            (actor.platform_id, actor.group_id),
        ).fetchone()
        return int(row[0])

    def list_active_members(self, actor: Owner, limit: int = 10,
                            offset: int = 0) -> list[GroupMember]:
        if limit < 1 or limit > 50 or offset < 0:
            raise ValueError("分页参数无效")
        rows = self._db().execute(
            """SELECT m.user_id, m.member_no, m.display_name,
                      COUNT(h.symbol) AS holding_count
               FROM portfolio_members AS m
               JOIN holdings AS h
                 ON m.platform_id=h.platform_id
                AND m.group_id=h.group_id
                AND m.user_id=h.user_id
               WHERE m.platform_id=? AND m.group_id=?
               GROUP BY m.user_id, m.member_no, m.display_name
               ORDER BY m.member_no LIMIT ? OFFSET ?""",
            (actor.platform_id, actor.group_id, limit, offset),
        ).fetchall()
        return [GroupMember(r['user_id'], r['member_no'],
                            r['display_name'], r['holding_count']) for r in rows]

    def get_group_summary(self, actor: Owner) -> list[GroupPosition]:
        """Group-scoped aggregation in Python Decimal, NOT SUM(TEXT) in SQL."""
        rows = self._db().execute(
            """SELECT user_id, symbol, currency, quantity, total_cost
               FROM holdings WHERE platform_id=? AND group_id=?""",
            (actor.platform_id, actor.group_id),
        ).fetchall()
        groups: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row['symbol'], row['currency'])
            if key not in groups:
                groups[key] = {'users': set(), 'quantity': 0,
                               'total_cost': Decimal('0')}
            groups[key]['users'].add(row['user_id'])
            groups[key]['quantity'] += row['quantity']
            groups[key]['total_cost'] += Decimal(row['total_cost'])
        result = [
            GroupPosition(symbol, currency, len(info['users']),
                          info['quantity'], info['total_cost'])
            for (symbol, currency), info in groups.items()
        ]
        return sorted(result, key=lambda r: (-r.holder_count, r.symbol, r.currency))

    def message_was_processed(self, actor: Owner, message_id: str) -> bool:
        """Cheap idempotency preflight before an expensive LLM call.

        The write transaction rechecks this, so there is no preflight TOCTOU risk.
        """
        key = (actor.platform_id, actor.group_id, str(message_id or '').strip())
        if not all(key):
            raise ValueError("消息身份不完整")
        for table in ('trades', 'toggle_events', 'seen_parse_requests'):
            if self._db().execute(
                f"""SELECT 1 FROM {table} WHERE platform_id=? AND group_id=?
                    AND source_message_id=?""", key,
            ).fetchone():
                return True
        return False

    def apply_trade(
        self, actor: Owner, source_message_id: str, action: str,
        raw_symbol: str, quantity: int, raw_price: str, display_name: str = "",
    ) -> WriteResult:
        """Public deterministic path; write ledger + audit atomically."""
        with self.transaction() as conn:
            return self._apply_trade_tx(
                conn, actor, source_message_id, action, raw_symbol, quantity,
                raw_price, display_name,
            )

    def _apply_trade_tx(
        self, conn: sqlite3.Connection, actor: Owner, source_message_id: str,
        action: str, raw_symbol: str, quantity: int, raw_price: str,
        display_name: str = "",
    ) -> WriteResult:
        """Internal only: caller MUST own BEGIN IMMEDIATE transaction."""
        if not all((actor.platform_id, actor.group_id, actor.user_id)):
            raise ValueError("发送者身份不完整")
        message_id = str(source_message_id or "").strip()
        if not message_id:
            raise ValueError("缺少消息 ID，拒绝写入以避免重复记账")
        symbol, currency = normalize_symbol(raw_symbol)
        price = parse_price(raw_price)
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        scope = (actor.platform_id, actor.group_id, actor.user_id, symbol)

        duplicate = conn.execute(
            """SELECT 1 FROM trades
               WHERE platform_id=? AND group_id=? AND source_message_id=?""",
            (actor.platform_id, actor.group_id, message_id),
        ).fetchone()
        if duplicate:
            raise DuplicateMessageError("这条 QQ 消息已经记过账，不会重复执行")
        if conn.execute(
            """SELECT 1 FROM toggle_events WHERE platform_id=? AND group_id=?
               AND source_message_id=?""",
            (actor.platform_id, actor.group_id, message_id),
        ).fetchone():
            raise DuplicateMessageError("这条 QQ 消息已经处理过")
        if conn.execute(
            """SELECT 1 FROM seen_parse_requests WHERE platform_id=? AND group_id=?
               AND source_message_id=?""",
            (actor.platform_id, actor.group_id, message_id),
        ).fetchone():
            raise DuplicateMessageError("该自然语言消息已由旧版本处理")

        row = conn.execute(
            """SELECT symbol, currency, quantity, total_cost, updated_at
               FROM holdings WHERE platform_id=? AND group_id=?
               AND user_id=? AND symbol=?""",
            scope,
        ).fetchone()
        old_quantity = int(row["quantity"]) if row else 0
        old_cost = Decimal(row["total_cost"]) if row else Decimal("0")
        if row and row["currency"] != currency:
            raise ValueError("已有持仓币种不一致")

        change = calculate_change(
            action, old_quantity, old_cost, quantity, price
        )

        # Participant alias and the actual trade change commit together.
        self._ensure_member(conn, actor, display_name, now)

        if change.quantity_after:
            conn.execute(
                """INSERT INTO holdings (
                    platform_id, group_id, user_id, symbol, currency,
                    quantity, total_cost, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform_id, group_id, user_id, symbol)
                DO UPDATE SET
                    currency=excluded.currency,
                    quantity=excluded.quantity,
                    total_cost=excluded.total_cost,
                    updated_at=excluded.updated_at""",
                (*scope, currency, change.quantity_after,
                 str(change.cost_after), now),
            )
            holding = Holding(
                symbol, currency, change.quantity_after, change.cost_after, now
            )
        else:
            conn.execute(
                """DELETE FROM holdings WHERE platform_id=? AND group_id=?
                   AND user_id=? AND symbol=?""",
                scope,
            )
            holding = None

        trade_id = str(uuid4())
        conn.execute(
            """INSERT INTO trades (
                id, platform_id, group_id, user_id, symbol, currency,
                action, quantity, unit_price, realized_pnl, quantity_after,
                cost_after, source_message_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, *scope, currency, action, quantity,
             str(price), str(change.realized_pnl),
             change.quantity_after, str(change.cost_after),
             message_id, now),
        )
        # Only the latest successful SET/BUY/SELL for THIS owner can be toggled.
        # A new input replaces the previous toggle target, including if it was undone.
        conn.execute(
            """INSERT INTO latest_input (
                platform_id, group_id, user_id, trade_id, symbol, currency,
                before_quantity, before_cost, before_updated_at,
                after_quantity, after_cost, after_updated_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(platform_id, group_id, user_id) DO UPDATE SET
                trade_id=excluded.trade_id,
                symbol=excluded.symbol,
                currency=excluded.currency,
                before_quantity=excluded.before_quantity,
                before_cost=excluded.before_cost,
                before_updated_at=excluded.before_updated_at,
                after_quantity=excluded.after_quantity,
                after_cost=excluded.after_cost,
                after_updated_at=excluded.after_updated_at,
                active=1""",
            (actor.platform_id, actor.group_id, actor.user_id,
             trade_id, symbol, currency, old_quantity, str(old_cost),
             str(row['updated_at']) if row else '',
             change.quantity_after, str(change.cost_after),
             now if change.quantity_after else ''),
        )
        return WriteResult(symbol, currency, action, holding, change.realized_pnl)

    @staticmethod
    def _snapshot(conn: sqlite3.Connection, actor: Owner, symbol: str):
        return conn.execute(
            """SELECT quantity, total_cost, updated_at FROM holdings
               WHERE platform_id=? AND group_id=? AND user_id=? AND symbol=?""",
            (actor.platform_id, actor.group_id, actor.user_id, symbol),
        ).fetchone()

    def toggle_latest(self, actor: Owner, source_message_id: str) -> ToggleResult:
        """Toggle only the most recent submitted input for this owner/group.

        Undo/redo messages do not change which input is the target. Each distinct
        message toggles once; duplicate delivery does nothing. Everything commits
        under the SAME BEGIN IMMEDIATE transaction, including deduplication.
        """
        if not all((actor.platform_id, actor.group_id, actor.user_id)):
            raise ValueError("无法确认发送者身份")
        message_id = str(source_message_id or '').strip()
        if not message_id:
            raise ValueError("缺少消息 ID，拒绝撤回")
        scope = (actor.platform_id, actor.group_id, actor.user_id)
        msg_scope = (actor.platform_id, actor.group_id, message_id)
        with self.transaction() as conn:
            for table in ('toggle_events', 'trades', 'seen_parse_requests'):
                if conn.execute(
                    f"""SELECT 1 FROM {table} WHERE platform_id=? AND group_id=?
                        AND source_message_id=?""", msg_scope
                ).fetchone():
                    raise DuplicateMessageError("这条 QQ 消息已经处理过，不会重复切换")

            row = conn.execute(
                """SELECT * FROM latest_input WHERE platform_id=?
                   AND group_id=? AND user_id=?""", scope,
            ).fetchone()
            if row is None:
                raise ValueError("没有可撤回的最新输入；旧版历史不支持直接撤回")

            was_active = bool(row['active'])
            expected_prefix = 'after' if was_active else 'before'
            target_prefix = 'before' if was_active else 'after'
            current = self._snapshot(conn, actor, row['symbol'])
            current_qty = int(current['quantity']) if current else 0
            current_cost = Decimal(current['total_cost']) if current else Decimal('0')
            current_updated = str(current['updated_at']) if current else ''
            if (current_qty != int(row[f'{expected_prefix}_quantity'])
                    or current_cost != Decimal(row[f'{expected_prefix}_cost'])
                    or current_updated != row[f'{expected_prefix}_updated_at']):
                raise ValueError("持仓与最新输入的快照不一致，拒绝撤回以保护数据")

            target_qty = int(row[f'{target_prefix}_quantity'])
            target_cost = Decimal(row[f'{target_prefix}_cost'])
            target_updated = row[f'{target_prefix}_updated_at']
            holding = None
            if target_qty == 0:
                conn.execute(
                    """DELETE FROM holdings WHERE platform_id=? AND group_id=?
                       AND user_id=? AND symbol=?""",
                    (*scope, row['symbol']),
                )
            else:
                conn.execute(
                    """INSERT INTO holdings (platform_id, group_id, user_id,
                        symbol, currency, quantity, total_cost, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform_id, group_id, user_id, symbol)
                       DO UPDATE SET currency=excluded.currency,
                           quantity=excluded.quantity, total_cost=excluded.total_cost,
                           updated_at=excluded.updated_at""",
                    (*scope, row['symbol'], row['currency'], target_qty,
                     str(target_cost), target_updated),
                )
                holding = Holding(
                    row['symbol'], row['currency'], target_qty, target_cost,
                    target_updated,
                )

            new_active = int(not was_active)
            conn.execute(
                """UPDATE latest_input SET active=? WHERE platform_id=?
                   AND group_id=? AND user_id=?""", (new_active, *scope),
            )
            updated = conn.execute(
                "UPDATE trades SET is_active=? WHERE id=? AND platform_id=?"
                " AND group_id=? AND user_id=?",
                (new_active, row['trade_id'], *scope),
            )
            if updated.rowcount != 1:
                raise ValueError("最新输入历史不存在，撤回已拒绝")
            conn.execute(
                """INSERT INTO toggle_events (platform_id, group_id, user_id,
                    source_message_id, trade_id, active_after, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*scope, message_id, row['trade_id'], new_active,
                 datetime.now(timezone.utc).isoformat(timespec='microseconds')),
            )
            return ToggleResult(
                symbol=row['symbol'], currency=row['currency'],
                now_active=bool(new_active), holding=holding,
            )
