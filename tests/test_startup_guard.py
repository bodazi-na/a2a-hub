#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""启动护栏（H1）：绑定非回环地址但没配认证时必须拒绝启动。

跑法：
    python tests/test_startup_guard.py

为什么值得一条常驻回归
----------------------
这是**唯一**阻止「hub 裸奔到网络上」的东西。hub 的能力 = 驱动本机 CLI agent
干活，暴露出去等于开放远程代码执行 —— 这条护栏一旦被改坏或绕过，不会有任何
功能测试变红，只会静默地少一层保护。

顺带钉住一个**实测踩到的坑**：重定向时 stdout 是块缓冲的，启动横幅
（**包括那条安全警告**）会一行都打不出来。护栏文案里写着「--allow-insecure
确认要裸奔（会在日志里留永久警告）」—— 缓冲没处理好的话，这句承诺就是空的。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 包在 src 布局下（src/a2a_hub），所以要把 <仓库根>/src 放进 sys.path。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.cli import _is_loopback, _normalize_host  # noqa: E402

PY = sys.executable
PORT = 9276


def check(name: str, cond: bool, detail: str = "") -> tuple[str, bool]:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return name, cond


def serve(host: str, extra: list[str] | None = None,
          env_extra: dict | None = None, wait: float = 0.0):
    """起一个 serve。wait>0 表示跑一会儿再杀，返回 (退出码|None, 输出, 是否活着)。"""
    env = {**os.environ, "PYTHONPATH": os.path.join(_REPO, "src")}
    env.pop("HUB_TOKEN", None)                 # 别让本机环境变量干扰
    if env_extra:
        env.update(env_extra)
    db = os.path.join(os.environ.get("TEMP", "."), "guard_test.db")
    p = subprocess.Popen(
        [PY, os.path.join(_REPO, "hub.py"), "--db", db,
         "serve", "--host", host, "--port", str(PORT), *(extra or [])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    if wait:
        time.sleep(wait)
        alive = p.poll() is None
        p.kill()
        out = p.communicate(timeout=10)[0]
        return (None if alive else p.returncode), out, alive
    try:
        # 注意求值顺序：**先 communicate 再读 returncode**。
        # 反过来（`return p.returncode, p.communicate()[0]`）会在进程还没结束时
        # 就读 returncode，拿到 None —— 而 None 既不等于 0 也不等于 2，
        # 断言会以「退出码不对」的形式失败，看不出真实原因是顺序问题。
        out = p.communicate(timeout=25)[0]
        return p.returncode, out, False
    except subprocess.TimeoutExpired:
        p.kill()
        return None, p.communicate()[0], True


# ------------------------------------------------------------------ 用例

def test_loopback_classification() -> list:
    """哪些地址算「只有本机能访问」。

    原则是**宁可误拒，不放行** —— 解析不了的一律按非回环。但「确实是本机的
    写法」不该误拒：`[::1]` 是 IPv6 在 URL 里的标准写法，`LOCALHOST` 只是大小写
    差异，两者都该放行（放行它们不削弱安全性，因为它们本来就是本机）。
    """
    print("\n[分类] 回环 / 非回环 判定")
    out = []
    for host, want in [
        # —— 确实是本机：必须放行
        ("127.0.0.1", True),
        ("127.1.2.3", True),            # 整个 127/8 都是回环，不只是 127.0.0.1
        ("::1", True),
        ("[::1]", True),                # URL 形式的 IPv6（从地址栏复制过来就带括号）
        ("localhost", True),
        ("LOCALHOST", True),            # 主机名大小写不敏感
        (" 127.0.0.1 ", True),          # 首尾空白
        # —— 会暴露到网络：必须拦住
        ("0.0.0.0", False),             # 所有网卡 —— 最典型的「裸露」
        ("::", False),                  # IPv6 的「所有网卡」
        ("192.168.1.5", False),
        ("10.0.0.1", False),
        ("example.com", False),         # 主机名一律不解析，按非回环
    ]:
        out.append(check(f"{host!r} → {'回环' if want else '非回环'}",
                         _is_loopback(host) is want))
    return out


def test_naked_start_refused() -> list:
    """非回环 + 无认证 → 必须拒绝，且**不能真的绑定端口**。"""
    print("\n[拒绝] 0.0.0.0 无认证 → 拒绝启动")
    code, out, _ = serve("0.0.0.0")
    return [
        check("退出码为 2", code == 2, f"实际 {code}"),
        check("说明点明「远程代码执行」这个真实后果", "远程代码执行" in out),
        check("给出三条出路（认证 / 改回环 / 显式豁免）",
              "①" in out and "②" in out and "③" in out),
        check("拒绝时没有真的起服务", "Uvicorn running" not in out),
    ]


def test_empty_token_is_not_auth() -> list:
    """空 token 不能算「配了认证」—— 否则 `--token ""` 就能绕过护栏。"""
    print("\n[绕过] --token '' 不算有效认证")
    code, _out, _ = serve("0.0.0.0", ["--token", ""])
    return [check("仍被拒绝（退出码 2）", code == 2, f"实际 {code}")]


def test_auth_and_override_pass() -> list:
    """两条合法出路：配 token、或显式 --allow-insecure。"""
    print("\n[放行] 配认证 / 显式豁免")
    out = []
    _c, o1, alive1 = serve("0.0.0.0", ["--token", "s3cret"], wait=4.0)
    out.append(check("--token → 起得来", alive1))
    out.append(check("日志标明认证已启用", "Bearer 已启用" in o1))
    out.append(check("配了认证就不打警告", "!! 警告" not in o1))

    _c, o2, alive2 = serve("0.0.0.0", env_extra={"HUB_TOKEN": "fromenv"}, wait=4.0)
    out.append(check("HUB_TOKEN 环境变量同样算数", alive2))

    _c, o3, alive3 = serve("0.0.0.0", ["--allow-insecure"], wait=4.0)
    out.append(check("--allow-insecure → 起得来", alive3))
    out.append(check("豁免后**仍然**打警告", "警告" in o3 and "同网段" in o3))
    return out


def test_banner_survives_redirect() -> list:
    """启动横幅必须能落进**重定向的日志** —— 尤其那条安全警告。

    实测踩到：重定向时 stdout 是块缓冲的，`[hub]` 一行都打不出来
    （0 行；加 PYTHONUNBUFFERED=1 才有 6 行）。护栏文案承诺「会在日志里留
    永久警告」，缓冲不处理的话这句就是空的。
    """
    print("\n[缓冲] 重定向时启动横幅（含安全警告）要能落盘")
    _c, out, _alive = serve("0.0.0.0", ["--allow-insecure"], wait=4.0)
    lines = [ln for ln in out.splitlines() if "[hub]" in ln]
    return [
        check("横幅有输出（>3 行）", len(lines) > 3, f"实际 {len(lines)} 行"),
        check("安全警告确实在里面", any("警告" in ln for ln in lines)),
    ]


def test_auth_actually_blocks() -> list:
    """护栏只是拦「没配认证就裸奔」；配了认证之后，认证本身要真的拦得住。"""
    print("\n[认证] 配了 token 时，受保护端点真的拦得住")
    env = {**os.environ, "PYTHONPATH": os.path.join(_REPO, "src")}
    env.pop("HUB_TOKEN", None)
    db = os.path.join(os.environ.get("TEMP", "."), "guard_auth.db")
    p = subprocess.Popen(
        [PY, os.path.join(_REPO, "hub.py"), "--db", db, "serve",
         "--host", "127.0.0.1", "--port", str(PORT + 1), "--token", "tok123"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    base = f"http://127.0.0.1:{PORT + 1}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(40):
        time.sleep(0.4)
        try:
            opener.open(f"{base}/healthz", timeout=2)
            break
        except Exception:                       # noqa: BLE001
            continue

    def status(path, headers=None, method="GET", body=None):
        req = urllib.request.Request(f"{base}{path}", method=method,
                                     data=body, headers=headers or {})
        try:
            return opener.open(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        out = [
            check("公开面：/healthz", status("/healthz") == 200),
            check("公开面：Agent Card", status("/.well-known/agent-card.json") == 200),
            check("公开面：/console（只是 HTML 壳）", status("/console") == 200),
            check("受保护：/admin/* 无 token → 401", status("/admin/agents") == 401),
            check("受保护：/admin/* 错 token → 401",
                  status("/admin/agents", {"Authorization": "Bearer wrong"}) == 401),
            check("受保护：/admin/* 正确 token → 200",
                  status("/admin/agents", {"Authorization": "Bearer tok123"}) == 200),
            check("受保护：JSON-RPC 无 token → 401",
                  status("/", {"Content-Type": "application/json"}, "POST",
                         b'{"jsonrpc":"2.0"}') == 401),
            check("路径变体 /console/ 不会漏（fail closed）",
                  status("/console/") == 401),
        ]
    finally:
        p.kill()
        p.wait(timeout=10)
    return out


def test_host_normalization() -> list:
    """归一化：判定与绑定必须用同一个值。

    `[::1]` 是 IPv6 在 URL 里的标准写法（从地址栏复制就带括号）。
    它判定上是回环、该放行，但 **uvicorn 拿 `[::1]` 当主机名解析会失败** ——
    只在判定处剥括号的话，护栏放行了、服务却起不来。
    """
    print("\n[归一化] 剥方括号 / 去空白")
    out = [
        check("[::1] → ::1", _normalize_host("[::1]") == "::1"),
        check("[::ffff:127.0.0.1] → ::ffff:127.0.0.1",
              _normalize_host("[::ffff:127.0.0.1]") == "::ffff:127.0.0.1"),
        check(" 127.0.0.1  → 去空白", _normalize_host(" 127.0.0.1 ") == "127.0.0.1"),
        check("0.0.0.0 不受影响", _normalize_host("0.0.0.0") == "0.0.0.0"),
        check("孤立的 [ 不误剥", _normalize_host("[::1") == "[::1"),
    ]
    return out


def test_loopback_spellings_actually_start() -> list:
    """本机地址的各种写法都要能**真的起起来**（不是只判定通过）。

    「进程 4 秒后仍活着」就说明绑定成功了 —— 绑不上 uvicorn 会直接退出。
    """
    print("\n[端到端] 本机地址的合法写法能真的绑定")
    out = []
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "[::1]", "::1"):
        _c, _o, alive = serve(host, wait=4.0)
        out.append(check(f"--host {host} → 起得来", alive))
    return out


CASES = [
    ("回环判定", test_loopback_classification),
    ("归一化", test_host_normalization),
    ("本机写法能起", test_loopback_spellings_actually_start),
    ("裸奔被拒", test_naked_start_refused),
    ("空 token 不算认证", test_empty_token_is_not_auth),
    ("认证与豁免放行", test_auth_and_override_pass),
    ("重定向时横幅不丢", test_banner_survives_redirect),
    ("认证真的拦得住", test_auth_actually_blocks),
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
