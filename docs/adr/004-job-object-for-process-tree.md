# ADR-004：Windows 子进程用 Job Object 回收，`taskkill` 降为兜底

## 状态

已采纳（2026-10-06）

## 背景

本机四个 CLI 里三个是 `.cmd` 启动器，真实结构是：

```
hub → cmd.exe → node（claude / codex / qoder 本体）
```

只杀直接子进程，底下的 node 会变成孤儿继续跑 —— 既烧 token 又占着下游 session。

原来用 `taskkill /PID <pid> /T /F`，但有两个问题：

| 问题 | 后果 |
| --- | --- |
| **按 PID 定位** | 从「检查 returncode」到「执行 taskkill」之间进程若恰好退出，PID 可能被复用 → **杀掉一个无关进程** |
| **返回码与 stderr 被丢弃** | 权限不足时**静默漏杀**，调用方一无所知 |
| `taskkill /T` 的边界 | 直接子进程已退出、只剩孙进程时，`/T` 找不到它 |

## 决策

**三级策略**：

1. **Job Object**（首选）—— `CreateJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
   + spawn 后立刻 `AssignProcessToJobObject`；终止用 `TerminateJobObject`
2. **`taskkill /T /F`**（兜底）—— 但**必须检查返回码**，失败原因写进错误文案
3. `proc.kill()` 最后手段

拿不到 job 时（非 Windows / 嵌套 job 受限）自动退回第 2 级，**行为不退化**。

## 理由

**Job Object 用句柄定位，没有 PID 复用窗口** —— 这是它对 TOCTOU 的直接回答。

**它能收掉 `taskkill /T` 够不着的情况**：直接子进程退出后，孙进程仍在 job 里，
`TerminateJobObject` 照样一次干掉。实测两个场景都过了
（`tests/test_job_object.py`）。

**`KILL_ON_JOB_CLOSE` 兜住了「hub 自己崩了」**：job 句柄随进程消失而关闭，
残余进程一起走。这是 `taskkill` 无论如何做不到的。

**为什么保留 taskkill 而不是直接 `proc.kill()`**：`proc.kill()` 只杀直接子进程，
对 `.cmd` 外壳无效。taskkill 至少能覆盖「job 创建失败但进程树还在」的情况。

## 已知边界

- **只在 Windows 验证过**。非 Windows 上 `_JobObject` 直接返回「不可用」，
  走 `proc.kill()`。跨平台需要另做（进程组 / `setsid`）
- **`AssignProcessToJobObject` 在 spawn 之后调用**，理论上存在「cmd.exe 已经拉起
  node 但还没被收进 job」的窗口。实测窗口极小（Python 侧只隔几条字节码），
  但要完全消除需要 `CREATE_SUSPENDED` + 先入 job 再 resume ——
  而 Python 的 `subprocess.Popen` 会关掉线程句柄，没法 resume，所以没走这条路

## 后果

**变容易的**：

- 取消 / 超时 / 异常三条路径的进程回收语义统一了
- 「有没有留下孤儿进程」有了明确的验证手段（枚举进程 + 查启动时刻）

**变难的**：

- 多了一层 ctypes 结构体定义（`JOBOBJECT_EXTENDED_LIMIT_INFORMATION` 的
  字段对齐要正确，否则 `SetInformationJobObject` 静默失败）
- 调试时要注意：**进程名和任务管理器里看到的不一致** ——
  dsh 的执行体是 `DeepSeek Harness.exe`，不是 `node.exe`

## 相关

- `docs/architecture.md` 的「写 CLI 适配器必踩的坑」第 4 条
- `tests/test_job_object.py`
