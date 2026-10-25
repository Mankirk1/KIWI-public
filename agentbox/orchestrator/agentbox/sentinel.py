"""Sentinel: a rule-based watchdog that reads what a run records and flags known failure
patterns while they happen, with the evidence (file, step, time) that triggered each rule.

It uses no model and changes nothing: findings go to the activity feed (kind "sentinel"), to
<data_dir>/control/sentinel.jsonl and, for high severity, to the phone through ntfy. Every rule
comes from a real incident in the field notes (docs/field-notes/2026-10-phase-c.md).

    live:   the orchestrator runs scan() every 30 s during a run (SentinelThread)
    replay: `agentctl sentinel [project]` runs the same rules over everything recorded so far
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Optional

from .util import log

ATTEMPT_DIR = re.compile(r"^(?P<task>[A-Za-z]+\d+)-(?P<mode>tests|implement)-a(?P<attempt>\d+)-(?P<stamp>\d{8}-\d{6})$")

# The coder doubting a locked test or the review, in its own words from runs 1-4: "the expected value
# ... doesn't match", "appears to be wrong", "already correct", "I cannot edit the locked test file".
CONFLICT = re.compile(r"suspicious|expected value[^\n]{0,80}?(?:doesn'?t match|does not match|is slightly different|differs)|"
                      r"(?:appears|seems|looks) to be wrong|already (?:complete and )?correct|already exists and looks correct|"
                      r"(?:can'?t|cannot) (?:edit|change|modify) (?:the )?locked|(?:tests|test file) (?:is|are) locked[^\n]{0,30}"
                      r"(?:can'?t|cannot)|contradicts? (?:the )?(?:spec|outline)|doesn'?t import pytest", re.I)
PATH_WARNING = re.compile(r"is not on PATH|No such file or directory: '[\w.-]+'\]?$|FileNotFoundError: \[Errno 2\]", re.M)
RUNNER_FILES = re.compile(r"(?:^|/)(?:conftest\.py|pytest\.ini|tox\.ini|sitecustomize\.py|usercustomize\.py)$|\.pth$")
EDIT_TOOLS = {"write_file", "edit_file"}
READ_TOOLS = {"read_file", "search", "list_files"}

SLOW_STEP_S = 6 * 60          # one coder step taking longer than this
LONG_ATTEMPT_S = 45 * 60      # one attempt running longer than this
DETOUR_STEPS = 4              # consecutive read/search-only steps after the coder already edited files
REPEATS = 3                   # the same tool call this many times without an edit in between
RUN_WIDE = {"env_path"}


@dataclass
class Finding:
    rule: str
    severity: str            # high | medium | info
    ts: float                # unix time of the evidence
    task: str
    attempt: str             # attempt directory name, or "" for task-level findings
    summary: str
    evidence: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.rule in RUN_WIDE:   # an environment problem: report it once, not once per attempt
            return self.rule
        return f"{self.rule}:{self.attempt or self.task}:{self.evidence[0] if self.evidence else ''}"[:300]

    def when(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M")


def _records(transcript: Path) -> list[dict]:
    out = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass   # a line still being written
    return out


def scan_attempt(d: Path, locked: Optional[bool] = None, now: Optional[float] = None) -> list[Finding]:
    """Findings for one attempt directory (work/<project>/<task>-<mode>-a<n>-<stamp>)."""
    m = ATTEMPT_DIR.match(d.name)
    tr = d / "task" / "out" / "transcript.jsonl"
    if not m or not tr.is_file():
        return []
    task, mode = m["task"], m["mode"]
    if locked is None:
        try:
            locked = bool(json.loads((d / "task" / "task.json").read_text()).get("locked_tests"))
        except (OSError, ValueError):
            locked = False
    recs = _records(tr)
    f: list[Finding] = []

    def add(rule, sev, ts, summary, *evidence):
        f.append(Finding(rule, sev, ts, task, d.name, summary, [e[:300] for e in evidence]))

    edited, detour, prev_t, calls = False, 0, None, {}
    steps = [r for r in recs if r.get("kind") == "assistant"]
    for r in recs:
        kind, t = r.get("kind"), float(r.get("t") or 0)
        if kind == "assistant":
            content = r.get("content") or ""
            if mode == "implement" and locked and CONFLICT.search(content):
                add("locked_test_conflict", "high", t, f"{task}: the coder questions a locked test (step {r.get('step')})",
                    f"step {r.get('step')}: {content[:240]}")
            if prev_t and t - prev_t > SLOW_STEP_S:
                add("slow_step", "info", t, f"{task}: step {r.get('step')} took {round((t - prev_t) / 60, 1)} min",
                    f"step {r.get('step')} after {round(t - prev_t)} s")
            prev_t = t
            tools = set(r.get("tools") or [])
            if tools & EDIT_TOOLS:
                edited, detour, calls = True, 0, {}
            elif edited and tools and tools <= READ_TOOLS:
                detour += 1
                if detour == DETOUR_STEPS:
                    add("detour", "medium", t, f"{task}: {DETOUR_STEPS} read/search-only steps after editing — "
                        "investigating instead of finishing?", f"steps {int(r.get('step') or 0) - DETOUR_STEPS + 1}-{r.get('step')}")
            elif tools - READ_TOOLS:
                detour = 0
        elif kind == "tool":
            name, args, result = r.get("name") or "", r.get("args") or "", r.get("result") or ""
            sig = name + args
            calls[sig] = calls.get(sig, 0) + 1
            if calls[sig] == REPEATS:
                add("repeated_call", "medium", t, f"{task}: the same {name} call {REPEATS} times", f"{name} {args[:200]}")
            if PATH_WARNING.search(result):
                add("env_path", "high", t, f"{task}: an installed command is not on PATH in the sandbox",
                    f"{name} {args[:120]} -> {PATH_WARNING.search(result).group(0)}")
            if name in EDIT_TOOLS and locked and mode == "implement" and not result.startswith("error"):
                try:
                    path = json.loads(args).get("path", "")
                except ValueError:
                    path = re.search(r'"path": "([^"]+)"', args).group(1) if '"path"' in args else ""
                if RUNNER_FILES.search(path or ""):
                    add("runner_config", "high", t, f"{task}: the coder writes {path} while tests are locked",
                        f"{name} {path} -> {result[:120]}")
            if "changes how the locked tests run" in result:
                add("runner_config", "high", t, f"{task}: the coder tried to change test-runner config",
                    f"{name} {args[:120]}")
        elif kind in ("compact", "compact_after_overflow"):
            add("compaction", "info", t, f"{task}: context compacted (prompt cache lost; the next step re-reads it)",
                f"est_tokens {r.get('est_tokens')}")
    if recs:
        start, last = float(recs[0].get("t") or 0), float(recs[-1].get("t") or 0)
        # Still running only if it has no end record AND was written to recently (a killed attempt
        # never gets an end record, and must not look like it is still running days later).
        live = now is not None and not any(r.get("kind") == "end" for r in recs) and now - tr.stat().st_mtime < 900
        end = now if live else last
        if end - start > LONG_ATTEMPT_S:
            add("long_attempt", "medium", start + LONG_ATTEMPT_S,
                f"{task}: {mode} attempt {m['attempt']} has run over {LONG_ATTEMPT_S // 60} min ({len(steps)} steps so far)",
                f"started {datetime.fromtimestamp(start).strftime('%H:%M')}")
    return f


def scan_history(task: dict) -> list[Finding]:
    """Task-level findings from the orchestrator's history (state.json)."""
    f: list[Finding] = []
    hist = task.get("history") or []
    streak = 0
    for h in hist:
        if h.get("outcome") == "no_changes":
            streak += 1
            if streak == 2:
                ts = datetime.fromisoformat(h["ts"]).timestamp()
                f.append(Finding("empty_attempts", "high", ts, task["id"], "",
                                 f"{task['id']}: two attempts in a row made no changes — the coder may disagree with "
                                 "the review or the tests", [h.get("note", "")[:300]]))
        elif h.get("phase") != "owner":
            streak = 0
    return f


