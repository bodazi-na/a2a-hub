#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定向 SSRF 防护：hub 不该被下游指定的地址牵着去打内网。

跑法：
    python tests/test_ssrf_guard.py

为什么值得一条常驻回归
----------------------
这是**唯一**阻止 hub 变成内网跳板的东西。它一旦被改坏，不会有任何功能测试
变红 —— 所有既有用例都在访问 127.0.0.1，而回环是**刻意放行**的。

威胁路径（不是假想，是代码里现成的）：
  1. `register --endpoint URL` 拉一次下游 Agent Card 就存库；
  2. `A2AHttpAdapter._pick_rpc_url()` **直接采信卡片里的
     `supportedInterfaces[].url`**；
  3. 此后每次 `probe` 与每次派活都会往那个地址发请求，响应体还会被解析回传。

所以「注册一个恶意下游」≈「拿到一条对内网任意端点的 POST 通道」。

本文件钉住三件事
----------------
  A. 分类口径：拦什么、放行什么（含这次实测抓到的漏判）；
  B. 拦截真的发生在**连接层**（不只是入库前扫一眼）；
  C. 放行面没有被误伤 —— 每个「该拦」的用例都配一个「该放行」的对照。

关于 B 的坑：`169.254.169.254` 在测试机上并不监听，**不拦也会报连接错误**。
所以断言不能是「抛异常就算拦住了」，必须断言**异常类型是 NetworkGuardError**。
写成本地版就等着假绿：任何网络故障都会让它通过。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from a2a_hub.adapters.a2a_http import A2AHttpAdapter            # noqa: E402
from a2a_hub.core.netguard import (                             # noqa: E402
    GuardedTransport,
    NetworkGuardError,
    TargetPolicy,
    check_url,
    is_blocked_host,
    make_client,
    resolve_and_check,
)


def check(name: str, cond: bool, detail: str = "") -> tuple[str, bool]:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return name, cond


def raises_guard(fn) -> tuple[bool, str]:
    """跑 fn，判断是否**恰好**抛出 NetworkGuardError。

    返回 (是否为护栏拒绝, 实际异常描述)。这里刻意不写成「抛了任何异常就算过」——
    那会把连接失败、超时、DNS 错误全都算成拦截成功。
    """
    try:
        fn()
    except NetworkGuardError as exc:
        return True, f"NetworkGuardError: {str(exc).splitlines()[0][:50]}"
    except Exception as exc:                                    # noqa: BLE001
        return False, f"非护栏异常（说明没拦住）: {type(exc).__name__}: {exc}"
    return False, "没有抛异常（说明放行过去了）"


# ------------------------------------------------------------------ A. 分类

def test_must_block() -> list:
    """必须拦下的目标。每一条都是「这台机器能到、但攻击者不该借道到达」的地方。"""
    print("\n[拦截] 本机 / 内网 / 保留段")
    out = []
    for host, why in [
        # —— 云元数据：本模块存在的主要理由
        ("169.254.169.254", "链路本地 · 云实例凭据端点"),
        ("169.254.1.1", "链路本地整段"),
        ("fe80::1", "IPv6 链路本地"),
        # —— 最常见的绕过写法
        ("::ffff:169.254.169.254", "IPv4-mapped IPv6 包装元数据地址"),
        ("::ffff:10.0.0.1", "IPv4-mapped IPv6 包装私网地址"),
        # —— 私网
        ("10.0.0.5", "RFC1918"),
        ("172.16.3.4", "RFC1918"),
        ("192.168.1.10", "RFC1918"),
        ("fc00::1", "IPv6 unique-local"),
        # —— 实测抓到的漏判：is_private 对 CGNAT 返回 False
        ("100.64.0.1", "CGNAT（RFC6598）—— 曾静默放行"),
        ("100.127.255.254", "CGNAT 段上界"),
        # —— 保留 / 文档段
        ("0.0.0.0", "unspecified"),
        ("255.255.255.255", "广播"),
        ("240.0.0.1", "保留段"),
        ("192.0.2.1", "TEST-NET-1"),
        ("198.18.0.1", "基准测试保留"),
        ("203.0.113.7", "TEST-NET-3"),
        # —— 组播
        ("224.0.0.1", "IPv4 组播"),
        ("ff02::1", "IPv6 组播"),
    ]:
        reason = is_blocked_host(host)
        out.append(check(f"{host:24} 拦（{why}）",
                         reason is not None, f"实际: {reason or '放行'}" ))
    return out


