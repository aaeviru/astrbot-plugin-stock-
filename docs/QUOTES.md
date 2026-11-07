# AKShare MCP 行情与浮动盈亏

本插件从 AstrBot **已经连接的 AKTools MCP** 读取证券行情，使用现有的 `holdings.total_cost`、`quantity` 计算市值和**未实现盈亏**。行情只读，不更改账本；获取行情不调用 LLM、不消耗 LLM Token。

## 先决条件

1. 在 AstrBot WebUI 的 MCP 工具配置中连接 [AKTools 官方 MCP](https://github.com/akfamily/aktools)，并确认服务名字包含 `aktools` 或 `akshare`。
2. MCP 服务需要包含名为 `get_data` 的工具。如果你的 AKShare MCP 实现提供的是 `akshare_stock_a` 等不同的工具，此版本不会自动兼容，请切换到官方 AKTools 或后续扩展适配器。
3. 启动插件后在 QQ 群执行 `/持仓 查看`；同时 `/持仓 汇总` 也会显示本页持仓的报价和按 CNY 合计的浮动盈亏。

插件使用 `context.get_llm_tool_manager().mcp_client_dict` 找到已连接的 MCP 客户端，通过 `MCPClient.call_tool_with_reconnect()` 调用：

```json
{
  "tool": "get_data",
  "arguments": {
    "name": "stock_individual_info_em",
    "params": {"symbol": "600519"},
    "max_rows": 30,
    "offset": 0
  }
}
```

**当前行情范围：只支持沪深 A 股代码** `600519.SH`、`000001.SZ` 等。`9432.T`、`NVDA` 和 `.HK` 股票仍能正常记录、查看**成本**，但行情显示“暂未接入”，不会使用错误的跨市场报价。

## 计算规则

- 市值 = `quantity × latest_price`
- 浮盈亏 = `market_value - total_cost`
- 浮盈亏率 = `unrealized_pnl / total_cost × 100%`
- 多只 A 股按 CNY 汇总，不与日元、美元或港币相加。
- **已实现盈亏**仍由 `SELL` 记账时计算；这里展示的是**当前剩余持仓**的浮动盈亏，不是历史累计总收益。

例如：600519.SH 持有 100 股、平均成本 1500 CNY。如果行情接口返回 1600.50 CNY，则计算市值 160050.00 CNY、浮盈亏 +10050.00 CNY、浮盈亏率 +6.70%。此例价格为模拟数据。

## 报价时间、缓存与失败处理

- 每只证券在插件内缓存 90 秒，减少重复请求；官方 AKTools 服务自身也可能缓存行情。
- 单次回复最多取 8 个 A 股代码，最多同时请求 3 个；单笔 MCP 请求设置 12 秒超时。
- 回复列出**抓取时间（UTC）**、来源和延迟提醒；它不是交易所的逐笔交易时间。休市时接口通常仍返回最近可用价格，不能将其理解为“此刻可成交价”。
- 如果 MCP 尚未连接、超时、返回异常/缺失/不匹配的证券代码或无效数字，则不计算该只证券的盈亏，仍返回原有持仓与成本。
- API 返回的任何价格都仅用于展示，不参与 `SET/BUY/SELL` 的成交价选择，也不会修改数据库。

## 测试

在仓库父目录执行：

```bash
python -m unittest discover -s astrbot_plugin_portfolio/tests -v
```

离线测试会模拟 MCP 工具的返回值、报价缓存、出错降级、跨群隔离和盈亏计算。正式使用前需要在你的 NAS AstrBot 实例中测试该 MCP 服务是否确实提供 `get_data`，及其上游 A 股行情接口是否能连通。
