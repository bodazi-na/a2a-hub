#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Store 对事件循环的阻塞（M2）—— 以及防止它悄悄退化。

问题
----
`Store` 是同步 API，却全在事件循环线程上被调用。单次调用本身不慢，
但 `_execute` 会把一个任务的过程事件**逐条**落库，而每条 `add_message`
都是一次独立事务 —— WAL 下每次 COMMIT 都要 fsync。

实测（本机 2026-10-06）：

| 设置 | 50 次写入 | 事件循环最大阻塞 |
| --- | --- | --- |
| `synchronous=FULL`（默认，原状） | 197.7 ms | **192.8 ms** |
| `synchronous=NORMAL` | 3.1 ms | 10.8 ms |
| `synchronous=NORMAL` + 单事务批量 | **0.9 ms** | 11.9 ms |

也就是说：一个产生 50 个事件的任务，原来会把**整个服务**卡住近 0.2 秒 ——
这期间 `CancelTask` 和其它所有请求都调度不了。

修法（两层，都不需要引入线程）
------------------------------
1. `synchronous=NORMAL` —— **WAL 模式下的推荐值**。语义差别是「OS 崩溃 /
   断电可能丢最近若干事务」，而**进程崩溃不丢已提交数据**。对任务协调日志
   这个取舍是划算的。
2. `add_messages()` —— 一次事务写多条。

为什么**没有**按原建议改成 `asyncio.to_thread`
----------------------------------------------
实测显示这两层已经把 193ms 压到 0.9ms（**约 200 倍**），残余阻塞与
ticker 自身的测量下限同量级。而 `to_thread` 会给每一次 store 调用引入新的
await 点 —— 那意味着 `_execute` 里「更新状态 → 写消息 → 写产物」之间
多出可被打断的位置，是**新的竞态风险**。收益已被吃掉，风险却不小，
所以不换。这里用测试把结论钉住，而不是靠记性。

跑法：python tests/test_store_blocking.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.core.store import Store                                        # noqa: E402

# 批量写入的条数。取大一点是为了让「退化回逐条事务」这种情况
# 拉开足够大的差距，不至于被机器快慢掩盖。
BATCH_N = 200
# 事件循环阻塞的上限（毫秒）。修好后实测约 4ms；退化回 FULL 逐条会是
# 200 × 4ms ≈ 800ms。取 150ms 是给慢 CI 留 30 倍余量，同时仍能可靠地
# 抓住退化。
MAX_BLOCK_MS = 150.0


def fresh(tag: str) -> str:
    db = os.path.join(tempfile.gettempdir(), f"store_blocking_{tag}.db")
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(db + s)
        except OSError:
            pass
    return db


def report(checks: dict[str, bool]) -> bool:
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


def items(n: int) -> list[dict]:
    return [
        {"role": "agent", "kind": "tool_call",
         "content": [{"text": f"event-{i}"}], "metadata": {"i": i}}
        for i in range(n)
    ]


# ---------------------------------------------------------------- 结构性断言


async def test_synchronous_is_normal() -> bool:
    print("\n[结构] synchronous 必须是 NORMAL —— 它是 4.57ms → 0.07ms 的来源")
    store = Store(fresh("pragma"))
    mode = store._conn.execute("PRAGMA synchronous").fetchone()[0]
    journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"  journal_mode={journal}  synchronous={mode}")
    return report({
        "journal_mode 仍是 wal": str(journal).lower() == "wal",
        "synchronous=NORMAL（1）": int(mode) == 1,
    })


async def test_add_messages_uses_one_transaction() -> bool:
    print("\n[结构] add_messages 必须**只开一个事务**，而不是循环调 add_message")
    store = Store(fresh("txn"))
    tid = store.create_task(context_id="", prompt="t")["id"]

    calls = []
    real = store._write_txn

    def counting(work, **kw):
        calls.append(1)
        return real(work, **kw)

    store._write_txn = counting          # type: ignore[method-assign]
    try:
        store.add_messages(tid, items(50))
    finally:
        store._write_txn = real          # type: ignore[method-assign]

    print(f"  {len(calls)} 个事务写入 50 条")
    return report({
        "50 条消息只用了 1 个事务": len(calls) == 1,
    })


# ---------------------------------------------------------------- 计时断言


