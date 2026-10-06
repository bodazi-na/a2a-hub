#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Job Object 实测：真的能把整棵进程树收掉吗？（平台级，Windows）

为什么值得单独测
----------------
本机四个 CLI 里三个是 `.cmd` 启动器，真实结构是
    hub → cmd.exe → node（claude / codex / qoder 本体）
只杀直接子进程，底下的 node 会变成孤儿继续烧 token、占着 session。

原来的做法 `taskkill /PID <pid> /T /F` 有两个问题：
  P2-21  按 PID 定位 —— 从「检查 returncode」到「执行 taskkill」之间进程若
         恰好退出，PID 可能被复用，于是杀掉一个**无关进程**。
  P1-7   返回码与 stderr 全被 DEVNULL 掉 —— 权限不足时**静默漏杀**，
         调用方一无所知。

Job Object 用句柄定位，没有复用窗口，且 `TerminateJobObject` 一次收掉整棵树。
本测试验证两个真实场景：

  场景 A  孙进程在「子进程还活着」时被创建 → 一起收掉
  场景 B  子进程已经退出、只剩孙进程（taskkill /T 找不到它）→ 仍要收掉

非 Windows 平台直接跳过（返回 PASS），因为 Job Object 是 Windows 独有机制。

跑法：python tests/test_job_object.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.cli import CLIAdapter, _JobObject                    # noqa: E402

STILL_ACTIVE = 259

# 注意：孙进程要**脱离父进程的 stdio**（DEVNULL），否则它会一直持有那两根管道，
# asyncio 的 `proc.wait()` 可能迟迟不返回 —— 实测会超时。
_DETACH = ("stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
           "stderr=subprocess.DEVNULL")

# 子进程：先睡一会儿（给 hub 时间把 job 挂上），再拉一个长命孙进程
CHILD_KEEPALIVE = (
    "import subprocess, sys, time\n"
    "time.sleep(1.5)\n"
    f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'], {_DETACH})\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(3600)\n"
)

# 子进程：拉完孙进程就自己退出，把孙进程留下当孤儿
CHILD_EXITS = (
    "import subprocess, sys, time\n"
    f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'], {_DETACH})\n"
    "print(g.pid, flush=True)\n"
    "sys.exit(0)\n"
)


def is_alive(pid: int) -> bool:
    """进程是否还活着（按 PID 查；短测试内不存在复用问题）。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def report(checks: dict[str, bool]) -> bool:
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


async def spawn_tree(script: str):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job = _JobObject(proc.pid)
    return proc, job


async def wait_gone(pid: int, timeout: float = 8.0) -> bool:
    """等一个 PID 消失。用轮询而不是 `proc.wait()` —— 见 _DETACH 的说明。"""
    waited = 0.0
    while waited < timeout:
        if not is_alive(pid):
            return True
        await asyncio.sleep(0.1)
        waited += 0.1
    return not is_alive(pid)


async def read_grandchild(proc, timeout: float = 15.0) -> int | None:
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    text = (line or b"").decode("utf-8", "replace").strip()
    return int(text) if text.isdigit() else None


# ---------------------------------------------------------------- 场景 A


async def test_kill_tree_while_child_alive() -> bool:
    print("\n[场景 A] 子进程还活着时，孙进程一起被收掉")
    proc, job = await spawn_tree(CHILD_KEEPALIVE)
    print(f"  job 挂载: {'成功' if job.ok else '失败'}")
    if not job.ok:
        await CLIAdapter._kill(proc, job)
        return report({"Job Object 挂载成功（本机应当支持）": False})

    gpid = await read_grandchild(proc)
    print(f"  子 PID={proc.pid}  孙 PID={gpid}")
    alive_before = is_alive(gpid) if gpid else False

    problems = await CLIAdapter._kill(proc, job)
    await asyncio.sleep(0.5)
    alive_after = is_alive(gpid) if gpid else True
    print(f"  终止返回的失败说明: {problems or '（无）'}")

    return report({
        "孙进程终止前确实活着": alive_before,
        "孙进程被收掉（核心）": not alive_after,
        "子进程被收掉": not is_alive(proc.pid),
        "终止过程没有报失败": not problems,
    })


# ---------------------------------------------------------------- 场景 B


async def test_kill_tree_after_child_exited() -> bool:
    print("\n[场景 B] 子进程已退出、只剩孙进程 —— 仍要收掉（taskkill /T 做不到）")
    proc, job = await spawn_tree(CHILD_EXITS)
    if not job.ok:
        await CLIAdapter._kill(proc, job)
        return report({"Job Object 挂载成功": False})

    gpid = await read_grandchild(proc)
    gone = await wait_gone(proc.pid)
    print(f"  子进程已退出={gone}  孙 PID={gpid}")

    alive_before = is_alive(gpid) if gpid else False
    problems = await CLIAdapter._kill(proc, job)
    await asyncio.sleep(0.5)
    alive_after = is_alive(gpid) if gpid else True
    print(f"  终止返回的失败说明: {problems or '（无）'}")

    return report({
        "直接子进程确实已退出": gone,
        "只剩孙进程时它仍活着": alive_before,
        "孙进程仍被收掉（核心）": not alive_after,
        "没有因为「子进程已退出」就提前返回": not problems,
    })


async def main() -> int:
    if os.name != "nt":
        print("\n非 Windows 平台：Job Object 不适用，跳过（按 PASS 计）")
        print("\n==== 汇总 ====")
        print("  PASS  Job Object（跳过：非 Windows）")
        return 0

    cases = [
        ("场景 A 子进程存活时收树", test_kill_tree_while_child_alive),
        ("场景 B 子进程退出后收树", test_kill_tree_after_child_exited),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, await fn()))
        except Exception as exc:                        # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
