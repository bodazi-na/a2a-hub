#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""状态机与持久化的完整性：`_execute` 那一段还有几个洞会让「取消」和「落库」同时失效。

覆盖 8 件事：

  M3  任务已进入终态时**绝不能再往下游派活** —— 否则会派活、产生真实副作用
      （写盘、烧额度），却永远不落库。
  M4a 取锁阶段被取消也要落终态。原版 `await lock.acquire()` 在 try 之外，
      在那里被取消会让任务永远停在活跃态：有活跃态、无 worker、无终态。
  M4b 结算阶段落库失败同样要落终态。原版事件回传 / 终态写入在 try 之外，
      add_message / add_artifact 抛异常会直接冒泡，任务停在 working。
  P2-9 终态必须**先抢**再写产物。反过来会留下「任务已被取消、却带着完整
      response artifact」的矛盾记录 —— 「假成功」的另一种形态。
  P1-4 调用方取消同步请求时，必须把取消**转嫁给后台任务**再抛出。
      原版只 `pass` 掉：调用方以为取消了，任务其实在后台跑完。
  M1  add_message 遇任何异常都要先 ROLLBACK。否则连接卡在未提交事务里，
      下一次 BEGIN IMMEDIATE 会以 "cannot start a transaction within a
      transaction" 失败 —— 一次脏数据毒化后续全部请求。
  P2-1 缺省 planId 必须是唯一的。原版 `abs(hash(...))` 会让同一进程里
      step id 列表相同的两个并发 plan 拿到同一个 planId，审计混在一起。
  P2-7 兜底取消不许给已完成的任务追加「已取消」消息。

用受控假适配器，不依赖真实 CLI、不依赖网络。
跑法：python tests/test_state_integrity.py
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

from a2a_hub.adapters.base import Adapter, CallResult, Event                # noqa: E402
from a2a_hub.core.hub_app import Hub                                       # noqa: E402
from a2a_hub.core.registry import AgentRecord, Registry                    # noqa: E402
from a2a_hub.core.router import Router                                     # noqa: E402
from a2a_hub.core.store import Store                                       # noqa: E402


class SpyAdapter(Adapter):
    """记录被调用了几次。用来断言「有没有真的派活」。"""

    kind = "test"

    def __init__(self, name: str = "spy"):
        super().__init__(name)
        self.calls = 0

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt: str, *, context_id=None, session_id=None,
                   timeout: float = 600.0) -> CallResult:
        self.calls += 1
        return CallResult(ok=True, text=f"ok:{prompt[:20]}",
                          events=[Event(kind="status", text="ran")])


class BlockingAdapter(Adapter):
    """一直阻塞，直到被取消。"""

    kind = "test"

    def __init__(self, name: str = "blocker"):
        super().__init__(name)
        self.entered = asyncio.Event()
        self.cancelled = False

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
        return CallResult(ok=True, text="should-not-happen")


def fresh_db(tag: str) -> str:
    db = os.path.join(tempfile.gettempdir(), f"state_integrity_{tag}.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass
    return db


def build_hub(db_path: str, adapters: dict[str, Adapter]) -> tuple[Hub, Store, Registry]:
    store = Store(db_path)
    registry = Registry(store)
    for name in adapters:
        registry.register(AgentRecord(name=name, kind="test", tags=[name]))
    hub = Hub(store, registry, Router(registry), adapters)
    return hub, store, registry


def report(checks: dict[str, bool]) -> bool:
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


# ---------------------------------------------------------------- M3


async def test_terminal_task_not_dispatched() -> bool:
    print("\n[M3] 已进入终态的任务不许再派活")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("m3"), {"spy": spy})

    # 模拟：编排层预落的任务，在被派发前就已被取消
    store.create_task(context_id="", prompt="go", state="canceled",
                      task_id="t-m3", plan_id="p-m3", step_id="s1")
    await hub.dispatch_task("go", agent="spy", task_id="t-m3")

    task = store.get_task("t-m3")
    msgs = [m for m in store.list_messages("t-m3")]
    skipped = any("已进入终态" in (m.get("content") or [{}])[0].get("text", "")
                  for m in msgs)
    return report({
        "下游一次都没被调用（M3 核心）": spy.calls == 0,
        "终态未被改写": task["state"] == "canceled",
        "留下了 skipped 记录（审计可见）": skipped,
    })


# ---------------------------------------------------------------- M4a


