# 真实负载演练报告 —— 用 hub 审 hub

> 日期：2026-10-06
> planId：`plan-b312f5a5aa99` ｜ traceId：`trace-96da377531b0`
> 编排文件：`examples/plan-code-review.json` ｜ 提交器：`tools/run_plan.py`
> 产出：`workspace/review/FINAL.md`（24 KB）

## 一、为什么做这次演练

此前对编排的验证都停留在 echo 级（「回复 OK」「用一句话说明幂等」）。
这类任务跑得通只能证明**接口通**，不能证明**系统在真实负载下站得住**。
本次刻意选了三个压力维度：

| 维度 | 具体压力 |
| --- | --- |
| 大输入 | 1082 行的 `hub_app.py`，6 个源文件共 126 KB |
| 真并行 | 三路 agent 同时起子进程，共抢一份 workspace |
| 长任务 | 单步 3 分钟量级，必然撞上客户端超时与层间等待 |
| 真产出 | 不是「回复 OK」，而是要求交付可执行的整改清单 |

任务选「审自己」而不是随便找个外部题目，是因为**代码审查是唯一能立刻验证对错的产出** ——
审查员说的每一处问题，我都可以直接去源码里核对。

## 二、编排设计

```
L0  conc     codex-cli    并发控制与取消语义     hub_app.py + orchestrator.py
    persist  qoder-cli    SQLite 持久化与事务    store.py + hub_app.py
    subproc  claude-cli   Windows 子进程管理     cli.py + a2a_http.py + base.py
L1  merge    dsh-cli      去重合并成整改清单
```

依赖不手写，靠 `{{steps.x}}` 引用自动推断。干跑确认分层为 `[['conc','persist','subproc'], ['merge']]`。

## 三、跑通了什么（正面结论）

1. **三路真并行已实证**。三个 step 的 `startedAt` 相差 3.5 毫秒
   （`06:09:45.005406` / `.008237` / `.011686`），不是伪并行。
2. **126 KB 输入无压力**。三个 agent 都正确读到了 staged 文件，
   codex 甚至准确引用了 `orchestrator.py` 的最后一行原文。
3. **汇总步真的在按指令执行**，没有敷衍：
   - 识别出 4 组「多源命中」（同一处被两人独立提到）并单独置顶
   - 遇到两名审查员给出**互斥修法**时，单列「存疑项」而**不自己拍板**
   - 明确声明「剔除噪声 0 条」「未新增问题」，并给出可核对的统计
4. **整条 plan 可审计重建**。`/admin/trace/<traceId>` 一次拿全：4 个任务、
   各步耗时、涉及 agent、归一化用量（input 443,955 / output 58,421 / total 502,376）。

产出质量：42 条原始发现 → 去重后 38 条（**2×P0、11×P1、25×P2**）。

## 四、演练暴露的问题

### N1 [P0] 编排路径的取消是假成功 —— 下游仍在跑，结果被丢弃

**发现方式**：codex-cli 在 step `conc` 里独立报出，我另行复核源码确认。
两个独立来源命中同一处，可信度高。

**位置**：`core/hub_app.py:642`（`dispatch_task` 内）

```python
await self._execute(task_id, record, adapter, prompt, context_id,
                    session_id, timeout)      # ← 直接 await，从不登记 self._running
```

而 `_send_message` 在 557 / 565 行都做了登记：

```python
bg = asyncio.create_task(self._execute(*args))
self._running[task_id] = bg
bg.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
```

**后果链**（三处全部核对过）：

1. `_cancel_task`（`hub_app.py:903`）查 `self._running.get(task_id)` → 必然 `None`
2. 落到「兜底」分支（`hub_app.py:916-921`），只把数据库状态写成 `canceled`，
   错误文案还写着 `canceled: no active worker` —— **与实际相反，worker 正在跑**
3. 子进程继续烧额度、继续往 workspace 写文件
4. `_execute` 收尾时 `update_task(state="completed", only_from=ACTIVE_STATES)`
   因状态已是 `canceled` 而 no-op，**但第 719 行的 `add_artifact` 是无条件执行的**

