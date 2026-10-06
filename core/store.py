# -*- coding: utf-8 -*-
"""持久化层：SQLite 四表 + schema 迁移。

这是协调内核的地基。它要保证的核心事实只有一条：

    进程重启后，重启前的任务仍能用 get_task() 查到。

表设计
------
tasks      任务主表（A2A Task 的持久化投影）
messages   任务下的消息序列（含 statusUpdate 历史、工具调用过程回传）
artifacts  任务产物（最终答复等）
contexts   contextId 到下游 agent session 的映射（跨轮续接）

约定
----
- 时间戳统一 UTC ISO8601，精确到微秒（避免同秒排序歧义）
- 主键统一 UUID4 字符串
- JSON 字段以 TEXT 存储，读写时序列化/反序列化
- 开启 WAL 与 foreign_keys
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 2

# 序号冲突时的退避基数。**必须小**：调用方全在事件循环线程上，
# 这里的 `time.sleep` 是硬阻塞整个服务的（M2）。
# 5 次重试的退避总量 = 0.01+0.02+0.03+0.04 = 100ms。
RETRY_BACKOFF_BASE = 0.01

TASK_STATES = ("submitted", "working", "completed", "failed", "canceled")

# 消息的 kind 取值，用于把下游过程事件映射成统一语义
MESSAGE_KINDS = (
    "text",          # 纯文本
    "status",        # 状态更新（如 turn started）
    "thinking",      # 思考片段
    "tool_call",     # 工具调用
    "tool_result",   # 工具结果
    "error",         # 错误
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    # `default=str` 是兜底：下游传来的 metadata 里可能出现 datetime / 自定义对象。
    # 没有它，json.dumps 抛 TypeError，而 add_message 的 except 只认 sqlite3 的
    # 两个异常 —— 异常会带着**未提交的事务**冒泡出去（M1）。
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    trace_id    TEXT,
    context_id  TEXT NOT NULL,
    agent       TEXT,
    state       TEXT NOT NULL,
    prompt      TEXT,
    plan_id     TEXT,
    step_id     TEXT,
    parent_id   TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_context  ON tasks(context_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state    ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_created  ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    kind        TEXT,
    content     TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    name        TEXT,
    content     TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);

CREATE TABLE IF NOT EXISTS contexts (
    context_id  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    session_id  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (context_id, agent)
);

CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    endpoint    TEXT,
    card        TEXT,
    tags        TEXT,
    config      TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    health      TEXT NOT NULL DEFAULT 'unknown',
    last_seen   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_enabled ON agents(enabled, health);
"""


