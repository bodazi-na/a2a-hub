# -*- coding: utf-8 -*-
"""MCP facade：让任意支持 MCP 的 agent 平台都能调用 a2a-hub。

它是**可选组件** —— core/ 保持纯 A2A 不变，删掉这个目录 hub 依然完整。
"""

from .server import main, server

__all__ = ["server", "main"]
