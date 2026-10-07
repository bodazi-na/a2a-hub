#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""流式（SSE）回归测试。

锁住三件事：

1. **帧是边跑边到的**，不是跑完一次性涌出。这是「流式」的定义 ——
   修复前过程事件是 `adapter.call` 跑完后批量落库的，客户端要等整轮结束。
2. **事件时间戳是发生时刻**，不是落库时刻（N5）。修复前同一批事件的时间戳
   会挤在几毫秒内：实测 qoder 真跑了 181 秒，事件却落在 64 毫秒之内。
3. **慢订阅者拖不住任务**。队列有界 + 丢最老，任务必须照常跑完。

零外部依赖：用假适配器，不碰真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import (                                      # noqa: E402
    EVENT_TEXT,
    EVENT_THINKING,
    Adapter,
    CallResult,
    Event,
    utcnow,
)
from core.hub_app import ACTIVE_STATES, Hub, SSE_QUEUE_MAX       # noqa: E402
from core.registry import AgentRecord, Registry                  # noqa: E402
from core.router import Router                                   # noqa: E402
from core.store import Store                                     # noqa: E402


class StreamingAdapter(Adapter):
    """分多次吐事件，每次间隔一小段 —— 模拟真实 agent 的过程回放。

    关键在于它**逐条**调用 `on_event`，而不是攒完一起给。真实适配器
    （CLI 的 stream-json 解析）就是这个行为。
    """

    kind = "test"

    def __init__(self, name: str = "streamer", steps: int = 4, gap: float = 0.05):
        super().__init__(name)
        self.steps = steps
        self.gap = gap

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt: str, *, context_id=None, session_id=None,
                   timeout: float = 600.0, on_event=None) -> CallResult:
        events: list[Event] = []
        for i in range(self.steps):
            await asyncio.sleep(self.gap)
            ev = Event(kind=EVENT_THINKING, text=f"step {i}", ts=utcnow())
            events.append(ev)
            if on_event is not None:
                await on_event(ev)          # 逐条推，不攒
        return CallResult(ok=True, text="done", events=events)


class SlowSubscriberAdapter(StreamingAdapter):
    """事件发得很多，用来把订阅者的有界队列撑爆。"""

    def __init__(self, name: str = "flood", steps: int = SSE_QUEUE_MAX + 40):
        super().__init__(name, steps=steps, gap=0.0)


class LegacyAdapter(Adapter):
    """**老签名**的适配器：`call` 没有 `on_event` 参数。

    这是加流式时真实踩到的坑：`_execute` 无条件传 `on_event`，于是所有
    既有的（含第三方）适配器全部 `TypeError` **任务直接失败** ——
    而它们本该只是「没有流式」而已。护栏见 `hub_app._accepts_on_event`。
    """

    kind = "test"

    def __init__(self, name: str = "legacy"):
        super().__init__(name)

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt: str, *, context_id=None, session_id=None,
                   timeout: float = 600.0) -> CallResult:
        return CallResult(ok=True, text="legacy-ok")


def fresh_db(tag: str) -> str:
    db = os.path.join(tempfile.gettempdir(), f"streaming_{tag}.db")
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
    return Hub(store, registry, Router(registry), adapters), store


def _params(agent: str, prompt: str = "go") -> dict:
    return {
        "agent": agent,
        "message": {"messageId": "m1", "role": "ROLE_USER",
                    "parts": [{"text": prompt}]},
    }


def _parse(chunk) -> dict | None:
    """从 SSE 帧里取出 JSON 载荷；心跳返回 None。"""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8")
    for line in chunk.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


async def _drain(resp) -> list[tuple[float, dict]]:
    """消费整个流，记下每帧的到达时刻。"""
    out: list[tuple[float, dict]] = []
    async for chunk in resp.body_iterator:
        payload = _parse(chunk)
        if payload is not None:
            out.append((time.monotonic(), payload))
    return out


# ------------------------------------------------------------------ 用例


