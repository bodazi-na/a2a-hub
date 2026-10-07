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
| **适配器接口新增 `on_event`（可选）** | `Adapter.call()` 多了个关键字参数。**老签名的适配器不会坏** —— hub 探测到不接受就不传，它们照常工作、只是推不了实时事件。见下方说明 | 想用实时流就加上 `on_event=None` 参数并在解析出事件时调用它；不加也能跑 |
| **可导入路径全部变了** | 从 `from core.store import ...` 变成 `from a2a_hub.core.store import ...` | **只有当过库用才需要改**。命令行用法（`a2a-hub` / `python -m a2a_hub` / `python hub.py`）不受影响；MCP 接入的 `-m mcp_facade` 要改成 `-m a2a_hub.mcp_facade`，`PYTHONPATH` 指向 `<仓库根>/src` |

### 重构：src 布局 + 单一顶层包

**解决的问题**：以前 `hub` / `core` / `adapters` / `probes` / `mcp_facade` 是**五个顶层
模块**，装进环境后会占据 site-packages 的顶层命名空间 —— `core` 这种名字几乎必然
和别人撞车。当时的「解法」是让用户装独立虚拟环境，**那是把设计缺陷转嫁成用户的负担**。

现在代码在 `src/a2a_hub/` 下，**装出去只占 `a2a_hub` 一个名字**：

```python
from a2a_hub.core.store import Store
from a2a_hub.adapters.cli import DshCLI
```

顺带拿到的两个好处：

- **`src/` 布局让「import 到工作目录里的同名文件」不可能发生** —— 测试验证的一定是
  真正会被装出去的那份代码
- **`pip install` 之后可以从任何目录用**（`a2a-hub serve` / `python -m a2a_hub serve`），
  不再依赖 cwd

仓库根保留了一个 `hub.py`（**不进安装包**），让文档里到处存在的 `python hub.py serve`
继续可用。它内部把 `src/` 加进 `sys.path` 再转发到 `a2a_hub.cli`。

**验收**（实测，不是推断）：

- 构建 wheel → 顶层条目只有 `a2a_hub`，子包 `core` / `adapters` / `probes` / `mcp_facade`
  都在它底下
- 临时 venv 真实 `pip install` → site-packages 顶层只有 `a2a_hub` + 第三方依赖
- 三个入口全通：`a2a-hub --help` / `python -m a2a_hub --help` / 从无关目录
  `from a2a_hub.core.store import Store`
- 打包 exe 照常构建并启动（26.7 MB / 76 文件，0.87s 就绪）
- 测试：单元 + 平台 **13/13**，含集成 **19/19**

### 新增

**打包成 exe（Windows，免安装）**

- `packaging/hub.spec` + `tools/build_exe.py`：一条命令打成免安装 exe。
  **构建脚本会自动实测启动**并打一次 `/healthz` —— 构建成功不等于能跑