async def test_cancel_while_waiting_for_lock() -> bool:
    print("\n[M4a] 在取锁阶段被取消，也必须落终态")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("m4a"), {"spy": spy})

    lock = hub._context_lock("ctx-m4a", "spy")
    await lock.acquire()                      # 占住锁，让 _execute 卡在 acquire()

    send = asyncio.create_task(
        hub.dispatch_task("go", agent="spy", context_id="ctx-m4a")
    )
    await asyncio.sleep(0.15)
    tid = next(iter(hub._running))
    print(f"  任务卡在取锁阶段: {store.get_task(tid)['state']}")

    send.cancel()
    try:
        await asyncio.wait_for(send, timeout=5)
    except (asyncio.CancelledError, Exception):   # noqa: BLE001
        pass
    lock.release()
    await asyncio.sleep(0.2)

    state = store.get_task(tid)["state"]
    print(f"  取消后终态: {state}")
    return report({
        "任务没有停在活跃态（M4a 核心）": state not in ("submitted", "working"),
        "终态是 canceled": state == "canceled",
    })


# ---------------------------------------------------------------- M4b


async def test_settlement_failure_still_settles() -> bool:
    print("\n[M4b] 结算阶段落库失败，也要落终态（不许停在 working）")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("m4b"), {"spy": spy})

    def boom(*a, **k):
        raise RuntimeError("模拟落库失败")

    store.add_artifact = boom                  # 让结算阶段炸掉

    result = await hub.dispatch_task("go", agent="spy")
    tid = result["id"]
    task = store.get_task(tid)
    msgs = store.list_messages(tid)
    noted = any("结算阶段出错" in (m.get("content") or [{}])[0].get("text", "")
                for m in msgs)

    print(f"  终态: {task['state']}（adapter 调用 {spy.calls} 次）")
    return report({
        "任务没有停在活跃态（M4b 核心）": task["state"] not in ("submitted", "working"),
        "失败没有被静默吞掉（留下了 error 消息）": noted,
    })


# ---------------------------------------------------------------- P2-9


async def test_no_artifact_when_losing_terminal_race() -> bool:
    print("\n[P2-9] 输了终态竞态时，不许写产物")
    spy = SpyAdapter("spy")
    hub, store, registry = build_hub(fresh_db("p29"), {"spy": spy})

    tid = store.create_task(context_id="", prompt="go", state="working")["id"]
    # 模拟：任务在 adapter 返回之后、结算之前被别人取消
    store.update_task(tid, state="canceled", error="canceled by caller",
                      finished=True, only_from=("submitted", "working"))

    record = registry.get("spy")
    await hub._settle_result(tid, record, CallResult(ok=True, text="SHOULD-NOT-STORED"))

    task = store.get_task(tid)
    arts = store.list_artifacts(tid)
    msgs = store.list_messages(tid)
    discarded = any("discarded" in (m.get("content") or [{}])[0].get("text", "")
                    for m in msgs)
    print(f"  终态={task['state']}  产物数={len(arts)}")
    return report({
        "没有留下 response 产物（P2-9 核心）": len(arts) == 0,
        "取消的结论被保留": task["state"] == "canceled",
        "留下了 discarded 记录": discarded,
    })


# ---------------------------------------------------------------- P1-4


async def test_caller_cancel_forwards_to_bg() -> bool:
    print("\n[P1-4] 调用方取消同步请求时，取消要转嫁给后台任务")
    blocker = BlockingAdapter("blocker")
    hub, store, _ = build_hub(fresh_db("p14"), {"blocker": blocker})

    send = asyncio.create_task(hub._send_message({
        "agent": "blocker",
        "message": {"messageId": "m1", "role": "ROLE_USER",
                    "parts": [{"text": "go"}]},
    }))
    await asyncio.wait_for(blocker.entered.wait(), timeout=5)

    send.cancel()
    raised = False
    try:
        await asyncio.wait_for(send, timeout=5)
    except asyncio.CancelledError:
        raised = True
    except Exception:                            # noqa: BLE001
        pass

    await asyncio.sleep(0.3)
    tid = next(iter(store.list_tasks(limit=1)) )["id"] if store.count_tasks() else None
    state = store.get_task(tid)["state"] if tid else "?"
    return report({
        "取消被抛出（没被 pass 吞掉）": raised,
        "取消确实转嫁给了下游（P1-4 核心）": blocker.cancelled,
        "任务已结算": state not in ("submitted", "working"),
    })


# ---------------------------------------------------------------- M1


