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
    """列出要扫的文件：**已追踪 + 未追踪但不被忽略**。

    为什么不能用光秃秃的 `git ls-files`：它**不列未追踪文件**，于是这个测试的
    结果会取决于「文件提交了没有」—— 一个刚写好、还没 commit 的 `.py` 里若带旧
    引用，扫描器看不见；等提交之后同一份代码又突然报错。

    这不是假想：**第一版就是这么挂的。** 本地跑（本文件当时未追踪）显示全绿，
    推上去（已追踪）立刻红 —— 它扫到了自己的文档说明与自检样本。
    `--others --exclude-standard` 把未追踪但未被忽略的文件也纳入，
    让「本地」和「CI」看到同一批文件。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=_REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return [_REPO / p for p in out.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        pass

    files = []
    for root, dirs, names in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_PARTS]
        files += [Path(root) / n for n in names]
    return files


# 本文件里有**故意的坏样本**（自检用）和解释性文档，扫自己必然误报。
_SELF = Path(os.path.abspath(__file__)).resolve()


def scan() -> list[str]:
    """返回「文件:行号: 内容」形式的问题清单。"""
    problems = []
    for path in tracked_files():
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue                       # .md 等散文跳过，见模块 docstring
        if SKIP_DIR_PARTS & set(path.parts):
            continue
        try:
            if path.resolve() == _SELF:
                continue                   # 见 _SELF 的说明
        except OSError:
            pass
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


def test_scanner_ignores_itself() -> list:
    """扫描器不得把**自己**报成残留。

    这条钉住一个真实踩过的坑：第一版用光秃秃的 `git ls-files`，它不列未追踪
    文件 —— 本地跑时本文件还没 `git add`，所以「扫不到自己」显示全绿；
    推上去之后它被追踪了，于是扫到自己的文档说明与自检样本，**CI 立刻红**。

    断言两件事：报告里不出现本文件；且文件清单**确实包含未追踪文件**
    （否则「本地绿、CI 红」还会再来一次）。
    """
    print("\n[自检] 扫描器不扫自己 / 能看到未追踪文件")
    problems = scan()
    self_reported = [p for p in problems if "test_no_stale_refs" in p]
    listed = {p.resolve() for p in tracked_files()}
    return [
        check("报告里没有本文件", not self_reported,
              f"共 {len(self_reported)} 处" if self_reported else ""),
        check("文件清单包含本文件（说明未追踪的也扫）", _SELF in listed),
    ]


CASES = [
    ("扫描器自检", test_scanner_itself_works),
    ("不扫自己", test_scanner_ignores_itself),
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
