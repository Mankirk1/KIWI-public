"""Unit tests for orchestrator and worker building blocks (no network, no Docker).
Run:  python3 -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator"), str(ROOT / "worker")]

from agentbox.gitops import Git  # noqa: E402
from agentbox.planner import Plan, StackSpec, TaskSpec, toposort, unverified_numbers, validate_plan  # noqa: E402
from agentbox.state import ProjectState, TaskState  # noqa: E402
from agentbox.util import extract_json, strip_think, truncate_middle  # noqa: E402
from agentbox.workspace import (export_tree, init_snapshot_git, is_test_path, non_test_changes,  # noqa: E402
                                protected_violations, runner_config_violations, sync_back)
from agentbox.loop import mentions_github, parse_issue_body, render  # noqa: E402
from agentbox.config import Config  # noqa: E402
from agentbox_worker.agent import compact, estimate_tokens, parse_text_tool_calls  # noqa: E402
from agentbox_worker.tools import LocalTools, ToolError  # noqa: E402
from agentbox_worker import prompts as wprompts  # noqa: E402


def cfg() -> Config:
    return Config.model_validate({"github": {"owner": "acme"}, "paths": {"data_dir": tempfile.mkdtemp()}})


class UtilTests(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('Sure!\n```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})
        self.assertEqual(extract_json('<think>hmm {"x":0}</think> result: {"b": "}"} trailing'), {"b": "}"})
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_strip_think_unterminated(self):
        self.assertEqual(strip_think("answer <think>never closed"), "answer")

    def test_truncate_middle(self):
        s = "a" * 5000 + "END"
        out = truncate_middle(s, 1000)
        self.assertLess(len(out), 1100)
        self.assertTrue(out.endswith("END"))


class PlannerTests(unittest.TestCase):
    def plan(self, tasks):
        return Plan(repo_name="demo-app", description="d",
                    stack=StackSpec(language="python", setup_command="true", test_command="pytest"), tasks=tasks)

    def t(self, i, deps=(), kind="feature"):
        return TaskSpec(id=i, title=i, kind=kind, description="x", acceptance_criteria=["a", "b"], depends_on=list(deps))

    def test_toposort_orders_dependencies(self):
        order = [t.id for t in toposort([self.t("T3", ["T2"]), self.t("T1"), self.t("T2", ["T1"])])]
        self.assertEqual(order, ["T1", "T2", "T3"])

    def test_cycle_and_unknown(self):
        with self.assertRaises(ValueError):
            toposort([self.t("A", ["B"]), self.t("B", ["A"])])
        with self.assertRaises(ValueError):
            toposort([self.t("A", ["Z"])])

    def test_validate_plan(self):
        good = self.plan([self.t("T1", kind="scaffold"), self.t("T2", ["T1"])])
        self.assertEqual(validate_plan(good, cfg()), [])
        bad = self.plan([self.t("T1"), self.t("T1")])
        bad.repo_name = "Bad Name"
        probs = validate_plan(bad, cfg())
        self.assertTrue(any("repo_name" in p for p in probs))
        self.assertTrue(any("unique" in p for p in probs))
        self.assertTrue(any("scaffold" in p for p in probs))

    def test_task_kind_is_required_in_schema(self):
        # The model must emit kind; a schema default let it be skipped under grammar mode.
        task_schema = Plan.model_json_schema()["$defs"]["TaskSpec"]
        self.assertIn("kind", task_schema["required"])

    def test_unverified_numbers_flags_values_the_planner_computed(self):
        def task(i, *criteria):
            return TaskSpec(id=i, title=i, kind="feature", description="x", acceptance_criteria=list(criteria))
        outline = "| steel | 200 GPa | 250 MPa | 7850 |\nI-beam: A = 2*b*tf + (h - 2*tf)*tw"
        plan = self.plan([
            task("T1", "IBeam(b=100, h=200, tf=10, tw=5).area returns `3400.0`",       # wrong, computed: flagged
               "get_material('steel').E returns `200e9`",                            # 200 GPa: found after scaling
               "density equals 7850"),                                               # verbatim
            task("T2", "main(['bad']) returns 2", "count is 3"),                     # small integers: skipped
            task("T3", "second_moment == pytest.approx(0.0833333)"),                  # computed: flagged
        ])
        flagged = unverified_numbers(plan, outline)
        self.assertEqual(len(flagged), 2)
        self.assertTrue(flagged[0].startswith("T1:") and "3400" in flagged[0])
        self.assertTrue(flagged[1].startswith("T3:"))
        self.assertEqual(unverified_numbers(self.plan([task("T1", "area returns 3400.0")]), "A = 3400"), [])


class StateTests(unittest.TestCase):
    def test_blocking_propagates_and_clears(self):
        st = ProjectState(repo="r", owner="o", tasks=[
            TaskState(id="A", title="A", status="needs_human"),
            TaskState(id="B", title="B", depends_on=["A"]),
            TaskState(id="C", title="C", depends_on=["B"]),
            TaskState(id="D", title="D"),
        ])
        changed = {t.id for t in st.refresh_blocked()}
        self.assertEqual(changed, {"B", "C"})
        self.assertEqual([t.id for t in st.ready_tasks()], ["D"])
        st.task("A").status = "done"
        st.refresh_blocked()
        self.assertEqual([t.id for t in st.ready_tasks()], ["B", "D"])


def make_repo(tmp: Path) -> Git:
    git = Git(tmp / "repo")
    git.dir.mkdir()
    git.run("init", "-q", "-b", "main")
    (git.dir / ".gitignore").write_text("node_modules/\n")
    (git.dir / "src").mkdir()
    (git.dir / "src" / "app.py").write_text("print('hi')\n")
    (git.dir / "tests").mkdir()
    (git.dir / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    (git.dir / "SPEC.md").write_text("spec\n")
    git.commit("init")
    return git


class WorkspaceTests(unittest.TestCase):
    def test_roundtrip_policy_and_safety(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            git = make_repo(tmp)
            files = git.ls_files()
            ws = tmp / "ws"
            export_tree(files, git.dir, ws)
            init_snapshot_git(ws)
            self.assertTrue((ws / ".git").is_dir())
            # simulate the model's edits
            (ws / "src" / "app.py").write_text("print('changed')\n")
            (ws / "src" / "new.py").write_text("x = 1\n")
            (ws / "SPEC.md").write_text("tampered\n")
            (ws / "tests" / "test_app.py").unlink()
            (ws / "node_modules").mkdir()
            (ws / "node_modules" / "big.js").write_text("ignored")
            os.symlink("/etc/passwd", ws / "evil")
            (ws / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n")
            notes = sync_back(ws, git.dir, files)
            self.assertTrue(any("symlink" in n for n in notes))
            self.assertFalse((git.dir / "evil").exists())
            self.assertFalse((git.dir / "node_modules").exists())
            changes = git.worktree_changes()
            paths = {p for _, p in changes}
            self.assertEqual(paths, {"src/app.py", "src/new.py", "SPEC.md", "tests/test_app.py"})
            bad = protected_violations(changes, [".github/**", "SPEC.md"])
            self.assertEqual(bad, ["SPEC.md"])
            for p in bad:
                git.restore_path(p)
            git.restore_path("tests/test_app.py")
            git.restore_path("src/new.py")
            self.assertEqual({p for _, p in git.worktree_changes()}, {"src/app.py"})
            self.assertEqual((git.dir / "SPEC.md").read_text(), "spec\n")
            self.assertFalse((git.dir / "src" / "new.py").exists())
            git.commit("change")
            self.assertEqual(git.diff_line_count("HEAD~1"), 2)
            # the sandbox's hook never reaches the real repo
            self.assertFalse((git.dir / ".git" / "hooks" / "pre-commit").exists())

    def test_test_path_detection(self):
        for p in ["tests/test_a.py", "pkg/tests/x.py", "src/foo.test.ts", "a/b_test.py", "conftest.py"]:
            self.assertTrue(is_test_path(p), p)
        for p in ["src/app.py", "testing.md", "src/contest.py"]:
            self.assertFalse(is_test_path(p), p)
        self.assertEqual(non_test_changes([("M", "src/a.py"), ("A", "tests/test_a.py")]), ["src/a.py"])


class WorkerToolTests(unittest.TestCase):
    def test_tools_confinement_and_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "AGENTS.md").write_text("rules")
            t = LocalTools(root, "implement", locked=["tests/test_locked.py"])
            self.assertIn("created", t.call("write_file", {"path": "src/a.py", "content": "x = 1\ny = 2\n"}))
            self.assertIn("1 replacement", t.call("edit_file", {"path": "/workspace/src/a.py", "old_text": "y = 2", "new_text": "y = 3"}))
            self.assertIn("y = 3", t.call("read_file", {"path": "src/a.py"}))
            for bad in ({"path": "../escape.txt", "content": ""}, {"path": "AGENTS.md", "content": ""},
                        {"path": "tests/test_locked.py", "content": ""}, {"path": ".github/workflows/x.yml", "content": ""}):
                with self.assertRaises(ToolError):
                    t.call("write_file", bad)
            with self.assertRaises(ToolError):
                t.call("edit_file", {"path": "src/a.py", "old_text": "nope", "new_text": "z"})
            out = t.call("run_command", {"command": "echo hello && exit 3"})
            self.assertTrue(out.startswith("exit code: 3") and "hello" in out)
            out = t.call("run_command", {"command": "sleep 30", "timeout_s": 5})
            self.assertIn("killed", out)
            self.assertIn("src/a.py", t.call("search", {"pattern": "y = 3"}))
            tt = LocalTools(root, "tests")
            with self.assertRaises(ToolError):
                tt.call("write_file", {"path": "src/b.py", "content": ""})
            self.assertIn("created", tt.call("write_file", {"path": "tests/test_b.py", "content": "pass\n"}))

    def test_runner_config_frozen_while_tests_are_locked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            locked = LocalTools(root, "implement", locked=["tests/test_a.py"])
            for path in ("tests/conftest.py", "conftest.py", "pytest.ini", "src/sitecustomize.py"):
                with self.assertRaises(ToolError):
                    locked.call("write_file", {"path": path, "content": ""})
            free = LocalTools(root, "implement")              # nothing locked: conftest is allowed
            self.assertIn("created", free.call("write_file", {"path": "tests/conftest.py", "content": ""}))
            tests_phase = LocalTools(root, "tests", locked=["tests/test_a.py"])
            self.assertIn("created", tests_phase.call("write_file", {"path": "tests/sub/conftest.py", "content": ""}))

    def test_text_tool_call_fallbacks(self):
        c = parse_text_tool_calls('<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>')
        self.assertEqual(c[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(c[0]["function"]["arguments"]), {"path": "a.py"})
        c = parse_text_tool_calls("<function=write_file>\n<parameter=path>\nx.py\n</parameter>\n<parameter=content>\nprint(1)\n</parameter>\n</function>")
        self.assertEqual(json.loads(c[0]["function"]["arguments"]), {"path": "x.py", "content": "print(1)"})

    def test_compaction_keeps_structure(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        for i in range(40):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 3000})
        before = estimate_tokens(msgs)
        head = [dict(m) for m in msgs[:2]]
        self.assertTrue(compact(msgs, 5000))
        self.assertLess(estimate_tokens(msgs), before)
        self.assertLessEqual(estimate_tokens(msgs), 5000)
        # system prompt and task message stay byte-identical (their prompt cache survives)
        self.assertEqual(msgs[:2], head)
        notes = [m for m in msgs[2:] if wprompts.CONTEXT_NOTE in (m.get("content") or "")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["role"], "tool")
        for i in range(40, 60):                        # the session goes on, then compacts again
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 3000})
        self.assertTrue(compact(msgs, 5000))
        self.assertEqual(msgs[:2], head)
        self.assertEqual(sum(wprompts.CONTEXT_NOTE in (m.get("content") or "") for m in msgs), 1)
        # every tool message still follows an assistant message with tool_calls
        for i, m in enumerate(msgs):
            if m["role"] == "tool":
                j = i
                while msgs[j]["role"] == "tool":
                    j -= 1
                self.assertEqual(msgs[j]["role"], "assistant")
                self.assertIn(m["tool_call_id"], [tc["id"] for tc in msgs[j]["tool_calls"]])


class GithubMentionTests(unittest.TestCase):
    def test_github_tools_only_when_the_task_mentions_issues(self):
        for text in ("see #12", "Attempt 1 in PR #9 is correct", "https://github.com/o/r/pull/7", "github.com/o/r/issues/3"):
            self.assertTrue(mentions_github(text), text)
        for text in ("IBeam area must be 2900", "color: #fff", "it&#39;s", "issue 12", ""):
            self.assertFalse(mentions_github(text), text)


class RunnerConfigTests(unittest.TestCase):
    def test_runner_config_violations(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            base = {"pyproject.toml": '[project]\nname = "x"\ndependencies = []\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
                    "setup.cfg": "[metadata]\nname = x\n"}
            (root / "pyproject.toml").write_text(base["pyproject.toml"].replace("dependencies = []", 'dependencies = ["rich"]'))
            (root / "setup.cfg").write_text(base["setup.cfg"] + "\n[tool:pytest]\naddopts = -p no:cacheprovider\n")
            changes = [("M", "pyproject.toml"), ("M", "setup.cfg"), ("A", "tests/conftest.py"), ("M", "src/a.py")]
            bad = runner_config_violations(changes, root, base.get)
            self.assertEqual(sorted(bad), ["setup.cfg", "tests/conftest.py"])   # a dependency change is fine
            (root / "pyproject.toml").write_text(base["pyproject.toml"].replace('"-q"', '"-q -p conftest_hack"'))
            self.assertEqual(runner_config_violations([("M", "pyproject.toml")], root, base.get), ["pyproject.toml"])


class TemplateTests(unittest.TestCase):
    def test_render_and_issue_parse(self):
        from agentbox.state import StackState
        txt = render("ci.yml", StackState(setup_command='pip install -e ".[dev]"\npip install x', test_command="python -m pytest -q"))
        self.assertIn('pip install -e ".[dev]" && pip install x', txt)
        import yaml
        doc = yaml.safe_load(txt)
        steps = doc["jobs"]["test"]["steps"]
        self.assertIn("python -m pytest -q", steps[-1]["run"])
        desc, crit = parse_issue_body("Do the thing\n\n- [ ] returns 200\n- [x] logs it\nmore")
        self.assertEqual(crit, ["returns 200", "logs it"])
        self.assertIn("Do the thing", desc)


if __name__ == "__main__":
    unittest.main()
