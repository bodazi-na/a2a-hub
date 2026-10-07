#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLI 事件流的解析：三条「一行坏数据就能毁掉整轮」的问题。

三者其实是同一个设计缺陷的三个侧面 —— 都是「逐行走 `readline` + 逐行
`json.loads`」造成的，所以一起验证：

  P2-5  `state["noise"]` 无限 append
        CLI 刷屏时内存无界增长（最终却只用它的长度）。
  P2-17 `STREAM_LIMIT` 是 StreamReader 的**硬上限**而不是截断
        单行超限 → `readline()` 抛 `LimitOverrunError` → 整个 call 被判失败
        并 kill 进程树。一条超大工具输出就能毁掉一轮。
  P2-24 逐行 `json.loads`，pretty-printed JSON 无法跨行重组
        以 `{` 开头但单行解析不出的行**全部落进 noise 被静默丢弃** ——
        任务显示成功，但下游真正说的东西被丢掉了。

用假流（`read(n)` 返回分块数据）驱动，不起任何进程，也不需要真实 CLI。
跑法：python tests/test_stream_parsing.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import a2a_hub.adapters.cli as cli_mod                                     # noqa: E402
from a2a_hub.adapters.cli import (                                         # noqa: E402
    MAX_NOISE_LINES,
    CLIAdapter,
    CLIOutcome,
    Event,
)


class FakeStream:
    """最小的流替身：`read(n)` 按固定块大小吐字节。

    块大小故意设得很小，好让「一行跨多次 read」这条路径被真正走到 ——
    真实场景里管道就是这么分块的。
    """

    def __init__(self, data: bytes, chunk: int = 7):
        self._data = data
        self._pos = 0
        self._chunk = chunk

    async def read(self, n: int = -1) -> bytes:
        size = self._chunk if n is None or n < 0 else min(n, self._chunk)
        out = self._data[self._pos:self._pos + size]
        self._pos += len(out)
        return out


class LineCLI(CLIAdapter):
    """把每个解析出来的 JSON 对象变成一个事件。"""

    kind = "cli"

    def build_argv(self, session_id: str | None) -> list[str]:
        return []

    def parse_line(self, obj, state):
        return [Event(kind="text", text=str(obj.get("t", "")))]

    def finalize(self, state, returncode) -> CLIOutcome:
        return CLIOutcome(ok=True)


def make(data: str | bytes, chunk: int = 7):
    ad = LineCLI("t", command=["x"])
    state = ad.new_state()
    events: list[Event] = []
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return ad, state, events, raw


async def drain(ad, state, events, raw, chunk=7):
    await ad._read_lines(FakeStream(raw, chunk), state, events)


def report(checks: dict[str, bool]) -> bool:
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


def jline(**kw) -> str:
    return json.dumps(kw) + "\n"


# ---------------------------------------------------------------- 正常路径


async def test_normal_lines() -> bool:
    print("\n[基线] 正常单行 JSON 事件")
    ad, state, events, raw = make(jline(t="a") + jline(t="b") + jline(t="c"))
    await drain(ad, state, events, raw)
    print(f"  事件: {[e.text for e in events]}")
    return report({
        "三个事件都被解析出来": [e.text for e in events] == ["a", "b", "c"],
        "没有噪声": state["noise_total"] == 0,
        "没有未解析行": state["unparsed"] == 0,
        "没有超长行": state["oversized"] == 0,
    })


async def test_line_spanning_read_chunks() -> bool:
    print("\n[基线] 一行跨多次 read（管道就是这样分块的）")
    ad, state, events, raw = make(jline(t="hello-world"), chunk=3)
    await drain(ad, state, events, raw, chunk=3)
    return report({
        "跨块的一行被正确拼回来": [e.text for e in events] == ["hello-world"],
    })


async def test_last_line_without_newline() -> bool:
    print("\n[基线] 最后一行没有换行符（EOF 时残留在缓冲里）")
    ad, state, events, raw = make(jline(t="first") + '{"t": "last"}')
    await drain(ad, state, events, raw)
    print(f"  事件: {[e.text for e in events]}")
    return report({
        "末尾无换行的行也被处理": [e.text for e in events] == ["first", "last"],
    })


# ---------------------------------------------------------------- P2-5


async def test_noise_is_bounded() -> bool:
    print("\n[P2-5] 噪声行必须有上限，不能无限增长")
    n = MAX_NOISE_LINES * 5
    ad, state, events, raw = make("noise line\n" * n)
    await drain(ad, state, events, raw, chunk=4096)
    print(f"  喂了 {n} 行噪声 → 保留 {len(state['noise'])} 行，计数 {state['noise_total']}")
    return report({
        "总数被如实计数": state["noise_total"] == n,
        f"列表被限制在 {MAX_NOISE_LINES} 行以内（P2-5 核心）":
            len(state["noise"]) <= MAX_NOISE_LINES,
    })


# ---------------------------------------------------------------- P2-17


