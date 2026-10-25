"""Live check B/C: run outline checks + mutation check in the real worker image on mechcalc.
    sandbox_run.py OUTDIR LABEL [planted]"""
import json, subprocess, sys, time
from pathlib import Path
from agentbox.config import load_config
from agentbox.oracle import judge, load_checks, stage, take_marked, ORACLE_MARK, MUTATION_MARK
from agentbox.sandbox import Sandbox

out, label = Path(sys.argv[1]), sys.argv[2]
planted = len(sys.argv) > 3
cfg = load_config()
repo = cfg.repos_dir / "mechcalc"
tree = out / f"tree-{label}"
subprocess.run(["rm", "-rf", str(tree)], check=True)
tree.mkdir(parents=True)
subprocess.run(["tar", "-x", "-C", str(tree)], check=True,
               input=subprocess.run(["git", "-C", str(repo), "archive", "4b21461"], capture_output=True, check=True).stdout)

PLANTS = [  # (file, old, new, the incident it imitates)
    ("src/mechcalc/materials.py", "E=193e9", "E=200e9", "run 2: stainless E 200 GPa instead of 193"),
    ("src/mechcalc/materials.py", "E=113.8e9", "E=114e9", "run 2: titanium E 114 GPa instead of 113.8"),
    ("src/mechcalc/materials.py", "yield_strength=880e6", "yield_strength=828e6", "run 2: titanium yield 828 MPa instead of 880"),
    ("src/mechcalc/cli.py", "print(json.dumps(result, indent=2))", "print(json.dumps(list(result.values()), indent=2))",
     "run 3: materials --json as an array, not one object"),
    ("src/mechcalc/cli.py", "except (ValueError, KeyError) as exc:", "except KeyError as exc:",
     "run 4 attempt 1: invalid geometry gives a traceback and exit 1"),
    ("src/mechcalc/cli.py", "print(_fmt_section(section))", "print(json.dumps({'area': section.area}))",
     "run 4 attempt 1: section prints JSON instead of readable text"),
    ("src/mechcalc/beams.py", "max_deflection = magnitude * length ** 3 / (3 * E * I)",
     "max_deflection = magnitude * length ** 3 / (48 * E * I)", "run 3 supervisor plant: cantilever point deflection /48"),
]
applied = []
if planted:
    for f, old, new, why in PLANTS:
        p = tree / f
        s = p.read_text()
        assert s.count(old) == 1, (f, old)
        p.write_text(s.replace(old, new))
        applied.append(why)

checks = load_checks(out)
src = ["src/mechcalc/sections.py", "src/mechcalc/materials.py", "src/mechcalc/beams.py", "src/mechcalc/cli.py"]
after = stage(tree, checks, src, [] if planted else src, "python -m pytest -q", mutation_max=40, mutation_budget_s=600)
cfg.sandbox.test_timeout_min = 20
t0 = time.time()
tr = Sandbox(cfg).run_tests(tree, 'pip install -e ".[dev]"', "python -m pytest -q", out / f"{label}.log", after=after)
secs = time.time() - t0
oracle, rest = take_marked(tr.output, ORACLE_MARK)
mutation, rest = take_marked(rest, MUTATION_MARK)
v = judge(oracle, mutation, checks, {t for t in ("T1", "T2", "T3", "T4", "T5", "T6")})
res = {"label": label, "planted": applied, "tests_passed": tr.passed, "seconds": round(secs),
       "line": v.line(), "passed": v.passed, "failed": v.failed, "dangling": v.dangling,
       "mutation": mutation, "test_tail": rest.splitlines()[-3:]}
(out / f"{label}-result.json").write_text(json.dumps(res, indent=2))
print(json.dumps({k: res[k] for k in ("label", "tests_passed", "seconds", "line")}, indent=1))
for f in v.failed:
    print("  FAIL", f["id"], f.get("brief", "")[:90], "|", f.get("detail", "")[:150])
for d in v.dangling:
    print("  IMPORT", d)
for s in v.survivors():
    print("  SURVIVED", s[:160])