def test_must_allow() -> list:
    """必须放行的目标。**这是防「把护栏写成拦一切」的对照面。**

    回环放行是刻意的设计取舍：hub 的全部既有用法（9100/9101 本地桥、
    本机 mock）都是回环，一刀拦掉等于砍掉主用法 —— 而一个砍掉主用法的
    默认值，最后一定会被 `--allow-*` 整体关掉，连带真正该拦的也一起放行。
    """
    print("\n[放行] 回环 / 公网 / 域名")
    out = []
    for host, why in [
        ("127.0.0.1", "回环 —— hub 的主场景"),
        ("127.1.2.3", "整个 127/8 都是回环"),
        ("::1", "IPv6 回环"),
        ("[::1]", "URL 写法（带方括号）"),
        ("localhost", "主机名写法"),
        ("LOCALHOST", "大小写不敏感"),
        ("8.8.8.8", "公网"),
        ("1.1.1.1", "公网"),
        ("2606:4700::1111", "公网 IPv6"),
        ("example.com", "域名：判定推迟到连接层"),
        ("10.0.0.1.example.com", "诡异域名：不当成 IP 处理"),
        ("", "空串不误判"),
        ("not-an-ip", "非 IP 字面量"),
    ]:
        out.append(check(f"{host!r:26} 放行（{why}）",
                         is_blocked_host(host) is None))
    return out


def test_scheme_and_url() -> list:
    """非 http/https 一律拒绝 —— 这套防护只管 HTTP 出网，别的协议直接关门。

    另外钉住**畸形 URL 必须优雅拒绝**：`urlsplit` 对畸形 IPv6 会抛裸
    `ValueError`，调用方只捕 `NetworkGuardError` —— 漏成别的异常类型就会被
    当成「非护栏问题」放过去。还有 `http://fe80::1/`（IPv6 忘了加方括号）：
    urlsplit 把它拆成 hostname=`fe80`、port=1，**静态预检直接放行** —— 实测踩到。
    """
    print("\n[协议] 只允许 http/https，畸形写法也要拦")
    out = []
    for url, want in [
        ("http://127.0.0.1:9200/", True),
        ("https://example.com/x", True),
        ("http://[::1]:9200/", True),
        ("http://a-b.example.com/", True),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("file:///etc/passwd", False),
        ("ftp://example.com/x", False),
        ("gopher://example.com/x", False),
        ("//169.254.169.254/", False),                 # 没有 scheme
        ("http:///nohost", False),                     # 没有 host
        # —— 畸形写法（都曾漏过或抛裸异常）
        ("http://fe80::1/", False),                    # IPv6 没加方括号
        ("http://[fe80::1]/", False),                  # 合法的 IPv6 链路本地
        ("http://[fe80::1/", False),                   # 括号不闭合 → 曾抛 ValueError
    ]:
        ok, detail = raises_guard(lambda u=url: check_url(u))
        out.append(check(f"{url:44} {'放行' if want else '拦'}",
                         ok is (not want), detail if ok or not want else ""))
    return out


