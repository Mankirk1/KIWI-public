"""agentctl - command line for the agentbox orchestrator.

  agentctl doctor [--quick]            check the whole stack
  agentctl plan-only PROJECT.md        spec + plan only, no GitHub writes (good first test)
  agentctl new PROJECT.md [--name N]   create the repo + issues and work until done/budget
           [--use-plan DIR]            ... reusing the spec and plan of a plan-only run (skips ~35 min of planning)
  agentctl continue REPO               resume a project (also picks up issues you labelled agent:todo)
  agentctl continue-all                start outlines queued in data/inbox, then resume every project
  agentctl queue PROJECT.md            queue an outline for the next continue-all (e.g. tonight's run)
  agentctl extend REPO "change ..."    plan new issues for an existing project and work on them
  agentctl status [REPO]               show task status
  agentctl digest [--hours N]          the latest digest: what the last command (night) merged or left for you
  agentctl checks REPO [--regenerate]  list (or rewrite) the outline checks the code must pass
  agentctl set-stack REPO --setup S --test T   fix the install/test commands of a project
  agentctl stop | unstop               create / remove the STOP file
  agentctl clean [--days N]            delete old sandbox work directories

Remote control (see README "Control from your phone"):
  agentctl serve                       run the phone control panel (http://127.0.0.1:8765)
  agentctl pair                        show a one-time code to sign a phone in to the panel
  agentctl mode [auto|manual]          show or switch how much the agent asks you
  agentctl pause | resume              hold the agent at its next checkpoint / let it continue
  agentctl ask                         list the questions waiting for you
  agentctl answer ID CHOICE [--note T] answer one (CHOICE: approve, revise, skip, handoff, ...)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import load_config
from .util import log, setup_logging


def _orch(cfg):
    from .loop import Orchestrator
    return Orchestrator(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentctl", description="Local autonomous GitHub project agent")
    ap.add_argument("--config", type=Path, help="path to config.yaml (default ~/agentbox/config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the whole stack")
    d.add_argument("--quick", action="store_true", help="skip loading/benchmarking models")

    p = sub.add_parser("plan-only", help="write spec + plan locally without touching GitHub")
    p.add_argument("outline", type=Path)
    p.add_argument("--name")

    n = sub.add_parser("new", help="start a new project from an outline file")
    n.add_argument("outline", type=Path)
    n.add_argument("--name", help="override the repository name")
    n.add_argument("--use-plan", type=Path, metavar="DIR",
                   help="reuse the spec and plan of a plan-only run (its folder, e.g. ~/agentbox-runtime/projects/NAME)")

    c = sub.add_parser("continue", help="resume a project")
    c.add_argument("repo")
    sub.add_parser("continue-all", help="start queued outlines, then resume every project")
    q = sub.add_parser("queue", help="queue an outline for the next continue-all")
    q.add_argument("outline", type=Path)

    e = sub.add_parser("extend", help="add work to an existing project")
    e.add_argument("repo")
    e.add_argument("request", nargs="?", help="change request text (or use --file)")
    e.add_argument("--file", type=Path)

    s = sub.add_parser("status", help="show task status")
    s.add_argument("repo", nargs="?")

    se = sub.add_parser("sentinel", help="replay the watchdog rules over a project's recorded runs")
    se.add_argument("repo", nargs="?")
    se.add_argument("--all", action="store_true", help="also show info findings (slow steps, compactions)")

    ck = sub.add_parser("checks", help="list a project's outline checks (or write them again)")
    ck.add_argument("repo")
    ck.add_argument("--regenerate", action="store_true", help="write them again from the outline (uses the model)")

    dg = sub.add_parser("digest", help="print the latest digest (summary of the last command's runs)")
    dg.add_argument("--hours", type=float, help="instead, summarise every run started in the last N hours")

    ss = sub.add_parser("set-stack", help="change a project's setup/test/lint commands")
    ss.add_argument("repo")
    ss.add_argument("--setup")
    ss.add_argument("--test")
    ss.add_argument("--lint")

    sub.add_parser("stop", help="ask a running agent to stop after the current step")
    sub.add_parser("unstop", help="remove the STOP file")

    sv = sub.add_parser("serve", help="run the phone control panel")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    pr = sub.add_parser("pair", help="one-time code to sign a phone in to the panel")
    pr.add_argument("--logout-all", action="store_true", help="sign out every paired phone instead")
    md = sub.add_parser("mode", help="show or switch auto/manual mode")
    md.add_argument("mode", nargs="?", choices=["auto", "manual"])
    sub.add_parser("pause", help="hold the agent at its next checkpoint")
    sub.add_parser("resume", help="let a paused agent continue")
    sub.add_parser("ask", help="list questions waiting for you")
    an = sub.add_parser("answer", help="answer a waiting question")
    an.add_argument("id")
    an.add_argument("choice")
    an.add_argument("--note", default="")
    cl = sub.add_parser("clean", help="delete old sandbox work directories")
    cl.add_argument("--days", type=float, default=3)

    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(f"agentctl: {e}")
        return 2
    except ValidationError as e:
        print("agentctl: config.yaml is invalid:")
        for err in e.errors():
            print(f"  {'.'.join(str(x) for x in err['loc'])}: {err['msg']}")
        return 2
    except yaml.YAMLError as e:
        print(f"agentctl: config.yaml is not valid YAML: {e}")
        return 2
    log_file = cfg.logs_dir / f"agentctl-{time.strftime('%Y%m%d')}.log"
    setup_logging(log_file, args.verbose)

    if args.cmd == "doctor":
        from .doctor import Doctor
        return Doctor(cfg).run(quick=args.quick)

    if args.cmd in ("stop", "unstop", "mode", "pause", "resume", "ask", "answer", "serve", "pair"):
        return _control(cfg, args)

    if args.cmd == "status":
        from .state import StateStore
        store = StateStore(cfg.projects_dir)
        repos = [args.repo] if args.repo else store.all()
        if not repos:
            print("No projects yet.")
        for r in repos:
            st = store.load(r)
            print(f"\n{st.slug}  ({', '.join(f'{k}: {v}' for k, v in sorted(st.counts().items()))})")
            for t in st.tasks:
                print(f"  {t.status:<12} {t.id:<5} #{t.issue or '-':<5} {t.title[:70]}  (attempts: {t.attempts_total})")
            if st.runs:
                last = st.runs[-1]
                print(f"  last run {last.run_id}: {last.stop_reason or 'running/unknown'}")
        return 0

    if args.cmd == "sentinel":
        from .sentinel import scan_project
        from .state import StateStore
        store = StateStore(cfg.projects_dir)
        for r in [args.repo] if args.repo else store.all():
            st = store.load(r)
            found = [f for f in scan_project(cfg.work_dir / r, [t.model_dump(mode="json") for t in st.tasks])
                     if args.all or f.severity != "info"]
            print(f"\n{st.slug}: {len(found)} finding(s)")
            for f in found:
                print(f"  {f.when()}  {f.severity:<6} {f.rule:<21} {f.summary}")
                for e in f.evidence[:1]:
                    print(f"  {'':>16}   evidence: {e[:150]}")
        return 0

    if args.cmd == "checks":
        from .oracle import checks_path, load_checks
        from .state import StateStore
        store = StateStore(cfg.projects_dir)
        st = store.load(args.repo)
        pdir = store.project_dir(st.repo)
        if args.regenerate:
            from .loop import run_lock
            from .oracle import extract_checks, save_checks
            with run_lock(cfg.paths.data):   # the model server is busy during a run
                spec = cfg.repos_dir / st.repo / "SPEC.md"
                new, dropped = extract_checks(_orch(cfg).llm, (pdir / "outline.md").read_text(encoding="utf-8"),
                                              spec.read_text(encoding="utf-8") if spec.is_file() else "",
                                              [t.model_dump() for t in st.tasks])
                for x in dropped:
                    print(f"  dropped: {x}")
                if new:
                    save_checks(pdir, new)
        checks = load_checks(pdir)
        if checks is None:
            print(f"{st.slug}: no outline checks yet (written at planning, or with --regenerate)")
            return 0
        print(f"{st.slug}: {len(checks)} outline checks in {checks_path(pdir)}")
        for c in checks:
            print(f"  {c.id:<4} {c.task:<4} {'' if c.enabled else '(off) '}{c.brief()}")
        return 0

    if args.cmd == "digest":
        from .digest import build_digest, latest_digest
        if args.hours:
            import datetime as dt
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)).replace(microsecond=0).isoformat()
            d = build_digest(cfg, since)
            print(d.text if d else f"No runs in the last {args.hours:g} hours.")
        else:
            p = latest_digest(cfg)
            print(p.read_text(encoding="utf-8") if p else "No digest yet: one is written when a run command ends.")
        return 0

    if args.cmd == "queue":
        inbox = cfg.paths.data / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / args.outline.name
        shutil.copy(args.outline, dest)
        print(f"queued {dest}; the next `agentctl continue-all` starts it")
        return 0

    if args.cmd == "set-stack":
        from .state import StateStore
        store = StateStore(cfg.projects_dir)
        st = store.load(args.repo)
        for field in ("setup", "test", "lint"):
            val = getattr(args, field)
            if val is not None:
                setattr(st.stack, f"{field}_command", val)
        store.save(st)
        print(f"{st.slug}: setup=`{st.stack.setup_command}` test=`{st.stack.test_command}` lint=`{st.stack.lint_command}`")
        print("Also update AGENTS.md (and .github/workflows/ci.yml) in the repository so humans and CI agree.")
        return 0

    if args.cmd == "clean":
        cutoff = time.time() - args.days * 86400
        removed = 0
        for d in cfg.work_dir.glob("*/*"):
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        print(f"removed {removed} work directories older than {args.days} days")
        return 0

    from .loop import run_lock
    with run_lock(cfg.paths.data):
        if cfg.budget.stop_path.exists():
            log.warning("STOP file exists at %s - run `agentctl unstop` first", cfg.budget.stop_path)
            return 2
        orch = _orch(cfg)
        orch.start(" ".join(str(a) for a in (argv if argv is not None else sys.argv[1:])) or args.cmd)
        try:
            return _run(orch, cfg, args)
        finally:
            orch.finish()
    return 0


def _control(cfg, args) -> int:
    from .control import Control
    ctl = Control(cfg)
    if args.cmd == "stop":
        ctl.set_stop(True, by="cli")
        print(f"STOP file created at {cfg.budget.stop_path}; the agent stops at its next checkpoint.")
    elif args.cmd == "unstop":
        ctl.set_stop(False, by="cli")
        print("STOP file removed.")
    elif args.cmd == "mode":
        if args.mode:
            ctl.set_mode(args.mode, by="cli")
        st = ctl.state()
        gates = cfg.control.manual_gates if st["mode"] == "manual" else cfg.control.always_ask
        print(f"mode: {st['mode']}  (asks at: {', '.join(gates) or 'nothing'})")
    elif args.cmd in ("pause", "resume"):
        ctl.set_paused(args.cmd == "pause", by="cli")
        print("Paused: the agent holds at its next checkpoint." if args.cmd == "pause" else "Resumed.")
    elif args.cmd == "ask":
        pending = ctl.approvals(status="pending")
        if not pending:
            print("Nothing is waiting for you.")
        for a in pending:
            print(f"\n{a.id}  [{a.gate}] {a.title}  ({a.project})")
            for line in a.summary:
                print(f"    {line}")
            print("    choices: " + ", ".join(f"{c['id']} ({c['label']}{', needs --note' if c.get('needs_note') else ''})"
                                         for c in a.choices))
    elif args.cmd == "answer":
        try:
            a = ctl.decide(args.id, args.choice, args.note, by="cli")
        except (LookupError, ValueError) as e:
            print(f"agentctl: {e}")
            return 2
        print(f"{a.id}: {a.choice}")
    elif args.cmd == "pair":
        from .panel import PanelAuth
        auth = PanelAuth(cfg)
        if args.logout_all:
            auth.logout_all()
            print("Every paired phone is signed out.")
            return 0
        code = auth.new_pair_code()
        print(f"\n  Pairing code:  {code[:3]} {code[3:]}\n")
        if cfg.control.public_url:
            print(f"  Open {cfg.control.public_url} on your phone and enter the code within 10 minutes.")
        else:
            print("  Open the panel's Tailscale address on your phone and enter the code within 10 minutes.")
            print("  (No control.public_url set yet: see README, 'Reaching it from the phone'.)")
        print("  The code works once. Run `agentctl pair --logout-all` to sign out every phone.\n")
    elif args.cmd == "serve":
        from .panel import serve
        return serve(cfg, host=args.host, port=args.port)
    return 0


def _run(orch, cfg, args) -> int:
    from .loop import StopRun
    try:
        if args.cmd == "plan-only":
            orch.check_guards("plan-only")
            orch.new_project(args.outline, name=args.name, plan_only=True)
        elif args.cmd == "new":
            if args.use_plan:
                try:
                    orch.load_plan(args.use_plan.expanduser(), args.outline.read_text(encoding="utf-8"))
                except ValueError as e:
                    print(f"agentctl: {e}")
                    return 2
            orch.check_guards("new project")
            orch.new_project(args.outline, name=args.name,
                             use_plan=args.use_plan.expanduser() if args.use_plan else None)
        elif args.cmd == "continue":
            orch.continue_project(args.repo)
        elif args.cmd == "continue-all":
            from .state import StateStore
            inbox = cfg.paths.data / "inbox"
            for outline in sorted(inbox.glob("*.md")) if inbox.exists() else []:
                try:
                    orch.check_budget()
                except Exception as e:
                    log.warning("not starting queued outlines: %s", e)
                    break
                started = inbox / "started"
                started.mkdir(exist_ok=True)
                moved = started / f"{time.strftime('%Y%m%d-%H%M%S')}-{outline.name}"
                outline.rename(moved)  # moved first, so a crash never starts it twice
                log.info("starting queued outline %s", outline.name)
                orch.new_project(moved)
            for repo in StateStore(cfg.projects_dir).all():
                try:
                    orch.check_budget()
                except Exception as e:
                    log.warning("stopping continue-all: %s", e)
                    break
                orch.continue_project(repo)
        elif args.cmd == "extend":
            text = args.file.read_text(encoding="utf-8") if args.file else (args.request or "")
            if not text.strip():
                print("give the change request as an argument or with --file")
                return 2
            orch.extend_project(args.repo, text)
    except StopRun as e:
        log.error("stopped: %s", e)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
