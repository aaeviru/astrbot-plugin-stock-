"""Read-only AKTools MCP quote adapter; it never modifies the holdings ledger.

This deliberately supports *A shares only* in v0.4.0. Other markets do not
share AKShare's source latency, timestamp or coverage guarantees.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


SHARE_CODE = re.compile(r"(?P<code>[0-9]{6})\.(?:SH|SZ)\Z")
MAX_QUOTE_FETCHES = 8
CACHE_SECONDS = 90
FETCH_TIMEOUT_SECONDS = 12


class QuoteUnavailable(Exception):
    """An unavailable quote must never be represented as a price of zero."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    currency: str
    source: str
    fetched_at: str  # UTC retrieval time; NOT exchange market time


@dataclass(frozen=True)
class QuoteResult:
    quote: Quote | None = None
    reason: str = ""


def _to_mapping(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise QuoteUnavailable("行情数据格式不正确")
    if "rows" in payload:
        return payload
    # MCP frameworks sometimes wrap a dict return under 'result'.
    if isinstance(payload.get("result"), dict):
        return _to_mapping(payload["result"])
    raise QuoteUnavailable("行情数据缺少 rows 字段")


def _unpack_mcp_response(response: Any) -> dict:
    if getattr(response, "isError", False):
        raise QuoteUnavailable("AKShare 数据源返回错误")
    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        try:
            return _to_mapping(structured)
        except QuoteUnavailable:
            pass
    for part in (getattr(response, "content", None) or []):
        txt = getattr(part, "text", None)
        if not isinstance(txt, str) or len(txt) > 300_000:
            continue
        try:
            return _to_mapping(json.loads(txt))
        except (ValueError, TypeError, QuoteUnavailable):
            continue
    raise QuoteUnavailable("AKShare MCP 没有返回可解析的报价")


def _extract_a_price(payload: dict, symbol: str) -> Decimal:
    """Parse AKShare stock_individual_info_em's item/value rows."""
    if payload.get("name") != "stock_individual_info_em":
        raise QuoteUnavailable("收到意外的行情接口数据")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > 100:
        raise QuoteUnavailable("行情记录数量异常")
    fields: dict[str, Any] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("item"), str):
            fields[row["item"]] = row.get("value")
    expected_code = symbol[:6]
    if str(fields.get("股票代码", "")).strip() != expected_code:
        raise QuoteUnavailable("行情代码与持仓代码不一致")
    value = fields.get("最新")
    if value is None or type(value) is bool:
        raise QuoteUnavailable("行情没有有效的最新价")
    try:
        # Do not pass floats through Decimal(float): use decimal text instead.
        price = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise QuoteUnavailable("行情价格不是有效数字") from None
    if not price.is_finite() or price <= 0 or price > Decimal("1000000000"):
        raise QuoteUnavailable("行情价格异常")
    return price


class AktoolsQuoteService:
    """Reuse active AstrBot AKTools MCP client (no second AKShare install).

    Do not ask an LLM to select tools and do not expose arbitrary MCP names
    or arguments to the QQ message user.
    """

    def __init__(self, context: Any, ttl_seconds: int = CACHE_SECONDS):
        self.context = context
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Quote]] = {}

    def _get_aktools_client(self) -> Any:
        if self.context is None:
            raise QuoteUnavailable("AKShare MCP 未连接")
        get_manager = getattr(self.context, "get_llm_tool_manager", None)
        if not callable(get_manager):
            raise QuoteUnavailable("AstrBot 未开放 MCP 工具管理器")
        manager = get_manager()
        clients = getattr(manager, "mcp_client_dict", None)
        if clients is None:
            raise QuoteUnavailable("AstrBot 的 MCP 客户端列表不可用")
        for server, client in clients.items():
            if not any(x in server.lower() for x in ("aktools", "akshare")):
                continue
            if any(getattr(tool, "name", None) == "get_data"
                   for tool in (getattr(client, "tools", None) or [])):
                return client
        raise QuoteUnavailable("没有找到已连接的 AKTools MCP（get_data 工具）")

    async def get_quote(self, symbol: str) -> Quote:
        match = SHARE_CODE.fullmatch(symbol)
        if match is None:
            raise QuoteUnavailable("当前仅支持沪深 A 股实时行情")
        code = match.group("code")
        expected_exchange = "SH" if code.startswith("6") else (
            "SZ" if code.startswith(("0", "3")) else None
        )
        if expected_exchange is None or not symbol.endswith("." + expected_exchange):
            raise QuoteUnavailable("A 股代码与交易所后缀不符")
        cached = self._cache.get(symbol)
        if cached is not None and time.monotonic() - cached[0] < self.ttl_seconds:
            return cached[1]

        client = self._get_aktools_client()
        # AKShare reads this six-digit code, not the .SH / .SZ suffix.
        try:
            response = await asyncio.wait_for(
                client.call_tool_with_reconnect(
                    tool_name="get_data",
                    arguments={
                        "name": "stock_individual_info_em",
                        "params": {"symbol": match.group("code")},
                        "max_rows": 30,
                        "offset": 0,
                    },
                    read_timeout_seconds=timedelta(seconds=FETCH_TIMEOUT_SECONDS),
                ),
                timeout=FETCH_TIMEOUT_SECONDS + 2,
            )
        except Exception as exc:
            # No raw remote errors in QQ replies. They may contain tokens/URLs.
            raise QuoteUnavailable("AKShare 行情请求超时或失败") from exc
        data = _unpack_mcp_response(response)
        price = _extract_a_price(data, symbol)
        quote = Quote(
            symbol=symbol, price=price, currency="CNY",
            source="AKShare / 东方财富",
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        self._cache[symbol] = (time.monotonic(), quote)
        return quote

    async def fetch_many(self, symbols: list[str]) -> dict[str, QuoteResult]:
        distinct = list(dict.fromkeys(symbols))
        results: dict[str, QuoteResult] = {}
        eligible = []
        for symbol in distinct:
            if not SHARE_CODE.fullmatch(symbol):
                results[symbol] = QuoteResult(reason="该市场行情暂未接入")
            elif len(eligible) >= MAX_QUOTE_FETCHES:
                results[symbol] = QuoteResult(reason="超过每次最多 8 只的行情查询上限")
            else:
                eligible.append(symbol)

        semaphore = asyncio.Semaphore(3)

        async def fetch(symbol: str):
            async with semaphore:
                try:
                    return symbol, QuoteResult(quote=await self.get_quote(symbol))
                except QuoteUnavailable as exc:
                    return symbol, QuoteResult(reason=str(exc))
                except Exception:
                    # Errors in tool manager or malformed adapter objects must
                    # never turn a read-only /view into a failed command.
                    return symbol, QuoteResult(reason="行情服务暂时不可用")

        for symbol, value in await asyncio.gather(*(fetch(symbol) for symbol in eligible)):
            results[symbol] = value
        return results


def compute_valuation(quantity: int, total_cost: Decimal, quote: Quote):
    """Same-currency unrealized P&L; never mixes CNY, JPY, USD or HKD."""
    market_value = quote.price * quantity
    pnl = market_value - total_cost
    pct = (pnl / total_cost * 100) if total_cost > 0 else None
    return market_value, pnl, pct


def signed_money(value: Decimal) -> str:
    return ("+" if value >= 0 else "") + format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def signed_percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return ("+" if value >= 0 else "") + format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") + "%"