class Store:
    """SQLite 持久化层。

    单个实例对应一条 sqlite3 连接。SQLite 连接不跨线程共享，
    多线程场景请各自构造实例（WAL 模式下并发读没问题）。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # synchronous=NORMAL：**这是 WAL 模式下的推荐值**，也是本层的性能关键。
        #
        # 默认的 FULL 让每次 COMMIT 都 fsync —— 实测单次 add_message 要 4.57ms。
        # 而 `_execute` 会把一个任务的过程事件**逐条**落库，50 个事件就是
        # 262ms 的**事件循环硬阻塞**（这期间连 CancelTask 都调度不了，M2）。
        #
        # 语义差别（SQLite 官方文档明确说明）：
        #   - WAL + NORMAL **不会因进程崩溃而损坏或丢已提交数据**；
        #   - 只有**操作系统崩溃 / 断电**才可能丢掉最近若干个事务。
        # 对「任务协调日志」这个用途，这个取舍是划算的：
        # 进程被杀（最常见的崩溃形态）不丢数据，而断电场景由 recover_orphans
        # 在启动时把活跃任务结算成 failed —— 不会出现「状态卡在 working」。
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self._migrate()
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        if current < SCHEMA_VERSION:
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utcnow()),
            )

    def _migrate(self) -> None:
        """对已存在的库补列。

        SQLite 的 ADD COLUMN 是 O(1) 的元数据操作，不需要重建表，
        所以老库升级只补缺的列即可。
        """
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        if "config" not in cols:
            self._conn.execute("ALTER TABLE agents ADD COLUMN config TEXT")

        task_cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        # 编排相关列：plan_id / step_id / parent_id 让「一个 plan 下所有 step」
        # 和「step 之间的父子关系」都能直接查出来
        for col in ("plan_id", "step_id", "parent_id", "trace_id"):
            if col not in task_cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        # started_at：**真正开始执行**的时刻，与 created_at（记录诞生）分开。
        # 编排层会先为每个 step 落占位任务（A2A-07），若拿 created_at 当开始时刻，
        # 层间排队等待会被算进执行时长 —— 实测 merge 步虚高 2.5 倍（N2）。
        # 老库回填 created_at 作近似：对非编排任务二者本来就几乎相同；
        # 历史编排任务的时长仍是旧的虚高值，无法追溯还原。
        if "started_at" not in task_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")
            terminal = tuple(s for s in TASK_STATES if s not in ("submitted",))
            self._conn.execute(
                f"UPDATE tasks SET started_at = created_at "
                f"WHERE started_at IS NULL AND state IN ({','.join('?' * len(terminal))})",
                terminal,
            )
        # 索引必须等列补好之后再建 —— 老库上 executescript 阶段还没有这些列
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_plan ON tasks(plan_id, step_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_trace ON tasks(trace_id, created_at)"
        )
        # 消息序号的唯一约束（A2A-18）：靠它给并发插入兜底。
        # 老库若已存在重复 seq，建唯一索引会失败 —— 那属于历史数据问题，
        # 不该让服务起不来，所以降级为普通索引并保留提示。
        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_task_seq "
                "ON messages(task_id, seq)"
            )
        except sqlite3.IntegrityError:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_task_seq_plain "
                "ON messages(task_id, seq)"
            )

    @property
    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        context_id: str,
        prompt: str | None = None,
        agent: str | None = None,
        state: str = "submitted",
        task_id: str | None = None,
        trace_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = task_id or new_id()
        now = utcnow()
        self._conn.execute(
            """
            INSERT INTO tasks(id, trace_id, context_id, agent, state, prompt,
                              plan_id, step_id, parent_id,
                              created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, trace_id, context_id, agent, state, prompt,
             plan_id, step_id, parent_id, now, now, _dumps(metadata)),
        )
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._task_row_to_dict(row) if row else None

    def list_tasks(
        self, *, limit: int = 50, offset: int = 0, state: str | None = None
    ) -> list[dict[str, Any]]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (state, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._task_row_to_dict(r) for r in rows]

    def list_active_tasks(self) -> list[dict[str, Any]]:
        """所有处于活跃态（submitted / working）的任务。

        重启后拿它来找「失去 worker 的孤儿任务」（A2A-09）。
        """
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE state IN ('submitted','working') "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [self._task_row_to_dict(r) for r in rows]

    def count_tasks(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        return int(row["n"])

    def update_task(
        self,
        task_id: str,
        *,
        state: str | None = None,
        agent: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        started: bool = False,
        finished: bool = False,
        only_from: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """更新任务。

        `only_from` 给出**允许的前置状态**，用于终态写入的原子保护：
        「取消」和「完成」可能并发发生，谁先到谁生效，后到的不许覆盖。
        没有这个条件，一个已被取消的同步任务跑完仍会把状态改成 completed。

        `started` / `finished` 分别打上「真正开始执行」和「进入终态」的时刻。
        **两者与 `created_at` 是三件不同的事**：编排层会预先落占位任务，
        记录在 plan 开始那一刻就诞生了，但可能几分钟后才真正开始跑（N2）。

        返回更新后的 task；若因前置状态不匹配而未更新，会带上 `_updated: False`。
        """
        sets = ["updated_at = ?"]
        args: list[Any] = [utcnow()]

        if state is not None:
            sets.append("state = ?")
            args.append(state)
        if agent is not None:
            sets.append("agent = ?")
            args.append(agent)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        if metadata is not None:
            sets.append("metadata = ?")
            args.append(_dumps(metadata))
        if started:
            sets.append("started_at = ?")
            args.append(utcnow())
        if finished:
            sets.append("finished_at = ?")
            args.append(utcnow())

        where = "id = ?"
        args.append(task_id)
        if only_from:
            placeholders = ",".join("?" * len(only_from))
            where += f" AND state IN ({placeholders})"
            args.extend(only_from)

        cursor = self._conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE {where}", args
        )
        updated = cursor.rowcount > 0

        task = self.get_task(task_id)
        if task is not None:
            task["_updated"] = updated
        return task

    @staticmethod
    def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "trace_id": row["trace_id"] if "trace_id" in keys else None,
            "context_id": row["context_id"],
            "agent": row["agent"],
            "state": row["state"],
            "prompt": row["prompt"],
            "plan_id": row["plan_id"] if "plan_id" in keys else None,
            "step_id": row["step_id"] if "step_id" in keys else None,
            "parent_id": row["parent_id"] if "parent_id" in keys else None,
            "created_at": row["created_at"],
            "started_at": row["started_at"] if "started_at" in keys else None,
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "metadata": _loads(row["metadata"], {}),
        }

    def list_plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        """查一个编排计划下的所有 step 任务。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE plan_id = ? ORDER BY created_at ASC",
            (plan_id,),
        ).fetchall()
        return [self._task_row_to_dict(r) for r in rows]

    def list_trace_tasks(self, trace_id: str) -> list[dict[str, Any]]:
        """查一个 trace 下的所有任务，按开始时间排序 —— 审计时间线的骨架。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE trace_id = ? ORDER BY created_at ASC",
            (trace_id,),
        ).fetchall()
        return [self._task_row_to_dict(r) for r in rows]

    def list_trace_messages(self, trace_id: str) -> list[dict[str, Any]]:
        """把整个 trace 下所有任务的消息按时间拉平，就是一条事件时间线。"""
        rows = self._conn.execute(
            """
            SELECT m.*, t.agent AS agent, t.step_id AS step_id, t.trace_id AS trace_id
            FROM messages m
            JOIN tasks t ON t.id = m.task_id
            WHERE t.trace_id = ?
            ORDER BY m.created_at ASC
            """,
            (trace_id,),
        ).fetchall()
        return [
            {
                "task_id": r["task_id"],
                "agent": r["agent"],
                "step_id": r["step_id"],
                "seq": r["seq"],
                "role": r["role"],
                "kind": r["kind"],
                "content": _loads(r["content"], []),
                "metadata": _loads(r["metadata"], {}),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_traces(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """列出最近的 trace（每个 trace 的概览）。"""
        rows = self._conn.execute(
            """
            SELECT trace_id,
                   MIN(created_at)  AS started_at,
                   MAX(finished_at) AS finished_at,
                   COUNT(*)         AS tasks,
                   SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN state = 'failed'    THEN 1 ELSE 0 END) AS failed,
                   GROUP_CONCAT(DISTINCT agent) AS agents
            FROM tasks
            WHERE trace_id IS NOT NULL
            GROUP BY trace_id
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    def _rollback(self) -> None:
        """回滚，并**吞掉回滚本身的失败** —— 回滚都失败了说明事务已经不在了，
        再抛只会掩盖真正的那个异常。"""
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def _write_txn(self, work: Callable[[], Any], *, retries: int = 5) -> Any:
        """在一个写事务里跑 `work`，序号冲突则重试。

        三件事必须做对：

        1. **任何异常都要 ROLLBACK**（M1）。否则这条连接会卡在未提交事务里，
           之后所有写都落在同一事务中永不提交，下一次 `BEGIN IMMEDIATE` 还会
           以 "cannot start a transaction within a transaction" 失败 ——
           一次脏数据毒化后续**全部**请求。
        2. **重试只针对 `IntegrityError`**（序号唯一约束冲突），其余异常立刻抛，
           不浪费重试预算。
        3. **退避必须短**（M2）。调用方全在事件循环线程上，这里的 `time.sleep`
           是**硬阻塞整个服务**的 —— 退避总量必须可控。
           注：`OperationalError`（SQLITE_BUSY）刻意不在这里重试 ——
           `busy_timeout=5000` 已经在 SQLite 内部等过了，再叠一层只是把
           阻塞时间翻倍。

        `work` 里**不要**再 `BEGIN`/`COMMIT`，事务由本方法负责。
        """
        for attempt in range(retries):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = work()
                self._conn.execute("COMMIT")
                return result
            except sqlite3.IntegrityError:
                self._rollback()
                if attempt == retries - 1:
                    raise
                time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
            except BaseException:
                # 兜底回滚：任何其它异常（`_dumps` 的 TypeError、KeyboardInterrupt…）
                # 都必须先 ROLLBACK 再抛（M1）
                self._rollback()
                raise

    def add_message(
        self,
        task_id: str,
        *,
        role: str,
        kind: str = "text",
        content: Any = None,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """追加一条消息。seq 在任务内自增，保证顺序稳定。

        序号分配与插入必须**在同一个事务里**（A2A-18）：
        分成两条自动提交语句的话，两个连接可能同时读到同一个 MAX(seq)，
        插入重复序号，之后按 seq 排序的消息顺序就不稳定了。

        **一条消息一次事务**，所以逐条追加很贵（WAL 下每次 COMMIT 都是一次
        fsync）。要写多条请用 `add_messages`（M2）。
        """
        message_id = message_id or new_id()
        now = utcnow()
        seq = 0

        def work() -> None:
            nonlocal seq
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            seq = int(row["s"]) + 1
            self._conn.execute(
                """
                INSERT INTO messages(id, task_id, seq, role, kind, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, task_id, seq, role, kind, _dumps(content),
                 _dumps(metadata), now),
            )

        self._write_txn(work)
        return {
            "id": message_id,
            "task_id": task_id,
            "seq": seq,
            "role": role,
            "kind": kind,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    def add_messages(
        self, task_id: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """**一次事务**写入多条消息（M2）。

        为什么要批量：`_execute` 会把一个任务的过程事件逐条落库，而每条
        `add_message` 都是一次独立事务 —— WAL 下每次 COMMIT 都要 fsync。
        实测 50 个事件 = 262ms 的事件循环**硬阻塞**，这期间连 `CancelTask`
        都调度不了。合成一个事务后 fsync 只发生一次。

        `items` 每项支持 `role` / `kind` / `content` / `metadata` / `message_id`。
        序号在同一事务内连续分配，顺序与传入顺序一致。
        """
        if not items:
            return []
        now = utcnow()
        out: list[dict[str, Any]] = []

        def work() -> None:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            seq = int(row["s"])
            for item in items:
                seq += 1
                mid = item.get("message_id") or new_id()
                self._conn.execute(
                    """
                    INSERT INTO messages(id, task_id, seq, role, kind, content, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, task_id, seq, item.get("role") or "agent",
                     item.get("kind") or "text", _dumps(item.get("content")),
                     _dumps(item.get("metadata")), now),
                )
                out.append({
                    "id": mid,
                    "task_id": task_id,
                    "seq": seq,
                    "role": item.get("role"),
                    "kind": item.get("kind"),
                    "content": item.get("content"),
                    "metadata": item.get("metadata") or {},
                    "created_at": now,
                })

        self._write_txn(work)
        return out

    def list_messages(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE task_id = ? ORDER BY seq ASC",
            (task_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "task_id": r["task_id"],
                "seq": r["seq"],
                "role": r["role"],
                "kind": r["kind"],
                "content": _loads(r["content"], []),
                "metadata": _loads(r["metadata"], {}),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------

    def add_artifact(
        self,
        task_id: str,
        *,
        name: str | None = None,
        content: Any = None,
        metadata: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        artifact_id = artifact_id or new_id()
        now = utcnow()
        self._conn.execute(
            """
            INSERT INTO artifacts(id, task_id, name, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, task_id, name, _dumps(content), _dumps(metadata), now),
        )
        return {
            "id": artifact_id,
            "task_id": task_id,
            "name": name,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "task_id": r["task_id"],
                "name": r["name"],
                "content": _loads(r["content"], []),
                "metadata": _loads(r["metadata"], {}),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # contexts（跨轮续接）
    # ------------------------------------------------------------------

    def set_context_session(
        self, context_id: str, agent: str, session_id: str | None
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO contexts(context_id, agent, session_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(context_id, agent)
            DO UPDATE SET session_id = excluded.session_id,
                          updated_at = excluded.updated_at
            """,
            (context_id, agent, session_id, utcnow()),
        )

    def get_context_session(self, context_id: str, agent: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM contexts WHERE context_id = ? AND agent = ?",
            (context_id, agent),
        ).fetchone()
        return row["session_id"] if row else None

    def list_contexts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM contexts ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # agents（注册表）
    # ------------------------------------------------------------------

    def upsert_agent(
        self,
        *,
        name: str,
        kind: str,
        endpoint: str | None = None,
        card: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utcnow()
        self._conn.execute(
            """
            INSERT INTO agents(name, kind, endpoint, card, tags, config, enabled,
                               health, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind       = excluded.kind,
                endpoint   = excluded.endpoint,
                card       = excluded.card,
                tags       = excluded.tags,
                config     = excluded.config,
                enabled    = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (name, kind, endpoint, _dumps(card), _dumps(tags or []),
             _dumps(config), 1 if enabled else 0, now, now),
        )
        return self.get_agent(name)  # type: ignore[return-value]

    def get_agent(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM agents WHERE name = ?", (name,)
        ).fetchone()
        return self._agent_row_to_dict(row) if row else None

    def list_agents(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name ASC"
        rows = self._conn.execute(sql).fetchall()
        return [self._agent_row_to_dict(r) for r in rows]

    def set_agent_health(self, name: str, health: str) -> None:
        self._conn.execute(
            "UPDATE agents SET health = ?, last_seen = ?, updated_at = ? WHERE name = ?",
            (health, utcnow(), utcnow(), name),
        )

    def delete_agent(self, name: str) -> None:
        self._conn.execute("DELETE FROM agents WHERE name = ?", (name,))

    @staticmethod
    def _agent_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "name": row["name"],
            "kind": row["kind"],
            "endpoint": row["endpoint"],
            "card": _loads(row["card"], {}),
            "tags": _loads(row["tags"], []),
            "config": _loads(row["config"], {}) if "config" in keys else {},
            "enabled": bool(row["enabled"]),
            "health": row["health"],
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
