"""The orchestrator: a deterministic state machine that drives the models.

new project:  outline -> spec -> plan -> repo + issues -> work()
work():       reconcile with GitHub -> repeat { pick ready task -> run_task() } until done/budget/STOP
run_task():   [tests-first phase] -> up to N attempts of
              { worker in sandbox -> sync + policy checks -> commit -> tests in fresh sandbox
                -> push + PR -> reviewer model -> CI -> merge }
              -> done | needs-human
"""
from __future__ import annotations

import fcntl
import json
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import BOT_MARKER
from .config import Config
from .control import (FRESH_TESTS_CHOICE, GATE_LABELS, LOCKED_TEST_CHOICES, MERGE_CHOICES, PLAN_CHOICES,
                      PUSH_CHOICES, RETRY_CHOICES, REVIEW_BLOCKED_CHOICES, SIMPLE_PUSH_CHOICES, TASK_CHOICES,
                      TESTS_CHOICES, Control, Decision, StopRun)
from .digest import build_digest, save_digest
from .github_api import GitHub
from .guards import GuardError, OllamaGuard
from .gitops import Git
from .http import HTTPError
from .llm import LLMClient
from .oracle import (MUTATION_MARK, ORACLE_MARK, Check, Verdict, checks_path, coder_text, extract_checks, judge,
                     load_checks, save_checks, stage, take_marked)
from .planner import Plan, Planner, TaskSpec, unverified_numbers
from .report import build_report, notify, save_report
from .reviewer import review_change
from .sandbox import Sandbox, TestResult
from .sentinel import Finding, SentinelThread
from .state import ProjectState, RunRecord, StackState, StateStore, TaskState
from .util import atomic_write_text, log, now_iso, slugify, truncate_middle
from .workspace import (export_tree, init_snapshot_git, is_test_path, non_test_changes,
                        protected_violations, runner_config_violations, sync_back)

TEMPLATES = Path(__file__).parent / "templates"
TDD_KINDS = {"feature", "refactor"}


__all__ = ["Orchestrator", "StopRun", "run_lock"]


@contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "agentbox.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another agentbox run is already in progress (data/agentbox.lock is held)")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _one_line(cmd: str) -> str:
    return " && ".join(l.strip() for l in cmd.strip().splitlines() if l.strip())


def render(template: str, stack: StackState) -> str:
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    lint_line = f"- Lint: `{stack.lint_command}`\n" if stack.lint_command else ""
    return (text.replace("{{SETUP_COMMAND}}", _one_line(stack.setup_command) or "true")
                .replace("{{TEST_COMMAND}}", _one_line(stack.test_command))
                .replace("{{LINT_LINE}}", lint_line))


def issue_body(task: TaskState | TaskSpec, dep_refs: list[str]) -> str:
    lines = [task.description.strip(), "", "### Acceptance criteria"]
    lines += [f"- [ ] {c}" for c in task.acceptance_criteria]
    if dep_refs:
        lines += ["", "### Depends on"] + [f"- {d}" for d in dep_refs]
    if task.files_hint:
        lines += ["", "### Likely files", ", ".join(f"`{f}`" for f in task.files_hint)]
    lines += ["", "---", f"<sub>Task {task.id} · kind: {task.kind} · managed by agentbox. "
              "Label `agent:todo` queues an issue for the agent.</sub>", f"<!-- agentbox:task={task.id} -->"]
    return "\n".join(lines)


def mentions_github(text: str) -> bool:
    """Does the text refer to another issue or pull request (#12, .../pull/12, .../issues/12)?"""
    return bool(re.search(r"(?<![\w&/])#\d+\b|/(?:pull|issues)/\d+", text or ""))