async def test_frames_arrive_incrementally() -> bool:
    """核心断言：帧是**边跑边到**的，不是跑完一次性涌出。"""
    print("\n[流式] 帧应当随下游进展陆续到达")
    adapter = StreamingAdapter(steps=4, gap=0.12)
    hub, _store = build_hub(fresh_db("incr"), {"streamer": adapter})

    resp = await hub._send_streaming_message("s1", _params("streamer"))
    if resp.media_type != "text/event-stream":
        print(f"  ✗ media_type 应为 text/event-stream，实际 {resp.media_type}")
        return False

    frames = await _drain(resp)
    data = [p for _t, p in frames if "statusUpdate" in (p.get("result") or {})]
    print(f"  收到 {len(frames)} 帧（其中 statusUpdate {len(data)} 条）")

    if len(frames) < 3:
        print("  ✗ 帧太少，没法判断是否流式")
        return False

    span = frames[-1][0] - frames[0][0]
    print(f"  首帧到末帧跨度 {span:.2f}s（下游共耗时约 {4 * 0.12:.2f}s）")
    # 真流式：跨度应当接近下游的总耗时。
    # 一次性补发的话跨度会趋近 0 —— 这正是修复前的行为。
    if span < 0.25:
        print("  ✗ 所有帧几乎同时到达 —— 像是跑完一次性补发，不是流式")
        return False
    print("  ✓ 帧是分散到达的")
    return True


async def test_event_timestamps_are_occurrence_times() -> bool:
    """N5：事件时间戳必须是**发生时刻**，且真的落进了库。"""
    print("\n[N5] 事件时间戳 = 发生时刻（不是落库时刻）")
    adapter = StreamingAdapter(steps=5, gap=0.08)
    hub, store = build_hub(fresh_db("ts"), {"streamer": adapter})

    resp = await hub._send_streaming_message("s1", _params("streamer"))
    frames = await _drain(resp)

    # 1) 流里带的 ts 应当各不相同、且单调递增
    tss = [
        (p["result"]["statusUpdate"].get("metadata") or {}).get("ts")
        for _t, p in frames
        if "statusUpdate" in (p.get("result") or {})
    ]
    tss = [t for t in tss if t]
    print(f"  流里带 ts 的帧：{len(tss)}")
    if len(tss) < 3:
        print("  ✗ 带 ts 的帧太少")
        return False
    if tss != sorted(tss):
        print("  ✗ ts 不是单调递增的")
        return False
    if len(set(tss)) != len(tss):
        print("  ✗ 有重复的 ts —— 时间戳没有区分度")
        return False

    # 2) 落库的 created_at 应当就是这些 ts（而不是统一的插入时刻）
    task_id = frames[0][1]["result"]["statusUpdate"]["taskId"]
    rows = [m for m in store.list_messages(task_id)
            if m["kind"] == EVENT_THINKING]
    stored = [r["created_at"] for r in rows]
    print(f"  落库的过程事件：{len(stored)} 条")
    if stored != tss:
        print("  ✗ 落库时间戳与事件发生时刻不一致")
        print(f"    流里: {tss[:3]}")
        print(f"    库里: {stored[:3]}")
        return False

    # 3) 跨度应当接近下游真实耗时；修复前这里会趋近 0
    from datetime import datetime
    a, b = datetime.fromisoformat(stored[0]), datetime.fromisoformat(stored[-1])
    span_ms = (b - a).total_seconds() * 1000
    print(f"  事件时间跨度 {span_ms:.0f}ms（下游共耗时约 {5 * 0.08 * 1000:.0f}ms）")
    if span_ms < 150:
        print("  ✗ 事件时间戳挤在一起 —— 又变回「落库时刻」了")
        return False
    print("  ✓ 时间戳是发生时刻，且已落库")
    return True


async def test_terminal_frame_is_full_task() -> bool:
    """流必须以**完整的 Task** 收尾 —— 客户端据此知道结束、并拿到产物。"""
    print("\n[流式] 末帧是完整 Task（含终态与产物）")
    adapter = StreamingAdapter(steps=2, gap=0.03)
    hub, _store = build_hub(fresh_db("final"), {"streamer": adapter})

    resp = await hub._send_streaming_message("s1", _params("streamer"))
    frames = await _drain(resp)

    last = frames[-1][1]["result"]
    if "task" not in last:
        print(f"  ✗ 末帧不是 task：{list(last)}")
        return False
    task = last["task"]
    state = (task.get("status") or {}).get("state")
    print(f"  末帧 task 状态 = {state}")
    if state != "TASK_STATE_COMPLETED":
        print("  ✗ 末帧状态不是 completed")
        return False
    arts = task.get("artifacts") or []
    print(f"  产物数 = {len(arts)}")
    if not arts:
        print("  ✗ 末帧没有带产物 —— 客户端得再发一次 GetTask 才知道结果")
        return False
    print("  ✓ 末帧自洽：状态 + 产物都在")
    return True