**可检验的签名**：一条 `state=canceled` 却带着完整 `response` artifact 的任务记录。

**为什么以前发现不了**：echo 级任务 2 秒跑完，来不及取消。

**修复方向**：让 `dispatch_task` 复用 `_send_message` 的登记逻辑，再 `await asyncio.shield(bg)`。

---

### N2 [P0] 编排任务的 `durationMs` 从占位任务创建时刻起算 —— 甘特图是错的

**发现方式**：审计视图里 `merge` 的 `durationMs` = 335040ms，与整条 plan 的
`elapsed` 335050ms 几乎相同。但 merge 在第二层，实际只跑了约 133 秒。

**直接查库取证**：

| step | agent | created_at | finished_at |
| --- | --- | --- | --- |
| conc | codex-cli | 06:09:45.005406 | 06:12:47.393989 |
| persist | qoder-cli | 06:09:45.008237 | 06:12:46.390019 |
| subproc | claude-cli | 06:09:45.011686 | 06:13:07.672053 |
| merge | dsh-cli | **06:09:45.015073** | 06:15:20.055978 |

四个占位任务的 `created_at` 全落在 **10 毫秒之内** —— 也就是 plan 开始那一刻
（A2A-07「先为全部 step 落占位任务」的必然结果）。而
`durationMs = _duration_ms(t["created_at"], t["finished_at"])`（`hub_app.py:396`），
于是 **`durationMs` 把层间排队等待时间算进了执行时长**。

对第一层恰好接近真实（它立刻就开始跑），对后续层严重虚高：merge 虚高 2.5 倍。

**连带影响**：控制台甘特图（`core/console.py:246-247`）用

```javascript
const start = (new Date(t.startedAt).getTime() - t0) / span * 100;
const width = (t.durationMs || 0) / span * 100;
```

画条 → **四根条全部从 t=0 起画、等长**，两层结构完全看不出来。
这是控制台的核心可视化，且同样只在真实负载下才看得出来（echo 级 plan 太快，肉眼分辨不了）。

**修复方向**：区分「任务创建时刻」与「实际开始执行时刻」，
新增 `started_at` 列在 `_execute` 取到信号量之后写入，`durationMs` 改用它。

---

### N3 [P1] 节点写能力不一致，且 hub 既不能声明也不需要知道

同一个 plan、同一份 workspace、同一套提示词，四个节点的写入结果：

| 节点 | 能否写盘 | 阻塞原因（实测） |
| --- | --- | --- |
| codex-cli | ❌ | 沙箱 `read-only` + 审批策略 `never` |
| qoder-cli | ❌ | **交互式授权提示**，无人应答 |
| claude-cli | ✅ | 写出 `subproc.md`（2881 B） |
| dsh-cli | ✅ | 写出 `FINAL.md`（24200 B） |

**两个节点的失败原因不同**，这比「都不能写」更值得注意：

- codex 是**策略拒绝** —— 适配器 `build_argv` 只传 `exec --json --skip-git-repo-check`，
  不传 `--sandbox` 也不传审批参数，于是走非交互默认值。写能力完全由用户本地
  `~/.codex/config.toml` 决定，**hub 不可控**。
- qoder 是**交互式授权** —— 审计时间线里留下了原始错误：
  `Error: Allow writing to D:\A2A_Engineering\a2a-hub\workspace\review\persist.md?`
  它是在**等人点确认**，而 hub 起的子进程没有 TTY，永远等不到。

**平台层缺口**：`Step` 无法表达「这一步需要写盘」，hub 也无从得知哪个节点能写。
想让整条流水线稳定产出文件，只能靠调用方自己记住「哪个 agent 能写」。

**顺带记录**：qoder 尝试调用 Git Bash 时撞上本机的 safe-delete 钩子
（`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`），它自己改用 Glob 绕过了。
说明**下游 agent 的可用工具面会被宿主机策略改变**，编排层无法预期。

