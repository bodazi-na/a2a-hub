#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""干净地验证 `_kill` 是否杀掉整棵进程树。

不依赖真实 CLI（它们跑太快/进程名混杂），而是构造一个可控的
`cmd.exe /c <长命子进程>` 场景：
  cmd.exe  ← 我们拿到的 proc.pid
    └─ ping.exe  ← 孙层，只 kill 外壳的话它会变孤儿

跑法：python tests/test_kill_tree.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.cli import CLIAdapter  # noqa: E402

MARKER = "PING-KILL-TREE-TEST"


def ping_alive() -> bool:
    """找那个带标记的 ping 进程还在不在。"""
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq PING.EXE"],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    ).stdout
    pids = [
        line.split('","')[1].strip('"')
        for line in out.splitlines()
        if "ping.exe" in line.lower()
    ]
    if not pids:
        return False
    # 用 wmic 被拦，改用 tasklist /V 看不到命令行；这里退化为「有 ping 就算有」
    return True


class _Probe(CLIAdapter):
    """只为复用 _kill，不做真实解析。"""

    def build_argv(self, session_id):
        return []

    def parse_line(self, obj, state):
        return []

    def finalize(self, state, returncode):
        raise NotImplementedError


async def main() -> int:
    probe = _Probe("kill-probe", command=["cmd.exe"])

    # cmd.exe /c ping -n 60 127.0.0.1  →  cmd 外壳 + ping 孙层
    proc = await asyncio.create_subprocess_exec(
        "cmd.exe", "/c", "ping", "-n", "60", "127.0.0.1",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(2.0)
    print(f"[1] 已启动 cmd.exe pid={proc.pid}，其下挂着 ping.exe")
    print(f"    此刻有 ping 进程: {ping_alive()}")

    print("[2] 调 _kill（应连 ping 一起杀掉）...")
    await _Probe._kill(proc)
    await asyncio.sleep(2.0)

    alive = ping_alive()
    print(f"    此刻有 ping 进程: {alive}")

    ok = not alive
    print()
    print("RESULT:", "PASS" if ok else "FAIL（ping 成了孤儿，说明只杀了外壳）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
