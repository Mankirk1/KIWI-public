#!/usr/bin/env python3
"""End-to-end coder benchmark: run the real sandboxed coder on a fixed, recorded task and
measure it, so a change to the worker can be compared before/after on identical input.

    .venv/bin/python scripts/bench-coder.py --label before --image agentbox-worker:bench-before

The default scenario is mechcalc's T5 implementation exactly as run 4 faced it: the repo at
the T4 merge plus T5's locked tests, the same task file (owner notes included). The coder runs
with temperature 0 to reduce randomness and with conversation capture on (history.jsonl, used
by scripts/bench-context.py). Results go to <data_dir>/bench/<label>-<time>/metrics.json.
Nothing is pushed anywhere; the model server must not be busy with a real run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from agentbox.config import load_config
from agentbox.sandbox import Sandbox
from agentbox.workspace import init_snapshot_git

SCENARIOS = {
    "mechcalc-t5": {
        "repo": "mechcalc", "base": "26603e1",                    # main after T4 merged
        "extra": {"tests/test_cli.py": "4b21461"},                 # T5's locked tests (as merged)
        "task": "work/mechcalc/T5-implement-a1-20261018-204900/task/task.json",
    },
}


def metrics_from(out: Path) -> dict:
    """Totals from the coder's capture (history.jsonl) and result.json."""
    resp, compactions = [], 0
    hist = out / "history.jsonl"
    if hist.is_file():
        for line in hist.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "response":
                resp.append(rec)
    tr = out / "transcript.jsonl"
    if tr.is_file():
        compactions = sum(1 for l in tr.read_text(encoding="utf-8").splitlines() if '"kind": "compact' in l)
    t = [r.get("timings") or {} for r in resp]
    pp_n, pp_ms = sum(x.get("prompt_n", 0) for x in t), sum(x.get("prompt_ms", 0) for x in t)
    tg_n, tg_ms = sum(x.get("predicted_n", 0) for x in t), sum(x.get("predicted_ms", 0) for x in t)
    result = json.loads((out / "result.json").read_text()) if (out / "result.json").is_file() else {}
    return {
        "status": result.get("status"), "steps": result.get("steps"), "requests": len(resp),
        "compactions": compactions,
        "prompt_tokens_sent": sum(r.get("prompt_tokens", 0) for r in resp),     # incl. cached prefix
        "prompt_tokens_evaluated": pp_n, "prompt_minutes": round(pp_ms / 60000, 2),
        "prompt_tok_per_s": round(pp_n / (pp_ms / 1000), 1) if pp_ms else None,
        "generated_tokens": tg_n, "generation_minutes": round(tg_ms / 60000, 2),
        "generation_tok_per_s": round(tg_n / (tg_ms / 1000), 2) if tg_ms else None,
        "max_context_tokens": max((r.get("prompt_tokens", 0) for r in resp), default=0),
        "mean_context_tokens": round(sum(r.get("prompt_tokens", 0) for r in resp) / len(resp)) if resp else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", required=True)
    ap.add_argument("--image", default="agentbox-worker:latest")
    ap.add_argument("--scenario", default="mechcalc-t5", choices=sorted(SCENARIOS))
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()

    cfg = load_config()
    cfg.sandbox.worker_image = args.image
    sc = SCENARIOS[args.scenario]
    data = cfg.paths.data
    run_dir = data / "bench" / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}"
    ws, task_dir = run_dir / "ws", run_dir / "task"
    (task_dir / "out").mkdir(parents=True)
    ws.mkdir()
    repo = cfg.repos_dir / sc["repo"]
    archive = subprocess.run(["git", "-C", str(repo), "archive", sc["base"]], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(ws)], input=archive, check=True)
    for path, rev in sc["extra"].items():
        content = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], check=True, capture_output=True).stdout
        (ws / path).parent.mkdir(parents=True, exist_ok=True)
        (ws / path).write_bytes(content)
    init_snapshot_git(ws)

    task = json.loads((data / sc["task"]).read_text())
    task["capture"] = True
    task["llm"]["temperature"] = args.temperature
    (task_dir / "task.json").write_text(json.dumps(task, indent=2))

    print(f"[bench] {args.label}: image {args.image}, scenario {args.scenario}, dir {run_dir}", flush=True)
    t0 = time.time()
    code, timed_out = Sandbox(cfg).run_worker(ws, task_dir, run_dir / "worker.log", agent_token="")
    t1 = time.time()
    stack = task["stack"]
    tr = Sandbox(cfg).run_tests(ws, stack["setup_command"], stack["test_command"], run_dir / "tests.log")
    m = {"label": args.label, "image": args.image, "scenario": args.scenario, "temperature": args.temperature,
         "worker_exit": code, "timed_out": timed_out, "wall_minutes": round((t1 - t0) / 60, 1),
         "tests_passed": tr.passed, **metrics_from(task_dir / "out")}
    (run_dir / "metrics.json").write_text(json.dumps(m, indent=2))
    print(json.dumps(m, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