---

### N4 [P1] `RunPlan` 同步阻塞、无任务句柄，客户端超时即结果丢失

`RunPlan` 在 RPC handler 里 `await` 完整条 plan（`hub_app.py:771`），
请求连接要一直挂着。本次 335 秒，而现成的
`skills/a2a-hub/scripts/hub_client.py` 里 RPC 超时写死 900 秒。

后果：**一旦 plan 超过客户端超时，调用方既拿不到结果，也拿不到 planId** ——
planId 是服务端生成的（`f"plan-{uuid4().hex[:12]}"`），只出现在响应里。
调用方只能去翻 `/admin/tasks` 反查，或直接查数据库。

本次我被迫另写了一个 `tools/run_plan.py`，仅仅为了把超时做成参数并落盘结果。

**修复方向**：`RunPlan` 支持异步模式（立即返回 `planId`），调用方用 `GetPlan` 轮询。

---

### N5 [P2] 「过程回传」是事后批量写入，时间线没有时序信息

`_execute` 是 `await adapter.call(...)` **跑完之后**，才在 709-715 行循环把
`result.events` 写进库。所以 tool_call / tool_result / thinking 的
`created_at` 是**落库时刻**，不是事件发生时刻。

实测证据：qoder-cli 实际运行 181 秒，但它的**全部**过程事件时间戳挤在
`06:12:46.286` 到 `06:12:46.350` 这 64 毫秒之内。

**这不算撒谎** —— Agent Card 明确声明了 `"streaming": false`。
但控制台把这段数据渲染成「timeline」，读者会自然理解为时序，
而它实际只是「同一批次内的插入顺序」。**声明诚实，呈现误导。**

**修复方向**：要么实现流式（SSE），要么在控制台明确标注「过程回传为事后补写，非实时」。

## 五、审查清单本身的可信度验证

LLM 审查的价值取决于「说的是不是真的」。我抽查了 **6 条实质结论**，全部到源码里核对：

| 条目 | 结论 | 源码核对 |
| --- | --- | --- |
| P0-1 `dispatch_task` 不登记 `_running` | ✅ 属实 | `hub_app.py:642` 直调 `_execute` |
| M1 `add_message` 异常回滚不全 | ✅ 属实 | 只捕 `IntegrityError`/`OperationalError`；`_dumps` 无 `default=str` |
| P1-1 子步骤取消被降级 | ⚠️ **代码属实、后果判断错误** | 见下方更正 |
| P2-1 缺省 `plan_id` 用 `abs(hash(...))` | ✅ 属实 | `orchestrator.py:206` |
| P2-2 `parse_timeout` 重复调用 | ✅ 属实 | `hub_app.py:509` 与 `546` |
| P2-3 复用占位任务时 `None` 覆盖 metadata | ✅ 属实 | `hub_app.py:602-607` |

**实质结论 5/6 属实，1 条后果判断错误。**

### 更正：P1-1 的后果分析不成立

审查员说的代码事实是对的（`orchestrator.py:262` 的 `isinstance(outcome, BaseException)`
确实会接住 `CancelledError`），但结论「取消被改写成这一步失败、调用方拿到正常
`PlanResult` 而不是取消」**不成立** —— 它混淆了两种取消。

用实验钉死（`tests/test_plan_cancel_and_timing.py` 的 `test_gather_cancel_semantics`）：

| 场景 | `gather(return_exceptions=True)` 的行为 |
| --- | --- |
| 调用方取消整轮 | **直接抛 `CancelledError`** —— 取消本来就正确传播，根本没被吞 |
| 只有单个 step 被取消 | 正常返回 `[CancelledError, 'str']` —— 取消作为**结果**出现 |

所以调用方断开那条路不经过这个分支；而单个 step 被取消时，
「作为该 step 的结果、交给 `on_error` 决定 plan 是否继续」**恰恰是正确行为**。

