# -*- coding: utf-8 -*-
"""CLI 类适配器：把 headless CLI 工具直接包成统一契约。

与 a2a_http 的分工
------------------
- `a2a_http` 对接「已经在跑的 A2A 服务」（本机的两条桥）。
- 本模块**直接起子进程调用 CLI 本体**，少一层桥、少一个常驻进程。

代价是要自己管子进程生命周期、超时、取消，以及各家不统一的输出格式。

实测契约（2026-10-06 本机）
--------------------------
| CLI      | 非交互             | 输出格式                     | 会话续接             |
| claude   | `-p`               | `--output-format stream-json`| `--resume <id>`      |
| qodercli | `-p`               | `-o stream-json`             | `-r <id>`            |
| codex    | `exec`             | `--json`（JSONL）            | `exec resume <id>`   |
| dsh      | `--profile headless`| `--json`（NDJSON）          | `--session-id <id>`  |

解析分两族：
  - **claude 族**（claude / qodercli）：单个 JSON 对象序列，schema 逐字段一致
  - **jsonl 族**（codex / dsh）：逐行事件

公共骨架负责：子进程、超时、**取消时杀进程**、按行容错解析（CLI 会在 stdout
混入非 JSON 的环境提示，实测 Qoder 就有）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .base import (
    EVENT_STATUS,
    EVENT_TEXT,
    EVENT_THINKING,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    HEALTH_DOWN,
    HEALTH_OK,
    Adapter,
    CallResult,
    Event,
    pick_session_id,
    pick_usage,
)

# 进程被 kill 后，等待它真正退出（避免僵尸进程）
KILL_GRACE_SECONDS = 5.0

# asyncio 子进程流单行上限。默认 64KB 对「工具输出塞进一行 JSON」的场景太小，
# 一超就抛异常（A2A-03）。
STREAM_LIMIT = 8 * 1024 * 1024

# argv 里允许出现的字符。
#
# 为什么必须用白名单（P1-8）
# --------------------------
# 本机四个 CLI 里三个是 `.cmd`，Windows 上不能直接 CreateProcess，
# 必须经 `cmd.exe /c`。而 **cmd.exe 会重新解释命令行**。实测（2026-10-06）：
#
#   传入        cmd 解释成        后果
#   a&b         a                 & 把命令切断 —— `a&calc` 会真的执行 calc
#   a|b         （管道）          把 b 当命令跑
#   a>b         （重定向）        输出被吞
#   a^b         ab                ^ 被当转义符吃掉
#   a%PATH%b    展开成 PATH 的值  **加引号也拦不住**
#
# 前四条可以用引号挡住，最后一条不行 —— cmd 的 `%` 展开不受引号约束，
# 而 `%` 在 cmd 命令行上**无法可靠转义**（`^%` 无效，`%%` 只在批处理文件里有效）。
# 所以「转义参数」这条路根本走不通，唯一稳妥的做法是**不让可疑值进入命令行**。
#
# 代价：下游若真的返回带空格的 session id，会被拒绝并明确报错。这是刻意的 ——
# 宁可报错，也不要静默地把命令拆错（静默拆错才是真危险）。
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9._:@/+\-]+$")


class UnsafeArgError(ValueError):
    """argv 里出现了会被 cmd.exe 重新解释的值。"""


# ---------------------------------------------------------------------------
# Windows Job Object
#
# 为什么需要它（P1-7 + P2-21）
# ---------------------------
# 1. `taskkill /PID` 是**按 PID 定位**的。从「检查 returncode」到「执行 taskkill」
#    之间进程若恰好退出，PID 可能已被复用 —— 于是杀掉一个无关进程（TOCTOU）。
# 2. taskkill 的返回码与 stderr 原来全被 DEVNULL 掉，权限不足时**静默漏杀**，
#    孤儿 node 继续烧 token 占 session，而且没有任何信号。
#
# Job Object 用**句柄**定位，没有 PID 复用窗口；`TerminateJobObject` 一次干掉
# 整棵树（包括「直接子进程已退出但孙进程还在」这种情况 —— 这是 taskkill 做不到的）；
# 再配 `KILL_ON_JOB_CLOSE`，连「hub 自己崩了」也能兜住，不会留下孤儿。
#
# 拿不到 job 时（非 Windows、或已被不允许嵌套的受限 job 包住）退回 taskkill，
# 但**必须检查返回码**，不许再静默。
# ---------------------------------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _JobObject:
    """把一个子进程及其后代收进 Job Object，之后用句柄语义整体终止。"""

    __slots__ = ("handle", "_k32")

    def __init__(self, pid: int) -> None:
        self.handle: int | None = None
        self._k32 = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount",
                    "WriteTransferCount", "OtherTransferCount")]

            class _BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),          # ULONG_PTR
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return

            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                k32.CloseHandle(handle)
                return

            # 按 PID 拿子进程句柄 —— 只在这一瞬（刚 spawn 完，进程必然还活着），
            # 之后一律用 job 句柄，不存在 PID 复用问题
            ph = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not ph:
                k32.CloseHandle(handle)
                return
            try:
                if not k32.AssignProcessToJobObject(handle, ph):
                    k32.CloseHandle(handle)
                    return
            finally:
                k32.CloseHandle(ph)

            self.handle = handle
            self._k32 = k32
        except Exception:  # noqa: BLE001
            # 任何失败都退化成「没有 job」—— 调用方会走 taskkill 兜底
            self.handle = None
            self._k32 = None

    @property
    def ok(self) -> bool:
        return self.handle is not None

    def terminate(self) -> bool:
        """终止 job 里**全部**进程。句柄语义，不涉及 PID 复用。"""
        if not self.handle or self._k32 is None:
            return False
        try:
            return bool(self._k32.TerminateJobObject(self.handle, 1))
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """关掉 job 句柄。配 KILL_ON_JOB_CLOSE，残余进程会一起走。"""
        if not self.handle or self._k32 is None:
            self.handle = None
            return
        try:
            self._k32.CloseHandle(self.handle)
        except Exception:  # noqa: BLE001
            pass
        self.handle = None


def _with_kill_problems(message: str, problems: list[str]) -> str:
    """把「终止进程树时的失败」拼进错误文案。

    **静默漏杀是这个适配器最危险的失败模式** —— 调用方以为任务停了，
    实际上孤儿 node 还在烧 token、还占着下游 session（P1-7）。
    宁可文案丑一点，也要让这件事被看见。
    """
    if not problems:
        return message
    return (f"{message}；⚠️ 终止子进程树时出错，可能留下了孤儿进程: "
            + "; ".join(problems))



@dataclass
class CLIOutcome:
    """一次 CLI 运行的解析结果。"""

    ok: bool
    text: str = ""
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class CLIAdapter(Adapter):
    """CLI 适配器骨架。

    子类只需实现三件事：
      - `build_argv(prompt, session_id)`：拼参数
      - `parse_line(obj, state)`：把一个 JSON 对象翻译成事件（可返回 []）
      - `finalize(state, returncode)`：产出最终结果
    """

    kind = "cli"

    def __init__(
        self,
        name: str,
        *,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600.0,
        **options: Any,
    ):
        super().__init__(name, endpoint=None, **options)
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env or {})
        self.default_timeout = timeout

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------

    def build_argv(self, session_id: str | None) -> list[str]:
        """只拼参数，**prompt 不进 argv**。

        prompt 一律走 stdin —— Windows 的 CreateProcess 有 32K 命令行上限，
        长 prompt（含引号/换行）会被截断或报错。归档里的 dsh_runner /
        codex_runner 当初就是为此刻意用 stdin 传 prompt 的，别把这个坑再踩回去。
        """
        raise NotImplementedError

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        raise NotImplementedError

    def finalize(self, state: dict[str, Any], returncode: int) -> CLIOutcome:
        raise NotImplementedError

    def new_state(self) -> dict[str, Any]:
        return {"noise": [], "usage": {}, "session_id": None}

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def detect(self) -> bool:
        """CLI 类：检查可执行文件是否在磁盘上/在 PATH 里。"""
        exe = self.command[0]
        if os.path.isabs(exe):
            return os.path.exists(exe)
        from shutil import which

        return which(exe) is not None

    def _argv(self, *extra: str) -> list[str]:
        """拼出最终 argv。

        Windows 上 `.cmd` / `.bat` 不能直接被 CreateProcess 执行，
        必须用 `cmd.exe /c` 包一层 —— 本机四个 CLI 里有三个是 .cmd 启动器。
        加 `/d` 关掉 AutoRun，免得注册表里的 AutoRun 命令混进输出。

        **正因为要经 cmd.exe，每个由下游决定的值都必须先过白名单** ——
        cmd 会重新解释命令行，而 `%` 展开连引号都拦不住（P1-8，
        细节见 `SAFE_ARG_RE` 的注释）。

        `command[0]` 若是**不带扩展名的裸命令名**（如 `codex`），得靠 PATH 解析
        才知道它其实是 `.cmd`。只按字符串判断会漏掉包裹，CreateProcess 于是
        报「找不到文件 / WinError 193」（P2-14）。
        """
        base = list(self.command)
        if base:
            exe = base[0]
            if not os.path.isabs(exe):
                from shutil import which

                exe = which(exe) or exe
            if exe.lower().endswith((".cmd", ".bat")):
                base = ["cmd.exe", "/d", "/c", exe] + base[1:]
            else:
                base = [exe] + base[1:]

        safe: list[str] = []
        for value in extra:
            text = str(value)
            if not SAFE_ARG_RE.match(text):
                raise UnsafeArgError(
                    "参数含 cmd.exe 会重新解释的字符，已拒绝执行"
                    f"（否则命令可能被拆分或注入）: {text!r}"
                )
            safe.append(text)
        return base + safe

    async def probe(self) -> str:
        """探活 = 跑一次 `--version`。

        超时或取消时必须回收子进程 —— 否则一个挂住的 --version
        会留下常驻的子孙进程（A2A-14）。

        注意 `asyncio.CancelledError` 在 Python 3.8+ 继承自 BaseException
        而**不是** Exception，`except Exception` 捕获不到它 —— 必须显式处理，
        否则取消路径会静默跳过清理。
        """
        proc: asyncio.subprocess.Process | None = None
        job: _JobObject | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv("--version"),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self._env(),
                limit=STREAM_LIMIT,
            )
            # spawn 之后立刻收进 job，越早越好 —— 晚了 cmd.exe 可能已经把
            # node 孙进程拉起来，那个孙进程就漏在 job 外面了
            job = _JobObject(proc.pid)
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            return HEALTH_OK if proc.returncode == 0 else HEALTH_DOWN
        except asyncio.CancelledError:
            if proc is not None:
                await self._kill(proc, job)
            raise
        except Exception:  # noqa: BLE001
            if proc is not None:
                await self._kill(proc, job)
            return HEALTH_DOWN
        finally:
            if job is not None:
                job.close()

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    async def call(
        self,
        prompt: str,
        *,
        context_id: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> CallResult:
        try:
            argv = self._argv(*self.build_argv(session_id or None))
        except UnsafeArgError as exc:
            # 下游给了会被 cmd.exe 重新解释的值 —— 宁可明确失败，
            # 也不要静默地把命令拆错（P1-8）
            return CallResult(ok=False, error=str(exc))

        events: list[Event] = []
        state = self.new_state()
        # 未显式指定时用适配器自己的配置值 —— 不能替调用方填死一个数，
        # 否则 agents/*.json 里配的 timeout 永远不生效（A2A-13）
        effective_timeout = timeout if timeout is not None else self.default_timeout

        # deadline 在 **spawn 之前** 建立（P2-8）：spawn 本身也可能挂住
        # （cmd.exe 冷启动、杀软扫描、网络盘），原来那版要等 spawn 返回才起算，
        # 这段时间完全不受预算约束。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + effective_timeout

        def remaining() -> float:
            return max(deadline - loop.time(), 1.0)

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    # stdin 既用来传 prompt，也提供输入结束信号（写完就 close）。
                    # 不能用 DEVNULL —— 那样既没数据也没有明确的输入边界，
                    # codex / claude 会一直等（实测让 codex 挂死 4 分半直到超时）。
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd,
                    env=self._env(),
                    # 单行事件可能很长（工具输出被塞进一行 JSON），
                    # 默认 64KB 的流限制会直接抛异常。
                    limit=STREAM_LIMIT,
                ),
                timeout=remaining(),
            )
        except asyncio.TimeoutError:
            return CallResult(
                ok=False, events=events, timed_out=True,
                error=f"启动超时：spawn 阶段就用掉了 {effective_timeout:.0f}s 预算",
            )
        except FileNotFoundError:
            return CallResult(ok=False, error=f"可执行文件不存在: {argv[0]}")
        except Exception as exc:  # noqa: BLE001
            return CallResult(ok=False, error=f"启动失败 {type(exc).__name__}: {exc}")

        # spawn 之后立刻收进 job，越早越好 —— 晚了 cmd.exe 可能已经把 node
        # 孙进程拉起来，那个孙进程就漏在 job 外面了（P1-7 / P2-21）
        job = _JobObject(proc.pid)

        try:
            # 写 stdin 也必须在超时保护之内（A2A-02）：
            # 下游若不消费 stdin 且 prompt 超过管道容量，drain() 会永久阻塞，
            # 而这一步原来在 wait_for 之外，超时根本管不到它。
            await asyncio.wait_for(self._feed_stdin(proc, prompt), timeout=remaining())
            await asyncio.wait_for(self._consume(proc, state, events), timeout=remaining())
        except asyncio.TimeoutError:
            problems = await self._kill(proc, job)
            return CallResult(
                ok=False, events=events, timed_out=True,
                error=_with_kill_problems(
                    f"超时 {effective_timeout:.0f}s，已终止子进程树", problems),
            )
        except asyncio.CancelledError:
            # 取消必须把子进程带走，否则它会变成孤儿继续跑
            await self._kill(proc, job)
            raise
        except Exception as exc:  # noqa: BLE001
            # A2A-03：任何异常（含流超限、解析错误）都必须回收子进程，
            # 否则它会带着下游会话继续跑，既烧资源又占 session。
            problems = await self._kill(proc, job)
            return CallResult(
                ok=False, events=events,
                error=_with_kill_problems(f"{type(exc).__name__}: {exc}", problems),
            )
        finally:
            if job is not None:
                job.close()

        stderr_tail = (state.get("stderr_tail") or "").strip()
        outcome = self.finalize(state, proc.returncode or 0)
        if not outcome.ok and not outcome.error:
            outcome.error = stderr_tail or f"退出码 {proc.returncode}"

        return CallResult(
            ok=outcome.ok,
            text=outcome.text,
            session_id=outcome.session_id,
            events=events,
            usage=outcome.usage,
            metadata={**outcome.metadata, "exitCode": proc.returncode},
            error=None if outcome.ok else outcome.error,
        )

    async def _consume(
        self, proc: asyncio.subprocess.Process, state: dict[str, Any], events: list[Event]
    ) -> None:
        """逐行读 stdout；stderr 单独收尾（用于失败诊断）。"""
        assert proc.stdout is not None

        async def drain_stderr() -> None:
            if proc.stderr is None:
                return
            chunks: list[str] = []
            async for raw in proc.stderr:
                chunks.append(raw.decode("utf-8", errors="replace"))
            state["stderr_tail"] = "".join(chunks)[-2000:]

        stderr_task = asyncio.create_task(drain_stderr())
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                obj = self._try_json(line)
                if obj is None:
                    # CLI 会在 stdout 混入非 JSON 提示（Qoder 实测有），
                    # 记下来备查，但不当作事件。
                    state["noise"].append(line)
                    continue
                for event in self.parse_line(obj, state) or []:
                    events.append(event)
            await proc.wait()
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @staticmethod
    def _try_json(line: str) -> dict[str, Any] | None:
        if not line.startswith("{"):
            return None
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    async def _feed_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
        """把 prompt 写进 stdin 然后关闭（= 给子进程一个 EOF）。

        子进程可能在读之前就退出，写入会拿到 BrokenPipe —— 那是正常情况，忽略即可。
        """
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process,
                    job: "_JobObject | None" = None) -> list[str]:
        """杀**整棵进程树**，并**把失败如实报出来**（不再静默漏杀）。

        为什么要杀树：直接 `proc.kill()` 在 Windows 上只杀掉 `cmd.exe /c xxx.cmd`
        的外壳，底下的 node 孙进程（claude / codex / qoder / dsh 本体）会变成
        孤儿继续跑 —— 既烧 token 又占着 session。本机四个 CLI 里三个是 .cmd 启动器。

        三级策略：
          1. **Job Object**（首选）—— 句柄语义，一次干掉整棵树，
             且「直接子进程已退出、孙进程还在」这种情况也能收掉；
             没有「检查 returncode 与终止之间 PID 被复用」的窗口（P2-21）。
          2. **taskkill /T /F**（兜底）—— **必须看返回码**：权限不足时它会失败，
             原来把返回码和 stderr 全 DEVNULL 掉，于是静默漏杀（P1-7）。
          3. `proc.kill()` 最后手段。

        返回**失败说明**的列表（全成功则为空）。调用方把它拼进错误文案 ——
        用户至少能知道「可能留下了孤儿进程」，而不是一无所知。
        """
        problems: list[str] = []

        # 1) Job Object：句柄语义。即使直接子进程已退出也要试 ——
        #    它的后代可能还在 job 里活着，这正是 taskkill 覆盖不到的情况。
        if job is not None:
            if job.terminate():
                return problems
            problems.append("TerminateJobObject 失败，回退 taskkill")

        if proc.returncode is not None:
            return problems

        # 2) taskkill 兜底，检查返回码
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(killer.communicate(), timeout=10.0)
            if killer.returncode != 0:
                detail = (err or out or b"").decode("utf-8", "replace").strip()
                problems.append(
                    f"taskkill 退出码 {killer.returncode}: {detail[:120] or '(无输出)'}"
                )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"taskkill 未能执行: {type(exc).__name__}: {exc}")

        # 3) 最后手段
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            problems.append(f"等待进程退出超过 {KILL_GRACE_SECONDS:.0f}s")

        return problems

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.env)
        return env


# ---------------------------------------------------------------------------
# claude 族：claude / qodercli —— 输出 schema 逐字段一致，只差参数拼法
# ---------------------------------------------------------------------------


def _content_text(content: Any) -> str:
    """把 tool_result 的 content 统一抽成纯文本。

    content 可能是 str，也可能是 `[{"type":"text","text":"..."}]` 这样的块数组，
    直接 str() 会得到 Python repr（带花括号和引号），对下游阅读毫无意义。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("content") or ""))
            else:
                chunks.append(str(item))
        return "".join(chunks)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