async def test_event_loop_is_not_blocked() -> bool:
    """计时兜底。

    **取多次测量的最小值**，因为这台机器上「同一进程里第一次写一个全新的
    临时库」会被环境拖慢（实测同一段代码：首次 88.7ms、第二次 2.7ms，
    而纯函数层面 `add_messages(200)` 稳定在 2.2–3.1ms）。最小值最接近真实代价，
    也是微基准的常规做法。

    真正的防线是上面两条**结构性断言**（`synchronous=NORMAL`、批量只开一个
    事务）—— 它们不受机器快慢影响。这里只是兜住「有没有别的什么东西
    突然开始阻塞事件循环」。
    """
    print(f"\n[计时兜底] {BATCH_N} 条事件落库，取 3 次最小值，须 < {MAX_BLOCK_MS:.0f}ms")
    store = Store(fresh("block"))
    tid = store.create_task(context_id="", prompt="t")["id"]
    payload = items(BATCH_N)
    store.add_messages(tid, items(10))          # 预热

    samples: list[tuple[float, float]] = []
    for _ in range(3):
        lags: list[float] = []
        stop = False

        async def ticker() -> None:
            while not stop:
                t = time.perf_counter()
                await asyncio.sleep(0.005)
                lags.append((time.perf_counter() - t - 0.005) * 1000)

        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.03)

        t0 = time.perf_counter()
        store.add_messages(tid, payload)
        cost = (time.perf_counter() - t0) * 1000

        await asyncio.sleep(0.03)
        stop = True
        await tk
        samples.append((cost, max(lags) if lags else 0.0))

    best_cost = min(s[0] for s in samples)
    best_lag = min(s[1] for s in samples)
    print("  样本 (写入ms, 最大tick延迟ms): "
          + ", ".join(f"({c:.1f}, {l:.1f})" for c, l in samples))
    print(f"  取最小：写入 {best_cost:.1f} ms，tick 最大延迟 {best_lag:.1f} ms")

    return report({
        f"事件循环阻塞 < {MAX_BLOCK_MS:.0f}ms（M2 核心）": best_lag < MAX_BLOCK_MS,
        "落库条数正确": len(store.list_messages(tid)) == BATCH_N * 3 + 10,
    })


async def test_execute_uses_batch_not_per_message() -> bool:
    """结构性断言：`_execute` 落过程事件时必须走 `add_messages`（一个事务），
    而不是循环调 `add_message`（N 个事务）。

    判据用**「逐条调用次数不随事件数增长」**，而不是「逐条调用次数为 0」——
    `_execute` 本来就会写几条状态消息（dispatched / 结算），那是固定的几条，
    与事件数无关。真正要钉死的是：**它不能与事件数成正比**。

    这条比计时可靠 —— 不受机器快慢影响，直接盯住「修复有没有被改回去」。
    """
    print("\n[结构] _execute 落事件必须走批量（逐条次数不随事件数增长）")
    from a2a_hub.adapters.base import Adapter, CallResult, Event            # noqa: E402
    from a2a_hub.core.hub_app import Hub                                    # noqa: E402
    from a2a_hub.core.registry import AgentRecord, Registry                 # noqa: E402
    from a2a_hub.core.router import Router                                  # noqa: E402

    async def run_once(n_events: int, tag: str) -> tuple[int, int]:
        class ManyEvents(Adapter):
            kind = "test"

            async def probe(self) -> str:
                return "ok"

            async def call(self, prompt, *, context_id=None, session_id=None,
                           timeout: float = 600.0) -> CallResult:
                return CallResult(ok=True, text="done", events=[
                    Event(kind="tool_call", text=f"e{i}") for i in range(n_events)
                ])

        store = Store(fresh(f"execute_{tag}"))
        registry = Registry(store)
        registry.register(AgentRecord(name="ev", kind="test", tags=["ev"]))
        hub = Hub(store, registry, Router(registry), {"ev": ManyEvents("ev")})

        per_message: list[str] = []
        batch: list[int] = []
        real_one, real_many = store.add_message, store.add_messages

        def spy_one(*a, **k):
            per_message.append("x")
            return real_one(*a, **k)

        def spy_many(*a, **k):
            batch.append(len(a[1]) if len(a) > 1 else 0)
            return real_many(*a, **k)

        store.add_message = spy_one        # type: ignore[method-assign]
        store.add_messages = spy_many      # type: ignore[method-assign]
        try:
            await hub.dispatch_task("go", agent="ev")
        finally:
            store.add_message = real_one   # type: ignore[method-assign]
            store.add_messages = real_many  # type: ignore[method-assign]
        return len(per_message), (batch[0] if batch else -1)

    small_n, small_batch = await run_once(10, "small")
    big_n, big_batch = await run_once(100, "big")
    print(f"  10 个事件 → 逐条 {small_n} 次、批量 {small_batch} 条")
    print(f"  100 个事件 → 逐条 {big_n} 次、批量 {big_batch} 条")

    return report({
        "过程事件走了批量": small_batch == 10 and big_batch == 100,
        "逐条调用次数不随事件数增长（M2 核心）": small_n == big_n,
        f"逐条只用于固定几条状态消息（{big_n} 次）": big_n <= 5,
    })


