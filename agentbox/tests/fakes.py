"""In-process fakes for end-to-end tests: an OpenAI-compatible model server with scripted
behaviour, a GitHub REST API backed by local bare git repos, and a Docker-free sandbox."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


class _Server:
    def __init__(self, handler_cls, host: str = "127.0.0.1", port: int = 0):
        self.httpd = ThreadingHTTPServer((host, port), handler_cls)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class _JSONHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ============================================================================ fake LLM
PLAN = {
    "repo_name": "calc-lib",
    "description": "A tiny calculator library",
    "stack": {"language": "python", "setup_command": "true",
              "test_command": "python3 -m unittest discover -s tests -t . -q", "lint_command": ""},
    "tasks": [
        {"id": "T1", "title": "Project skeleton", "kind": "scaffold", "description": "Create the package.",
         "acceptance_criteria": ["calc package importable", "test suite runs"], "depends_on": [], "files_hint": []},
        {"id": "T2", "title": "Add function", "kind": "feature", "description": "calc.ops.add(a, b)",
         "acceptance_criteria": ["add(2, 3) == 5", "add(-1, 1) == 0"], "depends_on": ["T1"], "files_hint": ["calc/ops.py"]},
        {"id": "T3", "title": "Multiply function", "kind": "feature", "description": "calc.ops.mul(a, b)",
         "acceptance_criteria": ["mul(2, 3) == 6", "mul(0, 5) == 0"], "depends_on": ["T2"], "files_hint": ["calc/ops.py"]},
    ],
}

TEST_CMD = PLAN["stack"]["test_command"]

# Outline checks for the calc plan: expected values are the formula applied to the inputs.
CHECKS = {"checks": [
    {"id": "C1", "task": "T2", "kind": "value", "source": "add", "imports": ["calc.ops"], "expr": "calc.ops.add(2, 3)",
     "expect": "2 + 3"},
    {"id": "C2", "task": "T2", "kind": "value", "source": "add", "imports": ["calc.ops"], "expr": "calc.ops.add(-1, 1)",
     "expect": "-1 + 1"},
    {"id": "C3", "task": "T3", "kind": "value", "source": "mul", "imports": ["calc.ops"], "expr": "calc.ops.mul(2, 3)",
     "expect": "2 * 3"},
    {"id": "C4", "task": "T2", "kind": "value", "source": "add", "imports": ["calc.ops"], "expr": "calc.ops.add(2, 2)",
     "expect": "17"},   # 17 is in no outline: dropped by the validator
]}
DOC_ADD = 'def add(a, b):\n    """Add two numbers and return the sum."""\n    return a + b\n'


def _call(name, **args):
    return {"id": "call_" + os.urandom(4).hex(), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLMHandler(_JSONHandler):
    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "qwen-coder"}, {"id": "gemma-reviewer"}]})
        self._send(404, {})

    def do_POST(self):
        req = self._body()
        owner: FakeLLM = self.server.owner  # type: ignore[attr-defined]
        owner.requests.append(req)
        msg = owner.route(req)
        self._send(200, {"choices": [{"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                         "timings": {"predicted_per_second": 20.0, "prompt_per_second": 300.0}})


class FakeLLM(_Server):
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.requests: list[dict] = []
        self.plan_calls = 0
        self.review_calls: dict[str, int] = {}
        self.reported: set[str] = set()  # titles whose locked tests the coder already reported
        self.checks_calls = 0
        super().__init__(FakeLLMHandler, host, port)

    # -------------------------------------------------------------- routing
    def route(self, req: dict) -> dict:
        msgs = req["messages"]
        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        if req.get("tools") and len(msgs) == 1:  # `agentctl doctor` tool-calling probe
            return {"role": "assistant", "content": "", "tool_calls": [_call("get_weather", city="Marseille")]}
        if req.get("tools"):
            return self.worker(msgs)
        if "sequenced backlog" in system:
            self.plan_calls += 1
            if self.plan_calls == 1:
                return {"role": "assistant", "content": "Here is my plan: it has tasks."}  # invalid -> repair path
            return {"role": "assistant", "content": json.dumps(PLAN)}
        if "executable checks" in system:
            self.checks_calls += 1
            asked = re.search(r"checks for task (\w+) only", msgs[-1]["content"]).group(1)
            mine = [c for c in CHECKS["checks"] if c["task"] == asked] or [CHECKS["checks"][0] | {"task": asked}]
            return {"role": "assistant", "content": json.dumps({"checks": mine})}
        if "skeptical staff engineer" in system:
            return {"role": "assistant", "content": json.dumps({"ok": True, "problems": []})}
        if "senior code reviewer" in system:
            return self.review(msgs[-1]["content"])
        if "adding new work" in system:
            return {"role": "assistant", "content": json.dumps({"tasks": [
                {"id": "X1", "title": "Subtract", "kind": "feature", "description": "calc.ops.sub",
                 "acceptance_criteria": ["sub(5, 3) == 2", "sub(0, 1) == -1"], "depends_on": ["T2"], "files_hint": []}]})}
        return {"role": "assistant", "content": "<think>planning...</think># Spec\n\nA calculator library with add and mul."}

    def review(self, user: str) -> dict:
        m = re.search(r"Issue #(\d+): (.+)", user)
        title = m.group(2) if m else "?"
        n = self.review_calls[title] = self.review_calls.get(title, 0) + 1
        if "Add function" in title and '"""Add two numbers' not in user:
            verdict = {"analysis": "Missing docstring required by conventions.", "approved": False,
                       "blocking_issues": ["add() must have a docstring starting 'Add two numbers'"], "suggestions": []}
        else:
            verdict = {"analysis": f"Looks correct (review {n}).", "approved": True, "blocking_issues": [],
                       "suggestions": ["consider type hints"]}
        return {"role": "assistant", "content": json.dumps(verdict)}

    def worker(self, msgs: list[dict]) -> dict:
        task = msgs[1]["content"]
        step = sum(1 for m in msgs if m["role"] == "assistant")
        title = re.search(r"Issue #\d+ \(\w+\): (.+)", task).group(1)
        tests_phase = "PHASE: TESTS FIRST" in task
        if step == 0:
            calls = self.first_step(title, tests_phase, task)
        elif step == 1:
            calls = [_call("run_command", command=TEST_CMD)]
        else:
            return {"role": "assistant", "content": "", "tool_calls": [_call("finish", summary=f"did {title}")]}
        return {"role": "assistant", "content": "", "tool_calls": calls}

    def first_step(self, title: str, tests_phase: bool, task: str) -> list[dict]:
        # Run 1/2's incident: a wrong value gets locked and the code is bent to match it.
        if "agentbox-test: wrong locked value" in task and title == "Add function":
            if tests_phase:
                right = "found a problem in the locked tests" in task
                return [_call("write_file", path="tests/test_add.py",
                              content="import unittest\nfrom calc.ops import add\n\nclass T(unittest.TestCase):\n"
                                      f"    def test_add(self):\n        self.assertEqual(add(2, 3), {5 if right else 6})\n")]
            if title in self.reported:
                return [_call("write_file", path="calc/ops.py", content=DOC_ADD)]
            if "disagrees with the owner's outline" in task:
                self.reported.add(title)
                return [_call("finish", status="gave_up", summary="The locked test contradicts the outline check.",
                              locked_test_problem="tests/test_add.py::test_add expects add(2, 3) == 6; check C1 says 2 + 3")]
            return [_call("write_file", path="calc/ops.py", content=DOC_ADD.replace("a + b", "a + b + 1"))]
        # Owner notes reach the coder's prompt; tests use them to trigger behaviours.
        if not tests_phase and "agentbox-test: report locked test" in task and title not in self.reported:
            self.reported.add(title)
            return [_call("finish", status="gave_up", summary="A locked test is wrong.",
                          locked_test_problem="tests/test_add.py::test_add expects add(2, 3) == 5 but the spec says 6")]
        if (not tests_phase and "agentbox-test: no changes after review" in task
                and ("Code review requested" in task or "no changes after the review" in task)):
            return [_call("finish", summary="The code is already correct; nothing to change.")]
        if not tests_phase and "agentbox-test: add conftest" in task:
            body = 'def add(a, b):\n    """Add two numbers and return the sum."""\n    return a + b\n'
            return [_call("write_file", path="tests/conftest.py", content="import builtins\n"),   # refused: tests locked
                    _call("run_command", command="printf 'import builtins\\n' > tests/conftest.py"),  # reverted afterwards
                    _call("write_file", path="calc/ops.py", content=body)]
        if title == "Project skeleton":
            return [_call("write_file", path="calc/__init__.py", content=""),
                    _call("write_file", path="calc/ops.py", content="# operations\n"),
                    _call("write_file", path="tests/__init__.py", content=""),
                    _call("write_file", path="tests/test_smoke.py",
                          content="import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")]
        if title == "Add function":
            if tests_phase:
                return [_call("write_file", path="tests/test_add.py",
                              content="import unittest\nfrom calc.ops import add\n\nclass T(unittest.TestCase):\n"
                                      "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add(-1, 1), 0)\n"),
                        _call("write_file", path="calc/ops.py", content="def add(a, b):\n    return a + b\n")]  # non-test: discarded
            if "Code review requested changes" in task:
                body = 'def add(a, b):\n    """Add two numbers and return the sum."""\n    return a + b\n'
            elif "Tests FAILED" in task:
                body = "def add(a, b):\n    return a + b\n"
            else:  # first attempt: buggy, and tries to cheat
                return [_call("write_file", path="calc/ops.py", content="def add(a, b):\n    return a - b\n"),
                        _call("write_file", path="tests/test_add.py", content="# gutted\n"),
                        _call("write_file", path="SPEC.md", content="hacked\n"),
                        _call("run_command", command="echo hacked > SPEC.md && mkdir -p .github && echo x > .github/evil.yml")]
            return [_call("write_file", path="calc/ops.py", content=body)]
        if title == "Multiply function":
            if tests_phase:
                return [_call("write_file", path="tests/test_mul.py",
                              content="import unittest\nfrom calc.ops import mul\n\nclass T(unittest.TestCase):\n"
                                      "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")]
            return [_call("edit_file", path="calc/ops.py", old_text="    return a + b\n",
                          new_text="    return a + b\n\n\ndef mul(a, b):\n    return 0\n")]  # always wrong
        if title == "Subtract":
            if tests_phase:
                return [_call("write_file", path="tests/test_sub.py",
                              content="import unittest\nfrom calc.ops import sub\n\nclass T(unittest.TestCase):\n"
                                      "    def test_sub(self):\n        self.assertEqual(sub(5, 3), 2)\n")]
            return [_call("write_file", path="calc/sub.py", content="def sub(a, b):\n    return a - b\n"),
                    _call("run_command", command="python3 - <<'EOF'\nopen('calc/ops.py','a').write('\\nfrom calc.sub import sub  # noqa\\n')\nEOF")]
        # human-filed issue
        if tests_phase:
            return [_call("write_file", path="tests/test_notes.py",
                          content="import unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n"
                                  "    def test_notes(self):\n        self.assertTrue(Path('NOTES.md').exists())\n")]
        return [_call("write_file", path="NOTES.md", content="# Notes\n")]


