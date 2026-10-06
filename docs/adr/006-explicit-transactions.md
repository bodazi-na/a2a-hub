# ADR-006：多语句读写必须显式包事务；事务不可重入

## 状态

已采纳（2026-10-06）

## 背景

`Store` 用 `sqlite3.connect(..., isolation_level=None)` —— 自动提交，
**每条语句各自一个隐式事务**。这对单条读写没问题，但三处多语句场景全出了错：

| 场景 | 症状 |
| --- | --- |
| metadata 合并 = 「读 → 整列替换」两步 | 两个并发写者**互相覆盖**：后写的把前一个刚加的键整列冲掉 |
| `update_task` = UPDATE + 回读两条语句 | 返回的 task 可能与 `_updated` 对不上 —— 调用方以为「状态归我了」，看到的却是别人改过的状态 |
| `list_tasks` + `count_tasks`、`get_task` + `list_messages` + `list_artifacts` | WAL 下没有跨语句快照 → **撕裂读**：典型症状是「状态已是终态，但 artifacts 还没到齐」，调用方以为产物丢了 |

## 决策

**所有多语句读写都显式包事务，且事务不可重入。**

| 用途 | API | 实现 |
| --- | --- | --- |
| 写 | `Store._write_txn(work)` | `BEGIN IMMEDIATE`；任何异常必 ROLLBACK；`IntegrityError` 重试 |
| 读 | `Store.read_txn()`（上下文管理器） | `BEGIN DEFERRED`，整串查询拿一致快照 |
| 改 metadata | `Store.merge_task_metadata(task_id, patch)` | 读-合并-写放进同一个写事务 |

嵌套时抛 `RuntimeError` 并说明怎么办，**不**让 SQLite 抛
`cannot start a transaction within a transaction`。

## 理由

**为什么读也要事务**：WAL 模式允许「写不阻塞读」，代价是**读者默认看到的是
每条语句各自的即时状态**。要拿到一致快照，必须显式 `BEGIN DEFERRED` ——
之后本连接读到的是事务开始那一刻的版本。

撕裂读的复现方式值得记住：**开第二个连接，在读序列的两次查询之间插一笔写**。
WAL 下那笔写立刻生效，有读事务时读方看不到它（快照），没有就会看到。

**为什么不用 SQL 的 `json_patch()` 做原子合并**：它的语义是 RFC 7396 ——
**值为 null 的键会被删除**，而我们要的是「把键设成 null」。
在「补 `planId=None`」这类场景下两者行为不同。
**「SQL 内合并」不等于「语义等价」**，采纳前要逐条对语义。

**为什么事务不可重入**：`BEGIN IMMEDIATE` 在已有事务时必然失败。让 SQLite
报的话信息毫无指向性 —— 它不告诉你**是谁**在哪儿嵌套的。用 `_depth` 计数器
主动报错，能把排查时间从「翻半天调用栈」降到「看一行错误」。

## 后果

**变容易的**：

- 「一次 payload 的多段查询来自同一版本」成了默认保证，调用方不用再操心
- metadata 的并发写不再丢键
- 事务嵌套会立刻、明确地失败，而不是留下一个卡住的事务

**变难的 / 需要遵守的**：

- **加事务本身会引入新的失败模式**：嵌套、事务不释放、只读事务里做了写。
  所以保护和测试是一起加的 —— `tests/test_query_consistency.py` 里专门有两条
  测「嵌套报错」和「事务用完释放」
- `_write_txn` 里**不要**再 `BEGIN`/`COMMIT`
- `read_txn` 里**只读**。在里面写不会立刻对别人可见，语义上也说不通
- `BEGIN IMMEDIATE` 会立刻拿写锁。对纯读路径不要用它 —— 那是 `read_txn` 的事

## 相关

- ADR-003（持久化设置）
- `docs/architecture.md` 的「数据库」节
- `tests/test_query_consistency.py`
