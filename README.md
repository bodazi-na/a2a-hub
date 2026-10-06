# a2a-hub

多 Agent A2A 协调内核。**薄核心 + 厚适配器**：core 平台无关可开源，adapters 每个工具一层可插拔。

它解决的是「一堆 A2A agent 各自为政」的问题——没有注册中心、没有路由、任务状态重启即失。hub 把这三件事补上，并且**自己也是一个标准的 A2A agent**，任何会说 A2A 的客户端都能调它。

## 它做了什么

| 能力 | 实现 |
| --- | --- |
| **持久化** | SQLite 六表（tasks / messages / artifacts / contexts / agents / schema_version），WAL 模式 |
| **注册与发现** | Agent Card 注册表，落库；健康探测走 HTTP 拉卡片 |
| **路由** | 按 agent 名或能力标签（tag / Agent Card 里的 skills）选节点 |
| **A2A 服务端** | 对外暴露 Agent Card + JSON-RPC（SendMessage / GetTask / ListTasks / CancelTask） |
| **过程回传** | 下游的 thinking / tool_call / tool_result / text 统一映射成消息落库 |

**核心设计：先落库，再派活。** 任务在任何下游动作之前就已经写进 SQLite，所以进程崩了、重启了，任务状态依然查得到；下游失败也不会丢任务，只会置为 `failed` 并记下原因。

## 快速开始

```bash
# 1) 依赖（已预装在隔离 venv）
#    C:\Users\a1299\.workbuddy-ai\binaries\python\envs\a2a-hub

# 2) 注册一个下游 agent
python hub.py register --name dsh --endpoint http://127.0.0.1:9101 --tag dsh
python hub.py register --name codex --endpoint http://127.0.0.1:9100 --tag codex

# 3) 看注册表 / 探测健康
python hub.py agents
python hub.py probe

# 4) 启动 hub
python hub.py serve --host 127.0.0.1 --port 9200
```

调用它（标准 A2A）：

```bash
curl -X POST http://127.0.0.1:9200/ \
  -H "Content-Type: application/json" -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{
        "messageId":"m1","role":"ROLE_USER",
        "parts":[{"text":"用一句话说明这个目录是做什么的"}],
        "contextId":"ctx-1"}}}'
```

指定路由目标或能力：

```jsonc
"params": { "message": {...}, "agent": "dsh" }      // 显式指定
"params": { "message": {...}, "tags": ["codex"] }   // 按能力匹配
```

## 任务模型

| 模式 | 用法 | 适用场景 |
| --- | --- | --- |
| **同步**（默认） | `SendMessage` | 短任务、脚本调用 —— 阻塞到下游结束再返回 |
| **异步** | `SendMessage` + `"async": true` | 长任务 —— 立即返回 `task_id`，用 `GetTask` 轮询 |

```jsonc
// 异步派发（立即返回 TASK_STATE_WORKING）
{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
   "agent":"dsh", "async":true,
   "message":{"messageId":"m1","role":"ROLE_USER",
              "parts":[{"text":"..."}],"contextId":"ctx-1"}}}

// 轮询
{"jsonrpc":"2.0","id":"2","method":"GetTask","params":{"id":"<task_id>"}}

// 取消
{"jsonrpc":"2.0","id":"3","method":"CancelTask","params":{"id":"<task_id>"}}
```

**取消的边界**：`CancelTask` 会中断 hub 侧的等待，并把任务置为 `TASK_STATE_CANCELED`。
对 CLI 类 agent，取消会调用 `taskkill /PID <pid> /T /F` **杀掉整棵进程树** ——
因为 CLI 是 `cmd.exe /c xxx.cmd` 启动的，只 kill 外壳的话底下的执行体
（`claude.exe` / `DeepSeek Harness.exe` 等）会变孤儿继续烧 token。
对 HTTP 类 agent，只能中断本地请求，**下游服务端的执行不受影响** ——
A2A 协议也没强制要求，所以那一侧仍是「尽力而为」。

**并发**：hub 用信号量限制同时运行的下游任务数（默认 4，构造时用 `max_concurrency` 调整）。

**调试端点**：`GET /admin/agents`（注册表与能力）、`GET /admin/tasks`（运行中任务）。

