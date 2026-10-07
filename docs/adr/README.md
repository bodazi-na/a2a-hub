# 架构决策记录（ADR）

这里记录**为什么这么选**，尤其是**被否决的方案和否决理由**。
只写「我们决定用 X」而没写「为什么不选 Y」的记录，等于没写 ——
下一个人会把 Y 重新提一遍。

| 编号 | 决策 | 一句话 |
| --- | --- | --- |
| [001](001-a2a-only-mcp-as-facade.md) | 协议面只做 A2A，MCP 作为可选 facade | 不做 REST/OpenAPI；MCP 是接入层的事，不是内核的事 |
| [002](002-thin-core-thick-adapters.md) | 薄核心 + 厚适配器，CLI 优先、桥为备选 | 判断能不能接入的标准是「有没有 headless 入口」，不是「是不是 Electron」 |
| [003](003-wal-synchronous-normal.md) | 持久化用 WAL + `synchronous=NORMAL` | 代价是断电可能丢最近若干事务；进程崩溃不丢 |
| [004](004-job-object-for-process-tree.md) | 用 Job Object 回收进程树，`taskkill` 降为兜底 | 句柄语义消除 PID 复用的 TOCTOU；能收掉 `/T` 够不着的孙进程 |
| [005](005-no-to-thread-for-store.md) | **不**把 Store 改造成 `asyncio.to_thread` | 实测根因是一次 fsync，改 PRAGMA 就拿到 64 倍 —— 重构不划算且新增竞态面 |
| [006](006-explicit-transactions.md) | 多语句读写显式包事务；事务不可重入 | 读用快照事务、写用 `_write_txn`、改 metadata 用原子合并 |
| [007](007-streaming-bounded-queue.md) | 实时流用**有界队列 + 丢最老**做背压 | 慢客户端只影响自己，绝不拖住正在跑的任务；丢了多少随流送出 |
| [008](008-src-layout-single-top-level-package.md) | src 布局 + 单一顶层包 `a2a_hub` | 「建议装独立 venv」是**把设计缺陷转嫁成用户的负担** —— 能被修掉的问题不该写成使用注意事项 |

## 怎么写一条新 ADR

复制这个骨架，编号取下一个可用值：

```markdown
# ADR-00N：<决策标题>

## 状态
提议中 | 已采纳（YYYY-MM-DD） | 已废弃 | 被 ADR-XXX 取代

## 背景
什么问题逼出了这个决策？有哪些可选方案？

## 决策
决定了什么？（一句话能说清最好）

## 理由
为什么是这个而不是别的？**被否决的方案要写清否决理由。**

## 后果
哪些事变容易了？哪些事变难了、或需要接受什么代价？

## 相关
链接到别的 ADR / 文档 / 测试
```

**几条纪律**：

- **「决定不做什么」也是决策**，同样值得记录（见 ADR-005）
- **写清代价**。只说收益的 ADR 是宣传，不是记录
- **数字要来自实测**，不要写「更快」「更稳」这种无法验证的话
- 决策变了不要改旧 ADR，**新写一条并标注取代关系**