def scan_project(work: Path, tasks: Iterable[dict] = (), now: Optional[float] = None) -> list[Finding]:
    out: list[Finding] = []
    for d in sorted(work.iterdir()) if work.is_dir() else []:
        out += scan_attempt(d, now=now)
    for t in tasks:
        out += scan_history(t)
    return sorted(out, key=lambda x: x.ts)


class SentinelThread(threading.Thread):
    """Runs during a run: scans every `interval` s, reports each new finding once."""

    def __init__(self, work: Path, sink: Path, report: Callable[[Finding], None], tasks: Callable[[], list[dict]],
                 interval: float = 30):
        super().__init__(daemon=True, name="sentinel")
        self.work, self.sink, self.report, self.tasks, self.interval = work, sink, report, tasks, interval
        self.halt = threading.Event()
        self.lock = threading.Lock()   # the orchestrator also calls scan_once() between tasks
        self.seen: set[str] = set()
        self.started_at = datetime.now(timezone.utc).timestamp()
        for x in scan_project(self.work, self.tasks()):   # what is already recorded is not news
            if x.rule not in RUN_WIDE:
                self.seen.add(x.key)

    def scan_once(self) -> list[Finding]:
        with self.lock:
            new = []
            for x in scan_project(self.work, self.tasks(), now=datetime.now(timezone.utc).timestamp()):
                if x.rule in RUN_WIDE and x.ts < self.started_at:
                    continue   # seen in an earlier run; report it again only if it happens in this one
                if x.key not in self.seen:
                    self.seen.add(x.key)
                    new.append(x)
            if new:
                self.sink.parent.mkdir(parents=True, exist_ok=True)
                with self.sink.open("a", encoding="utf-8") as fh:
                    for x in new:
                        fh.write(json.dumps(asdict(x)) + "\n")
        for x in new:
            self.report(x)
        return new

    def run(self) -> None:
        while not self.halt.wait(self.interval):
            try:
                self.scan_once()
            except Exception as e:  # the watchdog must never disturb the run
                log.warning("sentinel scan failed: %s", e)