async def test_add_message_rolls_back_on_any_error() -> bool:
    print("\n[M1] add_message 遇任何异常都要回滚，不许毒化连接")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("m1"), {"spy": spy})
    tid = store.create_task(context_id="", prompt="go")["id"]

    circular: dict = {}
    circular["self"] = circular          # json 序列化不了：ValueError

    raised = None
    try:
        store.add_message(tid, role="agent", kind="text",
                          content=[{"text": "x"}], metadata=circular)
    except Exception as exc:             # noqa: BLE001
        raised = type(exc).__name__
    print(f"  第一次写入抛出: {raised}")

    # 关键：连接必须还能用。原版会卡在未提交事务里，这里会再抛
    # "cannot start a transaction within a transaction"
    ok_after = False
    err_after = ""
    try:
        store.add_message(tid, role="agent", kind="text",
                          content=[{"text": "after"}])
        ok_after = True
    except Exception as exc:             # noqa: BLE001
        err_after = f"{type(exc).__name__}: {exc}"

    # 顺带验证 default=str 兜住了普通不可序列化对象
    class Weird:
        def __str__(self):
            return "weird-obj"

    ok_default = False
    try:
        store.add_message(tid, role="agent", kind="text",
                          content=[{"text": "w"}], metadata={"obj": Weird()})
        ok_default = True
    except Exception:                    # noqa: BLE001
        pass

    if err_after:
        print(f"  第二次写入仍失败: {err_after}")
    return report({
        "不可序列化内容确实抛错": raised is not None,
        "连接未被毒化（M1 核心）": ok_after,
        "default=str 兜住了普通对象": ok_default,
    })


# ---------------------------------------------------------------- P2-1


async def test_default_plan_id_is_unique() -> bool:
    print("\n[P2-1] 缺省 planId 必须唯一（并发同名 plan 不许混审计）")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("p21"), {"spy": spy})

    steps = [{"id": "s1", "agent": "spy", "prompt": "go"}]
    r1, r2 = await asyncio.gather(
        hub._run_plan({"steps": steps}),
        hub._run_plan({"steps": steps}),
    )
    p1, p2 = r1["planId"], r2["planId"]
    print(f"  两次缺省 planId: {p1} / {p2}")

    n1 = len(store.list_plan_tasks(p1))
    n2 = len(store.list_plan_tasks(p2))
    return report({
        "两次 planId 不同（P2-1 核心）": p1 != p2,
        "各自的任务没有混在一起": n1 == 1 and n2 == 1,
    })


# ---------------------------------------------------------------- P2-7


async def test_cancel_fallback_skips_completed_task() -> bool:
    print("\n[P2-7] 兜底取消不许给已完成的任务追加「已取消」消息")
    spy = SpyAdapter("spy")
    hub, store, _ = build_hub(fresh_db("p27"), {"spy": spy})

    tid = store.create_task(context_id="", prompt="go", state="completed")["id"]

    # 模拟竞态：状态更新前读到的是「活跃态」，但库里其实已经完成
    real_get = store.get_task

    def stale_get(task_id):
        t = real_get(task_id)
        if t and task_id == tid:
            t = {**t, "state": "working"}      # 假装读到活跃态
        return t

    store.get_task = stale_get
    try:
        await hub._cancel_task({"id": tid})
    finally:
        store.get_task = real_get

    task = store.get_task(tid)
    msgs = store.list_messages(tid)
    bogus = [m for m in msgs
             if "no active worker" in (m.get("content") or [{}])[0].get("text", "")]

    return report({
        "已完成的状态没被改写": task["state"] == "completed",
        "没有追加误导性的取消消息（P2-7 核心）": len(bogus) == 0,
    })


async def main() -> int:
    cases = [
        ("M3  终态任务不派活", test_terminal_task_not_dispatched),
        ("M4a 取锁阶段取消要落终态", test_cancel_while_waiting_for_lock),
        ("M4b 结算失败要落终态", test_settlement_failure_still_settles),
        ("P2-9 输了竞态不写产物", test_no_artifact_when_losing_terminal_race),
        ("P1-4 调用方取消要转嫁", test_caller_cancel_forwards_to_bg),
        ("M1  add_message 异常回滚", test_add_message_rolls_back_on_any_error),
        ("P2-1 缺省 planId 唯一", test_default_plan_id_is_unique),
        ("P2-7 兜底取消不碰已完成", test_cancel_fallback_skips_completed_task),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, await fn()))
        except Exception as exc:                    # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
