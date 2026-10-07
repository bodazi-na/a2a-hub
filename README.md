# a2a-hub

多 Agent A2A 协调内核。**薄核心 + 厚适配器**：core 平台无关可开源，adapters 每个工具一层可插拔。

它解决的是「一堆 A2A agent 各自为政」的问题——没有注册中心、没有路由、任务状态重启即失。hub 把这三件事补上，并且**自己也是一个标准的 A2A agent**，任何会说 A2A 的客户端都能调它。

---

> ## 🤖 AI 创作声明
>
> **本仓库的代码与文档由 AI 辅助创作** —— 由多个大模型 agent 生成，
> 人类维护者负责提出目标、拍板取舍与最终验收。
>
> 具体而言：
>
> - **代码 / 测试 / 文档**由 AI 生成，人类审阅并决定方向
> - README 与 `docs/` 里标注「**实测**」的数据，都写明了**验证方式**
>   （怎么复现、看到什么算通过）—— 你可以自己复核
> - `docs/adr/` 里每条决策都写明了**被否决的方案与否决理由**，
>   便于你判断这些取舍是否适用于你的场景
> - `CHANGELOG.md` 的「已知边界」一节如实列出了**没做的事**与**已知缺陷**
>
> **请据此评估**：AI 生成的代码建议自行审阅后再用于生产环境。
> 本仓库的测试分三档（单元 / 平台 / 集成），跑 `python tests/run_all.py`
> 可以自己验证 —— 单元级零外部依赖，clone 下来就能跑。

---

## 它做了什么

| 能力 | 实现 |
| --- | --- |
| **持久化** | SQLite 六表（tasks / messages / artifacts / contexts / agents / schema_version），WAL 模式 |
| **注册与发现** | Agent Card 注册表，落库；健康探测走 HTTP 拉卡片 |
| **路由** | 按 agent 名或能力标签（tag / Agent Card 里的 skills）选节点 |
| **A2A 服务端** | 对外暴露 Agent Card + JSON-RPC（SendMessage / **SendStreamingMessage** / GetTask / ListTasks / CancelTask） |
| **过程回传** | 下游的 thinking / tool_call / tool_result / text 统一映射成消息落库 |
| **实时流** | `SendStreamingMessage`（SSE）；事件在**发生那一刻**推送，时间戳是发生时刻而非落库时刻 |

**核心设计：先落库，再派活。** 任务在任何下游动作之前就已经写进 SQLite，所以进程崩了、重启了，任务状态依然查得到；下游失败也不会丢任务，只会置为 `failed` 并记下原因。

## 快速开始

**不需要装任何真实的 AI CLI。** 仓库自带一个零依赖的演示下游
（`examples/mock_agent.py`，只用 starlette —— 那本来就是 hub 的依赖）。
五分钟内你能看到整条链路跑通。

```bash
# 1) 装（虚拟环境随便建在哪儿）
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

```bash
# 2) 起一个演示下游（另开一个终端）
python examples/mock_agent.py --port 9301 --name demo --tag demo

# 3) 把它注册进来
python hub.py register --name demo --endpoint http://127.0.0.1:9301 --tag demo
python hub.py agents
#   demo             a2a_http   health=unknown  tags=demo

# 4) 起 hub（再开一个终端）
python hub.py serve --host 127.0.0.1 --port 9200
```

发一个任务（标准 A2A JSON-RPC）：

```bash
curl -X POST http://127.0.0.1:9200/ \
  -H "Content-Type: application/json" -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{
        "messageId":"m1","role":"ROLE_USER",
        "parts":[{"text":"你好，hub"}],
        "contextId":"ctx-demo"}}}'
