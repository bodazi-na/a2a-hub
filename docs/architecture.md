# 架构说明

> 想改这个仓库的代码，先读这一页。
> 「为什么这么设计」的决策依据见 [`adr/`](adr/)；这一页讲「它是怎么搭的」。

## 一句话

hub 是**一个标准 A2A agent**，对外是服务端、对内是协调者：
它把本机若干个 AI agent 注册进来，统一做注册发现、能力路由、任务持久化、
多 agent 编排和审计。

## 分层

```
┌─────────────────────────────────────────────────────────────────┐
│  客户端（任何会说 A2A 的东西）                                    │
│    curl · A2A SDK · MCP 平台（经 mcp_facade）· 本机 skill         │
└────────────────────────────┬────────────────────────────────────┘
                             │  JSON-RPC over HTTP
┌────────────────────────────▼────────────────────────────────────┐
│  core/  ——  平台无关，可开源                                      │
│                                                                  │
│    hub_app.py      A2A 服务端：路由表、鉴权、任务生命周期、admin 端点 │
│    orchestrator.py 编排引擎：拓扑分层 → 同层并行 → 模板变量传参     │
│    router.py       能力路由（按名 / 按 tag / fail-closed）          │
│    registry.py     注册表 + 健康探测                               │
│    store.py        SQLite 六表 + schema 迁移 + 事务边界            │
│    console.py      只读控制台（单页 HTML，零前端依赖）              │
└────────────────────────────┬────────────────────────────────────┘
                             │  Adapter 契约（base.py）
┌────────────────────────────▼────────────────────────────────────┐
│  adapters/  ——  每个下游一层，可插拔                              │
│                                                                  │
│    a2a_http.py   对接「已经在跑的 A2A 服务」（需常驻进程）         │
│    cli.py        直接起子进程调 CLI 本体（零常驻进程）             │
└────────────────────────────┬────────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   claude / codex       dsh / qoder         任意 A2A 服务
   （CLI 子进程）        （CLI 子进程）       （HTTP）
```

**这条分界线就是「薄核心 / 厚适配器」**：core 不认识任何具体工具，
adapters 吃掉每个工具的差异。**新增一种执行体 = 新增一个 `Adapter` 子类，core 一行不改。**

## 一次请求的数据流

```
客户端 SendMessage
   │
   ├─① 落库（先落库再派活）           store.create_task(state=submitted)
   │     任务在任何下游动作之前就已持久化
   │
   ├─② 路由                            router.route(agent=… / tags=…)
   │     fail-closed：只回退 unknown，不回退已知 down
   │
   ├─③ 取适配器                        hub_app._get_adapter()
   │     缓存键含配置签名 → 改配置即时生效
   │
   ├─④ 抢状态                          update_task(state=working, only_from=(…))
   │     _updated=False ⇒ 任务已被取消/终态 ⇒ **不派活**
   │
   ├─⑤ 派活                            adapter.call(prompt, session_id=…, on_event=…)
   │     CLI 系：起子进程 · 写 stdin · 读事件流 · 收进 Job Object
   │     on_event 给了 ⇒ 每解析出一条事件**立刻**推给订阅者（流式）
   │
   ├─⑥ 结算                            _settle_result()
   │     先抢终态（only_from 裁决）→ 抢到才写过程事件与 artifact
   │     过程事件带**自己的发生时刻**落库（不是落库时刻）
   │
   └─⑦ 返回 A2A task payload
```

**关键顺序都不能换**：

- ①在②③之前 —— 路由失败也要留下任务记录（`failed` + 原因），不能「任务凭空消失」
- ④在⑤之前 —— 否则取消拦不住下游，会派活、产生真实副作用却永不落库
- ⑥先抢终态再写产物 —— 反过来会留下「已取消却带 response artifact」的假象

## 实时流（SSE）

```
客户端 SendStreamingMessage
   │
   ├─① 与 SendMessage 完全同一条准备路径（_prepare_task，两条路不会分叉）
   │
   ├─② **先订阅**（hub.subscribe(task_id)）
   │     反过来的话，订阅与派活之间产生的事件会永久丢失
   │
   ├─③ 回放已落库的历史（接入已有任务时用）
   │     订阅在先 ⇒ 可能重复 ⇒ 回放时记 (kind, ts) 去重
   │
   ├─④ 派活（后台）
   │     adapter 每解析出一条事件 → _publish → 推给所有订阅者
   │
   └─⑤ 逐帧 yield → 末帧是完整 Task（含终态与产物）
```

**设计要点**：

- 载荷是标准 A2A `StreamResponse`（`statusUpdate` / `task`），过程事件的语义
  放 `metadata.kind` —— **不认识我们的客户端也能当进度消息正常显示**
- 订阅队列**有界**，满了丢最老的并计数。慢客户端只影响自己，
  **绝不拖住正在跑的任务**（理由见 [ADR-007](adr/007-streaming-bounded-queue.md)）
