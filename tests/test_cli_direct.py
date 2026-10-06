#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接测 CLI 适配器（不经 hub，隔离问题）。

    python tests/test_cli_direct.py dsh
    python tests/test_cli_direct.py claude
    python tests/test_cli_direct.py qoder
    python tests/test_cli_direct.py codex

验证三件事：detect / probe / 跨轮续接。
续接用「记住 7391 再追问」这个项目里一直用的验证套路，
好处是能一眼看出是真续接还是模型瞎猜。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.cli import ClaudeCLI, CodexCLI, DshCLI, QoderCLI  # noqa: E402

NPM = r"C:\Users\a1299\AppData\Roaming\npm"
DSH_LAUNCHER = r"D:\DSH\resources\runtime\cli\bin\dsh.cmd"
QODER_MODEL = "d6dc9388-f023-443f-b702-2d154f1ac322"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAT_FILE = os.path.join(os.path.dirname(ROOT), ".qoder_pat")


def read_qoder_pat() -> str:
    """从 .qoder_pat 读 PAT（跳过注释与空行）。

    qodercli 靠 QODER_PERSONAL_ACCESS_TOKEN 认证；IDE 的登录态它读不到，
    必须显式注入环境变量，否则一切调用都返回 Not logged in。
    """
    try:
        with open(PAT_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("<"):
                    return line
    except OSError:
        pass
    return ""


BUILDERS = {
    "dsh": lambda: DshCLI("dsh-cli", command=[DSH_LAUNCHER], timeout=300),
    "claude": lambda: ClaudeCLI("claude-cli", command=[NPM + r"\claude.cmd"], timeout=300),
    "qoder": lambda: QoderCLI(
        "qoder-cli", command=[NPM + r"\qodercli.cmd"], model=QODER_MODEL, timeout=300,
        env={"QODER_PERSONAL_ACCESS_TOKEN": read_qoder_pat()},
    ),
    "codex": lambda: CodexCLI("codex-cli", command=[NPM + r"\codex.cmd"], timeout=300),
}


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "dsh"
    if name not in BUILDERS:
        raise SystemExit(f"unknown cli: {name} (可选 {list(BUILDERS)})")

    adapter = BUILDERS[name]()
    print(f"===== {name} =====")
    print("detect :", adapter.detect())
    print("probe  :", await adapter.probe())

    print("\n--- 第 1 轮：记住一个数字 ---")
    first = await adapter.call("Remember the code 7391. Reply with exactly: SAVED")
    print("ok      :", first.ok)
    print("text    :", repr(first.text[:120]))
    print("session :", first.session_id)
    print("usage   :", first.usage)
    print("events  :", len(first.events), [e.kind for e in first.events][:8])
    if first.error:
        print("error   :", first.error)
    if not first.ok:
        print("\nRESULT: FAIL (第一轮就没跑通)")
        return 1

    print("\n--- 第 2 轮：同一 session 追问 ---")
    second = await adapter.call(
        "What was the code I asked you to remember? Reply with the number only.",
        session_id=first.session_id,
    )
    print("ok      :", second.ok)
    print("text    :", repr(second.text[:120]))
    print("session :", second.session_id)
    if second.error:
        print("error   :", second.error)

    ok = second.ok and "7391" in (second.text or "")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