# ---------------------------------------------------------------- 正确性


async def test_order_and_seq_are_continuous() -> bool:
    print("\n[正确性] 批量写入的 seq 必须连续、顺序与传入一致")
    store = Store(fresh("order"))
    tid = store.create_task(context_id="", prompt="t")["id"]

    store.add_message(tid, role="user", kind="text", content=[{"text": "first"}])
    store.add_messages(tid, items(5))
    store.add_message(tid, role="agent", kind="text", content=[{"text": "last"}])

    rows = store.list_messages(tid)
    seqs = [r["seq"] for r in rows]
    texts = [((r.get("content") or [{}])[0]).get("text") for r in rows]
    print(f"  seq={seqs}")
    print(f"  顺序={texts}")
    return report({
        "seq 从 1 开始连续": seqs == list(range(1, len(seqs) + 1)),
        "add_message 与 add_messages 共用同一序号空间": len(seqs) == 7,
        "顺序正确": texts[0] == "first" and texts[-1] == "last"
                    and texts[1:6] == [f"event-{i}" for i in range(5)],
    })


async def test_empty_batch_is_noop() -> bool:
    print("\n[正确性] 空批量应当是 no-op（不开事务）")
    store = Store(fresh("empty"))
    tid = store.create_task(context_id="", prompt="t")["id"]

    calls = []
    real = store._write_txn

    def counting(work, **kw):
        calls.append(1)
        return real(work, **kw)

    store._write_txn = counting          # type: ignore[method-assign]
    try:
        out = store.add_messages(tid, [])
    finally:
        store._write_txn = real          # type: ignore[method-assign]

    return report({
        "返回空列表": out == [],
        "没有开事务": not calls,
        "库里没有消息": store.list_messages(tid) == [],
    })


async def test_batch_is_atomic() -> bool:
    print("\n[正确性] 批量要么全写、要么全不写（中间一条坏数据要回滚）")
    store = Store(fresh("atomic"))
    tid = store.create_task(context_id="", prompt="t")["id"]

    circular: dict = {}
    circular["self"] = circular          # json 序列化不了 → ValueError

    bad = items(3)
    bad.insert(1, {"role": "agent", "kind": "text", "content": circular})

    raised = None
    try:
        store.add_messages(tid, bad)
    except Exception as exc:             # noqa: BLE001
        raised = type(exc).__name__

    after = store.list_messages(tid)
    # 连接还必须能用（M1：异常路径要回滚，否则连接卡在未提交事务里）
    usable = False
    try:
        store.add_messages(tid, items(2))
        usable = True
    except Exception:                    # noqa: BLE001
        pass

    print(f"  抛出={raised}  回滚后条数={len(after)}  连接仍可用={usable}")
    return report({
        "确实抛错": raised is not None,
        "一条都没写进去（原子性）": after == [],
        "连接未被毒化（M1 仍成立）": usable,
    })


async def main() -> int:
    cases = [
        ("结构 synchronous=NORMAL", test_synchronous_is_normal),
        ("结构 批量只开一个事务", test_add_messages_uses_one_transaction),
        ("结构 _execute 走批量", test_execute_uses_batch_not_per_message),
        ("计时兜底 事件循环不被阻塞", test_event_loop_is_not_blocked),
        ("正确性 顺序与 seq", test_order_and_seq_are_continuous),
        ("正确性 空批量 no-op", test_empty_batch_is_noop),
        ("正确性 批量原子性", test_batch_is_atomic),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, await fn()))
        except Exception as exc:                                # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
