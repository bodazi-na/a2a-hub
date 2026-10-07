#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一跑自检脚本，并按「是否需要本机环境」分组。

为什么要分组
------------
这些自检脚本不是同一种东西：

- **单元级**：用内存 Store + 假适配器，不碰真实 CLI / 网络 / 系统命令。
  → 任何机器上都能跑，**CI 里必须全绿**。它们验证的是 core 的逻辑。
- **平台级**：会**真的起进程**（但只用 `sys.executable`，不依赖外部 CLI），
  用来验证 Windows 独有机制（Job Object）。非 Windows 自动跳过并按 PASS 计。
  → CI 的 windows-latest 那格能跑到它。
- **集成级**：要真实 CLI、Windows 进程命令、或 hub 正在运行。
  → 只在开发机上跑。它们验证的是**适配器与本机工具的对接**。

单元级那条分界线恰好就是「薄核心 / 厚适配器」的边界：
单元级全绿 == 核心确实与平台无关。平台级与它分开，是为了不让这条约束失效。

用法
----
    python tests/run_all.py             # 单元级 + 平台级（CI 用这个）
    python tests/run_all.py --all       # 再加集成级
    python tests/run_all.py --list      # 只列分组
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# CI 的 windows-latest 默认代码页是 cp1252，编不下测试标题里的中文，
# print 直接 UnicodeEncodeError —— 两个 Windows 任务因此全挂（ubuntu 不受影响）。
# 统一把输出流切到 utf-8（py3.7+ 都支持 reconfigure），本机与 CI 行为一致。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 单元级：内存 Store + 假适配器 / 自建假下游，无外部依赖
UNIT = [
    ("test_cancel_semantics", "取消语义（假适配器）"),
    ("test_plan_cancel_and_timing", "编排取消 + 计时基准（假适配器）"),
    ("test_state_integrity", "状态机完整性（假适配器）"),
    ("test_subprocess_argv", "argv 白名单与 cmd.exe 包裹（纯逻辑）"),
    ("test_stream_parsing", "事件流解析：截断/限量/跨行重组（假流）"),
    ("test_store_blocking", "Store 对事件循环的阻塞（M2）"),
    ("test_query_consistency", "查询一致性：事务边界（P1-6/P2-12/P2-23）"),
    ("test_p2_fixes", "批次 2 修复（内存 Store）"),
    ("test_adapter_p2", "适配器修复（自建假下游）"),
    ("test_isolation", "隔离（假适配器，不执行 CLI）"),
    ("test_streaming", "流式 SSE：边跑边到 / 时间戳=发生时刻 / 背压（假适配器）"),
    ("test_parallelism", "并行度：分层推断 / 平均与峰值 / 空闲率（纯函数）"),
    ("test_no_stale_refs", "守卫：仓库里没有残留的旧布局导入/调用（纯文本扫描）"),
]

# 平台级：会**真的起进程**（用 sys.executable，不依赖任何外部 CLI），
# 非 Windows 上自行跳过并按 PASS 计 —— 所以 CI 的 windows-latest 那一格能真正跑到它。
#
# 为什么不塞进单元级：单元级那条「全绿 == core 确实与平台无关」的约束，
# 靠的是「不碰真实进程」。把进程测试混进去会让那条约束失效。
PLATFORM = [
    ("test_job_object", "Job Object 收进程树（需 Windows，非 Windows 跳过）"),
    ("test_startup_guard", "启动护栏：非回环+无认证必须拒绝（起真实进程）"),
]

# 集成级：需要真实 CLI / 系统命令 / hub 在跑
INTEGRATION = [
    ("test_cli_lifecycle", "CLI 生命周期（需 Windows tasklist/taskkill）"),
    ("test_kill_tree", "进程树杀死（需 taskkill）"),
    ("test_cli_direct", "CLI 直连（需真实 CLI，逐个指定）"),
    ("test_async_cancel", "异步取消（需 hub 在跑）"),
    ("test_p1_fixes", "P1 修复（需 hub 在跑）"),
    ("test_mcp_facade", "MCP facade（需 hub 在跑）"),
]


def run_one(name: str) -> tuple[bool, str]:
    path = os.path.join(ROOT, "tests", f"{name}.py")
    if not os.path.exists(path):
        return False, "脚本不存在"
    # 子进程会打印中文标题：CI runner（cp1252）下若继承到非 UTF-8 的
    # PYTHONIOENCODING 会直接 UnicodeEncodeError。这里强制切成 utf-8。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [PY, path], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, env=env,
    )
    tail = (proc.stdout or "").strip().splitlines()
    summary = next((l for l in reversed(tail) if "RESULT" in l or "汇总" in l), "")
    return proc.returncode == 0, summary or f"exit={proc.returncode}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连集成级一起跑")
    ap.add_argument("--list", action="store_true", help="只列分组")
    args = ap.parse_args()

    if args.list:
        print("单元级（CI 可跑，无外部依赖）:")
        for n, d in UNIT:
            print(f"  {n:<24} {d}")
        print("\n平台级（会起真实进程，非 Windows 自动跳过）:")
        for n, d in PLATFORM:
            print(f"  {n:<24} {d}")
        print("\n集成级（需本机环境）:")
        for n, d in INTEGRATION:
            print(f"  {n:<24} {d}")
        return 0

    groups = [("单元级", UNIT), ("平台级", PLATFORM)]
    if args.all:
        groups.append(("集成级", INTEGRATION))

    failures: list[str] = []
    for title, items in groups:
        print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
        for name, desc in items:
            ok, note = run_one(name)
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name:<24} {desc}")
            if not ok:
                print(f"         {note[:100]}")
                failures.append(name)

    total = sum(len(items) for _, items in groups)
    print(f"\n{'=' * 60}")
    print(f"合计 {total} 项，失败 {len(failures)} 项")
    if failures:
        print("失败:", ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
