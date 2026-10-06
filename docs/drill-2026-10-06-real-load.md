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

### 第四批：事件流解析（P2-5 / P2-17 / P2-24）

**这三条其实是同一个设计缺陷的三个侧面** —— 都源于「逐行走 `readline()` +
逐行 `json.loads`」。所以没有分开打补丁，而是把 `_consume` 整个换掉：
不再用 `readline`，改成**按块读 + 自己切行**。

| 编号 | 问题 | 改动 |
| --- | --- | --- |
| **P2-17** | `STREAM_LIMIT` 是 StreamReader 的**硬上限**而不是截断：单行超限 → `readline()` 抛 `LimitOverrunError` → 整个 call 被判失败并 kill 进程树。**一条超大工具输出就能毁掉一轮** | 改用 `read()`（不受该上限约束）自己切行；超长行**截断保留前缀**并计数，轮次继续 |
| **P2-5** | `state["noise"]` 无限 append，CLI 刷屏时内存无界增长（最终却只用它的长度） | 只留最近 200 行，总量另用计数器 |
| **P2-24** | 逐行 `json.loads`，pretty-printed JSON 无法跨行重组；以 `{` 开头但解析不出的行**全部落进 noise 被静默丢弃** | 对 `{` 开头但单行解析不出的行做**跨行缓冲聚合**；聚合失败**单独计数**（`unparsed`），不再混进普通噪声 |

**实现里有两处关键细节：**

1. **超长行的尾巴不能被当成新行。** 截断后清空缓冲，但这条行的剩余部分还在流里
   —— 必须进入「跳过模式」，直到下一个换行符才恢复。否则尾巴里的内容会被当作
   独立一行解析。测试里专门往尾巴塞了一个 `{"t": "SHOULD-NOT-APPEAR"}` 来钉死这点。
2. **跨行重组不能每行都重解析。** 只在「看起来收尾了」（行尾是 `}` 或 `]`）时才尝试
   `json.loads` 整个缓冲 —— 否则大对象会退化成 O(n²)。

**测试抓到的计数 bug**：超长行最初被记成 **3 条**而不是 1 条。原因是跳过模式下
每个数据块都会再次触发同一个分支，于是每满一个 `STREAM_LIMIT` 就加一次。
语义应该是「被截断的**行数**」，加了 `if not skipping` 判断才对。

**这批是纯逻辑，可以完全用假流测** —— 不需要起进程、也不需要真实 CLI。
测试用 **7 字节一块**的假流驱动，把「一行跨多次 read」这条真实路径真正走到了
（真实管道就是这么分块的）。新增 `tests/test_stream_parsing.py`（9 个用例）。

顺带把三个 `finalize` 里重复的计数上报抽成 `_stream_stats()`，
现在 metadata 会带 `noiseLines` / `oversizedLines` / `unparsedJsonLines` 三个数 ——
**「静默丢数据」是这个适配器最隐蔽的失败模式**：任务显示成功，
但下游真正说的东西被丢掉了。这三个数就是它的报警器。

### 第五批：Store 对事件循环的阻塞（M2）

**先测量，再决定改不改。** M2 的建议是「store 调用改走 `asyncio.to_thread`」——
但那是个大改动（Store 是同步 API，全项目都在直接调），而且会给**每一次**调用
引入新的 await 点，也就是新的竞态风险。所以在动手前先量：

| 项 | 实测 |
| --- | --- |
| 单次 `add_message` | **4.57 ms** |
| 写 50 个事件 | 262 ms，事件循环最大阻塞 **257 ms** |
| 读任务（GetTask 形态） | 50 次 49 ms，最大阻塞 59.5 ms |
| PRAGMA | `journal_mode=wal` **`synchronous=2`（FULL）** `busy_timeout=5000` |

**根因不是「没有线程」，是 `synchronous=FULL`** —— 每次 COMMIT 都 fsync。
于是做了对照实验：