**按原建议改成 re-raise 反而有害** —— 会让单个 step 的取消变成异常炸穿整条 plan，
而正确行为是产出一个干净的 `PlanResult(ok=False)`。
修复时改为显式转换并写明原因（错误文案 `canceled: 该 step 被显式取消`）。

**这条值得单独记下来**：LLM 审查可能「代码事实正确、因果链错误」。
只核对「它说的代码是不是那样」不够，还要核对「由此推出的后果是否真的成立」。

### 行号不可用

| 条目 | 清单标注 | 实际行号 |
| --- | --- | --- |
| `dispatch_task` | 532-594 | **574** |
| `_cancel_task` | 838-847 | **892** |
| `_execute` | 615-627 | **646** |

偏差 31 / 42 / 54，不固定 —— 是估算而非抄录。
（staged 文件与源文件经 `diff` 确认完全一致，1082 行，不存在「读错文件」的可能。）

**结论：这份清单实质可信（但需逐条验证后果），且引用前必须重新定位行号。**

## 六、结论与建议

**平台骨架在真实负载下站住了**：真并行、大输入、跨节点归一化、审计重建，
这些核心能力都通过了检验。

但暴露了两个此前无法发现的 P0：

- **N1 取消语义在编排路径上失效** —— 这是「假成功」，比直接失败更危险，
  因为调用方会以为已经停掉了
- **N2 计时基准错了** —— 直接影响控制台的核心可视化

两者的共同点值得记下来：**它们都只在「任务足够长、有层间等待」时才显形**。
echo 级验证永远碰不到。这就是真实负载演练不可替代的原因。

建议处理顺序：

1. N1（P0，修复方向明确，改动小）
2. N2（P0，需加 `started_at` 列，涉及 schema 迁移）
3. N3（P1，需要先定设计：`Step` 是否要能声明写需求）
4. N4（P1，异步 `RunPlan`）
5. N5（P2，或在控制台加一行免责说明即可）

---

## 七、修复记录（2026-10-06 下午）

### 已修：N1、N2，外加两个同源缺口

只修 N1 是不够的 —— `_running` 登记只解决 hub 层，编排层还有两处会让
「取消」失效，不一起修的话「取消已修复」是句假话。

| 编号 | 改动 | 文件 |
| --- | --- | --- |
| **N1** | `dispatch_task` 改为 `create_task` + 登记 `_running` + done_callback，并 `await`；调用方取消时把取消转嫁给该 step 并等它结算 | `core/hub_app.py` |
| **N2** | 新增 `started_at` 列（`created_at` = 记录诞生，`started_at` = 真正开始执行）；`_execute` 进入时打点；`_duration_ms` 与两处视图改用 `_task_started_at()`；schema v1 → v2，老库回填 | `core/store.py`、`core/hub_app.py` |
| **P1-5** | `Orchestrator.run` 的逐层循环包进 `try/finally`，新增 `_settle_unexecuted()`，任何路径（含被取消）都结算未执行的 step | `core/orchestrator.py` |
| **P1-1** | **不采纳原建议**。改为显式转换并写明判定依据（见第五节更正） | `core/orchestrator.py` |

新增回归：`tests/test_plan_cancel_and_timing.py`（已进单元级分组，CI 可跑）。

### 第二批：状态机与持久化的完整性（N1 的延续）

只修 N1 还不够 —— `_execute` 那一段还有几个洞会让「取消」和「落库」**同时**失效。
主题是同一个：**状态归属必须由 `only_from` 裁决，任何路径都要落终态**。

