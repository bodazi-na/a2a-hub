# -*- coding: utf-8 -*-
"""并行度分析：从任务的起止时间回答「并行到底用上没有」。

为什么单独成模块
----------------
这是一段**纯计算** —— 不碰 store、不碰 HTTP、不读时钟。所以它可以在单元级
测试里穷举边界（空 / 单任务 / 全串行 / 全并行 / 中间有空洞），
而不必起 hub 或造假下游。

要回答的问题
------------
「跑得快不快」不能只看总耗时 —— 那看不出并行有没有生效。真正有信息量的是：

- **平均并行度** = 串行总时长 / 墙钟。1.0 表示完全串行，N 表示平均有 N 个
  agent 同时干活。**这是最能一眼看出问题的数字。**
- **峰值并行度** = 最多时几个同时跑。
- **空闲率** = 墙钟里一个 agent 都没在跑的时间占比。高空闲率通常意味着
  调度在等锁、等前一层收尾，或者干脆是编排本身的开销。
- **编排开销** = 墙钟 − 理论最短。理论最短按**观测分层**算：
  每一层至少要花「该层最慢那个」的时间，加起来就是不可压缩的下界。

关于「按观测分层」
------------------
编排器是**严格分层**的：第 N+1 层要等第 N 层全部结束才开始。所以
「一个任务在上一批全部结束之后才启动」就是一条真实的层边界 ——
这是从**实际执行**反推出来的，比计划里的依赖声明更贴近事实。

代价是：它算不出严格的关键路径（那需要计划里的依赖图），
而且如果调用方绕过编排器自己串任务，分层推断未必有意义。
所以返回值里带了 `layerSource` 字段，把口径写清楚。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 判定「同一层」的启动时刻容差（毫秒）。
#
# 为什么按**启动时刻聚类**而不是按「上一批结束」：
# 严格分层下，第 N+1 层恰好是「第 N 层全部结束的那一刻」开始 —— 用
# `start > 上一层结束` 判定会因为边界取等而把两层并成一层（实测踩到：
# 串行的两个任务被判成同一层）。而编排器派发同一层时是**同时**起子进程的，
# 实测三路并行的 startedAt 只差 3.5 毫秒。
#
# 所以「启动时刻接近 ⇒ 同一层」是更贴合实际执行模型的判据。
# 50ms 是给子进程冷启动留的余量（杀软扫描、cmd.exe 拉起）。
LAYER_START_TOLERANCE_MS = 50.0

# 利用率时间线的默认桶数。太多看不清趋势，太少看不出毛刺。
DEFAULT_BUCKETS = 60


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _intervals(tasks: list[dict]) -> list[tuple[float, float, dict]]:
    """把任务转成相对起点的毫秒区间，丢掉「没真正跑过」的。

    为什么要丢：编排层会为每个 step **预先落占位任务**，它们有 created_at
    但没有 started_at。把它们算进来会凭空多出一段「并行」，指标就废了。
    """
    parsed: list[tuple[datetime, datetime, dict]] = []
    for t in tasks:
        start = _parse(t.get("startedAt") or t.get("started_at"))
        if start is None:
            continue                      # 从未派活（占位/被跳过）
        end = _parse(t.get("finishedAt") or t.get("finished_at"))
        if end is None:
            dur = t.get("durationMs")
            if isinstance(dur, (int, float)) and dur > 0:
                from datetime import timedelta
                end = start + timedelta(milliseconds=float(dur))
            else:
                continue                  # 还在跑、又没有时长 → 无法计入
        if end < start:
            end = start
        parsed.append((start, end, t))

    if not parsed:
        return []
    base = min(p[0] for p in parsed)
    return [
        ((s - base).total_seconds() * 1000.0,
         (e - base).total_seconds() * 1000.0, t)
        for s, e, t in parsed
    ]


def _sweep(spans: list[tuple[float, float, dict]]) -> dict[str, float]:
    """一次扫描算出峰值并发与「有活干」的总时长。

    用 ±1 事件排序，而不是逐个时间点去数 —— 后者在任务多时会退化成 O(n²)。
    """
    events: list[tuple[float, int]] = []
    for start, end, _t in spans:
        events.append((start, 1))
        events.append((end, -1))
    # 同一时刻先处理结束再处理开始：一个任务在 100ms 结束、另一个在 100ms
    # 开始，那不是重叠，不该算成 2 个并发。
    events.sort(key=lambda e: (e[0], e[1]))

    concurrent = 0
    peak = 0
    busy = 0.0
    last_t = events[0][0] if events else 0.0
    for t, delta in events:
        if concurrent > 0:
            busy += t - last_t
        last_t = t
        concurrent += delta
        peak = max(peak, concurrent)
    return {"peak": float(peak), "busyMs": busy}


def _infer_layers(spans: list[tuple[float, float, dict]]) -> list[list[tuple[float, float, dict]]]:
    """切层：同时派发的算一层。

    两条判据，**满足任一条就开新层**：

    1. **启动时刻相差超过容差** —— 编排器派发同一层时是一起起子进程的，
       实测三路并行只差 3.5 毫秒；下一层要等上一层全部结束才起，间隔通常是秒级。
       两者差着好几个数量级。
    2. **启动时刻不早于当前层的最晚结束时刻** —— 也就是「上一批已经全部跑完，
       这才开始」。这是严格分层的**定义**，比阈值更硬。

    为什么必须有第 2 条：只有第 1 条时，**整层跑得比容差还快**就会被并进下一层。
    实测踩到 —— 演示下游（mock）整层几毫秒就跑完，一个本该 2 层的 plan 被判成
    1 层，「理论最短」跟着算错。这类下游恰恰是最常见的（本地 echo、短脚本）。

    第 2 条不会破坏「同层但启动有先后」：那种情况下层里总还有任务在跑，
    `cur_end` 是**运行中的最大结束时刻**，后启动的那个通常早于它。
    """
    ordered = sorted(spans, key=lambda s: (s[0], s[1]))
    layers: list[list[tuple[float, float, dict]]] = []
    cur: list[tuple[float, float, dict]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for span in ordered:
        starts_new = bool(cur) and cur_start is not None and cur_end is not None and (
            span[0] - cur_start > LAYER_START_TOLERANCE_MS   # 判据 1：离层首够远
            or span[0] >= cur_end                            # 判据 2：上一层已跑完
        )
        if starts_new:
            layers.append(cur)
            cur = []
            cur_start = None
            cur_end = None
        cur.append(span)
        if cur_start is None:
            cur_start = span[0]
        cur_end = span[1] if cur_end is None else max(cur_end, span[1])
    if cur:
        layers.append(cur)
    return layers


def _buckets(spans: list[tuple[float, float, dict]], wall: float, count: int) -> list[dict]:
    """利用率时间线：每个时间桶里同时有几个 agent 在跑。"""
    if wall <= 0 or count <= 0:
        return []
    width = wall / count
    out: list[dict] = []
    for i in range(count):
        lo = i * width
        hi = lo + width
        # 桶内「至少被覆盖到」的任务数。用中点判定而不是求交集长度 ——
        # 目的是看趋势，不是做积分；中点判定足够且不会把短任务放大成整桶。
        mid = (lo + hi) / 2.0
        active = sum(1 for s, e, _t in spans if s <= mid < e)
        out.append({"at": round(lo, 1), "active": active})
    return out


def analyze(
    tasks: list[dict],
    *,
    buckets: int = DEFAULT_BUCKETS,
) -> dict[str, Any]:
    """算一条 trace 的并行度指标。`tasks` 用 trace 接口里那一份即可。"""
    spans = _intervals(tasks)
    if not spans:
        return {
            "layerSource": "observed",
            "wallMs": 0.0, "busyMs": 0.0, "idleMs": 0.0, "serialMs": 0.0,
            "avgParallelism": 0.0, "peakParallelism": 0, "idleRatio": 0.0,
            "theoreticalMs": 0.0, "overheadMs": 0.0, "overheadRatio": 0.0,
            "tasks": 0, "layers": [], "buckets": [],
        }

    wall = max(e for _s, e, _t in spans) - min(s for s, _e, _t in spans)
    serial = sum(e - s for s, e, _t in spans)
    sweep = _sweep(spans)
    layers = _infer_layers(spans)

    layer_rows = []
    theoretical = 0.0
    for i, layer in enumerate(layers):
        lo = min(s for s, _e, _t in layer)
        hi = max(e for _s, e, _t in layer)
        lw = hi - lo
        durs = [e - s for s, e, _t in layer]
        slowest = max(durs)
        total = sum(durs)
        theoretical += slowest
        layer_rows.append({
            "index": i,
            "tasks": len(layer),
            "wallMs": round(lw, 1),
            "slowestMs": round(slowest, 1),
            "sumMs": round(total, 1),
            # 该层的并行效率：最慢那个占该层墙钟的比例。
            # 1.0 = 所有任务同时结束（理想）；越低说明越参差。
            "efficiency": round(slowest / lw, 3) if lw > 0 else 1.0,
            # 该层实际拿到几倍加速：总时长 / 墙钟
            "speedup": round(total / lw, 2) if lw > 0 else 1.0,
            "steps": [
                {
                    "stepId": t.get("stepId") or t.get("step_id"),
                    "agent": t.get("agent"),
                    "state": t.get("state"),
                    "durationMs": round(e - s, 1),
                    "startedAt": t.get("startedAt") or t.get("started_at"),
                }
                for s, e, t in sorted(layer, key=lambda x: x[0])
            ],
        })

    wall = max(wall, 0.0)
    return {
        # 口径声明。调用方据此判断这些数字该怎么解读 —— 不要静默给一个
        # 看起来像「关键路径」其实是别的东西的数。
        "layerSource": "observed",
        "tasks": len(spans),
        "wallMs": round(wall, 1),
        "busyMs": round(sweep["busyMs"], 1),
        "idleMs": round(max(wall - sweep["busyMs"], 0.0), 1),
        "idleRatio": round(max(wall - sweep["busyMs"], 0.0) / wall, 3) if wall else 0.0,
        "serialMs": round(serial, 1),
        # 平均并行度 = 串行总时长 / 墙钟。**最有用的一眼指标。**
        "avgParallelism": round(serial / wall, 2) if wall else 0.0,
        "peakParallelism": int(sweep["peak"]),
        "theoreticalMs": round(theoretical, 1),
        "overheadMs": round(max(wall - theoretical, 0.0), 1),
        "overheadRatio": round(max(wall - theoretical, 0.0) / wall, 3) if wall else 0.0,
        "layers": layer_rows,
        "buckets": _buckets(spans, wall, buckets),
    }
