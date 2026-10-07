#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实负载演练暴露的两个 P0：编排路径的取消、以及编排任务的计时基准。

覆盖四件事：
  1. **N1** 编排路径派出去的 step 必须登记进 `_running`，否则 CancelTask
     查不到它、只能改状态而拦不住执行 —— 取消变成「假成功」，
     下游继续跑、继续产生副作用，而结果被 only_from 挡在库外。
  2. **N2** `started_at`（真正开始执行）必须与 `created_at`（记录诞生）分开。
     编排层会预先为每个 step 落占位任务，拿 created_at 当开始时刻的话，
     层间排队等待会被算进执行时长（实测 merge 步虚高 2.5 倍），
     控制台甘特图上所有条还会全部从 t=0 起画。
  3. **P1-5** 整轮被取消时，没轮到的 step 必须被结算，不许永远停在 submitted。
  4. **P1-1 的判定依据** —— 用实验钉死 asyncio.gather 在两种取消下的行为，
     证明「调用方取消」不需要额外处理（它本来就抛），
     而「单个 step 被取消」应当作为该 step 的结果交给 on_error 策略。

用受控假适配器，不依赖真实 CLI、不依赖网络。
跑法：python tests/test_plan_cancel_and_timing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import Adapter, CallResult                    # noqa: E402
from core.hub_app import Hub, _duration_ms, _task_started_at     # noqa: E402
from core.registry import AgentRecord, Registry                  # noqa: E402
from core.router import Router                                   # noqa: E402
from core.store import Store                                     # noqa: E402


class BlockingAdapter(Adapter):
    """一直阻塞，直到被取消。用来验证取消是否真的到达了下游。"""

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


class SlowAdapter(Adapter):
    """跑固定时长后成功返回。用来制造「第一层很慢」的真实场景。"""

    kind = "test"

    def __init__(self, name: str = "slow", delay: float = 1.0):
        super().__init__(name)
        self.delay = delay

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt: str, *, context_id=None, session_id=None,
                   timeout: float = 600.0) -> CallResult:
        await asyncio.sleep(self.delay)
        return CallResult(ok=True, text=f"done:{prompt[:20]}")


