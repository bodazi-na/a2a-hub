# -*- coding: utf-8 -*-
"""路由：按能力把任务派给合适的 agent。

第一版策略（够用、可扩展）：
  1. 显式指定 agent 名 → 直接用（不校验健康，尊重调用方意图）
  2. 按 tag / skill 匹配 → 只在 health=ok 的 agent 里挑
  3. 多个候选 → 取注册顺序第一个（后续可替换为优先级 / 负载策略）
  4. 无匹配 → 抛 NoRouteError，让上层明确失败而不是静默挑一个

能力标签有两个来源：注册时显式给的 tags，以及 Agent Card 里的 skills。
两者取并集，这样「上游 agent 自己声明的能力」能直接被路由用上。
"""

from __future__ import annotations

from .registry import AgentRecord, Registry


class NoRouteError(LookupError):
    """没有可用的 agent 能承接这个任务。"""


class Router:
    def __init__(self, registry: Registry):
        self.registry = registry

    def route(
        self,
        *,
        agent: str | None = None,
        tags: list[str] | None = None,
    ) -> AgentRecord:
        if agent:
            # 显式指定时不校验健康：调用方说了算，它可能就是要拿这个节点试
            rec = self.registry.get(agent)
            if rec is None or not rec.enabled:
                raise NoRouteError(f"agent 未注册或已禁用: {agent}")
            return rec

        wanted = [t for t in (tags or []) if t]
        all_agents = self.registry.list()
        healthy = [r for r in all_agents if r.health == "ok"]

        # **fail-closed**（A2A-17）：只回退到「尚未探测」的节点，
        # 不回退到已知 down 的节点。否则全 down 时会照样派活，
        # 把注定失败的任务堆起来 —— 那比明确报错更糟。
        candidates = healthy or [r for r in all_agents if r.health == "unknown"]

        if not candidates:
            if all_agents:
                names = [r.name for r in all_agents]
                raise NoRouteError(
                    f"所有已启用的 agent 都不可用（health=down）: {names}；"
                    f"可先跑 probe 刷新健康状态，或显式指定 agent 强制派发"
                )
            raise NoRouteError("注册表为空，没有可用的 agent")

        if not wanted:
            return candidates[0]

        for tag in wanted:
            hit = [r for r in candidates if tag in r.all_tags()]
            if hit:
                return hit[0]

        raise NoRouteError(f"没有 agent 提供这些能力: {wanted}")

    def explain(self) -> list[dict]:
        """给控制台 / 调试用：当前每个 agent 能被哪些 tag 命中。"""
        out = []
        for rec in self.registry.list(enabled_only=False):
            out.append({
                "name": rec.name,
                "kind": rec.kind,
                "endpoint": rec.endpoint,
                "health": rec.health,
                "enabled": rec.enabled,
                "tags": rec.all_tags(),
            })
        return out