## 适配器：两类下游

| 类型 | 类 | 对接对象 | 需要常驻进程 |
| --- | --- | --- | --- |
| **HTTP** | `A2AHttpAdapter` | 已经在跑的 A2A 服务（本机 9100 / 9101 桥） | 是 |
| **CLI** | `ClaudeCLI` / `QoderCLI` / `CodexCLI` / `DshCLI` | 直接起子进程调 CLI 本体 | 否 |

CLI 类少一层桥、少一个常驻进程，代价是自己管子进程生命周期、超时、取消。

### 四个 CLI 的实测契约

| CLI | 非交互 | 输出格式 | 会话续接 |
| --- | --- | --- | --- |
| claude | `-p --verbose` | `--output-format stream-json` | `--resume <id>` |
| qodercli | `-p` | `-o stream-json` | `-r <id>` |
| codex | `exec` | `--json`（JSONL） | `exec resume <id>` |
| dsh | `--profile headless` | `--json`（NDJSON） | `--session-id <id>` |

输出解析分两族：**claude 族**（claude / qodercli，schema 逐字段一致）与 **jsonl 族**（codex / dsh，逐行事件）。

### 注册一个 CLI agent

配置写文件（命令 / 工作目录 / 模型 / 环境变量），用 `--config` 指向它：

```bash
python hub.py register --name dsh-cli --kind cli --config agents/dsh-cli.json --tag dsh
```

配置形如：

```json
{"adapter": "DshCLI",
 "options": {"command": ["<dsh 启动器路径>"], "timeout": 300}}
```

Qoder 的 PAT **不要写进配置文件** —— 适配器的 `env` 会继承 hub 进程的环境变量，
启动时注入即可：`export QODER_PERSONAL_ACCESS_TOKEN=... && python hub.py serve`。

### 写 CLI 适配器必踩的三个坑（都实测过）

1. **stdin 要传 prompt 并主动关闭**。既解决 argv 长度问题（Windows CreateProcess 有 32K 上限），
又给出明确的输入结束信号。**不能简单用 `DEVNULL`** —— 那样既没数据也没有边界，
codex / claude 会一直等（实测让 codex 挂死 4 分半直到超时）。
2. **`.cmd` / `.bat` 不能直接被 CreateProcess 执行**，要用 `cmd.exe /c` 包一层。
   本机四个 CLI 里有三个是 .cmd 启动器。
3. **claude 的 `--print` 配 `stream-json` 必须带 `--verbose`**，否则直接报错退出。
4. **取消必须杀进程树**（`taskkill /T /F`），不能只 `proc.kill()`。
   本机四个 CLI 里三个是 .cmd 启动器，只杀外壳会让真正的执行体变孤儿。
   验证时注意数**正确的映像名** —— dsh 的执行体是 `DeepSeek Harness.exe` 而不是 `node.exe`。

## 编排

三种形态是同一套机制的三种写法：

| 形态 | 写法 |
| --- | --- |
| **串行链** | 每步依赖上一步 |
| **并行扇出** | 多步无依赖 → 同层并行；末尾 merge 步依赖它们全部 |
| **主管-工人** | 第一层是主管步，后续层依赖它（plan 仍由调用方显式给出） |

```jsonc
// 串行链：dsh 起草 → codex 审阅 → 汇总
{"jsonrpc":"2.0","id":"1","method":"RunPlan","params":{
  "contextId":"ctx-1",
  "input":"二进制搜索",
  "steps":[
    {"id":"draft", "agent":"dsh-cli",    "prompt":"用一句话解释 {{input}}"},
    {"id":"review","agent":"codex-cli",  "prompt":"这句有事实错误吗？\n{{steps.draft}}"},
    {"id":"final", "prompt":"拼成一行：{{steps.draft}} | {{steps.review}}"}
  ]}}
```

**依赖自动推断**：模板里写了 `{{steps.draft}}`，就自动加上 `dependsOn: draft` ——
不必手写两遍（漏写会导致同层并行、模板变量取不到值）。
显式写 `dependsOn` 仍然支持，用于「有先后顺序但不消费输出」的场景。

