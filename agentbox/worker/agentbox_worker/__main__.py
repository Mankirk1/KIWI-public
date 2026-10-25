"""Entry point: python -m agentbox_worker /task/task.json"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import Agent


def main() -> int:
    task_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/task/task.json")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    workspace = Path(os.environ.get("AGENTBOX_WORKSPACE", "/workspace"))
    out_dir = task_path.parent / "out"
    result = asyncio.run(Agent(task, workspace, out_dir).run())
    print(f"[worker] finished: {result['status']} after {result['steps']} steps", flush=True)
    return 0 if result["status"] in ("done", "gave_up", "step_limit", "timeout") else 1


if __name__ == "__main__":
    sys.exit(main())
