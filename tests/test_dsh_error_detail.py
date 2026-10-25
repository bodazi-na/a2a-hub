#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DshCLI 的失败原因不能被压成一个枚举值。

2026-10-25 实际发生的事：hub 的 dsh-cli 节点连续失败，任务记录里只有一句

    turn_end reason=error

而 DSH 当时**已经**把完整原因发过来了：

    {"type":"status","phase":"turn_end","turn":1,
     "reason":{"kind":"error","error":{
        "code":"MISSING_CREDENTIAL",
        "message":"llm-deepseek: no API key for provider route ..."}}}

两处把它丢掉的：

  1. `parse_line` 只取 `reason["kind"]`，`reason["error"]` 整个不看；
  2. `finalize` 一定会填 error，于是基类 `cli.py:501` 的「error 为空才用
     stderr 兜底」永远不触发 —— 已经采集到的 stderr 也被丢弃。

结果就是「有全部证据，却报出信息量最小的那个字符串」。
下面用**当时抓到的原始事件序列**钉住行为，防止以后再退回去。

跑法：python tests/test_dsh_error_detail.py
"""

from __future__ import annotations

import os
import sys

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.adapters.cli import DshCLI                              # noqa: E402

# 真实抓取的事件（2026-10-25，dsh --profile headless --json，凭据缺失场景）
MISSING_CREDENTIAL_STREAM = [
    {"type": "session", "sessionId": "session-bc01bb74-7e5e-494b-b299-3bd079e15537",
     "cwd": "D:\\A2A_Engineering"},
    {"type": "status", "phase": "turn_start", "turn": 1},
    {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
    {"type": "status", "phase": "step_end", "turn": 1, "step": 1},
    {"type": "status", "phase": "turn_end", "turn": 1,
     "reason": {"kind": "error", "error": {
         "message": 'llm-deepseek: no API key for provider route "deepseek-official"; '
                    "store DEEPSEEK_API_KEY through the credentials service",
         "code": "MISSING_CREDENTIAL"}}},
    {"type": "final", "text": ""},
]

MISSING_CREDENTIAL_STDERR = (
    "dsh: MISSING_CREDENTIAL: llm-deepseek: no API key for provider route "
    '"deepseek-official"\n'
)


def _feed(stream: list[dict], *, stderr: str = "") -> tuple[dict, list]:
    """按适配器真实路径逐行解析，返回 (state, events)。"""
    adapter = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300)
    state: dict = {}
    events: list = []
    for obj in stream:
        events.extend(adapter.parse_line(obj, state))
    if stderr:
        state["stderr_tail"] = stderr
    return state, events


def test_error_code_and_message_survive() -> bool:
    """核心断言：code 与 message 必须出现在 error 里。"""
    state, _ = _feed(MISSING_CREDENTIAL_STREAM)
    out = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300).finalize(state, 1)

    print(f"  error = {out.error!r}")
    checks = [
        (out.ok is False, "ok 必须是 False"),
        (out.error is not None, "error 不能为空"),
        ("MISSING_CREDENTIAL" in (out.error or ""), "error 要含 code"),
        ("no API key" in (out.error or ""), "error 要含 message 正文"),
        (out.metadata.get("turnEndErrorCode") == "MISSING_CREDENTIAL", "metadata 要留 code"),
        (out.metadata.get("turnEndReason") == "error", "metadata 要留 reason"),
    ]
    for ok, why in checks:
        if not ok:
            print(f"  !! {why}")
    return all(ok for ok, _ in checks)


def test_stderr_not_duplicated() -> bool:
    """stderr 与 message 是同一句话时，不要重复贴两遍。"""
    state, _ = _feed(MISSING_CREDENTIAL_STREAM, stderr=MISSING_CREDENTIAL_STDERR)
    out = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300).finalize(state, 1)
    err = out.error or ""
    print(f"  error = {err!r}")
    if "no API key" not in err:
        print("  !! 丢了 message")
        return False
    # message 里已含该句 → stderr 不该再追加一份
    if err.count("no API key for provider route") != 1:
        print("  !! stderr 与 message 重复了")
        return False
    return True


def test_stderr_used_when_no_reason_detail() -> bool:
    """reason 里没有 error 时，stderr 必须补上（不能又是空话一句）。"""
    stream = [
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "error"}},
    ]
    state, _ = _feed(stream, stderr="dsh: 配置文件解析失败: 第 3 行\n")
    out = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300).finalize(state, 1)
    print(f"  error = {out.error!r}")
    if "配置文件解析失败" not in (out.error or ""):
        print("  !! 没把 stderr 带进 error")
        return False
    return True


def test_completed_is_still_ok() -> bool:
    """正常完成不能被新逻辑误判成失败。"""
    stream = [
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "text", "text": "ok"},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1},
        {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
        {"type": "final", "text": "ok"},
    ]
    state, events = _feed(stream)
    out = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300).finalize(state, 0)
    print(f"  ok={out.ok} text={out.text!r} error={out.error!r}")
    checks = [
        (out.ok is True, "ok 应为 True"),
        (out.text == "ok", "final 文本要取出"),
        (out.error is None, "成功时 error 应为 None"),
        (any(getattr(e, "kind", "") == "text" for e in events), "text 事件要透出"),
    ]
    for ok, why in checks:
        if not ok:
            print(f"  !! {why}")
    return all(ok for ok, _ in checks)


def test_nonzero_exit_without_reason() -> bool:
    """没收到 turn_end 事件时，仍要报退出码。"""
    state, _ = _feed([{"type": "session", "sessionId": "s-1"}], stderr="boom\n")
    out = DshCLI("dsh-cli", command=["dsh.cmd"], timeout=300).finalize(state, 1)
    print(f"  error = {out.error!r}")
    if "退出码 1" not in (out.error or ""):
        print("  !! 缺退出码")
        return False
    return True


def main() -> int:
    print("=" * 60)
    print("DshCLI 失败原因透出（回归：2026-10-25 凭据缺失事件）")
    print("=" * 60)
    cases = [
        ("原因码与正文透出", test_error_code_and_message_survive),
        ("stderr 不重复", test_stderr_not_duplicated),
        ("无 error 时用 stderr 兜底", test_stderr_used_when_no_reason_detail),
        ("completed 仍判成功", test_completed_is_still_ok),
        ("无 turn_end 时报退出码", test_nonzero_exit_without_reason),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, fn()))
        except Exception as exc:                                # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
