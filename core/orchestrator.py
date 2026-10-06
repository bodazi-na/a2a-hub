# -*- coding: utf-8 -*-
"""编排引擎：把一组 Step 按依赖关系跑成一条流水线。

三种形态其实是同一套机制的三种写法
------------------------------------
| 形态 | 写法 |
| --- | --- |
| 串行链 | 每步 depends_on 上一步 |
| 并行扇出 | 多步无依赖 → 同层并行；末尾一个 merge 步依赖它们全部 |
| 主管-工人 | 第一层是主管步，后续层依赖它（plan 仍由调用方显式给出） |

执行模型
--------
1. 拓扑分层：把 steps 按依赖切成若干层，**同层内并行**
2. 逐层执行：层内 `asyncio.gather`，任一步失败按 `on_error` 决定是
   fail-fast（默认，整条链停）还是 continue（只跳过受影响的后续步）
3. 每一步都落库成一个 task，带 `plan_id` / `step_id` / `parent_id`，
   所以整个 plan 可以事后完整追溯

模板变量
--------
`{{input}}`      —— 调用方给的原始输入
`{{steps.<id>}}` —— 某个 step 的最终输出

刻意不做的事
------------
不在这里调用模型来「生成 plan」。plan 由调用方显式给出：确定性高、可审计、
不依赖任何 agent 的能力。想接主管模式，让主管 agent 先产出一份 plan 再传进来即可。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

STEP_REF_RE = re.compile(r"\{\{\s*steps\.([A-Za-z0-9_.\-]+)\s*\}\}")
INPUT_REF_RE = re.compile(r"\{\{\s*input\s*\}\}")

ON_ERROR_FAIL = "fail"
ON_ERROR_CONTINUE = "continue"


class PlanError(ValueError):
    """plan 本身有问题（循环依赖、引用不存在的 step 等）。"""


@dataclass
class Step:
    id: str
    prompt: str
    agent: str | None = None
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    on_error: str = ON_ERROR_FAIL
    timeout: float = 600.0
    # 是否与同 plan 的其他 step 共享下游会话上下文。
    # **默认 False**：step 之间靠模板变量显式传数据，不该靠共享会话隐式串味 ——
    # 共享会让并行扇出退化成串行（A2A-04 的锁按 (contextId, agent) 串行化），
    # 而且下游会看到别的分支的对话。想做链式推理时再显式打开。
    shared_context: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Step":
        if not raw.get("id"):
            raise PlanError("step 缺少 id")
        if not raw.get("prompt"):
            raise PlanError(f"step {raw.get('id')} 缺少 prompt")
        on_error = str(raw.get("onError") or raw.get("on_error") or ON_ERROR_FAIL)
        if on_error not in (ON_ERROR_FAIL, ON_ERROR_CONTINUE):
            raise PlanError(f"step {raw['id']} 的 onError 非法: {on_error}")
        return cls(
            id=str(raw["id"]),
            prompt=str(raw["prompt"]),
            agent=raw.get("agent"),
            tags=list(raw.get("tags") or []),
            depends_on=[str(d) for d in (raw.get("dependsOn") or raw.get("depends_on") or [])],
            on_error=on_error,
            timeout=float(raw.get("timeout") or 600.0),
            shared_context=bool(raw.get("sharedContext") or raw.get("shared_context") or False),
        )


@dataclass
class StepResult:
    id: str
    ok: bool
    task_id: str | None = None
    text: str = ""
    error: str | None = None
    skipped: bool = False


@dataclass
class PlanResult:
    plan_id: str
    ok: bool
    steps: dict[str, StepResult]
    layers: list[list[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "ok": self.ok,
            "layers": self.layers,
            "steps": [
                {
                    "id": r.id,
                    "ok": r.ok,
                    "skipped": r.skipped,
                    "taskId": r.task_id,
                    "text": r.text,
                    **({"error": r.error} if r.error else {}),
                }
                for r in self.steps.values()
            ],
        }


def infer_dependencies(steps: list[Step]) -> None:
    """从 prompt 模板里的 `{{steps.<id>}}` 自动补全 depends_on。

    模板已经表达了「这一步要用谁的输出」，再让调用方手写一遍 dependsOn
    既啰嗦又极易漏 —— 漏了就会与上游同层并行，模板变量取不到值。
    显式写的 dependsOn 仍然保留（用于「有顺序但不用输出」的场景）。

    校验也在这里做：引用了不存在的 step、或**引用自己**，
    都在入参阶段直接报错（A2A-21）—— 拖到运行时渲染才失败的话，
    计划已经跑了一半，而且没落库的步骤在 GetPlan 里也查不到。
    """
    ids = {s.id for s in steps}
    for step in steps:
        for ref in {m.group(1) for m in STEP_REF_RE.finditer(step.prompt)}:
            if ref not in ids:
                raise PlanError(f"step {step.id} 的模板引用了不存在的 step: {ref}")
            if ref == step.id:
                raise PlanError(f"step {step.id} 的模板引用了自己（自引用无法求值）")
            if ref not in step.depends_on:
                step.depends_on.append(ref)


def topo_layers(steps: list[Step]) -> list[list[str]]:
    """把 steps 切成拓扑层：同层之间无依赖，可以并行。

    同时做两项校验：依赖必须存在、不能有环。
    """
    by_id = {s.id: s for s in steps}
    if len(by_id) != len(steps):
        raise PlanError("step id 重复")

    for s in steps:
        for dep in s.depends_on:
            if dep not in by_id:
                raise PlanError(f"step {s.id} 依赖了不存在的 step: {dep}")
            if dep == s.id:
                raise PlanError(f"step {s.id} 依赖自己")

    remaining = {s.id: set(s.depends_on) for s in steps}
    layers: list[list[str]] = []
    done: set[str] = set()

    while remaining:
        layer = [sid for sid, deps in remaining.items() if deps <= done]
        if not layer:
            raise PlanError(f"检测到循环依赖: {sorted(remaining)}")
        layer.sort()
        layers.append(layer)
        for sid in layer:
            done.add(sid)
            remaining.pop(sid)

    return layers


def render(template: str, plan_input: str, results: dict[str, StepResult]) -> str:
    """把 {{input}} / {{steps.<id>}} 替换成实际内容。"""

    def sub_step(match: re.Match[str]) -> str:
        sid = match.group(1)
        res = results.get(sid)
        if res is None:
            raise PlanError(f"模板引用了尚未执行的 step: {sid}")
        return res.text

    out = STEP_REF_RE.sub(sub_step, template)
    return INPUT_REF_RE.sub(lambda _m: plan_input, out)


class Orchestrator:
    """执行 plan。依赖 Hub 提供的 dispatch_task 来派发单个 step。"""

    def __init__(self, hub: Any):
        self.hub = hub

    async def run(self, raw_steps: list[dict[str, Any]], *, context_id: str,
                  plan_input: str = "", plan_id: str | None = None,
                  trace_id: str | None = None) -> PlanResult:
        steps = [Step.from_dict(s) for s in raw_steps]
        if not steps:
            raise PlanError("plan 里没有 step")

        infer_dependencies(steps)
        layers = topo_layers(steps)
        by_id = {s.id: s for s in steps}
        plan_id = plan_id or f"plan-{abs(hash(tuple(s.id for s in steps)))}"
        results: dict[str, StepResult] = {}
        aborted = False

        # 先把**全部步骤**落库为占位任务（A2A-07）。
        # 否则被跳过 / 未执行的 step 在库里毫无痕迹，GetPlan 无法重建完整计划 ——
        # 审计要能回答「这次编排原本打算做什么」，而不只是「实际做了什么」。
        placeholders: dict[str, str] = {}
        for step in steps:
            task = self.hub.store.create_task(
                context_id=context_id,
                prompt=step.prompt,
                state="submitted",
                trace_id=trace_id,
                plan_id=plan_id,
                step_id=step.id,
                metadata={"planned": True, "requestedAgent": step.agent},
            )
            placeholders[step.id] = task["id"]

        for layer in layers:
            if aborted:
                break

            runnable: list[Step] = []
            for sid in layer:
                step = by_id[sid]
                failed_deps = [
                    d for d in step.depends_on
                    if d not in results or not results[d].ok
                ]
                if failed_deps:
                    if step.on_error == ON_ERROR_CONTINUE:
                        results[sid] = StepResult(
                            id=sid, ok=False, skipped=True,
                            error=f"依赖失败被跳过: {failed_deps}",
                        )
                        continue
                    results[sid] = StepResult(
                        id=sid, ok=False, error=f"依赖失败: {failed_deps}"
                    )
                    aborted = True
                    break
                runnable.append(step)

            if aborted or not runnable:
                continue

            settled = await asyncio.gather(
                *(self._run_step(s, plan_id, context_id, plan_input, results,
                                 trace_id, placeholders[s.id])
                  for s in runnable),
                return_exceptions=True,
            )

            for step, outcome in zip(runnable, settled):
                if isinstance(outcome, BaseException):
                    outcome = StepResult(
                        id=step.id, ok=False,
                        error=f"{type(outcome).__name__}: {outcome}",
                    )
                results[step.id] = outcome
                if not outcome.ok and step.on_error != ON_ERROR_CONTINUE:
                    aborted = True

        for sid in by_id:
            if sid not in results:
                results[sid] = StepResult(id=sid, ok=False, skipped=True,
                                          error="未执行（上游中止）")
                # 占位任务也要结算，否则它会永远停在 submitted（A2A-07 + A2A-09）
                self.hub.store.update_task(
                    placeholders[sid], state="canceled",
                    error="skipped: 上游中止，本步未执行",
                    finished=True, only_from=("submitted", "working"),
                )

        return PlanResult(
            plan_id=plan_id,
            ok=all(r.ok for r in results.values()),
            steps=results,
            layers=layers,
        )

    async def _run_step(self, step: Step, plan_id: str, context_id: str,
                        plan_input: str, results: dict[str, StepResult],
                        trace_id: str | None = None,
                        task_id: str | None = None) -> StepResult:
        try:
            prompt = render(step.prompt, plan_input, results)
        except PlanError as exc:
            return StepResult(id=step.id, ok=False, error=str(exc))

        # 每个 step 用**独立的会话上下文**（除非显式要求共享）。
        # 共享会让同 agent 的并行分支被 A2A-04 的锁串行化，并互相看到对方的对话；
        # step 之间的数据传递应该走模板变量 {{steps.x}}，那是显式的。
        step_context = context_id if step.shared_context else f"{context_id}::{step.id}"

        payload = await self.hub.dispatch_task(
            prompt,
            agent=step.agent,
            tags=step.tags,
            context_id=step_context,
            timeout=step.timeout,
            trace_id=trace_id,
            plan_id=plan_id,
            step_id=step.id,
            task_id=task_id,
        )

        state = (payload.get("status") or {}).get("state")
        text = ""
        for artifact in payload.get("artifacts") or []:
            for part in artifact.get("parts") or []:
                if isinstance(part, dict) and part.get("text"):
                    text += str(part["text"])

        if state == "TASK_STATE_COMPLETED":
            return StepResult(id=step.id, ok=True, task_id=payload.get("id"), text=text)

        message = (payload.get("status") or {}).get("message") or {}
        parts = message.get("parts") or []
        error = "".join(
            str(p.get("text") or "") for p in parts if isinstance(p, dict)
        ) or f"终态 {state}"
        return StepResult(id=step.id, ok=False, task_id=payload.get("id"),
                          text=text, error=error)