async def test_oversized_line_is_truncated_not_fatal() -> bool:
    print("\n[P2-17] 单行超限要截断，不能毁掉整轮")
    ad, state, events, raw = make("")
    limit = 4096
    # 一行远超上限，后面再跟两行正常的
    big = "x" * (limit * 4)
    payload = big + "\n" + jline(t="after1") + jline(t="after2")
    ad, state, events, raw = make(payload)

    old = cli_mod.STREAM_LIMIT
    cli_mod.STREAM_LIMIT = limit
    try:
        await drain(ad, state, events, raw, chunk=1024)
    except Exception as exc:                                   # noqa: BLE001
        print(f"  !! 抛异常了: {type(exc).__name__}: {exc}")
        return report({"超长行没有让整轮失败（P2-17 核心）": False})
    finally:
        cli_mod.STREAM_LIMIT = old

    texts = [e.text for e in events]
    print(f"  超长行计数={state['oversized']}  后续事件={texts}")
    return report({
        "超长行被截断并计数（P2-17 核心）": state["oversized"] == 1,
        "后续行照常解析 —— 一轮没有被毁掉": texts == ["after1", "after2"],
    })


async def test_oversized_tail_not_treated_as_new_line() -> bool:
    print("\n[P2-17] 超长行的尾巴不许被当成新行")
    limit = 2048
    # 超长行 = 前缀 + 尾巴；尾巴里塞一个看起来像 JSON 的东西，
    # 若实现有误，它会被当成独立一行解析出来
    big = "y" * (limit * 3) + '{"t": "SHOULD-NOT-APPEAR"}'
    payload = big + "\n" + jline(t="real")
    ad, state, events, raw = make(payload)

    old = cli_mod.STREAM_LIMIT
    cli_mod.STREAM_LIMIT = limit
    try:
        await drain(ad, state, events, raw, chunk=512)
    finally:
        cli_mod.STREAM_LIMIT = old

    texts = [e.text for e in events]
    print(f"  事件: {texts}")
    return report({
        "尾巴没有被解析成事件": "SHOULD-NOT-APPEAR" not in texts,
        "只有真正的那一行被解析": texts == ["real"],
    })


# ---------------------------------------------------------------- P2-24


async def test_multiline_json_is_reassembled() -> bool:
    print("\n[P2-24] pretty-printed 的多行 JSON 要能重组")
    pretty = json.dumps({"t": "joined", "extra": {"a": 1, "b": [1, 2, 3]}},
                        indent=2, ensure_ascii=False)
    payload = pretty + "\n" + jline(t="single")
    ad, state, events, raw = make(payload)
    await drain(ad, state, events, raw)
    texts = [e.text for e in events]
    print(f"  事件: {texts}  未解析计数={state['unparsed']}")
    return report({
        "多行 JSON 被拼回来并解析（P2-24 核心）": texts == ["joined", "single"],
        "没有被记成未解析": state["unparsed"] == 0,
        "也没有混进噪声": state["noise_total"] == 0,
    })


async def test_unparseable_json_is_counted_not_silently_dropped() -> bool:
    print("\n[P2-24] 拼不出来的 JSON 要**计数**，不能静默丢弃")
    # 永远闭合不了的 `{` 开头内容
    payload = "{\n  \"broken\": true,\n  \"never\": closed\n"
    ad, state, events, raw = make(payload)
    await drain(ad, state, events, raw)
    print(f"  unparsed={state['unparsed']}  noise_total={state['noise_total']}")
    return report({
        "被单独计数（P2-24 核心）": state["unparsed"] > 0,
        "没有混进普通噪声里无声无息": state["noise_total"] == 0,
    })


async def test_stream_stats_are_surfaced() -> bool:
    print("\n[P2-5/P2-24] 这些计数必须进 metadata —— 静默丢数据最隐蔽")
    ad = LineCLI("t", command=["x"])
    stats = ad._stream_stats({"noise_total": 3, "oversized": 1, "unparsed": 2})
    print(f"  {stats}")
    return report({
        "噪声计数上报": stats["noiseLines"] == 3,
        "超长行计数上报": stats["oversizedLines"] == 1,
        "未解析计数上报": stats["unparsedJsonLines"] == 2,
    })


async def main() -> int:
    cases = [
        ("基线 正常单行", test_normal_lines),
        ("基线 跨块拼行", test_line_spanning_read_chunks),
        ("基线 末尾无换行", test_last_line_without_newline),
        ("P2-5  噪声有上限", test_noise_is_bounded),
        ("P2-17 超长行截断不致命", test_oversized_line_is_truncated_not_fatal),
        ("P2-17 超长行尾巴不当新行", test_oversized_tail_not_treated_as_new_line),
        ("P2-24 多行 JSON 重组", test_multiline_json_is_reassembled),
        ("P2-24 拼不出来的要计数", test_unparseable_json_is_counted_not_silently_dropped),
        ("计数上报", test_stream_stats_are_surfaced),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, await fn()))
        except Exception as exc:                                # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