| 编号 | 问题 | 改动 |
| --- | --- | --- |
| **M3** | `only_from` 只写 `("submitted",)`，而异步路径早已把状态置成 `working`，这个更新必然 no-op 且返回值无人检查 → 落库 working 到取锁之间到达的取消拦不住下游，会派活、产生真实副作用却永不落库 | 放宽为 `("submitted","working")` 并**检查 `_updated`**，抢不到就不派活 |
| **M4** | `await lock.acquire()` 与结算段都在 `try` 之外 —— 在那里被取消或落库失败，任务永远停在活跃态（有活跃态、无 worker、无终态） | 整段（含取锁）纳入同一保护块，任何异常路径都落终态 |
| **P2-9** | 先写产物再写终态 → 「canceled 却带 response artifact」 | **先抢终态**，抢到了才写产物；输了就不写 |
| **P1-4** | `_send_message` 的 `except CancelledError: pass` 吞掉调用方取消，且 `await bg` 不会把取消传下去 → 调用方以为取消了、任务在后台跑完 | `bg.cancel()` + 等它结算 + re-raise |
| **M1** | `add_message` 只捕 `IntegrityError`/`OperationalError`，`_dumps` 抛 `TypeError` 会带着未提交事务冒泡 → 连接卡死，后续全部请求被毒化 | 加 `except BaseException` 兜底回滚；`_dumps` 加 `default=str` |
| **P2-7** | 兜底取消先写消息再改状态，会给已完成任务追加「已取消」消息 | 先做带 `only_from` 的状态更新，确认成功再补消息 |
| **P2-1** | 缺省 `planId` 用 `abs(hash(...))`，并发同名 plan 会混审计 | 改用 `uuid4` |

**P2-9 的取舍要说明**：先抢终态意味着产物写入失败会留下「completed 但无 artifact」。
选它的理由是 —— 「已取消却有交付物」会让调用方以为活干完了，是**误导决策的假象**；
「completed 但缺交付物」至少状态是诚实的，且由 `except` 分支记一条 error 消息，不静默。

新增回归：`tests/test_state_integrity.py`（8 个用例，已进单元级分组）。

### 第三批：Windows 子进程（P1-7 / P1-8 / P2-21 / P2-8 / P2-14）

#### P1-8：先做实验再动手，结论推翻了预设

本机四个 CLI 里三个是 `.cmd`，Windows 上不能直接 CreateProcess，必须经 `cmd.exe /c`
—— 而 cmd 会**重新解释**命令行。实测（四种调用形式 × 8 个样本）结果：

| 传入 | 现状下下游收到 | 后果 |
| --- | --- | --- |
| `a&b` | `a` | `&` 把命令切断 —— `a&calc` 会**真的执行 calc** |
| `a\|b` | 空 + 报错 | `\|` 变管道，把 `b` 当命令跑 |
| `a>b` | 空 | `>` 变重定向，输出被吞 |
| `a^b` | `ab` | `^` 被当转义符吃掉 |
| `a%PATH%b` | **展开成 PATH 的值** | **加引号也拦不住** |

我原本设想的 `/s` + 外层引号方案（`list2cmdline` 再包一层）**完全失败** ——
8 个用例全错，因为 `list2cmdline` 把引号转义成 `\"`，cmd 不认识，反而更乱。

而 `%` 那一条决定了整个方向：**cmd 命令行上的 `%` 无法可靠转义**
（`^%` 无效，`%%` 只在批处理文件里有效）。所以「转义参数」这条路根本走不通，
唯一稳妥的做法是**不让可疑值进入命令行**。

| 编号 | 改动 |
| --- | --- |
| **P1-8** | 加 `SAFE_ARG_RE` 白名单（`[A-Za-z0-9._:@/+\-]+`）。由下游决定的值命中不了就**明确拒绝**，而不是静默拆错；校验发生在 spawn **之前** |
| **P2-14** | `command[0]` 是裸命令名时先经 `which()` 解析，再判断要不要包 `cmd.exe`（原来只按字符串判断，漏掉包裹 → WinError 193） |
| **P2-8** | deadline 挪到 **spawn 之前**建立，并把 spawn 纳入 `wait_for` —— 原来 spawn 阶段（cmd.exe 冷启动、杀软扫描）完全不受预算约束 |
| — | `cmd.exe /d /c` 加 `/d`，关掉 AutoRun，免得注册表里的 AutoRun 命令混进输出 |

