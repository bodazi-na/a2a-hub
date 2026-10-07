#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLI 适配器的 argv 构造与校验（纯逻辑，不起任何进程）。

覆盖：

  P1-8 经 cmd.exe 的 argv 必须过白名单
       本机四个 CLI 里三个是 `.cmd`，Windows 上必须经 `cmd.exe /c`，
       而 cmd 会**重新解释**命令行。实测（2026-10-06）：
         a&b      → a          （& 切断命令，a&calc 会真的执行 calc）
         a|b      → 管道       （把 b 当命令跑）
         a>b      → 重定向     （输出被吞）
         a^b      → ab         （^ 被当转义符）
         a%PATH%b → 展开       （**加引号也拦不住**）
       最后一条决定了「转义」这条路走不通（cmd 命令行上的 % 无法可靠转义），
       所以只能不让可疑值进入命令行 —— 拒绝，而不是静默拆错。

  P2-14 裸命令名要经 PATH 解析后再判断是不是 .cmd
       只按字符串判断会漏掉包裹，CreateProcess 报「找不到文件 / WinError 193」。

  另外验证 `_with_kill_problems` 会把终止失败如实拼进文案 ——
  静默漏杀是这个适配器最危险的失败模式（P1-7）。

跑法：python tests/test_subprocess_argv.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.adapters.cli import (                                        # noqa: E402
    SAFE_ARG_RE,
    CLIAdapter,
    CLIOutcome,
    UnsafeArgError,
    _with_kill_problems,
)


class FakeCLI(CLIAdapter):
    """只用来测 argv 构造，绝不真的起进程。"""

    kind = "cli"

    def build_argv(self, session_id: str | None) -> list[str]:
        argv = ["--json"]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    def parse_line(self, obj, state):
        return []

    def finalize(self, state, returncode) -> CLIOutcome:
        return CLIOutcome(ok=True)


def report(checks: dict[str, bool]) -> bool:
    for name, ok in checks.items():
        print(f"    {'✓' if ok else '✗'} {name}")
    return all(checks.values())


# ---------------------------------------------------------------- P1-8


def test_whitelist_blocks_metacharacters() -> bool:
    print("\n[P1-8] 白名单必须拦下会被 cmd.exe 重新解释的字符")
    ad = FakeCLI("t", command=[r"C:\fake\tool.cmd"])

    bad = ["a&b", "a|b", "a>b", "a<b", "a^b", "a%b%", "a b", 'a"b',
           "a;b", "a(b)", "a`b", "a\nb", "a$b"]
    blocked, escaped = [], []
    for value in bad:
        try:
            ad._argv("--resume", value)
            escaped.append(value)
        except UnsafeArgError:
            blocked.append(value)
        except Exception as exc:                       # noqa: BLE001
            escaped.append(f"{value}({type(exc).__name__})")

    print(f"  拦下 {len(blocked)}/{len(bad)}")
    if escaped:
        print(f"  漏网: {escaped}")
    return report({
        "全部危险值都被拒绝（P1-8 核心）": not escaped,
        "% 也被拒绝（它无法转义，只能拒绝）": "a%b%" in blocked,
    })


def test_whitelist_allows_real_ids() -> bool:
    print("\n[P1-8] 白名单不能误伤真实的下游标识符")
    ad = FakeCLI("t", command=[r"C:\fake\tool.cmd"])

    good = [
        "sess-abc123",
        "01a10f16-1234-5678-9abc-def012345678",      # codex thread_id 形态
        "d6dc9388-f023-443f-b702-2d154f1ac322",      # qoder model UUID
        "a.b_c:d@e/f+g-h",
        "abc123",
    ]
    rejected = []
    for value in good:
        try:
            ad._argv("--resume", value)
        except Exception as exc:                       # noqa: BLE001
            rejected.append(f"{value} → {exc}")

    for value in rejected:
        print(f"  误伤: {value}")
    return report({"真实标识符全部放行": not rejected})


