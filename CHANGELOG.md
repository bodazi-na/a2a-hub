# 变更历史

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] — 未发布

首个对外可用的版本。此前只有内部基线。

> 这一版含 **schema 迁移（v1 → v2）** 和若干**破坏性行为变更**，
> 升级前请读下面的「升级须知」。

### 升级须知（从 0.1 基线升级）

| 变更 | 影响 | 需要做什么 |
| --- | --- | --- |
| **schema v1 → v2** | 新增 `tasks.started_at` 列 | **什么都不用做** —— hub 启动时自动迁移，老数据完好，`started_at` 回填为 `created_at` |
| **`durationMs` 语义变了** | 从 `started_at` 起算，不再从 `created_at` 起算 | 无需改动。但**历史编排任务的时长仍是旧口径**（虚高），无法追溯还原 |
| **argv 会拒绝含元字符的值** | 下游返回的 session id / model 里若含 `& \| > ^ %` 等字符，调用会**明确报错**而不是静默拆错 | 正常情况无需改动（真实 session id 都是 UUID / 十六进制）。若报错，检查下游返回的标识符 |
| **`synchronous=NORMAL`** | 断电可能丢掉最近若干事务（进程崩溃不丢） | 无需改动。若你的场景要求金融级持久性，请显式改回 `FULL` 并接受性能代价 |
| **事务不可重入** | 在事务里再开事务会抛 `RuntimeError` | 只在写扩展时可能遇到。把多个写操作合成一个 `work()` |

### 新增

**核心**

- `RunPlan` / `GetPlan`：多 agent 编排 —— 拓扑分层、同层并行、`{{steps.x}}` 模板变量传参、依赖自动推断
- 统一审计：`traceId` 贯穿一次顶层请求，`GetTrace` / `ListTraces` + `/admin/trace/<id>` 人类可读时间线
- 只读控制台 `/console`（单页 HTML，零前端依赖，含甘特图）
- CLI 类适配器：`ClaudeCLI` / `QoderCLI` / `CodexCLI` / `DshCLI` —— 直接起子进程，零常驻进程
- `mcp_facade/`（可选组件）：把 hub 包成 MCP server，让 Codex / Qoder / Claude Code / WorkBuddy 等平台能调它
- Bearer 鉴权（`--token` / `HUB_TOKEN`）

**存储与一致性**

- `Store.add_messages()`：一次事务写多条消息
- `Store.read_txn()`：读事务快照，消除跨语句撕裂读
- `Store.merge_task_metadata()`：原子 metadata 合并
- `Store._write_txn()`：统一写事务（异常必回滚、`IntegrityError` 重试、不可重入保护）
- schema **v2**：新增 `tasks.started_at`，与 `created_at`（记录诞生）区分开

**子进程与流**

- Windows **Job Object** 回收整棵进程树（`KILL_ON_JOB_CLOSE` + 句柄语义）
- argv **白名单校验**（`SAFE_ARG_RE`）—— 拒绝会被 `cmd.exe` 重新解释的值
- 事件流改为**按块读 + 自己切行**：单行超限截断而不毁掉整轮；跨行 JSON 可重组；噪声行有上限
- 过程事件**批量落库**（一个事务，而非 N 个）

**工具与测试**

- `examples/mock_agent.py`：零依赖演示下游（快速开始用它）
- `tools/run_plan.py`：长编排提交器（客户端超时可配、结果落盘）
- 测试分**三档**：单元级 / 平台级 / 集成级，共 17 项
- CI 覆盖 ubuntu + windows × py3.10 / py3.13

### 修复

按发现顺序（编号指向 [`docs/drill-2026-10-06-real-load.md`](docs/drill-2026-10-06-real-load.md)）：

**P0**

- **编排路径的取消是假成功**（N1）—— `dispatch_task` 不登记 `_running`，
  导致 `CancelTask` 只能改数据库状态而拦不住执行：子进程继续跑、继续写盘，
  留下「`canceled` 却带完整 response artifact」的矛盾记录
- **编排任务的计时基准错了**（N2）—— 拿占位任务的 `created_at` 当开始时刻，
  把层间排队等待算进执行时长（实测虚高 3.4 倍），控制台甘特图上所有条都从 t=0 起画

**P1**

- `add_message` 异常回滚不全，会让连接卡在未提交事务里**毒化后续全部请求**（M1）
- 已进入终态的任务仍会被派往下游，产生真实副作用却永不落库（M3）
- 取锁与结算段在保护块之外，异常路径下任务永远停在活跃态（M4）
- `_send_message` 吞掉调用方取消，且不转发给后台任务（P1-4）
- metadata「读 + 整列替换」非原子，并发写者互相覆盖（P1-6）
- `_kill` 忽略 `taskkill` 返回码，权限不足时**静默漏杀**（P1-7）
- 参数直接拼进 `cmd.exe /c`，会被重新解释（P1-8）
- **失效 session 被盲目续接**（本次演练新发现）—— session 在下游不存在时仍被
  当 `--session-id` 传下去，且死映射不被清理，该 contextId 之后**每次**都失败。
  现在会清映射并重试一次

**P2**

- 缺省 `planId` 用 `abs(hash(...))`，并发同名 plan 混审计（P2-1）
- `noise` 列表无界增长（P2-5）
- 兜底取消会给已完成任务追加「已取消」消息（P2-7）
- deadline 在 spawn 之后才建立（P2-8）
- 先写产物再写终态，取消后留下不一致（P2-9）
- 裸命令名经 PATH 解析到 `.cmd` 时未包裹 `cmd.exe`（P2-14）
- `STREAM_LIMIT` 是硬上限而非截断，一行超限毁掉整轮（P2-17）
- 按 PID 杀有 TOCTOU，可能杀错进程（P2-21）
- `update_task` 的 UPDATE 与回读分属两条语句（P2-12）
- 多段独立查询产生撕裂读（P2-23）
- 逐行解析，多行 JSON 无法重组且**静默丢数据**（P2-24）

### 性能

| 项 | 改前 | 改后 |
| --- | --- | --- |
| 50 个过程事件落库的事件循环阻塞 | 192.8 ms | **0.9 ms** |
| 单次 `add_message` | 4.57 ms | ~0.06 ms 量级 |

### 文档

- `docs/architecture.md` —— 分层、数据流、扩展点、数据库
- `docs/adr/` —— 6 条架构决策记录（含被否决的方案与理由）
- `CONTRIBUTING.md` —— 三档测试、写适配器、提交约定
- `docs/drill-2026-10-06-real-load.md` —— 一次真实负载演练的完整记录

### 已知问题

- 只实现 Windows；非 Windows 下 CLI 适配器未经测试
- 不做流式（Agent Card 声明 `streaming: false`），审计时间线无法用于时序分析
- `RunPlan` 同步阻塞，长编排需用 `tools/run_plan.py`
- 各节点写盘能力不一致且不可声明
- 清单里仍有 19 条 P2 未处理（索引细节、容错可观测、资源上限、语义边界），**无 P0/P1**
