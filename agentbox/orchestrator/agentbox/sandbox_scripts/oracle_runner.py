"""Runs the outline checks inside the test sandbox, after the project's tests (agentbox/oracle.py).
Standard library only; copied into the test tree as .agentbox/oracle_runner.py, never into git.

    python3 .agentbox/oracle_runner.py .agentbox/oracle.json

Prints one line: AGENTBOX-ORACLE {"results": [...], "dangling": [...]}.
"""
import ast
import importlib
import importlib.util
import json
import math
import os
import shlex
import subprocess
import sys

MARK = "AGENTBOX-ORACLE "
ROOT = os.getcwd()


def short(v, n=200):
    s = repr(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def namespace(imports):
    ns = {"math": math}
    for m in imports:
        importlib.import_module(m)
        top = m.split(".")[0]
        ns[top] = sys.modules[top]
    return ns


def same(actual, expect, rel):
    if isinstance(expect, bool) or isinstance(actual, bool):
        return actual == expect
    if isinstance(expect, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(actual, expect, rel_tol=rel, abs_tol=1e-15)
    return actual == expect


def run_value(c):
    try:
        ns = namespace(c.get("imports") or [])
    except ImportError as e:
        return "missing", "", f"cannot import: {e}"
    try:
        actual = eval(c["expr"], ns)  # noqa: S307 - project code, inside the sandbox
    except Exception as e:
        return "fail", "", f"raised {type(e).__name__}: {e}"
    if same(actual, c.get("expect_value"), float(c.get("rel") or 1e-9)):
        return "pass", short(actual), ""
    return "fail", short(actual), f"got {short(actual)}, the outline gives {c.get('expect')} = {short(c.get('expect_value'))}"


def run_raises(c):
    try:
        ns = namespace(c.get("imports") or [])
    except ImportError as e:
        return "missing", "", f"cannot import: {e}"
    want = c.get("error", "")
    try:
        actual = eval(c["expr"], ns)  # noqa: S307
    except Exception as e:
        names = [k.__name__ for k in type(e).__mro__]
        if want in names:
            return "pass", type(e).__name__, ""
        return "fail", type(e).__name__, f"raised {type(e).__name__}: {e} (the outline expects {want})"
    return "fail", short(actual), f"returned {short(actual)} instead of raising {want}"


def run_command(c):
    try:
        p = subprocess.run(shlex.split(c["run"]), capture_output=True, text=True, timeout=60, cwd=ROOT)
    except FileNotFoundError as e:
        return "missing", "", f"command not found: {e}"
    except subprocess.TimeoutExpired:
        return "fail", "", "did not finish within 60 s"
    problems = []
    if c.get("exit_code") is not None and p.returncode != c["exit_code"]:
        problems.append(f"exit status {p.returncode}, expected {c['exit_code']}")
    out = p.stdout.strip()
    kind = c.get("stdout") or "any"
    is_json = False
    try:
        parsed = json.loads(out)
        is_json = True
    except ValueError:
        parsed = None
    if kind == "json_object" and not isinstance(parsed, dict):
        problems.append("stdout is not one JSON object" + (f" (it is a JSON {type(parsed).__name__})" if is_json else ""))
    elif kind == "text" and (not out or is_json):
        problems.append("stdout is " + ("empty" if not out else "JSON") + ", expected readable text")
    elif kind == "empty" and out:
        problems.append("stdout is not empty")
    err_lines = [l for l in p.stderr.splitlines() if l.strip()]
    if c.get("stderr_lines") is not None and len(err_lines) != c["stderr_lines"]:
        problems.append(f"{len(err_lines)} line(s) on stderr, expected {c['stderr_lines']}"
                        + (f" (first: {err_lines[0][:120]!r}, last: {err_lines[-1][:120]!r})" if err_lines else ""))
    for s in c.get("contains") or []:
        if s not in p.stdout:
            problems.append(f"stdout does not contain {s!r}")
    actual = f"exit {p.returncode}; stdout {short(out, 120)}; stderr {short(p.stderr.strip(), 120)}"
    return ("fail" if problems else "pass"), actual, "; ".join(problems)


def local_module(name):
    """Is `name` a module of this project (found under the working directory)?"""
    try:
        spec = importlib.util.find_spec(name.split(".")[0])
    except (ImportError, ValueError):
        return False
    origin = getattr(spec, "origin", None) or ""
    locs = list(getattr(spec, "submodule_search_locations", None) or [])
    return any(os.path.realpath(p).startswith(os.path.realpath(ROOT) + os.sep) for p in [origin] + locs if p)


def package_of(path):
    """Dotted package of a source file, e.g. src/mechcalc/beams.py -> mechcalc."""
    parts = os.path.normpath(path).split(os.sep)[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def dangling(files):
    """`from X import Y` (also under `if TYPE_CHECKING:`) where X is this project's module and
    does not define Y: the T5 incident in run 3 (backlog 26)."""
    out = []
    for path in files:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if node.level:
                base = package_of(path).split(".") if package_of(path) else []
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join([p for p in base + ([mod] if mod else []) if p])
            if not mod or not local_module(mod):
                continue
            try:
                m = importlib.import_module(mod)
            except Exception as e:
                out.append(f"{path}:{node.lineno} imports from {mod}, which cannot be imported ({type(e).__name__}: {e})")
                continue
            for alias in node.names:
                if alias.name == "*" or hasattr(m, alias.name):
                    continue
                try:
                    importlib.import_module(f"{mod}.{alias.name}")
                except ImportError:
                    out.append(f"{path}:{node.lineno} imports {alias.name} from {mod}, which does not define it")
    return out


def main():
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    sys.path[:0] = [ROOT] + ([os.path.join(ROOT, "src")] if os.path.isdir(os.path.join(ROOT, "src")) else [])
    results = []
    for c in cfg.get("checks", []):
        fn = {"value": run_value, "raises": run_raises, "command": run_command}.get(c.get("kind"))
        try:
            status, actual, detail = fn(c) if fn else ("fail", "", f"unknown kind {c.get('kind')}")
        except Exception as e:  # a broken check must not hide the others
            status, actual, detail = "fail", "", f"check could not run: {type(e).__name__}: {e}"
        results.append({"id": c.get("id"), "status": status, "actual": actual, "detail": detail})
    try:
        found = dangling(cfg.get("changed") or [])
    except Exception as e:
        found = []
        print(f"oracle: import check failed: {e}", file=sys.stderr)
    print(MARK + json.dumps({"results": results, "dangling": found}), flush=True)


if __name__ == "__main__":
    main()
