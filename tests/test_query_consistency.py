#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查询一致性：三处「缺事务边界」造成的读写问题。

清单里是三条独立条目，但根因是同一个：`Store` 用 `isolation_level=None`
（自动提交），**每条语句各自一个隐式事务**。于是：

  P1-6  metadata「合并」= 「`get_task` 读 → `update_task` 整列替换」两步。
        两个并发写者会互相覆盖 —— 后写的把前一个刚加的键整列冲掉。
  P2-12 `update_task` 的 UPDATE 与随后的回读是两条独立语句。
        返回的 task 可能与 `_updated` 对不上（调用方以为「状态归我了」，
        看到的却是别人改过的状态）。
  P2-23 `list_tasks` + `count_tasks`、`get_task` + `list_messages` +
        `list_artifacts` 各自独立查询。WAL 下没有跨语句快照，会出现
        **撕裂读** —— 典型症状是「状态已终态，但 artifacts 还没到齐」。

撕裂读怎么确定性复现：开**第二个连接**，在读序列的两次查询之间写一笔。
WAL 下写不阻塞读，所以这笔写会立刻生效；有读事务时读方看不到它（快照），
没有读事务时就会看到 —— 这正是撕裂。

跑法：python tests/test_query_consistency.py
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


class Quick(Adapter):
    kind = "test"

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt, *, context_id=None, session_id=None,
                   timeout: float = 600.0) -> CallResult:
        return CallResult(ok=True, text="done",
                          events=[Event(kind="status", text="ran")])


def fresh(tag: str) -> str:
    db = os.path.join(tempfile.gettempdir(), f"query_consistency_{tag}.db")
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


def build(db: str) -> tuple[Hub, Store]:
    store = Store(db)
    registry = Registry(store)
    registry.register(AgentRecord(name="q", kind="test", tags=["q"]))
    return Hub(store, registry, Router(registry), {"q": Quick("q")}), store


# ---------------------------------------------------------------- P1-6


async def test_merge_semantics() -> bool:
    print("\n[P1-6] 原子合并的语义：保留旧键、加新键、覆盖同名键")
    store = Store(fresh("merge"))
    tid = store.create_task(context_id="", prompt="t",
                            metadata={"a": 1, "b": 2})["id"]

    store.merge_task_metadata(tid, {"b": 99, "c": 3})
    md = store.get_task(tid)["metadata"]
    print(f"  {md}")

    # null 必须是「设成 null」，不是「删掉」——
    # SQL 的 json_patch 是 RFC 7396 语义（null 即删除），所以没用它
    store.merge_task_metadata(tid, {"c": None})
    md2 = store.get_task(tid)["metadata"]
    print(f"  {md2}")
    return report({
        "旧键保留": md.get("a") == 1,
        "新键加上": md.get("c") == 3,
        "同名键被覆盖": md.get("b") == 99,
        "null 是「设成 null」而不是「删除」": "c" in md2 and md2["c"] is None,
    })


async def test_merge_keeps_other_writers_keys() -> bool:
    print("\n[P1-6] 两个写者各自合并，谁的键都不能丢")
    store = Store(fresh("merge2"))
    tid = store.create_task(context_id="", prompt="t", metadata={"base": 0})["id"]

    # 交替合并 —— 旧的「读 + 整列替换」写法在这种交替下会丢键
    for i in range(10):
        store.merge_task_metadata(tid, {f"k{i}": i})
    md = store.get_task(tid)["metadata"]
    missing = [f"k{i}" for i in range(10) if f"k{i}" not in md]
    print(f"  合并后键数 {len(md)}，缺 {missing or '无'}")
    return report({
        "所有写者的键都在": not missing,
        "原有键仍在": md.get("base") == 0,
    })


async def test_hub_uses_atomic_merge() -> bool:
    print("\n[P1-6] hub 的两处 metadata 写入必须走原子合并，而不是「读+整列替换」")
    hub, store = build(fresh("merge3"))

    merged: list[dict] = []
    replaced: list[dict] = []
    real_merge = store.merge_task_metadata
    real_update = store.update_task

    def spy_merge(task_id, patch):
        merged.append(patch)
        return real_merge(task_id, patch)

    def spy_update(task_id, **kw):
        if kw.get("metadata") is not None:
            replaced.append(kw["metadata"])
        return real_update(task_id, **kw)

    store.merge_task_metadata = spy_merge        # type: ignore[method-assign]
    store.update_task = spy_update               # type: ignore[method-assign]
    try:
        # ① 编排占位任务复用路径
        await hub.dispatch_task("go", agent="q", task_id=store.create_task(
            context_id="", prompt="go", state="submitted", task_id="ph-1",
        )["id"], plan_id="p", step_id="s")
        # ② 孤儿恢复路径
        store.create_task(context_id="", prompt="o", state="working",
                          task_id="orphan-1")
        hub.recover_orphans()
    finally:
        store.merge_task_metadata = real_merge   # type: ignore[method-assign]
        store.update_task = real_update          # type: ignore[method-assign]

    print(f"  原子合并调用 {len(merged)} 次；整列替换调用 {len(replaced)} 次")
    return report({
        "两条路径都用了原子合并（P1-6 核心）": len(merged) >= 2,
        "没有任何地方再用整列替换写 metadata": not replaced,
    })


# ---------------------------------------------------------------- P2-12


