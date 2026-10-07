#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""a2a-hub 命令行入口。

用法
----
  a2a-hub serve    [--host 127.0.0.1] [--port 9200]
  a2a-hub register --name NAME --endpoint URL [--tag TAG]... [--kind a2a_http]
  a2a-hub agents
  a2a-hub tasks    [--limit 20]
  a2a-hub probe    [--name NAME]

装过包之后用 `a2a-hub`；不想装包也可以用 `python -m a2a_hub`（两者等价，
且都不依赖你在哪个目录）。源码仓库里还留了个 `python hub.py` 的转发入口。

全局参数 --db 指定 SQLite 路径（默认 <仓库根>/data/hub.db），要放在子命令之前：
  a2a-hub --db data/dev.db serve
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """运行时数据（`data/` `workspace/` `agents/`）的根目录。

    **冻结后不能用 `__file__`**：PyInstaller 会把代码放进打包内部目录，
    而 onefile 模式下那是个临时目录、**退出即删**。数据库放那里等于每次
    重启都从零开始，而且退出时**静默**丢失 —— 不会有任何报错。

    所以冻结时用 exe 自己所在的目录。这也正好是「绿色免安装」该有的语义：
    exe 拷到哪，数据就跟到哪。

    未冻结时按「**仓库根**」算。注意别写成 `__file__.parent`：本文件在
    `src/a2a_hub/` 下，那样会得到 `src/a2a_hub/`，数据就落到包目录里去了。
    往上两级才是仓库根（`src/a2a_hub/cli.py` → `src/a2a_hub` → `src` → 仓库根）。
    装成 wheel 之后这个路径只是「当前工作目录之外的落点」，不重要；
    真正重要的是**源码仓库里跑**时数据落在仓库根，与文档一致。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


ROOT = _base_dir()

from a2a_hub.adapters.a2a_http import A2AHttpAdapter   # noqa: E402
from a2a_hub.core.hub_app import Hub                   # noqa: E402
from a2a_hub.core.registry import AgentRecord, Registry  # noqa: E402
from a2a_hub.core.router import Router                 # noqa: E402
from a2a_hub.core.store import Store                   # noqa: E402

DEFAULT_DB = ROOT / "data" / "hub.db"


def _configure_stdio() -> None:
    """把 stdout / stderr 固定成 UTF-8，并让 stdout **行缓冲**。

    编码：Python 在 Windows 上**重定向**输出时用系统代码页（中文 Windows 是
    GBK），于是日志里的中文变乱码。实测冻结成 exe 后 `(来自 Agent Card)` 输出成
    `(\\xc0\\xb4\\xd7\\xd4 Agent Card)`。源码运行时通常踩不到 —— 开发环境一般设了
    `PYTHONUTF8=1` / `PYTHONIOENCODING`，而**冻结后的 exe 不认这两个变量**。
    所以「我这儿跑着好好的」不能说明用户双击时也正常。

    缓冲：**重定向时 stdout 是块缓冲的**（4~8KB 才刷一次），于是启动横幅
    ——**包括那条「服务裸露在网络上」的安全警告**——在日志里一行都看不到，
    直到缓冲区满或进程退出。实测：`serve --allow-insecure` 重定向到管道时
    `[hub]` 输出 **0 行**，加 `PYTHONUNBUFFERED=1` 才有 6 行。

    这个坑对安全提示是致命的：护栏文案里写着「--allow-insecure 确认要裸奔
    （**会在日志里留永久警告**）」，而那条警告恰恰是最需要落进日志的一条。
    行缓冲让它逐行落盘，代价可以忽略（CLI 输出量本来就小）。

    固定成 UTF-8 而不是跟随系统代码页：这台工具的 HTTP / JSON 本来就是 UTF-8，
    日志跟它们保持一致比跟随一个历史代码页更有用。接真实控制台时 Windows 走的是
    `WriteConsoleW`，本来就按 Unicode 处理，不受影响。`errors="replace"`
    保证遇到不可编码字符时也不会抛异常把 CLI 弄挂。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass          # 被重定向到不支持 reconfigure 的对象，跳过即可