**失败策略**：默认 `fail-fast`（一步失败整条链停），可按步覆盖：

```jsonc
{"id":"flaky", "agent":"dsh-cli", "prompt":"...", "onError":"continue"}
```

`onError: continue` 的步失败后，**独立分支照常执行**，只有依赖它的下游被阻断。

**执行模型**：拓扑分层 → 同层并行（`asyncio.gather`）→ 逐层推进。
每一步都落库成一个 task，带 `planId` / `stepId`，用 `GetPlan` 可事后追溯整个 plan。

**A2A 兼容性**：编排不是 A2A 协议里的概念，`RunPlan` / `GetPlan` 是**扩展方法** ——
不影响标准方法，纯 A2A 客户端可以完全不感知它们的存在。

## 统一审计

一次顶层请求（`SendMessage` 或 `RunPlan`）共享一个 **`traceId`**，
把它引发的所有任务与消息串成一条可追溯的链。

**接口**：

| 方式 | 用途 |
| --- | --- |
| `GetTrace`（JSON-RPC） | 结构化数据：summary / tasks / timeline |
| `ListTraces`（JSON-RPC） | 最近的 trace 概览列表 |
| `GET /admin/trace/<traceId>` | 人类可读的时间线视图 |
| `GET /admin/traces` | 同上，列表 |

**输出三段**：

| 段 | 内容 |
| --- | --- |
| `summary` | 起止时间、总耗时、涉及哪些 agent、**归一化后的汇总用量** |
| `tasks` | 这次请求下的所有任务（含 plan step、父子关系、各自耗时） |
| `timeline` | 所有消息事件按时间拉平 —— 过程回传的完整复原 |

```
$ curl http://127.0.0.1:9200/admin/trace/trace-b04456c0158a
trace trace-b04456c0158a
  agents  : claude-cli, codex-cli, dsh-cli
  tasks   : 3 (ok 3 / fail 0 / cancel 0)
  elapsed : 20449 ms
  usage   : {"input": 37735, "output": 633, "cacheRead": 19328, "total": 38368}

  tasks:
    [completed] a      dsh-cli      3705ms   用一句话解释 快速排序
    [completed] b      codex-cli   15037ms   为「快速排序」写一个最小可运行示例的要点
    [completed] merge  claude-cli   5380ms   把下面两段合成一段…

  timeline:
    03:26:15.923  dsh-cli     text      用一句话解释 快速排序
    03:26:19.599  dsh-cli     thinking  The user asks for a one-sentence explanation…
    03:26:19.603  dsh-cli     text      快速排序是：从待排序列中选一个基准值…
    03:26:30.963  codex-cli   status    turn started
```

**从时间线能直接看出并行**：`dsh-cli` 与 `codex-cli` 在 `03:26:15` 同时启动。

**用量归一化**：下游的用量字段命名不统一（`inputTokens` / `input_tokens` / `prompt_tokens`…），
审计汇总前会先归一到 `input` / `output` / `cacheRead` / `cacheWrite` / `reasoning` / `total`。
**下游自报的 `total` 会被丢弃** —— 那是它自己那一次的合计，加进来会得到一个既不是总量也不是分量的怪数；
`total` 一律由汇总方用 `input + output` 现算。

## 鉴权

启用方式（默认关闭，仅绑回环时够用）：

```bash
python hub.py serve --token <your-token>     # 或 export HUB_TOKEN=<your-token>
```

**公开面刻意最小**：

| 路径 | 是否需认证 | 理由 |
| --- | --- | --- |
| `/.well-known/agent-card.json` | 公开 | A2A 规范要求卡片可被公开发现 |
| `/healthz` | 公开 | 监控/负载均衡探针 |
| `/console` | 公开 | 只是 HTML 壳，不含数据；它调的 `/admin/*` 仍需认证 |
| 其余（含所有 `/admin/*` 与 JSON-RPC） | **需要** | `Authorization: Bearer <token>` |

未通过时返回 401 + `WWW-Authenticate: Bearer`。token 比较用 `hmac.compare_digest`（定长比较）。

## 让任意 Agent 平台调用 hub

