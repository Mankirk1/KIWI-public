"""The outline oracle: executable checks taken from the owner's outline, run against the code
after its tests pass, so wrong values and missing behaviour are caught even when the tests
agree with the mistake (runs 1-2: wrong locked values; run 4: tracebacks and no readable
output that no test covered).

  plan time   the planner model turns the outline into checks (prompts.CHECKS_*). A deterministic
              validator refuses any expected number that is not in the outline or the check's own
              inputs: expectations are the outline's formula applied to the inputs, never a number
              the model worked out. Saved in <data_dir>/projects/<repo>/checks.json (edit it to
              fix or disable a check) and shown at the plan gate.
  attempts    each task's checks are given to the coder up front; after the tests pass, the
              checks run in the same sandbox container (sandbox_scripts/oracle_runner.py), with a
              check for imports of names that do not exist. A failing check of this task, or of
              a finished task (a regression), sends the attempt back like a failing test.
  mutation    sandbox_scripts/mutation_runner.py plants small errors in the task's code and reruns
              the tests: surviving mutants mean weak tests. Reported, never blocking.
"""
from __future__ import annotations

import ast
import json
import math
import re
import shlex
import time
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .util import atomic_write_text, log, truncate_middle

SCRIPTS = Path(__file__).parent / "sandbox_scripts"
ORACLE_MARK = "AGENTBOX-ORACLE "
MUTATION_MARK = "AGENTBOX-MUTATION "


class CheckDraft(BaseModel):
    """What the planner model writes for one check."""
    id: str
    task: str                                  # the task whose work makes it pass
    kind: Literal["value", "raises", "command"]
    source: str                                # the outline's words it comes from
    imports: list[str] = Field(default_factory=list)
    expr: str = ""                             # value/raises: Python expression run in the sandbox
    expect: str = ""                           # value: arithmetic or literal from the outline
    rel: float = 1e-9
    error: str = ""                            # raises: exception class name
    run: str = ""                              # command: command line
    exit_code: Optional[int] = None
    stdout: Literal["any", "json_object", "text", "empty"] = "any"
    stderr_lines: Optional[int] = None
    contains: list[str] = Field(default_factory=list)


class CheckDraftSet(BaseModel):
    checks: list[CheckDraft] = Field(min_length=1)


class Check(CheckDraft):
    enabled: bool = True
    expect_value: Any = None                   # filled in by validation (safe evaluation of `expect`)

    def brief(self) -> str:
        if self.kind == "value":
            return f"{self.expr} == {self.expect}" + (f" (rel {self.rel:g})" if isinstance(self.expect_value, float) else "")
        if self.kind == "raises":
            return f"{self.expr} raises {self.error}"
        want = [f"exit {self.exit_code}"] if self.exit_code is not None else []
        want += {"json_object": ["stdout is one JSON object"], "text": ["stdout is readable text, not JSON"],
                 "empty": ["stdout is empty"]}.get(self.stdout, [])
        want += [f"{self.stderr_lines} line(s) on stderr"] if self.stderr_lines is not None else []
        want += [f"stdout contains {c!r}" for c in self.contains]
        return f"`{self.run}`: " + ", ".join(want)


# ---------------------------------------------------------------------- safe arithmetic
_FUNCS = {"sqrt": math.sqrt, "exp": math.exp, "log": math.log, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "abs": abs, "min": min, "max": max}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b,
        ast.FloorDiv: lambda a, b: a // b}


def safe_eval(text: str) -> Any:
    """Evaluate an expected value: numbers, strings, booleans, + - * / ** % //, pi, e and a few
    math functions. Nothing else (no names, attributes or calls of the project)."""
    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str, bool)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else +v
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"not allowed in an expected value: {ast.dump(n)[:80]}")
    return ev(ast.parse(text.strip(), mode="eval"))


_INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "perl", "ruby"}
_NUM = re.compile(r"(?<![\w.])\d[\d_]*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text.replace(",", "")):
        try:
            out.append(float(m.group().replace("_", "")))
        except ValueError:
            pass
    return out


def _known(v: float, known: list[float]) -> bool:
    if v.is_integer() and abs(v) < 10:
        return True
    return any(math.isclose(v, k * f, rel_tol=1e-9, abs_tol=1e-12)
               for k in known for f in (1, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9))


def validate_checks(checks: list[Check], outline: str, task_ids: set[str]) -> list[str]:
    """Problems with checks written by the model (fed back to it for a repair). Also fills in
    each check's `expect_value`."""
    problems: list[str] = []
    seen: set[str] = set()
    outline_nums = _numbers(outline)
    for c in checks:
        where = f"check {c.id}"
        if c.id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(c.id)
        if c.task not in task_ids:
            problems.append(f"{where}: task '{c.task}' is not one of {sorted(task_ids)}")
        if c.kind in ("value", "raises"):
            try:
                ast.parse(c.expr.strip(), mode="eval")
            except SyntaxError:
                problems.append(f"{where}: expr is not a Python expression: {c.expr!r}")
                continue
            if not c.imports:
                problems.append(f"{where}: list the modules to import in imports")
            top = {m.split(".")[0] for m in c.imports}
            if c.expr and not any(re.match(rf"\s*{re.escape(t)}\b", c.expr) for t in top):
                problems.append(f"{where}: expr must start with an imported module, e.g. {sorted(top)[0] if top else 'pkg'}.module.name(...)")
        if c.kind == "value":
            try:
                c.expect_value = safe_eval(c.expect)
            except Exception as e:
                problems.append(f"{where}: expect must be arithmetic on numbers or a literal: {e}")
                continue
            if isinstance(c.expect_value, str):
                if c.expect_value not in outline:
                    problems.append(f"{where}: the string {c.expect_value!r} does not appear in the outline")
            elif isinstance(c.expect_value, (int, float)) and not isinstance(c.expect_value, bool):
                known = outline_nums + _numbers(c.expr)
                bad = [n for n in _numbers(c.expect) if not _known(n, known)]
                if bad:
                    problems.append(f"{where}: {bad} in expect appear neither in the outline nor in expr: write the "
                                    "outline's formula applied to the inputs, not a number you computed")
            c.rel = min(max(c.rel, 1e-12), 1e-3)
        elif c.kind == "raises":
            if not re.fullmatch(r"[A-Za-z_]\w*", c.error or ""):
                problems.append(f"{where}: error must be an exception class name such as ValueError")
        elif c.kind == "command":
            try:
                words = shlex.split(c.run)
            except ValueError:
                words = c.run.split()
            if words[:1] and words[0] in _INTERPRETERS and any(w in ("-c", "-e") for w in words[1:3]):
                problems.append(f"{where}: run the project's own command, not inline code ({words[0]} {words[1]} ...)")
            if words[:1] and words[0] in _INTERPRETERS and words[1:2] == ["-m"] and len(words) > 2 \
                    and not re.search(rf"-m\s+{re.escape(words[2])}(?![\w.-])", outline):
                problems.append(f"{where}: the outline never runs `{' '.join(words[:3])}`")
            if any(w in ("|", "||", "&&", ";", ">", ">>", "<") for w in words):
                problems.append(f"{where}: one command, no shell syntax (pipes, ;, &&, redirections)")
            first = (words or [""])[0]
            if not first or not re.search(rf"(?<![\w-]){re.escape(first)}(?![\w-])", outline):
                problems.append(f"{where}: the command {first!r} does not appear in the outline")
            if c.exit_code is None and c.stdout == "any" and c.stderr_lines is None and not c.contains:
                problems.append(f"{where}: say what to expect (exit_code, stdout, stderr_lines or contains)")
            if any(s not in outline for s in c.contains):
                problems.append(f"{where}: every 'contains' string must appear in the outline")
    return problems


def extract_checks(llm, outline: str, spec: str, tasks: list[dict], repairs: int = 2) -> tuple[list[Check], list[str]]:
    """Checks from the model, one task at a time (short answers: one answer for a whole outline
    took over 30 minutes on the CPU). Problems go back to the model for repair; checks still
    invalid after that are dropped (returned as the second value) instead of failing the set."""
    from . import prompts
    task_ids = {t["id"] for t in tasks}
    listing = json.dumps([{k: t.get(k) for k in ("id", "title", "kind")} for t in tasks], indent=1)
    checks: list[Check] = []
    dropped: list[str] = []
    for t in tasks:
        if t.get("kind") in ("scaffold", "docs"):   # no behaviour of their own to check
            continue
        user = prompts.CHECKS_USER.format(
            outline=outline, spec=truncate_middle(spec, 10000), tasks=listing, id=t["id"], title=t.get("title", ""),
            description=t.get("description", ""), criteria="\n".join(f"- {c}" for c in t.get("acceptance_criteria", [])))
        ask, good, bad = user, [], []
        for round_ in range(repairs + 1):
            t0 = time.time()
            try:
                drafts = llm.structured("checker", prompts.CHECKS_SYSTEM, ask, CheckDraftSet, max_repairs=1).checks
            except Exception as e:
                bad = [f"{t['id']}: no checks ({e})"]
                break
            good, bad = [], []
            for d in drafts:
                c = Check.model_validate({**d.model_dump(), "task": t["id"]})
                problems = validate_checks([c], outline, task_ids)
                (bad.extend(problems) if problems else good.append(c))
            log.info("checks for %s: %d valid, %d problem(s) (%.0fs)", t["id"], len(good), len(bad), time.time() - t0)
            if not bad or round_ == repairs:
                break
            ask = (user + "\n\nYour previous checks:\n" + json.dumps([d.model_dump() for d in drafts], indent=1)
                   + "\n\nThese problems were found; return the complete corrected list:\n" + "\n".join(f"- {b}" for b in bad))
        checks += good
        dropped += [f"{t['id']}: {b}" for b in bad]
    for i, c in enumerate(checks, 1):   # stable, unique ids across tasks
        c.id = f"C{i}"
    return checks, dropped


# ---------------------------------------------------------------------- storage
def checks_path(project_dir: Path) -> Path:
    return project_dir / "checks.json"


def load_checks(project_dir: Path) -> Optional[list[Check]]:
    p = checks_path(project_dir)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    checks = [Check.model_validate(c) for c in data.get("checks", [])]
    for c in checks:  # the owner may have edited the file: recompute
        if c.kind == "value":
            try:
                c.expect_value = safe_eval(c.expect)
            except Exception as e:
                log.warning("check %s disabled: its expect does not evaluate (%s)", c.id, e)
                c.enabled = False
    return checks


def save_checks(project_dir: Path, checks: list[Check]) -> Path:
    p = checks_path(project_dir)
    atomic_write_text(p, json.dumps({"note": "Outline checks (agentbox/oracle.py). Fix a wrong one or set "
                                             "\"enabled\": false; changes apply from the next attempt.",
                                     "checks": [c.model_dump(exclude={"expect_value"}) for c in checks]}, indent=2))
    return p


def task_checks(checks: list[Check], task_id: str) -> list[Check]:
    return [c for c in checks if c.enabled and c.task == task_id]


def coder_text(checks: list[Check], task_id: str) -> str:
    mine = task_checks(checks, task_id)
    if not mine:
        return ""
    return ("Outline checks for this task: after the tests pass, the code is also run against these checks taken "
            "from OUTLINE.md, and the attempt fails if one fails. Write tests and code that agree with them:\n"
            + "\n".join(f"- {c.id}: {c.brief()}   (outline: {truncate_middle(c.source, 160)})" for c in mine))


# ---------------------------------------------------------------------- sandbox side
def stage(tree: Path, checks: list[Check], changed_py: list[str], mutate: list[str], test_command: str,
          mutation_max: int = 20, mutation_budget_s: int = 180) -> str:
    """Put the runner scripts and their inputs into a test tree (never into git); returns the
    shell lines to run after the tests (they never change the tests' exit code)."""
    d = tree / ".agentbox"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("oracle_runner.py", "mutation_runner.py"):
        (d / name).write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")
    (d / "oracle.json").write_text(json.dumps({
        "checks": [c.model_dump() for c in checks if c.enabled], "changed": changed_py}), encoding="utf-8")
    lines = ["python3 .agentbox/oracle_runner.py .agentbox/oracle.json || true"]
    if mutate:
        (d / "mutation.json").write_text(json.dumps({"files": mutate, "test": test_command, "max": mutation_max,
                                                     "budget_s": mutation_budget_s}), encoding="utf-8")
        lines.append("if [ $rc -eq 0 ]; then python3 .agentbox/mutation_runner.py .agentbox/mutation.json || true; fi")
    return "\n".join(lines)


def take_marked(output: str, mark: str) -> tuple[Optional[dict], str]:
    """The last `mark{json}` line of a test run's output, and the output without such lines."""
    found, kept = None, []
    for line in output.splitlines():
        if line.startswith(mark):
            try:
                found = json.loads(line[len(mark):])
            except json.JSONDecodeError:
                pass
        else:
            kept.append(line)
    return found, "\n".join(kept)


class Verdict(BaseModel):
    """What the oracle and the mutation check found in one attempt."""
    ran: bool = False
    passed: list[str] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)        # enforced checks that did not pass
    later: int = 0                                           # checks of tasks not done yet
    dangling: list[str] = Field(default_factory=list)
    mutation: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.failed and not self.dangling

    def line(self) -> str:
        parts = []
        if self.ran:
            parts.append(f"Outline checks: {len(self.passed)} pass, {len(self.failed)} fail"
                         + (f", {self.later} for later tasks" if self.later else ""))
        if self.dangling:
            parts.append(f"{len(self.dangling)} import(s) of names that don't exist")
        m = self.mutation
        if m and m.get("tested"):
            parts.append(f"Mutation check: tests caught {m['killed']}/{m['tested']} planted errors")
        return " · ".join(parts)

    def problems(self) -> list[str]:
        out = [f"{f['id']} ({f.get('brief', '')}): {f.get('detail') or f.get('status')}" for f in self.failed]
        return out + self.dangling

    def survivors(self) -> list[str]:
        return [f"{s['file']}:{s['line']}  {s['change']}  ({s.get('code', '').strip()[:100]})"
                for s in (self.mutation or {}).get("survived", [])]

    def feedback(self) -> str:
        lines = []
        if self.failed:
            lines.append("The tests pass, but the code disagrees with the owner's outline (OUTLINE.md) in these checks:")
            lines += [f"- {p}" for p in self.problems()[:len(self.failed)]]
        if self.dangling:
            lines.append("These imports refer to names that do not exist (remove or fix them, also type-only imports):")
            lines += [f"- {d}" for d in self.dangling]
        return "\n".join(lines)


def judge(oracle: Optional[dict], mutation: Optional[dict], checks: list[Check], enforce: set[str]) -> Verdict:
    """Enforce checks of the tasks in `enforce` (this task and the finished ones)."""
    v = Verdict(mutation=mutation)
    if oracle is None:
        return v
    v.ran = True
    by_id = {c.id: c for c in checks}
    for r in oracle.get("results", []):
        c = by_id.get(r.get("id"))
        if c is None or not c.enabled:
            continue
        if c.task not in enforce:
            v.later += 1
        elif r.get("status") == "pass":
            v.passed.append(c.id)
        else:
            v.failed.append({**r, "brief": c.brief(), "task": c.task})
    v.dangling = list(oracle.get("dangling", []))
    return v
