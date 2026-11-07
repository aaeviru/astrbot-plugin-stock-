# 第七讲：自然语言持仓输入，必须本人确认才入账

本讲延续第六讲的 `astrbot_plugin_portfolio`，将 SQLite 数据库版本升级为 v3，在 `main.py` 增加 `解析 / 确认 / 取消` 三条命令。新的功能默认**只在用户明确发送 `/持仓 解析` 时**调用大模型，而不是监听每条群聊消息。

## 1. 目标

```text
群友A: /持仓 解析 今天165.7日元买了200股NTT
机器人: 【待确认｜尚未记账】
        操作：实际买入
        标的：9432.T (JPY)
        数量：200股
        价格：165.7
        执行后数量：300股
        执行后平均成本：160.47
        请本人在5分钟内发送 /持仓 确认 45C85ABC12
群友A: /持仓 确认 45C85ABC12
机器人: 买入已记录……
```

上例只是演示；确认码随机生成，不要复制演示里的码。若群友B复制这个码，也**不能**替A确认，因为数据库同时校验发起者的平台、群和用户身份。

## 2. 为什么不让 LLM 直接写数据库？

LLM 只适合做语义抽取。自然语言里可能含有反问、计划、否定、跨语言表达、对话引用或提示注入。股票账本需要可重现的确定性状态变更。写入职责仍留给第五讲的 `SqliteRepository.apply_trade` 逻辑。图示：

```text
/持仓 解析 原话
   | 检查群和消息长度
   v
AstrBot 的 llm_generate() [只有此步骤消耗模型调用]
   | 返回严格 JSON: status/action/symbol/quantity/unit_price
   v
Python 做严格 JSON / 数据类型 / Decimal / 股票代码校验
   | 若不明确 -> 拒绝，不记账
   v
SQLite pending_actions (保存草稿、原持仓快照、确认码、过期时间)
   | 在 QQ 群公开显示拟写入结果（尚未入账）
   v
/持仓 确认 CODE（或取消 CODE）
   | 重新校验平台 + 群 + 本人 + CODE + 过期 + 旧持仓是否被改过
   v
BEGIN IMMEDIATE -> 更新 holdings + 追加 trades + 删除 pending -> COMMIT
```

普通 `/持仓 买入`、`卖出`、`设置`、`查看`、`名单`、`汇总` 仍是**零 LLM 调用**的确定性命令。

## 3. 使用 AstrBot 当前 LLM 接口

根据 AstrBot 官方 `docs/zh/dev/star/guides/ai.md`（v4.5.7 后的 API）：

```python
provider_id = await self.context.get_current_chat_provider_id(
    event.unified_msg_origin
)
response = await self.context.llm_generate(
    chat_provider_id=provider_id,
    system_prompt=SYSTEM_PROMPT,
    prompt='请解析这条用户消息，只输出JSON: ...',
)
text = response.completion_text
```

完整实现见 `main.py` 的 `parse_natural()`。这里仅调用单次聊天模型生成，**不使用 Agent / tool_loop_agent / 自动工具调用**。不把其他群成员的持仓内容传给模型；只发送该用户明确提交的文字描述。

- 显式命令 `/持仓 解析` 控制模型调用和 API 费用。
- 模型不配置、返回格式错误或网络报错：拒绝写入，提示使用标准指令。
- `SYSTEM_PROMPT` 指示模型不要猜测股票代码、缺失数量/单价，并拒绝计划、假设、否定和意图不明的句子。
- 系统提示不是安全边界；安全边界是 `parse_model_response()` 的结构化验证与必须手动确认。

## 4. 为什么 JSON 解析仍要严格验证？

解析器 `services/natural_language.py` 只接受：

```json
{
  "status": "valid",
  "action": "BUY",
  "symbol": "NTT",
  "quantity": 200,
  "unit_price": "165.7"
}
```

不能包含 `owner_id`、`user_id`、`sql` 等多余字段；`quantity` 必须是整数，`unit_price` 必须是十进制字符串。模型输出不完整、类型错误、多笔混杂、价格不合法，直接拒绝。`status=reject` 也拒绝写入。

