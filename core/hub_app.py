# -*- coding: utf-8 -*-
"""hub 服务端：对外是一个 A2A agent，对内是注册中心 + 路由器。

对外接口
--------
GET  /.well-known/agent-card.json   hub 自己的 Agent Card
GET  /healthz                       健康检查
POST /                              JSON-RPC：
                                      SendMessage   同步，或 async=true 转后台
                                      GetTask / ListTasks / CancelTask
GET  /admin/agents                  注册表快照
GET  /admin/tasks                   运行中任务快照

核心设计
--------
**先落库，再派活。** 任务在任何下游动作之前就已经写进 SQLite，
所以进程崩了、重启了，任务状态依然查得到。

任务模型
--------
- **默认同步**：SendMessage 阻塞到下游结束。适合短任务与脚本调用。
- **`async=true`**：立即返回 task_id，后台执行；用 GetTask 轮询、CancelTask 取消。
  长任务必须走这条，否则会一直占住 HTTP 连接。
- 进程内用 `asyncio.Task` 跟踪运行中的任务。

取消的边界（诚实说明）
----------------------
`CancelTask` 能中断 hub 侧的等待与对下游的 HTTP 请求，并立即把任务置为
`canceled`。但**下游自己派生的子进程不保证被杀** —— 那是下游实现的责任，
A2A 协议也没有强制要求。所以取消是「尽力而为」。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adapters.base import Adapter
from core.orchestrator import Orchestrator, PlanError
from core.registry import Registry
from core.router import NoRouteError, Router
from core.store import Store

HUB_NAME = "a2a-hub"
HUB_VERSION = "0.2.0"
HUB_DESCRIPTION = "多 Agent A2A 协调内核：注册、路由、持久化"

# 内部小写状态 → A2A 协议状态
STATE_MAP = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
}

ACTIVE_STATES = ("submitted", "working")


# 用量字段的命名在各家 CLI 之间不统一（dsh 用 inputTokens，codex 用 input_tokens，
# OpenAI 系用 prompt_tokens…）。审计汇总必须先把它们归一到同一套口径，
# 否则「总共烧了多少」会被重复计数、也没法跨 agent 比较。
USAGE_ALIASES = {
    "input_tokens": "input",
    "inputTokens": "input",
    "prompt_tokens": "input",
    "output_tokens": "output",
    "outputTokens": "output",
    "completion_tokens": "output",
    "cached_input_tokens": "cacheRead",
    "cacheReadTokens": "cacheRead",
    "cache_read_input_tokens": "cacheRead",
    "cache_write_input_tokens": "cacheWrite",
    "cacheWriteTokens": "cacheWrite",
    "cache_creation_input_tokens": "cacheWrite",
    "reasoning_output_tokens": "reasoning",
    "total_tokens": "total",
    "totalTokens": "total",
}


def normalize_usage(usage: dict[str, Any]) -> dict[str, float]:
    """把一份用量归一到统一口径。

    注意**丢弃下游自报的 total**：那是它自己那一次的合计，
    跨 agent 汇总时把它加进来会得到一个既不是总量、也不是分量的怪数。
    total 一律由汇总方用 input + output 现算。
    """
    out: dict[str, float] = {}
    for key, value in (usage or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        canonical = USAGE_ALIASES.get(key)
        if canonical is None or canonical == "total":
            continue
        out[canonical] = out.get(canonical, 0) + value
    out["total"] = out.get("input", 0) + out.get("output", 0)
    return out


def clamp_limit(raw: Any, *, default: int = 50, maximum: int = 500) -> int:
    """把用户给的 limit 收敛到 [1, maximum]。

    SQLite 的 `LIMIT -1` 表示**不限量**（A2A-06）—— 而 ListTasks 还会为每个任务
    拼装完整 history/artifacts，等于一次请求就能把整个库拉出来。
    所以这里既挡负数，也挡「给个超大值」。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def parse_timeout(raw: Any, *, default: float | None = None) -> float | None:
    """解析并校验 timeout；未提供时返回 `default`（通常是 None）。

    **必须返回 None 而不是替调用方填一个数**（A2A-13）：
    Hub 若统一填 600，适配器里 `timeout or self.default_timeout` 就永远取 600，
    `agents/*.json` 里配的 timeout 等于白写。语义应该是
    「未指定 → 由适配器决定」。

    校验必须在**落库之前**做：参数非法时直接报错，不能让一个没人执行的任务
    留在 submitted 状态（A2A-10）。
    """
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"timeout 必须是数字，收到: {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"timeout 必须是正的有限值，收到: {value!r}")
    return min(value, 24 * 3600.0)


async def _safe_close(adapter: Adapter) -> None:
    """尽力关闭一个被替换掉的适配器，别让异常冒出来。"""
    try:
        await adapter.close()
    except Exception:  # noqa: BLE001
        pass


