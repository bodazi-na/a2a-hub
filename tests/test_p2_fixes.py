#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证批次 2 的修复（A2A-06/07/08/09/10/13/17/21）。

全部走内存 Store + 假适配器，不依赖真实下游、不依赖网络，
所以可以精确断言每条的行为。

跑法：python tests/test_p2_fixes.py
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

from a2a_hub.adapters.base import Adapter, CallResult          # noqa: E402
from a2a_hub.adapters.cli import CLIAdapter                    # noqa: E402
from a2a_hub.core.hub_app import Hub, clamp_limit, parse_timeout  # noqa: E402
from a2a_hub.core.orchestrator import PlanError, topo_layers, Step, infer_dependencies  # noqa: E402
from a2a_hub.core.registry import AgentRecord, Registry        # noqa: E402
from a2a_hub.core.router import NoRouteError, Router           # noqa: E402
from a2a_hub.core.store import Store                           # noqa: E402


class EchoAdapter(Adapter):
    kind = "test"

    def __init__(self, name="echo"):
        super().__init__(name)
        self.calls = 0
        self.last_timeout = None

    async def probe(self) -> str:
        return "ok"

    async def call(self, prompt, *, context_id=None, session_id=None, timeout=None):
        self.calls += 1
        self.last_timeout = timeout
        return CallResult(ok=True, text=f"echo:{prompt}")


def fresh_hub(adapters=None, **kwargs):
    db = os.path.join(tempfile.gettempdir(), f"p2_{os.urandom(4).hex()}.db")
    store = Store(db)
    registry = Registry(store)
    router = Router(registry)
    ad = adapters or {}
    for name in ad:
        registry.register(AgentRecord(name=name, kind="test", tags=[name]))
    hub = Hub(store, registry, router, ad, **kwargs)
    return hub, store, router


# ---------------------------------------------------------------- A2A-06
def test_a2a_06_limit() -> bool:
    print("\n[A2A-06] 负数 / 超大 limit 必须被收敛")
    cases = [(-1, 50), (0, 50), (None, 50), ("abc", 50), (10, 10), (10**9, 500)]
    ok = True
    for raw, expected in cases:
        got = clamp_limit(raw)
        flag = "✓" if got == expected else "✗"
        if got != expected:
            ok = False
        print(f"    {flag} clamp_limit({raw!r}) = {got}（期望 {expected}）")
    return ok


# ---------------------------------------------------------------- A2A-10
def test_a2a_10_bad_timeout() -> bool:
    print("\n[A2A-10] 非法 timeout 不得留下孤儿任务")
    ok = True
    for bad in ("oops", -5, 0, float("inf")):
        try:
            parse_timeout(bad)
            print(f"    ✗ parse_timeout({bad!r}) 竟然通过了")
            ok = False
        except ValueError:
            print(f"    ✓ parse_timeout({bad!r}) 被拒")
    # 未提供时应返回 None（交给适配器决定，见 A2A-13）
    got = parse_timeout(None)
    print(f"    {'✓' if got is None else '✗'} parse_timeout(None) = {got}（期望 None）")
    ok = ok and got is None
    return ok


async def test_a2a_10_no_orphan() -> bool:
    print("\n[A2A-10] 非法 timeout 时任务表里不应多出记录")
    hub, store, _ = fresh_hub({"echo": EchoAdapter()})
    before = store.count_tasks()
    try:
        await hub._send_message({
            "agent": "echo", "timeout": "oops",
            "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x"}]},
        })
        print("    ✗ 没有抛错")
        return False
    except ValueError as exc:
        after = store.count_tasks()
        ok = after == before
        print(f"    校验先于落库: 任务数 {before} -> {after}  {'✓' if ok else '✗'}")
        print(f"    错误信息: {exc}")
        return ok


