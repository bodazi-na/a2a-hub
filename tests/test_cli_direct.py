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

本机路径从哪来
--------------
这些启动器路径**因机器而异，不要写死**。默认从环境推导，可用环境变量覆盖：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `NPM_GLOBAL_BIN` | npm 全局 bin 目录 | `%APPDATA%\\npm` |
| `DSH_LAUNCHER` | dsh 启动器完整路径 | 空 → 用裸名 `dsh`，靠 PATH 解析 |
| `QODER_MODEL` | Qoder 的 modelID（**UUID**，不是显示名） | 空 → 该节点跳过 |
| `QODER_PAT_FILE` | Qoder PAT 文件路径 | `<仓库上级>/.qoder_pat` |

这是**集成级**测试（见 CONTRIBUTING.md）—— 它验证的是适配器与本机工具的对接，
只在开发机上跑。缺哪个节点就跳过哪个，不算失败。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import HEALTH_OK                              # noqa: E402
from adapters.cli import ClaudeCLI, CodexCLI, DshCLI, QoderCLI  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_config(agent_file: str, key: str = "command"):
    """从本机的 `agents/<name>.json` 读一项配置（读不到返回 None）。

    为什么值得读它：`agents/` 是**本机配置**（不进仓库），里面存的才是这台机器上
    真正能用的路径。实测踩过 —— 本机 PATH 上有个同名的**空壳** `dsh.cmd`
    （打印 "Harness CLI not found" 后退出码 1），真正能用的在别的路径；
    而 hub 自己的 `agents/dsh-cli.json` 里写的正是后者。

    优先级：环境变量 > 本机 agents 配置 > 内置默认值。
    """
    import json

    try:
        with open(os.path.join(ROOT, "agents", agent_file), encoding="utf-8") as fh:
            spec = json.load(fh)
        value = (spec.get("options") or {}).get(key)
        return value or None
    except (OSError, ValueError):
        return None


# 默认值从环境推导；**不要把某个人的用户名写进仓库**
NPM = os.environ.get("NPM_GLOBAL_BIN") or os.path.join(
    os.environ.get("APPDATA", ""), "npm"
)
# 环境变量 > 本机 agents 配置 > 裸名（交给适配器按 PATH 解析）
DSH_LAUNCHER = (
    os.environ.get("DSH_LAUNCHER")
    or (_local_config("dsh-cli.json") or ["dsh"])[0]
)
# Qoder 的 -m 必须传 modelID（UUID）；传显示名会静默回退到内置模型
QODER_MODEL = os.environ.get("QODER_MODEL") or _local_config("qoder-cli.json", "model") or ""
PAT_FILE = os.environ.get("QODER_PAT_FILE") or os.path.join(
    os.path.dirname(ROOT), ".qoder_pat"
)


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

    # 缺配置就**明确跳过**，不要崩，也不要假装测过。
    # 这是集成级测试：它验证的是「适配器与本机工具的对接」，
    # 本机没装那个工具时本来就无从验证。
    if name == "qoder" and not QODER_MODEL:
        print("SKIP（未测试）：没有 QODER_MODEL。")
        print("  Qoder 的 -m 必须传 modelID（UUID），不能传显示名 ——")
        print("  传显示名会被静默回退到内置模型并消耗 Qoder 额度。")
        print("  用 `qodercli --list-models` 查到 UUID 后：export QODER_MODEL=<uuid>")
        return 0

    adapter = BUILDERS[name]()
    if not adapter.detect():
        print(f"SKIP（未测试）：本机找不到 {name} 的启动器。")
        print(f"  command = {adapter.command}")
        print("  用环境变量指定路径（见本文件顶部说明），或确认它已装好并在 PATH 上。")
        return 0

    # 启动器存在但不可用，也要**明确跳过**。
    # 实测踩过：本机 PATH 上有个同名的**空壳** `dsh.cmd`（会打印
    # "Harness CLI not found" 然后退出码 1），而真正能用的在别的路径。
    # 这时 probe 判为 down 是**正确行为** —— 是本机配置问题，不是适配器缺陷，
    # 所以不该报 FAIL；但也不能悄悄跳过，要把解析到的路径打出来。
    health = await adapter.probe()
    if health != HEALTH_OK:
        print(f"SKIP（未测试）：{name} 的启动器存在但不可用（probe={health}）。")
        print(f"  解析到的 command = {adapter.command}")
        print("  常见原因：PATH 上有个同名的空壳启动器，真正能用的在别的路径。")
        print("  用环境变量指定真实路径后重跑，例如：")
        print("    export DSH_LAUNCHER='D:/path/to/real/dsh.cmd'")
        return 0

    print(f"===== {name} =====")
    print("detect :", adapter.detect())
    print("probe  :", health)

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