- 目录版与单文件版都支持（`--onefile` / `--both`）
- **`start-hub.bat` 启动器**：双击 exe 是起不来的（不带子命令只打一行用法错误、
  退出码 2），构建时会在 exe 旁边放一个启动器 —— 双击它 = 起服务 + 等就绪 +
  自动开浏览器，服务跑在自己的窗口里，日志可见、Ctrl-C 能停。
  **它会自己找 exe**（脚本旁 → `dist\a2a-hub\` → 上一级的 `dist\`），
  所以源码仓库里的 `packaging\start-hub.bat` 也能直接用；找不到时逐条列出
  查过的路径并给出修法。定位后 `cd` 到 exe 所在目录再启动（`data\` 跟着 exe 走）。
  另加 `.gitattributes` 声明 `*.bat text eol=crlf`（cmd 需要 CRLF）

  这个 `.bat` **刻意写成纯 ASCII**：cmd.exe 按**系统代码页**（中文 Windows 是
  GBK）解析 .bat，带中文的 UTF-8 批处理会乱码，更糟的是截断的多字节序列可能
  把解析器搞坏。开头加 `chcp 65001` 救不了 —— 那时 cmd 已经在读文件了。

过程中修掉三个坑（都会写进代码注释）：

| 坑 | 症状 | 修法 |
| --- | --- | --- |
| 冻结后 `__file__` 指向打包内部 | **onefile 下数据落在临时目录、退出即删**，每次重启从零开始且**静默**丢失 | `sys.frozen` 时改用 exe 所在目录 |
| 冻结后的 exe 不认 `PYTHONUTF8` / `PYTHONIOENCODING` | 开发机正常，用户双击时中文乱码（`(来自 Agent Card)` → `(\xc0\xb4\xd7\xd4 …)`） | `hub.py` 显式把 stdout/stderr 固定成 UTF-8 |
| 两种形态共用一个 workpath | 先建目录版再建单文件版，会复用陈旧中间产物，产出**能构建成功但一跑就报 `Could not create temporary directory!`** 的坏 bundle | 各用各的 workpath |

**并行度分析**

- `core/parallelism.py`：从任务的起止时间算**平均/峰值并行度、空闲率、
  编排开销、利用率时间线**，以及按观测分层的理论最短。纯函数、零依赖，
  可在单元级穷举边界
- trace 接口新增 `parallelism` 块；控制台 Trace 详情里出指标卡 +
  利用率时间线 + 分层表
- **口径写在返回值里**（`layerSource: "observed"`）：分层是从**实际启动时刻**
  聚类推出来的（编排器派发同层是一起起子进程的，实测差 3.5 毫秒），
  理论最短 = 各层「最慢那个」之和。**这是分层的下界，不是严格关键路径** ——
  严格关键路径需要计划里的依赖图，而它没有落库

**实时流**

- `SendStreamingMessage`（含 `message/stream` 别名）：A2A 标准的 SSE 流式。
  载荷是标准 `StreamResponse`（`statusUpdate` / `task`），过程事件的语义放在
  `metadata.kind` —— **不认识我们的客户端也能当进度消息正常显示**
- Agent Card 的 `capabilities.streaming` 由 `false` 改为 **`true`**（以前声明
  false 是因为确实没实现，现在是如实声明）
- 事件时间戳改为**发生时刻**（N5）。以前是 `adapter.call` 跑完后批量落库、
  时间戳统一取插入时刻 —— 实测一个跑了 181 秒的任务，全部过程事件的时间戳
  挤在 64 毫秒之内。**顺序还在、时序全丢**，而控制台把那段渲染成「时间线」，
  属于「声明诚实、呈现误导」
- 支持**接入一个已在跑的任务**（只传 `taskId`）：先回放历史再跟着推
- 背压：订阅队列有界，满了**丢最老的**并计数，`dropped` 随流送出。
  慢客户端只影响自己，**绝不拖住正在跑的任务**
- 心跳（SSE 注释行）：agent 思考几分钟是常态，不发心跳会被中间层掐连接
- 控制台新增**实时**视图（SSE 驱动，事件在发生那一刻出现）

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

**代码审查第二轮（2026-10-07）** —— 针对实时流、并行度与打包三块的复审，
8 条全部核实并修复：

| 级别 | 问题 | 修法 |
| --- | --- | --- |
| 高 | **冻结版的工作区路径不跟随 exe**：`workspace_dir` 默认 `Path.cwd()`，双击 exe 时 cwd 是「当时碰巧在哪」（桌面、`C:\Windows\System32` 都可能），`workspace/` 就散落在那种地方 | 入口显式传 `ROOT/workspace`（`ROOT` 已做 frozen 判断） |
| 高 | **运行中重连补不回尚未落库的进度**：事件只在结算时落一次库，接入时回放的是库里内容，而库里还没有它们 | 订阅时先 flush 待落库缓冲，再回放 |
| 高 | **取消时已推送的事件永久不入 history**：`adapter.call` 从未返回 ⇒ `result.events` 不存在 ⇒ 客户端在流里看到了、审计里查不到 | 取消/异常路径也 flush；用 `_flushed_count` 保证结算时不重复写 |
| 中 | **流式请求的非法 timeout 冒泡成 HTTP 500**：流式方法在 `rpc()` 的 try **之外**，客户端看到「服务器炸了」而真实原因只是参数写错 | 纳入同一套错误处理，以**单帧 SSE 错误**返回（保持流式语义，错误仍可读） |
| 中 | **`started_at` 打在取得并发名额之前**：负载超上限时排队时间被算成「Agent 在跑」，并行度虚高 | 移到取得会话锁与并发名额之后 |
| 中 | **构建脚本的冒烟检查可能无限等待**：`proc.stdout.read()` 在 `terminate()` 之前，进程还活着时会一直等它退出 | 先终止再读，并给等待加显式上界 |
| 低 | **控制台流控制竞态**：旧请求的 `finally` 无条件清 `liveAbort`，快速重派会让新流失去「断开」控制 | 只清理自己那条流（`liveAbort === ctrl`） |
| 低 | **丢事件提示重复显示**：服务端送的是累计 `dropped`，前端判非零就插提示，慢客户端恢复后被同一句刷屏 | 只报**本次新增**的条数 |

**已知局限（不改，但钉住）**：`core/parallelism.py` 的分层靠启动时刻聚类，
间隔小于 50ms 容差的首尾相接任务会被并进同一层，令「理论最短/编排开销」偏小。
**仅凭时间戳区分不了**「同层启动有先后」和「不同层但前一层极短」——
两种情况观测量完全一样。已补边界测试把当前取舍钉住，并在文档里写明。

**后续修正（2026-10-07 实测踩到，两次）**：上面那条「已知局限」在实际使用中
**真的会误判**，而且改了两次才对。

第一次：演示下游（mock）整层几毫秒就跑完 → 2 层的 plan 被判成 1 层。
当时加了一条判据（启动时刻不早于当前层最晚结束时刻），但**保留了原来的阈值判据**。

第二次（连 hub 实测时发现）：**保留阈值判据本身就是错的** —— HTTP 适配器派发
同一层的三个任务间隔约 45ms，第三路离层首已 89ms，于是**真重叠的三路并行被拆成
2+1 层**。

最终定案：**判据只留一条** ——

> 上一批全部跑完之后才开始的那一批，算新的一层。

这是严格分层的**定义**，不依赖任何经验阈值。想通了其实很简单：
**两个时间区间重叠的任务本来就是同一层**，「启动时刻差多少」根本不是这件事的判据。
容差只保留 1ms 用于吸收时间戳抖动，且**不能调大**（它会被拿去和任务时长比较 ——
设成 5ms 时，只跑 5ms 的任务会被自己层里的下一个判成新层，实测判成 4 层）。

回归测试从 52 条扩到 **59 条**，新增：重叠即同层 / 边界取等要分层 / 边界抖动仍分层 /
快下游仍正确分层。端到端实测：3 路并行 + 汇总正确判成 2 层，与 plan 一致。

**第一轮（演练发现）**

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

**启动护栏复审（2026-10-07）**

复审 H1 启动护栏（绑定非回环地址 + 未配认证 ⇒ 拒绝启动），实测 33 项断言。
护栏逻辑本身没问题，但发现**它承诺的那件事没有兑现**：

| 级别 | 问题 | 修法 |
| --- | --- | --- |
| 中 | **安全警告落不进日志**。重定向时 stdout 是**块缓冲**的，启动横幅（含「服务裸露在网络上」那条警告）一行都打不出来。实测 `serve --allow-insecure` 重定向到管道时 `[hub]` 输出 **0 行**，加 `PYTHONUNBUFFERED=1` 才有 6 行。而护栏文案里明写着「③ --allow-insecure 确认要裸奔（**会在日志里留永久警告**）」—— 缓冲不处理，这句就是空的 | `_force_utf8_output` → `_configure_stdio`，在固定 UTF-8 的同时设 `line_buffering=True`。CLI 输出量本来就小，代价可忽略 |

同时把这条护栏固化成回归测试 `tests/test_startup_guard.py`（平台级，30 项断言）——
它是**唯一**阻止 hub 裸奔到网络上的东西，一旦被改坏或绕过，不会有任何功能测试变红，
只会静默地少一层保护。覆盖：回环/非回环分类、拒绝路径（含「拒绝时不得真的绑定端口」）、
`--token ""` 不能当认证、`HUB_TOKEN` 环境变量、`--allow-insecure` 豁免后仍打警告、
重定向时横幅不丢、以及配了认证后认证本身真的拦得住（公开面最小化 + 路径变体 fail closed）。

### 性能

| 项 | 改前 | 改后 |
| --- | --- | --- |
| 50 个过程事件落库的事件循环阻塞 | 192.8 ms | **0.9 ms** |
| 单次 `add_message` | 4.57 ms | ~0.06 ms 量级 |

### 平台接入验证

逐个平台实测「平台 → hub」这条路（不是照着配置推断的），
验证方式：让每个平台列出 hub 上的 agent 名字 —— 那些名字它猜不出来。

| 平台 | 路径 | 结果 |
| --- | --- | --- |
| Claude Code | MCP | ✅ |
| Qoder CLI | MCP | ✅ |
| Codex | skill | ✅ |
| DSH | skill | ✅ |
| WorkBuddy | skill | ✅ |

过程中修正/发现三件事：

- **Qoder 的 CLI 与 IDE 用两套独立的 MCP 配置**。`~/.qoder/mcp.json` 是 IDE 的；
  `qodercli` 有自己的一份（`qodercli mcp list` / `mcp add`）。
  只配 IDE 那份时 CLI 会明确报「没有 MCP 工具可用」。
- **无头模式下 MCP 工具需要显式放开权限**，否则会被权限提示拦下（无人可确认）。
  Qoder CLI 用 `--permission-mode auto`。
- **Codex 实际走的是 skill 而非 MCP** —— 它先试 MCP，然后改用
  `~/.codex/skills/a2a-hub/scripts/hub_client.py`。两条路都通。

### 文档

- `docs/architecture.md` —— 分层、数据流、扩展点、数据库
- `docs/adr/` —— 8 条架构决策记录（含被否决的方案与理由）
- `CONTRIBUTING.md` —— 三档测试、写适配器、提交约定
- `docs/drill-2026-10-06-real-load.md` —— 一次真实负载演练的完整记录
- **`README.en.md` —— 英文版 README**，与中文版内容对齐，两边顶部各有语言切换链接。
  相对链接与文件内锚点都做过校验（GitHub 的锚点规则是**每个空格各换一个连字符**，
  不折叠连续空格 —— 自己写校验脚本时容易在这里写错）

### 已知问题

- 只实现 Windows；非 Windows 下 CLI 适配器未经测试
- **时间戳精度因下游而异**：CLI 类下游走 SSE，时间戳是**真实发生时刻**；
  HTTP 类下游只能轮询，时间戳受轮询间隔限制。**本版本之前跑的历史任务**，
  时间戳是批量写入时刻，不能用来看时序
- `RunPlan` 同步阻塞，长编排需用 `tools/run_plan.py`
- 各节点写盘能力不一致且不可声明
- 清单里仍有 19 条 P2 未处理（索引细节、容错可观测、资源上限、语义边界），**无 P0/P1**
