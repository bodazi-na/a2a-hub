# -*- coding: utf-8 -*-
"""a2a-hub —— 多 Agent A2A 协调内核。

**顶层只占这一个名字。** 以前 `core` / `adapters` / `probes` / `mcp_facade` /
`hub` 是五个**顶层**模块，装进环境后会占据 site-packages 的顶层命名空间 ——
`core` 这种名字几乎必然和别人撞车。现在它们都收进 `a2a_hub` 底下：

    a2a_hub.core         平台无关的持久化 / 注册 / 路由 / A2A 服务端
    a2a_hub.adapters     每种下游执行体一层（HTTP / CLI）
    a2a_hub.probes       本机 AI 工具进程探测
    a2a_hub.mcp_facade   可选：把 hub 包成 MCP server
    a2a_hub.cli          命令行入口

于是「它是个服务，不是一个 import 进去用的库」这句话里**唯一的硬伤**没有了 ——
现在它既是个服务，也可以安心当库用。
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