def _duration_ms(started: str | None, finished: str | None) -> int | None:
    """两个 ISO8601 时间戳之间的毫秒差；缺任一则为 None。"""
    if not started or not finished:
        return None
    try:
        a = datetime.fromisoformat(started)
        b = datetime.fromisoformat(finished)
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _task_started_at(task: dict[str, Any]) -> str | None:
    """任务的「开始时刻」——**不是** `created_at`。

    编排层会预先为每个 step 落占位任务，记录在 plan 开始那一刻就诞生了，
    但可能几分钟后才真正开始跑。拿 `created_at` 当开始时刻，层间排队等待
    会被算进执行时长（实测 merge 步虚高 2.5 倍），控制台甘特图上四根条
    还会全部从 t=0 起画（N2）。

    尚未开始的任务（还没轮到的 step、排队中的请求）`started_at` 为空，
    回退到 `created_at` —— 这样它在图上表现为「从那时起就在等」，
    而不是一片空白。
    """
    return task.get("started_at") or task.get("created_at")


def _to_a2a_task(task: dict[str, Any], messages: list, artifacts: list) -> dict[str, Any]:
    """把库里的 task 投影成 A2A Task 结构。"""
    status: dict[str, Any] = {"state": STATE_MAP.get(task["state"], "TASK_STATE_UNKNOWN")}
    if task.get("error"):
        status["message"] = {
            "role": "ROLE_AGENT",
            "parts": [{"text": task["error"]}],
        }

    history = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            parts = content
        elif isinstance(content, str):
            parts = [{"text": content}]
        elif content:
            parts = [{"text": str(content)}]
        else:
            parts = []
        if not parts:
            continue
        entry: dict[str, Any] = {
            "role": "ROLE_AGENT" if m["role"] == "agent" else "ROLE_USER",
            "parts": parts,
        }
        if m.get("metadata"):
            entry["metadata"] = m["metadata"]
        history.append(entry)

    out: dict[str, Any] = {
        "id": task["id"],
        "contextId": task["context_id"],
        "status": status,
    }
    if history:
        out["history"] = history
    if artifacts:
        out["artifacts"] = [
            {
                "name": a.get("name") or "response",
                "parts": a.get("content")
                if isinstance(a.get("content"), list)
                else [{"text": str(a.get("content") or "")}],
                **({"metadata": a["metadata"]} if a.get("metadata") else {}),
            }
            for a in artifacts
        ]
    if task.get("metadata"):
        out["metadata"] = task["metadata"]
    return out


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Bearer 认证（A2A-05）。

    放行规则刻意保留最小公开面：
      - `/.well-known/agent-card.json` —— A2A 规范要求卡片可被公开发现
      - `/healthz` —— 负载均衡/监控探针
      - `/console` —— 只是 HTML 壳，不含任何数据；它调的 /admin/* 仍需认证
    其余路径（含所有 /admin/* 与 JSON-RPC）一律要求 `Authorization: Bearer <token>`。
    """

    PUBLIC_PATHS = {"/.well-known/agent-card.json", "/healthz", "/console"}

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        # 定长比较，避免因长度差异泄露信息
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                {"error": "unauthorized", "detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


class Hub:
    """把 store / registry / router / adapters 组装成一个可服务的对象。"""

    def __init__(
        self,
        store: Store,
        registry: Registry,
        router: Router,
        adapters: dict[str, Adapter],
        *,
        max_concurrency: int = 4,
        public_url: str | None = None,
        auth_token: str | None = None,
        workspace_dir: str | None = None,
    ):
        self.store = store
        self.registry = registry
        self.router = router
        # name -> (配置签名, adapter)。签名变了就重建（A2A-08）；
        # 空签名表示「外部直接注入的实例」，无条件复用（测试与定制场景）。
        self._adapters: dict[str, tuple[str, Adapter]] = {
            name: ("", ad) for name, ad in adapters.items()
        }
        self.max_concurrency = max_concurrency
        self.public_url = (public_url or "").rstrip("/") or None
        # 运行中的后台任务：task_id -> asyncio.Task
        self._running: dict[str, asyncio.Task] = {}
        # 按 (contextId, agent) 的会话锁，防止并发请求从同一旧 session 分叉
        self._context_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._sem: asyncio.Semaphore | None = None
        self.orchestrator = Orchestrator(self)
        self.auth_token = (auth_token or "").strip() or None
        # 下游 agent 的工作目录：默认 <cwd>/workspace，与代码目录隔离。
        # 不让 CLI agent 把产物写进仓库，这是「代码」与「运行时数据」的分界。
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd() / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def _context_lock(self, context_id: str, agent: str) -> asyncio.Lock:
        key = (context_id, agent)
        lock = self._context_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._context_locks[key] = lock
        return lock

    def _semaphore(self) -> asyncio.Semaphore:
        """延迟创建：Semaphore 必须在事件循环里构造。"""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    # ------------------------------------------------------------------
    # HTTP 端点
    # ------------------------------------------------------------------

    async def agent_card(self, request: Request) -> JSONResponse:
        base = self.public_url or str(request.base_url).rstrip("/")
        agents = self.registry.list(enabled_only=False)
        return JSONResponse({
            "name": HUB_NAME,
            "description": HUB_DESCRIPTION,
            "version": HUB_VERSION,
            "protocolVersion": "1.0",
            # A2A 客户端靠这两个字段发现端点，缺了就不是合规卡片
            "url": base + "/",
            "supportedInterfaces": [
                {
                    "url": base + "/",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                },
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
            },
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [
                {
                    "id": "dispatch",
                    "name": "任务派发",
                    "description": "把任务按能力路由给已注册的下游 agent",
                },
                {
                    "id": "history",
                    "name": "任务历史",
                    "description": "持久化的任务与消息查询，进程重启不丢",
                },
                {
                    "id": "cancel",
                    "name": "任务取消",
                    "description": "取消运行中的任务（尽力而为）",
                },
            ],
            "metadata": {
                "registeredAgents": [a.name for a in agents],
            },
        })

    async def healthz(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "schemaVersion": self.store.schema_version,
            "tasks": self.store.count_tasks(),
            "agents": len(self.registry.list()),
            "running": len(self._running),
        })

    async def admin_agents(self, request: Request) -> JSONResponse:
        return JSONResponse({"agents": self.router.explain()})

    async def console(self, request: Request):
        """只读控制台面板（单页 HTML，零构建、零前端依赖）。"""
        from starlette.responses import HTMLResponse

        from core.console import render_console

        return HTMLResponse(render_console())

    async def admin_tasks(self, request: Request) -> JSONResponse:
        limit = clamp_limit(request.query_params.get("limit"), default=50)
        recent = self.store.list_tasks(limit=limit)
        return JSONResponse({
            "running": [
                {"id": tid, "done": t.done(), "cancelled": t.cancelled()}
                for tid, t in self._running.items()
            ],
            "total": self.store.count_tasks(),
            "recent": [
                {
                    "taskId": t["id"],
                    "traceId": t.get("trace_id"),
                    "planId": t.get("plan_id"),
                    "stepId": t.get("step_id"),
                    "agent": t.get("agent"),
                    "state": t["state"],
                    "createdAt": t["created_at"],
                    "startedAt": _task_started_at(t),
                    "finishedAt": t.get("finished_at"),
                    "durationMs": _duration_ms(_task_started_at(t),
                                               t.get("finished_at")),
                    "prompt": (t.get("prompt") or "")[:160],
                    "error": t.get("error"),
                }
                for t in recent
            ],
        })

    async def admin_probe(self, request: Request) -> JSONResponse:
        """触发一次健康探测（控制台用）。给了 name 就只探那一个。"""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        name = payload.get("name") if isinstance(payload, dict) else None
        if name:
            return JSONResponse({"probed": {str(name): await self.probe_agent(str(name))}})
        return JSONResponse({"probed": await self.probe_all()})

    async def admin_trace(self, request: Request) -> JSONResponse:
        """人类可读的审计视图：/admin/trace/<traceId>"""
        trace_id = request.path_params.get("trace_id")
        try:
            data = await self._get_trace({"traceId": trace_id})
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

        s = data["summary"]
        lines = [
            f"trace {data['traceId']}",
            f"  context : {s['contextId'] or '-'}",
            f"  plan    : {s['planId'] or '-'}",
            f"  agents  : {', '.join(s['agents']) or '-'}",
            f"  tasks   : {s['tasks']} (ok {s['completed']} / fail {s['failed']} / cancel {s['canceled']})",
            f"  elapsed : {s['durationMs']} ms",
            f"  usage   : {s['usage'] or '-'}",
            "",
            "  tasks:",
        ]
        for t in data["tasks"]:
            dur = f"{t['durationMs']}ms" if t["durationMs"] is not None else "-"
            lines.append(
                f"    [{t['state']:<9}] {str(t['stepId'] or '-'):<10} "
                f"{str(t['agent'] or '-'):<14} {dur:>8}  {(t['prompt'] or '').splitlines()[0][:50] if t['prompt'] else ''}"
            )
        lines.append("")
        lines.append("  timeline:")
        for e in data["timeline"]:
            text = (e["text"] or "").replace("\n", " ")[:90]
            lines.append(
                f"    {e['at'][11:23]}  {str(e['agent'] or '-'):<14} "
                f"{str(e['kind'] or '-'):<12} {text}"
            )
        return JSONResponse({"view": "\n".join(lines), **data})

    async def admin_traces(self, request: Request) -> JSONResponse:
        limit = clamp_limit(request.query_params.get("limit"), default=30)
        return JSONResponse({"traces": self.store.list_traces(limit=limit)})

    async def rpc(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return self._rpc_error(None, -32700, "Parse error")

        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        handlers = {
            "SendMessage": self._send_message,
            "message/send": self._send_message,          # A2A 0.3 兼容别名
            "GetTask": self._get_task,
            "tasks/get": self._get_task,
            "ListTasks": self._list_tasks,
            "tasks/list": self._list_tasks,
            "CancelTask": self._cancel_task,
            "tasks/cancel": self._cancel_task,
            # 编排扩展（非 A2A 标准方法，纯 A2A 客户端可完全不感知）
            "RunPlan": self._run_plan,
            "GetPlan": self._get_plan,
            # 审计
            "GetTrace": self._get_trace,
            "ListTraces": self._list_traces,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._rpc_error(req_id, -32601, f"Method not found: {method}")
        try:
            result = await handler(params)
        except NoRouteError as exc:
            return self._rpc_error(req_id, -32001, f"No route: {exc}")
        except LookupError as exc:
            return self._rpc_error(req_id, -32004, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._rpc_error(req_id, -32603, f"{type(exc).__name__}: {exc}")
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})

    # ------------------------------------------------------------------
    # JSON-RPC 方法
    # ------------------------------------------------------------------

    async def _send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message") or {}
        context_id = message.get("contextId") or params.get("contextId") or ""
        prompt = "".join(
            str(part.get("text", ""))
            for part in (message.get("parts") or [])
            if isinstance(part, dict)
        )
        want_async = bool(params.get("async"))
        # 参数校验必须在落库之前 —— 否则非法 timeout 会留下一个
        # 没人执行的 submitted 孤儿任务（A2A-10）
        timeout = parse_timeout(params.get("timeout"))
        # trace 边界 = 一次顶层请求：这里生成，往下传给所有 step / 子任务
        trace_id = str(params.get("traceId") or f"trace-{uuid.uuid4().hex[:12]}")

        # 1) 先落库 —— 任务在任何下游动作之前就已持久化
        task = self.store.create_task(
            context_id=context_id,
            prompt=prompt,
            state="submitted",
            trace_id=trace_id,
            metadata={
                "requestedAgent": params.get("agent"),
                "async": want_async,
            },
        )
        task_id = task["id"]
        self.store.add_message(task_id, role="user", kind="text",
                               content=[{"text": prompt}])

        # 2) 路由
        try:
            record = self.router.route(
                agent=params.get("agent"),
                tags=params.get("tags") or [],
            )
        except NoRouteError as exc:
            self.store.add_message(task_id, role="agent", kind="error",
                                   content=[{"text": str(exc)}])
            self.store.update_task(task_id, state="failed", error=str(exc), finished=True)
            return {"task": self._task_payload(task_id)}

        adapter = self._get_adapter(record)
        if adapter is None:
            err = f"agent {record.name} 的 kind={record.kind} 暂无可用的适配器实现"
            self.store.update_task(task_id, state="failed", error=err, finished=True)
            return {"task": self._task_payload(task_id)}

        timeout = parse_timeout(params.get("timeout"))
        session_id = (
            self.store.get_context_session(context_id, record.name)
            if context_id else None
        )
        args = (task_id, record, adapter, prompt, context_id, session_id, timeout)

        # 3) 异步模式：转后台，立即返回
        if want_async:
            self.store.update_task(task_id, agent=record.name, state="working")
            bg = asyncio.create_task(self._execute(*args))
            self._running[task_id] = bg
            bg.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
            return {"task": self._task_payload(task_id)}

        # 4) 同步模式：跑完再返回。
        #    但仍然登记进 _running —— 否则 CancelTask 找不到这个任务，
        #    只能改状态而拦不住执行，取消会「假成功」（A2A-01）。
        bg = asyncio.create_task(self._execute(*args))
        self._running[task_id] = bg
        bg.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
        try:
            await bg
        except asyncio.CancelledError:
            # 调用方取消了等待；任务本身已在 _execute 里被结算成 canceled
            pass
        return {"task": self._task_payload(task_id)}

    async def dispatch_task(
        self,
        prompt: str,
        *,
        agent: str | None = None,
        tags: list[str] | None = None,
        context_id: str = "",
        timeout: float | None = None,
        trace_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        parent_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """派发一个任务并等它结束，返回 A2A 格式的 task payload。

        与 JSON-RPC 的 SendMessage 共用同一段落库 / 路由 / 执行逻辑，
        编排层（Orchestrator）直接用这个，不重复实现。

        `task_id` 用于复用编排层预先落库的占位任务（A2A-07）——
        这样即使某个 step 最终没被执行，它在库里也有记录，审计能重建完整 plan。
        注意：给了 task_id 且该任务已存在时是**复用**，不能再 INSERT（会撞主键）。
        """
        existing = self.store.get_task(task_id) if task_id else None
        if existing is not None:
            # 复用编排层预落的占位任务：它的 metadata 里只有 planned/requestedAgent，
            # 这里补上 planId / stepId，否则 ListTasks 查不到编排归属（审计会缺线索）
            task = existing
            self.store.update_task(
                task_id,
                metadata={**(existing.get("metadata") or {}),
                          "planId": plan_id, "stepId": step_id,
                          "requestedAgent": agent},
            )
        else:
            task = self.store.create_task(
                context_id=context_id,
                prompt=prompt,
                state="submitted",
                task_id=task_id,
                trace_id=trace_id,
                plan_id=plan_id,
                step_id=step_id,
                parent_id=parent_id,
                metadata={"requestedAgent": agent, "planId": plan_id, "stepId": step_id},
            )
        task_id = task["id"]
        self.store.add_message(task_id, role="user", kind="text",
                               content=[{"text": prompt}])

        try:
            record = self.router.route(agent=agent, tags=tags or [])
        except NoRouteError as exc:
            self.store.add_message(task_id, role="agent", kind="error",
                                   content=[{"text": str(exc)}])
            self.store.update_task(task_id, state="failed", error=str(exc), finished=True)
            return self._task_payload(task_id)

        adapter = self._get_adapter(record)
        if adapter is None:
            err = f"agent {record.name} 的 kind={record.kind} 暂无可用的适配器实现"
            self.store.update_task(task_id, state="failed", error=err, finished=True)
            return self._task_payload(task_id)

        session_id = (
            self.store.get_context_session(context_id, record.name)
            if context_id else None
        )
        # 与 SendMessage 共用同一套「登记 → 等待」语义。
        # **必须登记进 _running**：否则 CancelTask 查不到这个任务，只能走兜底分支
        # 改状态而拦不住执行 —— 取消变成「假成功」，子进程继续跑、继续烧额度、
        # 继续往 workspace 写盘，而结果又被 only_from 挡在库外（N1）。
        bg = asyncio.create_task(
            self._execute(task_id, record, adapter, prompt, context_id,
                          session_id, timeout)
        )
        self._running[task_id] = bg
        bg.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
        try:
            await bg
        except asyncio.CancelledError:
            # 调用方不等了（RPC handler 被取消）：把取消转嫁给这一步并等它结算完。
            # 只 re-raise 不等它的话，会留下一个「还在后台跑、但没人知道」的任务。
            bg.cancel()
            await asyncio.gather(bg, return_exceptions=True)
            raise
        return self._task_payload(task_id)

    async def _execute(
        self,
        task_id: str,
        record,
        adapter: Adapter,
        prompt: str,
        context_id: str,
        session_id: str | None,
        timeout: float | None,
    ) -> None:
        """真正派活并落库。同步与异步两种模式共用这一段。

        会话续接按 (contextId, agent) 串行化：读 session → 执行 → 写回
        这中间不能并发，否则两个请求会从同一个旧 session 分叉，
        后完成的覆盖前者的映射（A2A-04）。

        终态写入一律带 `only_from`：取消和完成可能并发，
        谁先到谁生效，后到的不许覆盖（A2A-01）。
        """
        # 「真正开始执行」的时刻。**与 created_at 区分开**：编排层会预先为每个
        # step 落占位任务，记录在 plan 开始那一刻就诞生了，但这一层可能几分钟后
        # 才轮到它。拿 created_at 当开始时刻，层间排队等待会被算进执行时长（N2）。
        # 带 only_from：任务若在派活前就已被取消，不该再补 started_at。
        self.store.update_task(task_id, started=True, only_from=ACTIVE_STATES)

        lock = self._context_lock(context_id, record.name) if context_id else None
        if lock is not None:
            await lock.acquire()
        try:
            async with self._semaphore():
                self.store.update_task(
                    task_id, agent=record.name, state="working",
                    only_from=("submitted",),
                )
                self.store.add_message(
                    task_id, role="agent", kind="status",
                    content=[{"text": f"dispatched to {record.name}"}],
                    metadata={"agent": record.name, "endpoint": record.endpoint},
                )
                # 拿锁之后再读 session，保证读到的是最新的
                if context_id:
                    session_id = self.store.get_context_session(context_id, record.name)
                result = await adapter.call(
                    prompt,
                    context_id=context_id or None,
                    session_id=session_id,
                    timeout=timeout,
                )
                # 会话映射的写回也在锁内 —— 出锁即已落库
                if context_id and result.session_id:
                    self.store.set_context_session(
                        context_id, record.name, result.session_id
                    )
        except asyncio.CancelledError:
            self.store.add_message(task_id, role="agent", kind="status",
                                   content=[{"text": "canceled by caller"}])
            self.store.update_task(task_id, state="canceled",
                                   error="canceled by caller", finished=True,
                                   only_from=ACTIVE_STATES)
            raise
        except Exception as exc:  # noqa: BLE001
            self.store.update_task(task_id, state="failed",
                                   error=f"{type(exc).__name__}: {exc}",
                                   finished=True, only_from=ACTIVE_STATES)
            return
        finally:
            if lock is not None:
                lock.release()

        # 过程回传落库
        for event in result.events:
            self.store.add_message(
                task_id, role="agent", kind=event.kind,
                content=[{"text": event.text}],
                metadata=event.metadata,
            )

        # 终态 + artifact
        if result.ok:
            self.store.add_artifact(
                task_id, name="response",
                content=[{"text": result.text}],
                metadata={
                    **(result.metadata or {}),
                    "usage": result.usage,
                    **({"sessionId": result.session_id} if result.session_id else {}),
                    "agent": record.name,
                },
            )
            self.store.update_task(task_id, state="completed", finished=True,
                                   only_from=ACTIVE_STATES)
        else:
            self.store.update_task(task_id, state="failed",
                                   error=result.error or "unknown failure",
                                   finished=True, only_from=ACTIVE_STATES)

    async def _get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id") or params.get("taskId")
        task = self.store.get_task(task_id) if task_id else None
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        return self._task_payload(task_id)

    async def _list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = clamp_limit(params.get("limit"), default=50)
        offset = max(int(params.get("offset") or 0), 0)
        tasks = self.store.list_tasks(limit=limit, offset=offset)
        return {
            "tasks": [
                _to_a2a_task(t, self.store.list_messages(t["id"]),
                             self.store.list_artifacts(t["id"]))
                for t in tasks
            ],
            "total": self.store.count_tasks(),
        }

    async def _run_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        """编排入口：按显式 plan 跑一条流水线。

        plan 由调用方给出（见 core/orchestrator.py 的说明），
        每一步都落库成一个 task 并挂上 plan_id / step_id，可事后完整追溯。
        """
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PlanError("params.steps 必须是非空数组")
        context_id = params.get("contextId") or ""
        plan_input = str(params.get("input") or "")
        plan_id = params.get("planId") or f"plan-{uuid.uuid4().hex[:12]}"
        # 整个 plan 共享一个 trace：编排里所有 step 都挂在同一条审计时间线上
        trace_id = str(params.get("traceId") or f"trace-{uuid.uuid4().hex[:12]}")

        result = await self.orchestrator.run(
            steps, context_id=context_id, plan_input=plan_input,
            plan_id=plan_id, trace_id=trace_id,
        )
        payload = result.to_dict()
        payload["traceId"] = trace_id
        return payload

    async def _get_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        """查一个 plan 下所有 step 任务的落库记录。"""
        plan_id = params.get("planId") or params.get("plan_id")
        if not plan_id:
            raise LookupError("缺少 planId")
        tasks = self.store.list_plan_tasks(str(plan_id))
        if not tasks:
            raise LookupError(f"plan not found: {plan_id}")
        return {
            "planId": plan_id,
            "steps": [
                {
                    "stepId": t["step_id"],
                    "taskId": t["id"],
                    "agent": t["agent"],
                    "state": t["state"],
                    "error": t["error"],
                }
                for t in tasks
            ],
        }

    async def _get_trace(self, params: dict[str, Any]) -> dict[str, Any]:
        """审计视图：一条时间线看全貌。

        返回三段：
          summary  —— 起止时间、总耗时、涉及哪些 agent、汇总用量
          tasks    —— 这次请求下的所有任务（含 plan step 与父子关系）
          timeline —— 所有消息事件按时间拉平，即过程回传的完整复原
        """
        trace_id = params.get("traceId") or params.get("trace_id")
        if not trace_id:
            # 也允许用 taskId 反查它所属的 trace
            task_id = params.get("taskId") or params.get("id")
            if task_id:
                task = self.store.get_task(str(task_id))
                trace_id = task.get("trace_id") if task else None
        if not trace_id:
            raise LookupError("缺少 traceId（或给 taskId 反查）")

        tasks = self.store.list_trace_tasks(str(trace_id))
        if not tasks:
            raise LookupError(f"trace not found: {trace_id}")

        messages = self.store.list_trace_messages(str(trace_id))

        started = min(t["created_at"] for t in tasks)
        finished_candidates = [t["finished_at"] for t in tasks if t.get("finished_at")]
        finished = max(finished_candidates) if finished_candidates else None
        duration_ms = _duration_ms(started, finished)

        # 汇总用量：从各任务的 artifact metadata 里取，并归一到统一口径
        raw_usage: dict[str, float] = {}
        for task in tasks:
            for artifact in self.store.list_artifacts(task["id"]):
                usage = (artifact.get("metadata") or {}).get("usage") or {}
                for key, value in usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        raw_usage[key] = raw_usage.get(key, 0) + value
        total_usage = normalize_usage(raw_usage)

        return {
            "traceId": trace_id,
            "summary": {
                "startedAt": started,
                "finishedAt": finished,
                "durationMs": duration_ms,
                "tasks": len(tasks),
                "completed": sum(1 for t in tasks if t["state"] == "completed"),
                "failed": sum(1 for t in tasks if t["state"] == "failed"),
                "canceled": sum(1 for t in tasks if t["state"] == "canceled"),
                "agents": sorted({t["agent"] for t in tasks if t.get("agent")}),
                "contextId": tasks[0].get("context_id"),
                "planId": next((t["plan_id"] for t in tasks if t.get("plan_id")), None),
                "usage": total_usage,
            },
            "tasks": [
                {
                    "taskId": t["id"],
                    "stepId": t.get("step_id"),
                    "planId": t.get("plan_id"),
                    "parentId": t.get("parent_id"),
                    "agent": t.get("agent"),
                    "state": t["state"],
                    "createdAt": t["created_at"],
                    "startedAt": _task_started_at(t),
                    "finishedAt": t.get("finished_at"),
                    "durationMs": _duration_ms(_task_started_at(t),
                                               t.get("finished_at")),
                    "prompt": (t.get("prompt") or "")[:200],
                    "error": t.get("error"),
                }
                for t in tasks
            ],
            "timeline": [
                {
                    "at": m["created_at"],
                    "taskId": m["task_id"],
                    "agent": m.get("agent"),
                    "stepId": m.get("step_id"),
                    "kind": m.get("kind"),
                    "text": "".join(
                        str(p.get("text", "")) for p in (m.get("content") or [])
                        if isinstance(p, dict)
                    )[:300],
                    **({"metadata": m["metadata"]} if m.get("metadata") else {}),
                }
                for m in messages
            ],
        }

    async def _list_traces(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = clamp_limit(params.get("limit"), default=50)
        return {"traces": self.store.list_traces(limit=limit)}

    async def _cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        """取消任务。

        对 CLI 类 agent，会杀掉整棵进程树（见 adapters/cli.py 的 _kill）；
        对 HTTP 类 agent 只能中断本地请求，下游服务端的执行不受影响。
        """
        task_id = params.get("id") or params.get("taskId")
        task = self.store.get_task(task_id) if task_id else None
        if task is None:
            raise LookupError(f"task not found: {task_id}")

        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            # _execute 内部已把状态置为 canceled，这里不重复写

        # 兜底：任务处于活跃态但没在跑（例如进程重启后遗留的孤儿任务）
        current = self.store.get_task(task_id)
        if current and current["state"] in ACTIVE_STATES:
            self.store.add_message(task_id, role="agent", kind="status",
                                   content=[{"text": "canceled (no active worker)"}])
            self.store.update_task(task_id, state="canceled",
                                   error="canceled: no active worker",
                                   finished=True, only_from=ACTIVE_STATES)
        return self._task_payload(task_id)

    # ------------------------------------------------------------------
    # 健康探测
    #
    # 探测逻辑放在 Hub 而不是 Registry：因为只有 Hub 拿得到适配器实例，
    # 而 CLI 类 agent 根本没有 endpoint，只能靠适配器自己的 probe（跑 --version）。
    # Registry 保持纯存储职责。
    # ------------------------------------------------------------------

    def recover_orphans(self) -> int:
        """结算上次进程遗留的活跃任务（A2A-09）。

        这些任务已经没有 worker 在跑了，但状态还停在 submitted/working，
        调用方会一直等下去。启动时统一标成 failed（interrupted）。

        **注意语义**：`ok=false` 不代表「什么都没干」—— 任务被中断时，
        下游可能已经把文件写进了 workspace。所以错误文案里明确提示先验盘，
        而不是让调用方以为可以安全重跑。
        """
        orphans = self.store.list_active_tasks()
        for task in orphans:
            self.store.add_message(
                task["id"], role="agent", kind="status",
                content=[{"text": (
                    "interrupted: hub restarted while this task was active; "
                    "side effects (e.g. files written to workspace) may already exist"
                )}],
            )
            self.store.update_task(
                task["id"], state="failed",
                error=("interrupted: hub 在任务执行期间重启。"
                       "**该任务可能已产生副作用**（写入 workspace 的文件等）—— "
                       "重跑前请先检查 workspace，不要仅凭 ok=false 判定它什么都没做。"),
                finished=True, only_from=ACTIVE_STATES,
                metadata={**(task.get("metadata") or {}),
                          "interrupted": True, "sideEffectsPossible": True},
            )
        return len(orphans)

    async def probe_agent(self, name: str, *, timeout: float = 20.0) -> str:
        rec = self.registry.get(name)
        if rec is None:
            return "down"
        adapter = self._get_adapter(rec)
        if adapter is not None:
            health = await adapter.probe()
            # 适配器探测时若顺带拿到了 Agent Card，就用它刷新注册表 ——
            # 否则下游更新了 skills，路由仍按注册那一刻的旧能力表走（A2A-12）
            card = getattr(adapter, "last_card", None)
            if card and card != (rec.card or {}):
                self.store.upsert_agent(
                    name=rec.name, kind=rec.kind, endpoint=rec.endpoint,
                    card=card, tags=rec.tags, config=rec.config,
                    enabled=rec.enabled,
                )
        else:
            health = await self.registry.probe(name, timeout=timeout)
        self.registry.set_health(name, health)
        return health

    async def probe_all(self, *, timeout: float = 20.0) -> dict[str, str]:
        names = [r.name for r in self.registry.list()]
        if not names:
            return {}
        results = await asyncio.gather(
            *(self.probe_agent(n, timeout=timeout) for n in names),
            return_exceptions=True,
        )
        return {n: (r if isinstance(r, str) else "down") for n, r in zip(names, results)}

    # ------------------------------------------------------------------

    def _task_payload(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        assert task is not None
        return _to_a2a_task(
            task,
            self.store.list_messages(task_id),
            self.store.list_artifacts(task_id),
        )

    def _get_adapter(self, record) -> Adapter | None:
        """按需构建适配器，已构建的缓存复用。

        缓存键带上**配置签名**：同名 agent 的 endpoint / config 被改过之后，
        必须重建适配器，否则新任务会路由到新记录、却用着旧 endpoint 派活（A2A-08）。

        CLI 类的配置放在 record.config 里，形如：
            {"adapter": "DshCLI",
             "options": {"command": ["...dsh.cmd"], "cwd": "...", "timeout": 300}}
        """
        signature = json.dumps(
            {"kind": record.kind, "endpoint": record.endpoint, "config": record.config},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        cached = self._adapters.get(record.name)
        if cached is not None and cached[0] in ("", signature):
            return cached[1]

        adapter: Adapter | None = None

        if record.kind == "a2a_http" and record.endpoint:
            from adapters.a2a_http import A2AHttpAdapter

            adapter = A2AHttpAdapter(record.name, record.endpoint)

        elif record.kind == "cli":
            from adapters import cli as cli_mod

            spec = record.config or {}
            factory = getattr(cli_mod, str(spec.get("adapter") or ""), None)
            if factory is not None:
                options = dict(spec.get("options") or {})
                # timeout 允许写在 options 里，也允许写在 config 顶层
                if "timeout" not in options and spec.get("timeout") is not None:
                    options["timeout"] = spec["timeout"]
                options.setdefault("timeout", 600.0)
                # **必须给独立工作目录**：CLI 子进程的 cwd 默认继承 hub 进程，
                # 而 hub 通常就跑在代码目录里 —— 于是 agent 随手写个文件
                # 就落进仓库（实测把 history-of-computing.md / memory/ 写进了 a2a-hub/）。
                options.setdefault("cwd", str(self.workspace_dir))
                adapter = factory(record.name, **options)

        if adapter is None:
            return None

        old = self._adapters.get(record.name)
        if old is not None and old[0] != "":
            # 配置变了：把旧适配器关掉，别让它继续占着资源
            asyncio.create_task(_safe_close(old[1]))
        self._adapters[record.name] = (signature, adapter)
        return adapter

    @staticmethod
    def _rpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        })

    # ------------------------------------------------------------------

    def build_app(self) -> Starlette:
        routes = [
            Route("/.well-known/agent-card.json", self.agent_card, methods=["GET"]),
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/admin/agents", self.admin_agents, methods=["GET"]),
            Route("/admin/tasks", self.admin_tasks, methods=["GET"]),
            Route("/admin/traces", self.admin_traces, methods=["GET"]),
            Route("/admin/trace/{trace_id}", self.admin_trace, methods=["GET"]),
            Route("/admin/probe", self.admin_probe, methods=["POST"]),
            # 控制台（只读面板，零构建、零前端依赖）
            Route("/console", self.console, methods=["GET"]),
            Route("/", self.rpc, methods=["POST"]),
        ]
        middleware = []
        if self.auth_token:
            middleware.append(Middleware(BearerAuthMiddleware, token=self.auth_token))
        return Starlette(routes=routes, middleware=middleware)
