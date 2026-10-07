#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A2A-20：取消必须真的取消。

原 `test_async_cancel.py` 的断言是
    final_state in {"TASK_STATE_CANCELED", "TASK_STATE_COMPLETED"}
——「取消后变成 COMPLETED」也被判通过，所以 A2A-01 那个状态回退的 bug
从测试里溜过去了。这里把断言收紧，并用**受控阻塞的假适配器**做真正的单元测试：
不依赖真实 CLI、不依赖网络，能同时断言两件事：
  1. 取消后终态必须保持 canceled（不许被跑完的执行体覆盖回 completed）
  2. 下游调用**确实被中断**（假适配器要观察到 CancelledError）

跑法：python tests/test_cancel_semantics.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.adapters.base import Adapter, CallResult                    # noqa: E402
from a2a_hub.core.hub_app import Hub                                     # noqa: E402
from a2a_hub.core.registry import AgentRecord, Registry                  # noqa: E402
from a2a_hub.core.router import Router                                   # noqa: E402
from a2a_hub.core.store import Store                                     # noqa: E402


class BlockingAdapter(Adapter):
    """受控阻塞：call() 一直等，直到被取消。"""

    kind = "test"

    def __init__(self, name: str = "blocker"):
        super().__init__(name)
        self.entered = asyncio.Event()
        self.cancelled = False
        self.completed = False

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt: str, *, context_id=None, session_id=None,
                   timeout: float = 600.0) -> CallResult:
        self.entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return CallResult(ok=True, text="should-not-happen")


def build_hub(db_path: str):
    store = Store(db_path)
    registry = Registry(store)
    router = Router(registry)
    adapter = BlockingAdapter("blocker")
    registry.register(AgentRecord(name="blocker", kind="test", tags=["block"]))
    hub = Hub(store, registry, router, {"blocker": adapter})
    return hub, store, adapter


async def test_async_cancel() -> bool:
    """异步任务取消后：终态 canceled，且下游被中断。"""
    print("\n[1] 异步任务取消语义")
    db = os.path.join(tempfile.gettempdir(), "cancel_semantics_async.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass

    hub, store, adapter = build_hub(db)
    sent = await hub._send_message({
        "agent": "blocker",
        "async": True,
        "message": {"messageId": "m1", "role": "ROLE_USER",
                    "parts": [{"text": "go"}], "contextId": "ctx-1"},
    })
    task_id = sent["task"]["id"]
    state_after_send = sent["task"]["status"]["state"]
    print(f"  派发后: {state_after_send}")

    await asyncio.wait_for(adapter.entered.wait(), timeout=5)
    print("  下游已进入执行")

    cancelled = await hub._cancel_task({"id": task_id})
    print(f"  取消后: {cancelled['status']['state']}")

    # 关键：再等一会儿，确认没有「跑完回写 completed」
    await asyncio.sleep(1.5)
    final = store.get_task(task_id)
    print(f"  1.5 秒后: {final['state']}")

    checks = {
        "派发即 working": state_after_send == "TASK_STATE_WORKING",
        "取消返回 canceled": cancelled["status"]["state"] == "TASK_STATE_CANCELED",
        "终态保持 canceled（不回退）": final["state"] == "canceled",
        "下游调用确实被中断": adapter.cancelled,
        "下游没有跑完": not adapter.completed,
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


async def test_sync_cancel() -> bool:
    """同步任务取消后：同样必须保持 canceled（A2A-01 的根因场景）。"""
    print("\n[2] 同步任务取消语义")
    db = os.path.join(tempfile.gettempdir(), "cancel_semantics_sync.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass

    hub, store, adapter = build_hub(db)

    send_task = asyncio.create_task(hub._send_message({
        "agent": "blocker",
        "message": {"messageId": "m2", "role": "ROLE_USER",
                    "parts": [{"text": "go"}], "contextId": "ctx-2"},
    }))
    await asyncio.wait_for(adapter.entered.wait(), timeout=5)

    # 同步路径也要能在 _running 里被找到
    task_id = next(iter(hub._running))
    print(f"  同步任务已登记进 _running: {task_id[:8]}…")

    cancelled = await hub._cancel_task({"id": task_id})
    print(f"  取消后: {cancelled['status']['state']}")

    try:
        await asyncio.wait_for(send_task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001
        pass

    await asyncio.sleep(1.5)
    final = store.get_task(task_id)
    print(f"  1.5 秒后: {final['state']}")

    checks = {
        "取消返回 canceled": cancelled["status"]["state"] == "TASK_STATE_CANCELED",
        "终态保持 canceled（不回退）": final["state"] == "canceled",
        "下游调用确实被中断": adapter.cancelled,
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


async def main() -> int:
    results = [await test_async_cancel(), await test_sync_cancel()]
    print("\n==== 汇总 ====")
    for name, ok in zip(["异步取消", "同步取消"], results):
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
