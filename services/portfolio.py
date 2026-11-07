import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

COST_SCALE = Decimal("0.00000001")
MAX_QUANTITY = 1_000_000_000
MAX_UNIT_PRICE = Decimal("1000000000")
PRICE_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?\Z")
SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")


@dataclass(frozen=True)
class Change:
    quantity_after: int
    cost_after: Decimal
    realized_pnl: Decimal


def normalize_symbol(raw: str) -> tuple[str, str]:
    """Basic stock symbol policy. No network data or automatic guessing."""
    symbol = raw.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("股票代码格式错误")
    if symbol.endswith(".T"):
        return symbol, "JPY"
    if symbol.endswith((".SH", ".SZ")):
        return symbol, "CNY"
    if symbol.endswith(".HK"):
        return symbol, "HKD"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?", symbol):
        return symbol, "USD"
    raise ValueError("代码不明确：日股请用 9432.T，A 股用 600519.SH 等")


def parse_price(raw: str) -> Decimal:
    """Accept up to eight decimal places, never binary float inputs."""
    if not PRICE_PATTERN.fullmatch(raw.strip()):
        raise ValueError("价格须是非负十进制数，最多 8 位小数")
    price = Decimal(raw.strip())
    if not price.is_finite() or price > MAX_UNIT_PRICE:
        raise ValueError("价格超出允许范围")
    return price


def calculate_change(
    action: str,
    old_quantity: int,
    old_cost: Decimal,
    quantity: int,
    price: Decimal,
) -> Change:
    """Moving-average cost; SELL is a real trade, SET is a calibration."""
    if action not in {"SET", "BUY", "SELL"}:
        raise ValueError("未知的操作类型")
    if type(quantity) is not int or not (0 <= quantity <= MAX_QUANTITY):
        raise ValueError("数量必须是有效的非负整数")
    if old_quantity < 0 or old_cost < 0 or not old_cost.is_finite():
        raise ValueError("已有持仓数据无效")
    if not price.is_finite() or price < 0 or price > MAX_UNIT_PRICE:
        raise ValueError("价格无效")

    if action == "SET":
        if (quantity == 0) != (price == 0):
            raise ValueError("清仓请使用 设置 代码 0 0；非零持仓价格必须大于 0")
        return Change(
            quantity,
            (price * quantity).quantize(COST_SCALE, rounding=ROUND_HALF_UP),
            Decimal("0"),
        )

    if quantity == 0 or price == 0:
        raise ValueError("买入/卖出的数量及价格必须大于零")

    if action == "BUY":
        if old_quantity + quantity > MAX_QUANTITY:
            raise ValueError("买入后数量超出上限")
        return Change(
            old_quantity + quantity,
            (old_cost + price * quantity).quantize(
                COST_SCALE, rounding=ROUND_HALF_UP
            ),
            Decimal("0"),
        )

    if old_quantity == 0:
        raise ValueError("尚未持有这只股票，不能卖出")
    if quantity > old_quantity:
        raise ValueError(f"卖出 {quantity} 股超过当前持仓 {old_quantity} 股")

    released_cost = (
        old_cost
        if quantity == old_quantity
        else (old_cost * quantity / old_quantity).quantize(
            COST_SCALE, rounding=ROUND_HALF_UP
        )
    )
    cost_after = old_cost - released_cost
    realized = (price * quantity - released_cost).quantize(
        COST_SCALE, rounding=ROUND_HALF_UP
    )
    return Change(old_quantity - quantity, cost_after, realized)
