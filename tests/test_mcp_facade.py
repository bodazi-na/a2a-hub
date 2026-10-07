#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP facade 冒烟测试。

用官方 MCP 客户端以 stdio 拉起 facade，列工具、调一个真实工具。

跑法（hub 需在 9200 上运行）：
    python tests/test_mcp_facade.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client           # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SRC = os.path.join(ROOT, "src")

# 子进程用 `-m a2a_hub.mcp_facade`：包在 src 布局下，所以 PYTHONPATH 要给
# **<仓库根>/src**（不是仓库根）。给错了子进程会直接起不来，客户端只看到
# 一句 "Connection closed" —— 看不出跟路径有关。
PARAMS = StdioServerParameters(
    command=PY,
    args=["-m", "a2a_hub.mcp_facade"],
    cwd=ROOT,
    env={**os.environ, "PYTHONPATH": SRC},
)


async def main() -> int:
    print("=== 以 stdio 拉起 MCP facade ===")
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            # mcp 2.x 用 snake_case（v1 是 serverInfo）
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            print(f"  server  : {info.name}")
            print(f"  version : {info.version}")

            tools = (await session.list_tools()).tools
            print(f"\n=== 暴露 {len(tools)} 个工具 ===")
            for t in tools:
                first = (t.description or "").split("\n")[0][:64]
                print(f"  {t.name:<14} {first}")

            print("\n=== 调 hub_status ===")
            res = await session.call_tool("hub_status", {})
            text = "".join(c.text for c in res.content if hasattr(c, "text"))
            print(f"  {text[:300]}")

            print("\n=== 调 hub_agents ===")
            res = await session.call_tool("hub_agents", {})
            text = "".join(c.text for c in res.content if hasattr(c, "text"))
            print(f"  {text[:400]}")

    expected = {"hub_status", "hub_agents", "hub_send", "hub_plan", "hub_trace", "hub_tasks"}
    got = {t.name for t in tools}
    ok = expected <= got
    print("\n==== 汇总 ====")
    print(f"  工具齐全: {'PASS' if ok else 'FAIL'}（缺 {sorted(expected - got)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
