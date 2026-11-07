# 第七讲：QQ 群自然语言记账与最新输入撤回

本讲在第六讲的公开持仓账本上增加 AI 解析与简单的“撤回/恢复”切换。

## 1. 先明确交互：提出 → 写入 → 返回 → 可撤回

```text
群友：/持仓 记 今天165.7买了200股NTT
机器人：买入已记录；NTT 200股 @165.7；当前持仓...；有误请 /持仓 撤回
群友：/持仓 撤回
机器人：已撤回最新输入；NTT 当前持仓恢复为操作前状态
群友：/持仓 撤回
机器人：已恢复最新输入；NTT 当前持仓恢复为操作后状态
```

用户没有必要输入“确认”。一条完整、明确、已成交的描述经模型解析和 Python 校验后**立即记账**。群友可以检查回执，不正确时撤回。对计划、疑问句、未成交、多个标的、缺失股数/单价，拒绝而非猜测。

其他命令继续保持精确语法：`/持仓 设置`、`买入`、`卖出`、`查看`、`名单`、`汇总`、`历史`。普通聊天不会触发模型；只有 `/持仓 记` 用一次 LLM 请求。

## 2. AstrBot 的 AI 接口

```py
provider_id = await self.context.get_current_chat_provider_id(
    event.unified_msg_origin
)
response = await self.context.llm_generate(
    chat_provider_id=provider_id,
    system_prompt=SYSTEM_PROMPT,
    prompt=user_text,
)
parsed = parse_model_response(response.completion_text)
```

使用 SDK 的无工具 `llm_generate()` 而不是让 Agent 直接执行数据库 Tool。模型只返回 JSON 格式的交易建议，`services/natural_language.py` 负责严格检验返回结构、别名和数值。`reject_uncertain_intent()` 会在付费调用模型**之前**保守拒绝部分计划/假设/疑问表达。即使通过，模型仍可能误解，因此机器人回执应清晰展示真实写入的数量和价格。

预期 JSON：

```json
{"status":"valid","action":"BUY","symbol":"NTT","quantity":200,"unit_price":"165.7"}
```

所有者不允许由模型提供；写入身份必须由当前 `event` 提供。`unit_price` 使用 Decimal 字符串，禁止未知 JSON 属性。模型无效或超额卖出会停止操作。消息回执后不需要发第二条确认命令。

## 3. 为什么「撤回」是单个状态开关，而不是撤销栈？

每位用户在每个群里只有一条 `latest_input`，主键为：

```text
(platform_id, group_id, user_id)
```

它存储最新一次**成功输入**的交易 ID、股票、原状态、新状态、是否生效。新输入自动覆盖这一条记录中的撤回目标，但不修改以前的交易历史。

例如：

| 顺序 | 输入或操作 | NTT | ASICS | 下次撤回目标 |
|---|---|---:|---:|---|
| 1 | 设置 NTT 100 | 100 | 0 | 设置 NTT |
| 2 | 买入 NTT 200 | 300 | 0 | 买入 NTT |
| 3 | 撤回 | 100 | 0 | 仍是买入 NTT |
| 4 | 再次撤回 | 300 | 0 | 仍是买入 NTT |
| 5 | 买入 ASICS 10 | 300 | 10 | 买入 ASICS |
| 6 | 撤回 | 300 | 0 | 仍是买入 ASICS |

撤回操作本身不是新的持仓输入；连续发送不同的 `/持仓 撤回` 消息，就是在最近这笔输入的操作前、操作后两个快照之间切换。新输入会替换当前切换目标，不会产生一整条撤销/重做栈。没有确认码、撤销编号或时间窗口。

## 4. 数据库 v4

- `trades.is_active`：表示某次输入目前是否生效；已撤回的历史记录仍可查询。
- `latest_input`：每位用户/每群一条，保存原持仓数量、成本、更新时间和执行后的同类快照。
- `toggle_events`：只记录撤回消息 ID、针对的交易 ID、切换后的状态；用来保证消息重投递不会连续反转两次。

由于旧的 `trades` 表没有完整的操作前快照，**只有升级到 v4 后新增的交易支持撤回/恢复**。原有持仓及历史安全保留。旧 v3 未入账确认草稿 (`pending_actions`) 在升级时被删除；新的简化流程不再使用确认码。

## 5. SQLite 原子切换

在一个 `BEGIN IMMEDIATE` 事务中：

1. 校验来自 QQ 事件的 `(platform_id, group_id, user_id)` 与新消息 ID。
2. 检查交易历史、撤回事件和旧解析消息是否已处理相同消息 ID。
3. 查询当前用户最新的 `latest_input`；如果没有新版本输入，拒绝撤回。
4. 校验当前持仓与预期快照一致（防止其他代码直接更改数据库导致覆盖数据）。
5. 将 `holdings` 恢复为操作前快照（撤销）或操作后快照（恢复）。
6. 同时更新 `trades.is_active`、`latest_input.active` 并追加 `toggle_events` 去重记录。
7. 统一 `COMMIT`，任一步出错则 `ROLLBACK`。

没有使用 `await`，也没有在事务内部调用模型。第一版依然假定单 AstrBot 进程 / 单 Python 线程使用此连接，不允许多个进程共享同一连接。

## 6. 执行测试与验收

```sh
python -m unittest discover -s astrbot_plugin_portfolio/tests -v
```

重点测试：

- `test_undo_redo_is_a_switch_not_stack`：连续撤销、恢复、再撤销。
- `test_new_input_replaces_toggle_target_after_undo`：旧的撤销不会被新输入意外恢复。
- `test_full_sale_undo_restores_cost_and_realized_history`：全额卖出后撤销能恢复原成本。
- `test_duplicate_toggle_does_not_toggle_twice`：QQ 重投递同一消息不能执行两次。
- `test_failure_to_record_toggle_rolls_back_position_and_flag`：模拟数据库写入失败后状态仍一致。
- `test_per_owner_per_group_per_platform`：同一群其他人无法撤回你的数据。
- `test_migration_v3_keeps_old_data_but_does_not_undo_legacy`：旧数据保留且迁移清晰。

**真实 QQ 平台与 LLM Provider 尚未在这里联调。** 在 NAS 测试群中核对数据后再正式启用。

## 7. 暂不扩展的功能

多级撤销、选择历史交易进行撤销、批准步骤、定时自动确认、自然语言自动监听全群消息、对接券商下单，都不在当前范围。若更早的输入确实有误，用 `/持仓 设置` 校准。