#### P1-7 + P2-21：用 Job Object 换掉 taskkill

原来的 `taskkill /PID <pid> /T /F` 有两个问题：**按 PID 定位**（检查 returncode 与
执行 taskkill 之间进程若退出，PID 可能被复用 → 杀掉无关进程），且**返回码与 stderr
全被 DEVNULL 掉**（权限不足时静默漏杀，孤儿 node 继续烧 token）。

改成三级策略：

1. **Job Object**（首选）—— `CreateJobObject` + `KILL_ON_JOB_CLOSE`，
   spawn 后立刻 `AssignProcessToJobObject`。用**句柄**定位，没有 PID 复用窗口；
   `TerminateJobObject` 一次干掉整棵树，**包括「直接子进程已退出、只剩孙进程」**
   这种情况 —— 那是 `taskkill /T` 够不着的。
2. **taskkill**（兜底）—— 但**必须看返回码**，失败就把原因写进错误文案。
3. `proc.kill()` 最后手段。

拿不到 job 时（非 Windows / 嵌套 job 受限）自动退回第 2 级，行为不退化。

#### 顺带发现的新问题（不在原清单里）：失效 session 被盲目续接

验证这批时 `test_p1_fixes` 失败，查下去发现**不是我的改动引起的**（我用
`git stash` 收起改动、用旧代码跑了一遍，症状完全一致）。根因是另一件事：

`contexts` 表把 `(contextId, agent) → sessionId` **永久**保存。当那个 session 在下游
已经不存在（重启、被清理、换了 cwd）时，hub 仍会把它当 `--session-id` 传下去，
下游回**退出码 1 且没有任何输出** —— 从错误里根本看不出原因。更糟的是这条死映射
**不会被清理**，于是这个 contextId 之后**每次**都失败，等于把它彻底弄坏了。

**实测证据**（同一个 contextId，只换 session）：

| 调用方式 | 结果 |
| --- | --- |
| 不带 session | `ok=True`，exit 0 |
| 带那个 4 小时前的 session | `ok=False`，exit 1，**无任何输出** |

**修法**：续接失败且不是超时时，清掉映射、**不带 session 重试一次**。
三个条件都满足才做 —— 用了 session 续接、不是超时（超时说明下游确实跑了那么久，
重试等于把开销翻倍）、还没重试过。审计里留下完整痕迹：

```
resume failed with session session-79752a4c-...; clearing the mapping and retrying without session
→ turn_start turn=1 → HEALED
```

为此给 `CallResult` 加了 `timed_out` 字段，让「超时」和「秒级失败」可区分。

#### 测试基础设施：加了「平台级」第三档

Job Object 测试要**真的起进程**，塞进单元级会让「单元级全绿 == core 与平台无关」
这条约束失效。所以新开一档：

| 档 | 内容 | CI |
| --- | --- | --- |
| 单元级 | 假适配器，不碰真实进程 | ✅（ubuntu + windows） |
| **平台级** | 起真实进程（只用 `sys.executable`），验证 Windows 独有机制；非 Windows 自行跳过 | ✅（windows-latest 那格真正跑到） |
| 集成级 | 真实 CLI / hub 在跑 | 仅开发机 |

顺带修掉 `test_async_cancel.py` 里一个陈旧默认值：节点名写的是 `dsh`，
而注册的是 `dsh-cli` —— 这个测试一直以「agent 未注册」失败，**白白掩盖了它本该
验证的取消路径**。

### 一个自己引入的 bug（被新测试抓出来）

重构 `_execute` 时把 `lock.acquire()` 移进了 `try`，于是 `finally` 里的
`lock.release()` 变成**无条件执行** —— 即使 `acquire()` 从未成功。
除了抛 `Lock is not acquired` 打断取消传播，更糟的是：
这把锁若正被别的协程持有，**误释放会直接破坏互斥**。
改为用 `acquired` 标志判断。

