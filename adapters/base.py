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
from typing import Any

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
    ) -> CallResult:
        """派一个任务给下游，等它结束并返回结果。

        context_id 是 hub 侧的会话标识；session_id 是下游自己的会话标识
        （由上一次 call 返回并落库）。适配器负责把后者喂给下游以续接。
        """

    async def close(self) -> None:
        return None
