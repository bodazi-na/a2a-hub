#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并行度分析的回归测试。

纯函数、零依赖 —— 这是它单独成模块的理由：可以在单元级穷举边界，
不必起 hub、不必造假下游。

锁住的核心是**分层推断**：它错一次，理论最短和编排开销就全错，
而那两个数字正是「还能不能更快」的答案。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parallelism import (                                    # noqa: E402
    LAYER_START_TOLERANCE_MS,
    analyze,
)

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def task(step: str, start_ms: float, dur_ms: float, agent: str = "a",
         state: str = "completed") -> dict:
    s = BASE + timedelta(milliseconds=start_ms)
    return {
        "stepId": step,
        "agent": agent,
        "state": state,
        "startedAt": s.isoformat(),
        "finishedAt": (s + timedelta(milliseconds=dur_ms)).isoformat(),
    }


def approx(a: float, b: float, tol: float = 1.0) -> bool:
    return abs(a - b) <= tol


def check(name: str, cond: bool, detail: str = "") -> tuple[str, bool]:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return name, cond


# ------------------------------------------------------------------ 用例


def test_empty() -> list:
    print("\n[边界] 空输入不能炸")
    r = analyze([])
    return [
        check("任务数为 0", r["tasks"] == 0),
        check("墙钟为 0", r["wallMs"] == 0),
        check("平均并行度为 0（不是除零）", r["avgParallelism"] == 0.0),
        check("层为空", r["layers"] == []),
        check("桶为空", r["buckets"] == []),
    ]


def test_single() -> list:
    print("\n[边界] 单任务")
    r = analyze([task("x", 0, 1000)])
    return [
        check("墙钟 = 时长", approx(r["wallMs"], 1000)),
        check("平均并行度 = 1.0", approx(r["avgParallelism"], 1.0, 0.01)),
        check("峰值 = 1", r["peakParallelism"] == 1),
        check("1 层", len(r["layers"]) == 1),
        check("理论最短 = 该任务时长", approx(r["theoreticalMs"], 1000)),
        check("开销 = 0", approx(r["overheadMs"], 0)),
        check("空闲率 = 0", approx(r["idleRatio"], 0.0, 0.001)),
    ]


def test_fully_parallel() -> list:
    print("\n[全并行] 4 个同时跑 1 秒 —— 平均并行度应当是 4")
    r = analyze([task(c, 0, 1000) for c in "abcd"])
    return [
        check("平均并行度 = 4.0", approx(r["avgParallelism"], 4.0, 0.01),
              f"实际 {r['avgParallelism']}"),
        check("峰值 = 4", r["peakParallelism"] == 4),
        check("只有 1 层", len(r["layers"]) == 1),
        check("理论最短 = 1000（不是 4000）", approx(r["theoreticalMs"], 1000),
              f"实际 {r['theoreticalMs']}"),
        check("该层加速 = 4×", approx(r["layers"][0]["speedup"], 4.0, 0.05)),
    ]


def test_fully_serial() -> list:
    print("\n[全串行] 3 个首尾相接 —— 分层必须分出 3 层")
    r = analyze([task("a", 0, 1000), task("b", 1000, 1000), task("c", 2000, 1000)])
    return [
        check("平均并行度 = 1.0", approx(r["avgParallelism"], 1.0, 0.01),
              f"实际 {r['avgParallelism']}"),
        check("峰值 = 1", r["peakParallelism"] == 1),
        # 这条是关键：串行的任务「上一层结束的同一刻」开始，
        # 早期版本用 `start > 上一层结束` 判定，边界取等会把它们并成一层
        check("分出 3 层（边界取等不能并层）", len(r["layers"]) == 3,
              f"实际 {len(r['layers'])} 层"),
        check("理论最短 = 3000", approx(r["theoreticalMs"], 3000)),
        check("开销 = 0", approx(r["overheadMs"], 0)),
    ]


def test_layered() -> list:
    print("\n[分层] 2 路并行 → 1 个串行 → 3 路并行")
    r = analyze([
        task("a", 0, 1000), task("b", 0, 1200),
        task("c", 1200, 800),
        task("d", 2000, 500), task("e", 2000, 600), task("f", 2000, 400),
    ])
    return [
        check("分出 3 层", len(r["layers"]) == 3, f"实际 {len(r['layers'])}"),
        check("层0 有 2 个任务", r["layers"][0]["tasks"] == 2),
        check("层1 有 1 个任务", r["layers"][1]["tasks"] == 1),
        check("层2 有 3 个任务", r["layers"][2]["tasks"] == 3),
        check("峰值 = 3", r["peakParallelism"] == 3),
        # 1200 + 800 + 600 = 2600
        check("理论最短 = 2600", approx(r["theoreticalMs"], 2600),
              f"实际 {r['theoreticalMs']}"),
        # 层2 墙钟 = 最慢那个 600ms，总时长 500+600+400 = 1500ms
        check("层2 的加速 = 2.5×（1500/600）",
              approx(r["layers"][2]["speedup"], 2.5, 0.05),
              f"实际 {r['layers'][2]['speedup']}"),
        check("层2 的效率 = 1.0（最慢那个就占满整层墙钟）",
              approx(r["layers"][2]["efficiency"], 1.0, 0.01)),
    ]


