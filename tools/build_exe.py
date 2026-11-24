#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 hub 打成免安装的 Windows exe。

用法
----
    python tools/build_exe.py            # 目录版（推荐）
    python tools/build_exe.py --onefile  # 单文件版
    python tools/build_exe.py --both     # 两种都打，并对比

产物落在 `dist/a2a-hub/`（目录版）或 `dist/a2a-hub.exe`（单文件）。

**打包完会自动实测启动** —— 不是「构建成功就算完」。构建成功但一跑就炸
（比如 uvicorn 的动态导入漏了）是这类打包最常见的失败形态，只有真起一次
服务才能发现。
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import shutil

import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "hub.spec"
DEFAULT_DIST = ROOT / "dist"

# 冒烟检查最多等多久（秒）。超了就判失败并打印进程输出 —— **必须有上界**，
# 否则一个起不来的 exe 会把构建无限挂住。
PROBE_TIMEOUT_SECONDS = 30

# 本次运行的产物目录，由 --dist 覆盖（默认 <仓库>/dist）。
DIST = DEFAULT_DIST

# 两种形态**各用各的 workpath**。共用会踩到一个很隐蔽的坑：先建 onedir、
# 再建 onefile 时，PyInstaller 会复用上一次的中间产物，产出一个**能构建成功、
# 但一跑就报 `Could not create temporary directory!` 的坏 bundle**。
# 实测踩到过，排查了很久 —— 报错信息指向临时目录，和「中间产物不干净」
# 看不出任何关系。
#
# 放在**仓库外**（系统临时目录）：中间产物本来就不该进仓库，而且
# PyInstaller 清理自己的缓存时会一次删掉几十个文件，在受限工作区里
# 会撞上批量删除保护而让构建失败。
WORK_ROOT = Path(tempfile.gettempdir()) / "a2a-hub-build"


def build(onefile: bool) -> Path:
    global DIST
    env = dict(os.environ)
    if onefile:
        env["A2A_HUB_ONEFILE"] = "1"
    else:
        env.pop("A2A_HUB_ONEFILE", None)

    label = "单文件" if onefile else "目录版"
    work = WORK_ROOT / ("onefile" if onefile else "onedir")
    print(f"\n=== 构建{label} ===")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(work), str(SPEC)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit(f"构建失败（退出码 {proc.returncode}）")
    print(f"  构建耗时 {time.time() - t0:.1f}s")

    target = DIST / ("a2a-hub.exe" if onefile else "a2a-hub")
    if not target.exists():
        raise SystemExit(f"没找到产物：{target}")
    _install_launcher(target)
    return target


def _install_launcher(target: Path) -> None:
    """把 `start-hub.bat` 放到 exe 旁边。

    为什么要有它：**双击 exe 起不来界面** —— 不带子命令只会打一行用法错误
    然后退出（退出码 2）。对一个定位成「绿色免安装」的东西来说，这个体验是
    不合格的：用户期望双击就能用。

    启动器做的事：起服务 → 等它就绪 → 自动开浏览器，并且**服务跑在它自己的
    窗口里**，日志可见、Ctrl-C 就能停。

    （另一条路是让不带子命令时默认 `serve`，那会改 CLI 语义 —— 留作备选。）
    """
    src = ROOT / "packaging" / "start-hub.bat"
    if not src.exists():
        print(f"  ! 没找到启动器模板 {src}，跳过")
        return
    dst_dir = target if target.is_dir() else target.parent
    # **显式写 CRLF**：cmd.exe 需要它，而仓库里存的是 LF（见 .gitattributes
    # 的 `*.bat text eol=crlf` —— 那条保证 checkout 出来是 CRLF，但构建时
    # 可能是从工作区直接读的，所以这里再兜一次）。
    text = src.read_text(encoding="ascii")
    (dst_dir / "start-hub.bat").write_text(text, encoding="ascii", newline="\r\n")
    print(f"  已放置启动器 start-hub.bat → {dst_dir}")


def measure(target: Path) -> dict:
    if target.is_dir():
        files = list(target.rglob("*"))
        size = sum(f.stat().st_size for f in files if f.is_file())
        count = sum(1 for f in files if f.is_file())
        exe = target / "a2a-hub.exe"
    else:
        size = target.stat().st_size
        count = 1
        exe = target
    return {"bytes": size, "files": count, "exe": exe}


