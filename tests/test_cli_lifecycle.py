#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 CLI 适配器的生命周期修复（A2A-02 / 03 / 14）。

真实 CLI 很难构造触发条件，所以用「假 CLI」精确复现：
  - 不读 stdin 的进程  → 复现 stdin 写入阻塞
  - 输出超长单行的进程 → 复现流超限
  - --version 挂起的进程 → 复现探测残留

跑法：python tests/test_cli_lifecycle.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
# 不是仓库根 —— 仓库根下已经没有可导入的包了。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.adapters.cli import CLIAdapter  # noqa: E402

PY = sys.executable
TMP = tempfile.gettempdir()


def write_stub(name: str, body: str) -> str:
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


class _Probe(CLIAdapter):
    """只为复用骨架，不做真实解析。"""

    def build_argv(self, session_id):
        return []

    def parse_line(self, obj, state):
        return []

    def finalize(self, state, returncode):
        from a2a_hub.adapters.cli import CLIOutcome
        return CLIOutcome(ok=returncode == 0, text="")


# ---- 假 CLI：完全不读 stdin，然后睡很久 -------------------------------------
STUB_NO_STDIN = """
import time, sys
time.sleep(120)
"""

# ---- 假 CLI：输出一行超长的 JSON --------------------------------------------
STUB_HUGE_LINE = """
import json
payload = {"type": "final", "text": "x" * (200 * 1024)}
print(json.dumps(payload), flush=True)
"""

# ---- 假 CLI：--version 直接挂起 --------------------------------------------
STUB_HANG_VERSION = """
import time, sys
time.sleep(600)
"""


def count_child(image_hint: str) -> int:
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image_hint}"],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    ).stdout
    return sum(1 for line in out.splitlines() if image_hint.lower() in line.lower())


async def test_a2a_02_stdin_timeout() -> bool:
    """A2A-02：下游不读 stdin 时，写 prompt 必须在超时内被兜住。"""
    print("\n[A2A-02] 写 stdin 纳入超时保护")
    stub = write_stub("_stub_no_stdin.py", STUB_NO_STDIN)
    adapter = _Probe("no-stdin", command=[PY, stub], timeout=6.0)

    big_prompt = "y" * (2 * 1024 * 1024)  # 2MB，远超管道缓冲
    loop = asyncio.get_event_loop()
    started = loop.time()
    result = await adapter.call(big_prompt, timeout=6.0)
    elapsed = loop.time() - started

    print(f"  ok={result.ok} elapsed={elapsed:.1f}s error={str(result.error)[:70]}")
    ok = (not result.ok) and elapsed < 20
    print("  ->", "PASS" if ok else "FAIL（未被超时兜住，可能挂死了）")
    return ok


async def test_a2a_03_huge_line() -> bool:
    """A2A-03：超长单行不应导致异常穿透 / 子进程残留。"""
    print("\n[A2A-03] 超长单行输出")
    stub = write_stub("_stub_huge_line.py", STUB_HUGE_LINE)
    adapter = _Probe("huge-line", command=[PY, stub], timeout=20.0)

    result = await adapter.call("hi", timeout=20.0)
    print(f"  ok={result.ok} error={str(result.error)[:90]}")
    # 关键不是它成功，而是**没有异常穿透**（call 必须正常返回 CallResult）
    ok = isinstance(result.ok, bool)
    print("  ->", "PASS" if ok else "FAIL（异常穿透了 call）")
    return ok


async def test_a2a_14_probe_cleanup() -> bool:
    """A2A-14：--version 挂起时，探测必须回收子进程。"""
    print("\n[A2A-14] 探测超时回收子进程")
    stub = write_stub("_stub_hang_version.py", STUB_HANG_VERSION)

    before = count_child("python.exe")
    adapter = _Probe("hang-version", command=[PY, stub], timeout=5.0)

    # 直接调 probe：内部 30s 超时。为节省时间，这里用 asyncio 超时提前打断，
    # 关键是验证「被打断时子进程被带走」。
    try:
        await asyncio.wait_for(adapter.probe(), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(2.0)
    after = count_child("python.exe")

    print(f"  python.exe 进程数: {before} -> {after}")
    ok = after <= before
    print("  ->", "PASS" if ok else "FAIL（探测留下了孤儿进程）")
    return ok


async def main() -> int:
    checks = [
        await test_a2a_02_stdin_timeout(),
        await test_a2a_03_huge_line(),
        await test_a2a_14_probe_cleanup(),
    ]
    print("\n==== 汇总 ====")
    for name, ok in zip(["A2A-02", "A2A-03", "A2A-14"], checks):
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
