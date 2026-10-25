"""Persistent per-project state (data/projects/<repo>/state.json).

GitHub remains the source of truth for anything a human can change (issue labels,
open/closed, comments); `reconcile()` in loop.py folds those changes back in before
every run. This file holds what GitHub cannot: attempt history, the plan, the stack.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .util import atomic_write_json, now_iso

Status = Literal["todo", "in_progress", "done", "needs_human", "blocked", "skipped"]


class AttemptRecord(BaseModel):
    ts: str = Field(default_factory=now_iso)
    phase: str
    attempt: int
    outcome: str
    note: str = ""


class TaskState(BaseModel):
    id: str
    title: str
    kind: str = "feature"
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    files_hint: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    issue: Optional[int] = None
    status: Status = "todo"
    branch: Optional[str] = None
    pr: Optional[int] = None
    attempts_total: int = 0
    human_feedback_since: Optional[str] = None
    # Tests approved and locked in the tests-first phase, kept until the task is merged, so a run
    # that stops right after the tests phase does not throw them away (path -> content).
    locked_tests: dict[str, str] = Field(default_factory=dict)
    history: list[AttemptRecord] = Field(default_factory=list)

    def record(self, phase: str, attempt: int, outcome: str, note: str = "") -> None:
        self.history.append(AttemptRecord(phase=phase, attempt=attempt, outcome=outcome, note=note[:4000]))


class StackState(BaseModel):
    language: str = "python"
    setup_command: str = ""
    test_command: str = ""
    lint_command: str = ""


class RunRecord(BaseModel):
    run_id: str
    started: str
    finished: Optional[str] = None
    command: str = ""
    done: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    task_minutes: dict[str, float] = Field(default_factory=dict)   # wall minutes per task, waits included
    waited_minutes: float = 0                                      # spent waiting for your answers or paused


class ProjectState(BaseModel):
    repo: str
    owner: str
    description: str = ""
    created: str = Field(default_factory=now_iso)
    stack: StackState = Field(default_factory=StackState)
    tasks: list[TaskState] = Field(default_factory=list)
    log_issue: Optional[int] = None
    runs: list[RunRecord] = Field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def task(self, task_id: str) -> Optional[TaskState]:
        return next((t for t in self.tasks if t.id == task_id), None)

    def task_by_issue(self, number: int) -> Optional[TaskState]:
        return next((t for t in self.tasks if t.issue == number), None)

    def ready_tasks(self) -> list[TaskState]:
        done = {t.id for t in self.tasks if t.status in ("done", "skipped")}
        return [t for t in self.tasks if t.status == "todo" and all(d in done for d in t.depends_on)]

    def refresh_blocked(self) -> list[TaskState]:
        """Mark todo tasks blocked when any dependency needs a human (transitively), and
        unblock tasks whose blockers were resolved. Returns tasks whose status changed."""
        changed: list[TaskState] = []
        by_id = {t.id: t for t in self.tasks}

        def stuck(tid: str, seen: set[str]) -> bool:
            t = by_id.get(tid)
            if t is None or tid in seen:
                return False
            if t.status == "needs_human":
                return True
            seen.add(tid)
            return any(stuck(d, seen) for d in t.depends_on)

        for t in self.tasks:
            if t.status not in ("todo", "blocked"):
                continue
            is_stuck = any(stuck(d, set()) for d in t.depends_on)
            new = "blocked" if is_stuck else "todo"
            if new != t.status:
                t.status = new  # type: ignore[assignment]
                changed.append(t)
        return changed

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out


class StateStore:
    def __init__(self, projects_dir: Path):
        self.dir = projects_dir

    def path(self, repo: str) -> Path:
        return self.dir / repo / "state.json"

    def project_dir(self, repo: str) -> Path:
        d = self.dir / repo
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exists(self, repo: str) -> bool:
        return self.path(repo).exists()

    def load(self, repo: str) -> ProjectState:
        p = self.path(repo)
        if not p.exists():
            raise FileNotFoundError(f"no state for project '{repo}' at {p}")
        return ProjectState.model_validate(json.loads(p.read_text(encoding="utf-8")))

    def save(self, st: ProjectState) -> None:
        atomic_write_json(self.path(st.repo), st.model_dump(mode="json"))

    def all(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.parent.name for p in self.dir.glob("*/state.json"))
