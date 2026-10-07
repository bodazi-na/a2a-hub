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

# 层边界的时钟抖动容差（毫秒）。
#
# 判据是「启动时刻不早于当前层的最晚结束时刻」。严格分层下这两者**恰好相等**
# （上一层结束的同一刻，下一层开始），所以留一点点余量吸收时间戳抖动。
#
# **必须很小**：它会被拿去和任务的**时长**比较。设成 5ms 时，一个只跑 5ms 的
# 任务会被自己层里的下一个任务判成新层（`cur_end - 5` 正好等于它的启动时刻）——
# 实测把「整层 5ms 的 2 层 plan」判成了 4 层。
#
# 1ms 够用：`utcnow()` 是**微秒**精度（`isoformat(timespec="microseconds")`），
# 抖动本来就在亚毫秒量级，而编排器让下一层等上一层全部结束才起，
# 正常情况下 `start` 只会**晚于** `end`，根本用不到这个容差。
LAYER_BOUNDARY_EPSILON_MS = 1.0

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
    """切层：**上一批全部跑完之后才开始的那一批，算新的一层。**

    判据只有一条：`start >= 当前层的最晚结束时刻 - 抖动容差`。

    为什么是这条而不是「启动时刻相差多少」：

    - 这是严格分层的**定义** —— 编排器让第 N+1 层等第 N 层全部结束才起。
      用定义判定，不依赖任何经验阈值。
    - **「启动时刻相差 X 毫秒」是错的方向**。实测踩过两次：
      先是拿 50ms 当阈值，结果**整层比 50ms 还快**时判据完全失效（演示下游
      mock 整层几毫秒跑完，2 层的 plan 被判成 1 层）；把阈值调小之后又发现
      **HTTP 适配器派发同一层的三个任务间隔约 45ms**，第三路离层首已经 89ms，
      于是真重叠的三路并行被拆成 2+1 层。
    - 换个角度想就清楚了：**两个时间区间重叠的任务，本来就是同一层**。
      「启动时刻差多少」根本不是这件事的判据。

    唯一的代价：如果同一层里的任务因为并发上限被**串行化**（前一个跑完才开始
    下一个），它们会被判成不同层。但那恰恰是事实 —— 那一刻它们确实没在并行，
    指标就该如实反映。
    """
    ordered = sorted(spans, key=lambda s: (s[0], s[1]))
    layers: list[list[tuple[float, float, dict]]] = []
    cur: list[tuple[float, float, dict]] = []
    cur_end: float | None = None

    for span in ordered:
        if cur and cur_end is not None and span[0] >= cur_end - LAYER_BOUNDARY_EPSILON_MS:
            layers.append(cur)
            cur = []
            cur_end = None
        cur.append(span)
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