```

你会拿到：

```jsonc
{
  "status": { "state": "TASK_STATE_COMPLETED" },
  "artifacts": [{ "name": "response",
                  "parts": [{ "text": "[demo] echo: 你好，hub" }] }]
}
```

**然后把它杀掉再起来试试** —— 任务还在：

```bash
# Ctrl-C 停掉 hub，重新 python hub.py serve ...
curl -X POST http://127.0.0.1:9200/ -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"GetTask","params":{"id":"<上面那个 taskId>"}}'
```

这就是 hub 存在的理由：**任务在任何下游动作之前就已经落库**。
进程崩了、重启了，状态依然查得到；下游失败也不会丢任务，只会置为 `failed` 并记下原因。

### 接你自己的 agent

演示下游只是让你先看到它工作。接真实工具：

- 用现成的 CLI（claude / codex / dsh / qoder）：见 [适配器](#适配器两类下游) 与
  [注册一个 CLI agent](#注册一个-cli-agent)；`examples/agents/` 下有四个真实配置样例
- 下游已经是一个 A2A 服务：直接 `hub.py register --endpoint <url>` 即可
- 都不是：照着 [写 CLI 适配器必踩的三个坑](#写-cli-适配器必踩的三个坑都实测过)
  和 `examples/mock_agent.py` 自己写一层

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

## 怎么选接入方式：CLI 优先，桥为备选

同一个 agent 往往两条路都能走（DSH 既有 `dsh-a2a` 桥，也有 `dsh` CLI）。**优先选 CLI**：

| | CLI 类 | HTTP 类（桥） |
| --- | --- | --- |
| 常驻进程 | **零** | 每个 agent 一个 |
| 部署 | 直接可用 | 要先起桥 |
| 跨机器 | ❌ 只能本机 | ✅ |
| 给别的进程用 | ❌ | ✅ |

**单机自用 → CLI；要对外暴露或跨机器 → 桥。**

本项目的实际拓扑就是这么定的：`claude-cli` / `codex-cli` / `dsh-cli` / `qoder-cli` 四个 CLI 节点，
两条 HTTP 桥（9100 / 9101）**已退役**——CLI 已能直连，没必要多养两个常驻进程。

`A2AHttpAdapter` 的代码保留并继续维护，因为跨机器场景离不开它。

## 适配器：两类下游

| 类型 | 类 | 对接对象 | 需要常驻进程 |
| --- | --- | --- | --- |
| **HTTP** | `A2AHttpAdapter` | 已经在跑的 A2A 服务 | 是 |
| **CLI** | `ClaudeCLI` / `QoderCLI` / `CodexCLI` / `DshCLI` | 直接起子进程调 CLI 本体 | 否 |

CLI 类少一层桥、少一个常驻进程，代价是自己管子进程生命周期、超时、取消。
**单机自用优先 CLI；跨机器或对外暴露才起桥。**

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

`examples/agents/` 下有四个真实 CLI 的样例可参考。

> **凭据不要写进配置文件。** 适配器的 `env` 会继承 hub 进程的环境变量，
> 启动时注入即可：`export QODER_PERSONAL_ACCESS_TOKEN=... && python hub.py serve`。

**想自己写一层适配器？** 见
[`docs/architecture.md`](docs/architecture.md#写-cli-适配器必踩的坑全部实测过) ——
里面有完整的 `Adapter` 契约、下游差异归一化表，以及五个必踩的坑（全部实测过）。

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

**⚠️ 依赖必须显式表达 —— 自然语言不算。**

hub 只从 `{{steps.<id>}}` 模板变量推断依赖。prompt 里写「先做 A 再做 B」这种
**自然语言描述不会被识别** —— 那两步会被判为无依赖，**同层并行跑**。

两条正确写法，二选一：

```jsonc
// ① 引用上一步的输出（推荐，最自然）
{"id": "b", "prompt": "审阅这段：\n{{steps.a}}"}