def test_policy_switch() -> list:
    """`--allow-local-targets` 只放开回环与私网；**链路本地永远不放**。"""
    print("\n[开关] 放宽策略的口径")
    permissive = TargetPolicy(allow_loopback=True, allow_private=True)
    strict_loopback = TargetPolicy(allow_loopback=False)
    return [
        check("默认：回环放行", is_blocked_host("127.0.0.1") is None),
        check("默认：私网拦截", is_blocked_host("10.0.0.5") is not None),
        check("放宽后：私网放行",
              is_blocked_host("10.0.0.5", policy=permissive) is None),
        check("放宽后：CGNAT 也放行",
              is_blocked_host("100.64.0.1", policy=permissive) is None),
        check("放宽后：**链路本地仍拦**（元数据没有开关）",
              is_blocked_host("169.254.169.254", policy=permissive) is not None),
        check("放宽后：组播仍拦",
              is_blocked_host("224.0.0.1", policy=permissive) is not None),
        check("收紧：可单独关掉回环放行",
              is_blocked_host("127.0.0.1", policy=strict_loopback) is not None),
    ]


# ------------------------------------------------------ B. 连接层真的拦住了

def test_transport_blocks() -> list:
    """护栏必须落在**连接层**，不是只在入口扫一眼。

    为什么这条重要：`_pick_rpc_url()` 是「先拉卡片、再按卡片内容请求」——
    最终请求目标运行时才确定，入口预检查不到。把判定放到拨号那一刻，
    查到的是**本次连接实际要拨的地址**，DNS rebinding 也随之失效。

    这里同时钉住一个**踩过的坑**：最初是重写
    `httpx.AsyncHTTPTransport.connect_tcp()` —— 但 httpx 0.28 根本没这个方法
    （只有 `handle_async_request` / `aclose`），那个重写是死代码。
    症状很隐蔽：IP 字面量地址因为静态预检仍然被拦，看起来「管用」，
    而**域名形式的地址会溜到真正建连**（实测 `10.0.0.5` 报的是 ConnectTimeout，
    说明请求确实发出去了）。所以本用例必须走**真实 client**，
    直连 transport 的内部方法测不出这个问题。
    """
    print("\n[连接层] 实际建连时拒绝（走真实 client，不走内部方法）")

    async def try_client(host: str, port: int = 80):
        """走完整 client —— 这是唯一能暴露「护栏没生效」的路径。"""
        try:
            async with make_client(timeout=3.0) as c:
                await c.get(f"http://{host}:{port}/")
        except NetworkGuardError as exc:
            return True, f"NetworkGuardError: {str(exc).splitlines()[0][:56]}"
        except Exception as exc:                                # noqa: BLE001
            return False, f"非护栏异常（说明没拦住）: {type(exc).__name__}: {exc}"
        return False, "请求成功"

    async def run():
        out = []
        for host in ("169.254.169.254", "10.0.0.5", "100.64.0.1", "[fe80::1]"):
            blocked, detail = await try_client(host)
            out.append(check(f"拒连 {host}", blocked, detail))

        # 域名解析到内网 —— 静态预检看不出，只有连接层能拦。
        # **注意不能拿 localhost 当例子**：它确实解析到 127.0.0.1，而回环是
        # 刻意放行的，用它会得到一个「本来就该放行」的假失败。
        # 这里打桩解析器，模拟真正的 rebinding 场景（域名指向内网）。
        real_gai = socket.getaddrinfo
        try:
            socket.getaddrinfo = lambda h, p, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', p or 0))]
            blocked, detail = await try_client("evil.example.com")
            out.append(check("拒连 解析到内网的域名（模拟 rebinding）",
                             blocked, detail))
        finally:
            socket.getaddrinfo = real_gai

        # 对照：解析到回环的域名**应当放行**（回环是刻意放行的设计）
        blocked, detail = await try_client("localhost", 9)
        out.append(check("放行 解析到回环的域名（localhost）", not blocked, detail))
        return out

    return asyncio.run(run())


