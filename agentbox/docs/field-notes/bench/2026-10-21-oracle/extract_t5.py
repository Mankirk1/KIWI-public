"""Re-extract T5's checks with the tightened prompt and replace them in checks.json."""
import json, subprocess, sys, time
from pathlib import Path
from agentbox.config import load_config
from agentbox.llm import LLMClient
from agentbox.oracle import extract_checks, load_checks, save_checks
from agentbox.state import StateStore
out = Path(sys.argv[1]); cfg = load_config()
st = StateStore(cfg.projects_dir).load("mechcalc")
outline = (cfg.projects_dir / "mechcalc" / "outline.md").read_text()
spec = subprocess.run(["git", "-C", str(cfg.repos_dir / "mechcalc"), "show", "4b21461:SPEC.md"], capture_output=True, text=True, check=True).stdout
tasks = [t.model_dump() for t in st.tasks]
llm = LLMClient(cfg.llm); t0 = time.time()
new, dropped = extract_checks(llm, outline, spec, [t for t in tasks if t["id"] == "T5"] + [dict(t, kind="scaffold") for t in tasks if t["id"] != "T5"])
old = [c for c in load_checks(out) if c.task != "T5"]
allc = old + new
for i, c in enumerate(allc, 1): c.id = f"C{i}"
save_checks(out, allc)
(out / "extract-t5-metrics.json").write_text(json.dumps({"seconds": round(time.time() - t0), "t5_checks": [c.brief() for c in new],
    "dropped": dropped, "calls": llm.usage.calls, "prompt_tokens": llm.usage.prompt_tokens, "completion_tokens": llm.usage.completion_tokens}, indent=2))
print(f"T5: {len(new)} checks, {len(dropped)} dropped, {time.time()-t0:.0f}s, {llm.usage.calls} calls")