# ============================================================================ fake GitHub
def _git(*args, cwd=None):
    # No background gc/maintenance: a detached job still writing .git/objects made the temp-clone
    # cleanup in _merge fail after the merge had already been pushed (flaky on CI).
    r = subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                       cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout


class FakeGitHubHandler(_JSONHandler):
    def _route(self, method):
        gh: FakeGitHub = self.server.owner  # type: ignore[attr-defined]
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self._body() if method in ("POST", "PATCH", "PUT") else None
        with gh.lock:
            try:
                code, obj = gh.handle(method, u.path, q, body)
            except KeyError:
                code, obj = 404, {"message": "Not Found"}
        self._send(code, obj)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_PUT(self):
        self._route("PUT")


class FakeGitHub(_Server):
    def __init__(self, bare_root: Path):
        self.bare_root = bare_root
        self.lock = threading.Lock()
        self.repos: dict[str, dict] = {}
        super().__init__(FakeGitHubHandler)

    def bare(self, owner, repo) -> Path:
        return self.bare_root / owner / f"{repo}.git"

    def _issue_view(self, r, i):
        return {**i, "labels": [{"name": l} for l in i["labels"]]}

    def handle(self, method, path, q, body):
        parts = [p for p in path.split("/") if p]
        if path == "/user":
            return 200, {"login": "tester", "type": "User"}
        if parts[0] == "orgs" and len(parts) == 2:
            return 200, {"login": parts[1]}
        if parts[0] == "orgs" and parts[2] == "repos" and method == "POST":
            owner, name = parts[1], body["name"]
            b = self.bare(owner, name)
            b.parent.mkdir(parents=True, exist_ok=True)
            _git("init", "-q", "--bare", "-b", "main", str(b))
            self.repos[f"{owner}/{name}"] = {"issues": {}, "labels": set(), "prs": {}, "next": 1}
            return 201, {"name": name, "html_url": f"https://github.test/{owner}/{name}"}
        if parts[0] != "repos":
            return 404, {}
        owner, name = parts[1], parts[2]
        key = f"{owner}/{name}"
        if len(parts) == 3:
            return (200, {"name": name}) if key in self.repos else (404, {"message": "Not Found"})
        r = self.repos[key]
        rest = parts[3:]
        if rest[0] == "labels":
            if method == "POST":
                r["labels"].add(body["name"])
                return 201, {}
            return 200, ([{"name": l} for l in sorted(r["labels"])] if q.get("page", "1") == "1" else [])
        if rest[0] == "branches":
            return 403, {"message": "Upgrade to GitHub Pro"}
        if rest[0] == "issues" and len(rest) == 1:
            if method == "POST":
                n = r["next"]; r["next"] += 1
                r["issues"][n] = {"number": n, "title": body["title"], "body": body["body"], "labels": list(body["labels"]),
                                  "state": "open", "comments": []}
                return 201, {"number": n}
            if q.get("page", "1") != "1":
                return 200, []
            items = [self._issue_view(r, i) for i in r["issues"].values()
                     if q.get("state", "open") in ("all", i["state"])]
            return 200, items
        if rest[0] == "issues":
            n = int(rest[1])
            issue = r["issues"][n]
            if len(rest) == 2:
                if method == "PATCH":
                    if "labels" in body:
                        issue["labels"] = list(body["labels"])
                    if "state" in body:
                        issue["state"] = body["state"]
                return 200, self._issue_view(r, issue)
            if rest[2] == "comments":
                if method == "POST":
                    issue["comments"].append({"body": body["body"], "user": {"login": "tester", "type": "User"},
                                              "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                    return 201, {}
                return 200, (issue["comments"] if q.get("page", "1") == "1" else [])
        if rest[0] == "pulls" and len(rest) == 1:
            if method == "POST":
                n = r["next"]; r["next"] += 1
                r["prs"][n] = {"number": n, "head": body["head"], "body": body["body"], "title": body["title"], "state": "open"}
                r["issues"][n] = {"number": n, "title": body["title"], "body": body["body"], "labels": [], "state": "open",
                                  "comments": [], "pull_request": {}}
                return 201, {"number": n}
            head = q.get("head", ":").split(":", 1)[1]
            return 200, [p for p in r["prs"].values() if p["head"] == head and p["state"] == "open"]
        if rest[0] == "pulls":
            n = int(rest[1])
            pr = r["prs"][n]
            if len(rest) == 2:
                return 200, {**pr, "mergeable": True}
            if rest[2] == "merge":
                self._merge(owner, name, pr, body)
                pr["state"] = "closed"
                r["issues"][n]["state"] = "closed"
                for m in re.finditer(r"Closes #(\d+)", pr["body"]):
                    r["issues"][int(m.group(1))]["state"] = "closed"
                return 200, {"merged": True}
        if rest[0] == "actions":
            return 200, {"workflow_runs": []}
        return 404, {}

    def _merge(self, owner, name, pr, body):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            _git("clone", "-q", str(self.bare(owner, name)), d)
            env = ["-c", "user.name=gh", "-c", "user.email=gh@test"]
            _git("checkout", "-q", "main", cwd=d)
            _git(*env, "merge", "-q", "--squash", f"origin/{pr['head']}", cwd=d)
            _git(*env, "commit", "-q", "-m", body.get("commit_title", "merge"), cwd=d)
            _git("push", "-q", "origin", "main", cwd=d)
            _git("push", "-q", "origin", "--delete", pr["head"], cwd=d)

    # helpers for tests
    def add_human_issue(self, owner, name, title, body):
        r = self.repos[f"{owner}/{name}"]
        n = r["next"]; r["next"] += 1
        r["issues"][n] = {"number": n, "title": title, "body": body, "labels": ["agent:todo"], "state": "open", "comments": []}
        return n


# ============================================================================ fake sandbox
def make_fake_sandbox(cfg):
    from agentbox.sandbox import Sandbox, TestResult

    class LocalSandbox(Sandbox):
        """Runs the real worker and tests as local processes instead of containers."""

        def run_worker(self, workspace, task_dir, log_file, agent_token):
            env = dict(os.environ, AGENTBOX_WORKSPACE=str(workspace), PYTHONPATH=str(ROOT / "worker"))
            env.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)
            with open(log_file, "ab") as lf:
                p = subprocess.run([sys.executable, "-m", "agentbox_worker", str(task_dir / "task.json")],
                                   stdout=lf, stderr=subprocess.STDOUT, env=env, timeout=120)
            return p.returncode, False

        def run_tests(self, tree, setup_command, test_command, log_file, after=""):
            t0 = time.time()
            script = f"{setup_command} && {test_command}" + (f"\nrc=$?\n{after}\nexit $rc" if after.strip() else "")
            p = subprocess.run(["bash", "-c", script], cwd=tree, capture_output=True, text=True, timeout=300)
            out = p.stdout + p.stderr
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a") as f:
                f.write(out)
            return TestResult(passed=p.returncode == 0, exit_code=p.returncode, output=out,
                              timed_out=False, seconds=time.time() - t0)

    return LocalSandbox(cfg)


if __name__ == "__main__":  # used by the Docker sandbox test: python3 fakes.py llm 8080
    srv = FakeLLM("0.0.0.0", int(sys.argv[2]))
    print(f"fake LLM on {srv.url}", flush=True)
    threading.Event().wait()