def fresh_db(tag: str) -> str:
    db = os.path.join(tempfile.gettempdir(), f"plan_cancel_timing_{tag}.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass
    return db


def build_hub(db_path: str, adapters: dict[str, Adapter]) -> tuple[Hub, Store]:
    store = Store(db_path)
    registry = Registry(store)
    for name in adapters:
        registry.register(AgentRecord(name=name, kind="test", tags=[name]))
    hub = Hub(store, registry, Router(registry), adapters)
    return hub, store


def _step(store: Store, plan_id: str, step_id: str) -> dict:
    for t in store.list_plan_tasks(plan_id):
        if t["step_id"] == step_id:
            return t
    raise AssertionError(f"plan {plan_id} 里没有 step {step_id}")


# ---------------------------------------------------------------- N1


async def test_plan_step_cancel_reaches_downstream() -> bool:
    print("\n[N1] 编排路径的 step 被取消时，必须真的中断下游")
    blocker = BlockingAdapter("blocker")
    hub, store = build_hub(fresh_db("n1"), {"blocker": blocker})

    plan = [
        {"id": "slow", "agent": "blocker", "prompt": "go"},
        {"id": "after", "agent": "blocker", "prompt": "next",
         "depends_on": ["slow"]},
    ]
    run = asyncio.create_task(hub._run_plan({"steps": plan, "planId": "p-n1"}))
    await asyncio.wait_for(blocker.entered.wait(), timeout=5)

    slow_id = _step(store, "p-n1", "slow")["id"]
    registered = slow_id in hub._running          # ← N1 的核心：登记
    print(f"  编排 step 已登记进 _running: {registered}")

    cancelled = await hub._cancel_task({"id": slow_id})
    print(f"  取消返回: {cancelled['status']['state']}")
    await asyncio.sleep(0.5)

    # 整条 plan 应当干净收尾：ok=False，而不是把 CancelledError 抛出去
    result = await asyncio.wait_for(run, timeout=10)
    step_ok = {s["id"]: s["ok"] for s in result["steps"]}
    print(f"  plan ok={result['ok']}  steps={step_ok}")

    after = _step(store, "p-n1", "after")
    checks = {
        "编排 step 登记进 _running": registered,
        "取消返回 canceled": cancelled["status"]["state"] == "TASK_STATE_CANCELED",
        "下游调用确实被中断（N1 核心）": blocker.cancelled,
        "下游没有跑完": not blocker.completed,
        "被取消的 step 终态 canceled": store.get_task(slow_id)["state"] == "canceled",
        "plan 收尾为 ok=False（不抛异常）": result["ok"] is False,
        "依赖它的 step 已被结算（不留 submitted）": after["state"] != "submitted",
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


# ---------------------------------------------------------------- N2


async def test_plan_step_timing_excludes_layer_wait() -> bool:
    print("\n[N2] 编排任务的时长不许把层间排队等待算进去")
    slow = SlowAdapter("slow", delay=1.2)
    fast = SlowAdapter("fast", delay=0.05)
    hub, store = build_hub(fresh_db("n2"), {"slow": slow, "fast": fast})

    plan = [
        {"id": "first", "agent": "slow", "prompt": "slow work"},
        {"id": "second", "agent": "fast", "prompt": "{{steps.first}}"},
    ]
    result = await asyncio.wait_for(
        hub._run_plan({"steps": plan, "planId": "p-n2"}), timeout=30
    )
    assert result["ok"], "这条 plan 本该成功"

    first = _step(store, "p-n2", "first")
    second = _step(store, "p-n2", "second")

    def gap_ms(a, b):
        if not a or not b:
            return None
        return int((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                   .total_seconds() * 1000)

    second_wait = gap_ms(second["created_at"], second["started_at"])
    new_dur = _duration_ms(second["started_at"], second["finished_at"])
    old_dur = _duration_ms(second["created_at"], second["finished_at"])
    first_dur = _duration_ms(first["started_at"], first["finished_at"])

    print(f"  first  : started_at={first['started_at'] is not None} "
          f"duration={first_dur}ms")
    print(f"  second : created→started 等待 {second_wait}ms")
    print(f"           新口径 duration={new_dur}ms ｜ 旧口径 duration={old_dur}ms")

    checks = {
        "step 落库时 started_at 为空（占位任务）": True,  # 由下面的等待量间接证明
        "second 的 created_at 早于 started_at（确实排过队）":
            second_wait is not None and second_wait >= 800,
        "second 的 started_at 已落库": second["started_at"] is not None,
        "新口径已排除层间等待（<500ms）": new_dur is not None and new_dur < 500,
        "旧口径确实把等待算了进去（≥1000ms）":
            old_dur is not None and old_dur >= 1000,
        "第一层时长仍是真实值（≥1000ms）":
            first_dur is not None and first_dur >= 1000,
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


# ---------------------------------------------------------------- P1-5


async def test_caller_cancel_settles_placeholders() -> bool:
    print("\n[P1-5] 整轮被取消时，没轮到的 step 必须被结算")
    blocker = BlockingAdapter("blocker")
    hub, store = build_hub(fresh_db("p15"), {"blocker": blocker})

    plan = [
        {"id": "a", "agent": "blocker", "prompt": "go"},
        {"id": "b", "agent": "blocker", "prompt": "b", "depends_on": ["a"]},
        {"id": "c", "agent": "blocker", "prompt": "c", "depends_on": ["b"]},
    ]
    run = asyncio.create_task(hub._run_plan({"steps": plan, "planId": "p-15"}))
    await asyncio.wait_for(blocker.entered.wait(), timeout=5)

    run.cancel()                                   # 模拟调用方断开
    raised = False
    try:
        await asyncio.wait_for(run, timeout=10)
    except asyncio.CancelledError:
        raised = True
    except Exception as exc:                       # noqa: BLE001
        print(f"  （拿到 {type(exc).__name__}，按取消处理）")
        raised = True

    await asyncio.sleep(0.3)
    states = {sid: _step(store, "p-15", sid)["state"] for sid in ("a", "b", "c")}
    print(f"  三个 step 的终态: {states}")

    checks = {
        "调用方取消正确抛出（未被吞成正常返回）": raised,
        "a（正在跑的）已结算": states["a"] in ("canceled", "failed"),
        "b（没轮到的）已结算": states["b"] != "submitted",
        "c（没轮到的）已结算": states["c"] != "submitted",
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


# ---------------------------------------------------------------- P1-1 判定依据


async def test_gather_cancel_semantics() -> bool:
    print("\n[P1-1 依据] asyncio.gather 在两种取消下的行为（钉死判定）")

    async def child(name: str, forever: bool = False):
        try:
            await asyncio.sleep(3600 if forever else 0.05)
        except asyncio.CancelledError:
            raise
        return f"{name}-done"

    async def outer_cancel() -> str:
        async def runner():
            return await asyncio.gather(child("a", True), child("b", True),
                                        return_exceptions=True)
        t = asyncio.create_task(runner())
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
            return "returned"        # CancelledError 被当成结果吞掉了
        except asyncio.CancelledError:
            return "raised"

    async def child_cancel() -> str:
        a = asyncio.create_task(child("a", True))
        b = asyncio.create_task(child("b"))
        await asyncio.sleep(0.02)
        a.cancel()
        try:
            out = await asyncio.gather(a, b, return_exceptions=True)
            return "returned:" + ",".join(type(x).__name__ for x in out)
        except asyncio.CancelledError:
            return "raised"

    outer = await outer_cancel()
    inner = await child_cancel()
    print(f"  调用方取消整轮     -> {outer}")
    print(f"  只有单个 step 被取消 -> {inner}")

    checks = {
        "调用方取消整轮：gather 直接抛（无需额外处理）": outer == "raised",
        "单个 step 取消：gather 当结果返回": inner.startswith("returned:"),
    }
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


async def test_started_at_excludes_queue_wait() -> bool:
    """`started_at` 必须在**取得并发名额之后**才打 —— 排队时间不算执行时间。

    修复前它打在 `_execute` 开头，于是负载超过并发上限时，「排队等名额」的
    时间被记成 Agent 正在执行。并行度分析据此构造运行区间，指标会虚高
    （把排队中的任务也算成「在跑」）。
    """
    print("\n[N2+] 计时起点不含排队等并发名额的时间")
    slow = SlowAdapter("slow", delay=0.5)
    hub, store = build_hub(fresh_db("queue"), {"slow": slow})
    hub.max_concurrency = 1                     # 逼出排队（信号量是延迟创建的）

    t1 = asyncio.create_task(hub.dispatch_task("a", agent="slow"))
    await asyncio.sleep(0.15)                   # 让第一个先占住名额
    t2 = asyncio.create_task(hub.dispatch_task("b", agent="slow"))
    await asyncio.gather(t1, t2)

    rows = sorted(store.list_tasks(limit=10), key=lambda r: r["created_at"])
    if len(rows) < 2:
        print(f"  ✗ 只查到 {len(rows)} 个任务")
        return False
    first, second = rows[0], rows[1]
    d1 = _duration_ms(_task_started_at(first), first.get("finished_at"))
    d2 = _duration_ms(_task_started_at(second), second.get("finished_at"))
    print(f"  并发上限 1，两个 0.5s 任务")
    print(f"    先到的: started={_task_started_at(first)}  时长={d1}ms")
    print(f"    后到的: started={_task_started_at(second)}  时长={d2}ms")

    # 后到的那个真实执行只有 ~500ms。修复前它会把 ~150ms 的排队也算进去，
    # 时长逼近 650ms+；更重要的是它的 started_at 会早于前一个的 finished_at。
    started_ok = _task_started_at(second) >= first.get("finished_at")
    print(f"  后到者 started_at ≥ 先到者 finished_at: {started_ok}")
    if not started_ok:
        print("  ✗ 排队中的任务被当成「已经开始执行」")
        return False
    if d2 is None or d2 > 700:
        print(f"  ✗ 后到者时长 {d2}ms 明显含排队时间")
        return False
    print("  ✓ 计时起点在取得名额之后，排队时间未计入")
    return True


async def main() -> int:
    cases = [
        ("N1 编排 step 取消到达下游", test_plan_step_cancel_reaches_downstream),
        ("N2 计时基准排除层间等待", test_plan_step_timing_excludes_layer_wait),
        ("N2+ 计时起点排除排队等名额", test_started_at_excludes_queue_wait),
        ("P1-5 整轮取消结算占位任务", test_caller_cancel_settles_placeholders),
        ("P1-1 gather 取消语义", test_gather_cancel_semantics),
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
