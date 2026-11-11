from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Owner:
    platform_id: str
    group_id: str
    user_id: str


@dataclass(frozen=True)
class Holding:
    symbol: str
    currency: str
    quantity: int
    total_cost: Decimal
    updated_at: str

    @property
    def average_cost(self) -> Decimal:
        return self.total_cost / self.quantity


@dataclass(frozen=True)
class Trade:
    id: str
    action: str
    symbol: str
    currency: str
    quantity: int
    unit_price: Decimal
    realized_pnl: Decimal
    quantity_after: int
    cost_after: Decimal
    recorded_at: str
    is_active: bool = True


@dataclass(frozen=True)
class WriteResult:
    symbol: str
    currency: str
    action: str
    holding: Holding | None
    realized_pnl: Decimal


@dataclass(frozen=True)
class GroupMember:
    user_id: str
    member_no: int
    display_name: str
    holding_count: int


@dataclass(frozen=True)
class GroupPosition:
    symbol: str
    currency: str
    holder_count: int
    quantity: int
    total_cost: Decimal
    realized_pnl: Decimal = Decimal("0")

    @property
    def average_cost(self) -> Decimal:
        return self.total_cost / self.quantity


@dataclass(frozen=True)
class ToggleResult:
    symbol: str
    currency: str
    now_active: bool
    holding: Holding | None