各个平台的扩展机制并不统一：WorkBuddy / DSH 用 **skills**，Codex / Qoder / Claude Code 用 **MCP**。
但**它们全都支持 MCP 客户端** —— 所以一个 MCP facade 就能覆盖全部平台，不必逐个写 skill。

### MCP facade（可选组件）

```
mcp_facade/           # 独立于 core，删掉它 hub 依然完整
└── server.py         # 通过 HTTP 调 hub（不 import core），所以可单独起停
```

暴露 6 个工具：

| 工具 | 作用 |
| --- | --- |
| `hub_status` | 健康检查：任务数 / 节点数 / 运行中 |
| `hub_agents` | 列出注册的 agent（名字 / 类型 / 健康 / 能力标签） |
| `hub_send` | 派任务（可指定 agent 或按 tag 路由，支持 `context_id` 续接） |
| `hub_plan` | 跑多 agent 编排流水线（steps 数组） |
| `hub_trace` | 查跨 agent 的审计时间线 |
| `hub_tasks` | 最近任务列表 |

**设计边界**：它是**接入形态**，不是核心。`core/` 保持纯 A2A 一行不改；
facade 通过 HTTP 与 hub 通信，所以两者解耦 —— 换掉 facade，hub 仍是完整的 A2A 服务。

### 各平台配置

复制 `examples/mcp/` 里对应平台的片段：

| 平台 | 配置文件 | 示例 |
| --- | --- | --- |
| WorkBuddy | `~/.workbuddy-ai/mcp.json` | `examples/mcp/workbuddy.json` |
| Codex | `~/.codex/config.toml` | `examples/mcp/codex.toml` |
| Qoder | `~/.qoder/mcp.json` | `examples/mcp/qoder.json` |
| Claude Code | `claude mcp add ...` | `examples/mcp/claude-code.txt` |

**两个必须注意的点**：

1. **`env.PYTHONPATH` 要给** —— `mcp_facade` 是本仓库源码，不在解释器的 site-packages 里。
   缺了会启动失败，而**多数客户端不会报「服务器起不来」**，只会让你看不到工具（静默失败）。
2. **`cwd` 指向 a2a-hub 目录** —— facade 需要能 import 到自己。

### 验证接入成功

```bash
python tests/test_mcp_facade.py
```

它会用官方 MCP 客户端以 stdio 拉起 facade，列出工具并实调两个。

### 前置条件

hub 必须先在跑（它不是常驻服务）：

```bash
python hub.py serve --host 127.0.0.1 --port 9200
```

facade 连不上 hub 时会返回明确的错误提示（含「请先运行 hub.py serve」），不会静默失败。

## 控制台

浏览器打开 `http://127.0.0.1:9200/console`。

**零构建、零前端依赖** —— 单文件 HTML + 原生 fetch。对「可复用开源框架」这个定位很重要：
别人 clone 下来直接能看，不需要 `npm install`。

| 视图 | 内容 |
| --- | --- |
| **概览** | 统计卡片 + 最近 trace + 最近任务 |
| **注册表** | 每个 agent 的 kind / health / tags / endpoint，可一键「探测全部」 |
| **Trace** | trace 列表 → 选中后展示**甘特图 + 完整时间线** |

甘特图按任务真实起止时间绘制，所以**并行段一眼可见** —— 同层的两个 agent 会显示为同一水平区间的两条并排条。

**能力边界**：只读 + 触发健康探测。不做启用/禁用 agent、不取消任务 ——
控制台一旦能写就得配鉴权，那是另一个量级的事。

**配套端点**：

| 端点 | 用途 |
| --- | --- |
| `GET /console` | 控制台页面 |
| `GET /admin/agents` | 注册表快照 |
| `GET /admin/tasks?limit=N` | 最近任务 + 运行中任务 |
| `GET /admin/traces?limit=N` | trace 概览列表 |
| `GET /admin/trace/<id>` | 单个 trace（含人类可读的 `view` 字段） |
| `POST /admin/probe` | 触发健康探测（可传 `{"name":"..."}` 只探一个） |

## 测试

自检脚本按「**是否需要本机环境**」分两组：

```bash
python tests/run_all.py          # 单元级（CI 用这个）
python tests/run_all.py --all    # 单元 + 集成
python tests/run_all.py --list   # 只看分组
```

