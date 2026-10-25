"""Weak-test check inside the test sandbox (agentbox/oracle.py): plant one small error at a time in
the task's source files and rerun the tests. A planted error the tests still pass with (a
surviving mutant) shows behaviour the tests do not pin down. Standard library only; copied into
the test tree as .agentbox/mutation_runner.py, never into git.

    python3 .agentbox/mutation_runner.py .agentbox/mutation.json
      {"files": ["src/pkg/mod.py"], "test": "python -m pytest -q", "max": 20, "budget_s": 180}

Prints one line: AGENTBOX-MUTATION {"sites": n, "tested": n, "killed": n, "survived": [...], ...}.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import time

MARK = "AGENTBOX-MUTATION "
SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.Pow: ast.Mult,
        ast.FloorDiv: ast.Div, ast.Mod: ast.Mult}
CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
SYM = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**", ast.FloorDiv: "//", ast.Mod: "%",
       ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


def skip_ranges(tree):
    """Lines not worth mutating: docstrings, type annotations, `if __name__ == ...` and TYPE_CHECKING blocks."""
    lines = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
        if isinstance(node, ast.If):
            src = ast.unparse(node.test)
            if "__name__" in src or "TYPE_CHECKING" in src:
                lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for ann in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if ann is not None:
                lines.update(range(ann.lineno, (ann.end_lineno or ann.lineno) + 1))
    return lines


def sites(tree):
    """Mutation sites in a fixed order: (node index in ast.walk order, kind, description)."""
    skip = skip_ranges(tree)
    out = []
    for i, node in enumerate(ast.walk(tree)):
        line = getattr(node, "lineno", None)
        if line is None or line in skip:
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in SWAP:
            out.append((i, "op", f"`{SYM[type(node.op)]}` -> `{SYM[SWAP[type(node.op)]]}`", line))
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMP:
            out.append((i, "cmp", f"`{SYM[type(node.ops[0])]}` -> `{SYM[CMP[type(node.ops[0])]]}`", line))
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float) and not isinstance(node.value, bool):
            new = node.value + 1 if isinstance(node.value, int) else node.value * 1.5 + (1 if node.value == 0 else 0)
            out.append((i, "const", f"`{node.value!r}` -> `{new!r}`", line))
    return out


def mutate(source, index, kind):
    tree = ast.parse(source)
    for i, node in enumerate(ast.walk(tree)):
        if i != index:
            continue
        if kind == "op":
            node.op = SWAP[type(node.op)]()
        elif kind == "cmp":
            node.ops = [CMP[type(node.ops[0])]()]
        else:
            node.value = node.value + 1 if isinstance(node.value, int) else node.value * 1.5 + (1 if node.value == 0 else 0)
        break
    return ast.unparse(tree) + "\n"


def spread(items, n):
    """Pick n items spread evenly over the list, preferring operators (formulas) over constants."""
    ranked = [s for s in items if s[1][1] == "op"] + [s for s in items if s[1][1] == "cmp"] + [s for s in items if s[1][1] == "const"]
    if len(ranked) <= n:
        return ranked
    step = len(ranked) / n
    return [ranked[int(k * step)] for k in range(n)]


def clear_cache(path):
    shutil.rmtree(os.path.join(os.path.dirname(path) or ".", "__pycache__"), ignore_errors=True)


def run_tests(cmd, timeout):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout, env=env)
        return "killed" if p.returncode != 0 else "survived"
    except subprocess.TimeoutExpired:
        return "timeout"


def main():
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    t_start = time.time()
    budget = float(cfg.get("budget_s", 180))
    candidates = []
    originals = {}
    for path in cfg.get("files", []):
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        originals[path] = src
        lines = src.splitlines()
        for s in sites(tree):
            candidates.append((path, s, lines[s[3] - 1] if s[3] - 1 < len(lines) else ""))
    chosen = spread(candidates, int(cfg.get("max", 20)))
    t0 = time.time()
    base = run_tests(cfg["test"], 300)  # baseline (also warms caches); a red baseline makes mutants meaningless
    per_run = max(20.0, (time.time() - t0) * 3)
    result = {"sites": len(candidates), "tested": 0, "killed": 0, "timeouts": 0, "survived": [], "baseline": base}
    if base != "survived":
        result["error"] = "the tests do not pass before mutation"
        print(MARK + json.dumps(result), flush=True)
        return
    for path, (index, kind, change, line), code in chosen:
        if time.time() - t_start + per_run > budget:
            result["stopped"] = "time budget"
            break
        try:
            mutant = mutate(originals[path], index, kind)
            with open(path, "w", encoding="utf-8") as f:
                f.write(mutant)
            clear_cache(path)
            outcome = run_tests(cfg["test"], per_run)
        finally:
            with open(path, "w", encoding="utf-8") as f:
                f.write(originals[path])
            clear_cache(path)
        result["tested"] += 1
        if outcome == "survived":
            result["survived"].append({"file": path, "line": line, "change": change, "code": code.strip()[:160]})
        else:
            result["killed"] += 1
            result["timeouts"] += outcome == "timeout"
    result["seconds"] = round(time.time() - t_start, 1)
    print(MARK + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
