# 群友持仓记录（AstrBot）

**教学版 v0.2.0（第六讲）**。在第五讲的 SQLite 持仓账本上增加：群成员公开名单、群内稳定编号、全群持仓汇总、身份权限单测、QQ @ 提及的降级查询。

> 本插件登记用户主动填写的持仓与交易，不连接券商，也不会执行真实证券交易。群内成员可查看别人主动登记的持仓；仅发起消息的本人可更改自己的持仓。

## 仓库地址

https://github.com/aaeviru/astrbot-plugin-stock-

## 运行与安装

将整个 `astrbot_plugin_portfolio` 文件夹放入 AstrBot `data/plugins/`，在插件管理中重载。测试前确保 AstrBot `data` 在 NAS/Docker 上持久化。插件会把 SQLite 数据库保存在 `data/plugin_data/astrbot_plugin_portfolio/portfolio.db`。

不需要额外 pip 依赖，使用 Python 标准库的 SQLite、Decimal、unittest。

## QQ 群命令

```text
/持仓 身份
/持仓 设置 9432.T 100 150
/持仓 买入 9432.T 200 165.7
/持仓 卖出 9432.T 100 170
/持仓 查看                         # 查看自己
/持仓 查看 #1                     # 查看群内固定编号为 #1 的用户
/持仓 查看 用户ID                  # 按平台原始用户 ID 查看
/持仓 查看 @群友                   # 仅当适配器把 @ 转为真实 At 段时支持
/持仓 名单                         # 当前群有持仓的登记用户（前 10 人）
/持仓 名单 2                       # 下一页
/持仓 汇总                         # 全群按股票汇总（前 10 只）
/持仓 汇总 2                       # 下一页
/持仓 历史                         # 仅本人可查询历史
/持仓 帮助
```

`/持仓` 本身会展示 AstrBot 指令组的帮助树，不等于 `/持仓 查看`。

`/持仓 设置` 表示覆盖校准，不是一笔真实买卖；设置为 0 0 会删除当前持仓，但保留交易历史。`/持仓 买入` / `卖出` 用移动平均法计算成本和已实现盈亏。无手续费、税费、汇率、股份拆分、碎股或行情估值。

## 隐私和权限说明

- **写入**：从消息事件 `get_platform_id()`、`get_group_id()`、`get_sender_id()` 得到可信写入范围；命令参数不能指定其他所有者。
- **读取**：可以指定别人用户 ID、`#编号` 或真实 `At` 消息段，但平台、群永远取自发起查询消息。私聊默认拒绝。
- **编号不是密钥**：编号只用于查询，不可用于修改他人持仓。
- **名单不是 QQ 的实时成员表**：仅显示当前有持仓的用户；已经全部清仓的用户会隐藏，但历史与原编号仍然保留。退群者的持仓不会自动清理。
- 昵称是最后一次记账时从消息事件取得的显示快照；可能过期，不参与认证；为避免误 @ 全体用户，显示时把 `@` 转成全角形式。
- `@群友`：OneBot 可能提供结构化的 `At(qq=...)`；QQ 官方机器人群聊可能只提供 `member_openid`、不提供可用于指定其他人的 At 段。因此 **优先推荐** `/持仓 名单` + `/持仓 查看 #编号`。适配器能力需要在实际 QQ 环境验证。
- 公告和账本都对**当前群内能够发指令的成员**可见，没有对真实 QQ 群成员列表做在线校验；不要登记不愿在群内公开的资产。

## 数据库 v1 -> v2 升级

从第五讲的数据结构 `user_version=1` 升级到本版 `user_version=2`：

1. 重载时创建 `portfolio_members` 表（群内固定编号）。
2. 将原有 `holdings` 和 `trades` 中出现过的用户补录为登记用户，编号在每群内按用户 ID 稳定排序赋予。
3. 使用一个数据库事务提交迁移结果和版本号，原有交易记录和持仓不会被删除。

**建议升级前先备份**：停止插件/容器后备份 `portfolio.db`，或者在数据库仍运行时使用 Python `sqlite3.Connection.backup()` 在线备份。不要直接复制正在写入中的 WAL 文件组合，也不要将数据库提交到 Git。

- `holdings`：当前持仓、复合主键
- `trades`：交易历史、消息 ID 去重，原子更新
- `portfolio_members`：每群递增固定编号、昵称显示快照
- 用 Python Decimal 汇总相同股票和币种的持仓；避免在 SQLite 中用浮点 `SUM(total_cost)` 计算十进制文本。

## 自动化测试

在名为 `astrbot_plugin_portfolio` 的插件目录的**父目录**执行（Git 克隆时建议指定此目录名）：

```sh
git clone https://github.com/aaeviru/astrbot-plugin-stock-.git astrbot_plugin_portfolio
```

然后执行：

```sh
python -m unittest discover -s astrbot_plugin_portfolio/tests -v
```

测试覆盖：原 15 项账本逻辑、新增数据迁移、编号持久化、全群聚合、跨群/跨平台隔离、权限解析、消息去重、事务回滚、以及使用 AstrBot API 模拟桩的命令 Handler 验证。

命令 Handler 测试使用的是**模拟事件**，不是对真实 OneBot/QQ 官方适配器的集成测试。必须在测试 QQ 群中试用 `@`、`#` 和命令解析再正式使用。

## 文件说明

- `main.py`：注册指令与回复；不直接写 SQL
- `identity.py`：由消息事件获得本人身份；从真实 At 段解析读取目标
- `services/access.py`：不依赖 AstrBot 的目标解析与验证
- `services/portfolio.py`：纯 Decimal 买卖计算
- `models.py`：Owner、Holding、Trade、GroupMember、GroupPosition 等结构
- `repositories/sqlite.py`：SQL、事务、写入去重、迁移、全群聚合
- `tests/`：离线 unittest

## 后续限制

目前按 **同一 Python 进程 / 同一线程 / 单 SQLite 连接，事务内无 await** 的模式工作，符合单个小群教学验证。不要将连接直接跨线程共享。未来提高并发时，要重新设计连接访问和锁策略。

未来更换 PostgreSQL 应创建新的 Repository 实现和独立 schema；SQLite `TEXT` 成本可以迁移到 PostgreSQL `NUMERIC`，但事务和并发锁逻辑需要重新测试。