def test_same_layer_jitter() -> list:
    """同层任务因起子进程有先后，启动时刻差几十毫秒 —— 不能被拆成两层。"""
    print("\n[容差] 同层启动差 30ms（< 容差）应仍算一层")
    r = analyze([task("a", 0, 1000), task("b", 30, 1000)])
    return [
        check(f"仍是一层（容差 {LAYER_START_TOLERANCE_MS:.0f}ms）",
              len(r["layers"]) == 1, f"实际 {len(r['layers'])} 层"),
        check("峰值 = 2（确实重叠）", r["peakParallelism"] == 2),
    ]


def test_gap_is_idle() -> list:
    print("\n[空闲] 中间空 500ms —— 空闲率应当是 20%")
    r = analyze([task("a", 0, 1000), task("b", 1500, 1000)])
    return [
        check("空闲率 = 0.2", approx(r["idleRatio"], 0.2, 0.01),
              f"实际 {r['idleRatio']}"),
        check("空闲时长 = 500ms", approx(r["idleMs"], 500)),
        check("开销 = 500ms（空档全算开销）", approx(r["overheadMs"], 500)),
    ]


def test_placeholders_excluded() -> list:
    """没派活的占位任务不能计入 —— 算进去会凭空多出一段并行。"""
    print("\n[排除] 未开始的占位 step 不应计入")
    r = analyze([task("a", 0, 1000), {"stepId": "p", "state": "submitted"}])
    return [
        check("只算了 1 个任务", r["tasks"] == 1, f"实际 {r['tasks']}"),
        check("墙钟 = 1000（占位没撑大它）", approx(r["wallMs"], 1000)),
        check("平均并行度 = 1.0", approx(r["avgParallelism"], 1.0, 0.01)),
    ]


def test_buckets() -> list:
    print("\n[时间线] 桶数正确、能反映并发变化")
    r = analyze([task("a", 0, 1000), task("b", 0, 1000), task("c", 1000, 1000)],
                buckets=10)
    active = [b["active"] for b in r["buckets"]]
    return [
        check("桶数 = 10", len(r["buckets"]) == 10, f"实际 {len(r['buckets'])}"),
        check("前半段并发 2、后半段并发 1",
              set(active[:4]) == {2} and set(active[6:]) == {1},
              f"实际 {active}"),
    ]


def test_real_drill_shape() -> list:
    """照四节点编排实测的形状造一条：3 路并行 21.4s → 汇总 39.7s。"""
    print("\n[实测形状] 3 路并行 + 1 个汇总（按 2026-10-06 演练的真实数字）")
    r = analyze([
        task("a", 0, 14659, "claude-cli"),
        task("b", 0, 21379, "codex-cli"),
        task("c", 0, 6841, "dsh-cli"),
        task("merge", 21379, 39662, "qoder-cli"),
    ])
    return [
        check("分出 2 层", len(r["layers"]) == 2, f"实际 {len(r['layers'])}"),
        check("层0 = 3 路并行", r["layers"][0]["tasks"] == 3),
        check("层0 的最慢 = 21379ms（取最慢那个，不是求和）",
              approx(r["layers"][0]["slowestMs"], 21379, 2)),
        check("理论最短 ≈ 61041ms", approx(r["theoreticalMs"], 61041, 5),
              f"实际 {r['theoreticalMs']:.0f}"),
        check("墙钟 ≈ 61041ms", approx(r["wallMs"], 61041, 5),
              f"实际 {r['wallMs']:.0f}"),
        check("编排开销 ≈ 0（实测就是这样）", approx(r["overheadRatio"], 0.0, 0.01)),
        check("峰值 = 3", r["peakParallelism"] == 3),
    ]


