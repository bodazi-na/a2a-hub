#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""守卫：仓库里不得残留旧布局（顶层模块）的**可执行引用**。

跑法：
    python tests/test_no_stale_refs.py

为什么需要这条
--------------
`core` / `adapters` / `probes` / `mcp_facade` 从顶层收进 `a2a_hub.*` 之后，
「残留旧引用」这件事**已经漏了三次**：

  1. `examples/mcp/*` 五个平台接入配置里的 `-m mcp_facade`
  2. `tests/test_mcp_facade.py` 拉起子进程用的 `-m mcp_facade`
  3. `.github/workflows/ci.yml` 里的 `from mcp_facade.server import server`
     —— 这一条直接让 CI 红了

三次都不是「难发现」，而是**没有任何东西会替你发现**：单元测试全绿、
本地跑得好好的，只有真的去用那个入口（或 CI）才暴露。

所以补一条纯文本扫描的守卫。它不判断语义，只问一件事：
**这个仓库里还有没有按旧路径去 import / 调用的地方。**

范围与取舍
----------
只扫**非 Markdown 的可执行/配置文件**（.py / .yml / .json / .toml / .txt /
.cmd / .bat / .spec）。理由：README、CHANGELOG、ADR 里**会正当引用旧写法**
（比如「以前是 `from core.store import Store`」），扫进去只会制造假警报；
而真正会跑起来的引用都在非 Markdown 文件里 —— 三次漏掉的全在那儿。

只看**两种精确形态**，不做宽泛的关键字匹配：
  - `from core.x` / `import adapters.y` 这类**导入语句**
  - `-m mcp_facade` 这类**模块调用**
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 旧布局下的顶层模块名
OLD_TOP = ("core", "adapters", "probes", "mcp_facade")

SCAN_SUFFIXES = {".py", ".yml", ".yaml", ".json", ".toml",
                 ".txt", ".cmd", ".bat", ".spec", ".ps1"}

# ① 导入语句：行首（允许缩进）的 from/import 后跟旧顶层名
RE_IMPORT = re.compile(
    r"^[ \t]*(?:from|import)[ \t]+(?:" + "|".join(OLD_TOP) + r")(?:\.|\s|$)",
    re.MULTILINE,
)
# ② 模块调用：`-m mcp_facade`，含列表形式 ["-m", "mcp_facade"]
RE_MODULE = re.compile(r"""-m["']?[ \t,]+["']?mcp_facade(?![\w.])""")

SKIP_DIR_PARTS = {".git", "__pycache__", "build", "dist", ".venv", "venv",
                  "node_modules", "workspace", "data"}


def check(name: str, cond: bool, detail: str = "") -> tuple[str, bool]:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return name, cond


def tracked_files() -> list[Path]:
    """优先用 git 列出被追踪的文件（天然排除 dist/build/缓存）。

    不在 git 仓库里（比如从源码包解开）时退化成目录遍历。
    """
    try:
        out = subprocess.run(["git", "ls-files"], cwd=_REPO, capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return [_REPO / p for p in out.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        pass

    files = []
    for root, dirs, names in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_PARTS]
        files += [Path(root) / n for n in names]
    return files


def scan() -> list[str]:
    """返回「文件:行号: 内容」形式的问题清单。"""
    problems = []
    for path in tracked_files():
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue                       # .md 等散文跳过，见模块 docstring
        if SKIP_DIR_PARTS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if RE_IMPORT.search(line) or RE_MODULE.search(line):
                rel = path.relative_to(_REPO).as_posix()
                problems.append(f"{rel}:{lineno}: {line.strip()[:90]}")
    return problems


def test_no_stale_refs() -> list:
    print("\n[扫描] 仓库里是否还有按旧布局导入/调用的地方")
    problems = scan()
    if problems:
        print("    发现残留：")
        for p in problems:
            print(f"      {p}")
    return [
        check("没有残留的旧模块导入 / 调用", not problems,
              f"共 {len(problems)} 处" if problems else ""),
    ]


def test_scanner_itself_works() -> list:
    """**扫描器自身要先被验过** —— 一个永远返回空的问题清单看起来和「全绿」一样。

    这条用已知的坏样本喂给它，确认它真的能抓到。
    """
    print("\n[自检] 扫描规则本身有效（拿坏样本喂它）")
    bad = [
        "from core.store import Store",
        "import adapters.cli as m",
        "            from mcp_facade.server import server",
        'args = ["-m", "mcp_facade"]',
        "  -- <PYTHON> -m mcp_facade",
    ]
    good = [
        "from a2a_hub.core.store import Store",
        "from .registry import AgentRegistry",
        "from core_utils import helper",        # 前缀相同但不是旧模块
        "import adapters_extra",                 # 同上
        "python -m a2a_hub.mcp_facade",
        "# 以前是 from core.store import Store（散文里正当引用）",
    ]
    missed = [s for s in bad if not (RE_IMPORT.search(s) or RE_MODULE.search(s))]
    spurious = [s for s in good if RE_IMPORT.search(s) or RE_MODULE.search(s)]
    return [
        check("坏样本全部被抓到", not missed, f"漏掉: {missed}" if missed else ""),
        check("好样本一个都不误报", not spurious,
              f"误报: {spurious}" if spurious else ""),
    ]


CASES = [
    ("扫描器自检", test_scanner_itself_works),
    ("无残留引用", test_no_stale_refs),
]


def main() -> int:
    results: list[tuple[str, bool]] = []
    for name, fn in CASES:
        try:
            results.extend(fn())
        except Exception as exc:                # noqa: BLE001
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