- 总线是**进程内**的，不持久化。持久化由 `store` 负责，总线只服务
  「同一时刻正在看的人」—— 重启丢订阅是对的，不该假装能恢复
- 心跳走 SSE 注释行（`: keep-alive`），不污染数据
- `on_event` 是**后加的可选参数**。老签名的适配器由 `_accepts_on_event`
  探测并跳过 —— 它们照常工作，只是推不了实时事件

## 适配器契约

`adapters/base.py` 定义两件事：`Adapter` 抽象类，以及**下游差异归一化**。

| 类型 | 类 | 对接对象 | 需要常驻进程 |
| --- | --- | --- | --- |
| **HTTP** | `A2AHttpAdapter` | 已经在跑的 A2A 服务 | 是 |
| **CLI** | `ClaudeCLI` / `QoderCLI` / `CodexCLI` / `DshCLI` | 直接起子进程调 CLI 本体 | 否 |

CLI 类少一层桥、少一个常驻进程，代价是自己管子进程生命周期、超时、取消。
**单机自用优先 CLI；跨机器或对外暴露才起桥。**

### 下游差异归一化

各家 CLI 的字段名不统一，统一在 `base.py` 收口：

| 差异 | 归一化 |
| --- | --- |
| 会话 id 字段名 | `dshSessionId` / `codexThreadId` / `thread_id` / `sessionId` → `session_id` |
| 用量字段名 | `inputTokens` / `input_tokens` / `prompt_tokens` → `input` |
| 用量与元信息 | **严格分开**：`usage` 只放 token 数，`metadata` 放 exitCode / profile / 各类 id |

混在一起会让「用量」失去语义 —— 想统计成本时得先猜哪些键是数字、哪些是标识。

### 四个 CLI 的实测契约

| CLI | 非交互 | 输出格式 | 会话续接 |
| --- | --- | --- | --- |
| claude | `-p --verbose` | `--output-format stream-json` | `--resume <id>` |
| qodercli | `-p` | `-o stream-json` | `-r <id>` |
| codex | `exec` | `--json`（JSONL） | `exec resume <id>` |
| dsh | `--profile headless` | `--json`（NDJSON） | `--session-id <id>` |

输出解析分两族：**claude 族**（claude / qodercli，schema 逐字段一致）
与 **jsonl 族**（codex / dsh，逐行事件）。

## 写 CLI 适配器必踩的坑（全部实测过）

1. **prompt 走 stdin 并主动 close**。既解决 argv 长度问题（Windows CreateProcess
   有 32K 上限），又给出明确的输入结束信号。**不能用 `DEVNULL`** —— 那样既没数据
   也没有边界，codex / claude 会一直等（实测让 codex 挂死 4 分半直到超时）。
2. **`.cmd` / `.bat` 不能直接被 CreateProcess 执行**，要用 `cmd.exe /d /c` 包一层。
   **正因为要经 cmd.exe，由下游决定的值必须过白名单** —— cmd 会重新解释命令行：
   `&` 会切断命令（`a&calc` 会真的执行 calc）、`|` 变管道、`>` 变重定向、`^` 被吃掉，
   而 **`%` 展开连引号都拦不住、且无法可靠转义**。细节见 `adapters/cli.py` 的 `SAFE_ARG_RE`。
3. **claude 的 `--print` 配 `stream-json` 必须带 `--verbose`**，否则直接报错退出。
4. **取消要收整棵进程树**，用 **Job Object**（`KILL_ON_JOB_CLOSE` + 句柄语义），
   `taskkill /T /F` 降为兜底。只 `proc.kill()` 杀不掉 `.cmd` 外壳下的执行体；
   而按 PID 定位有 TOCTOU（进程若在检查与终止之间退出，PID 可能被复用 → 杀错进程）。
   验证时注意数**正确的映像名** —— dsh 的执行体是 `DeepSeek Harness.exe` 而不是 `node.exe`。
5. **事件流不要用 `readline()`**。`limit=` 是 StreamReader 的**硬上限**，
   单行超限会抛 `LimitOverrunError` 并让整个 call 失败 —— 一条超大工具输出就能毁掉一轮。
   按块读 + 自己切行，超长行截断并计数。

## 写 HTTP 适配器必踩的两个坑（实测）

1. **本地探测必须 `trust_env=False`**。机器上若有系统代理（实测某台机器上是
   `127.0.0.1:7393`），`127.0.0.1` 的请求会被代理拦下并返回 **502**
   —— 不是 connection refused。极易误判成「服务在跑但报错」。
   命令行验证时同理，需要 `curl --noproxy '*'`。
2. **拉 Agent Card 与发 JSON-RPC 不要复用同一个 keep-alive 连接**。
   部分实现（实测 `codex-a2a`）在同一连接上先 GET 再 POST 会稳定返回 404。
   适配器里每次请求都用独立的 `AsyncClient`。

## 目录分层：代码 / 运行时 / 本机配置

