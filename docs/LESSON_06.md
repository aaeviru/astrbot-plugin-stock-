# 第六讲：公开查询、群持仓汇总与访问控制

本讲基于第五讲 `astrbot_plugin_portfolio` v0.1.0，升级为 v0.2.0，保留原有持仓及历史。先看 `README.md` 的指令表，再按照以下顺序阅读代码。

## 1. 需求边界

- 发送 `/持仓 买入/卖出/设置` 的人才能变更自己的持仓；写入账户由 `event.get_sender_id()`、`get_platform_id()`、`get_group_id()` 决定。
- 当前群的任一用户都能查询本群其他人的登记持仓，但不能查其他群。
- 交易历史 `/持仓 历史` 保持仅本人可查；这一点与持仓公开是不同的产品规则。
- 不对接真实 QQ 群成员名单。`名单` 指**曾经登记且当前还有持仓的人**，清仓者不显示；已分配的编号不会回收。

## 2. 对比第五讲的代码变化

```text
models.py                + GroupMember、GroupPosition
identity.py              -> 使用 services/access.py 做纯权限判断
services/access.py       + 只读目标解析（QQ @ 消息只用于读）
repositories/sqlite.py   + portfolio_members、v1->v2迁移、群名单、群汇总
main.py                  + /持仓 名单、/持仓 汇总、/持仓 查看 #编号
README.md                + 安装、升级、边界说明
tests/                   + 访问控制、跨群隔离、迁移、Handler 模拟测试
```

## 3. 身份和权限：只读目标与当前操作人

```python
actor = get_actor(event)  # 从可信事件获取本人身份
# 写入只能传 actor：
repo.apply_trade(actor, event.message_obj.message_id, ...)

# 公开查询可以换 target.user_id，但不能换平台、群：
read_target = resolve_read_target(event, raw_target, actor)
repo.get_holdings(read_target)
```

请查阅 `services/access.py` 的 `read_owner()`：它允许指定目标用户，但是 `return Owner(actor.platform_id, actor.group_id, target)` 保证目标永远属于当前群。

`@群友` 只处理结构化的 `Comp.At` 消息段，不尝试把纯文本昵称当作用户身份。普通的 `@` 文本会要求使用 `/持仓 名单` 中的 `#编号`。

## 4. portfolio_members：稳定编号

`repositories/sqlite.py` 增加 `portfolio_members` 表（platform_id, group_id, user_id, member_no, display_name, joined_at），满足：

- 主键：一个群内用户只能登记一次；
- 唯一键：同一个群内编号不能重复；
- 编号生成：只在写事务中获取 `MAX(member_no)+1`，分配后持久保存；
- 昵称只是展示缓存：读写身份完全不依赖昵称；
- 清仓时只删当前持仓，不删除用户目录和交易历史。

编号不是访问令牌；任何群友都能知道编号，但不能因此写入其他用户账户。

## 5. /持仓 名单：按当前群筛选

```sql
SELECT m.user_id, m.member_no, m.display_name,
       COUNT(h.symbol) AS holding_count
FROM portfolio_members AS m
JOIN holdings AS h
  ON m.platform_id=h.platform_id
 AND m.group_id=h.group_id
 AND m.user_id=h.user_id
WHERE m.platform_id=? AND m.group_id=?
GROUP BY m.user_id, m.member_no, m.display_name
ORDER BY m.member_no
LIMIT ? OFFSET ?;
```

为什么使用 `JOIN holdings`？因为这样只展示**当前还有持仓**的登记用户，自动隐藏已清仓的人。查询分页每页最多 10 个用户。

## 6. /持仓 查看 #1：只影响读接口

在 `main.py` 的 `_read_target()` 中，`#1` 先查 `portfolio_members`，得到本群的真实 `user_id`。然后调用第三讲的 `resolve_read_target()`，生成固定当前群的 `Owner` 对象，再读取 `holdings`。

用 `#编号` 而不是让用户输入昵称，可以避免昵称重复、昵称变更、QQ 官方机器人没有公开数字 QQ 号等问题。

## 7. /持仓 汇总：按股票聚合

`repositories/sqlite.py` 的 `get_group_summary()` 先取出当前群每个人的持仓：

```sql
SELECT user_id, symbol, currency, quantity, total_cost
FROM holdings
WHERE platform_id=? AND group_id=?;
```

然后在 Python 中按 `(symbol,currency)` 聚合：

- `holder_count` = 不同用户 ID 的数量；
- `quantity` = 合计股数；
- `total_cost` = 每人剩余成本的 Decimal 精确合计；
- `average_cost` = 汇总成本 / 汇总股数（群加权平均成本）。

不能把 JPY、USD、CNY 的成本直接加成一个数字；也不能用 SQLite 浮点 `SUM(total_cost)` 计算 Decimal 字符串。没有外部报价，因此不展示市值或浮动盈亏。

## 8. 数据库 v1 -> v2 升级

`SqliteRepository.open()` 根据 `PRAGMA user_version` 决定是否升级：

1. 原 `SCHEMA` 保持兼容；
2. `BEGIN IMMEDIATE` 事务中创建 `portfolio_members`；
3. 从 `holdings UNION trades` 取历史用户，按每群稳定顺序分配编号；
4. 在同一个事务中将 `user_version` 提升为 2；
5. 发生异常则回滚，插件应拒绝加载，而不是无声丢失数据。

迁移前请备份 SQLite。升级后的 v2 数据库不要直接拿第五讲 v1 代码重载使用，旧版实现会拒绝不认识的版本。

## 9. 自动测试与实际 QQ 集成测试

在插件父目录执行：

```sh
python -m unittest discover -s astrbot_plugin_portfolio/tests -v
```

`tests/test_access.py` 验证纯权限判断；`tests/test_group_public.py` 验证 SQLite 查询、事务、跨群隔离、迁移；`tests/test_handlers.py` 用 AstrBot API 模拟桩验证 `main.py` 的指令方法和结构化 At 提取。

注意：模拟测试不能证明真实适配器一定将 `@群友` 转为 At。请在测试 QQ 群按这个顺序人工验证：

1. A 发送 `/持仓 设置 9432.T 100 150`，获得编号 #1。
2. B 发送 `/持仓 名单`，应看到 A；发送 `/持仓 查看 #1`，应看到 A 的 NTT。
3. B 在另一个群发送 `/持仓 查看 #1`，不可看到原群的 A（除非新群本身已经有对应 #1）。
4. B 使用 `/持仓 买入 9432.T 5 200`，只增加 B 的持仓，A 仍为 100 股。
5. 如果是 OneBot，再尝试 `/持仓 查看 @A`；QQ 官方机器人如果不支持，继续使用 `#编号`。
6. A 把 9432.T 设置为 `0 0`：A 在 `/持仓 名单` 中隐藏，但历史及 #1 仍保留。
7. 重载插件并重试，编号和数据保持不变。

## 10. 安全与已知限制

- 仅开放群内查询，未提供私聊查询、群间复制和管理员代操作。
- 群内已登记的人即使退群，持仓不会自动删除或隐藏；要做实时群成员状态校验需要对接特定平台 API，暂不在 MVP 范围。
- 成本信息公开意味着每位群友可以看到别人的数量和成本；使用前请获得群友知情同意。
- 当前是小群、单 AstrBot 进程、单线程 SQLite 实现。多个线程/进程并发访问需要额外设计。
- 交易历史暂不对其他人公开。以后若开放交易历史，需要单独做产品权限设计。

官方资料：
- https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- https://docs.astrbot.app/dev/star/plugin-new.html
