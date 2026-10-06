#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证适配器侧的批次 2 修复（A2A-11/12/15/16/18）。

用一个可配置的「假 A2A 下游」精确复现：
  - 首次响应很慢 → 验证 A2A-11（timeout 是整次任务的截止）
  - 卡片端点要求 Bearer → 验证 A2A-16
  - 卡片声明 JSON-RPC 挂在 /rpc → 验证 A2A-15
  - 卡片里的 skills 变化 → 验证 A2A-12（探测刷新能力表）
A2A-18 用多连接并发写消息验证。

跑法：python tests/test_adapter_p2.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.a2a_http import A2AHttpAdapter      # noqa: E402
from core.registry import AgentRecord, Registry   # noqa: E402
from core.store import Store                      # noqa: E402

CARD = {
    "name": "fake-downstream",
    "version": "0.0.1",
    "protocolVersion": "1.0",
    # 刻意把 JSON-RPC 声明在 /rpc（A2A-15）
    "supportedInterfaces": [
        {"url": None, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "skills": [{"id": "skill-v1", "name": "skill-v1"}],
}

STATE = {"slow_send": 0.0, "require_token": None, "rpc_path": "/rpc", "skill": "skill-v1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/.well-known/agent-card.json"):
            if STATE["require_token"]:
                if self.headers.get("authorization") != f"Bearer {STATE['require_token']}":
                    self._json(401, {"error": "unauthorized"})
                    return
            card = dict(CARD)
            card["supportedInterfaces"] = [
                {"url": f"http://127.0.0.1:{self.server.server_port}{STATE['rpc_path']}",
                 "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ]
            card["skills"] = [{"id": STATE["skill"], "name": STATE["skill"]}]
            self._json(200, card)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != STATE["rpc_path"]:
            self._json(404, {"error": f"wrong path {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        rid = payload.get("id")
        if method == "SendMessage":
            # 模拟「首次响应很慢」（A2A-11）
            if STATE["slow_send"]:
                time.sleep(STATE["slow_send"])
            self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"task": {
                "id": "t1", "contextId": "c1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"name": "response", "parts": [{"text": "ok"}],
                               "metadata": {"usage": {"input_tokens": 1}}}],
            }}})
            return
        self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {}})


def start_server() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


# ---------------------------------------------------------------- A2A-15 + 16
async def test_a2a_15_16() -> bool:
    print("\n[A2A-15/16] 按 Card 声明选端点 + Card 请求带 token")
    srv, base = start_server()
    try:
        # 15：RPC 挂在 /rpc，硬编码根路径会 404
        adapter = A2AHttpAdapter("d", base, token=None, poll_interval=0.2)
        card = await adapter.get_card()
        print(f"    卡片声明端点: {card['supportedInterfaces'][0]['url']}")
        print(f"    适配器选中  : {adapter.rpc_url}")
        r15 = adapter.rpc_url.endswith("/rpc")

        # 16：卡片端点要求 token
        STATE["require_token"] = "secret-token"
        no_token = A2AHttpAdapter("d2", base, token=None)
        with_token = A2AHttpAdapter("d3", base, token="secret-token")
        h_no = await no_token.probe()
        h_yes = await with_token.probe()
        print(f"    无 token 探测: {h_no}（期望 down）")
        print(f"    有 token 探测: {h_yes}（期望 ok）")
        r16 = h_no == "down" and h_yes == "ok"
        STATE["require_token"] = None

        ok = r15 and r16
        print("    ->", "PASS" if ok else "FAIL")
        return ok
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- A2A-11
async def test_a2a_11_deadline() -> bool:
    print("\n[A2A-11] timeout 必须是整次任务的截止时间")
    srv, base = start_server()
    try:
        STATE["slow_send"] = 4.0  # 首次响应就吃掉 4 秒
        adapter = A2AHttpAdapter("d", base, poll_interval=0.2)
        # 先解析卡片，让 rpc_url 指向假下游真正的 /rpc ——
        # 否则 call() 会打到根路径拿个 404 立即返回，测不到超时路径
        await adapter.get_card()
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        result = await adapter.call("x", timeout=2.0)
        elapsed = loop.time() - t0
        print(f"    timeout=2s，首次响应需 4s → 实际耗时 {elapsed:.1f}s，ok={result.ok}")
        print(f"    错误: {str(result.error)[:70]}")
        # 旧实现会在首次响应之后才开始计时，总耗时至少 4s 以上；
        # 现在 deadline 从调用开始就建立，所以应在 ~2s 内收手
        ok = 1.5 < elapsed < 3.5 and not result.ok
        print("    ->", "PASS" if ok else "FAIL（未按整次任务截止收手）")
        return ok
    finally:
        STATE["slow_send"] = 0.0
        srv.shutdown()


# ---------------------------------------------------------------- A2A-12
async def test_a2a_12_card_refresh() -> bool:
    print("\n[A2A-12] 探测应刷新注册表里的 skills")
    srv, base = start_server()
    try:
        db = os.path.join(tempfile.gettempdir(), f"a12_{os.urandom(3).hex()}.db")
        store = Store(db)
        registry = Registry(store)
        registry.register(AgentRecord(name="d", kind="a2a_http", endpoint=base, tags=["d"]))
        adapter = A2AHttpAdapter("d", base)

        before = registry.get("d").skills
        STATE["skill"] = "skill-v2"      # 下游升级了能力
        await adapter.probe()
        card = adapter.last_card
        # 模拟 Hub 的刷新动作
        registry.store.upsert_agent(name="d", kind="a2a_http", endpoint=base,
                                    card=card, tags=["d"], enabled=True)
        after = registry.get("d").skills
        print(f"    刷新前 skills: {before}")
        print(f"    刷新后 skills: {after}")
        ok = before != after and "skill-v2" in after
        print("    ->", "PASS" if ok else "FAIL")
        return ok
    finally:
        STATE["skill"] = "skill-v1"
        srv.shutdown()


# ---------------------------------------------------------------- A2A-18
def test_a2a_18_seq_race() -> bool:
    print("\n[A2A-18] 多连接并发追加消息不应产生重复 seq")
    db = os.path.join(tempfile.gettempdir(), f"a18_{os.urandom(3).hex()}.db")
    main = Store(db)
    task = main.create_task(context_id="c", prompt="x", state="working")

    errors: list[str] = []

    def worker(n: int):
        try:
            s = Store(db)
            for i in range(20):
                s.add_message(task["id"], role="agent", kind="text",
                              content=[{"text": f"w{n}-{i}"}])
            s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = main.list_messages(task["id"])
    seqs = [m["seq"] for m in msgs]
    dupes = len(seqs) - len(set(seqs))
    print(f"    写入 {len(seqs)} 条，唯一 seq {len(set(seqs))} 个，重复 {dupes} 个")
    if errors:
        print(f"    异常: {errors[:3]}")
    ok = dupes == 0 and len(seqs) == 80 and not errors
    print("    ->", "PASS" if ok else "FAIL")
    return ok


async def main() -> int:
    checks = {
        "A2A-15/16": await test_a2a_15_16(),
        "A2A-11": await test_a2a_11_deadline(),
        "A2A-12": await test_a2a_12_card_refresh(),
        "A2A-18": test_a2a_18_seq_race(),
    }
    print("\n==== 汇总 ====")
    for name, ok in checks.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