# ---------------------------------------------------------------- A2A-13
async def test_a2a_13_timeout_passthrough() -> bool:
    print("\n[A2A-13] Hub 未指定 timeout 时，应传 None 给适配器")
    adapter = EchoAdapter("echo")
    hub, store, _ = fresh_hub({"echo": adapter})
    await hub._send_message({
        "agent": "echo",
        "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x"}]},
    })
    ok = adapter.last_timeout is None
    print(f"    适配器收到的 timeout = {adapter.last_timeout!r}（期望 None，由适配器用自己的配置）")
    print("    ->", "PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------- A2A-17
def test_a2a_17_fail_closed() -> bool:
    print("\n[A2A-17] 全部 down 时应拒绝路由（fail-closed）")
    hub, store, router = fresh_hub({"a": EchoAdapter("a")})
    store.set_agent_health("a", "down")
    try:
        router.route()
        print("    ✗ 仍然选中了 down 的节点")
        return False
    except NoRouteError as exc:
        print(f"    ✓ 拒绝路由: {str(exc)[:80]}")

    # unknown 仍应可回退（未探测 ≠ 已知故障）
    store.set_agent_health("a", "unknown")
    try:
        rec = router.route()
        print(f"    ✓ unknown 可回退到 {rec.name}")
        return True
    except NoRouteError:
        print("    ✗ unknown 也被拒了（过严）")
        return False


# ---------------------------------------------------------------- A2A-21
def test_a2a_21_self_reference() -> bool:
    print("\n[A2A-21] 自引用模板应在依赖推断阶段报错")
    steps = [Step.from_dict({"id": "a", "prompt": "用 {{steps.a}} 解释一下"})]
    try:
        infer_dependencies(steps)
        print("    ✗ 没有报错")
        return False
    except PlanError as exc:
        print(f"    ✓ 报错: {exc}")

    # 引用不存在的 step 同样要拦
    steps2 = [Step.from_dict({"id": "a", "prompt": "{{steps.nope}}"})]
    try:
        infer_dependencies(steps2)
        print("    ✗ 引用不存在的 step 没有报错")
        return False
    except PlanError as exc:
        print(f"    ✓ 报错: {exc}")
        return True


# ---------------------------------------------------------------- A2A-09
def test_a2a_09_orphan_recovery() -> bool:
    print("\n[A2A-09] 启动时应结算失去 worker 的活跃任务")
    hub, store, _ = fresh_hub({"echo": EchoAdapter()})
    t = store.create_task(context_id="c", prompt="x", state="working")
    print(f"    造一个 working 的孤儿任务: {t['id'][:8]}…")
    n = hub.recover_orphans()
    after = store.get_task(t["id"])
    ok = n == 1 and after["state"] == "failed" and "interrupted" in (after["error"] or "")
    print(f"    结算 {n} 个；状态 -> {after['state']}；error -> {str(after['error'])[:60]}")
    print("    ->", "PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------- A2A-08
def test_a2a_08_config_change() -> bool:
    print("\n[A2A-08] 同名 agent 的配置变更后应重建适配器")
    # 注意：这里**不注入**适配器 —— 注入的实例签名是空串（无条件复用），
    # 测不到「按配置签名重建」这条路径。要让 hub 自己构建。
    hub, store, router = fresh_hub()
    store.upsert_agent(name="a", kind="a2a_http", endpoint="http://127.0.0.1:9001",
                       tags=["a"], enabled=True)
    rec1 = hub.registry.get("a")
    ad1 = hub._get_adapter(rec1)
    print(f"    首次构建: endpoint={getattr(ad1, 'endpoint', None)}")

    # 同配置再取一次，应当命中缓存（同一个对象）
    ad1b = hub._get_adapter(hub.registry.get("a"))
    print(f"    同配置复用缓存: {ad1 is ad1b}")

    store.upsert_agent(name="a", kind="a2a_http", endpoint="http://127.0.0.1:9002",
                       tags=["a"], enabled=True)
    rec2 = hub.registry.get("a")
    ad2 = hub._get_adapter(rec2)
    print(f"    改 endpoint 后: {getattr(ad2, 'endpoint', None)}")

    ok = (
        ad1 is ad1b
        and ad1 is not ad2
        and getattr(ad2, "endpoint", None) == "http://127.0.0.1:9002"
    )
    print("    ->", "PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------- A2A-07
async def test_a2a_07_plan_completeness() -> bool:
    print("\n[A2A-07] 未执行的 step 也必须在库里留痕")
    hub, store, _ = fresh_hub({"echo": EchoAdapter()})
    plan = await hub._run_plan({
        "contextId": "c",
        "steps": [
            {"id": "bad", "agent": "不存在的agent", "prompt": "x"},
            {"id": "after", "prompt": "依赖 {{steps.bad}}"},
        ],
    })
    plan_id = plan["planId"]
    tasks = store.list_plan_tasks(plan_id)
    ids = sorted(t["step_id"] for t in tasks)
    print(f"    RunPlan 返回步骤: {sorted(s['id'] for s in plan['steps'])}")
    print(f"    库里 plan 的步骤: {ids}")
    ok = ids == ["after", "bad"]
    print("    ->", "PASS" if ok else "FAIL（未执行的 step 没有落库）")
    return ok


async def main() -> int:
    checks = {
        "A2A-06": test_a2a_06_limit(),
        "A2A-10a": test_a2a_10_bad_timeout(),
        "A2A-10b": await test_a2a_10_no_orphan(),
        "A2A-13": await test_a2a_13_timeout_passthrough(),
        "A2A-17": test_a2a_17_fail_closed(),
        "A2A-21": test_a2a_21_self_reference(),
        "A2A-09": test_a2a_09_orphan_recovery(),
        "A2A-08": test_a2a_08_config_change(),
        "A2A-07": await test_a2a_07_plan_completeness(),
    }
    print("\n==== 汇总 ====")
    for name, ok in checks.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