def build_hub(db_path: Path, *, auth_token: str | None = None) -> Hub:
    """从持久化的注册表重建整个 hub —— 这正是「重启后一切还在」的落点。"""
    store = Store(db_path)
    registry = Registry(store)
    router = Router(registry)
    adapters = {}
    for rec in registry.list(enabled_only=False):
        if rec.kind == "a2a_http" and rec.endpoint:
            adapters[rec.name] = A2AHttpAdapter(rec.name, rec.endpoint)
    # 下游 agent 的工作目录**必须显式传**，不能用 Hub 的默认值（`Path.cwd()`）：
    # 双击 exe 时 cwd 是「当时碰巧在哪」，可能是桌面、也可能是 C:\Windows\System32，
    # 于是 workspace/ 就散落在那种地方。这里用 ROOT —— 它已经做了 frozen 判断，
    # 源码运行时是仓库根、打包后是 exe 所在目录，两种形态行为一致。
    hub = Hub(store, registry, router, adapters, auth_token=auth_token,
              workspace_dir=str(ROOT / "workspace"))
    # 启动时结算上次进程遗留的活跃任务，别让它们永远卡在 working（A2A-09）
    orphans = hub.recover_orphans()
    if orphans:
        print(f"[hub] 已结算 {orphans} 个失去 worker 的孤儿任务（标记为 interrupted）")
    return hub


def _is_loopback(host: str) -> bool:
    """判断绑定地址是否只有本机能访问。主机名等一律按「非回环」处理——往安全方向错。"""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    token = args.token or os.environ.get("HUB_TOKEN") or None

    # H1 护栏：hub 的能力 = 驱动 CLI agent 在本机干活，暴露到非回环网络
    # 又不配认证，等于把任意代码执行开放给同网段。宁可拒绝启动也不静默裸奔。
    if not _is_loopback(args.host) and not token and not args.allow_insecure:
        print(
            "[hub] 拒绝启动：绑定到非回环地址（"
            f"{args.host}）但未启用认证。\n"
            "      hub 能驱动本机 CLI agent 执行任务——裸奔到网络上等于开放远程代码执行。\n"
            "      三选一：① --token <值> 或环境变量 HUB_TOKEN 启用 Bearer 认证；\n"
            "              ② 改回 --host 127.0.0.1 仅本机使用；\n"
            "              ③ --allow-insecure 确认要裸奔（会在日志里留永久警告）。"
        )
        raise SystemExit(2)

    hub = build_hub(Path(args.db), auth_token=token)
    names = [a.name for a in hub.registry.list(enabled_only=False)]
    print(f"[hub] db      = {args.db}")
    print(f"[hub] agents  = {names or '(空)'}")
    print(f"[hub] auth    = {'Bearer 已启用' if token else '关闭（仅本机使用）'}")
    if not _is_loopback(args.host) and not token:
        print(f"[hub] !! 警告：服务暴露在 {args.host} 上且未启用认证（--allow-insecure 豁免）——"
              "同网段任何人都能派发任务")
    print(f"[hub] card    = http://{args.host}:{args.port}/.well-known/agent-card.json")
    print(f"[hub] console = http://{args.host}:{args.port}/console")
    uvicorn.run(hub.build_app(), host=args.host, port=args.port, log_level="info")


