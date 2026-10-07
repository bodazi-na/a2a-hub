# -*- coding: utf-8 -*-
"""适配器协议：把任意执行体统一成 detect → probe → call。

设计意图
--------
下游执行体千差万别（HTTP A2A 服务、headless CLI、MCP 服务），
但对内核来说它们只需要提供同一件事：

    async call(prompt, context_id) -> CallResult

于是 core 层完全不需要知道下游是什么形态 —— 这就是「薄核心 + 厚适配器」
在代码结构上的落点。新增一种执行体 = 新增一个 Adapter 子类，core 一行不改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def utcnow() -> str:
    """当前 UTC 时刻（ISO8601，微秒精度）。

    与 `a2a_hub/core/store.py` 里的同名函数**故意重复**：分层方向是 core → adapters，
    适配器不该反向 import core。这个函数只有三行，重复的代价远小于
    在适配器层引入对 core 的依赖 —— 那会让「新增一种执行体不必碰 core」
    这条设计约束失效。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

# 过程事件的统一语义。下游各自的原始事件都往这几个桶里映射，
# 这样 core 层做过程回传时不必关心下游是 dsh 还是 qoder。
EVENT_THINKING = "thinking"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_TEXT = "text"
EVENT_STATUS = "status"

HEALTH_OK = "ok"
HEALTH_DOWN = "down"
HEALTH_UNKNOWN = "unknown"


@dataclass
class Event:
    """一条过程事件。"""

    kind: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # 事件**发生**时刻（ISO8601 UTC）。适配器解析出这条事件时立刻打上。
    #
    # 为什么非有不可：以前这里是空的，落库时统一拿「插入时刻」当 created_at，
    # 于是同一批事件的时间戳全挤在几毫秒内 —— 实测 qoder 真跑了 181 秒，
    # 它的全部过程事件时间戳落在 64 毫秒之内。**顺序还在，时序丢了**；
    # 控制台把这段数据渲染成 timeline，读者会自然理解成时序（N5）。
    ts: str | None = None


# 事件回调：适配器每解析出一条事件就**立刻**调用它（实时流式推送用）。
# 声明成 async 是因为推送要写进事件总线，可能需要 await 队列。
OnEvent = Callable[[Event], Awaitable[None]]


@dataclass
class CallResult:
    """一次派活的结果。

    `usage` 与 `metadata` 严格分开：`usage` 只放 token 用量，其余下游元信息
    （exitCode / profile / 各类 id）进 `metadata`。混在一起会让「用量」这个
    字段失去语义 —— 想统计成本时得先猜哪些键是数字、哪些是标识。
    """

    ok: bool
    text: str = ""                                            # 最终答复
    session_id: str | None = None                             # 下游会话 id，用于续接
    events: list[Event] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)        # 只放 token 用量
    metadata: dict[str, Any] = field(default_factory=dict)     # 其余下游元信息
    error: str | None = None
    # 失败是否由「超时」造成。调用方据此决定要不要重试 ——
    # 超时说明下游确实跑了那么久，重试等于把开销翻倍；
    # 而「续接的 session 已失效」这类失败通常**秒级**返回，重试是划算的。
    timed_out: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 下游差异归一化（所有适配器共用）
#
# 适配器的核心职责就是吃掉下游差异。这两件事最容易漏，也最容易埋雷：
#   1. 会话 id 的字段名不统一（dshSessionId / codexThreadId / sessionId / ...）
#   2. 把下游返回的整个 dict 当成某个内部字段用（典型是拿整个 metadata 当 usage）
# 所以统一在这里做一次，任何适配器都走同一套归一化。
# ---------------------------------------------------------------------------

USAGE_CONTAINER_KEYS = ("usage", "tokenUsage", "token_usage", "tokens")

SESSION_ID_KEYS = (
    "dshSessionId",
    "codexThreadId",
    "sessionId",
    "session_id",
    "threadId",
    "thread_id",
)


def pick_session_id(meta: dict[str, Any]) -> str | None:
    """从下游返回的元信息里提取会话 id。"""
    for key in SESSION_ID_KEYS:
        value = meta.get(key)
        if value:
            return str(value)
    for key, value in meta.items():
        lowered = key.lower()
        if value and lowered.endswith("id") and ("session" in lowered or "thread" in lowered):
            return str(value)
    return None


def pick_usage(meta: dict[str, Any]) -> dict[str, Any]:
    """提取**真正的用量**，不要把整个 metadata 都当成 usage。

    先找已知的用量容器键；找不到再退化为「键名含 token 的数值」。
    """
    for key in USAGE_CONTAINER_KEYS:
        value = meta.get(key)
        if isinstance(value, dict) and value:
            return dict(value)
    return {
        key: value
        for key, value in meta.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and "token" in key.lower()
    }


class Adapter(ABC):
    """下游执行体的统一门面。

    kind 供注册表分类；detect / probe 回答「能不能用」，
    call 回答「怎么派活」。
    """

    kind: str = "unknown"

    def __init__(self, name: str, endpoint: str | None = None, **options: Any):
        self.name = name
        self.endpoint = endpoint
        self.options = options

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------

    def detect(self) -> bool:
        """本机是否存在这个执行体。

        HTTP 类默认认为存在（进程可能没开，交给 probe 判断）；
        CLI 类应当覆写它去查进程或可执行文件。
        """
        return True

    @abstractmethod
    async def probe(self) -> str:
        """返回 ok / down / unknown。"""

    # ------------------------------------------------------------------
    # 派活
    # ------------------------------------------------------------------

    @abstractmethod
    async def call(
        self,
        prompt: str,
        *,
        context_id: str | None = None,
        session_id: str | None = None,
        timeout: float = 600.0,
        on_event: OnEvent | None = None,
    ) -> CallResult:
        """派一个任务给下游，等它结束并返回结果。

        context_id 是 hub 侧的会话标识；session_id 是下游自己的会话标识
        （由上一次 call 返回并落库）。适配器负责把后者喂给下游以续接。

        `on_event` 是**可选的实时事件回调**。给了它，适配器每解析出一条过程
        事件就立刻回调一次（同时仍照常收集进 `CallResult.events` 以便落库）。
        不给则行为和以前完全一样 —— 攒完一次性返回。所以这是纯增量接口，
        不传回调的调用方一行都不用改。
        """

    async def close(self) -> None:
        return None
