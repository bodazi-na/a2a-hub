# -*- coding: utf-8 -*-
"""MCP facade：把 hub 的 A2A 接口包装成 MCP 工具。

为什么要有它
------------
各个 agent 平台的扩展机制并不统一 —— WorkBuddy / DSH 用 skills，
Codex / Qoder / Claude Code 用 MCP。逐个写 skill 是 5 份重复内容；
而**它们全都支持 MCP 客户端**，所以一个 facade 就能覆盖全部平台。

设计边界
--------
它是**可选组件**，不是 hub 的核心：

- `a2a_hub/core/` 保持纯 A2A，一行不改
- facade 通过 **HTTP** 调 hub（不 import core），所以它和 hub 是解耦的 ——
  可以单独起、单独停、单独部署
- 换掉它，hub 依然是完整的 A2A 服务

这与「薄核心 + 厚适配器」是同一个思路：MCP 只是一种**接入形态**。

用法
----
stdio（多数 MCP 客户端）：

    python -m mcp.server          # 由客户端拉起

或作为模块：

    python -m a2a_hub.mcp_facade.server

环境变量
--------
  HUB_URL    默认 http://127.0.0.1:9200
  HUB_TOKEN  hub 启用 Bearer 认证时必填
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer

HUB_URL = (os.environ.get("HUB_URL") or "http://127.0.0.1:9200").rstrip("/")

# 本机系统代理会把 127.0.0.1 的请求变成 502，必须绕开
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

server = MCPServer(
    name="a2a-hub",
    instructions=(
        "多 Agent A2A 协调内核。用它来：查看本机有哪些 agent 在线、"
        "把任务派给合适的 agent、跑多 agent 编排流水线、查跨 agent 的审计时间线。\n"
        "注意：只调一个 agent 的简单任务不必绕 hub；hub 的价值在于"
        "「不知道派给谁」或「需要多个 agent 协作」的场景。"
    ),
)


# ---------------------------------------------------------------------------
# 传输层：HTTP 调 hub
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    token = os.environ.get("HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc(method: str, params: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method,
                       "params": params}).encode("utf-8")
    req = urllib.request.Request(HUB_URL + "/", data=body, headers=_headers())
    with _OPENER.open(req, timeout=timeout) as resp:
        payload = json.load(resp)
    if "error" in payload:
        raise RuntimeError(payload["error"].get("message") or "unknown hub error")
    return payload.get("result") or {}


def _get(path: str, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(HUB_URL + path, headers=_headers())
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.load(resp)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
        return "hub 启用了 Bearer 认证，请设置环境变量 HUB_TOKEN"
    if isinstance(exc, urllib.error.URLError):
        return (f"连不上 hub（{HUB_URL}）。请先在 a2a-hub 目录运行 "
                f"`python hub.py serve`。原因：{exc.reason}")
    return f"{type(exc).__name__}: {exc}"


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    text = ""
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                text += str(part["text"])
    status = task.get("status") or {}
    message = (status.get("message") or {}).get("parts") or []
    error = "".join(str(p.get("text", "")) for p in message if isinstance(p, dict))
    out: dict[str, Any] = {
        "state": status.get("state"),
        "taskId": task.get("id"),
        "text": text,
    }
    if error:
        out["error"] = error
    return out


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

@server.tool(
    name="hub_status",
    description="检查 a2a-hub 是否在跑，以及任务数 / 节点数 / 运行中任务数。",
)
def hub_status() -> dict[str, Any]:
    try:
        return _get("/healthz")
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}


@server.tool(
    name="hub_agents",
    description=(
        "列出 hub 上注册的所有 agent：名字、类型（cli / a2a_http）、健康状态、能力标签。"
        "派任务前先用它看有哪些可用节点。"
    ),
)
def hub_agents() -> dict[str, Any]:
    try:
        return _get("/admin/agents")
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}


@server.tool(
    name="hub_send",
    description=(
        "把一个任务派给 hub 上的某个 agent 并等待结果。\n"
        "- 不指定 agent 时按健康状态自动路由\n"
        "- 可用 agent 精确指定，或用 tags 按能力选（如 ['codex']）\n"
        "- 传同一个 context_id 可以续接上一轮会话\n"
        "返回值含 state（TASK_STATE_COMPLETED 才算成功）、taskId、text。"
    ),
)
def hub_send(
    prompt: str,
    agent: str | None = None,
    tags: list[str] | None = None,
    context_id: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {
            "messageId": "mcp-hub-send",
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
        }
    }
    if agent:
        params["agent"] = agent
    if tags:
        params["tags"] = tags
    if context_id:
        params["message"]["contextId"] = context_id
    if timeout:
        params["timeout"] = timeout
    try:
        result = _rpc("SendMessage", params)
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}
    return _task_summary(result.get("task") or {})


@server.tool(
    name="hub_plan",
    description=(
        "跑一条多 agent 编排流水线。steps 是一个数组，每个元素形如\n"
        '  {"id": "draft", "agent": "claude-cli", "prompt": "写一段关于 {{input}} 的初稿"}\n'
        "要点：\n"
        "- 依赖必须显式表达：只有 prompt 里写了 {{steps.draft}} 才会等 draft 完成；\n"
        "  自然语言描述（「先做 A 再做 B」）不会被识别，那两步会并行跑。\n"
        "- 模板变量：{{input}}（外层输入）、{{steps.<id>}}（某步输出）\n"
        "- 无依赖关系的步骤会并行执行\n"
        "- 默认各步独立会话；要共享下游会话，在该 step 上加 \"sharedContext\": true\n"
        "- 失败策略默认 fail-fast；单步加 \"onError\": \"continue\" 可让它失败后不拖累独立分支\n"
        "返回 planId、traceId、layers（并行分层）与每步结果。"
    ),
)
def hub_plan(steps: list[dict[str, Any]], context_id: str = "", input: str = "") -> dict[str, Any]:
    params: dict[str, Any] = {"steps": steps, "contextId": context_id, "input": input}
    try:
        return _rpc("RunPlan", params)
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}


@server.tool(
    name="hub_trace",
    description=(
        "查看一次请求的完整审计时间线：涉及哪些 agent、各自耗时、"
        "以及按时间排序的全部过程事件（含 thinking / 工具调用）。"
        "传 RunPlan 返回的 traceId，或任一 taskId。"
    ),
)
def hub_trace(trace_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if trace_id:
        params["traceId"] = trace_id
    if task_id:
        params["taskId"] = task_id
    if not params:
        return {"error": "需要给 trace_id 或 task_id"}
    try:
        return _rpc("GetTrace", params)
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}


@server.tool(
    name="hub_tasks",
    description="列出 hub 上最近的任务（状态、agent、耗时、提示词摘要）。",
)
def hub_tasks(limit: int = 20) -> dict[str, Any]:
    try:
        return _get(f"/admin/tasks?limit={max(1, min(int(limit), 200))}")
    except Exception as exc:  # noqa: BLE001
        return {"error": _friendly_error(exc)}


def main() -> None:
    """以 stdio 传输启动（MCP 客户端会拉起这个进程）。"""
    server.run("stdio")


if __name__ == "__main__":
    main()