| 组 | 脚本 | 依赖 |
| --- | --- | --- |
| **单元级** | `test_cancel_semantics` · `test_p2_fixes` · `test_adapter_p2` · `test_isolation` | 内存 Store + 假适配器，**零外部依赖** |
| **集成级** | `test_cli_lifecycle` · `test_kill_tree` · `test_cli_direct` · `test_async_cancel` · `test_p1_fixes` · `test_mcp_facade` | 真实 CLI / Windows 进程命令 / hub 在跑 |

**这条分界线就是「薄核心 / 厚适配器」的边界**：

```
单元级全绿  ⟺  core 确实与平台无关（store · router · orchestrator · hub_app 的逻辑）
集成级      ⟺  适配器与本机工具的对接（只能在开发机上验证）
```

所以 CI 只跑单元级 —— 它同时起到了**架构约束的自动验证器**的作用，
而不只是防回归。见 `.github/workflows/ci.yml`（ubuntu + windows × py3.10 / py3.13）。

## 目录分层：代码 / 运行时 / 本机配置

这个仓库**可以独立使用，也可以直接开源** —— 代码层不含任何本机路径、凭据或运行时数据。

```
a2a-hub/                     ← 代码层（进仓库）
├── core/                    store · registry · router · orchestrator · hub_app · console
├── adapters/                base · a2a_http · cli
├── probes/                  process_monitor（本机进程探测）
├── tests/                   自检脚本（多数可独立运行）
├── examples/agents/         示例配置（占位符，无本机路径）
├── hub.py                   命令行入口
├── pyproject.toml           依赖声明
└── README.md / LICENSE

以下由 .gitignore 排除（运行时层）：
├── data/                    SQLite 数据库
├── workspace/               下游 agent 的工作目录（它们在这里写文件）
├── agents/                  你的本机 agent 配置（含真实路径与命令）
└── __pycache__/
```

**为什么要分**：

- **`workspace/` 必须独立** —— CLI 子进程的 cwd 默认继承 hub 进程，而 hub 通常就跑在代码目录里。
  不给它独立工作目录的话，agent 随手写个文件就落进仓库（实测把 `history-of-computing.md`
  和 `memory/` 写进了项目根）。现在 `Hub` 会给没配 `cwd` 的 CLI 适配器注入 `<cwd>/workspace`。
- **`agents/` 必须独立** —— 里面的配置写死了本机路径（`C:\Users\...`），
  还可能带模型 ID 之类的环境特定值。仓库里只放 `examples/agents/` 的占位符版本。
- **凭据不进文件** —— Qoder 的 PAT 走环境变量 `QODER_PERSONAL_ACCESS_TOKEN`，
  hub 的 token 走 `HUB_TOKEN` 或 `--token`，都不落盘。

**移植到新机器**：`git clone` → `pip install -e .` → 照着 `examples/agents/` 写自己的 `agents/*.json` → 起服务。

## 目录结构

```
a2a-hub/
├── core/                  平台无关，可开源
│   ├── store.py           SQLite 六表 + schema 迁移 + 查询接口
│   ├── registry.py        Agent 注册表 + 健康探测
│   ├── router.py          能力匹配路由
│   └── orchestrator.py    编排引擎（拓扑分层 + 同层并行 + 模板变量）
│   ├── hub_app.py         对外 A2A 服务端（含 RunPlan / GetPlan 编排扩展 + admin 端点）
│   └── console.py         只读控制台（单页 HTML，零前端依赖）
├── adapters/              每个下游一层，可插拔
│   ├── base.py            Adapter 契约 + 下游差异归一化
│   ├── a2a_http.py        通用 HTTP A2A 适配器（裸 JSON-RPC，零 SDK 依赖）
│   └── cli.py             CLI 适配器（claude / qodercli / codex / dsh）
├── agents/                各 CLI agent 的注册配置
├── probes/
│   └── process_monitor.py 本机 AI 工具进程探测（适配器 detect 层的实现）
├── tests/
│   └── mock_agent.py      最小假下游，用于零成本验证链路
├── hub.py                 命令行入口
└── data/hub.db            SQLite（运行时生成）
```

