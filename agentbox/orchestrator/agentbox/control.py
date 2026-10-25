"""Remote control: approval gates, auto/manual mode, pause and stop, live status and an
activity feed.

The running agent and the phone panel (`agentctl serve`) are separate processes. They
share state only through small files in <data_dir>/control, so either can restart
without the other noticing:

  state.json            {"mode": "manual", "paused": false, ...}   (written by the panel/CLI)
  status.json           what the agent is doing right now          (written by the agent)
  events.jsonl          activity feed, newest last                 (written by the agent)
  approvals/<id>.json   one file per question to you: pending -> answered / expired
  sessions.json, pair.json   signed-in phones (hashes only; see panel.py)

A gate is a point where the agent may wait for you: `plan` (before the repo and issues
are created), `task` (before work on an issue starts), `tests` (before the tests written from
the acceptance criteria are locked), `push` (before anything is pushed or a pull request
opened) and `merge`. In auto mode only the gates in
`control.always_ask` wait; in manual mode every gate in `control.manual_gates` does.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import Config
from .util import atomic_write_json, log, now_iso, truncate_middle

MAX_DIFF_CHARS = 80_000
EVENTS_MAX_BYTES = 512 * 1024
EVENTS_KEEP = 1000

GATE_LABELS = {"plan": "Plan", "task": "Start task", "tests": "Review tests", "push": "Push", "merge": "Merge"}


class StopRun(Exception):
    """The run must end now (STOP requested, budget reached, guard failed, ...)."""


@dataclass
class Choice:
    id: str
    label: str
    style: str = "secondary"      # primary | secondary | danger
    needs_note: bool = False      # the panel requires a note (e.g. "send back with feedback")
    hint: str = ""


@dataclass
class Decision:
    choice: str
    note: str = ""
    by: str = "auto"
    auto: bool = True

    @property
    def approved(self) -> bool:
        return self.choice == "approve"


@dataclass
class Approval:
    id: str
    gate: str
    title: str
    project: str = ""
    task: str = ""
    summary: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    diff: str = ""
    links: list[dict] = field(default_factory=list)
    choices: list[dict] = field(default_factory=list)
    status: str = "pending"       # pending | answered | expired
    created: str = field(default_factory=now_iso)
    answered: Optional[str] = None
    choice: Optional[str] = None
    note: str = ""
    by: str = ""

    def public(self, with_diff: bool = False) -> dict:
        d = asdict(self)
        if not with_diff:
            d["diff_lines"] = self.diff.count("\n") + (1 if self.diff else 0)
            d.pop("diff")
        return d


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


class Control:
    def __init__(self, cfg: Config, notifier: Optional[Callable[..., None]] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 poll_s: float = 2.0):
        self.cfg = cfg
        self.c = cfg.control
        self.dir = cfg.control_dir
        self.approvals_dir = self.dir / "approvals"
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        self.sleep, self.clock, self.poll_s = sleep, clock, poll_s
        if notifier is None:
            from .report import notify as notifier  # late import: report imports state only
        self.notifier = notifier
        self.waited_s = 0.0          # time spent waiting for you (not charged to the run budget)
        self._status: dict = {}

    # ------------------------------------------------------------------ files
    @contextmanager
    def lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------ mode / pause / stop
    def state(self) -> dict:
        s = self._read("state.json")
        mode = s.get("mode") if s.get("mode") in ("auto", "manual") else self.c.mode
        return {"mode": mode, "paused": bool(s.get("paused")), "updated": s.get("updated"), "by": s.get("by", "")}

    def mode(self) -> str:
        return self.state()["mode"]

    def _write_state(self, by: str, **changes) -> dict:
        with self.lock():
            s = self._read("state.json")
            s.update(changes, updated=now_iso(), by=by)
            atomic_write_json(self.dir / "state.json", s)
        return self.state()

    def set_mode(self, mode: str, by: str = "cli") -> dict:
        if mode not in ("auto", "manual"):
            raise ValueError("mode must be auto or manual")
        old = self.mode()
        st = self._write_state(by, mode=mode)
        if old != mode:
            self.event(f"Mode switched to {mode} ({by})", kind="mode")
        return st

    def set_paused(self, paused: bool, by: str = "cli") -> dict:
        st = self._write_state(by, paused=bool(paused))
        self.event(("Pause requested" if paused else "Resumed") + f" ({by})", kind="control")
        return st

    def stop_requested(self) -> bool:
        return self.cfg.budget.stop_path.exists()

    def set_stop(self, stop: bool, by: str = "cli") -> None:
        p = self.cfg.budget.stop_path
        if stop:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"stop requested by {by} at {now_iso()}\n")
            self.event(f"Stop requested ({by}): the agent stops at its next checkpoint", kind="control", level="warn")
        else:
            p.unlink(missing_ok=True)
            self.event(f"Stop cleared ({by})", kind="control")

    def needs_you(self, gate: str) -> bool:
        if gate in self.c.always_ask:
            return True
        return self.mode() == "manual" and gate in self.c.manual_gates

    # ------------------------------------------------------------------ night rules
    def is_night(self) -> bool:
        return self.c.night.active(tuple(time.localtime(self.clock())[3:5]))

    def priority(self, default: str = "", wake: bool = False) -> str:
        """ntfy priority for a notification: silent ("low") during the night's quiet hours,
        unless `wake` (Sentinel's high alerts, when the owner wants to be woken for them)."""
        n = self.c.night
        if n.quiet and self.is_night() and not (wake and n.wake_for_sentinel_high):
            return "low"
        return default

    # ------------------------------------------------------------------ status + events
    def set_status(self, phase: str, **fields) -> None:
        """What the agent is doing now. Fields not given keep their previous value, except
        that a new phase resets `detail` and the phase timer."""
        prev = self._status
        st = dict(prev)
        if phase != prev.get("phase") or "detail" in fields:
            st["since"] = now_iso()
        if phase != prev.get("phase") and "detail" not in fields:
            st["detail"] = ""
        st.update(fields, phase=phase, pid=os.getpid(), updated=now_iso())
        self._status = st
        try:
            atomic_write_json(self.dir / "status.json", st)
        except OSError as e:  # status is informational: never break a run over it
            log.debug("could not write status.json: %s", e)

    def status(self) -> dict:
        return self._read("status.json")

    def event(self, text: str, kind: str = "info", level: str = "info", project: str = "", task: str = "",
              link: str = "") -> None:
        rec = {"ts": now_iso(), "kind": kind, "level": level, "text": text[:500]}
        if project:
            rec["project"] = project
        if task:
            rec["task"] = task
        if link:
            rec["link"] = link
        path = self.dir / "events.jsonl"
        try:
            with self.lock():
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if path.stat().st_size > EVENTS_MAX_BYTES:
                    lines = path.read_text(encoding="utf-8").splitlines()[-EVENTS_KEEP:]
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    os.replace(tmp, path)
        except OSError as e:
            log.debug("could not write event: %s", e)

    def events(self, n: int = 60) -> list[dict]:
        try:
            lines = (self.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-n:]
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    # ------------------------------------------------------------------ approvals
    def _path(self, aid: str) -> Path:
        if not aid.replace("-", "").isalnum():
            raise ValueError("bad approval id")
        return self.approvals_dir / f"{aid}.json"

    def get(self, aid: str) -> Optional[Approval]:
        try:
            return Approval(**json.loads(self._path(aid).read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def _save(self, a: Approval) -> None:
        atomic_write_json(self._path(a.id), asdict(a))

    def approvals(self, status: Optional[str] = None, limit: int = 50) -> list[Approval]:
        items = []
        for p in sorted(self.approvals_dir.glob("*.json"), reverse=True)[: limit * 4]:
            a = self.get(p.stem)
            if a and (status is None or a.status == status):
                items.append(a)
        return items[:limit]

    def decide(self, aid: str, choice: str, note: str = "", by: str = "phone") -> Approval:
        note = (note or "").strip()[:4000]
        with self.lock():
            a = self.get(aid)
            if a is None:
                raise LookupError("no such request")
            if a.status != "pending":
                raise ValueError(f"already {a.status}" + (f" ({a.choice} by {a.by})" if a.choice else ""))
            spec = next((c for c in a.choices if c["id"] == choice), None)
            if spec is None:
                raise ValueError(f"'{choice}' is not one of: " + ", ".join(c["id"] for c in a.choices))
            if spec.get("needs_note") and not note:
                raise ValueError(f"'{spec['label']}' needs a note")
            a.status, a.choice, a.note, a.by, a.answered = "answered", choice, note, by, now_iso()
            self._save(a)
        return a

    def expire_pending(self, reason: str) -> int:
        n = 0
        with self.lock():
            for a in self.approvals(status="pending", limit=500):
                a.status, a.note, a.answered = "expired", reason, now_iso()
                self._save(a)
                n += 1
        return n

    def ask(self, gate: str, title: str, *, choices: list[Choice], project: str = "", task: str = "",
            summary: Optional[list[str]] = None, details: Optional[dict] = None, diff: str = "",
            links: Optional[list[dict]] = None, stop_check: Optional[Callable[[], None]] = None) -> Decision:
        """Ask the owner at a gate. Returns immediately with "approve" when the gate does not
        need them; otherwise waits for an answer from the phone panel or the CLI.
        `stop_check` is called while waiting and may raise StopRun."""
        if not self.needs_you(gate):
            return Decision("approve", by="auto", auto=True)

        aid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        a = Approval(id=aid, gate=gate, title=title, project=project, task=task, summary=summary or [],
                     details=details or {}, diff=truncate_middle(diff or "", MAX_DIFF_CHARS), links=links or [],
                     choices=[asdict(c) for c in choices])
        prev_status = dict(self._status)
        self.set_status("waiting", detail=f"{GATE_LABELS.get(gate, gate)}: {title}", approval=aid,
                        project=project or prev_status.get("project", ""), task=task or prev_status.get("task", ""))
        self._save(a)  # after the status, so the panel never shows a question without "waiting"
        self.event(f"Waiting for you — {GATE_LABELS.get(gate, gate)}: {title}", kind="ask", level="ask",
                   project=project, task=task)
        log.info("waiting for your answer (%s): %s  - answer on the phone panel or with `agentctl ask`", gate, title)
        self._notify_ask(a)

        t0 = self.clock()
        try:
            while True:
                cur = self.get(aid)
                if cur is None:
                    raise StopRun(f"approval request {aid} disappeared")
                if cur.status == "answered":
                    d = Decision(cur.choice or "approve", cur.note, cur.by, auto=False)
                    label = next((c["label"] for c in cur.choices if c["id"] == d.choice), d.choice)
                    self.event(f"You chose “{label}” — {title}" + (f": {d.note[:200]}" if d.note else ""),
                               kind="answer", project=project, task=task)
                    return d
                if cur.status == "expired":
                    raise StopRun(f"approval request expired: {cur.note}")
                if not self.needs_you(gate):  # switched to auto while waiting
                    with self.lock():
                        cur = self.get(aid)
                        if cur and cur.status == "pending":
                            cur.status, cur.choice, cur.by, cur.answered = "answered", "approve", "auto mode", now_iso()
                            self._save(cur)
                    if cur and cur.status == "answered" and cur.by == "auto mode":
                        self.event(f"Approved automatically after switching to auto — {title}", kind="answer",
                                   project=project, task=task)
                        return Decision("approve", by="auto mode", auto=True)
                    continue
                if stop_check:
                    stop_check()
                timeout_min, on_timeout = self._timeout()
                if timeout_min and self.clock() - t0 >= timeout_min * 60:
                    return self._timed_out(aid, title, project, task, timeout_min, on_timeout)
                self.sleep(self.poll_s)
        except BaseException:
            with self.lock():
                cur = self.get(aid)
                if cur and cur.status == "pending":
                    cur.status, cur.note, cur.answered = "expired", "the run stopped while waiting", now_iso()
                    self._save(cur)
            raise
        finally:
            self.waited_s += self.clock() - t0
            if prev_status.get("phase"):
                self.set_status(prev_status["phase"], detail=prev_status.get("detail", ""), approval=None)

    def _timeout(self) -> tuple[float, str]:
        """(minutes, action) for an unanswered question, checked while waiting: one asked at
        22:50 still times out once the night starts. At night the question is left for you."""
        n = self.c.night
        if n.question_timeout_min > 0 and self.is_night():
            return n.question_timeout_min, "skip"
        return self.c.approval_timeout_min, self.c.on_timeout

    def _timed_out(self, aid: str, title: str, project: str, task: str, mins: float, action: str) -> Decision:
        with self.lock():
            cur = self.get(aid)
            ids = [c["id"] for c in cur.choices] if cur else []
            choice = "approve" if action == "approve" else ("skip" if "skip" in ids else
                                                            "handoff" if "handoff" in ids else None)
            if cur and cur.status == "pending":
                if action == "stop" or choice is None:
                    cur.status, cur.note, cur.answered = "expired", f"no answer within {mins:g} min", now_iso()
                else:
                    cur.status, cur.choice, cur.by, cur.answered = "answered", choice, "timeout", now_iso()
                self._save(cur)
        if action == "stop" or choice is None:
            self.event(f"No answer within {mins:g} min — stopping the run ({title})", kind="answer", level="warn",
                       project=project, task=task)
            raise StopRun(f"no answer within {mins:g} min for: {title}")
        self.event(f"No answer within {mins:g} min — {choice} ({title})", kind="answer", level="warn",
                   project=project, task=task)
        return Decision(choice, note=f"no answer within {mins:g} min", by="timeout", auto=True)

    def _notify_ask(self, a: Approval) -> None:
        url = self.cfg.notify.ntfy_url
        if not url:
            return
        link = self.c.public_url.rstrip("/") + f"/#a={a.id}" if self.c.public_url else ""
        lines = [a.title] + [f"• {s}" for s in a.summary[:4]]
        if self.is_night() and self.c.night.question_timeout_min > 0:
            lines.append(f"Night: left for you after {self.c.night.question_timeout_min:g} min without an answer")
        try:
            self.notifier(url, f"agentbox needs you: {GATE_LABELS.get(a.gate, a.gate)}", "\n".join(lines),
                          click=link, priority=self.priority("high"), tags="raising_hand",
                          actions=f"view, Open panel, {link}" if link else "")
        except TypeError:  # an older/simple notifier
            self.notifier(url, f"agentbox needs you: {GATE_LABELS.get(a.gate, a.gate)}", "\n".join(lines))

    # ------------------------------------------------------------------ pause
    def wait_if_paused(self, stop_check: Optional[Callable[[], None]] = None) -> None:
        if not self.state()["paused"]:
            return
        prev = dict(self._status)
        self.set_status("paused", detail="Paused from the panel — tap Resume to continue")
        self.event("Paused at a checkpoint", kind="control")
        t0 = self.clock()
        try:
            while self.state()["paused"]:
                if stop_check:
                    stop_check()
                self.sleep(self.poll_s)
        finally:
            self.waited_s += self.clock() - t0
            if prev.get("phase"):
                self.set_status(prev["phase"], detail=prev.get("detail", ""))
        self.event("Resumed", kind="control")


# Choice sets used by the orchestrator's gates -------------------------------------------
PLAN_CHOICES = [
    Choice("approve", "Create repo & start", "primary"),
    Choice("revise", "Re-plan with my notes", needs_note=True, hint="Your note is given to the planner"),
    Choice("cancel", "Cancel project", "danger"),
]
TASK_CHOICES = [
    Choice("approve", "Start", "primary", hint="Optional note is passed to the coder as guidance"),
    Choice("skip", "Skip for now", hint="Marks the issue agent:needs-human; relabel agent:todo later"),
    Choice("stop", "Stop the run", "danger"),
]
# Offered at the task gate when tests were locked in an earlier run of the same task.
FRESH_TESTS_CHOICE = Choice("fresh", "Start with new tests", hint="Discards the tests you locked in an earlier run")
TESTS_CHOICES = [
    Choice("approve", "Lock tests & implement", "primary", hint="The implementation may not change locked tests"),
    Choice("revise", "Rewrite tests with my notes", needs_note=True, hint="These tests are discarded; the coder gets your note"),
    Choice("handoff", "Hand off to me", "danger", hint="Stops work on this issue (agent:needs-human)"),
]
PUSH_CHOICES = [
    Choice("approve", "Push & open PR", "primary"),
    Choice("revise", "Send back with feedback", needs_note=True, hint="Counts as an attempt; the coder gets your note"),
    Choice("handoff", "Hand off to me", "danger", hint="Stops work on this issue (agent:needs-human)"),
]
# "approve" is what auto mode does at each of these: send back, try again, rewrite the tests.
REVIEW_BLOCKED_CHOICES = [
    Choice("approve", "Send back to the coder", "primary", hint="The coder gets the reviewer's issues (counts as an attempt)"),
    Choice("override", "Overrule the reviewer", hint="Continue as if approved; CI and the merge question still follow"),
    Choice("revise", "Send back with my notes", needs_note=True, hint="The coder gets your note instead of the review"),
    Choice("handoff", "Leave PR for me", "danger", hint="Stops work on this issue (agent:needs-human)"),
]
RETRY_CHOICES = [
    Choice("approve", "Try again", "primary", hint="Optional note is passed to the coder as guidance"),
    Choice("handoff", "Hand off to me", "danger", hint="Stops work on this issue (agent:needs-human)"),
]
LOCKED_TEST_CHOICES = [
    Choice("approve", "Rewrite the tests", "primary", hint="The coder's report (and your note) go to a new tests-first phase"),
    Choice("keep", "Keep the tests", hint="The tests are right: the coder must satisfy them"),
    Choice("handoff", "Hand off to me", "danger", hint="Stops work on this issue (agent:needs-human)"),
]
MERGE_CHOICES = [
    Choice("approve", "Merge", "primary"),
    Choice("revise", "Send back with feedback", needs_note=True),
    Choice("handoff", "Leave PR for me", "danger"),
]
SIMPLE_PUSH_CHOICES = [
    Choice("approve", "Push", "primary"),
    Choice("skip", "Don't push"),
]
