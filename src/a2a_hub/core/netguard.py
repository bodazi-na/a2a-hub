# -*- coding: utf-8 -*-
"""定向 SSRF 防护：拦住 hub 向外发起的、指向**本机与内网**的请求。

要解决的问题
------------
hub 对下游 agent 的地址**不是自己决定的**，有两个来源：

  1. `register --endpoint URL` —— 运维填的；
  2. **下游 Agent Card 里的 `supportedInterfaces[].url`** —— 下游自己填的。

第 2 条才是要害。`A2AHttpAdapter._pick_rpc_url()` 会直接采信卡片声明的
JSON-RPC 端点，于是「注册一个恶意/被投毒的 A2A 下游」就等价于
「拿到一条对内网任意端点发 POST 的通道」，而且响应体还会被 `resp.json()`
解析回传。落到云环境里，`169.254.169.254`（链路本地地址）就是实例凭据。
hub 的定位是「本机工具」，不该顺手变成一个内网跳板。

设计取舍
--------
**拦什么：链路本地（含云元数据）+ 私网 + 保留段。**
**放行什么：回环。**

不拦回环是刻意的，也是这套防护与「教科书式 SSRF 防护」最大的分歧 ——
hub 的全部既有用法（`127.0.0.1:9100/9101` 的本地桥、本机 mock）都是回环。
一刀拦掉等于砍掉主场景，最后一定会被人用 `--allow-*` 全局关掉，
反而把真正该拦的也一起放行了。**能长期开着的默认值，比理论上更严的默认值有用。**

不拦「攻击者能直接访问的公网地址」—— SSRF 的额外能力来自**越过网络边界**
（打到用户/运维到不了的内网与控制面）。公网目标没有这个加成，
拦它只会误伤「把 hub 指向远端的另一个 hub」这种正常用法。

**在哪里拦：连接层（`httpcore` 的网络后端），不是只在入口预检。**
只在 register/probe 时检查地址是不够的 —— `A2AHttpAdapter.rpc_url` 是
**先拉卡片、再按卡片内容请求**，请求目标在运行时才确定。护栏包在
`httpcore` 的网络后端上，**每一次 TCP 建连**都从这里过，
于是域名可以在**解析之后**再判定，DNS rebinding（预检解析到公网、
连接时解析到内网）随之失效。

已知局限（写在代码里，不假装没有）
----------------------------------
- **不做域名黑名单**：只判「这次连接实际要拨的地址」，不维护域名名单。
  因此对「解析结果稳定指向公网」的域名一律放行。
- **只允许 http/https**：其它 scheme（file / data / ftp / gopher …）
  在 `check_url` 里直接拒绝 —— 它们本来就不是 A2A 的传输方式。
- **不覆盖所有私有用途网段**：网段表列的是常见项（RFC1918 / CGNAT /
  链路本地 / 保留 / 组播）。表越长误拦可信目标的风险越高，这是刻意的取舍。
- **`GuardedTransport` 依赖 httpx 的内部结构**（`_pool._network_backend`）。
  它变了就必须改 —— 取不到时**直接抛异常，不静默降级**。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

__all__ = [
    "NetworkGuardError",
    "TargetPolicy",
    "is_blocked_ip",
    "is_blocked_host",
    "resolve_and_check",
    "check_url",
    "GuardedTransport",
    "make_client",
    "LABEL_LOOPBACK",
    "LABEL_LINK_LOCAL_V4",
    "LABEL_LINK_LOCAL_V6",
    "LABEL_PRIVATE",
    "LABEL_NON_GLOBAL",
    "LABEL_MULTICAST",
]


class NetworkGuardError(RuntimeError):
    """请求目标被网络护栏拦截。调用方据此区分「目标不合规」与「连不上」。"""


# 显式网段表。**刻意不靠 `is_private` 一个属性下判断。**
#
# 起因是一次实测（Python 3.13）：`is_private` 是个「桶」而不是一个定义 ——
# 它把 RFC1918、回环、链路本地、TEST-NET 文档段、基准测试段（198.18/15）、
# 保留段全混在一起返回 True，而 **`100.64.0.0/10`（CGNAT，运营商级 NAT）
# 反而返回 False**，直接漏过。同一属性下还有 `is_global` 对组播返回 True 的
# 反直觉行为。凡是「一个属性代表好几件不同的事」，就一定会在某个段上出错，
# 而且**不会报错**，只会静默放行。
#
# 所以：想知道「这是不是私网」就写清哪些段算私网。顺序即优先级，
# 报错文案因此也能说准话（早先 `0.0.0.0` 被报成 "private"、`203.0.113.7`
# 也被报成 "private"，全是桶语义外溢造成的）。
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # 100.64.0.0/10（CGNAT，RFC 6598）**必须显式写进来**：实测 `is_private`
    # 对它返回 False，只靠属性判断会静默放行运营商级 NAT 地址。
    # 它虽然常被归到「共享地址」而非严格私网，但对 hub 来说同属「不可信内网」。
    ipaddress.ip_network("100.64.0.0/10"),
)
_NON_GLOBAL_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),         # 「本网络」/ unspecified
    ipaddress.ip_network("192.0.0.0/24"),      # IETF 协议专用
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1（文档用）
    ipaddress.ip_network("198.18.0.0/15"),     # 基准测试保留
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3
    ipaddress.ip_network("240.0.0.0/4"),       # 保留（含 255.255.255.255）
)


# 标签常量：`rejects()` 要按标签决定是否放行，用字符串前缀匹配太脆
LABEL_LOOPBACK = "loopback"
LABEL_LINK_LOCAL_V4 = "link-local（含云元数据服务 169.254.169.254）"
LABEL_LINK_LOCAL_V6 = "link-local（IPv6 fe80::/10）"
LABEL_PRIVATE = "private"
LABEL_NON_GLOBAL = "reserved/non-routable"
LABEL_MULTICAST = "multicast"


def _link_local_reason(ip: Any) -> str:
    return LABEL_LINK_LOCAL_V4 if ip.version == 4 else LABEL_LINK_LOCAL_V6


def _classify(ip: Any) -> str | None:
    """返回一个「拒绝理由」标签；返回 None 表示放行。

    顺序即优先级 —— 越具体的档排越前，标签才不会张冠李戴。
    """
    # IPv4-mapped IPv6（::ffff:169.254.169.254）要先还原成 IPv4 再判定，
    # 否则会漏 —— 这是 SSRF 过滤最常见的绕过方式之一。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.version == 4:
        # 回环放在最前：它同时命中 is_private，但我们想给它一个独立档位
        # （默认放行），所以必须先判。
        if ip in _LOOPBACK_V4:
            return LABEL_LOOPBACK
        if ip in _LINK_LOCAL_V4:
            return LABEL_LINK_LOCAL_V4
        if ip.is_multicast:
            return LABEL_MULTICAST
        if any(ip in n for n in _PRIVATE_V4):
            return LABEL_PRIVATE
        if any(ip in n for n in _NON_GLOBAL_V4):
            return LABEL_NON_GLOBAL
        # 兜底用 ipaddress 自己的判断。放在显式表之后：它只是补漏，
        # 不再承担「定义私网」的职责。
        if ip.is_private or ip.is_reserved or ip.is_unspecified:
            return LABEL_NON_GLOBAL
        return None

    # IPv6
    if ip.is_loopback:
        return LABEL_LOOPBACK
    if ip.is_link_local:
        return LABEL_LINK_LOCAL_V6
    if ip.is_multicast:
        return LABEL_MULTICAST
    if ip.is_private:          # fc00::/7 unique-local
        return LABEL_PRIVATE
    if ip.is_reserved or ip.is_unspecified:
        return LABEL_NON_GLOBAL
    return None



def _strip_brackets(host: str) -> str:
    """剥掉 IPv6 字面量的方括号（URL 里带括号，拨号时不带）。"""
    if len(host) >= 2 and host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_blocked_ip(host: str, *, policy: "TargetPolicy | None" = None) -> str | None:
    """`host` 是点分/冒号形式的 IP 字面量时给出拒绝理由，否则 None。

    非 IP 字面量（域名）返回 None —— 域名要等解析之后再判，
    见 `resolve_and_check`。
    """
    policy = policy or TargetPolicy()
    raw = _strip_brackets(host.strip())
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return policy.rejects(ip)


class TargetPolicy:
    """拦截口径。**默认拦链路本地 / 私网 / 保留段，放行回环。**

    `allow_loopback` 之外只给一个 `allow_private`，是刻意的：每多一个放行旋钮，
    就多一条「配了没生效」的路。真要连内网目标，用 `--allow-local-targets`
    一次性显式承担（它同时打开这两个）。
    """

    def __init__(
        self,
        *,
        allow_loopback: bool = True,
        allow_private: bool = False,
        allow_link_local: bool = False,
        allow_non_global: bool = False,
    ) -> None:
        self.allow_loopback = allow_loopback
        self.allow_private = allow_private
        # 链路本地**没有开关**：云元数据是 SSRF 到「拿到实例凭据」那一步的关键，
        # 没有任何一个说得通的用例需要在 hub 里访问 169.254.0.0/16。
        # 想都别想放宽 —— 需要的话请自己 fork。
        self.allow_link_local = allow_link_local
        self.allow_non_global = allow_non_global

    def rejects(self, ip: Any) -> str | None:
        reason = _classify(ip)
        if reason is None:
            return None
        if reason == "loopback":
            return None if self.allow_loopback else reason
        if reason == "private":
            return None if self.allow_private else reason
        if reason in (LABEL_LINK_LOCAL_V4, LABEL_LINK_LOCAL_V6):
            return None if self.allow_link_local else reason
        if reason == "reserved/non-routable":
            return None if self.allow_non_global else reason
        return reason          # multicast 等：始终拒绝

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"TargetPolicy(allow_loopback={self.allow_loopback}, "
                f"allow_private={self.allow_private})")


def is_blocked_host(host: str, *, policy: "TargetPolicy | None" = None) -> str | None:
    """对**主机名或 IP** 给出拒绝理由；域名一律放行（交由连接层判定）。

    与 `_is_loopback`（启动护栏用）方向相反：那个是「解析不了就当作不安全」，
    这个是「拿不准就放行、把判定推迟到真正建立连接时」—— 因为这里多的是
    域名，现在解析不了不代表连的时候解析不了。
    """
    policy = policy or TargetPolicy()
    raw = _strip_brackets(host.strip())
    if raw.lower() == "localhost":
        # 语义上等价回环，不必等解析
        return None if policy.allow_loopback else "loopback"
    return is_blocked_ip(raw, policy=policy)


def check_url(url: str, *, policy: "TargetPolicy | None" = None) -> None:
    """对完整 URL 做一次**静态**预检；不合规抛 `NetworkGuardError`。

    只覆盖「URL 里写的就是 IP 字面量」这一种情况 —— 写域名的 URL 必须等到
    连接层才能真正判定，所以这个函数是**快速失败 + 更好的报错**，
    不是防护的全部。真正的防护在 `_GuardedNetworkBackend`。

    **三种畸形写法都要能优雅拒绝**，不能让 `urlsplit` 的原始异常漏出去
    （实测踩到）：`urlsplit` 对畸形 IPv6 会抛裸 `ValueError`，而调用方只捕
    `NetworkGuardError` —— 漏成别的异常类型就会被当成「非护栏问题」放过去。
    """
    policy = policy or TargetPolicy()
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
    except ValueError as exc:
        # 如 `http://[fe80::1/`（IPv6 括号不闭合）
        raise NetworkGuardError(f"拒绝请求 {url!r}：URL 无法解析（{exc}）") from None

    if scheme not in ("http", "https"):
        raise NetworkGuardError(
            f"拒绝请求 {url!r}：只允许 http/https，实际是 {scheme or '(空)'}"
        )
    if not host:
        raise NetworkGuardError(f"拒绝请求 {url!r}：URL 里没有主机名")

    # 没有方括号的 IPv6 会被 urlsplit 拆成「主机名 + 端口」——
    # 实测 `http://fe80::1/` 得到 hostname='fe80'、port=1：它既不是合法域名
    # 也不是我们要判的 IP，于是**静态预检直接放行**。这里补一道：
    # 不含点的全十六进制短串（`fe80` / `deadbeef`）几乎不可能是真域名，
    # 而它恰恰是「IPv6 忘了加括号」的典型形态。判成恶意写法而拒绝。
    if host and ":" not in host and "." not in host:
        try:
            int(host, 16)
        except ValueError:
            pass
        else:
            raise NetworkGuardError(
                f"拒绝请求 {url!r}：主机名 {host!r} 像是没加方括号的 IPv6 字面量。\n"
                "      IPv6 地址在 URL 里必须写成 [::1] 这种带方括号的形式。"
            )

    reason = is_blocked_host(host, policy=policy)
    if reason:
        raise NetworkGuardError(
            f"拒绝请求 {url!r}：目标是本机/内网地址（{reason}）。\n"
            "      hub 会驱动本机 agent 执行任务，允许它向内网发请求等于给它一个内网跳板。\n"
            "      若目标确实可信（本机桥接服务 / 自建内网 hub），请显式放宽策略。"
        )


def resolve_and_check(host: str, policy: "TargetPolicy") -> None:
    """把主机名解析成 IP，逐个过护栏；任一个落在受限段就拒绝。

    **为什么必须自己做这一步**（实测才发现）：

    我原以为「连接层的 host 参数就是解析后的 IP」—— **不是**。
    `httpcore` 把域名原样交给后端，解析发生在后端内部（`socket.getaddrinfo`
    之类）。实测：`make_client().get("http://localhost:9/")` 时护栏后端收到的
    `host` 参数是字符串 `'localhost'`，不是 `127.0.0.1`。而
    `is_blocked_host('localhost')` 对域名一律返回 None（判定推迟），
    于是**一个解析到 10.0.0.5 的域名会畅通无阻** —— 正是 DNS rebinding
    那类最需要拦的情形，防护形同虚设。

    所以判定必须显式做「解析 → 对每个结果过护栏」。**常见做法是只查第一个
    地址，这里刻意查全部**：一个域名可以同时解析出公网与内网地址，
    只查第一个等于给攻击者留一个「把内网地址排在后面」的机会。

    维修记录：本函数第一版把「IP 字面量已被拒」写成了 `return`（当成放行），
    等于**把该拦的 IP 字面量静默放过**。当时因为 `check_url()` 的静态预检在更
    早一步已经拦下 IP 字面量，这个错被完全遮住 —— 而连接层这条路（刚为了
    防 rebinding 加的）会直接放行 `10.0.0.5`。**同一事实有两条通路，就要问
    它们会不会不一致**：这里正是「入口拦住了、连接层没拦」。
    """
    literal = _strip_brackets(host.strip())
    if literal:
        try:
            ipaddress.ip_address(literal)
        except ValueError:
            pass            # 不是 IP 字面量 → 走下面的解析判断
        else:
            # 是 IP 字面量：判定结果就是最终结论，没有「解析」这一步。
            # **被拒必须抛**（这正是上面那条维修记录修的地方）。
            reason = policy.rejects(ipaddress.ip_address(literal))
            if reason:
                raise NetworkGuardError(
                    f"网络护栏拦截：拒绝连接 {literal}（{reason}）。"
                    "该目标属于本机/内网地址，hub 只应访问可信的下游 agent。\n"
                    "      若这是可信的内部服务，请用 --allow-local-targets 显式放行。"
                )
            return          # 回环等已放行的字面量

    if not literal:
        return
    try:
        infos = socket.getaddrinfo(literal, None, proto=socket.IPPROTO_TCP)
    except OSError:
        # 解析不了：连都连不上，交给 socket 层去报错。
        # 这里**不**当作拒绝 —— 与 `_is_loopback`（宁可误拒）方向相反，
        # 这个函数服务于「正常请求要能过」，不解析就拒会让 DNS 抖动变成故障。
        return

    for info in infos:
        addr = info[4][0]
        reason = is_blocked_ip(addr, policy=policy)
        if reason:
            raise NetworkGuardError(
                f"网络护栏拦截：{literal} 解析到 {addr}（{reason}）。"
                "域名不应指向本机/内网地址 —— 这类写法常被用来绕过入口检查。\n"
                "      若这是可信的内部服务，请用 --allow-local-targets 显式放行。"
            )


class _GuardedNetworkBackend:
    """包住 `httpcore` 的网络后端，在**真正 TCP 建连**那一刻校验目标。

    为什么落在这一层（踩过一次坑，记下来）：

    最初的写法是继承 `httpx.AsyncHTTPTransport` 并重写 `connect_tcp()`。
    **httpx 0.28 里根本没有这个方法** —— `AsyncHTTPTransport` 只有
    `handle_async_request` / `aclose`，连接是由内部的 `httpcore` 连接池建的。
    那个重写是死代码，被同名方法静默遮住，一点作用都没有。

    它的后果是**一半有效**：IP 字面量的地址在 `check_url()` 静态预检那步就
    被拦了（所以看起来「管用」），而**写域名的地址会溜到真正建连**——
    正是 DNS rebinding 那一类最需要拦的情形。实测证据：`10.0.0.5` 走完整
    client 时报的是 `ConnectTimeout`（说明请求真的发出去了）而不是护栏异常。

    包在后端这层比在 httpx 层更彻底：无论 URL 怎么拼出来的、走不走连接池、
    有没有复用连接，**每一次 TCP 建连**都从这里过。
    """

    def __init__(self, inner: Any, policy: "TargetPolicy") -> None:
        self._inner = inner
        self._policy = policy
        # 惰性构造：真正的后端每个事件循环只能有一个，必须在循环里建。
        self._backend: Any = None

    def _real_backend(self) -> Any:
        if self._backend is None:
            from httpcore._backends.auto import AutoBackend

            self._backend = AutoBackend()
        return self._backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Any = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        # host 可能是 IP 字面量、也可能是**没解析过的域名** ——
        # 后者必须自己解析后逐个判定，见 resolve_and_check 的说明。
        resolve_and_check(host, self._policy)
        return await self._real_backend().connect_tcp(
            host, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        # Unix socket 不走网络，但也不该由「下游指定的地址」触发 —— 原样透传，
        # 交由 httpcore 处理（hub 目前不使用该路径）。
        return await self._real_backend().connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        return await self._real_backend().sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """把受护栏保护的网络后端装进 httpx 的传输层。

    继承 httpx 的 transport 是为了保留它的连接池、超时与代理语义，
    只把 `_pool` 的网络后端换成带护栏的那一个 —— 见
    `_GuardedNetworkBackend` 里记的那次踩坑。
    """

    def __init__(self, *, policy: "TargetPolicy | None" = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.policy = policy or TargetPolicy()
        # `_pool` 是 httpx 的 httpcore 连接池；它的后端决定「怎么拨号」。
        # 这里做一次替换，其余行为（连接复用、TLS、超时）全部不变。
        try:
            self._pool._network_backend = _GuardedNetworkBackend(  # noqa: SLF001
                self._pool._network_backend, self.policy,   # noqa: SLF001
            )
            self._guarded = True
        except AttributeError:          # pragma: no cover - httpx 内部结构变了
            # 内部结构不像预期时**不能装作没事** —— 静默降级等于护栏失效。
            # 这里选择直接抛，宁可起不来也不要「以为有护栏、实际没有」。
            raise NetworkGuardError(
                "无法给 httpx 连接池装上网络护栏（httpx 内部结构可能已变）。"
                "为避免『以为有保护、实际是裸奔』，这里选择直接失败："
                "请核对该 httpx 版本的 AsyncHTTPTransport._pool 结构。"
            ) from None


def make_client(
    *,
    timeout: float | httpx.Timeout,
    policy: "TargetPolicy | None" = None,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """构造一个自带网络护栏的 `AsyncClient`。

    统一在这里设 `trust_env=False`：所有出网请求都必须绕开系统代理，
    否则本机探测会被代理变成 502（把「工具没开」误判成「下游挂了」）。
    """
    transport = GuardedTransport(policy=policy or TargetPolicy())
    return httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        trust_env=False,
        headers=headers,
    )
