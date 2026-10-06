#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复 workspace 下「早于授权存在」的子目录 —— 让它们重新继承父目录的沙箱授权。

问题
----
CLI agent 在 workspace 里建目录时，目录会继承父目录当时的 ACL。
如果那个父目录**当时还没有**沙箱授权项（例如 hub 的 workspace 刚建好、
或某个子目录在权限补齐之前就存在），这个子目录就会缺授权，
之后 agent 在里面新建文件会被拒：

    SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)
    # 或更常见的：PermissionError / 拒绝访问

**关键**：后来给父目录补的授权**不会回溯**到已存在的子目录 —— Windows 的继承
只在对象创建那一刻生效。所以需要显式地把这些子目录的继承重新打开。

处置
----
对每个「未启用继承」的目录执行 `icacls <dir> /inheritance:e`，
它只**启用继承**（把父目录的可继承项拉下来），**不清除**该目录已有的显式项，
比 `/reset` 温和。

用法
----
    python tools/fix_workspace_acl.py --dry-run     # 只看哪些目录有问题
    python tools/fix_workspace_acl.py               # 实际修复
    python tools/fix_workspace_acl.py --root <dir>  # 指定其它根目录
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

DEFAULT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")


def _icacls(path: str) -> str:
    """读一个路径的 ACL 文本（icacls 在中文系统下输出 GBK）。"""
    proc = subprocess.run(
        ["icacls", path],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    )
    return proc.stdout or proc.stderr or ""


def has_inherited_aces(path: str) -> bool:
    """该路径是否已有「继承来的」授权项。

    icacls 输出里继承项带 `(I)` 标记。完全没有 (I) 项 = 继承被切断，
    父目录后补的授权到不了这里。
    """
    return "(I)" in _icacls(path)


def enable_inheritance(path: str) -> tuple[bool, str]:
    """启用继承（只拉取可继承项，不清除显式项）。"""
    proc = subprocess.run(
        ["icacls", path, "/inheritance:e"],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    )
    ok = proc.returncode == 0
    return ok, (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] and \
        (proc.stdout or proc.stderr or "").strip().splitlines()[-1] or ""


def scan(root: str) -> list[str]:
    """找出 root 下所有「缺继承」的目录（含 root 自身）。"""
    broken: list[str] = []
    if not os.path.isdir(root):
        return broken
    if not has_inherited_aces(root):
        broken.append(root)
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if not has_inherited_aces(full):
                broken.append(full)
    return broken


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT, help="要扫描的根目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不修改")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    print(f"扫描根目录: {root}")
    if not os.path.isdir(root):
        print("  (目录不存在，无事可做)")
        return 0

    broken = scan(root)
    if not broken:
        print("\n所有目录都已有继承授权，无需修复。")
        return 0

    print(f"\n发现 {len(broken)} 个缺继承授权的目录:")
    for p in broken:
        print(f"  {os.path.relpath(p, root)}")

    if args.dry_run:
        print("\n(--dry-run：未做任何修改)")
        return 0

    print("\n开始修复（icacls <dir> /inheritance:e）...")
    fixed, failed = 0, []
    for p in broken:
        ok, _ = enable_inheritance(p)
        if ok:
            fixed += 1
            print(f"  [OK]   {os.path.relpath(p, root)}")
        else:
            failed.append(p)
            print(f"  [FAIL] {os.path.relpath(p, root)}")

    print(f"\n完成：修复 {fixed} 个，失败 {len(failed)} 个")
    if failed:
        print("\n失败的多半是「当前进程也没有权限改它」——")
        print("那属于令牌降权，需要在普通终端里跑本脚本。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