def _brief(text: str, n: int = 160) -> str:
    """One-line digest of a note: the most informative line, whitespace collapsed."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip() and not set(l.strip()) <= set("=-~^ ")]
    key = None
    for markers in (("Error:", "Error ", "error:"), ("assert", "Assert"), ("FAIL",)):
        key = next((l for l in reversed(lines) if any(k in l for k in markers)), None)
        if key:
            break
    out = key or (lines[0] if lines else "")
    if not key and out.endswith(":") and len(lines) > 1:
        out = f"{out} {lines[1]}"
    return (out[: n - 1] + "…") if len(out) > n else out


def parse_issue_body(body: str) -> tuple[str, list[str]]:
    """Split an issue body into (description, acceptance criteria). Understands the
    layout agentbox writes (sections, footer after ---) and free-form human issues
    (any checkbox line is a criterion)."""
    crit: list[str] = []
    desc: list[str] = []
    section = ""
    for line in (body or "").splitlines():
        s = line.strip()
        if s == "---" or s.startswith("<!-- agentbox"):
            break
        if s.startswith("### "):
            section = s[4:].strip().lower()
            continue
        if s.startswith(("- [ ]", "- [x]", "- [X]", "* [ ]")):
            crit.append(s[5:].strip())
        elif section in ("", "description", "details"):
            desc.append(line)
    return "\n".join(desc).strip(), crit


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = LLMClient(cfg.llm)
        self.gh = GitHub(cfg.github.admin_token, cfg.github.api_url)
        self.sandbox = Sandbox(cfg)
        self.store = StateStore(cfg.projects_dir)
        self.guard = OllamaGuard(cfg.guards.ollama, cfg.guards.restore_ollama)
        self.ctl = Control(cfg)
        self.t0 = time.time()
        self.started_iso = now_iso()   # the digest covers the runs this command started
        self.consecutive_failures = 0
        self.last_stop = ""
        self.sentinel: Optional[SentinelThread] = None

    # ================================================================== budget
    def stop_check(self) -> None:
        """Also called while waiting for your answer or paused."""
        if self.cfg.budget.stop_path.exists():
            raise StopRun(f"STOP requested ({self.cfg.budget.stop_path})")
        wall = self.cfg.budget.max_wall_hours
        if wall and time.time() - self.t0 >= wall * 3600:
            raise StopRun(f"wall-clock limit of {wall:g}h reached")

    def check_budget(self) -> None:
        """Checkpoint between steps: STOP, pause, time budget, failure streak. Time spent
        paused or waiting for your answers is not charged to the budget."""
        b = self.cfg.budget
        self.stop_check()
        self.ctl.wait_if_paused(self.stop_check)
        if (time.time() - self.t0 - self.ctl.waited_s) / 3600 >= b.max_hours:
            raise StopRun(f"time budget of {b.max_hours}h reached")
        if b.max_wall_hours:
            # An attempt can take the coder's and the test runner's full time limits; start one
            # only if it ends before the wall-clock limit (the nightly task is killed after it).
            reserve_h = (self.cfg.sandbox.worker_timeout_min + self.cfg.sandbox.test_timeout_min) / 60
            if (time.time() - self.t0) / 3600 + reserve_h >= b.max_wall_hours:
                raise StopRun(f"wall-clock limit of {b.max_wall_hours:g}h: not enough time left for another "
                              f"attempt (up to {reserve_h * 60:.0f} min)")
        if self.consecutive_failures >= b.max_consecutive_failures:
            raise StopRun(f"{self.consecutive_failures} tasks failed in a row - stopping to avoid wasting the night")

    def check_guards(self, when: str) -> None:
        try:
            self.guard.enforce(when)
        except GuardError as e:
            raise StopRun(f"Ollama guard: {e}") from None

    def start(self, command: str) -> None:
        """Call once when a command starts (after taking the run lock)."""
        n = self.ctl.expire_pending("the agent restarted before you answered")
        if n:
            log.info("expired %d unanswered request(s) from an earlier run", n)
        self.ctl.set_status("starting", detail=command, command=command, project="", task="",
                            run_started=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.ctl.event(f"agentctl {command} started (mode: {self.ctl.mode()})", kind="run")

    def finish(self) -> None:
        """Call once when the whole command ends (restarts Ollama if the guard stopped it)."""
        self.guard.restore_all()
        self.ctl.set_status("idle", detail=self.last_stop, task="")
        self.send_digest()

    def send_digest(self) -> Optional[Path]:
        """One summary of everything this command did: saved, in the panel, and one notification."""
        try:
            d = build_digest(self.cfg, self.started_iso)
            if d is None:
                return None
            path = save_digest(self.cfg, d)
            log.info("digest written to %s", path)
            self.ctl.event(f"Digest: {d.headline}", kind="run", level="warn" if d.handed else "info")
            url = self.cfg.control.public_url.rstrip("/")
            body = "\n".join([d.headline + (f" · Sentinel: {d.sentinel_high} high" if d.sentinel_high else "")]
                             + (d.merged + d.handed)[:6] + (["…"] if len(d.merged + d.handed) > 6 else []))
            notify(self.cfg.notify.ntfy_url, "agentbox digest", body, click=f"{url}/#digest" if url else "",
                   priority=self.ctl.priority(), tags="sunrise")
            return path
        except Exception as e:  # a summary must never break the end of a run
            log.warning("digest failed: %s", e)
            return None

    @staticmethod
    def _web(st: ProjectState, path: str = "") -> str:
        return f"https://github.com/{st.owner}/{st.repo}" + (f"/{path}" if path else "")

    # ================================================================== helpers
    def git_for(self, st: ProjectState) -> Git:
        g = self.cfg.github
        git = Git(self.cfg.repos_dir / st.repo, g.admin_token, g.git_author_name, g.git_author_email)
        if not git.exists():
            git.clone(g.remote(st.owner, st.repo))
        return git

    def _label(self, st: ProjectState, task: TaskState, status_label: str) -> None:
        if task.issue:
            try:
                self.gh.set_status_label(st.owner, st.repo, task.issue, status_label)
            except HTTPError as e:
                log.warning("could not label #%s: %s", task.issue, e)

    def _comment(self, st: ProjectState, number: Optional[int], body: str) -> None:
        if number:
            try:
                self.gh.comment(st.owner, st.repo, number, body)
            except HTTPError as e:
                log.warning("could not comment on #%s: %s", number, e)

    # ================================================================== new project
    def plan_project(self, outline: str, name: Optional[str] = None) -> tuple[str, Plan]:
        planner = Planner(self.cfg, self.llm)
        spec = planner.write_spec(outline)
        plan = planner.make_plan(outline, spec)
        if name:
            plan.repo_name = name
        return spec, plan

    @staticmethod
    def load_plan(plan_dir: Path, outline: str) -> tuple[str, Plan]:
        """Spec and plan from an earlier `plan-only` run (its project folder), made from this outline."""
        saved = plan_dir / "outline.md"
        if not (plan_dir / "plan.json").is_file() or not (plan_dir / "SPEC.md").is_file():
            raise ValueError(f"no plan.json and SPEC.md in {plan_dir}")
        if not saved.is_file() or saved.read_text(encoding="utf-8").strip() != outline.strip():
            raise ValueError(f"the plan in {plan_dir} was made from a different outline; plan again or edit it back")
        return ((plan_dir / "SPEC.md").read_text(encoding="utf-8"),
                Plan.model_validate_json((plan_dir / "plan.json").read_text(encoding="utf-8")))

    def new_project(self, outline_path: Path, name: Optional[str] = None, plan_only: bool = False,
                    use_plan: Optional[Path] = None) -> Optional[ProjectState]:
        outline = outline_path.read_text(encoding="utf-8")
        owner = self.cfg.github.owner
        planning_input = outline
        while True:
            self.ctl.set_status("planning", project=name or outline_path.stem, task="",
                                detail="writing the specification and the task plan")
            reuse_from = use_plan
            if use_plan:
                spec, plan = self.load_plan(use_plan, planning_input)
                if name:
                    plan.repo_name = name
                self.ctl.event(f"Reusing the plan from {use_plan} (no new planning)", kind="plan")
                use_plan = None  # a re-plan with your notes plans from scratch
            else:
                self.ctl.event(f"Planning {outline_path.name}", kind="plan")
                spec, plan = self.plan_project(planning_input, name)

            repo_name = plan.repo_name
            if not plan_only:
                for i in range(2, 20):
                    if self.gh.get_repo(owner, repo_name) is None:
                        break
                    repo_name = f"{plan.repo_name}-{i}"
                plan.repo_name = repo_name

            pdir = self.store.project_dir(repo_name)
            atomic_write_text(pdir / "outline.md", planning_input)
            atomic_write_text(pdir / "SPEC.md", spec)
            atomic_write_text(pdir / "plan.json", plan.model_dump_json(indent=2))
            log.info("plan: %s (%s) with %d tasks -> %s", repo_name, plan.stack.language, len(plan.tasks), pdir)
            self.ctl.event(f"Plan ready: {repo_name}, {len(plan.tasks)} tasks", kind="plan", project=f"{owner}/{repo_name}")
            check = unverified_numbers(plan, planning_input, spec)
            checks, dropped = self._plan_checks(pdir, planning_input, spec, [t.model_dump() for t in plan.tasks], reuse_from)
            if plan_only:
                for t in plan.tasks:
                    log.info("  %s [%s] %s  deps=%s", t.id, t.kind, t.title, ",".join(t.depends_on) or "-")
                for c in check:
                    log.warning("  value not in the outline/spec, check it: %s", c)
                for c in checks:
                    log.info("  check %s [%s] %s", c.id, c.task, c.brief())
                for x in dropped:
                    log.warning("  check dropped: %s", x)
                return None

            d = self.ctl.ask(
                "plan", f"New project {owner}/{repo_name}", choices=PLAN_CHOICES, project=f"{owner}/{repo_name}",
                summary=[plan.description, f"{len(plan.tasks)} tasks · {plan.stack.language} · tests: {plan.stack.test_command}",
                         f"Creates a {'private' if self.cfg.github.private else 'PUBLIC'} repository with one issue per task"],
                details={"Tasks": [f"{t.id} [{t.kind}] {t.title}" + (f"  (after {', '.join(t.depends_on)})" if t.depends_on else "")
                                   for t in plan.tasks],
                         **({"Values to check (not in your outline; the planner computed them)": check} if check else {}),
                         **({f"Outline checks ({len(checks)}): the code must pass them; edit "
                             f"{checks_path(pdir)} to fix one": [f"{c.id} [{c.task}] {c.brief()}" for c in checks]}
                            if checks else {}),
                         **({"Checks dropped (invalid after repair)": dropped} if dropped else {}),
                         "Specification": spec},
                stop_check=self.stop_check)
            if d.choice == "approve":
                break
            if d.choice == "cancel":
                self.ctl.event(f"Project {repo_name} cancelled", kind="plan", level="warn")
                log.warning("project cancelled from the control panel; nothing was created on GitHub")
                self.last_stop = f"project {repo_name} cancelled before anything was created"
                return None
            planning_input = (planning_input.rstrip() + "\n\n## Owner feedback on the previous plan (follow it)\n\n"
                              + d.note.strip() + "\n")
            log.info("re-planning with the owner's notes")

        g = self.cfg.github
        self.gh.create_repo(owner, g.owner_type, repo_name, plan.description, g.private)
        self.gh.ensure_labels(owner, repo_name)

        st = ProjectState(repo=repo_name, owner=owner, description=plan.description,
                          stack=StackState(**plan.stack.model_dump()))
        git = Git(self.cfg.repos_dir / repo_name, g.admin_token, g.git_author_name, g.git_author_email)
        if git.dir.exists():
            shutil.rmtree(git.dir)
        git.init_new(g.remote(owner, repo_name))
        self._write_scaffold(git.dir, st, plan, spec, planning_input)
        git.commit("chore: project specification and agent rules\n\nCreated by agentbox from the project outline.")
        git.push("main")
        self.gh.protect_branch(owner, repo_name, "main")

        st.log_issue = self.gh.create_issue(owner, repo_name, "🤖 Agent run log",
                                            "Run reports from agentbox are posted here as comments.", ["agent:log"])
        id_to_issue: dict[str, int] = {}
        for spec_task in plan.tasks:
            refs = [f"#{id_to_issue[d]} ({d})" for d in spec_task.depends_on if d in id_to_issue]
            number = self.gh.create_issue(owner, repo_name, f"{spec_task.id}: {spec_task.title}",
                                          issue_body(spec_task, refs), ["agent:todo"])
            id_to_issue[spec_task.id] = number
            st.tasks.append(TaskState(**spec_task.model_dump(), issue=number))
        self.store.save(st)
        log.info("created %d issues in https://github.com/%s/%s", len(st.tasks), owner, repo_name)
        self.work(st, f"new {outline_path.name}")
        return st

    def _write_scaffold(self, repo_dir: Path, st: ProjectState, plan: Plan, spec: str, outline: str) -> None:
        (repo_dir / "SPEC.md").write_text(spec.strip() + "\n", encoding="utf-8")
        (repo_dir / "OUTLINE.md").write_text(outline.strip() + "\n", encoding="utf-8")
        (repo_dir / "AGENTS.md").write_text(render("AGENTS.md", st.stack), encoding="utf-8")
        gi = TEMPLATES / f"gitignore-{st.stack.language}"
        if gi.exists():
            shutil.copy(gi, repo_dir / ".gitignore")
        readme = (f"# {plan.repo_name}\n\n{plan.description}\n\n"
                  "This project is being built by an autonomous agent (agentbox) from the specification in "
                  "[SPEC.md](SPEC.md). Work is tracked in the issues; each change arrives as a reviewed pull request.\n")
        (repo_dir / "README.md").write_text(readme, encoding="utf-8")

    def _install_ci(self, st: ProjectState, git: Git) -> None:
        """Add the CI workflow once the scaffold exists (so CI does not fail on an empty repo)."""
        if self.cfg.workflow.ci.mode == "off":
            return
        wf = git.dir / ".github" / "workflows" / "ci.yml"
        if wf.exists():
            return
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(render("ci.yml", st.stack), encoding="utf-8")
        if git.commit("ci: run the test suite on every push and pull request"):
            d = self.ctl.ask("push", f"Add the CI workflow to main of {st.slug}", choices=SIMPLE_PUSH_CHOICES,
                             project=st.slug, summary=["Pushes .github/workflows/ci.yml straight to main",
                                                       "CI then runs the tests on every pull request"],
                             diff=git.diff("HEAD~1"), stop_check=self.stop_check)
            if d.choice != "approve":
                git.reset_hard("origin/main")
                self.ctl.event("CI workflow not installed (your choice)", kind="push", project=st.slug)
                return
            git.push("main")
            self.ctl.event("CI workflow installed on main", kind="push", project=st.slug)
            log.info("CI workflow installed")

    def _ensure_outline(self, st: ProjectState) -> None:
        """Projects created before OUTLINE.md existed get it, and the current agent rules, on main:
        the coder and the reviewer read the owner's data tables and exact names from it."""
        src = self.store.project_dir(st.repo) / "outline.md"
        if not src.is_file():
            return
        git = self.git_for(st)
        git.sync_main()
        changed = []
        if not (git.dir / "OUTLINE.md").exists():
            (git.dir / "OUTLINE.md").write_text(src.read_text(encoding="utf-8").strip() + "\n", encoding="utf-8")
            changed.append("OUTLINE.md")
        agents, rules = git.dir / "AGENTS.md", render("AGENTS.md", st.stack)
        if agents.exists() and agents.read_text(encoding="utf-8") != rules:
            agents.write_text(rules, encoding="utf-8")
            changed.append("AGENTS.md")
        if not changed or not git.commit("docs: add the owner's outline (OUTLINE.md) and update the agent rules"):
            return
        d = self.ctl.ask("push", f"Add {' and '.join(changed)} to main of {st.slug}", choices=SIMPLE_PUSH_CHOICES,
                         project=st.slug, summary=["Pushes straight to main: " + ", ".join(changed),
                                                   "The coder and reviewer then read your outline's exact data from OUTLINE.md"],
                         diff=git.diff("HEAD~1"), stop_check=self.stop_check)
        if d.choice != "approve":
            git.reset_hard("origin/main")
            self.ctl.event("OUTLINE.md not added (your choice)", kind="push", project=st.slug)
            return
        git.push("main")
        self.ctl.event(f"{' and '.join(changed)} updated on main", kind="push", project=st.slug)
        log.info("%s updated on main", ", ".join(changed))

    # ================================================================== outline checks
    def _plan_checks(self, pdir: Path, outline: str, spec: str, tasks: list[dict],
                     reuse_from: Optional[Path] = None) -> tuple[list[Check], list[str]]:
        """Checks for a new plan (reused from a plan-only folder when there are some), saved in the
        project folder. Never fails the planning: without checks the project still runs."""
        if not self.cfg.workflow.checks.enabled:
            return [], []
        checks = load_checks(reuse_from) if reuse_from else None
        dropped: list[str] = []
        if checks is None:
            self.ctl.set_status("planning", detail="writing outline checks")
            try:
                checks, dropped = extract_checks(self.llm, outline, spec, tasks)
            except Exception as e:
                log.warning("could not write outline checks: %s", e)
                return [], [f"could not write outline checks: {e}"]
        save_checks(pdir, checks)
        log.info("outline checks: %d (%d dropped) -> %s", len(checks), len(dropped), checks_path(pdir))
        return checks, dropped

    def _ensure_checks(self, st: ProjectState) -> None:
        """Projects planned before the checks existed get them on their next run."""
        pdir = self.store.project_dir(st.repo)
        if not self.cfg.workflow.checks.enabled or checks_path(pdir).is_file():
            return
        outline_p, spec_p = pdir / "outline.md", self.cfg.repos_dir / st.repo / "SPEC.md"
        if not outline_p.is_file() or not any(t.status in ("todo", "blocked") for t in st.tasks):
            return
        checks, dropped = self._plan_checks(pdir, outline_p.read_text(encoding="utf-8"),
                                            spec_p.read_text(encoding="utf-8") if spec_p.is_file() else "",
                                            [t.model_dump() for t in st.tasks])
        if checks:
            self.ctl.event(f"Wrote {len(checks)} outline checks for {st.repo}" + (f" ({len(dropped)} dropped)" if dropped else ""),
                           kind="plan", project=st.slug)

    def _checks(self, st: ProjectState) -> list[Check]:
        """Read on every use: you may edit checks.json between attempts."""
        if not self.cfg.workflow.checks.enabled:
            return []
        try:
            return load_checks(self.store.project_dir(st.repo)) or []
        except Exception as e:
            log.warning("checks.json of %s is unreadable, checks are off: %s", st.repo, e)
            return []

    # ================================================================== extend / continue
    def extend_project(self, repo: str, request: str) -> ProjectState:
        st = self.store.load(repo)
        git = self.git_for(st)
        git.sync_main()
        spec_path = git.dir / "SPEC.md"
        spec = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
        existing = [{"id": t.id, "title": t.title, "status": t.status} for t in st.tasks]
        new_tasks = Planner(self.cfg, self.llm).extend(spec, existing, request, {t.id for t in st.tasks})
        id_to_issue = {t.id: t.issue for t in st.tasks if t.issue}
        for spec_task in new_tasks:
            refs = [f"#{id_to_issue[d]} ({d})" for d in spec_task.depends_on if id_to_issue.get(d)]
            number = self.gh.create_issue(st.owner, st.repo, f"{spec_task.id}: {spec_task.title}",
                                          issue_body(spec_task, refs), ["agent:todo"])
            id_to_issue[spec_task.id] = number
            st.tasks.append(TaskState(**spec_task.model_dump(), issue=number))
        self.store.save(st)
        log.info("added %d tasks to %s", len(new_tasks), st.slug)
        self.work(st, f"extend: {request[:60]}")
        return st

    def continue_project(self, repo: str) -> ProjectState:
        st = self.store.load(repo)
        self.work(st, "continue")
        return st

    def reconcile(self, st: ProjectState) -> None:
        """Fold human changes on GitHub (labels, closed issues, new issues) into state."""
        issues = {i["number"]: i for i in self.gh.list_issues(st.owner, st.repo, state="all")}
        for t in st.tasks:
            if t.status == "in_progress":  # leftover from a crash
                t.status = "todo"
            issue = issues.get(t.issue) if t.issue else None
            if not issue:
                continue
            labels = {l["name"] for l in issue.get("labels", [])}
            if issue["state"] == "closed" and t.status not in ("done", "skipped"):
                log.info("issue #%s was closed by a human; marking %s done", t.issue, t.id)
                t.status = "done"
            elif "agent:todo" in labels and t.status in ("needs_human", "blocked"):
                log.info("issue #%s re-queued by a human", t.issue)
                t.status = "todo"
                t.human_feedback_since = t.history[-1].ts if t.history else None
            if t.status in ("todo", "blocked") and issue["state"] == "open":
                self._apply_issue(t, issue)
        known = {t.issue for t in st.tasks if t.issue}
        for num, issue in sorted(issues.items()):
            labels = {l["name"] for l in issue.get("labels", [])}
            if num in known or num == st.log_issue or issue["state"] != "open" or "agent:todo" not in labels:
                continue
            desc, crit = parse_issue_body(issue.get("body") or "")
            st.tasks.append(TaskState(id=f"I{num}", title=issue["title"], description=desc or issue["title"],
                                      acceptance_criteria=crit or ["The behaviour described in the issue works and is covered by tests."],
                                      issue=num, kind="feature"))
            log.info("picked up new human-filed issue #%s", num)
        for t in st.refresh_blocked():
            self._label(st, t, "agent:blocked" if t.status == "blocked" else "agent:todo")
        self.store.save(st)

    @staticmethod
    def _apply_issue(t: TaskState, issue: dict) -> None:
        """The issue text is the task: edits you make on GitHub steer the next attempt."""
        desc, crit = parse_issue_body(issue.get("body") or "")
        if desc:
            t.description = desc
        if crit:
            t.acceptance_criteria = crit
        t.title = issue["title"].split(": ", 1)[1] if issue["title"].startswith(f"{t.id}: ") else issue["title"]

    def _refresh_from_issue(self, st: ProjectState, t: TaskState) -> None:
        """Re-read the issue right before a task starts: it may have been edited during the run."""
        if not t.issue:
            return
        try:
            issue = self.gh.get_issue(st.owner, st.repo, t.issue)
        except HTTPError as e:
            log.warning("could not re-read issue #%s: %s", t.issue, e)
            return
        if issue.get("state") == "open":
            self._apply_issue(t, issue)

    # ================================================================== main loop
    def work(self, st: ProjectState, command: str) -> RunRecord:
        run = RunRecord(run_id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], started=now_iso(), command=command)
        waited0 = self.ctl.waited_s
        st.runs.append(run)
        self.store.save(st)
        self.ctl.set_status("running", project=st.slug, task="", detail=command, run=run.run_id)
        self.ctl.event(f"Run {run.run_id} on {st.slug} ({command})", kind="run", project=st.slug, link=self._web(st))
        try:
            self.check_guards("run start")
            self.reconcile(st)
            self._ensure_outline(st)
            self._ensure_checks(st)
            self._start_sentinel(st)
            while True:
                self.check_budget()
                self.check_guards("before task")
                ready = st.ready_tasks()
                if not ready:
                    remaining = [t for t in st.tasks if t.status in ("todo", "blocked")]
                    needs = [t for t in st.tasks if t.status == "needs_human"]
                    if not remaining and not needs:
                        run.stop_reason = "all tasks finished"
                    elif not remaining:
                        run.stop_reason = f"all workable tasks finished; {len(needs)} need a human"
                    else:
                        run.stop_reason = (f"no task is ready: {len(remaining)} blocked behind "
                                           f"{len(needs)} task(s) that need a human")
                    break
                task = ready[0]
                t_task = time.time()
                try:
                    outcome = self.run_task(st, task)
                finally:
                    run.task_minutes[task.id] = round(run.task_minutes.get(task.id, 0) + (time.time() - t_task) / 60, 1)
                if self.sentinel:
                    self.sentinel.scan_once()   # a fast task may finish between two background scans
                if outcome == "done":
                    run.done.append(task.id)
                    self.consecutive_failures = 0
                elif outcome == "skipped":
                    pass  # you skipped it from the panel: not a failure
                elif outcome == "handoff":
                    run.failed.append(task.id)  # handed to you by your choice or a night timeout: not a failure streak
                else:
                    run.failed.append(task.id)
                    self.consecutive_failures += 1
                for t in st.refresh_blocked():
                    self._label(st, t, "agent:blocked" if t.status == "blocked" else "agent:todo")
                self.store.save(st)
        except StopRun as e:
            run.stop_reason = str(e)
            log.warning("stopping: %s", e)
        except KeyboardInterrupt:
            run.stop_reason = "interrupted"
            log.warning("interrupted")
        except Exception as e:  # keep the report even when something unexpected breaks
            log.exception("run crashed")
            run.stop_reason = f"crashed: {type(e).__name__}: {str(e)[:300]}"
        finally:
            self._stop_sentinel()
            for t in st.tasks:
                if t.status == "in_progress":
                    t.status = "todo"
                    self._label(st, t, "agent:todo")
            run.finished = now_iso()
            run.waited_minutes = round((self.ctl.waited_s - waited0) / 60, 1)
            u = self.llm.usage
            run.usage = {"calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                         "seconds": round(u.seconds), "by_role": u.by_role}
            self.store.save(st)
            text = build_report(st, run)
            path = save_report(self.cfg.reports_dir, st, run, text)
            log.info("report written to %s", path)
            self._comment(st, st.log_issue, text)
            self.last_stop = run.stop_reason
            self.ctl.event(f"Run finished: {len(run.done)} merged, {len(run.failed)} handed to you — {run.stop_reason}",
                           kind="run", project=st.slug, level="warn" if run.failed else "info")
            self.ctl.set_status("idle", project=st.slug, task="", detail=run.stop_reason)
        return run  # the phone hears about it once per command, in the digest (finish)

    # ================================================================== sentinel
    def _start_sentinel(self, st: ProjectState) -> None:
        if not self.cfg.sentinel.enabled:
            return
        work = self.cfg.work_dir / st.repo
        work.mkdir(parents=True, exist_ok=True)
        self.sentinel = SentinelThread(work, self.cfg.control_dir / "sentinel.jsonl",
                                       lambda f: self._sentinel_report(st, f),
                                       lambda: [t.model_dump(mode="json") for t in st.tasks],
                                       self.cfg.sentinel.interval_s)
        self.sentinel.start()

    def _sentinel_report(self, st: ProjectState, f: Finding) -> None:
        if f.severity == "info":
            return  # kept in sentinel.jsonl only: too frequent for the feed
        evidence = f.evidence[0] if f.evidence else ""
        log.warning("sentinel %s %s: %s | %s", f.severity, f.rule, f.summary, evidence)
        self.ctl.event(f"🛰️ Sentinel: {f.summary}", kind="sentinel", level="warn" if f.severity == "high" else "info",
                       project=st.slug, task=f.task)
        rank = {"medium": 1, "high": 2}
        if rank[f.severity] >= {"off": 99, "medium": 1, "high": 2}[self.cfg.sentinel.notify]:
            notify(self.cfg.notify.ntfy_url, f"agentbox sentinel: {f.rule}", f"{f.summary}\n{evidence}",
                   click=self.cfg.control.public_url,
                   priority=self.ctl.priority("high" if f.severity == "high" else "", wake=f.severity == "high"),
                   tags="rotating_light")

    def _stop_sentinel(self) -> None:
        if self.sentinel is None:
            return
        self.sentinel.halt.set()
        try:
            self.sentinel.scan_once()
        except Exception as e:  # never let the watchdog break the end of a run
            log.warning("sentinel final scan failed: %s", e)
        self.sentinel = None

    # ================================================================== one task
    def run_task(self, st: ProjectState, task: TaskState) -> str:
        wf = self.cfg.workflow
        self.ctl.set_status("running", project=st.slug, task=task.id, detail=f"#{task.issue} {task.title}")
        self._refresh_from_issue(st, task)
        reuse = bool(task.locked_tests) and wf.tdd and task.kind in TDD_KINDS
        d = self.ctl.ask(
            "task", f"{task.id}: {task.title}", choices=TASK_CHOICES + ([FRESH_TESTS_CHOICE] if reuse else []),
            project=st.slug, task=task.id,
            summary=[f"Issue #{task.issue} · {task.kind} · {len(task.acceptance_criteria)} acceptance criteria"]
                    + ([f"Earlier attempts: {task.attempts_total}"] if task.attempts_total else [])
                    + ([f"Reuses {len(task.locked_tests)} test file(s) you locked in an earlier run"] if reuse else []),
            details={"Description": task.description, "Acceptance criteria": task.acceptance_criteria,
                     **({"Locked tests from an earlier run": sorted(task.locked_tests)} if reuse else {})},
            links=[{"label": f"Issue #{task.issue}", "url": self._web(st, f"issues/{task.issue}")}] if task.issue else [],
            stop_check=self.stop_check)
        if d.choice == "stop":
            raise StopRun("stopped from the control panel")
        if d.choice == "skip":
            task.status = "needs_human"
            self._label(st, task, "agent:needs-human")
            self._comment(st, task.issue, (f"Skipped: nobody answered the Start question ({d.note})." if d.by == "timeout" else
                                           "Skipped by the project owner from the agentbox control panel."
                                           + (f"\n\n> {d.note}" if d.note else ""))
                          + "\n\nSet the label back to `agent:todo` to queue it again.")
            self.store.save(st)
            self.ctl.event(f"{task.id} skipped", kind="task", project=st.slug, task=task.id)
            return "skipped"
        if d.note:  # kept in the task's history, so it also reaches later runs and the reviewer
            task.record("owner", 0, "owner_note_at_task", d.note)
        if d.choice == "fresh":
            task.locked_tests = {}

        git = self.git_for(st)
        git.sync_main()
        log.info("=== %s #%s: %s", task.id, task.issue, task.title)
        self.ctl.event(f"Started {task.id}: {task.title}", kind="task", project=st.slug, task=task.id,
                       link=self._web(st, f"issues/{task.issue}") if task.issue else "")
        task.status = "in_progress"
        self._label(st, task, "agent:in-progress")
        self.store.save(st)

        branch = f"agent/{task.issue or task.id}-{slugify(task.title, 30)}"
        task.branch = branch
        git.checkout_new(branch, "origin/main")
        base_sha = git.head()
        main_files = set(git.ls_files())
        human = self._human_guidance(st, task)

        def guidance() -> str:
            """Issue comments plus every owner note so far (notes can arrive at any gate)."""
            g = "\n\n".join(x for x in (human, self._owner_notes(task)) if x)
            return g + "\n\n" if g else ""

        feedback = guidance()

        tests_first: Optional[list[str]] = []
        if wf.tdd and task.kind in TDD_KINDS:
            tests_first = self._restore_locked_tests(st, task, git) or self._tests_first(st, task, git, guidance())
        locked = tests_first or []
        rewrites = 0  # tests-first phases re-run because a locked test was reported wrong
        oracle_seen: Optional[list] = None  # the previous attempt's failing checks, to spot a deadlock
        verdict = Verdict()

        last_problem = ""
        owner_handoff = tests_first is None  # handed off at the tests review: no implementation attempts
        if owner_handoff:
            note = task.history[-1].note if task.history else ""
            last_problem = "Handed off by the project owner at the tests review." + (f" Note: {note}" if note else "")
        for attempt in range(1, 0 if owner_handoff else wf.max_attempts_per_task + 1):
            self.check_budget()
            task.attempts_total += 1
            pre = git.head()
            result, notes, changes = self._worker_attempt(st, task, git, "implement", attempt, feedback, locked)
            summary = (result.get("summary") or "").strip()

            # --- the coder reports that a locked test is wrong (instead of bending the code to it)
            reported = str(result.get("locked_test_problem") or "").strip()
            if locked and reported:
                task.record("implement", attempt, "locked_test_problem", reported)
                d = self.ctl.ask(
                    "tests", f"{task.id}: the coder says a locked test is wrong", choices=LOCKED_TEST_CHOICES,
                    project=st.slug, task=task.id,
                    summary=[f"Reported in attempt {attempt}/{wf.max_attempts_per_task}",
                             "Rewrite: the tests are discarded and written again with this report",
                             "Keep: the tests stay; the coder must satisfy them"],
                    details={"Coder's report": reported, "Locked tests": locked},
                    diff=git.diff(base_sha), stop_check=self.stop_check)
                git.reset_hard(pre)
                if d.choice == "handoff":
                    self._owner_said(st, task, attempt, "tests", d)
                    owner_handoff = True
                    last_problem = "Handed off by the project owner: a locked test was reported wrong.\n" + reported
                    break
                if d.note:
                    task.record("owner", attempt, "owner_note_at_tests", d.note)
                if d.choice == "approve" and (rewrites == 0 or not d.auto):  # auto mode rewrites once per task
                    rewrites += 1
                    git.reset_hard(base_sha)
                    tests_first = self._tests_first(st, task, git, guidance() + self._fb(
                        "The coder found a problem in the locked tests:\n" + reported,
                        "Write the tests again from the acceptance criteria and SPEC.md/OUTLINE.md, without that problem."))
                    if tests_first is None:
                        owner_handoff = True
                        last_problem = "Handed off by the project owner at the tests review."
                        break
                    locked = tests_first
                    feedback = guidance()
                    continue
                why = "they were already rewritten once" if d.auto else "the project owner checked your report"
                last_problem = f"The locked tests stay as they are ({why}):\n{reported}"
                feedback = guidance() + self._fb(last_problem, "Make the implementation satisfy the locked tests.")
                continue

            # --- policy: protected paths, locked tests, deleted tests, test-runner config
            bad = protected_violations(changes, wf.protected_paths, locked)
            bad += [p for s, p in changes if s == "D" and is_test_path(p) and p in main_files]
            runner = runner_config_violations(changes, git.dir, lambda p: git.show("HEAD", p)) if locked else []
            for p in sorted(set(bad + runner)):
                git.restore_path(p)
            policy_note = ""
            if bad:
                policy_note = ("These changes were reverted because you may not modify them: "
                               + ", ".join(sorted(set(bad))) + ". ")
            if runner:
                log.warning("%s: reverted changes to test-runner config while tests are locked: %s",
                            task.id, ", ".join(sorted(set(runner))))
                policy_note += ("These changes were reverted because they change how the locked tests run: "
                                + ", ".join(sorted(set(runner))) + ". ")
            if notes:
                policy_note += " ".join(notes) + " "
            changes = git.worktree_changes()
            if not changes:
                last = next((h.outcome for h in reversed(task.history)
                             if h.phase != "owner" and h.outcome != "no_changes"), "")
                after_review = last == "changes_requested"
                last_problem = (policy_note + ("The coder made no changes after the review (it may disagree with it). "
                                               if after_review else "No usable changes were produced. ")
                                + (f"Worker said: {summary}" if summary else ""))
                task.record("implement", attempt, "no_changes", last_problem)
                d = self.ctl.ask(
                    "task", f"{task.id}: attempt {attempt} made no changes", choices=RETRY_CHOICES,
                    project=st.slug, task=task.id,
                    summary=[f"Attempt {attempt}/{wf.max_attempts_per_task} ended without usable changes",
                             f"PR #{task.pr} keeps the last pushed version" if task.pr else "Nothing has been pushed yet"],
                    details={"Coder's summary": summary or "(none)", "Problem": last_problem},
                    links=[{"label": f"PR #{task.pr}", "url": self._web(st, f"pull/{task.pr}")}] if task.pr else [],
                    stop_check=self.stop_check)
                if d.choice != "approve":
                    self._owner_said(st, task, attempt, "retry", d)
                    owner_handoff = True
                    last_problem = "Handed off by the project owner after an attempt without changes." + (
                        f" Note: {d.note}" if d.note else "")
                    break
                if d.note:
                    task.record("owner", attempt, "owner_note_at_retry", d.note)
                feedback = guidance() + self._fb(last_problem, "Make the code changes required by the issue this time.")
                continue

            git.commit(f"feat(#{task.issue}): {task.title}\n\nAttempt {attempt}. {summary[:1500]}")
            lines = git.diff_line_count(base_sha)
            if lines > wf.max_diff_lines:
                last_problem = f"The change is {lines} lines, over the limit of {wf.max_diff_lines}."
                task.record("implement", attempt, "too_large", last_problem)
                git.reset_hard(pre)
                feedback = guidance() + self._fb(last_problem, "Make a smaller, focused change that only does what the issue asks.")
                continue

            checks = self._checks(st)
            tests = self._run_tests(st, task, git, f"implement-a{attempt}", checks=checks, base=base_sha)
            if not tests.passed:
                last_problem = policy_note + tests.summary(5000)
                task.record("implement", attempt, "tests_failed", tests.summary(1500))
                self.ctl.event(f"{task.id} attempt {attempt}: tests failed — {_brief(tests.summary(1500), 120)}",
                               kind="tests", level="warn", project=st.slug, task=task.id)
                feedback = guidance() + self._fb(last_problem, "Fix the code so the whole test suite passes. Do not weaken the tests.")
                self.store.save(st)
                continue
            self.ctl.event(f"{task.id} attempt {attempt}: tests pass in the sandbox", kind="tests",
                           project=st.slug, task=task.id)

            # --- the outline checks: the code must agree with the outline, not only with its tests
            done_ids = {t.id for t in st.tasks if t.status == "done"}
            verdict = judge(tests.oracle, tests.mutation, checks, {task.id} | done_ids)
            if verdict.line():
                log.info("%s attempt %d: %s", task.id, attempt, verdict.line())
            if not verdict.ok:
                last_problem = policy_note + verdict.feedback()
                task.record("implement", attempt, "checks_failed", "; ".join(verdict.problems())[:3000])
                self.ctl.event(f"{task.id} attempt {attempt}: outline checks failed — {_brief(verdict.problems()[0], 120)}",
                               kind="tests", level="warn", project=st.slug, task=task.id)
                self.store.save(st)
                seen = sorted([(f["id"], f.get("actual", "")) for f in verdict.failed]) + sorted(verdict.dangling)
                if seen == oracle_seen:  # same checks, same results twice: the code or a check is wrong
                    last_problem += (f"\n\nThe same checks failed twice with the same results, so the coder and the "
                                     f"checks disagree. If a check misreads the outline, fix or disable it in "
                                     f"{checks_path(self.store.project_dir(st.repo))} (`agentctl checks {st.repo}`).")
                    break
                oracle_seen = seen
                feedback = guidance() + self._fb(last_problem, (
                    "Make the code agree with OUTLINE.md (and fix tests you are allowed to change). If a locked test "
                    "contradicts OUTLINE.md, report it in locked_test_problem instead of bending the code."))
                continue

            diff = git.diff(base_sha)
            files = git.changed_files(base_sha)
            d = self.ctl.ask(
                "push", f"{task.id}: {task.title}", choices=PUSH_CHOICES, project=st.slug, task=task.id,
                summary=[f"{len(files)} file(s), {lines} changed lines · attempt {attempt}/{wf.max_attempts_per_task}",
                         "Tests pass in a fresh sandbox" + (f" · {len(locked)} locked test file(s)" if locked else "")]
                        + ([verdict.line()] if verdict.line() else [])
                        + [f"Pushes branch {branch} and opens a pull request"],
                details={"Coder's summary": summary or "(none)",
                         "Files": [f"{s} {p}" for s, p in files],
                         **({"Planted errors the tests did not catch": verdict.survivors()} if verdict.survivors() else {})},
                diff=diff, stop_check=self.stop_check)
            if d.choice != "approve":
                if self._owner_said(st, task, attempt, "push", d):
                    owner_handoff = True
                    last_problem = "Handed off by the project owner before pushing." + (f" Note: {d.note}" if d.note else "")
                    break
                last_problem = "The project owner reviewed the change and asked for changes:\n" + d.note
                feedback = guidance() + self._fb(last_problem + "\n\n" + policy_note,
                                               "Make the changes the owner asked for. Keep the whole test suite passing.")
                continue

            self.ctl.set_status("publishing", detail=f"pushing {branch}")
            git.push(branch, force=True)
            pr = self._ensure_pr(st, task, branch)
            self.ctl.event(f"{task.id}: pushed, PR #{pr}", kind="push", project=st.slug, task=task.id,
                           link=self._web(st, f"pull/{pr}"))
            agents_md = (git.dir / "AGENTS.md").read_text(encoding="utf-8") if (git.dir / "AGENTS.md").exists() else ""
            self.ctl.set_status("reviewing", detail=f"PR #{pr}: {self.cfg.llm.roles['reviewer'].model} is reviewing")
            try:
                review = review_change(self.llm, task, diff, tests.summary(2500), agents_md,
                                       owner_notes=guidance(), outline=self._outline_text(st, git))
            except Exception as e:
                last_problem = f"automated review failed to run: {e}"
                task.record("review", attempt, "review_error", last_problem)
                break
            self._comment(st, pr, review.as_markdown())
            if verdict.line():
                self._comment(st, pr, self._checks_markdown(verdict))
            if not review.passed:
                last_problem = "Code review requested changes:\n" + "\n".join(f"- {i}" for i in review.blocking_issues)
                task.record("review", attempt, "changes_requested", last_problem)
                self.ctl.event(f"{task.id}: review requested changes ({len(review.blocking_issues)} issue(s))",
                               kind="review", level="warn", project=st.slug, task=task.id, link=self._web(st, f"pull/{pr}"))
                self.store.save(st)
                d = self.ctl.ask(  # in manual mode the owner decides; auto mode sends it back
                    "merge", f"{task.id}: the reviewer requested changes — PR #{pr}", choices=REVIEW_BLOCKED_CHOICES,
                    project=st.slug, task=task.id,
                    summary=[f"{len(review.blocking_issues)} blocking issue(s) · tests pass in a fresh sandbox",
                             f"Attempt {attempt}/{wf.max_attempts_per_task}"],
                    details={"Blocking issues": review.blocking_issues, "Review": review.as_markdown()}, diff=diff,
                    links=[{"label": f"PR #{pr}", "url": self._web(st, f"pull/{pr}")}], stop_check=self.stop_check)
                if d.choice == "override":
                    task.record("owner", attempt, "owner_override_review", d.note)
                    self.store.save(st)
                    self.ctl.event(f"{task.id}: the owner overruled the review of PR #{pr}", kind="review",
                                   project=st.slug, task=task.id, link=self._web(st, f"pull/{pr}"))
                elif d.choice == "approve":
                    feedback = guidance() + self._fb(last_problem + "\n\n" + policy_note,
                                                     "Address every blocking review issue. Keep the tests passing.")
                    continue
                else:
                    if self._owner_said(st, task, attempt, "review", d):
                        owner_handoff = True
                        last_problem = f"The project owner kept PR #{pr} open for themselves after the review." + (
                            f" Note: {d.note}" if d.note else "")
                        break
                    last_problem = "The project owner reviewed the pull request and asked for changes:\n" + d.note
                    feedback = guidance() + self._fb(last_problem, "Make the changes the owner asked for. Keep the whole test suite passing.")
                    continue
            else:
                self.ctl.event(f"{task.id}: review approved PR #{pr}", kind="review", project=st.slug, task=task.id,
                               link=self._web(st, f"pull/{pr}"))

            self.ctl.set_status("ci", detail=f"PR #{pr}: waiting for CI")
            ci_status, ci_detail = self._wait_ci(st, git.head())
            unattended = not self.ctl.needs_you("merge") and wf.ci.require_for_auto_merge
            if ci_status == "failure" or (ci_status in ("none", "timeout") and (wf.ci.mode == "require" or unattended)):
                last_problem = f"CI {ci_status}: {ci_detail}" + (
                    "" if ci_status == "failure" or wf.ci.mode == "require" else
                    f". CI did not confirm PR #{pr}, so it is not merged without you (ci.require_for_auto_merge).")
                task.record("ci", attempt, f"ci_{ci_status}", last_problem)
                if ci_status != "failure":
                    break  # infrastructure problem: retrying the code will not help
                feedback = guidance() + self._fb(last_problem, "The tests pass locally but CI failed. Make the setup/test commands work on a clean machine.")
                continue

            d = self.ctl.ask(
                "merge", f"{task.id}: {task.title} — PR #{pr}", choices=MERGE_CHOICES, project=st.slug, task=task.id,
                summary=[(f"Review: approved" + (f" with {len(review.suggestions)} suggestion(s)" if review.suggestions else ""))
                         if review.passed else f"Review: you overruled {len(review.blocking_issues)} blocking issue(s)",
                         f"CI: {ci_status}" + (f" ({ci_detail})" if ci_detail and ci_status != "success" else "")]
                        + ([verdict.line()] if verdict.line() else [])
                        + [f"{len(files)} file(s), {lines} changed lines · {wf.merge_method} merge into main"],
                details={"Review": review.as_markdown()}, diff=diff,
                links=[{"label": f"PR #{pr}", "url": self._web(st, f"pull/{pr}")}], stop_check=self.stop_check)
            if d.choice != "approve":
                if self._owner_said(st, task, attempt, "merge", d):
                    owner_handoff = True
                    last_problem = f"The project owner kept PR #{pr} open for themselves." + (f" Note: {d.note}" if d.note else "")
                    break
                last_problem = "The project owner reviewed the pull request and asked for changes:\n" + d.note
                feedback = guidance() + self._fb(last_problem, "Make the changes the owner asked for. Keep the whole test suite passing.")
                continue
            self.ctl.set_status("merging", detail=f"PR #{pr}")
            try:
                self.gh.merge_pr(st.owner, st.repo, pr, git.head(), wf.merge_method, f"{task.title} (#{pr})")
            except HTTPError as e:
                last_problem = f"merge failed: {e.status} {e.body[:300]}"
                task.record("merge", attempt, "merge_failed", last_problem)
                break
            task.status = "done"
            task.locked_tests = {}
            task.record("merge", attempt, "merged",
                        f"PR #{pr} · {len(files)} file(s), {lines} changed lines · attempt {attempt} · "
                        f"review: {'approved' if review.passed else 'overruled by you'} · CI: {ci_status}"
                        + (f" · {self._checks_short(verdict)}" if verdict.line() else "")
                        + (" · merged by you" if not d.auto else " · merged automatically"))
            self._label(st, task, "agent:done")
            self._comment(st, task.issue, f"Merged in #{pr} after {attempt} attempt(s).")
            self.store.save(st)
            git.sync_main()
            if task.kind == "scaffold":
                self._install_ci(st, git)
            log.info("✅ %s merged (PR #%s)", task.id, pr)
            self.ctl.event(f"✅ {task.id} merged (PR #{pr})", kind="merge", project=st.slug, task=task.id,
                           link=self._web(st, f"pull/{pr}"))
            return "done"

        # ---- give up on this task: hand it to a human
        last = task.history[-1] if task.history else None
        if owner_handoff and last and last.outcome.startswith("timeout_handoff_at_"):
            gate = last.outcome.rsplit("_at_", 1)[1]
            last_problem = (f"Nobody answered the {GATE_LABELS.get(gate, gate)} question ({last.note}), so the agent "
                            "left this task for you and moved on to work that doesn't depend on it."
                            + (f" PR #{task.pr} stays open for you." if task.pr else ""))
        wip_pushed = False
        if git.head() != base_sha and not owner_handoff and git.remote_head(branch) == git.head():
            wip_pushed = True  # already on GitHub (the open PR): nothing new to push
        elif git.head() != base_sha and not owner_handoff:
            d = self.ctl.ask("push", f"{task.id}: push the unfinished work for you?", choices=SIMPLE_PUSH_CHOICES,
                             project=st.slug, task=task.id,
                             summary=[f"The agent could not finish {task.id}; it will be labelled agent:needs-human",
                                      f"Pushes branch {branch} so you can continue from it"],
                             diff=git.diff(base_sha), stop_check=self.stop_check)
            if d.choice == "approve":
                try:  # publish the work in progress so a human can pick it up
                    git.push(branch, force=True)
                    wip_pushed = True
                except Exception as e:
                    log.warning("could not push work-in-progress branch: %s", e)
        task.status = "needs_human"
        self._label(st, task, "agent:needs-human")
        hist = "\n".join(f"- attempt {h.attempt} ({h.phase}): **{h.outcome}** {_brief(h.note)}" for h in task.history[-6:])
        self._comment(st, task.issue,
                      "I could not complete this issue automatically.\n\n"
                      f"**Last problem**\n```\n{truncate_middle(last_problem, 3500)}\n```\n\n**History**\n{hist}\n\n"
                      + (f"The work in progress is on branch `{branch}`" + (f" (PR #{task.pr})" if task.pr else "")
                         if (wip_pushed or task.pr) else
                         f"The work in progress was not pushed; it is on the local branch `{branch}` in the agent's clone")
                      + ". Add guidance as a comment or fix it yourself, then set the label back to `agent:todo`.")
        task.record("handoff", 0, "needs_human", last_problem)   # the reason, for reports and the digest
        self.store.save(st)
        git.sync_main()
        log.warning("🙋 %s needs a human", task.id)
        self.ctl.event(f"🙋 {task.id} needs you: {_brief(last_problem, 140)}", kind="task", level="warn",
                       project=st.slug, task=task.id, link=self._web(st, f"issues/{task.issue}") if task.issue else "")
        return "handoff" if owner_handoff else "failed"

    def _owner_said(self, st: ProjectState, task: TaskState, attempt: int, gate: str, d: Decision) -> bool:
        """Record a non-approval from the panel. Returns True for a hand-off, False for
        'send back with feedback' (the caller retries with the note)."""
        handoff = d.choice in ("handoff", "skip")
        if d.by == "timeout":  # nobody answered at night: not an owner note, never shown to the coder as one
            task.record("night", attempt, f"timeout_handoff_at_{gate}", d.note)
        else:
            task.record("owner", attempt, f"owner_{'handoff' if handoff else 'changes'}_at_{gate}", d.note)
        self.store.save(st)
        return handoff

    # ------------------------------------------------------------------ phases
    def _tests_first(self, st: ProjectState, task: TaskState, git: Git, feedback: str) -> Optional[list[str]]:
        """Have the coder write failing tests from the acceptance criteria first. Those test
        files are then locked: the implementation attempts may not change them. At the `tests`
        gate the owner can have them rewritten first; returns None if the owner hands off."""
        fb = feedback
        attempt = empty = 0
        while empty < 2:  # owner rewrites do not count; two attempts without any test file do
            attempt += 1
            self.check_budget()
            task.attempts_total += 1
            pre = git.head()
            result, notes, changes = self._worker_attempt(st, task, git, "tests", attempt, fb, [])
            bad = set(non_test_changes(changes)) | set(protected_violations(changes, self.cfg.workflow.protected_paths))
            bad |= {p for s, p in changes if s == "D"}
            for p in sorted(bad):
                git.restore_path(p)
            changes = git.worktree_changes()
            test_files = sorted(p for s, p in changes if s != "D" and is_test_path(p))
            if not test_files:
                empty += 1
                task.record("tests", attempt, "no_tests", "no test files were written")
                fb = feedback + self._fb("No test files were written (non-test changes are discarded in this phase).",
                              "Write test files only, under the project's test directory.")
                continue
            git.commit(f"test(#{task.issue}): tests for {task.title}")
            tr = self._run_tests(st, task, git, f"tests-a{attempt}")
            outcome = "tests_written_failing" if not tr.passed else "tests_written_passing"
            task.record("tests", attempt, outcome, ", ".join(test_files))
            self.store.save(st)
            d = self.ctl.ask(
                "tests", f"{task.id}: {task.title}", choices=TESTS_CHOICES, project=st.slug, task=task.id,
                summary=[f"{len(test_files)} test file(s) written from the acceptance criteria · attempt {attempt}",
                         "They fail for now, as they should before the feature exists" if not tr.passed
                         else "They already pass, before anything is implemented",
                         "Check the expected values: once locked, the implementation cannot change them"],
                details={"Acceptance criteria": task.acceptance_criteria, "Coder's summary":
                         (result.get("summary") or "").strip() or "(none)", "Files": test_files},
                diff=git.diff(pre), stop_check=self.stop_check)
            if d.choice != "approve":
                if self._owner_said(st, task, attempt, "tests", d):
                    task.locked_tests = {}
                    return None
                git.reset_hard(pre)  # discard the rejected tests; they are written again
                fb = feedback + self._fb("The project owner reviewed your tests and asked for changes:\n" + d.note,
                                         "Write the tests again from the acceptance criteria, following the owner's note.")
                continue
            log.info("tests-first phase locked %d test file(s) (%s)", len(test_files), outcome)
            task.locked_tests = {p: (git.dir / p).read_text(encoding="utf-8", errors="replace") for p in test_files}
            self.store.save(st)
            return test_files
        log.warning("tests-first phase produced no tests; continuing without locked tests")
        return []

    def _restore_locked_tests(self, st: ProjectState, task: TaskState, git: Git) -> list[str]:
        """Tests you locked in an earlier run (which stopped before the task was merged) are put
        back as they were, instead of being written again from scratch."""
        if not task.locked_tests:
            return []
        for path, content in task.locked_tests.items():
            p = git.dir / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        git.commit(f"test(#{task.issue}): tests for {task.title} (locked in an earlier run)")
        files = sorted(task.locked_tests)
        log.info("%s: reusing %d locked test file(s) from an earlier run", task.id, len(files))
        self.ctl.event(f"{task.id}: reusing {len(files)} locked test file(s) from an earlier run", kind="tests",
                       project=st.slug, task=task.id)
        task.record("tests", 0, "tests_reused", ", ".join(files))
        return files

    def _worker_attempt(self, st: ProjectState, task: TaskState, git: Git, mode: str, attempt: int,
                        feedback: str, locked: list[str]) -> tuple[dict, list[str], list[tuple[str, str]]]:
        sb, role = self.cfg.sandbox, self.cfg.llm.roles["coder"]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = self.cfg.work_dir / st.repo / f"{task.id}-{mode}-a{attempt}-{stamp}"
        ws, task_dir = root / "ws", root / "task"
        (task_dir / "out").mkdir(parents=True, exist_ok=True)
        files = git.ls_files()
        export_tree(files, git.dir, ws)
        init_snapshot_git(ws)
        payload = {
            "mode": mode,
            "repo": st.slug,
            "issue": task.issue,
            "task_id": task.id,
            "title": task.title,
            "kind": task.kind,
            "description": task.description,
            "acceptance_criteria": task.acceptance_criteria,
            "files_hint": task.files_hint,
            "feedback": "\n\n".join(x for x in (feedback.strip(), coder_text(self._checks(st), task.id)) if x),
            "locked_tests": locked,
            "stack": st.stack.model_dump(),
            "llm": {"base_url": sb.llm_url_in_sandbox, "model": role.model, "temperature": role.temperature,
                    "top_p": role.top_p, "top_k": role.top_k, "max_tokens": role.max_tokens,
                    "thinking": role.thinking, "context_tokens": role.context_tokens, "extra_body": role.extra_body},
            "max_steps": sb.max_agent_steps,
            "time_budget_s": max(300, sb.worker_timeout_min * 60 - 90),
            "mcp": {"enabled": bool(self.cfg.github.agent_token) and (
                        sb.mcp == "always" or (sb.mcp == "auto" and mentions_github(task.description + "\n" + feedback))),
                    "toolsets": sb.mcp_toolsets},
        }
        (task_dir / "task.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("%s attempt %d (%s): starting coder", task.id, attempt, mode)
        self.ctl.set_status("coding", project=st.slug, task=task.id,
                            detail=f"{'writing tests' if mode == 'tests' else 'implementing'} · attempt {attempt}"
                                   f"/{self.cfg.workflow.max_attempts_per_task if mode != 'tests' else 2}",
                            worker_log=str(root / "worker.log"))
        code, timed_out = self.sandbox.run_worker(ws, task_dir, root / "worker.log", self.cfg.github.agent_token)
        result: dict = {"status": "timeout" if timed_out else "error", "summary": ""}
        rp = task_dir / "out" / "result.json"
        if rp.exists():
            try:
                result = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                pass
        notes = sync_back(ws, git.dir, files)
        changes = git.worktree_changes()
        shutil.rmtree(ws, ignore_errors=True)  # keep task/ (transcript) and worker.log for debugging
        log.info("%s attempt %d (%s): worker %s, %d file(s) changed", task.id, attempt, mode,
                 result.get("status"), len(changes))
        return result, notes, changes

    def _run_tests(self, st: ProjectState, task: TaskState, git: Git, label: str,
                   checks: Optional[list[Check]] = None, base: str = "") -> TestResult:
        """Tests in a fresh sandbox. With `checks` (implementation attempts), the outline checks and
        the mutation check run in the same container afterwards (on the files changed since `base`)."""
        tree = self.cfg.work_dir / st.repo / f"{task.id}-testrun-{label}-{time.strftime('%H%M%S')}"
        export_tree(git.ls_files(), git.dir, tree)
        log_file = self.cfg.logs_dir / st.repo / f"{task.id}-tests.log"
        log.info("running tests in a fresh sandbox: %s", st.stack.test_command)
        self.ctl.set_status("testing", detail=f"{label}: {st.stack.test_command}")
        after = ""
        if checks is not None and self.cfg.workflow.checks.enabled:
            ck = self.cfg.workflow.checks
            changed = [p for s_, p in git.changed_files(base) if s_ != "D" and p.endswith(".py")] if base else []
            mutate = ([p for p in changed if not is_test_path(p)]
                      if ck.mutation and st.stack.language == "python" else [])
            after = stage(tree, checks, changed, mutate, _one_line(st.stack.test_command), ck.mutation_max,
                          ck.mutation_budget_s)
        tr = self.sandbox.run_tests(tree, st.stack.setup_command, st.stack.test_command, log_file, after=after)
        if after:
            tr.oracle, rest = take_marked(tr.output, ORACLE_MARK)
            tr.mutation, tr.output = take_marked(rest, MUTATION_MARK)
        shutil.rmtree(tree, ignore_errors=True)
        log.info("tests %s", "passed" if tr.passed else f"failed (exit {tr.exit_code})")
        return tr

    def _ensure_pr(self, st: ProjectState, task: TaskState, branch: str) -> int:
        existing = self.gh.find_open_pr(st.owner, st.repo, branch)
        if existing:
            task.pr = int(existing["number"])
            return task.pr
        body = (f"Closes #{task.issue}\n\n**Task {task.id}: {task.title}**\n\n"
                + "\n".join(f"- [x] {c}" for c in task.acceptance_criteria)
                + f"\n\n{BOT_MARKER}\n_Opened by agentbox. Tests pass in the sandbox; an automated review follows below._")
        pr = self.gh.create_pr(st.owner, st.repo, branch, "main", f"{task.id}: {task.title}", body)
        task.pr = int(pr["number"])
        log.info("opened PR #%s", task.pr)
        return task.pr

    def _wait_ci(self, st: ProjectState, sha: str) -> tuple[str, str]:
        mode = self.cfg.workflow.ci.mode
        if mode == "off" or not (self.cfg.repos_dir / st.repo / ".github" / "workflows").exists():
            return "skipped", "CI disabled or not installed"
        status, detail = self.gh.wait_for_ci(st.owner, st.repo, sha, self.cfg.workflow.ci.wait_timeout_min * 60)
        log.info("CI: %s (%s)", status, detail)
        return status, detail

    def _human_guidance(self, st: ProjectState, task: TaskState) -> str:
        if not task.issue:
            return ""
        try:
            comments = self.gh.human_comments(st.owner, st.repo, task.issue, since=task.human_feedback_since)
        except HTTPError:
            return ""
        if not comments:
            return ""
        text = "\n\n".join(f"@{(c.get('user') or {}).get('login', 'owner')}: {c.get('body', '').strip()}" for c in comments[-5:])
        return "Guidance from the project owner on the issue (follow it):\n" + truncate_middle(text, 4000)

    @staticmethod
    def _owner_notes(task: TaskState) -> str:
        """Notes the owner gave for this task at any gate, in any run (kept in its history)."""
        notes = [h for h in task.history if h.phase == "owner" and h.note.strip()]
        if not notes:
            return ""
        lines = [f"- ({h.outcome.removeprefix('owner_').replace('_', ' ')}) {h.note.strip()}" for h in notes[-8:]]
        return ("Notes from the project owner (follow them; they override the issue text and acceptance criteria):\n"
                + truncate_middle("\n".join(lines), 4000))

    def _outline_text(self, st: ProjectState, git: Git) -> str:
        """The owner's outline: OUTLINE.md in the repo, else the copy kept with the project state."""
        for p in (git.dir / "OUTLINE.md", self.store.project_dir(st.repo) / "outline.md"):
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        return ""

    @staticmethod
    def _checks_markdown(v: Verdict) -> str:
        lines = ["### Outline checks and mutation check", "", v.line()]
        if v.survivors():
            lines += ["", "Planted errors the tests did not catch (behaviour the tests don't pin down; worth a look):"]
            lines += [f"- `{x}`" for x in v.survivors()[:15]]
        return "\n".join(lines)

    @staticmethod
    def _checks_short(v: Verdict) -> str:
        parts = [f"checks {len(v.passed)}/{len(v.passed) + len(v.failed)}"] if v.ran else []
        m = v.mutation or {}
        parts += [f"mutants caught {m['killed']}/{m['tested']}"] if m.get("tested") else []
        return " · ".join(parts)

    @staticmethod
    def _fb(problem: str, instruction: str) -> str:
        return f"Your previous attempt did not succeed.\n\nProblem:\n{truncate_middle(problem, 6000)}\n\nWhat to do: {instruction}"