股票名字只支持**显式白名单**：`NTT -> 9432.T`、`ASICS/亚瑟士 -> 7936.T`、`英伟达 -> NVDA`，并支持本来就是规范证券代码的输入。其他中文公司名需要改用股票代码；不会让模型擅自猜出证券标识。

这是入门版本的有限白名单，不是行情服务或证券代码验证。将来可添加独立证券主数据表，由用户确认标的映射。

## 5. 为什么使用 SQLite 待确认表？

数据库新增 `pending_actions`，字段包括平台 ID、群 ID、发送者 ID、动作/代码/数量/单价、随机 `token`、来源消息 ID、原持仓快照、创建时间和过期时间。

- 唯一主键是 `platform_id + group_id + user_id`，每名用户在每群同时最多有**一条**待确认操作；新的解析替换旧草稿。
- 草稿保存在 SQLite，而不是 Python 内存，热重载或容器重新启动后可以继续确认尚未到期的草稿。
- 确认有效期默认 **300 秒**。
- 即使第三者知道 token，也不能跨用户确认或取消。
- 草稿创建时计算一次预览，确认时重新检查 `quantity + total_cost + updated_at` 原持仓快照，防止期间普通命令改变了持仓。
- **删除草稿 + 更新持仓 + 追加交易**发生在同一个 SQLite 事务。数据库出错会同时回滚。
- 交易历史的 `source_message_id` 保存**确认消息 ID**，保证重发相同确认消息不会重复入账。

不应把原始语句、用户 QQ ID 或完整群历史上传到额外 LLM 日志。SQLite 草稿过期后仅在确认或后续新草稿时处理；当前版本不含后台定时清理任务，可以后增加简单的定期清理。

## 6. 指令与负例

| 用户操作 | 行为 |
|---|---|
| `/持仓 解析 今天165.7买了200股NTT` | 请求模型解析；展示待确认 BUY |
| `/持仓 解析 目前NTT共300股，成本160.47` | 可能识别为 SET；提醒会覆盖原记录，必须确认 |
| `/持仓 解析 我打算明天买200股NTT` | 计划，不是已成交；应拒绝 |
| `/持仓 解析 昨天买了NTT` | 缺股数和单价，拒绝 |
| `/持仓 确认 ABCDEF0123` | 只有创建草稿的人能在有效期内确认 |
| `/持仓 取消 ABCDEF0123` | 取消本人的草稿，绝不入账 |
| `/持仓 买入 9432.T 200 165.7` | 原有的直接指令，完全不调用 AI |

模型可能把不明确的自然语言解释成错误动作，因此读懂预览再确认是使用者必须完成的一步。`SET` 永远需要尤其认真核对，因为它会覆盖旧持仓数量与成本。

## 7. 运行测试

仓库克隆后，为兼容相对导入和 `unittest`，建议在父目录使用如下目录名：

```sh
git clone https://github.com/aaeviru/astrbot-plugin-stock-.git astrbot_plugin_portfolio
python -m unittest discover -s astrbot_plugin_portfolio/tests -v
```

覆盖：原账本功能、数据库从 v1/v2 升级到 v3、LLM JSON 验证、模型拒绝时零写入、本人确认、跨用户/跨群权限、过期、草稿替换、原持仓变化后的拒绝、数据库写入失败回滚、重复确认、重载后继续确认、模拟 AstrBot handler。

模拟 handler 使用本地桩，不等于实际 QQ 平台或真实 LLM 调用。必须先在测试群验证模型返回 JSON、QQ 指令参数解析、确认码操作和 AstrBot 日志。

## 8. 何时可考虑自动识别所有群消息？

第一版**不建议**这样做：所有群消息都可能触发模型调用、增加费用，还可能错误读取聊天中的假设买卖。以后若群友确有需求，可提供**群级开关 + 明确提及机器人 + 单人每日限额 + 仍需确认**的模式，但不应默认开启。

## 9. 与 PostgreSQL 的关系

新 `pending_actions` 同样位于 Repository 层。迁移时将其转换成 PostgreSQL 表即可，LLM 解析代码不关心数据库类型；原子确认要用 PostgreSQL 的事务和并发锁重新实现并重测。

**参考**：[AstrBot AI 插件官方指南](https://docs.astrbot.app/dev/star/guides/ai.html)；[AstrBot 消息指令指南](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)。