def test_transport_installs_guard() -> list:
    """护栏确实被装上了 —— 装不上时必须**大声失败**，不能静默降级。

    httpx 的内部结构不是公开 API；万一某天变了，`_pool._network_backend`
    可能取不到。那时如果只是静默跳过替换，就会出现最坏的情况：
    整套代码看起来都在、测试也在跑，**实际一次都没拦过**。
    所以构造期取不到结构就直接抛，这条用例把它钉住。
    """
    print("\n[装配] 护栏装得上，装不上要报错")
    t = GuardedTransport()
    inner = t._pool._network_backend
    out = [
        check("transport 构造成功", isinstance(t, GuardedTransport)),
        check("连接池的后端已被替换成护栏后端",
              inner.__class__.__name__ == "_GuardedNetworkBackend",
              f"实际: {inner.__class__.__name__}"),
        check("后端持有策略对象", getattr(inner, "_policy", None) is not None),
    ]
    from a2a_hub.core.netguard import _GuardedNetworkBackend
    backend = _GuardedNetworkBackend(None, TargetPolicy())

    async def go():
        try:
            await backend.connect_tcp("169.254.169.254", 80)
        except NetworkGuardError:
            return True
        except Exception as exc:                                # noqa: BLE001
            return f"非护栏异常: {type(exc).__name__}"
        return "连通了"

    res = asyncio.run(go())
    out.append(check("后端自身的 connect_tcp 拦住元数据地址", res is True, str(res)))
    return out


def test_card_declared_endpoint() -> list:
    """**核心威胁路径**：卡片声明的 RPC 端点不能直接采信。

    这就是 SSRF 的入口 —— 下游 Agent Card 说「JSON-RPC 打到我这儿：
    http://169.254.169.254/」，如果不校验，hub 之后每次派活都会往那儿 POST。
    """
    print("\n[卡片] supportedInterfaces 里声明的端点必须过护栏")

    def card(url: str) -> dict:
        return {"supportedInterfaces": [
            {"protocolBinding": "JSONRPC", "url": url}]}

    a = A2AHttpAdapter("evil", "http://127.0.0.1:9999")
    picked = a._pick_rpc_url(card("http://169.254.169.254/"))
    out = [
        check("恶意端点被忽略，退回 base",
              picked == "http://127.0.0.1:9999/", f"实际选中: {picked}"),
    ]

    b = A2AHttpAdapter("good", "http://127.0.0.1:9999")
    picked2 = b._pick_rpc_url(card("http://127.0.0.1:9999/rpc"))
    out.append(check("合法端点仍被采用",
                     picked2 == "http://127.0.0.1:9999/rpc", f"实际: {picked2}"))

    c = A2AHttpAdapter("evil2", "http://127.0.0.1:9999")
    picked3 = c._pick_rpc_url(card("file:///etc/passwd"))
    out.append(check("非 http scheme 的端点被忽略",
                     picked3 == "http://127.0.0.1:9999/", f"实际: {picked3}"))

    # 放宽策略后，私网端点应被接受（但链路本地依然不行）
    d = A2AHttpAdapter("lan", "http://127.0.0.1:9999",
                       policy=TargetPolicy(allow_loopback=True, allow_private=True))
    picked4 = d._pick_rpc_url(card("http://10.0.0.7:8080/"))
    out.append(check("放宽后私网端点被接受",
                     picked4 == "http://10.0.0.7:8080/", f"实际: {picked4}"))
    picked5 = d._pick_rpc_url(card("http://169.254.169.254/"))
    out.append(check("放宽后链路本地**仍被忽略**",
                     picked5 == "http://127.0.0.1:9999/", f"实际: {picked5}"))
    return out


def test_rpc_call_blocked() -> list:
    """`_rpc()` 本身也要拦 —— 不能指望调用方每次都先走一遍预检。"""
    print("\n[派活] _rpc 直连内网地址时被拦")

    async def run():
        a = A2AHttpAdapter("evil", "http://127.0.0.1:9999")
        # 模拟「卡片已把 rpc_url 设成内网地址」（绕过 _pick_rpc_url 的极端情形）
        a.rpc_url = "http://169.254.169.254/"
        try:
            await a._rpc("SendMessage", {}, timeout=3.0)
        except NetworkGuardError as exc:
            return [check("_rpc 抛 NetworkGuardError 而非发起请求",
                          True, f"{str(exc).splitlines()[0][:52]}")]
        except Exception as exc:                                # noqa: BLE001
            return [check("_rpc 抛 NetworkGuardError 而非发起请求", False,
                          f"实际: {type(exc).__name__}: {exc}")]
        return [check("_rpc 抛 NetworkGuardError 而非发起请求", False,
                      "请求居然成功了")]

    return asyncio.run(run())