def test_call_rejects_before_spawn() -> bool:
    print("\n[P1-8] call() 必须在起进程**之前**就拒绝")
    ad = FakeCLI("t", command=[r"C:\fake\tool.cmd"])

    async def run():
        # 注意：这个 command 根本不存在。若校验发生在 spawn 之后，
        # 拿到的会是「可执行文件不存在」而不是 UnsafeArgError。
        return await ad.call("hi", session_id="a&calc")

    result = asyncio.run(run())
    print(f"  error = {result.error}")
    return report({
        "返回失败而不是抛异常": result.ok is False,
        "失败原因指向参数而非找不到文件（说明没走到 spawn）":
            "拒绝执行" in (result.error or ""),
    })


# ---------------------------------------------------------------- P2-14


def test_bare_command_resolved_via_path() -> bool:
    print("\n[P2-14] 裸命令名要先经 PATH 解析，才知道它是 .cmd")
    ad = FakeCLI("t", command=["codex"])

    real_which = shutil.which
    shutil.which = lambda name: r"C:\fake\bin\codex.CMD" if name == "codex" else real_which(name)
    try:
        argv = ad._argv("exec", "--json")
    finally:
        shutil.which = real_which

    print(f"  argv = {argv}")
    return report({
        "裸名被解析成 .cmd 后正确包裹了 cmd.exe（P2-14 核心）":
            argv[0].lower() == "cmd.exe" and argv[1] == "/d" and argv[2] == "/c",
        "解析后的真实路径进了 argv": any("codex.CMD" in a for a in argv),
        "传参没丢": "exec" in argv and "--json" in argv,
    })


def test_absolute_cmd_still_wrapped() -> bool:
    print("\n[P1-8] 绝对路径的 .cmd 仍然要包裹 cmd.exe")
    ad = FakeCLI("t", command=[r"C:\Users\x\AppData\Roaming\npm\claude.cmd"])
    argv = ad._argv("-p")
    print(f"  argv = {argv}")
    return report({
        "已包裹": argv[:3] == ["cmd.exe", "/d", "/c"],
        "/d 关掉了 AutoRun": argv[1] == "/d",
        "原路径保留": argv[3].endswith("claude.cmd"),
    })


def test_non_cmd_not_wrapped() -> bool:
    print("\n[回归] 非 .cmd 的可执行文件不该被包裹")
    ad = FakeCLI("t", command=[r"C:\Windows\System32\where.exe"])
    argv = ad._argv("/r")
    return report({
        "没有被 cmd.exe 包裹": argv[0].endswith("where.exe"),
    })


# ---------------------------------------------------------------- P1-7 报告


def test_kill_problems_are_surfaced() -> bool:
    print("\n[P1-7] 终止失败必须被如实报出来，不许静默")
    base = "超时 30s，已终止子进程树"
    quiet = _with_kill_problems(base, [])
    loud = _with_kill_problems(base, ["taskkill 退出码 5: Access is denied"])

    print(f"  无失败 → {quiet}")
    print(f"  有失败 → {loud}")
    return report({
        "无失败时文案不变": quiet == base,
        "有失败时提示可能留下孤儿进程": "孤儿进程" in loud,
        "具体原因被带上": "Access is denied" in loud,
    })


def main() -> int:
    cases = [
        ("P1-8 白名单拦元字符", test_whitelist_blocks_metacharacters),
        ("P1-8 白名单不误伤", test_whitelist_allows_real_ids),
        ("P1-8 spawn 前拒绝", test_call_rejects_before_spawn),
        ("P2-14 裸名解析", test_bare_command_resolved_via_path),
        ("P1-8 绝对路径包裹", test_absolute_cmd_still_wrapped),
        ("回归 非 .cmd 不包裹", test_non_cmd_not_wrapped),
        ("P1-7 终止失败上报", test_kill_problems_are_surfaced),
    ]
    results = []
    for name, fn in cases:
        try:
            results.append((name, fn()))
        except Exception as exc:                        # noqa: BLE001
            print(f"  !! {name} 异常: {type(exc).__name__}: {exc}")
            results.append((name, False))

    print("\n==== 汇总 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
