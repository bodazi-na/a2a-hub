#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小 A2A 下游 agent —— **零外部依赖的演示执行体**。

它不做任何真实工作，只按 A2A 协议回一个已完成的 task。用途有两个：

1. **让你在没装任何真实 CLI（claude / codex / dsh / qoder）的情况下，
   五分钟内看到 hub 的整条链路跑通** —— 注册 → 路由 → 派活 → 落库 → 查询。
2. **当作写真实适配器的参照**：协议面刻意与真实的 A2A 服务对齐
   （camelCase 字段 + `A2A-Version` 头），照着它实现即可。

```bash
python examples/mock_agent.py --port 9301 --name demo --tag demo
```

它只依赖 starlette —— 那本来就是 hub 的依赖，所以**不需要额外安装任何东西**。
"""

from __future__ import annotations

import argparse
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

TASKS: dict[str, dict] = {}
COUNTER = {"n": 0}


def make_app(name: str, tag: str) -> Starlette:
    async def card(request: Request) -> JSONResponse:
        return JSONResponse({
            "name": name,
            "description": f"mock downstream agent ({name})",
            "version": "0.0.1",
            "protocolVersion": "1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": tag, "name": tag, "description": f"{tag} capability"}],
        })

    async def rpc(request: Request) -> JSONResponse:
        payload = await request.json()
        rid = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if method in ("SendMessage", "message/send"):
            message = params.get("message") or {}
            text = "".join(
                str(p.get("text", ""))
                for p in (message.get("parts") or [])
                if isinstance(p, dict)
            )
            context_id = message.get("contextId") or ""
            COUNTER["n"] += 1
            task_id = uuid.uuid4().hex
            reply = f"[{name}] echo: {text}"
            TASKS[task_id] = {
                "id": task_id,
                "contextId": context_id,
                "status": {"state": "TASK_STATE_COMPLETED"},
                "history": [
                    {"role": "ROLE_USER", "parts": [{"text": text}]},
                    {"role": "ROLE_AGENT", "parts": [{"text": f"turn {COUNTER['n']} done"}]},
                    {"role": "ROLE_AGENT", "parts": [{"text": reply}]},
                ],
                "artifacts": [{
                    "name": "response",
                    "parts": [{"text": reply}],
                    "metadata": {
                        "sessionId": f"sess-{name}-{COUNTER['n']}",
                        "agent": name,
                        "turn": COUNTER["n"],
                    },
                }],
            }
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"task": TASKS[task_id]}})

        if method in ("GetTask", "tasks/get"):
            task_id = params.get("id") or params.get("taskId")
            task = TASKS.get(task_id)
            if task is None:
                return JSONResponse({
                    "jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32001, "message": f"task not found: {task_id}"},
                })
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": task})

        return JSONResponse({
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })

    return Starlette(routes=[
        Route("/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/", rpc, methods=["POST"]),
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9301)
    ap.add_argument("--name", default="mock-agent")
    ap.add_argument("--tag", default="mock")
    args = ap.parse_args()

    import uvicorn

    print(f"[mock] {args.name} on http://{args.host}:{args.port} tag={args.tag}")
    uvicorn.run(make_app(args.name, args.tag), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