// ② 显式声明依赖（用于「有先后顺序但不消费输出」）
{"id": "b", "dependsOn": ["a"], "prompt": "把产物复制一份到 backup/"}
```

漏写依赖的症状：并行执行、模板变量取不到值、或者拿到未完成的上游结果。

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

| 平台 | 配置方式 | 示例 |
| --- | --- | --- |
| WorkBuddy | `~/.workbuddy-ai/mcp.json` | `examples/mcp/workbuddy.json` |
| Codex | `~/.codex/config.toml` | `examples/mcp/codex.toml` |
| Qoder | **CLI 用 `qodercli mcp add`**；IDE 才用 `~/.qoder/mcp.json` | `examples/mcp/qoder.json` |
| Claude Code | `claude mcp add ...` | `examples/mcp/claude-code.txt` |

**三个必须注意的点**：

1. **`env.PYTHONPATH` 要给** —— `mcp_facade` 是本仓库源码，不在解释器的 site-packages 里。
   缺了会启动失败，而**多数客户端不会报「服务器起不来」**，只会让你看不到工具（静默失败）。
2. **`cwd` 指向 a2a-hub 目录** —— facade 需要能 import 到自己。
3. **无头模式下要放开工具权限**。MCP 工具调用通常会弹权限确认，而无人值守时
   没人能点「允许」，于是工具被拦下、任务拿不到数据。实测：

   | 平台 | 需要加 |
   | --- | --- |
   | Qoder CLI | `--permission-mode auto`（或 `--allowed-mcp-server-names a2a-hub`，视版本而定） |
   | Claude Code | `--allowedTools` / settings 里的 allowlist |
   | Codex | 见它自己的 sandbox / approval 配置 |

   **注意 Qoder 的 CLI 与 IDE 用的是两套 MCP 配置**：`~/.qoder/mcp.json` 是 IDE 的，
   CLI 有自己的一份（用 `qodercli mcp list` 查看、`qodercli mcp add` 添加）。
   只配了 IDE 那份的话，`qodercli -p` 会告诉你「没有 MCP 工具可用」。

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

## 实时流（SSE）

hub 实现了 A2A 的 `SendStreamingMessage`，Agent Card 如实声明
`capabilities.streaming = true`。

```bash
curl -N -X POST http://127.0.0.1:9200/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendStreamingMessage",
       "params":{"agent":"dsh-cli",
                 "message":{"messageId":"m1","role":"ROLE_USER",
                            "parts":[{"text":"帮我查一下 xxx"}]}}}'
```

事件体是**标准 A2A `StreamResponse`**（`statusUpdate` / `task`），所以纯 A2A
客户端不需要认识任何扩展就能解析：

```
data: {"jsonrpc":"2.0","id":"1","result":{"statusUpdate":{...状态=working...}}}
data: {"jsonrpc":"2.0","id":"1","result":{"statusUpdate":{...一条过程事件...}}}
data: {"jsonrpc":"2.0","id":"1","result":{"task":{...终态 + 产物...}}}
```

**为什么值得单独说**：过程事件以前是 `adapter.call` **跑完之后**才批量落库的，
时间戳全是插入时刻 —— 实测一个跑了 181 秒的任务，它全部过程事件的时间戳挤在
64 毫秒之内。控制台把那段渲染成「时间线」，**声明诚实、呈现误导**。
现在事件在**发生那一刻**就推出去，时间戳也是发生时刻，时序是真的。

三个设计取舍：

| 取舍 | 为什么 |
| --- | --- |
| 载荷用标准 `StreamResponse`，过程事件的语义放进 `metadata.kind` | A2A 没有「thinking / tool_call」这类类型，最贴近的载体是 `status.message`。这样**不认识我们的客户端也能当进度消息正常显示** |
| 订阅队列**有界**，满了丢最老的并计数 | 慢客户端（浏览器卡住）绝不能反过来拖住正在跑的任务。丢了多少会随流送出去，客户端能标出断点而不是被无声骗过 |
| 心跳用 SSE 注释行（`: keep-alive`） | agent 想几分钟是常态，不发心跳会被中间层掐连接。注释行不污染数据 |

带 `taskId` 而不带 `message` 时，是**接入一个已在跑的任务**：先回放历史，
再跟着推。控制台刷新页面后重新接上靠的就是这个。

## 控制台

浏览器打开 `http://127.0.0.1:9200/console`。

**零构建、零前端依赖** —— 单文件 HTML + 原生 fetch。对「可复用开源框架」这个定位很重要：
别人 clone 下来直接能看，不需要 `npm install`。

| 视图 | 内容 |
| --- | --- |
| **实时** | 选一个 agent、输入任务，**边跑边看**每个动作。SSE 驱动，事件在发生那一刻出现 |
| **概览** | 统计卡片 + 最近 trace + 最近任务 |
| **注册表** | 每个 agent 的 kind / health / tags / endpoint，可一键「探测全部」 |
| **Trace** | trace 列表 → 选中后展示**甘特图 + 完整时间线** |

