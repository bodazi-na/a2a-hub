#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证第三轮审查提的两条：

  1. plan 各 step 不应共享 context —— 共享会让同 agent 的并行分支被
     A2A-04 的锁串行化，并互相看到对方的会话。
  2. CLI 适配器的 cwd 不能是代码目录 —— 否则 agent 随手写的文件会落进仓库。

跑法：python tests/test_isolation.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import Adapter, CallResult          # noqa: E402
from adapters.cli import DshCLI                        # noqa: E402
from core.hub_app import Hub                           # noqa: E402
from core.registry import AgentRecord, Registry        # noqa: E402
from core.router import Router                         # noqa: E402
from core.store import Store                           # noqa: E402


class RecordingAdapter(Adapter):
    """记录每次调用收到的 context_id。"""

    kind = "test"

    def __init__(self, name="rec"):
        super().__init__(name)
        self.contexts: list[str | None] = []

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt, *, context_id=None, session_id=None, timeout=None):
        self.contexts.append(context_id)
        return CallResult(ok=True, text=f"ok:{prompt[:20]}")


def fresh_hub(adapters, workspace=None):
    db = os.path.join(tempfile.gettempdir(), f"iso_{os.urandom(4).hex()}.db")
    store = Store(db)
    registry = Registry(store)
    router = Router(registry)
    for name in adapters:
        registry.register(AgentRecord(name=name, kind="test", tags=[name]))
    hub = Hub(store, registry, router, adapters, workspace_dir=workspace)
    return hub, store


# ------------------------------------------------------- 问题 1
async def test_step_context_isolation() -> bool:
    print("\n[问题1] plan 的每个 step 应用独立 context")
    rec = RecordingAdapter("rec")
    hub, store = fresh_hub({"rec": rec})

    plan = await hub._run_plan({
        "contextId": "plan-ctx",
        "steps": [
            {"id": "a", "agent": "rec", "prompt": "第一步"},
            {"id": "b", "agent": "rec", "prompt": "第二步"},
            {"id": "merge", "agent": "rec", "prompt": "汇总 {{steps.a}} / {{steps.b}}"},
        ],
    })
    print(f"    plan ok={plan['ok']} layers={plan['layers']}")
    print(f"    各 step 收到的 context_id: {rec.contexts}")

    # 期望：三个 step 各自独立，且都带上了 plan-ctx 前缀
    unique = len(set(rec.contexts)) == len(rec.contexts)
    prefixed = all(c and c.startswith("plan-ctx::") for c in rec.contexts)
    ok = unique and prefixed and len(rec.contexts) == 3
    print(f"    互不相同: {unique} | 带 plan 前缀: {prefixed}")
    print("    ->", "PASS" if ok else "FAIL")

    # 对照组：显式要求共享时应共享
    rec2 = RecordingAdapter("rec")
    hub2, _ = fresh_hub({"rec": rec2})
    await hub2._run_plan({
        "contextId": "shared-ctx",
        "steps": [
            {"id": "a", "agent": "rec", "prompt": "第一步", "sharedContext": True},
            {"id": "b", "agent": "rec", "prompt": "第二步", "sharedContext": True},
        ],
    })
    print(f"    [对照] sharedContext=true 时: {rec2.contexts}")
    ok2 = len(set(rec2.contexts)) == 1 and rec2.contexts[0] == "shared-ctx"
    print("    ->", "PASS" if ok2 else "FAIL")
    return ok and ok2


# ------------------------------------------------------- 问题 2
def test_cli_workspace_isolation() -> bool:
    print("\n[问题2] CLI 适配器的 cwd 应是独立 workspace，不是代码目录")
    ws = Path(tempfile.gettempdir()) / f"a2a_ws_{os.urandom(3).hex()}"
    hub, store = fresh_hub({}, workspace=ws)

    store.upsert_agent(
        name="dsh-cli", kind="cli", tags=["cli"], enabled=True,
        config={"adapter": "DshCLI", "options": {
            "command": [r"D:\DSH\resources\runtime\cli\bin\dsh.cmd"],
            "timeout": 60,
        }},
    )
    rec = hub.registry.get("dsh-cli")
    adapter = hub._get_adapter(rec)
    print(f"    workspace_dir      : {hub.workspace_dir}")
    print(f"    适配器 cwd          : {adapter.cwd}")
    print(f"    hub 进程 cwd        : {Path.cwd()}")

    ok = (
        isinstance(adapter, DshCLI)
        and Path(adapter.cwd) == ws
        and Path(adapter.cwd) != Path.cwd()
        and Path(adapter.cwd).exists()
    )
    print(f"    workspace 已创建     : {ws.exists()}")
    print("    ->", "PASS" if ok else "FAIL")
    return ok


async def main() -> int:
    checks = {
        "问题1 step context 隔离": await test_step_context_isolation(),
        "问题2 CLI workspace 隔离": test_cli_workspace_isolation(),
    }
    print("\n==== 汇总 ====")
    for name, ok in checks.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
