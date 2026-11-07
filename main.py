import re
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .identity import get_actor, resolve_read_target
from .repositories.sqlite import DuplicateMessageError, SqliteRepository


def money(value: Decimal) -> str:
    return format(value.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ), "f")


class PortfolioPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.repo: SqliteRepository | None = None

    async def initialize(self):
        data_dir = StarTools.get_data_dir("astrbot_plugin_portfolio")
        repo = SqliteRepository(data_dir / "portfolio.db")
        repo.open()
        self.repo = repo
        logger.info("Portfolio: SQLite 已准备就绪")

    async def terminate(self):
        if self.repo is not None:
            self.repo.close()
            self.repo = None
        logger.info("Portfolio: SQLite 已关闭")

    def _write(self, event: AstrMessageEvent,
               action: str, symbol: str, quantity: int, price: str) -> str:
        if self.repo is None:
            return "持仓数据库尚未初始化"
        try:
            actor = get_actor(event)
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
            result = self.repo.apply_trade(
                actor, message_id, action, symbol, quantity, price,
                display_name=event.get_sender_name(),
            )
        except DuplicateMessageError as exc:
            return str(exc)
        except ValueError as exc:
            return f"操作失败：{exc}"
        except (sqlite3.Error, ArithmeticError):
            logger.exception("Portfolio: 写入失败")
            return "数据库写入失败，未完成本次操作，请联系管理员检查日志"

        holding = result.holding
        if holding is None:
            result_text = f"{result.symbol} 已清仓，操作记录已保留"
            if result.action == "SELL":
                result_text += f"\n本次已实现盈亏：{money(result.realized_pnl)}"
            return result_text

        status = {"SET": "持仓已校准", "BUY": "买入已记录",
                  "SELL": "卖出已记录"}[result.action]
        detail = (
            f"{status}\n"
            f"{result.symbol} ({result.currency})\n"
            f"当前数量：{holding.quantity} 股\n"
            f"平均成本：{money(holding.average_cost)}\n"
            f"剩余总成本：{money(holding.total_cost)}"
        )
        if result.action == "SELL":
            detail += f"\n本次已实现盈亏：{money(result.realized_pnl)}"
        return detail

    @filter.command_group("持仓")
    def portfolio(self):
        """群聊持仓管理"""
        pass

    @portfolio.command("设置")
    async def set_holding(self, event: AstrMessageEvent,
                          symbol: str, quantity: int, price: str):
        yield event.plain_result(
            self._write(event, "SET", symbol, quantity, price)
        )

    @portfolio.command("买入")
    async def buy(self, event: AstrMessageEvent,
                  symbol: str, quantity: int, price: str):
        yield event.plain_result(
            self._write(event, "BUY", symbol, quantity, price)
        )

    @portfolio.command("卖出")
    async def sell(self, event: AstrMessageEvent,
                   symbol: str, quantity: int, price: str):
        yield event.plain_result(
            self._write(event, "SELL", symbol, quantity, price)
        )

    def _read_target(self, event: AstrMessageEvent,
                     target_id: str, actor):
        """Translate #N aliases only in the READ path, never on writes."""
        target_id = str(target_id or "").strip()
        if target_id.startswith("#"):
            if not re.fullmatch(r"#[1-9][0-9]{0,5}", target_id):
                raise ValueError("编号格式错误，请输入 /持仓 查看 #1")
            target_user_id = self.repo.get_member_by_no(actor, int(target_id[1:]))
            if target_user_id is None:
                raise ValueError("本群不存在这个已登记用户编号")
            return resolve_read_target(event, target_user_id, actor)
        return resolve_read_target(event, target_id, actor)

    @portfolio.command("名单")
    async def members(self, event: AstrMessageEvent, page: int = 1):
        if self.repo is None:
            yield event.plain_result("持仓数据库尚未初始化")
            return
        try:
            actor = get_actor(event)
            if not 1 <= page <= 1000:
                raise ValueError("页码必须在 1 到 1000 之间")
            total = self.repo.count_active_members(actor)
            if total == 0:
                yield event.plain_result("本群目前没有用户登记持仓")
                return
            pages = (total + 9) // 10
            if page > pages:
                raise ValueError(f"只有 {pages} 页")
            members = self.repo.list_active_members(actor, 10, (page - 1) * 10)
        except ValueError as exc:
            yield event.plain_result(f"名单查询失败：{exc}")
            return
        except sqlite3.Error:
            logger.exception("Portfolio: 名单查询失败")
            yield event.plain_result("数据库查询失败")
            return

        lines = [f"本群有持仓的登记用户：{total} 人（第 {page}/{pages} 页）"]
        for member in members:
            name = member.display_name or f"用户 {member.user_id}"
            lines.append(f"#{member.member_no} {name}：{member.holding_count} 只股票")
        lines.append("查询：/持仓 查看 #编号")
        yield event.plain_result("\n".join(lines))

    @portfolio.command("汇总")
    async def summary(self, event: AstrMessageEvent, page: int = 1):
        if self.repo is None:
            yield event.plain_result("持仓数据库尚未初始化")
            return
        try:
            actor = get_actor(event)
            if not 1 <= page <= 1000:
                raise ValueError("页码必须在 1 到 1000 之间")
            positions = self.repo.get_group_summary(actor)
            members = self.repo.count_active_members(actor)
            if not positions:
                yield event.plain_result("本群目前没有登记持仓")
                return
            pages = (len(positions) + 9) // 10
            if page > pages:
                raise ValueError(f"只有 {pages} 页")
            items = positions[(page - 1) * 10:page * 10]
        except ValueError as exc:
            yield event.plain_result(f"汇总查询失败：{exc}")
            return
        except sqlite3.Error:
            logger.exception("Portfolio: 汇总查询失败")
            yield event.plain_result("数据库查询失败")
            return

        lines = [
            f"本群持仓汇总：{members} 人、{len(positions)} 种股票（第 {page}/{pages} 页）"
        ]
        for position in items:
            lines.append(
                f"{position.symbol}：{position.holder_count} 人 / "
                f"{position.quantity} 股 / "
                f"群加权成本 {money(position.average_cost)} {position.currency}"
            )
        lines.append("只汇总相同标的和币种，不代表当前市值。")
        yield event.plain_result("\n".join(lines))

    @portfolio.command("查看")
    async def view(self, event: AstrMessageEvent, target_id: str = ""):
        if self.repo is None:
            yield event.plain_result("持仓数据库尚未初始化")
            return
        try:
            actor = get_actor(event)
            target = self._read_target(event, target_id, actor)
            holdings = self.repo.get_holdings(target)
            target_label = self.repo.get_member_label(target)
        except ValueError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return
        except sqlite3.Error:
            logger.exception("Portfolio: 查询失败")
            yield event.plain_result("数据库查询失败")
            return

        if not holdings:
            yield event.plain_result("该群友尚未登记任何持仓")
            return

        lines = [f"{target_label} 的公开持仓："]
        for h in holdings:
            lines.append(
                f"{h.symbol}：{h.quantity} 股 / "
                f"成本 {money(h.average_cost)} {h.currency}"
            )
        yield event.plain_result("\n".join(lines))

    @portfolio.command("历史")
    async def history(self, event: AstrMessageEvent):
        if self.repo is None:
            yield event.plain_result("持仓数据库尚未初始化")
            return
        try:
            actor = get_actor(event)
            trades = self.repo.get_history(actor)
        except ValueError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return
        except sqlite3.Error:
            logger.exception("Portfolio: 历史查询失败")
            yield event.plain_result("数据库查询失败")
            return

        if not trades:
            yield event.plain_result("你还没有交易或调整记录")
            return

        labels = {"SET": "设置", "BUY": "买入", "SELL": "卖出"}
        lines = ["你最近的操作（时间为 UTC）："]
        for trade in trades:
            lines.append(
                f"{trade.recorded_at[:16]} "
                f"{labels[trade.action]} {trade.symbol} "
                f"{trade.quantity} 股 @{trade.unit_price}"
            )
        yield event.plain_result("\n".join(lines))

    @portfolio.command("身份")
    async def whoami(self, event: AstrMessageEvent):
        try:
            actor = get_actor(event)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        member_no = self.repo.get_member_no(actor) if self.repo else None
        yield event.plain_result(
            f"平台：{actor.platform_id}\n"
            f"群：{actor.group_id}\n"
            f"用户：{actor.user_id}\n"
            f"本群登记编号：{'#' + str(member_no) if member_no else '尚未登记'}"
        )

    @portfolio.command("帮助")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "/持仓 设置 9432.T 100 150\n"
            "/持仓 买入 9432.T 200 165.7\n"
            "/持仓 卖出 9432.T 100 170\n"
            "/持仓 查看 [用户ID、#编号或@群友]\n"
            "/持仓 名单 [页码]\n"
            "/持仓 汇总 [页码]\n"
            "/持仓 历史\n"
            "/持仓 身份\n"
            "SET 用于校准；买卖用于记录真实交易；仅本人可更改。"
        )