async def test_slow_subscriber_does_not_block_task() -> bool:
    """订阅者跟不上时，任务必须照常跑完 —— 队列有界、丢最老。"""
    print("\n[背压] 慢订阅者不能拖住任务")
    adapter = SlowSubscriberAdapter()
    hub, store = build_hub(fresh_db("slow"), {"flood": adapter})

    # 订阅了但**一条都不读**，模拟卡死的客户端
    resp = await hub._send_streaming_message("s1", _params("flood"))
    gen = resp.body_iterator
    first = await gen.__anext__()           # 只取一帧就停手
    if _parse(first) is None:
        print("  （首帧是心跳，继续）")

    # 给任务足够时间跑完，而我们始终不读流
    for _ in range(200):
        await asyncio.sleep(0.02)
        tasks = store.list_tasks(limit=5)
        if tasks and tasks[0]["state"] not in ACTIVE_STATES:
            break

    tasks = store.list_tasks(limit=5)
    state = tasks[0]["state"] if tasks else "?"
    print(f"  订阅者全程不读，任务状态 = {state}")
    await gen.aclose()

    if state != "completed":
        print("  ✗ 任务没跑完 —— 慢订阅者把它拖住了")
        return False
    print("  ✓ 任务照常完成，慢订阅者只影响自己")
    return True


async def test_legacy_adapter_still_works() -> bool:
    """**兼容性护栏**：老签名的适配器（call 没有 on_event）必须照常工作。

    加流式时真实踩到的坑 —— 无条件传 `on_event` 会让所有既有适配器
    `TypeError`，任务**直接失败**。正确行为是：不传，照常跑，只是没有实时流。
    """
    print("\n[兼容] 老签名适配器（无 on_event）不能被流式改动弄挂")
    hub, store = build_hub(fresh_db("legacy"), {"legacy": LegacyAdapter()})

    r = await hub._send_message({"agent": "legacy",
                                 "message": {"parts": [{"text": "go"}]}})
    state = (r["task"].get("status") or {}).get("state")
    print(f"  普通 SendMessage 状态 = {state}")
    if state != "TASK_STATE_COMPLETED":
        print("  ✗ 老适配器挂了 —— 兼容护栏失效")
        return False

    # 流式路径也要能用（会退化成「只有首尾两帧」，但绝不能报错）
    resp = await hub._send_streaming_message("s1", _params("legacy"))
    frames = await _drain(resp)
    last = frames[-1][1]["result"]
    print(f"  流式路径帧数 = {len(frames)}，末帧有 task = {'task' in last}")
    if "task" not in last:
        print("  ✗ 流式路径没能正常收尾")
        return False
    print("  ✓ 老适配器两条路径都正常（只是没有过程事件）")
    return True


async def test_agent_card_declares_streaming() -> bool:
    """Agent Card 必须如实声明 streaming=true —— 以前是 false，因为确实没有。"""
    print("\n[声明] Agent Card 的 streaming 与实现一致")
    adapter = StreamingAdapter(steps=1, gap=0.01)
    hub, _store = build_hub(fresh_db("card"), {"streamer": adapter})
    # 设了 public_url 就不必去碰 request.base_url —— 这里测的是声明，不是 URL 推导
    hub.public_url = "http://127.0.0.1:9200"

    card = json.loads(bytes((await hub.agent_card(None)).body).decode())
    streaming = card["capabilities"]["streaming"]
    print(f"  capabilities.streaming = {streaming}")
    if streaming is not True:
        print("  ✗ 声明与实现不一致")
        return False
    print("  ✓ 声明为 true，且确实实现了")
    return True


async def main() -> int:
    cases = [
        ("帧随进展陆续到达", test_frames_arrive_incrementally),
        ("N5 事件时间戳=发生时刻", test_event_timestamps_are_occurrence_times),
        ("末帧是完整 Task", test_terminal_frame_is_full_task),
        ("慢订阅者不拖住任务", test_slow_subscriber_does_not_block_task),
        ("老签名适配器仍可用（兼容护栏）", test_legacy_adapter_still_works),
        ("Agent Card 声明一致", test_agent_card_declares_streaming),
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
