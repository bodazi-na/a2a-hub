# -*- coding: utf-8 -*-
"""Agent 注册表：Agent Card 的注册、持久化与健康探测。

设计要点
--------
- 注册信息落在 store 的 agents 表里，进程重启不丢。
- 健康探测走 HTTP 拉 Agent Card；**必须 trust_env=False**，
  否则本机的系统代理（127.0.0.1:7393）会把本地探测全部变成 502。
- 探测失败不删记录，只把 health 置为 down ——
  「工具没开」和「这个 agent 不存在」是两回事。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from .store import Store

A2A_CARD_PATH = "/.well-known/agent-card.json"

HEALTH_UNKNOWN = "unknown"
HEALTH_OK = "ok"
HEALTH_DOWN = "down"


@dataclass
class AgentRecord:
    name: str
    kind: str                                   # a2a_http | cli | mcp_http
    endpoint: str | None = None
    card: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    health: str = HEALTH_UNKNOWN
    last_seen: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def skills(self) -> list[str]:
        """从 Agent Card 里抽 skill id，作为路由的能力来源。"""
        out: list[str] = []
        for skill in (self.card or {}).get("skills", []) or []:
            sid = skill.get("id") or skill.get("name")
            if sid:
                out.append(str(sid))
        return out

    def all_tags(self) -> list[str]:
        return sorted(set(self.tags) | set(self.skills))


class Registry:
    """注册表读写与探测。所有变更立即落库。"""

    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def register(self, record: AgentRecord) -> AgentRecord:
        self.store.upsert_agent(
            name=record.name,
            kind=record.kind,
            endpoint=record.endpoint,
            card=record.card,
            tags=record.tags,
            config=record.config,
            enabled=record.enabled,
        )
        got = self.get(record.name)
        assert got is not None, "upsert 后应当能读回"
        return got

    def unregister(self, name: str) -> None:
        self.store.delete_agent(name)

    def set_health(self, name: str, health: str) -> None:
        self.store.set_agent_health(name, health)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, name: str) -> AgentRecord | None:
        row = self.store.get_agent(name)
        return self._to_record(row) if row else None

    def list(self, *, enabled_only: bool = True) -> list[AgentRecord]:
        rows = self.store.list_agents(enabled_only=enabled_only)
        return [self._to_record(r) for r in rows]

    def find_by_tag(self, tag: str) -> list[AgentRecord]:
        return [r for r in self.list() if tag in r.all_tags()]

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    async def probe(self, name: str, *, timeout: float = 5.0) -> str:
        """探测单个 agent 并落库，返回 health。

        探测成功时**顺手刷新 Agent Card** —— 下游升级了 skills，
        注册表要跟着更新，否则路由会一直用注册那一刻的旧能力表。
        """
        rec = self.get(name)
        if rec is None:
            return HEALTH_DOWN
        if not rec.endpoint:
            return HEALTH_UNKNOWN

        if rec.kind == "a2a_http":
            card = await self._fetch_card(rec.endpoint, timeout=timeout)
            if card is None:
                health = HEALTH_DOWN
            else:
                health = HEALTH_OK
                if card != (rec.card or {}):
                    self.store.upsert_agent(
                        name=rec.name,
                        kind=rec.kind,
                        endpoint=rec.endpoint,
                        card=card,
                        tags=rec.tags,
                        config=rec.config,
                        enabled=rec.enabled,
                    )
        else:
            health = HEALTH_UNKNOWN

        self.set_health(name, health)
        return health

    async def probe_all(self, *, timeout: float = 5.0) -> dict[str, str]:
        names = [r.name for r in self.list()]
        if not names:
            return {}
        results = await asyncio.gather(
            *(self.probe(n, timeout=timeout) for n in names),
            return_exceptions=True,
        )
        return {
            n: (r if isinstance(r, str) else HEALTH_DOWN)
            for n, r in zip(names, results)
        }

    @staticmethod
    async def _fetch_card(endpoint: str, *, timeout: float) -> dict[str, Any] | None:
        """拉 Agent Card；任何失败都返回 None（调用方据此判定 down）。"""
        url = endpoint.rstrip("/") + A2A_CARD_PATH
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------

    @staticmethod
    def _to_record(row: dict[str, Any]) -> AgentRecord:
        return AgentRecord(
            name=row["name"],
            kind=row["kind"],
            endpoint=row.get("endpoint"),
            card=row.get("card") or {},
            tags=row.get("tags") or [],
            config=row.get("config") or {},
            enabled=bool(row.get("enabled", True)),
            health=row.get("health") or HEALTH_UNKNOWN,
            last_seen=row.get("last_seen"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