def test_sub_threshold_serial_tasks_merge() -> list:
    """**已知局限**：间隔小于容差的首尾相接任务会被并进同一层。

    这不是「期望行为」，而是把当前取舍钉住，免得以后有人当 bug 改掉又没改对。

    为什么不做更聪明的推断：**仅凭时间戳区分不了**这两种情况 ——
      (a) 同层任务因起子进程有先后（观测：启动差几十毫秒）
      (b) 不同层但前一层极短（观测：启动差几十毫秒）
    两种情况的观测量**完全一样**。既然区分不了，就选一个并在文档里写清楚，
    而不是假装能算准。

    后果：这类 trace 的「理论最短」偏小、「编排开销」也随之偏小。
    对普通场景（层间隔是秒级）没有影响。
    """
    print("\n[已知局限] 间隔 40ms 的串行任务会被并进同一层")
    gap = LAYER_START_TOLERANCE_MS - 10          # 40ms，小于容差
    r = analyze([task("a", 0, 1000), task("b", gap, 1000)])
    return [
        check(f"间隔 {gap:.0f}ms < 容差 {LAYER_START_TOLERANCE_MS:.0f}ms → 判成 1 层",
              len(r["layers"]) == 1, f"实际 {len(r['layers'])} 层"),
        # 真实是两层串行（理论最短应 2000），被并层后只算 1000 —— 偏小
        check("理论最短因此偏小（1000 而非 2000）",
              approx(r["theoreticalMs"], 1000, 1),
              f"实际 {r['theoreticalMs']:.0f}"),
        check("峰值仍是 2（区间确实重叠，这部分没算错）",
              r["peakParallelism"] == 2),
    ]


def test_over_threshold_stays_separate() -> list:
    """对照：间隔超过容差就必须分层，否则这条启发式就没意义了。"""
    print("\n[对照] 间隔超过容差 → 正确分层")
    gap = LAYER_START_TOLERANCE_MS + 20          # 70ms，大于容差
    r = analyze([task("a", 0, 1000), task("b", gap, 1000)])
    return [
        check(f"间隔 {gap:.0f}ms > 容差 → 分出 2 层",
              len(r["layers"]) == 2, f"实际 {len(r['layers'])} 层"),
        check("理论最短 = 2000（两层各自最慢之和）",
              approx(r["theoreticalMs"], 2000, 1),
              f"实际 {r['theoreticalMs']:.0f}"),
    ]


def test_fast_layers_still_split() -> list:
    """**整层比容差还快时，仍必须正确分层。**

    实测踩到的真实场景：演示下游（mock）整层几毫秒就跑完，于是「启动时刻相差
    50ms」这条判据完全失效 —— 一个本该 2 层的 plan 被判成 1 层，「理论最短」
    跟着算错。而这恰恰是最常见的下游类型（本地 echo、短脚本）。

    修法是加一条更硬的判据：**启动时刻不早于当前层的最晚结束时刻** ⇒ 新层。
    那是严格分层的定义，比阈值可靠。
    """
    print("\n[快下游] 整层 5ms 跑完，仍要分出 2 层")
    r = analyze([task("a", 0, 5), task("b", 0, 5), task("c", 0, 5),
                 task("merge", 5, 3)])
    return [
        check("分出 2 层（阈值判据在此完全失效）",
              len(r["layers"]) == 2, f"实际 {len(r['layers'])} 层"),
        check("层0 = 3 路并行", r["layers"][0]["tasks"] == 3),
        check("层1 = 汇总", r["layers"][1]["tasks"] == 1),
        check("理论最短 = 5 + 3 = 8ms",
              approx(r["theoreticalMs"], 8, 0.5),
              f"实际 {r['theoreticalMs']:.1f}"),
        check("峰值 = 3", r["peakParallelism"] == 3),
    ]


CASES = [
    ("空输入", test_empty),
    ("单任务", test_single),
    ("全并行", test_fully_parallel),
    ("全串行", test_fully_serial),
    ("三层混合", test_layered),
    ("同层抖动容差", test_same_layer_jitter),
    ("空闲与开销", test_gap_is_idle),
    ("排除占位任务", test_placeholders_excluded),
    ("利用率时间线", test_buckets),
    ("实测形状", test_real_drill_shape),
    ("已知局限：亚阈值并层", test_sub_threshold_serial_tasks_merge),
    ("对照：超阈值正常分层", test_over_threshold_stays_separate),
    ("快下游仍正确分层", test_fast_layers_still_split),
]


def main() -> int:
    results: list[tuple[str, bool]] = []
    for name, fn in CASES:
        try:
            results.extend(fn())
        except Exception as exc:                    # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    passed = sum(1 for _n, ok in results if ok)
    for n, ok in results:
        if not ok:
            print(f"  FAIL  {n}")
    print(f"  {passed}/{len(results)} 项断言通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
