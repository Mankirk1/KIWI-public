"""Planner: outline -> specification -> validated task graph."""
from __future__ import annotations

import json
import math
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import prompts
from .config import Config
from .llm import LLMClient
from .util import log

REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


class TaskSpec(BaseModel):
    id: str
    title: str
    # Required, not defaulted: llama-server's json_schema grammar lets the model skip optional
    # fields, and a skipped kind silently became "feature" (so no plan had a scaffold task).
    kind: Literal["scaffold", "feature", "refactor", "docs", "test"]
    description: str
    acceptance_criteria: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    files_hint: list[str] = Field(default_factory=list)


class StackSpec(BaseModel):
    language: str
    setup_command: str
    test_command: str
    lint_command: str = ""


class Plan(BaseModel):
    repo_name: str
    description: str
    stack: StackSpec
    tasks: list[TaskSpec] = Field(min_length=1)


class PlanReview(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)


class TaskList(BaseModel):
    tasks: list[TaskSpec] = Field(min_length=1)


def toposort(tasks: list[TaskSpec], known_done: Optional[set[str]] = None) -> list[TaskSpec]:
    """Order tasks so dependencies come first. Raises ValueError on cycles/unknown ids."""
    known_done = known_done or set()
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            if d not in by_id and d not in known_done:
                raise ValueError(f"task {t.id} depends on unknown task {d}")
    order: list[TaskSpec] = []
    state: dict[str, int] = {}

    def visit(tid: str, stack: list[str]) -> None:
        if tid in known_done and tid not in by_id:
            return
        s = state.get(tid, 0)
        if s == 2:
            return
        if s == 1:
            raise ValueError("dependency cycle: " + " -> ".join(stack + [tid]))
        state[tid] = 1
        for d in by_id[tid].depends_on:
            visit(d, stack + [tid])
        state[tid] = 2
        order.append(by_id[tid])

    for t in tasks:
        visit(t.id, [])
    return order


_NUMBER = r"[-+]?\d[\d_,]*(?:\.\d+)?(?:[eE][-+]?\d+)?"
_EXPECTED = re.compile(r"(?:returns?|equals?|==|must (?:be|return|equal)|should (?:be|return|equal)|\bis|gives?|"
                       r"approx\()\s*`?\s*(?:pytest\.approx\(\s*)?(" + _NUMBER + ")")


def unverified_numbers(plan: Plan, *sources: str) -> list[str]:
    """Acceptance criteria whose expected value appears nowhere in the outline/spec, even after
    SI scaling (200 GPa = 200e9): numbers the planner worked out itself, for the owner to check.
    Small integers (counts, exit codes) are skipped."""
    known = []
    for text in sources:
        for m in re.finditer(_NUMBER, text):
            try:
                known.append(float(m.group().replace(",", "").replace("_", "")))
            except ValueError:
                pass

    def found(v: float) -> bool:
        if v.is_integer() and abs(v) < 10:
            return True
        return any(math.isclose(v, k * f, rel_tol=1e-9, abs_tol=1e-12)
                   for k in known for f in (1, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9))

    out = []
    for t in plan.tasks:
        for c in t.acceptance_criteria:
            for m in _EXPECTED.finditer(c):
                try:
                    v = float(m.group(1).replace(",", "").replace("_", ""))
                except ValueError:
                    continue
                if not found(v):
                    out.append(f"{t.id}: {c}")
                    break
    return out


