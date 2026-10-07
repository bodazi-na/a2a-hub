# -*- coding: utf-8 -*-
"""适配器层：每种下游执行体一个实现，统一暴露 base.Adapter 契约。

两类适配器：
  - `A2AHttpAdapter`  —— 对接已经在跑的 A2A 服务（本机的 9100 / 9101 桥）
  - `CLIAdapter` 系   —— 直接起子进程调 CLI 本体（claude / qodercli / codex / dsh）
"""

from .a2a_http import A2AHttpAdapter
from .base import (
    EVENT_STATUS,
    EVENT_TEXT,
    EVENT_THINKING,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_UNKNOWN,
    Adapter,
    CallResult,
    Event,
    pick_session_id,
    pick_usage,
)
from .cli import (
    CLIAdapter,
    ClaudeCLI,
    ClaudeStyleCLI,
    CodexCLI,
    DshCLI,
    QoderCLI,
)

__all__ = [
    "Adapter",
    "CallResult",
    "Event",
    "A2AHttpAdapter",
    "CLIAdapter",
    "ClaudeStyleCLI",
    "ClaudeCLI",
    "QoderCLI",
    "CodexCLI",
    "DshCLI",
    "pick_session_id",
    "pick_usage",
    "HEALTH_OK",
    "HEALTH_DOWN",
    "HEALTH_UNKNOWN",
    "EVENT_TEXT",
    "EVENT_THINKING",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_STATUS",
]
