"""Live check A: the real planner model writes outline checks for mechcalc."""
import json, subprocess, sys, time
from pathlib import Path
from agentbox.config import load_config
from agentbox.llm import LLMClient
from agentbox.oracle import extract_checks, save_checks
from agentbox.state import StateStore

out = Path(sys.argv[1])
cfg = load_config()
st = StateStore(cfg.projects_dir).load("mechcalc")
outline = (cfg.projects_dir / "mechcalc" / "outline.md").read_text()
spec = subprocess.run(["git", "-C", str(cfg.repos_dir / "mechcalc"), "show", "4b21461:SPEC.md"],
                      capture_output=True, text=True, check=True).stdout
llm = LLMClient(cfg.llm)
t0 = time.time()
checks, dropped = extract_checks(llm, outline, spec, [t.model_dump() for t in st.tasks])
secs = time.time() - t0
save_checks(out, checks)
u = llm.usage
(out / "extract-metrics.json").write_text(json.dumps({
    "seconds": round(secs), "checks": len(checks), "dropped": dropped, "calls": u.calls,
    "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}, indent=2))
print(f"{len(checks)} checks, {len(dropped)} dropped, {secs/60:.1f} min, {u.calls} calls")