class ClaudeStyleCLI(CLIAdapter):
    """claude / qodercli 共用实现。

    实测两者的 `-o json` 输出结构完全相同：
      {"type":"result","subtype":...,"is_error":...,"result":"...",
       "session_id":"...","num_turns":N,"total_cost_usd":...,"usage":{...}}
    差别只有：claude 用 `--resume`，qodercli 用 `-r`。
    """

    resume_flag = "--resume"
    print_flag = "-p"
    extra_flags: list[str] = []

    def build_argv(self, session_id: str | None) -> list[str]:
        argv = [self.print_flag, *self.extra_flags, "--output-format", "stream-json"]
        if session_id:
            argv += [self.resume_flag, session_id]
        return argv

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")

        if kind == "system":
            if obj.get("session_id"):
                state["session_id"] = obj["session_id"]
            return []

        if kind == "assistant":
            out: list[Event] = []
            for block in (obj.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    text = str(block.get("thinking") or "")
                    if text:
                        out.append(Event(kind=EVENT_THINKING, text=text[:400]))
                elif btype == "tool_use":
                    name = str(block.get("name") or "tool")
                    out.append(Event(
                        kind=EVENT_TOOL_CALL, text=f"$ {name}",
                        metadata={"tool": name, "status": "started"},
                    ))
                elif btype == "text":
                    text = str(block.get("text") or "")
                    if text:
                        out.append(Event(kind=EVENT_TEXT, text=text))
            return out

        if kind == "user":
            out = []
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(Event(
                        kind=EVENT_TOOL_RESULT,
                        text=_content_text(block.get("content"))[:400],
                        metadata={"status": "completed"},
                    ))
            return out

        if kind == "result":
            state["result"] = obj
            return []

        return []

    def finalize(self, state: dict[str, Any], returncode: int) -> CLIOutcome:
        result = state.get("result") or {}
        ok = returncode == 0 and not result.get("is_error", False)

        error = None
        if not ok:
            errors = result.get("errors")
            if isinstance(errors, list) and errors:
                error = str(errors[0])
            else:
                error = str(result.get("result") or "") or None

        return CLIOutcome(
            ok=ok,
            text=str(result.get("result") or ""),
            session_id=str(result.get("session_id") or state.get("session_id") or "") or None,
            usage=pick_usage(result),
            metadata={
                "subtype": result.get("subtype"),
                "numTurns": result.get("num_turns"),
                "totalCostUsd": result.get("total_cost_usd"),
                "stopReason": result.get("stop_reason"),
                "errorCode": result.get("error_code"),
                "noiseLines": len(state.get("noise") or []),
            },
            error=error,
        )


class ClaudeCLI(ClaudeStyleCLI):
    name = "claude"
    # claude 的硬性约束：--print 配 --output-format=stream-json 必须带 --verbose，
    # 否则直接报错退出。qodercli 用同一套输出 schema，但没有这个要求。
    extra_flags = ["--verbose"]


class QoderCLI(ClaudeStyleCLI):
    """qodercli。

    注意：`-m` 传 BYOK 自定义模型必须传 **modelID（UUID）**，传显示名会被
    静默回退到内置模型（走 Qoder 额度，额度耗尽即 error_code 118）。
    所以 model 由构造参数显式传入，不在这里做猜测。
    """

    resume_flag = "-r"

    def __init__(self, name: str = "qodercli", *, model: str | None = None, **kwargs: Any):
        self.model = model
        super().__init__(name, **kwargs)

    def build_argv(self, session_id: str | None) -> list[str]:
        argv = super().build_argv(session_id)
        if self.model:
            argv = argv[:1] + ["-m", self.model] + argv[1:]
        return argv


# ---------------------------------------------------------------------------
# jsonl 族：dsh —— 逐行 NDJSON
# ---------------------------------------------------------------------------


class DshCLI(CLIAdapter):
    """`dsh --profile headless --json`。

    实测事件序列：
      {"type":"session","sessionId":"..."}
      {"type":"status","phase":"turn_start"|"step_end"|"turn_end",...}
      {"type":"thinking","text":"..."}
      {"type":"text","text":"..."}
      {"type":"tool_call","name":"...","input":{...}}
      {"type":"tool_result","name":"...","status":"ok","output":"..."}
      {"type":"final","text":"..."}          ← 最终答复
    用量在 `status.phase == "step_end"` 的 `usage` 字段里。
    """

    def build_argv(self, session_id: str | None) -> list[str]:
        argv = ["--profile", "headless", "--json"]
        if session_id:
            argv += ["--session-id", session_id]
        return argv

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")

        if kind == "session":
            state["session_id"] = obj.get("sessionId")
            return []

        if kind == "status":
            phase = str(obj.get("phase") or "")
            if phase == "step_end" and obj.get("usage"):
                state["usage"] = obj["usage"]
            if phase == "turn_end":
                reason = obj.get("reason") or {}
                state["turn_end_reason"] = reason.get("kind")
            return [Event(
                kind=EVENT_STATUS,
                text=f"{phase} turn={obj.get('turn')}",
                metadata={"phase": phase},
            )]

        if kind == "thinking":
            text = str(obj.get("text") or "")
            return [Event(kind=EVENT_THINKING, text=text[:400])] if text else []

        if kind == "text":
            text = str(obj.get("text") or "")
            return [Event(kind=EVENT_TEXT, text=text)] if text else []

        if kind == "tool_call":
            name = str(obj.get("name") or "tool")
            return [Event(kind=EVENT_TOOL_CALL, text=f"$ {name}",
                          metadata={"tool": name, "status": "started"})]

        if kind == "tool_result":
            name = str(obj.get("name") or "tool")
            return [Event(
                kind=EVENT_TOOL_RESULT,
                text=str(obj.get("output") or "")[:400],
                metadata={"tool": name, "status": obj.get("status")},
            )]

        if kind == "final":
            state["final"] = str(obj.get("text") or "")
            return []

        return []

    def finalize(self, state: dict[str, Any], returncode: int) -> CLIOutcome:
        reason = state.get("turn_end_reason")
        ok = returncode == 0 and reason in (None, "completed")
        error = None
        if not ok:
            error = f"turn_end reason={reason}" if reason else f"退出码 {returncode}"
        return CLIOutcome(
            ok=ok,
            text=state.get("final") or "",
            session_id=state.get("session_id"),
            usage=state.get("usage") or {},
            metadata={
                "turnEndReason": reason,
                "noiseLines": len(state.get("noise") or []),
            },
            error=error,
        )


# ---------------------------------------------------------------------------
# jsonl 族：codex —— 逐行 JSONL（`codex exec --json`）
# ---------------------------------------------------------------------------


class CodexCLI(CLIAdapter):
    """`codex exec --json`。

    事件 schema 与 claude 族不同，这里按 type 做**容错分派**：
    已知的映射成统一事件，未知的原样忽略（不报错），
    这样 Codex 升级事件类型时适配器不会直接崩。
    """

    def __init__(self, name: str = "codex", *, model: str | None = None,
                 skip_git_check: bool = True, **kwargs: Any):
        self.model = model
        self.skip_git_check = skip_git_check
        super().__init__(name, **kwargs)

    def build_argv(self, session_id: str | None) -> list[str]:
        argv = ["exec", "--json"]
        if self.skip_git_check:
            argv.append("--skip-git-repo-check")
        if self.model:
            argv += ["-m", self.model]
        if session_id:
            # 续接是子命令形态：codex exec resume <id>（prompt 走 stdin）
            argv += ["resume", session_id]
        return argv

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        """`codex exec --json` 是「外层 type + 内层 item.type」两层结构。

        实测原始输出（2026-10-06）：
          {"type":"thread.started","thread_id":"01a10f16-..."}
          {"type":"turn.started"}
          {"type":"item.completed","item":{"type":"agent_message","text":"CX-OK"}}
          {"type":"turn.completed","usage":{"input_tokens":13998,...}}
        """
        kind = str(obj.get("type") or "")

        if kind == "thread.started":
            state["session_id"] = obj.get("thread_id")
            return []

        if kind == "turn.started":
            return [Event(kind=EVENT_STATUS, text="turn started", metadata={"phase": "turn_start"})]

        if kind == "turn.completed":
            if isinstance(obj.get("usage"), dict):
                state["usage"] = obj["usage"]
            state["turn_completed"] = True
            return [Event(kind=EVENT_STATUS, text="turn completed", metadata={"phase": "turn_end"})]

        if kind in ("turn.failed", "thread.failed"):
            state["cli_error"] = str(obj.get("error") or obj.get("message") or "turn failed")
            return [Event(kind=EVENT_STATUS, text="turn failed", metadata={"phase": "turn_failed"})]

        if kind.startswith("item."):
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            itype = str(item.get("type") or "")
            started = kind.endswith("started")

            if itype == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    state["final"] = text
                    return [Event(kind=EVENT_TEXT, text=text)]
                return []

            if itype in ("reasoning", "agent_reasoning"):
                text = str(item.get("text") or "")
                return [Event(kind=EVENT_THINKING, text=text[:400])] if text else []

            if itype in ("command_execution", "exec_command", "tool_call", "function_call"):
                name = str(item.get("command") or item.get("name") or "command")
                if started:
                    return [Event(kind=EVENT_TOOL_CALL, text=f"$ {name}",
                                  metadata={"tool": name, "status": "started"})]
                output = item.get("aggregated_output") or item.get("output") or ""
                return [Event(kind=EVENT_TOOL_RESULT, text=str(output)[:400],
                              metadata={"tool": name, "status": item.get("status") or "completed"})]

            if itype == "error":
                message = str(item.get("message") or "")
                if message:
                    # Codex 会把「配置项被忽略」这类提示也塞成 error item，
                    # 不该当作任务失败 —— 收进 warnings 备查即可。
                    state.setdefault("warnings", []).append(message)
                return []

            return []

        return []

    def finalize(self, state: dict[str, Any], returncode: int) -> CLIOutcome:
        ok = (
            returncode == 0
            and not state.get("cli_error")
            and state.get("turn_completed", False)
        )
        error = None
        if not ok:
            error = state.get("cli_error") or f"退出码 {returncode}"
        return CLIOutcome(
            ok=ok,
            text=state.get("final") or "",
            session_id=state.get("session_id"),
            usage=state.get("usage") or {},
            metadata={
                "noiseLines": len(state.get("noise") or []),
                # Codex 会把「配置项被忽略」之类的提示塞成 error item，
                # 收在这里备查，不当作任务失败。
                "warnings": (state.get("warnings") or [])[:3],
                "turnCompleted": bool(state.get("turn_completed")),
            },
            error=error,
        )