**新增一种执行体 = 新增一个 `Adapter` 子类，core 一行不改。** 这是「厚适配器」的含义。

## 两个必须知道的坑

都来自实测，写适配器时一定会撞上：

1. **本地探测必须 `trust_env=False`**。本机系统代理（`127.0.0.1:7393`）会把 `127.0.0.1` 的请求变成 **502**（不是 connection refused），极易误判成「服务在跑但报错」。
2. **拉 Agent Card 与发 JSON-RPC 不要复用同一个 keep-alive 连接**。部分实现（如 codex-a2a）在同一连接上先 GET 再 POST 会稳定返回 404。适配器里每次请求都用独立的 `AsyncClient`。

## 已验证

### 真实下游（2026-10-06 实测）

| 下游 | 端点 | 结果 |
| --- | --- | --- |
| **dsh-a2a** | `127.0.0.1:9101` | ✅ 端到端 + 跨轮续接（`continuedSession: true`，session 复用） |
| **codex-a2a** | `127.0.0.1:9100` | ✅ 端到端 + 跨轮续接（`codexThreadId` 复用） |
| mock-dsh | `127.0.0.1:9301` | ✅ 测试替身，零成本验证链路 |

**零代码改动接入**：两条真实桥直接注册即可，`a2a_http` 适配器没有为它们写任何特化代码。

### 能力验证

| 项 | 结果 |
| --- | --- |
| 端到端派发 | 客户端 → hub → 路由 → 真实下游 → artifact 回传，`TASK_STATE_COMPLETED` |
| **跨 agent 会话续接** | 同 `contextId` 追问，下游 resume 同一 session 并答对上一轮内容 |
| **重启持久化** | hub 进程 kill 后重启，`GetTask` 仍能查到重启前的任务（含完整 history 与 artifact） |
| 会话映射归一化 | 三种下游命名（`dshSessionId` / `codexThreadId` / `sessionId`）统一提取为 `session_id` |
| 过程回传 | 下游的 status / text / 工具事件映射成消息并持久化 |
| 动态注册 | 服务运行中注册新 agent 即生效，无需重启（适配器按需构建） |

### 接真实下游（实测命令）

```bash
# DSH 桥（工作区外的运行时 Python + PYTHONPATH）
cd /d/DS-harness/.dsh-a2a
export PYTHONPATH="D:/DS-harness/.dsh-a2a/src;D:/DS-harness/.dsh-a2a/.venv/Lib/site-packages;D:/DS-harness/.dsh-a2a/.venv/Lib/site-packages/win32;D:/DS-harness/.dsh-a2a/.venv/Lib/site-packages/win32/lib"
export DSH_MCP_WORKDIR="D:/DS-harness/.dsh-a2a" DSH_MCP_HOST=127.0.0.1 DSH_MCP_PORT=9101 DSH_MCP_PROFILE=headless
"C:/Users/a1299/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe" -m dsh_mcp

# Codex 桥
cd "/c/Users/a1299/Documents/Codex/2026-10-04/codex/outputs/codex-a2a"
export CODEX_A2A_WORKDIR="C:/Users/a1299/Documents/Codex/2026-10-04/codex/work/a2a-demo" CODEX_A2A_HOST=127.0.0.1 CODEX_A2A_PORT=9100 CODEX_A2A_SANDBOX=workspace-write
.venv/Scripts/python.exe -m codex_a2a

# 注册
python hub.py register --name dsh   --endpoint http://127.0.0.1:9101 --tag dsh
python hub.py register --name codex --endpoint http://127.0.0.1:9100 --tag codex
```

## 下一步

- **接真实下游**：把 `dsh-a2a`(:9101) / `codex-a2a`(:9100) 注册进来即可，无需改代码
- **Qoder 适配器**：`qodercli -p -o stream-json`，注意 `-m` 必须传 modelID（UUID），且 stdout 会混非 JSON 文本需按行过滤
- **CLI 类适配器**：`adapters/cli.py`，把 `claude -p` / `codex exec` / `qodercli -p` 包成同一契约
- **编排**：串行链、并行扇出、主管-工人（依赖本内核的持久化）
- **统一审计**：跨 agent 的调用链追踪