| 设置 | 50 次写入 | 事件循环最大阻塞 |
| --- | --- | --- |
| `synchronous=FULL`（原状） | 197.7 ms | **192.8 ms** |
| `synchronous=NORMAL` | 3.1 ms | 10.8 ms |
| `synchronous=NORMAL` + 单事务批量 | **0.9 ms** | 11.9 ms |

**64 倍 / 220 倍。** 所以决定**不引入线程** —— 收益已经被吃掉了，而 `to_thread`
带来的新增 await 点（`_execute` 里「更新状态 → 写消息 → 写产物」之间多出可被打断
的位置）是实打实的风险。这个判断写在测试文件的 docstring 里，不是口头结论。

两层修法：

| 层 | 改动 |
| --- | --- |
| **`synchronous=NORMAL`** | WAL 模式下的推荐值。语义差别只有「**OS 崩溃 / 断电**可能丢最近若干事务」；**进程崩溃不丢已提交数据**。对任务协调日志这个取舍划算，且断电场景由 `recover_orphans` 兜住（不会留下卡在 `working` 的任务） |
| **`add_messages()`** | 一次事务写多条。`_execute` 的过程事件从「N 次事务」变成「1 次事务」 |

顺带把 `add_message` 里那坨 BEGIN/COMMIT/ROLLBACK/重试抽成了 `_write_txn()`，
两条写入路径共用 —— 顺便把退避总量从 150ms 压到 100ms（M2 里「硬睡阻塞服务」
的那部分）。

#### 用测试把结论钉住，而不是靠记性

这批最重要的产出是**防退化**。三条**结构性断言**（不受机器快慢影响）：

1. `PRAGMA synchronous` 必须是 NORMAL
2. `add_messages(N)` 只能开**一个**事务
3. `_execute` 落过程事件时，逐条 `add_message` 的调用次数**不随事件数增长**
   （跑 10 个事件和 100 个事件，逐条调用次数必须相同）

加一条计时兜底（取 3 次最小值，阈值 150ms）。

**计时测试的一段插曲值得记**：最初写的是单次测量 + 绝对阈值，结果在本机
全量跑时稳定 170–180ms，而单独跑只有 2.8ms。查下去发现是**环境噪声** ——
同一进程里「第一次写一个全新的临时库」会被拖慢（对照：纯函数层面
`add_messages(200)` 稳定在 2.2–3.1ms，线性、每条约 14µs）。
所以改成取多次最小值，并把主要防线放在结构性断言上。

### 第六批：查询一致性（P1-6 / P2-12 / P2-23）

**三者同源：缺事务边界。** `Store` 用 `isolation_level=None`（自动提交），
**每条语句各自一个隐式事务** —— 于是读序列会被写插进来，读改写会丢更新。

| 编号 | 问题 | 改动 |
| --- | --- | --- |
| **P1-6** | metadata「合并」= 「`get_task` 读 → `update_task` 整列替换」两步，无事务包裹。两个并发写者互相覆盖（典型：`dispatch_task` 补 planId vs `recover_orphans` 打 interrupted 标记）| 新增 `merge_task_metadata()`，**读-合并-写放进同一个写事务** |
| **P2-12** | `update_task` 的 UPDATE 与随后的回读是两条独立语句 —— 返回的 task 可能与 `_updated` 对不上 | 两者合进一个事务 |
| **P2-23** | `list_tasks`+`count_tasks`、`get_task`+`list_messages`+`list_artifacts` 各自独立查询，WAL 下无跨语句快照 → **撕裂读** | 新增 `read_txn()`（`BEGIN DEFERRED`）；`_task_payload` / `admin_tasks` / `_get_trace` 各自包成一个快照 |

**为什么没用 SQL 的 `json_patch()`**：它的语义是 RFC 7396 ——
**值为 null 的键会被删除**。而我们要的是「把键设成 null」。
在 `dispatch_task` 补 `planId=None` 这类场景下两者行为不同，所以选了显式的
读-合并-写，而不是 SQL 内合并。