async def test_update_task_update_and_reread_consistent() -> bool:
    print("\n[P2-12] update_task 被 only_from 挡下时，_updated 与回读状态必须一致")
    store = Store(fresh("upd"))
    tid = store.create_task(context_id="", prompt="t", state="working")["id"]

    out = store.update_task(tid, state="completed", finished=True,
                            only_from=("submitted",))
    print(f"  _updated={out['_updated']}  实际 state={out['state']}")
    return report({
        "被挡下时 _updated=False": out["_updated"] is False,
        "回读到的就是库里的真实状态（不撕裂）": out["state"] == "working",
        "库里状态没被改": store.get_task(tid)["state"] == "working",
    })


# ---------------------------------------------------------------- P2-23


async def test_task_payload_is_a_snapshot() -> bool:
    print("\n[P2-23] 拼 task payload 的三个查询必须来自同一个快照")
    db = fresh("snap")
    hub, store = build(db)
    other = Store(db)                       # 第二个连接：模拟并发写者

    tid = store.create_task(context_id="", prompt="t")["id"]
    store.add_artifact(tid, name="response", content=[{"text": "EARLY"}])

    real_list_messages = store.list_messages

    def list_messages_with_interleaved_write(task_id):
        msgs = real_list_messages(task_id)
        # 在读序列的**中间**插一笔写 —— WAL 下写不阻塞读，会立刻生效
        other.add_artifact(task_id, name="late", content=[{"text": "LATE"}])
        return msgs

    store.list_messages = list_messages_with_interleaved_write   # type: ignore
    try:
        payload = hub._task_payload(tid)
    finally:
        store.list_messages = real_list_messages                 # type: ignore

    names = [a.get("name") for a in payload.get("artifacts") or []]
    db_names = [a["name"] for a in other.list_artifacts(tid)]
    print(f"  payload 里的产物名: {names}")
    print(f"  库里实际的产物名  : {db_names}")

    return report({
        "读到的是一致快照（看不到中途插入的产物，P2-23 核心）":
            "late" not in names,
        "但库里确实有那笔写（证明插入真的发生了）":
            "late" in db_names,
        "原有产物仍在": "response" in names,
    })


async def test_admin_tasks_list_and_total_agree() -> bool:
    print("\n[P2-23] 任务列表与总数必须来自同一个快照")
    db = fresh("adm")
    hub, store = build(db)
    other = Store(db)

    for i in range(3):
        store.create_task(context_id="", prompt=f"t{i}")

    real_list = store.list_tasks

    def list_with_interleaved_write(**kw):
        rows = real_list(**kw)
        other.create_task(context_id="", prompt="LATE")   # 中途插入
        return rows

    store.list_tasks = list_with_interleaved_write           # type: ignore
    try:
        with store.read_txn():
            rows = store.list_tasks(limit=50)
            total = store.count_tasks()
    finally:
        store.list_tasks = real_list                         # type: ignore

    print(f"  列表 {len(rows)} 条，total={total}")
    return report({
        "列表与总数一致（P2-23 核心）": len(rows) == total,
        "中途插入的那条没进快照": total == 3,
        "库里确实多了一条（证明插入发生了）": other.count_tasks() == 4,
    })


# ---------------------------------------------------------------- 事务保护


async def test_nested_transaction_raises_clearly() -> bool:
    print("\n[保护] 事务不可重入 —— 嵌套时给一句能看懂的报错")
    store = Store(fresh("nest"))
    msg = ""
    try:
        with store.read_txn():
            store._write_txn(lambda: None)
    except RuntimeError as exc:
        msg = str(exc)
    except Exception as exc:                                 # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
    print(f"  {msg[:90]}")
    return report({
        "抛的是 RuntimeError 而不是 SQLite 的模糊报错": "不可重入" in msg,
        "报错里说清了怎么办": "合成一个" in msg,
    })


async def test_transaction_is_released_after_use() -> bool:
    print("\n[保护] 事务用完必须释放 —— 否则后续写入全落在同一事务里")
    store = Store(fresh("release"))
    tid = store.create_task(context_id="", prompt="t")["id"]

    with store.read_txn():
        store.get_task(tid)
    ok_read = not store._conn.in_transaction

    try:
        store.merge_task_metadata(tid, {"x": 1})
        ok_write = True
    except Exception:                                        # noqa: BLE001
        ok_write = False

    # 读事务里抛异常也要释放
    try:
        with store.read_txn():
            raise ValueError("boom")
    except ValueError:
        pass
    ok_after_error = not store._conn.in_transaction

    return report({
        "读事务正常结束后已提交": ok_read,
        "之后还能正常写": ok_write,
        "读事务里抛异常也已回滚": ok_after_error,
    })


async def main() -> int:
    cases = [
        ("P1-6  合并语义", test_merge_semantics),
        ("P1-6  不丢别人的键", test_merge_keeps_other_writers_keys),
        ("P1-6  hub 走原子合并", test_hub_uses_atomic_merge),
        ("P2-12 UPDATE 与回读一致", test_update_task_update_and_reread_consistent),
        ("P2-23 payload 是一致快照", test_task_payload_is_a_snapshot),
        ("P2-23 列表与总数一致", test_admin_tasks_list_and_total_agree),
        ("保护 事务不可重入", test_nested_transaction_raises_clearly),
        ("保护 事务用完释放", test_transaction_is_released_after_use),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, await fn()))
        except Exception as exc:                             # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