这个仓库**可以独立使用，也可以直接开源** —— 代码层不含任何本机路径、凭据或运行时数据。

```
a2a-hub/                     ← 代码层（进仓库）
├── core/                    store · registry · router · orchestrator · hub_app · console
├── adapters/                base · a2a_http · cli
├── mcp_facade/              可选：把 hub 包成 MCP server
├── probes/                  process_monitor（本机进程探测）
├── examples/                演示下游 + 配置样例
├── docs/                    架构与 ADR
├── tools/                   run_plan.py（长编排提交器）
├── tests/                   三档自检
├── hub.py                   命令行入口
└── pyproject.toml / README.md / LICENSE

以下由 .gitignore 排除（运行时层，**注意必须锚定到仓库根**）：
├── /data/                   SQLite 数据库
├── /workspace/              下游 agent 的工作目录（它们在这里写文件）
└── /agents/                 你的本机 agent 配置（含真实路径与命令）
```

**为什么要分**：

- **`workspace/` 必须独立** —— CLI 子进程的 cwd 默认继承 hub 进程，而 hub 通常就跑在
  代码目录里。不给它独立工作目录的话，agent 随手写个文件就落进仓库
  （实测把 `history-of-computing.md` 和 `memory/` 写进了项目根）。
  现在 `Hub` 会给没配 `cwd` 的 CLI 适配器注入 `<cwd>/workspace`。
- **`agents/` 必须独立** —— 里面的配置写死了本机路径，还可能带模型 ID 之类的
  环境特定值。仓库里只放 `examples/agents/` 的占位符版本。
- **凭据不进文件** —— Qoder 的 PAT 走环境变量 `QODER_PERSONAL_ACCESS_TOKEN`，
  hub 的 token 走 `HUB_TOKEN` 或 `--token`，都不落盘。

> `.gitignore` 里这些目录模式**必须写成 `/data/` 这种锚定形式**。
> 写 `agents/` 会匹配任意层级下名叫 `agents` 的目录，把 `examples/agents/`
> 一起忽略掉 —— 陌生人 clone 下来会一个示例都拿不到。

**移植到新机器**：`git clone` → `pip install -e .` →
照着 `examples/agents/` 写自己的 `agents/*.json` → 起服务。

## 扩展点

| 想做什么 | 改哪儿 |
| --- | --- |
| 接入一种新的 CLI 工具 | 在 `adapters/cli.py` 加一个 `CLIAdapter` 子类，实现 `build_argv` / `parse_line` / `finalize` 三个方法 |
| 接入一种非 A2A 的协议 | 在 `adapters/` 加一个新的 `Adapter` 子类；core 不用动 |
| 加一个 A2A 方法 | `hub_app.py` 的 `_methods` 路由表 + 一个 `_xxx` 方法 |
| 加一张表 / 一列 | `store.py` 的 `SCHEMA_SQL` + `_migrate()`（**必须同时加迁移**，老库要能升上来） |
| 加一个 admin 端点 | `hub_app.py` 的 `routes()` |
| 换控制台样式 | `core/console.py`（单页 HTML，零前端依赖） |

**加迁移的约定**：`SCHEMA_VERSION` 加一，并在 `_migrate()` 里用
`PRAGMA table_info` 判断列是否存在再 `ALTER TABLE`。
索引要等列补好之后再建（老库上 `executescript` 阶段还没有那些列）。

## 数据库

六张表：

| 表 | 作用 |
| --- | --- |
| `tasks` | 任务：状态机、`plan_id`/`step_id`/`parent_id`/`trace_id`、三个时间戳 |
| `messages` | 过程回传：`seq` 在任务内自增，`UNIQUE(task_id, seq)` 兜底 |
| `artifacts` | 产物（最终答复等） |
| `contexts` | `(contextId, agent) → sessionId` 映射，用于跨轮续接 |
| `agents` | 注册表（含 `config`、`health`） |
| `schema_version` | 迁移版本 |

**三个时间戳是三件事**：`created_at`（记录诞生）、`started_at`（真正开始执行）、
`finished_at`（进入终态）。编排层会预先为每个 step 落占位任务，拿 `created_at`
当开始时刻会把层间排队等待算进执行时长 —— 实测虚高 3.4 倍。

**事务约定**：`isolation_level=None`（自动提交），所以**多语句读写必须显式包事务**：

- 写用 `_write_txn()`（`BEGIN IMMEDIATE`，异常必回滚，事务不可重入）
- 读用 `read_txn()`（`BEGIN DEFERRED`，整串查询拿到一致快照）
- 改 metadata 用 `merge_task_metadata()`，**不要**「读出来 + 整列替换」

## 相关文档

- [`adr/`](adr/) —— 为什么这么选（含被否决的方案与理由）
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) —— 怎么跑测试、怎么提 PR
- [`drill-2026-10-06-real-load.md`](drill-2026-10-06-real-load.md) —— 一次真实负载演练的完整记录
