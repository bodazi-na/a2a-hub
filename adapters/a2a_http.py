# -*- coding: utf-8 -*-
"""通用 HTTP A2A 适配器。

对接任何说 A2A 的下游：本机的 codex-a2a(:9100) / dsh-a2a(:9101)，
或将来任何远端 agent。

刻意用裸 JSON-RPC 而不是 a2a-sdk —— 协议面只有
Agent Card + SendMessage + GetTask 三件事，自己实现更可控，
也让内核少一个重依赖（对「可复用开源框架」这个定位很重要）。

两条必须遵守的约束，都来自归档里的实测教训：
  1. **拉 Agent Card 与发 JSON-RPC 不复用同一个 keep-alive 连接** ——
     部分实现（codex-a2a）在同一连接上先 GET 再 POST 会稳定返回 404。
     这里每次请求都用独立的 AsyncClient。
  2. **trust_env=False** —— 本机系统代理会把 127.0.0.1 的请求变成 502。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from .base import (
    EVENT_STATUS,
    EVENT_TEXT,
    EVENT_THINKING,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    HEALTH_DOWN,
    HEALTH_OK,
    Adapter,
    CallResult,
    Event,
    pick_session_id,
    pick_usage,
)

CARD_PATH = "/.well-known/agent-card.json"

TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}

# 下游 metadata.status 的取值并不统一，这里分两档兜住。
# 关键在于**未知状态的默认方向**：带 metadata.tool 的事件里，
# 结果回传比调用发起更常见（调用与结果成对，而结果侧信息更多），
# 所以默认判成 tool_result —— 误标成调用会让过程回传语义颠倒。
TOOL_CALL_STATES = {
    "started", "start", "running", "in_progress", "in-progress",
    "calling", "call", "pending", "queued",
}
TOOL_RESULT_STATES = {
    "ok", "error", "finished", "failed", "completed", "complete",
    "success", "succeeded", "done", "cancelled", "canceled", "timeout",
}

# metadata 里用量容器的候选键名
USAGE_CONTAINER_KEYS = ("usage", "tokenUsage", "token_usage", "tokens")


def _extract_text(payload: dict[str, Any]) -> str:
    """从 message / task 结构里抽出纯文本。"""
    chunks: list[str] = []
    for part in payload.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            chunks.append(str(part["text"]))
    return "".join(chunks)


def _history_entry_to_event(entry: dict[str, Any]) -> Event | None:
    """把 A2A history 里的一条记录映射成统一语义的 Event。

    约定（与 dsh-a2a 的过程回传对齐）：
      - parts 里有 text 且 metadata 带工具名 → 工具相关事件
      - parts 里有 text 且前缀像思考          → thinking
      - 其余文本                              → text
    """
    text = _extract_text(entry)
    meta = entry.get("metadata") or {}
    tool = meta.get("tool") or meta.get("toolName")
    if tool:
        status = str(meta.get("status") or "").lower()
        if status in TOOL_CALL_STATES:
            kind = EVENT_TOOL_CALL
        elif status in TOOL_RESULT_STATES:
            kind = EVENT_TOOL_RESULT
        else:
            # 状态缺失或不在已知集合里：默认按结果回传处理，见文件头常量处的说明
            kind = EVENT_TOOL_RESULT
        return Event(kind=kind, text=text, metadata={"tool": tool, **meta})
    if not text:
        return None
    if text.startswith("(thinking)") or text.startswith("thinking:"):
        return Event(kind=EVENT_THINKING, text=text)
    role = entry.get("role")
    if role in {"ROLE_AGENT", "ROLE_USER"}:
        return Event(kind=EVENT_TEXT, text=text, metadata={"role": role})
    return Event(kind=EVENT_STATUS, text=text)


# pick_session_id / pick_usage 已上移到 base.py，与 cli 适配器共用


class A2AHttpAdapter(Adapter):
    kind = "a2a_http"

    def __init__(
        self,
        name: str,
        endpoint: str,
        *,
        token: str | None = None,
        poll_interval: float = 1.5,
        **options: Any,
    ):
        super().__init__(name, endpoint, **options)
        self.token = token
        self.poll_interval = poll_interval
        # 最近一次探测拿到的 Agent Card，供 Hub 刷新注册表用（A2A-12）
        self.last_card: dict[str, Any] | None = None
        # 实际使用的 JSON-RPC 端点，由 get_card 按 supportedInterfaces 决定（A2A-15）
        self.rpc_url: str | None = None

    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"A2A-Version": "1.0", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get_card(self) -> dict[str, Any]:
        assert self.endpoint, "A2A 适配器必须有 endpoint"
        url = self.endpoint.rstrip("/") + CARD_PATH
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            # 卡片端点也可能要求认证（A2A-16）：不带 token 会被 401，
            # 结果就是「RPC 能用但健康检查永远 down」。
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            card = resp.json()
        self.last_card = card
        # 按 Card 声明的接口选 RPC 端点（A2A-15）：
        # 下游可能把 JSON-RPC 挂在 /rpc 而不是根路径，硬编码根路径会 404。
        self.rpc_url = self._pick_rpc_url(card)
        return card

    def _pick_rpc_url(self, card: dict[str, Any]) -> str:
        """从 supportedInterfaces 里挑一个 JSONRPC 端点；挑不到就退回 base + '/'。"""
        interfaces = card.get("supportedInterfaces") or []
        for itf in interfaces:
            if not isinstance(itf, dict):
                continue
            binding = str(itf.get("protocolBinding") or "").upper().replace("-", "")
            url = itf.get("url")
            if url and binding in ("JSONRPC", ""):
                return str(url)
        return self.endpoint.rstrip("/") + "/"

    async def probe(self) -> str:
        try:
            await self.get_card()
        except Exception:
            return HEALTH_DOWN
        # get_card 里已把卡片存进 last_card，供 Hub 刷新注册表（A2A-12）
        return HEALTH_OK

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str = "1",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        assert self.endpoint, "A2A 适配器必须有 endpoint"
        # 优先用卡片声明的端点；还没拉过卡片时退回 base + '/'
        url = self.rpc_url or (self.endpoint.rstrip("/") + "/")
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"{method} failed: {payload['error']}")
        return payload.get("result") or {}

    # ------------------------------------------------------------------

    async def call(
        self,
        prompt: str,
        *,
        context_id: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> CallResult:
        budget = timeout if timeout is not None else 600.0
        # 截止时间从**调用开始**就建立（A2A-11）。原来是在首次 SendMessage
        # 返回之后才建，于是「首次请求耗时 + N 次各 60 秒的轮询」可以远超预算。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + budget

        def remaining(minimum: float = 1.0) -> float:
            return max(deadline - loop.time(), minimum)

        message: dict[str, Any] = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
        }
        if context_id:
            message["contextId"] = context_id

        try:
            result = await self._rpc(
                "SendMessage", {"message": message}, timeout=remaining()
            )
        except Exception as exc:
            return CallResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        task = result.get("task")
        if task is None:
            # 少数实现直接回一条 message 而不建 task
            text = _extract_text(result.get("message") or result)
            return CallResult(ok=True, text=text, raw=result)

        task_id = task.get("id")
        events: list[Event] = []
        seen = 0
        current = task

        while True:
            state = (current.get("status") or {}).get("state", "")
            history = current.get("history") or []
            while seen < len(history):
                event = _history_entry_to_event(history[seen])
                seen += 1
                if event is not None:
                    events.append(event)

            if state in TERMINAL_STATES or loop.time() >= deadline:
                break

            # 睡眠也不能超过剩余预算
            await asyncio.sleep(min(self.poll_interval, remaining(0.1)))
            try:
                current = await self._rpc(
                    "GetTask", {"id": task_id}, request_id="2", timeout=remaining()
                )
            except Exception as exc:
                return CallResult(
                    ok=False, events=events, error=f"GetTask failed: {exc}"
                )

        ok = state == "TASK_STATE_COMPLETED"
        text = ""
        metadata: dict[str, Any] = {}
        for artifact in current.get("artifacts") or []:
            text += _extract_text(artifact)
            if artifact.get("metadata"):
                metadata.update(artifact["metadata"])

        error = None
        if not ok:
            status_msg = (current.get("status") or {}).get("message") or {}
            error = _extract_text(status_msg) or state or "unknown failure"

        return CallResult(
            ok=ok,
            text=text,
            session_id=pick_session_id(metadata),
            events=events,
            usage=pick_usage(metadata),
            metadata=metadata,
            error=error,
            raw=current,
        )