甘特图按任务真实起止时间绘制，所以**并行段一眼可见** —— 同层的两个 agent 会显示为同一水平区间的两条并排条。

「实时」页用 `fetch` + `ReadableStream` 读 SSE 而**不是** `EventSource`：
后者只能发 GET，带不了 message 体。

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

```bash
python tests/run_all.py          # 单元级 + 平台级（CI 跑这个，零外部依赖）
python tests/run_all.py --all    # 再加集成级（需真实 CLI / hub 在跑）
python tests/run_all.py --list   # 只看分组
```

三档的分界线**就是「薄核心 / 厚适配器」的边界**：
单元级全绿 ⟺ core 确实与平台无关。详见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 目录结构

仓库可以独立使用、也可以直接开源 —— 代码层不含任何本机路径、凭据或运行时数据。
下面标了「**运行时**」的几处由 `.gitignore` 排除；为什么要这么分、移植到新机器
怎么做，见 [`docs/architecture.md`](docs/architecture.md#目录分层代码--运行时--本机配置)。

```
a2a-hub/
├── core/                  平台无关，可开源
│   ├── store.py           SQLite 六表 + schema 迁移 + 事务边界 + 查询接口
│   ├── registry.py        Agent 注册表 + 健康探测
│   ├── router.py          能力匹配路由
│   ├── orchestrator.py    编排引擎（拓扑分层 + 同层并行 + 模板变量）
│   ├── hub_app.py         对外 A2A 服务端（含 RunPlan / GetPlan 编排扩展 + admin 端点）
│   └── console.py         只读控制台（单页 HTML，零前端依赖）
├── adapters/              每个下游一层，可插拔
│   ├── base.py            Adapter 契约 + 下游差异归一化
│   ├── a2a_http.py        通用 HTTP A2A 适配器（裸 JSON-RPC，零 SDK 依赖）
│   └── cli.py             CLI 适配器（claude / qodercli / codex / dsh）
├── mcp_facade/            **可选组件**：把 hub 包成 MCP server，让支持 MCP 的平台能调它
├── examples/
│   ├── mock_agent.py      零依赖演示下游（快速开始用的就是它）
│   ├── agents/            四个真实 CLI 的注册配置样例
│   ├── mcp/               各平台的 MCP 接入配置样例
│   └── plan-*.json        编排定义样例
├── docs/
│   ├── architecture.md    架构说明（分层 / 数据流 / 扩展点）
│   └── adr/               架构决策记录（为什么这么选）
├── tools/
│   └── run_plan.py        长编排提交器（客户端超时可配、结果落盘）
├── probes/
│   └── process_monitor.py 本机 AI 工具进程探测（适配器 detect 层的实现）
├── tests/                 三档自检：单元 / 平台 / 集成（见 CONTRIBUTING.md）
├── hub.py                 命令行入口
├── agents/                本机 agent 注册配置（**运行时**，不进仓库）
└── data/hub.db            SQLite（**运行时**生成，不进仓库）
```

**新增一种执行体 = 新增一个 `Adapter` 子类，core 一行不改。** 这是「厚适配器」的含义。

## 两个必须知道的坑

都来自实测，**命令行验证和写适配器时都会撞上**：

1. **本地探测必须绕开系统代理**。机器上若有系统代理，`127.0.0.1` 的请求会被拦下
   并返回 **502**（不是 connection refused），极易误判成「服务在跑但报错」。
   命令行加 `--noproxy '*'`，代码里用 `trust_env=False`。
2. **拉 Agent Card 与发 JSON-RPC 不要复用同一个 keep-alive 连接**。
   部分 A2A 实现在同一连接上先 GET 再 POST 会稳定返回 404。

## 已验证

### 真实下游（2026-10-06 实测）

hub 侧走 **CLI 直连**（直接起子进程，零常驻进程）：

| 下游 | 启动方式 | 结果 |
| --- | --- | --- |
| claude-cli | `claude -p --verbose --output-format stream-json` | ✅ 单任务 + 编排 |
| codex-cli | `codex exec --json --skip-git-repo-check` | ✅ 单任务 + 编排 |
| dsh-cli | `dsh --profile headless --json` | ✅ 单任务 + 编排 |
| qoder-cli | `qodercli -p -m <modelID> -o stream-json` | ✅ 单任务 + 编排（走 BYOK，不消耗 Qoder 额度） |
| demo | `examples/mock_agent.py` | ✅ 零依赖演示下游 |

四个节点曾用 **4 步 2 层**的真实编排一起跑过（三路并行审查 + 汇总），
完整记录见 [`docs/drill-2026-10-06-real-load.md`](docs/drill-2026-10-06-real-load.md)。

> 早期还有两条常驻 HTTP 桥（`dsh-a2a` / `codex-a2a`），已退役 ——
> 单机自用走 CLI 更简单，桥留给「跨机器 / 对外暴露」场景。
> `A2AHttpAdapter` 仍然保留，任何符合 A2A 的服务注册即可用。

### 平台调用（2026-10-06 实测）

「各个 Agent 平台能不能调用 hub」——逐个实测，不是照着配置推的：

| 平台 | 路径 | 结果 |
| --- | --- | --- |
| **Claude Code** | MCP（`claude mcp add`） | ✅ 正确列出四个节点 |
| **Qoder CLI** | MCP（`qodercli mcp add` + `--permission-mode auto`） | ✅ 正确列出四个节点 |
| **Codex** | skill（`~/.codex/skills/a2a-hub`） | ✅ 正确列出四个节点 |
| **DSH** | skill（`~/.dsh/skills/a2a-hub`） | ✅ 正确列出四个节点 |
| **WorkBuddy** | skill（`~/.workbuddy-ai/skills/a2a-hub`） | ✅ 正确列出四个节点 |

验证方式：让每个平台「列出 hub 上注册的所有 agent 名字」—— 那些名字它猜不出来，
答对即证明工具/命令**真的被执行了**。四个平台都返回了
`claude-cli, codex-cli, dsh-cli, qoder-cli`。

**两点与预期不同，值得记**：

- **Codex 实际走的是 skill 而不是 MCP**：它先试了 MCP，然后改用
  `~/.codex/skills/a2a-hub/scripts/hub_client.py`。两条路都通，但它选了后者。
- **Qoder 的 CLI 与 IDE 用两套 MCP 配置**：`~/.qoder/mcp.json` 是 IDE 的，
  `qodercli` 有自己的一份（`qodercli mcp list` 查看）。只配 IDE 那份时，
  CLI 会明确告诉你「没有 MCP 工具可用」—— 这个失败是**可见的**，不是静默的。

### 能力验证

| 项 | 结果 |
| --- | --- |
| 端到端派发 | 客户端 → hub → 路由 → 真实下游 → artifact 回传，`TASK_STATE_COMPLETED` |
| **跨 agent 会话续接** | 同 `contextId` 追问，下游 resume 同一 session 并答对上一轮内容 |
| **重启持久化** | hub 进程 kill 后重启，`GetTask` 仍能查到重启前的任务（含完整 history 与 artifact） |
| 会话映射归一化 | 三种下游命名（`dshSessionId` / `codexThreadId` / `sessionId`）统一提取为 `session_id` |
| 过程回传 | 下游的 status / text / 工具事件映射成消息并持久化 |
| 动态注册 | 服务运行中注册新 agent 即生效，无需重启（适配器按需构建） |

### 编排与一致性（实测）

| 项 | 结果 |
| --- | --- |
| **实时流** | 用真实 CLI 下游（dsh）实测：**35 帧、跨度 17.90 秒，帧是分散到达的**。关键证据是帧到达时刻与事件发生时刻几乎重合（到达 13.58s ↔ 事件 ts 40.792；到达 15.00s ↔ ts 42.208，间隔都是 ~1.42 秒）—— 说明延迟极小，确实是「发生即推送」。修复前这一整段会在结束时**一次性涌到** |
| **事件时间戳** | 落库的 `created_at` 就是事件发生时刻（不再是一批挤在几毫秒内） |
| **背压** | 订阅者全程不读，任务照常跑完 —— 慢客户端只影响自己 |
| **兼容性** | 老签名适配器（`call` 无 `on_event`）两条路径都正常，不会被流式改动弄挂 |
| 并行扇出 | 三路 agent 的 `startedAt` 相差 **3.5 毫秒**（真并行，非伪并行） |
| **四节点编排** | 4 步 2 层（三路并行读文件 + 汇总），四个 CLI 节点全部参与，61 秒完成；三个并行步的答案与源码逐字一致（证明它们真的读了文件，不是编的） |
| **模板变量传参** | 汇总步正确拿到三份上游输出并合并 —— `{{steps.x}}` 的数据流通了 |
| **时长语义** | `a`=14.7s / `b`=21.4s / `c`=6.8s（并行取最大）+ `merge`=39.7s ≈ 总时长 61.1s。汇总步报的是**它自己的** 39.7s，不是整条的 61s |
| 取消编排 step | `CancelTask` 真的中断下游子进程树；plan 收尾为 `ok=False` 并正常返回 |
| 失败策略 | 默认 fail-fast；`onError: continue` 可按步覆盖 |
| 一致性 | 读用快照事务、写用原子合并；跨语句撕裂读有回归测试兜住 |
| 性能 | 50 个过程事件落库的事件循环阻塞：**193ms → 0.9ms** |

### 三档自检（当前全绿）

```bash
python tests/run_all.py          # 单元级 + 平台级（CI 跑这个）
python tests/run_all.py --all    # 再加集成级（需真实 CLI / hub 在跑）
```

| 档 | 数量 | 依赖 |
| --- | --- | --- |
| 单元级 | 11 套 | 无（假适配器 + 内存 Store） |
| 平台级 | 1 套 | 会起真实进程，但只用 `sys.executable`；非 Windows 自动跳过 |
| 集成级 | 6 套 | 真实 CLI / Windows 进程命令 / hub 在跑 |

## 文档

| 文件 | 内容 |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | 分层、数据流、扩展点 —— **想改代码先看这个** |
| [`docs/adr/`](docs/adr/) | 架构决策记录：为什么只做 A2A、为什么 CLI 优先、为什么不用线程、为什么用 Job Object…… |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 怎么跑测试、怎么写新适配器、提交约定 |
| [`CHANGELOG.md`](CHANGELOG.md) | 变更历史（含 schema 迁移说明） |
| [`docs/drill-2026-10-06-real-load.md`](docs/drill-2026-10-06-real-load.md) | 一次真实负载演练的完整记录（用它自己审自己，暴露并修掉 2 个 P0） |

## 已知边界

诚实地列出来，免得你踩了才发现：

- **只实现 Windows**。接口是平台无关的，但 Job Object、`cmd.exe` 包裹、
  `taskkill` 兜底这些只在 Windows 上验证过。非 Windows 下 CLI 适配器未经测试。
- **流式已实现，但下游差异会体现在时间戳精度上**。CLI 类下游走
  `SendStreamingMessage`（SSE）时，事件时间戳是**真实发生时刻**；
  HTTP 类下游走轮询，时间戳只能取「这一轮拉到的时刻」，精度受轮询间隔限制。
  历史任务（本版本之前跑的）时间戳是批量写入时刻，**不能用来看时序**。
- **`RunPlan` 是同步阻塞的**。长编排会超过客户端超时，届时你拿不到结果也拿不到
  `planId`。用 `tools/run_plan.py`（超时可配 + 结果落盘）。
- **各节点写盘能力不一致且不可声明**。codex-cli 是沙箱只读、qoder-cli 会等人
  点授权（hub 起的子进程没有 TTY），claude-cli / dsh-cli 可写。需要产出文件时
  别指望前两个。
- **`hub → WorkBuddy` 这条没打通**，是设计边界不是缺陷：对方只暴露工具集、
  没有任务级入口。反方向（WorkBuddy → hub）是通的。
- **它是个服务，不是一个 import 进去用的库**。发行名叫 `a2a-hub`，可导入的顶层模块是
  `hub` / `core` / `adapters` / `probes` / `mcp_facade`。`core` 和 `adapters`
  这种通用名会占据 site-packages 的顶层命名空间 —— 如果你要把它当库用，
  建议装进独立的虚拟环境（README 的快速开始就是这么做的）。
- **尚未做**：CI 只跑单元级 + 平台级；无 PyPI 发布；无 Docker。
