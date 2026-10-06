# ADR-001：协议面只做 A2A，MCP 作为可选 facade

## 状态

已采纳（2026-10-06）

## 背景

本机有 6 个想纳管的 AI 工具（Codex / WorkBuddy / Qoder / DeepSeek Harness /
Claude Code / Claude Desktop），它们各自支持的接入方式并不统一：

- 有的只提供 CLI（claude / codex / qodercli / dsh）
- 有的只暴露 MCP（WorkBuddy、Qoder、Claude Code 都能作 MCP 客户端）
- 有的本来就是 A2A 服务（早期的两条自建桥）

核心要选一个「对内统一」的协议面。可选方案：

| 方案 | 说明 |
| --- | --- |
| A. 只做 A2A | core 只讲 A2A；想用 MCP 的平台另加一层 |
| B. 只做 MCP | 把 hub 做成 MCP server，各平台当工具调 |
| C. A2A 为主 + MCP facade | core 纯 A2A，MCP 作为独立可选组件 |
| D. 同时支持 REST/OpenAPI | 再开一个通用 HTTP 面 |

## 决策

**选 C。**

- `core/` 与 `adapters/` **只讲 A2A** —— 不含任何 MCP 依赖
- `mcp_facade/` 是**独立可选组件**，通过 HTTP 调 hub（**不 import core**），
  装 `.[mcp]` 才有；不装它 core 完整可用

## 理由

**为什么不做 REST/OpenAPI（否掉 D）**：A2A 已经是一个带任务模型、状态机、
artifact 和会话续接的协议。再开一个 REST 面意味着**两套任务语义要维护同步** ——
而 hub 的核心价值恰恰是「一份可信的任务记录」。多一个入口就多一份语义漂移的风险。

**为什么 MCP 不做进 core（否掉 B）**：

1. **协议定位不同**。A2A 是 agent↔agent（有任务生命周期），MCP 是
   host↔tool（无状态工具调用）。把 hub 降级成「一组工具」，就丢掉了任务模型。
2. **依赖代价**。MCP SDK 是个重依赖；core 的依赖只有 starlette / uvicorn / httpx。
3. **有更好的位置**。MCP 是「让别的平台能调 hub」，那是**接入层**的事，
   不是内核的事。

**为什么 facade 走 HTTP 而不是 import core**：走 HTTP 意味着 facade 与 hub
是**两个进程**，facade 可以放在任何地方（甚至另一台机器）。走 import 会让
「MCP 面」和「A2A 面」共享同一份内存状态，反而更难推理。

## 后果

**变容易的**：

- core 的依赖面极小，移植和审计都简单
- 「薄核心 / 厚适配器」这条约束有了一个具体的检验点：
  单元级测试全绿就说明 core 与具体工具无关
- 想加别的接入面（比如 gRPC facade）照抄 `mcp_facade/` 的形状即可

**变难的**：

- 同时用 A2A 和 MCP 两种方式调 hub 时，要理解它们是**两条路径到同一个内核**，
  不是两套实现
- facade 多一跳 HTTP，延迟略高（实测可忽略）

## 相关

- `docs/architecture.md` 的「分层」节
- ADR-002（薄核心 / 厚适配器）
