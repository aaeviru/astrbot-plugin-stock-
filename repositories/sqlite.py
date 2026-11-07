import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from secrets import token_hex

from ..models import DraftPreview, GroupMember, GroupPosition, Holding, Owner, Trade, WriteResult
from ..services.portfolio import calculate_change, normalize_symbol, parse_price
from ..services.natural_language import ParsedAction

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

PENDING_ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    platform_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK(action IN ('SET', 'BUY', 'SELL')),
    symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price TEXT NOT NULL,
    original_message_id TEXT NOT NULL,
    expected_quantity INTEGER NOT NULL,
    expected_cost TEXT NOT NULL,
    expected_updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (platform_id, group_id, user_id),
    UNIQUE (platform_id, group_id, original_message_id)
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
            if version not in (0, 1, 2, 3):
                raise RuntimeError(f"不支持的数据库版本：{version}")
            # executescript may implicitly commit, so run it BEFORE our migration
            # transaction, never in the middle of a business transaction.
            db.executescript(SCHEMA)
            with self.transaction() as conn:
                conn.execute(MEMBER_SCHEMA)
                if version in (0, 1):
                    self._backfill_members(conn)
                conn.execute(PENDING_SCHEMA)
                conn.execute(PENDING_ACTIONS_SCHEMA)
                conn.execute("PRAGMA user_version = 3")
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
                      realized_pnl, quantity_after, cost_after, recorded_at
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

        conn.execute(
            """INSERT INTO trades (
                id, platform_id, group_id, user_id, symbol, currency,
                action, quantity, unit_price, realized_pnl, quantity_after,
                cost_after, source_message_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), *scope, currency, action, quantity,
             str(price), str(change.realized_pnl),
             change.quantity_after, str(change.cost_after),
             message_id, now),
        )
        return WriteResult(symbol, currency, action, holding, change.realized_pnl)

    @staticmethod
    def _snapshot(conn: sqlite3.Connection, actor: Owner, symbol: str):
        return conn.execute(
            """SELECT quantity, total_cost, updated_at FROM holdings
               WHERE platform_id=? AND group_id=? AND user_id=? AND symbol=?""",
            (actor.platform_id, actor.group_id, actor.user_id, symbol),
        ).fetchone()

    def create_draft(
        self, actor: Owner, source_message_id: str, parsed: ParsedAction,
        ttl_seconds: int = 300,
    ) -> DraftPreview:
        """Create ONE pending proposal per user, within current group, with snapshot."""
        if not all((actor.platform_id, actor.group_id, actor.user_id)):
            raise ValueError("无法识别发送者身份")
        message_id = str(source_message_id or "").strip()
        if not message_id:
            raise ValueError("缺少消息 ID，不能创建待确认记录")
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("确认有效期设置无效")
        now = datetime.now(timezone.utc)
        created_at = now.isoformat(timespec="microseconds")
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="microseconds")
        scope = (actor.platform_id, actor.group_id, actor.user_id)
        with self.transaction() as conn:
            old = conn.execute(
                """SELECT original_message_id FROM pending_actions WHERE
                   platform_id=? AND group_id=? AND user_id=?""",
                scope,
            ).fetchone()
            if old is not None and old["original_message_id"] == message_id:
                raise DuplicateMessageError("这条自然语言消息已经生成过确认请求")
            row = self._snapshot(conn, actor, parsed.symbol)
            old_quantity = int(row['quantity']) if row else 0
            old_cost = Decimal(row['total_cost']) if row else Decimal('0')
            old_updated = str(row['updated_at']) if row else ''
            change = calculate_change(
                parsed.action, old_quantity, old_cost, parsed.quantity, parsed.unit_price,
            )
            # Persist seen source IDs even AFTER a successful confirmation.
            # A redelivery of the original /解析 message must not create a new draft.
            try:
                conn.execute(
                    """INSERT INTO seen_parse_requests
                       (platform_id, group_id, source_message_id, user_id)
                       VALUES (?, ?, ?, ?)""",
                    (actor.platform_id, actor.group_id, message_id, actor.user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateMessageError("这条自然语言消息此前已经生成过草稿") from exc
            conn.execute(
                """DELETE FROM pending_actions WHERE
                   platform_id=? AND group_id=? AND user_id=?""",
                scope,
            )
            token = token_hex(5).upper()
            conn.execute(
                """INSERT INTO pending_actions
                   (platform_id, group_id, user_id, token, action, symbol,
                    currency, quantity, unit_price, original_message_id,
                    expected_quantity, expected_cost, expected_updated_at,
                    created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*scope, token, parsed.action, parsed.symbol, parsed.currency,
                 parsed.quantity, str(parsed.unit_price), message_id,
                 old_quantity, str(old_cost), old_updated, created_at, expires_at),
            )
        return DraftPreview(
            token, parsed.action, parsed.symbol, parsed.currency, parsed.quantity,
            parsed.unit_price, change.quantity_after, change.cost_after,
            change.realized_pnl, expires_at,
        )

    def confirm_draft(
        self, actor: Owner, token: str, confirm_message_id: str,
        display_name: str = "",
    ) -> WriteResult:
        """Only message sender can confirm. Recheck snapshot + time in same transaction."""
        scope = (actor.platform_id, actor.group_id, actor.user_id)
        if not all(scope):
            raise ValueError("无法识别发送者身份")
        if not str(confirm_message_id or '').strip():
            raise ValueError("确认消息缺少 ID")
        with self.transaction() as conn:
            # Check confirm event idempotency before touching pending proposal.
            used = conn.execute(
                """SELECT 1 FROM trades WHERE platform_id=? AND group_id=?
                   AND source_message_id=?""",
                (actor.platform_id, actor.group_id, str(confirm_message_id)),
            ).fetchone()
            if used:
                raise DuplicateMessageError("该确认消息已经记账")
            draft = conn.execute(
                """SELECT * FROM pending_actions WHERE
                   platform_id=? AND group_id=? AND user_id=?""",
                scope,
            ).fetchone()
            if draft is None:
                raise ValueError("没有待确认操作，请先使用 /持仓 解析")
            if str(token or '').strip().upper() != draft['token']:
                raise ValueError("确认码不正确，未修改持仓")
            if datetime.fromisoformat(draft['expires_at']) <= datetime.now(timezone.utc):
                conn.execute(
                    """DELETE FROM pending_actions WHERE
                       platform_id=? AND group_id=? AND user_id=?""", scope,
                )
                raise ValueError("确认码已过期，请重新解析")
            current = self._snapshot(conn, actor, draft['symbol'])
            actual_qty = int(current['quantity']) if current else 0
            actual_cost = str(current['total_cost']) if current else '0'
            actual_updated = str(current['updated_at']) if current else ''
            if (actual_qty != draft['expected_quantity'] or
                    Decimal(actual_cost) != Decimal(draft['expected_cost']) or
                    actual_updated != draft['expected_updated_at']):
                raise ValueError("持仓已经发生变化，旧草稿已失效，请重新解析")
            # Delete + write in ONE transaction, no awaited or external operations.
            conn.execute(
                """DELETE FROM pending_actions WHERE
                   platform_id=? AND group_id=? AND user_id=?""", scope,
            )
            return self._apply_trade_tx(
                conn, actor, confirm_message_id, draft['action'], draft['symbol'],
                draft['quantity'], draft['unit_price'], display_name,
            )

    def cancel_draft(self, actor: Owner, token: str) -> bool:
        scope = (actor.platform_id, actor.group_id, actor.user_id)
        if not all(scope):
            raise ValueError("无法识别发送者身份")
        with self.transaction() as conn:
            result = conn.execute(
                """DELETE FROM pending_actions WHERE platform_id=? AND group_id=?
                   AND user_id=? AND token=?""",
                (*scope, str(token or '').strip().upper()),
            )
            return result.rowcount == 1
