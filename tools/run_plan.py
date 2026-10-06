#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一条编排（RunPlan），并把原始结果落盘。

存在的理由
----------
`skills/a2a-hub/scripts/hub_client.py` 里 RPC 超时写死 900s。
真实编排（多个 agent 并行做重活 + 汇总）很容易超过这个数，
客户端会先超时，而服务端其实还在跑 —— 于是调用方拿不到 planId，
只能靠盲猜 / 翻数据库去找。这个脚本把超时做成参数，并把结果落盘，
避免「跑完了但结果丢了」。

用法
----
  python tools/run_plan.py examples/plan-code-review.json
  python tools/run_plan.py <plan.json> --timeout 3600 --out workspace/review/plan-result.json

环境变量
--------
  HUB_URL    默认 http://127.0.0.1:9200
  HUB_TOKEN  启用 Bearer 认证时必填
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 本机系统代理会把 127.0.0.1 的请求变成 502，必须绕开
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def base() -> str:
    return (os.environ.get("HUB_URL") or "http://127.0.0.1:9200").rstrip("/")


def headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    token = os.environ.get("HUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def rpc(method: str, params: dict, timeout: float) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method,
                       "params": params}).encode("utf-8")
    req = urllib.request.Request(base() + "/", data=body, headers=headers())
    with OPENER.open(req, timeout=timeout) as resp:
        payload = json.load(resp)
    if "error" in payload:
        err = payload["error"]
        raise SystemExit(f"{method} 失败 [{err.get('code')}]: {err.get('message')}")
    return payload.get("result") or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="跑一条 a2a-hub 编排")
    ap.add_argument("plan_file", help="steps 数组的 JSON 文件")
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="客户端等待上限（秒），默认 3600")
    ap.add_argument("--out", help="把原始结果 JSON 写到这里")
    args = ap.parse_args()

    steps = json.loads(open(args.plan_file, encoding="utf-8").read())
    if not isinstance(steps, list) or not steps:
        raise SystemExit("plan 文件必须是 steps 数组")

    started = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] 提交 {len(steps)} 步编排 → {base()}")
    result = rpc("RunPlan", {"steps": steps}, timeout=args.timeout)
    elapsed = time.time() - started

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"原始结果已写入 {args.out}")

    print(f"\n[{time.strftime('%H:%M:%S')}] 完成，用时 {elapsed:.1f}s")
    print(f"ok      : {result.get('ok')}")
    print(f"planId  : {result.get('planId')}")
    print(f"traceId : {result.get('traceId')}")
    print(f"layers  : {result.get('layers')}")
    print("-" * 70)
    # RunPlan 返回的 step 结构是 {id, ok, skipped, taskId, text}，
    # **不含** state / agent / durationMs —— 那些要另查 GetPlan 或 /admin/tasks。
    for step in result.get("steps") or []:
        text = step.get("text") or ""
        mark = "ok " if step.get("ok") else ("skip" if step.get("skipped") else "FAIL")
        print(f"  [{mark}] {str(step.get('id')):<9} {len(text):>7} chars  "
              f"taskId={step.get('taskId')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("401：hub 启用了 Bearer 认证，请设置 HUB_TOKEN", file=sys.stderr)
        else:
            print(f"HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        sys.exit(2)