**这条值得记**：重构时把语句移进/移出 `try`，必须同时检查 `finally` 的清理动作
是否还成立。这个 bug 靠读代码没看出来，是 `test_cancel_while_waiting_for_lock`
跑出来才暴露的。

### 验证结果

| 验证项 | 结果 |
| --- | --- |
| 单元级回归 | **7/7 PASS**（新增三套共 19 个用例） |
| 平台级 | **1/1 PASS**（Job Object 两个场景：子进程存活时收树、子进程退出后收孙进程） |
| 集成级（真实 CLI + hub） | **6/6 PASS** |
| **合计** | **14/14 PASS，0 失败** |
| schema 迁移（线上库副本，87 个任务） | 列已加、`schemaVersion` 1→2、数据完好、`started_at` 全部回填 |
| hub 重启（真实线上库） | `schemaVersion: 2`，任务与 4 个 agent 全部保留 |
| N1 线上实证 | `running` 快照从恒为 0 → **1**；CancelTask 后回到 **0**，无孤儿进程 |
| N1 取消后收尾 | plan 返回 `ok=False`（**不抛异常**），被取消的 step 记 `canceled: 该 step 被显式取消`，未执行的 step 全部结算 |
| N2 线上实证 | `p2` 排队等待 8895ms、实际执行 3690ms —— 新口径报 3690ms，旧口径会报 12585ms（**虚高 3.4 倍**） |
| N2 甘特图 | `p1` left=0.0% / `p2` left=70.7%，两根条首尾相接 —— 修复前两根都会是 `left=0%` |
| 单元测试里的极端值 | 第二层真实 70ms，旧口径 1351ms（**虚高 19 倍**） |
| 第二批后线上冒烟 | 真实 2 步 plan 9.6s 通过；审计耗时 `a=5739ms` + `b=3822ms` ≈ 总时长（串行两层，符合预期） |
| argv 白名单 | 13/13 危险值被拒、5/5 真实标识符放行；`call()` 在 spawn **之前**就拒绝 |
| 会话自愈 | 失效 session 触发「清映射 + 不带 session 重试」→ 任务从 FAILED 变 COMPLETED |
| 四个节点的 argv | `/d` + 解析后的 `.cmd` 路径，四个 CLI 全部正确 |

**老数据的限制**：历史编排任务的 `started_at` 只能回填 `created_at`，
那部分虚高值无法追溯还原 —— 已在 `_migrate` 的注释里写明。

### 未修（留待决策）

**演练自身的问题**：N3（节点写能力不一致，需先定设计）、N4（`RunPlan` 同步阻塞）、
N5（过程回传非实时）。

**合并清单 38 条里的进度**：已处理 12 条 ——
`M1`/`M3`/`M4`(P0/P1)、`P1-4`/`P1-7`/`P1-8`(P1)、
`P2-1`/`P2-7`/`P2-8`/`P2-9`/`P2-14`/`P2-21`(P2)，
外加一条**不在原清单里**的新发现（失效 session 被盲目续接）。
其余 26 条未动，按主题大致分三堆：

| 主题 | 代表条目 | 备注 |
| --- | --- | --- |
| **并发与事件循环** | `M2`（`add_message` 在事件循环线程里 `time.sleep`，SQLITE_BUSY 时会阻塞**整个 asyncio 服务**，连 CancelTask 都调度不了）| 影响面大，改动需谨慎 |
| **流解析** | `P2-5`（`noise` 无界增长）、`P2-17`（`STREAM_LIMIT` 是硬上限而非截断，一行超限毁掉整轮）、`P2-24`（逐行解析，多行 JSON 无法重组且静默丢弃）| 三条都是 `_consume`，建议一起做 |
| **查询一致性** | `P1-6`（metadata 合并是读+整列替换，非原子）、`P2-12`、`P2-23`（多次独立查询产生撕裂读）| 需引入事务边界 |

另有 4 处审查员自己标注的「待确认」项，采纳前需先确认前提是否成立 ——
按 P1-1 的教训，这些尤其不能直接采信。