def test_resolve_then_check() -> list:
    """连接层拿到的可能是**没解析过的域名**，必须自己解析后逐个判定。

    两个实测踩到的坑，都钉在这里：

    1. **`httpcore` 送来的 host 是域名本身**（`'localhost'`），不是解析后的 IP。
       而 `is_blocked_host('localhost')` 对域名返回 None（判定推迟）。所以
       「解析到内网的域名」在一开始是**畅通无阻**的 —— 防护形同虚设。
    2. 修这个问题时，第一版把「IP 字面量已被拒」写成了 `return`（当成放行），
       等于**把该拦的 IP 静默放过**。当时因为入口的静态预检更早拦下 IP 字面量，
       这个错被完全遮住 —— 只有连接层这条路会暴露它。
       所以下面必须有「IP 字面量在连接层也要抛」这一条。
    """
    print("\n[解析] 域名解析后再判 + IP 字面量在连接层也要拦")

    out = []
    # ① IP 字面量：连接层直接判，且**被拒要抛**
    for host in ("10.0.0.5", "169.254.169.254", "100.64.0.1"):
        ok, detail = raises_guard(lambda h=host: resolve_and_check(h, TargetPolicy()))
        out.append(check(f"连接层拦 IP 字面量 {host}", ok, detail))
    for host in ("127.0.0.1", "::1"):
        try:
            resolve_and_check(host, TargetPolicy())
            out.append(check(f"连接层放行 {host}", True))
        except NetworkGuardError as exc:
            out.append(check(f"连接层放行 {host}", False, str(exc)[:50]))

    # ② 域名解析到内网 → 拦（不依赖真实 DNS：打桩 getaddrinfo）
    real_gai = socket.getaddrinfo

    def fake_internal(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 0))]

    def fake_mixed(host, port, *a, **kw):
        # 先公网、后内网 —— 只查第一个地址的实现会漏掉这条
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 0)),
        ]

    try:
        socket.getaddrinfo = fake_internal
        ok, detail = raises_guard(
            lambda: resolve_and_check("evil.example.com", TargetPolicy()))
        out.append(check("域名解析到内网 → 拦", ok, detail))

        socket.getaddrinfo = fake_mixed
        ok, detail = raises_guard(
            lambda: resolve_and_check("mixed.example.com", TargetPolicy()))
        out.append(check("解析出多个地址，含内网 → 拦（不是只看第一个）",
                         ok, detail))

        socket.getaddrinfo = real_gai
        ok, detail = raises_guard(
            lambda: resolve_and_check("8.8.8.8", TargetPolicy()))
        out.append(check("公网 IP 字面量放行", not ok, detail))
    finally:
        socket.getaddrinfo = real_gai

    return out


CASES = [
    ("必须拦的目标", test_must_block),
    ("必须放行的目标", test_must_allow),
    ("协议白名单", test_scheme_and_url),
    ("策略开关口径", test_policy_switch),
    ("连接层拦截", test_transport_blocks),
    ("护栏装配", test_transport_installs_guard),
    ("解析后判定", test_resolve_then_check),
    ("卡片端点不采信", test_card_declared_endpoint),
    ("_rpc 直连被拦", test_rpc_call_blocked),
]


def main() -> int:
    results: list[tuple[str, bool]] = []
    for name, fn in CASES:
        try:
            results.extend(fn())
        except Exception as exc:                                # noqa: BLE001
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