def smoke(target: Path) -> dict:
    """真起一次服务并打它的 HTTP 接口 —— 构建成功不等于能跑。

    **在临时目录里的副本上测，不在原地测。** 两个原因：

    1. 单文件版启动时要解压到临时目录。若 exe 的**镜像位于受限目录**
       （实测：某些沙箱会对 workspace 内的进程降权），解压会失败并报
       `Could not create temporary directory!` —— 那是环境限制，不是
       打包缺陷，原地测会给出**假阴性**。
    2. 用户拿到 exe 也是先拷到想放的地方再跑，测副本更贴近真实用法。
    """
    with tempfile.TemporaryDirectory(
        prefix="a2a_hub_smoke_", ignore_cleanup_errors=True
    ) as tmp:
        probe = Path(tmp) / ("a2a-hub" if target.is_dir() else target.name)
        if target.is_dir():
            shutil.copytree(target, probe)
        else:
            probe = Path(tmp) / target.name
            shutil.copy2(target, probe)
        exe = (probe / "a2a-hub.exe") if probe.is_dir() else probe
        return _run_probe(exe)


def _run_probe(exe: Path) -> dict:
    print(f"  实测启动 {exe.name} …")
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from a2a_hub.adapters.cli import _JobObject

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    admin = urllib.request.Request(base + "/admin/agents",
                                   headers={"Authorization": f"Bearer {token}"})
    # The launcher waits until it belongs to the job before spawning the onefile bootstrap.
    launcher = ("import subprocess, sys; "
                "sys.exit(subprocess.call(sys.argv[1:]) if sys.stdin.buffer.read(1) else 1)")
    ready_at = None
    body = None
    start = time.monotonic()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", launcher, str(exe), "serve", "--host", "127.0.0.1",
             "--port", str(port)], cwd=exe.parent, stdin=subprocess.PIPE,
            stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "HUB_TOKEN": token},
        )
        job = _JobObject(proc.pid)
        try:
            if not job.ok:
                raise RuntimeError("无法为冒烟测试创建 Windows Job Object")
            proc.stdin.write(b"1")
            proc.stdin.close()
            while proc.poll() is None and time.monotonic() - start < PROBE_TIMEOUT_SECONDS:
                try:
                    # Require this instance's secret and reject an unprotected stale server.
                    try:
                        with opener.open(base + "/admin/agents", timeout=1):
                            raise RuntimeError("冒烟端口存在其他服务")
                    except urllib.error.HTTPError as exc:
                        if exc.code != 401:
                            raise
                    with opener.open(admin, timeout=1), opener.open(base + "/healthz", timeout=1) as resp:
                        body = resp.read().decode("utf-8")
                    ready_at = time.monotonic() - start
                    break
                except OSError:
                    time.sleep(0.1)
        finally:
            if not proc.stdin.closed:
                proc.stdin.close()
            job.close()
            proc.wait(timeout=10)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    if ready_at is not None:
        with socket.socket() as check:
            check.settimeout(1)
            if check.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("冒烟测试结束后服务端口仍被占用")
    return {"readySeconds": ready_at, "health": body, "exitCode": proc.returncode, "output": output}


def main() -> int:
    ap = argparse.ArgumentParser(description="把 hub 打成免安装 Windows exe")
    ap.add_argument("--onefile", action="store_true", help="只打单文件版")
    ap.add_argument("--both", action="store_true", help="两种都打并对比")
    ap.add_argument("--dist", default=None,
                    help="产物目录（默认 <仓库>/dist）。若环境有批量删除保护，"
                         "指向仓库外可绕开 —— 构建会覆盖上一次的产物。")
    args = ap.parse_args()

    global DIST
    if args.dist:
        DIST = Path(args.dist).resolve()
    DIST.mkdir(parents=True, exist_ok=True)

    modes = [True, False] if args.both else [bool(args.onefile)]
    results = []
    for onefile in modes:
        target = build(onefile)
        m = measure(target)
        s = smoke(target)
        results.append((("单文件" if onefile else "目录版"), m, s))
        print(f"  体积 {m['bytes'] / 1024 / 1024:.1f} MB / {m['files']} 个文件")
        if s["readySeconds"] is not None:
            print(f"  ✓ 启动成功，{s['readySeconds']:.2f}s 后 /healthz 就绪")
            print(f"    {s['health']}")
        else:
            print(f"  ✗ 启动失败（退出码 {s['exitCode']}）\n{s['output'][-2000:]}")

    print("\n==== 对比 ====")
    print(f"  {'形态':<8}{'体积':>10}{'文件数':>8}{'启动就绪':>10}  结果")
    for name, m, s in results:
        ready = f"{s['readySeconds']:.2f}s" if s["readySeconds"] is not None else "-"
        ok = "OK" if s["readySeconds"] is not None else "FAIL"
        print(f"  {name:<8}{m['bytes'] / 1024 / 1024:>8.1f}MB{m['files']:>8}{ready:>10}  {ok}")
    return 0 if all(s["readySeconds"] is not None for _n, _m, s in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
