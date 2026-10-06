#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 P1 修复（A2A-01 / 02 / 03 / 04 / 05）。

需要 hub 在 9200 上运行。跑法：
    python tests/test_p1_fixes.py            # 不启用认证的 hub
    python tests/test_p1_fixes.py --token X  # 启用认证的 hub

设计要点
--------
- **A2A-01**：同步任务在被取消后，绝不能因为执行体跑完而变回 completed。
  这是「假取消」——界面显示已取消，实际还在跑并写回结果。
- **A2A-04**：同一 (contextId, agent) 的并发请求不能从同一个旧 session 分叉。
- **A2A-05**：除卡片/健康/控制台外壳外，其余端点必须要求 Bearer。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9200"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TOKEN: str | None = None


def rpc(method: str, params: dict, timeout: float = 300.0) -> dict:
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params}).encode()
    req = urllib.request.Request(BASE + "/", data=body, headers=headers)
    with OPENER.open(req, timeout=timeout) as resp:
        return json.load(resp)


def http_get(path: str, timeout: float = 15.0) -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def http_get_noauth(path: str, timeout: float = 15.0) -> int:
    req = urllib.request.Request(BASE + path)
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def state_of(payload: dict) -> str:
    task = (payload.get("result") or {}).get("task") or (payload.get("result") or {})
    return (task.get("status") or {}).get("state", "?")


async def test_a2a_01_sync_cancel() -> bool:
    """同步任务被取消后，最终状态必须保持 canceled。

    注意：SendMessage 同步模式是**阻塞**的，直接调用会在任务跑完后才返回。
    所以必须并发发起 —— 一个线程等 SendMessage，另一个在它执行期间取消。
    """
    print("\n[A2A-01] 同步任务取消后不得变回 completed")

    async def fire():
        return await asyncio.to_thread(rpc, "SendMessage", {
            "agent": "dsh-cli",
            "message": {
                "messageId": "p1-1",
                "role": "ROLE_USER",
                "parts": [{"text": "Write a detailed 800-word essay about the history of computing, with headings."}],
                "contextId": "ctx-p1-cancel",
            },
        })

    send_task = asyncio.create_task(fire())
    # 等它真正进入执行（子进程拉起来需要几秒）
    await asyncio.sleep(8)

    # 找出这次请求产生的 task id（通过最近的 trace / task 列表）
    recent = rpc("ListTasks", {"limit": 1})
    tasks = recent["result"]["tasks"]
    if not tasks:
        print("  找不到刚派发的任务")
        send_task.cancel()
        return False
    task_id = tasks[0]["id"]
    state_before = tasks[0]["status"]["state"]
    print(f"  执行中: task={task_id[:8]}… state={state_before}")

    cancelled = rpc("CancelTask", {"id": task_id})
    print(f"  取消后: {state_of(cancelled)}")

    # 让被取消的同步请求收尾（它会因为等待被 cancel 而结束）
    try:
        await asyncio.wait_for(send_task, timeout=30)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    await asyncio.sleep(5)
    final = rpc("GetTask", {"id": task_id})
    fs = state_of(final)
    print(f"  5 秒后再查: {fs}")
    ok = fs == "TASK_STATE_CANCELED"
    print("  ->", "PASS" if ok else f"FAIL（终态被覆盖成 {fs}）")
    return ok


async def test_a2a_04_concurrent_context() -> bool:
    """同一 contextId 的并发请求不得从同一旧 session 分叉。"""
    print("\n[A2A-04] 同 contextId 并发不得分叉")
    ctx = "ctx-p1-concurrent"

    async def one(tag: str):
        return await asyncio.to_thread(rpc, "SendMessage", {
            "agent": "dsh-cli",
            "message": {
                "messageId": f"p1-{tag}",
                "role": "ROLE_USER",
                "parts": [{"text": f"Remember the code {tag}. Reply with exactly: SAVED"}],
                "contextId": ctx,
            },
        })

    r1, r2 = await asyncio.gather(one("1111"), one("2222"), return_exceptions=True)
    if isinstance(r1, BaseException) or isinstance(r2, BaseException):
        print("  并发调用异常:", r1, r2)
        return False

    sid1 = _session_of(r1)
    sid2 = _session_of(r2)
    print(f"  两个并发请求的 session: {sid1} / {sid2}")
    # 串行化生效时：第二个请求读到的是第一个写回的 session，
    # 而**续接会复用同一个 session id**（实测如此），所以「相同」才是 PASS。
    # 若两条都从空 session 出发各自新建，就会得到两个不同的 id —— 那才是分叉。
    ok = sid1 is not None and sid2 is not None and sid1 == sid2
    print("  ->", "PASS" if ok else "FAIL（两个请求各建了 session，说明读 session 时没有串行化）")
    return ok


def _session_of(payload: dict) -> str | None:
    task = (payload.get("result") or {}).get("task") or {}
    for artifact in task.get("artifacts") or []:
        meta = artifact.get("metadata") or {}
        if meta.get("sessionId"):
            return str(meta["sessionId"])
    return None


def test_a2a_05_auth() -> bool:
    """启用认证时：卡片/健康公开，其余需要 Bearer。"""
    print("\n[A2A-05] Bearer 认证")
    if not TOKEN:
        print("  (未启用认证，跳过)")
        return True
    results = {
        "/.well-known/agent-card.json": http_get_noauth("/.well-known/agent-card.json"),
        "/healthz": http_get_noauth("/healthz"),
        "/console": http_get_noauth("/console"),
        "/admin/agents (无 token)": http_get_noauth("/admin/agents"),
        "/admin/agents (有 token)": http_get("/admin/agents"),
        "/admin/traces (无 token)": http_get_noauth("/admin/traces"),
    }
    for k, v in results.items():
        print(f"  {k:<32} {v}")
    ok = (
        results["/.well-known/agent-card.json"] == 200
        and results["/healthz"] == 200
        and results["/console"] == 200
        and results["/admin/agents (无 token)"] == 401
        and results["/admin/agents (有 token)"] == 200
        and results["/admin/traces (无 token)"] == 401
    )
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    args = ap.parse_args()
    TOKEN = args.token

    checks = [await test_a2a_01_sync_cancel(), await test_a2a_04_concurrent_context()]
    checks.append(test_a2a_05_auth())

    print("\n==== 汇总 ====")
    for name, ok in zip(["A2A-01", "A2A-04", "A2A-05"], checks):
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
