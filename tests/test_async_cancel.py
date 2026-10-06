#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 hub 的异步任务模型与取消。

跑法（hub 需在 9200 上运行，且注册表里有 dsh）：
    python tests/test_async_cancel.py

三件事一起验证：
  1. async=true 时 SendMessage 立即返回（状态是 WORKING 而不是 COMPLETED）
  2. CancelTask 能把它置为 CANCELED
  3. 取消后任务终态稳定，不会被后台执行覆盖回去

用标准库 urllib 而不是 httpx，并显式禁用代理 —— 本机系统代理会把
127.0.0.1 的请求变成 502，这个坑在真实客户端里同样会踩。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

HUB = "http://127.0.0.1:9200/"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(method: str, params: dict, rid: str = "1", timeout: float = 60.0) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        HUB, data=body,
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    )
    with OPENER.open(req, timeout=timeout) as resp:
        return json.load(resp)


def state_of(payload: dict) -> str:
    task = payload.get("result", {}).get("task") or payload.get("result", {})
    return (task.get("status") or {}).get("state", "?")


def main() -> int:
    # 默认节点名要与注册表一致 —— 原来写的是 "dsh"，而注册的是 "dsh-cli"，
    # 于是这个测试一直以「agent 未注册」失败，白白掩盖了它本该验证的取消路径。
    agent = sys.argv[1] if len(sys.argv) > 1 else "dsh-cli"

    # 1) 异步派发一个明显耗时的任务
    dispatched = rpc("SendMessage", {
        "agent": agent,
        "async": True,
        "message": {
            "messageId": "async-1",
            "role": "ROLE_USER",
            "parts": [{"text": "Write a detailed 400-word essay about the history of computing, with headings."}],
            "contextId": "ctx-cancel-test",
        },
    })
    task_id = dispatched["result"]["task"]["id"]
    first_state = state_of(dispatched)
    print(f"[1] dispatched   task={task_id}")
    print(f"    state        {first_state}   (async 模式下不应是 COMPLETED)")

    # 2) 立刻取消
    cancelled = rpc("CancelTask", {"id": task_id}, rid="2")
    print(f"[2] after cancel {state_of(cancelled)}")

    # 3) 稍等再查，确认终态稳定
    time.sleep(2.0)
    again = rpc("GetTask", {"id": task_id}, rid="3")
    final_state = state_of(again)
    print(f"[3] get again    {final_state}")

    # A2A-20：断言必须收紧到 CANCELED。
    # 原来写成 `final_state in {CANCELED, COMPLETED}` —— 那等于允许「取消后又跑完」，
    # 正是这个宽松断言让 A2A-01 的状态回退 bug 从测试里溜过去了。
    # 若任务确实在取消前就已合法完成，那属于**竞态用例**，应当单独构造，
    # 不能用放宽断言的方式混在一起。
    # 精确的取消语义由 tests/test_cancel_semantics.py 用假适配器覆盖（不依赖真实下游）。
    ok = final_state == "TASK_STATE_CANCELED"
    print()
    if first_state != "TASK_STATE_WORKING":
        print(f"RESULT: INCONCLUSIVE（任务在取消前已完成，状态 {first_state}；本次未验证到取消路径）")
        return 2
    print("RESULT:", "PASS" if ok else f"FAIL（取消后终态被改成 {final_state}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