def validate_plan(plan: Plan, cfg: Config) -> list[str]:
    problems: list[str] = []
    if not REPO_RE.match(plan.repo_name):
        problems.append(f"repo_name '{plan.repo_name}' must be 3-60 chars of lowercase letters, digits, hyphens")
    if plan.stack.language not in cfg.workflow.allowed_stacks:
        problems.append(f"stack.language must be one of {cfg.workflow.allowed_stacks}")
    if not plan.stack.test_command.strip():
        problems.append("stack.test_command is empty")
    ids = [t.id for t in plan.tasks]
    if len(ids) != len(set(ids)):
        problems.append("task ids must be unique")
    if len(plan.tasks) > cfg.workflow.max_tasks_per_project:
        problems.append(f"too many tasks ({len(plan.tasks)}); maximum is {cfg.workflow.max_tasks_per_project}")
    if plan.tasks and plan.tasks[0].kind != "scaffold":
        problems.append("the first task must have kind 'scaffold'")
    for t in plan.tasks:
        if len(t.acceptance_criteria) < 2 and t.kind != "docs":
            problems.append(f"task {t.id} needs at least 2 acceptance criteria")
    try:
        toposort(plan.tasks)
    except ValueError as e:
        problems.append(str(e))
    return problems


class Planner:
    def __init__(self, cfg: Config, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm

    def write_spec(self, outline: str) -> str:
        log.info("planner: writing specification")
        system = prompts.SPEC_SYSTEM.format(stacks=", ".join(self.cfg.workflow.allowed_stacks))
        return self.llm.text("planner", system, prompts.SPEC_USER.format(outline=outline))

    def make_plan(self, outline: str, spec: str) -> Plan:
        system = prompts.PLAN_SYSTEM.format(max_tasks=self.cfg.workflow.max_tasks_per_project,
                                            stacks=", ".join(self.cfg.workflow.allowed_stacks))
        user = prompts.PLAN_USER.format(spec=spec, outline=outline)
        log.info("planner: building task plan")
        plan = self.llm.structured("planner", system, user, Plan, max_repairs=3,
                                   extra_check=lambda p: validate_plan(p, self.cfg))
        if self.cfg.workflow.plan_review:
            plan = self._review_and_revise(system, user, spec, plan)
        plan.tasks = toposort(plan.tasks)
        return plan

    def _review_and_revise(self, system: str, user: str, spec: str, plan: Plan) -> Plan:
        log.info("reviewer: critiquing the plan")
        try:
            review = self.llm.structured(
                "reviewer", prompts.PLAN_REVIEW_SYSTEM,
                prompts.PLAN_REVIEW_USER.format(spec=spec, plan=plan.model_dump_json(indent=2)),
                PlanReview, max_repairs=1)
        except Exception as e:  # a failed critique should never sink the run
            log.warning("plan review failed, keeping original plan: %s", e)
            return plan
        if review.ok or not review.problems:
            log.info("reviewer: plan approved")
            return plan
        log.info("reviewer raised %d plan problems; revising", len(review.problems))
        revise_user = (user + "\n\nYour previous plan:\n" + plan.model_dump_json(indent=2) + "\n\n" +
                       prompts.PLAN_REVISE_USER.format(problems="\n".join(f"- {p}" for p in review.problems)))
        try:
            return self.llm.structured("planner", system, revise_user, Plan, max_repairs=2,
                                       extra_check=lambda p: validate_plan(p, self.cfg))
        except Exception as e:
            log.warning("plan revision failed, keeping original plan: %s", e)
            return plan

    def extend(self, spec: str, existing: list[dict], request: str, existing_ids: set[str]) -> list[TaskSpec]:
        system = prompts.EXTEND_SYSTEM.format(max_tasks=min(10, self.cfg.workflow.max_tasks_per_project))
        user = prompts.EXTEND_USER.format(spec=spec, existing=json.dumps(existing, indent=1), request=request)

        def check(tl: TaskList) -> list[str]:
            probs = []
            ids = [t.id for t in tl.tasks]
            if len(ids) != len(set(ids)):
                probs.append("task ids must be unique")
            clash = [i for i in ids if i in existing_ids]
            if clash:
                probs.append(f"ids {clash} already exist; use new X-prefixed ids")
            try:
                toposort(tl.tasks, known_done=existing_ids)
            except ValueError as e:
                probs.append(str(e))
            return probs

        tl = self.llm.structured("planner", system, user, TaskList, max_repairs=3, extra_check=check)
        return toposort(tl.tasks, known_done=existing_ids)
