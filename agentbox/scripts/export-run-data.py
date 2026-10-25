#!/usr/bin/env python3
"""Export what agentbox recorded about each project's runs into the repo, for later analysis.

    .venv/bin/python scripts/export-run-data.py [project ...] [--out docs/field-notes/runs]

Writes, per project: summary.md (runs, tasks, every attempt with its duration and token use,
the owner's decisions), attempts.csv, and copies of the run reports. Only structured data and
the agent's own reports are exported - never transcripts or logs - and everything passes
through the panel's token redaction, with the home directory shown as ~ (the repo may be
published). Safe to re-run: files are overwritten.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentbox.config import load_config
from agentbox.panel import redact
from agentbox.state import StateStore

HOME = Path(__file__).resolve().parent.parent
WORK_DIR = re.compile(r"^(?P<task>[A-Za-z]+\d+)-(?P<mode>tests|implement)-a(?P<attempt>\d+)-(?P<stamp>\d{8}-\d{6})$")


def _scrub(text: str) -> str:
    """Token redaction, and the local home directory (it contains the user name) as ~."""
    return redact(text).replace(str(Path.home()), "~")


def _clip(text: str, n: int = 240) -> str:
    text = " ".join(redact(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _attempts(work: Path) -> list[dict]:
    rows = []
    for d in sorted(work.iterdir()) if work.is_dir() else []:
        m = WORK_DIR.match(d.name)
        if not m:
            continue
        out = d / "task" / "out"
        result = json.loads((out / "result.json").read_text()) if (out / "result.json").is_file() else {}
        times = []
        if (out / "transcript.jsonl").is_file():
            for line in (out / "transcript.jsonl").read_text(errors="replace").splitlines():
                try:
                    times.append(float(json.loads(line)["t"]))
                except (ValueError, KeyError, TypeError):
                    pass
        usage = result.get("usage") or {}
        rows.append({
            "task": m["task"], "mode": m["mode"], "attempt": int(m["attempt"]),
            "started": datetime.strptime(m["stamp"], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M"),
            "minutes": round((max(times) - min(times)) / 60, 1) if len(times) > 1 else "",
            "steps": result.get("steps", ""), "status": result.get("status", "(no result: stopped)"),
            "prompt_tokens": usage.get("prompt_tokens", ""), "completion_tokens": usage.get("completion_tokens", ""),
            "locked_test_problem": _clip(result.get("locked_test_problem", "")),
            "summary": _clip(result.get("summary", "")),
        })
    return rows


def _table(header: list[str], rows: list[list]) -> str:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def export(cfg, repo: str, out_root: Path) -> Path:
    st = StateStore(cfg.projects_dir).load(repo)
    out = out_root / repo
    (out / "reports").mkdir(parents=True, exist_ok=True)
    attempts = _attempts(cfg.work_dir / repo)

    md = [f"# Run data: {st.owner}/{st.repo}", "",
          f"Exported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/export-run-data.py`. "
          "Times in the attempt table are local; run times are UTC.", "",
          "## Runs", "",
          _table(["run", "command", "started", "finished", "merged", "handed to owner", "stop reason", "model calls",
                  "prompt tok", "completion tok", "generation min"],
                 [[r.run_id, r.command, r.started[:16], (r.finished or "")[:16], ", ".join(r.done) or "-",
                   ", ".join(r.failed) or "-", r.stop_reason, r.usage.get("calls", ""), r.usage.get("prompt_tokens", ""),
                   r.usage.get("completion_tokens", ""), round(r.usage.get("seconds", 0) / 60, 1) if r.usage else ""]
                  for r in st.runs]),
          "", "## Tasks", "",
          _table(["task", "title", "kind", "status", "attempts", "issue", "PR"],
                 [[t.id, t.title, t.kind, t.status, t.attempts_total, t.issue or "", t.pr or ""] for t in st.tasks]),
          "", "## Attempts (one sandboxed coder run each)", "",
          _table(["task", "phase", "attempt", "started", "minutes", "steps", "status", "prompt tok", "completion tok",
                  "coder's summary"],
                 [[a["task"], a["mode"], a["attempt"], a["started"], a["minutes"], a["steps"], a["status"],
                   a["prompt_tokens"], a["completion_tokens"], a["summary"]] for a in attempts]),
          "", "## Task history (orchestrator's record, including the owner's decisions)", ""]
    for t in st.tasks:
        if not t.history:
            continue
        md += [f"### {t.id}: {t.title}", "",
               _table(["time (UTC)", "phase", "attempt", "outcome", "note"],
                      [[h.ts[:16], h.phase, h.attempt, h.outcome, _clip(h.note)] for h in t.history]), ""]
    (out / "summary.md").write_text(_scrub("\n".join(md) + "\n"), encoding="utf-8")

    buf = io.StringIO()
    if attempts:
        w = csv.DictWriter(buf, fieldnames=list(attempts[0]))
        w.writeheader()
        w.writerows(attempts)
    (out / "attempts.csv").write_text(_scrub(buf.getvalue()), encoding="utf-8")

    for report in sorted((cfg.reports_dir / repo).glob("*.md")):
        (out / "reports" / report.name).write_text(_scrub(report.read_text(errors="replace")), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("projects", nargs="*", help="project (repo) names; default: all")
    ap.add_argument("--out", default=str(HOME / "docs" / "field-notes" / "runs"))
    args = ap.parse_args()
    cfg = load_config()
    repos = args.projects or StateStore(cfg.projects_dir).all()
    if not repos:
        print("no projects found in", cfg.projects_dir, file=sys.stderr)
        return 1
    for repo in repos:
        print("exported", export(cfg, repo, Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