#### 撕裂读怎么确定性复现

这是这批测试里最值得记的一招：**开第二个连接，在读序列的两次查询之间插一笔写**。

WAL 下写不阻塞读，所以那笔写立刻生效 —— 有读事务时读方看不到它（快照），
没有读事务时就会看到。这正是撕裂。测试里就是这么钉的：

```
payload 里的产物名: ['response']          ← 快照，看不到中途插入的
库里实际的产物名  : ['response', 'late']   ← 插入确实发生了
```

#### 顺手加的一个保护

**事务不可重入**，嵌套时直接抛 `RuntimeError` 并说明怎么办 ——
而不是让 SQLite 抛 `cannot start a transaction within a transaction`：
那句话不告诉你**是谁**在哪儿嵌套的。

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
| 单元级回归 | **10/10 PASS**（新增六套共 43 个用例） |
| 平台级 | **1/1 PASS**（Job Object 两个场景：子进程存活时收树、子进程退出后收孙进程） |
| 集成级（真实 CLI + hub） | **6/6 PASS** |
| **合计** | **17/17 PASS，0 失败** |
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
| 流解析（假流） | 9/9：超长行截断不致命、尾巴不当新行、噪声限量、多行 JSON 重组、拼不出的单独计数 |
| 流解析（真实 CLI） | claude-cli 实跑：COMPLETED，`noiseLines/oversizedLines/unparsedJsonLines` 全 0 |
| M2 阻塞（前后对比） | 50 个事件落库：**192.8ms → 0.9ms**（事件循环最大阻塞） |
| M2 真实任务 | 含工具调用的任务：COMPLETED，6 条消息 seq 连续，过程事件走批量 |
| 查询一致性 | 8/8：合并语义、不丢键、hub 走原子合并、UPDATE 与回读一致、两处快照、嵌套保护、事务释放 |
| 一致性真实任务 | 真实 2 步 plan：metadata 保住了原有 `planned` 键并补上 `planId`/`stepId`；trace 快照正常 |

**老数据的限制**：历史编排任务的 `started_at` 只能回填 `created_at`，
那部分虚高值无法追溯还原 —— 已在 `_migrate` 的注释里写明。

### 未修（留待决策）

**演练自身的问题**：N3（节点写能力不一致，需先定设计）、N4（`RunPlan` 同步阻塞）、
N5（过程回传非实时）。

**合并清单 38 条里的进度**：已处理 19 条 ——
`M1`/`M2`/`M3`/`M4`(P0/P1)、`P1-4`/`P1-6`/`P1-7`/`P1-8`(P1)、
`P2-1`/`P2-5`/`P2-7`/`P2-8`/`P2-9`/`P2-12`/`P2-14`/`P2-17`/`P2-21`/`P2-23`/`P2-24`(P2)，
外加一条**不在原清单里**的新发现（失效 session 被盲目续接）。

**清单里所有 P0 和 P1 已全部处理完毕。** 剩下 19 条全是 P2，且多是低危的健壮性条目：

| 类别 | 代表条目 |
| --- | --- |
| 索引与 schema 细节 | `P2-15`（重复索引、缺聚合列索引）、`P2-16`（老库重复 seq 时静默降级唯一索引）|
| 容错与可观测 | `P2-4`（`str()` 投影非 list content）、`P2-10`（`_loads` 解析失败静默返回默认值）、`P2-11`（`IntegrityError` 不区分原因）|
| 资源上限 | `P2-13`（`_context_locks` 只增不删）、`P2-20`（Semaphore/Lock 绑定首次使用的事件循环）、`P2-25`（`_get_adapter` 立即关闭在飞适配器）|
| 语义边界 | `P2-19`（`_context_lock` 锁键在 `sharedContext` 下会串行化扇出）、`P2-22`（同 P2-19）|

另有 4 处审查员自己标注的「待确认」项，采纳前需先确认前提是否成立 ——
按 P1-1 的教训，这些尤其不能直接采信。