def cmd_register(args: argparse.Namespace) -> None:
    hub = build_hub(Path(args.db))

    # CLI 类的配置（命令 / 工作目录 / 模型 / 环境变量）从 --config 读
    config: dict = {}
    if args.config:
        raw = args.config
        if raw.lstrip().startswith("{"):
            config = json.loads(raw)
        else:
            with open(raw, encoding="utf-8") as fh:
                config = json.load(fh)

    # 主动拉 Agent Card：下游自己声明的 skills 要能直接参与路由，
    # 否则就只能靠人工 --tag 补，等于把下游的能力表丢掉。
    card: dict = {}
    if args.kind == "a2a_http":
        if not args.endpoint:
            raise SystemExit("a2a_http 类型必须给 --endpoint")
        try:
            adapter = A2AHttpAdapter(args.name, args.endpoint)
            card = asyncio.run(adapter.get_card())
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 拉取 Agent Card 失败：{type(exc).__name__}: {exc}")
            print("    卡片留空，路由暂时只能靠 --tag；下游起来后跑 `hub.py probe` 刷新。")

    rec = hub.registry.register(AgentRecord(
        name=args.name,
        kind=args.kind,
        endpoint=args.endpoint,
        card=card,
        tags=args.tag or [],
        config=config,
        enabled=True,
    ))
    target = rec.endpoint or (rec.config.get("adapter") if rec.config else "-")
    print(f"registered: {rec.name} [{rec.kind}] {target}")
    print(f"  tags   : {','.join(rec.tags) or '-'}")
    print(f"  skills : {','.join(rec.skills) or '-'}   (来自 Agent Card)")


def cmd_unregister(args: argparse.Namespace) -> None:
    hub = build_hub(Path(args.db))
    hub.registry.unregister(args.name)
    print(f"unregistered: {args.name}")


def cmd_agents(args: argparse.Namespace) -> None:
    hub = build_hub(Path(args.db))
    rows = hub.router.explain()
    if not rows:
        print("(注册表为空)")
        return
    for r in rows:
        print(f"  {r['name']:<16} {r['kind']:<10} health={r['health']:<8} "
              f"tags={','.join(r['tags']) or '-'}")


def cmd_tasks(args: argparse.Namespace) -> None:
    hub = build_hub(Path(args.db))
    tasks = hub.store.list_tasks(limit=args.limit)
    if not tasks:
        print("(没有任务)")
        return
    for t in tasks:
        prompt = (t["prompt"] or "").replace("\n", " ")[:44]
        print(f"  {t['id']}  {t['state']:<10} {str(t['agent'] or '-'):<12} {prompt}")


def cmd_probe(args: argparse.Namespace) -> None:
    hub = build_hub(Path(args.db))

    async def run() -> None:
        # 走 Hub 而不是 Registry：CLI 类 agent 没有 endpoint，
        # 只能由适配器自己的 probe（跑 --version）来判定健康。
        if args.name:
            print(f"{args.name}: {await hub.probe_agent(args.name)}")
            return
        results = await hub.probe_all()
        if not results:
            print("(注册表为空)")
        for name, health in results.items():
            print(f"  {name:<18} {health}")

    asyncio.run(run())


def main() -> None:
    # 在**任何输出之前**固定编码与缓冲 —— 见 _configure_stdio 的说明。
    # 放在 main 而不是模块顶层：这样被当库 import 时不会去动调用方的 stdout。
    _configure_stdio()

    # prog 固定成发行名。不写的话 argparse 会拿 sys.argv[0] 当名字 ——
    # 同一个命令可能显示成 `hub`（源码 shim）、`a2a-hub`（控制台脚本）
    # 或 `python.exe`（-m 形式），帮助信息里对不上号。
    ap = argparse.ArgumentParser(prog="a2a-hub")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="启动 hub 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9200)
    p.add_argument("--token", help="启用 Bearer 认证（也可用环境变量 HUB_TOKEN）")
    p.add_argument("--allow-insecure", action="store_true",
                   help="显式豁免「非回环绑定必须配认证」的启动护栏")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("register", help="注册下游 agent")
    p.add_argument("--name", required=True)
    p.add_argument("--endpoint", help="HTTP 类必填；CLI 类不用给")
    p.add_argument("--kind", default="a2a_http", choices=["a2a_http", "cli"])
    p.add_argument("--tag", action="append", help="能力标签，可重复")
    p.add_argument("--config", help="CLI 类配置：JSON 字符串，或 .json 文件路径")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("unregister", help="移除下游 agent")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_unregister)

    p = sub.add_parser("agents", help="列出注册表")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("tasks", help="列出历史任务")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("probe", help="探测 agent 健康状态")
    p.add_argument("--name")
    p.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
