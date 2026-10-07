# -*- coding: utf-8 -*-
"""
AI 工具进程检测程序
监测 Codex / WorkBuddy / WorkBuddy-AI / Qoder / DeepSeek Harness /
     Claude Desktop / Claude Code 是否启动
零依赖（仅标准库），Windows 下通过 tasklist 枚举进程
Claude Code 为 node 进程，通过命令行关键字识别（WMI 查询）
"""
import subprocess
import sys
import time
from datetime import datetime

# 目标进程配置：显示名称 -> (匹配方式, 匹配值列表)
#   "exe"     精确匹配映像名（小写）
#   "cmdline" 命令行包含任一关键字（用于 node 类 CLI 工具）
TARGETS = {
    "Codex":            ("exe", ["codex.exe"]),
    "WorkBuddy":        ("exe", ["workbuddy.exe"]),
    "WorkBuddy-AI":     ("exe", ["workbuddyai.exe"]),
    "Qoder":            ("exe", ["qoder.exe"]),
    "DeepSeek Harness": ("exe", ["deepseek harness.exe"]),
    "Claude Desktop":   ("exe", ["claude.exe"]),
    "Claude Code":      ("cmdline", ["claude-code"]),
}


def get_processes():
    """返回 [(映像名, PID, 内存KB), ...]，使用 tasklist CSV 输出解析。"""
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    ).stdout
    procs = []
    for line in out.strip().splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 5:
            name = parts[0].strip('"')
            pid = parts[1].strip('"')
            mem = "".join(ch for ch in parts[4] if ch.isdigit())  # 中文系统格式为 "12,345 K"
            procs.append((name, pid, mem))
    return procs


def get_cmdline_procs():
    """返回 [(映像名, PID, 命令行), ...]，通过 WMI 查询（用于识别 node 类 CLI 进程）。"""
    ps = ("Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,Name,CommandLine | "
          "ConvertTo-Json -Compress")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    ).stdout.strip()
    if not out:
        return []
    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(p.get("Name") or "", str(p.get("ProcessId") or ""),
             (p.get("CommandLine") or "")) for p in data]


def match_target(proc_name, exe_names):
    """精确匹配映像名（不区分大小写），避免 workbuddy 误命中 workbuddyai。"""
    return proc_name.lower() in exe_names


def check_once():
    procs = get_processes()
    need_cmdline = any(m == "cmdline" for m, _ in TARGETS.values())
    cmd_procs = get_cmdline_procs() if need_cmdline else []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n===== 进程检测报告  {now} =====")
    running, stopped = 0, 0
    for display, (mode, patterns) in TARGETS.items():
        if mode == "exe":
            hits = [(n, p, m) for n, p, m in procs if match_target(n, patterns)]
        else:  # cmdline：按命令行关键字匹配，内存从 tasklist 结果中按 PID 补齐
            mem_by_pid = {p: m for _, p, m in procs}
            hits = []
            for n, pid, cmd in cmd_procs:
                cmd_l = cmd.lower()
                # 排除自身检测命令，避免误报
                if "get-ciminstance" in cmd_l or "convertto-json" in cmd_l:
                    continue
                if any(k in cmd_l for k in patterns):
                    hits.append((n, pid, mem_by_pid.get(pid, "")))
        if hits:
            running += 1
            print(f"[运行中] {display}")
            for n, p, m in hits[:5]:
                mem_mb = int(m) / 1024 if m.isdigit() else 0
                print(f"    - {n:<40} PID={p:<8} 内存={mem_mb:.1f} MB")
            if len(hits) > 5:
                print(f"    ... 另有 {len(hits) - 5} 个相关进程")
        else:
            stopped += 1
            print(f"[未启动] {display}")
    print(f"-------------------------------------")
    print(f"合计：运行中 {running} 个，未启动 {stopped} 个")
    print("说明：WorkBuddy 与 WorkBuddy-AI 为两个独立程序，"
          "分别对应 WorkBuddy.exe 与 WorkBuddyAI.exe，已精确区分。")
    print("      Claude Code 为 node CLI 进程，按命令行关键字 claude-code 识别。")


def main():
    watch = "--watch" in sys.argv
    interval = 5
    for i, a in enumerate(sys.argv):
        if a == "--interval" and i + 1 < len(sys.argv):
            interval = float(sys.argv[i + 1])
    if not watch:
        check_once()
        return
    print(f"持续监测模式，每 {interval} 秒刷新，Ctrl+C 退出")
    try:
        while True:
            check_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已退出监测。")


if __name__ == "__main__":
    main()
