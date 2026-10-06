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
| P1-1 子步骤取消被降级 | ✅ 属实 | `orchestrator.py:262` `isinstance(outcome, BaseException)` |
| P2-1 缺省 `plan_id` 用 `abs(hash(...))` | ✅ 属实 | `orchestrator.py:206` |
| P2-2 `parse_timeout` 重复调用 | ✅ 属实 | `hub_app.py:509` 与 `546` |
| P2-3 复用占位任务时 `None` 覆盖 metadata | ✅ 属实 | `hub_app.py:602-607` |

**实质结论 6/6 属实。**

但**行号不可用**：

| 条目 | 清单标注 | 实际行号 |
| --- | --- | --- |
| `dispatch_task` | 532-594 | **574** |
| `_cancel_task` | 838-847 | **892** |
| `_execute` | 615-627 | **646** |

偏差 31 / 42 / 54，不固定 —— 是估算而非抄录。
（staged 文件与源文件经 `diff` 确认完全一致，1082 行，不存在「读错文件」的可能。）

**结论：这份清单实质可信，但引用前必须重新定位行号。**
这也是把 LLM 审查接入流水线时必须配套的一步 —— 不能直接采信位置信息。

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
