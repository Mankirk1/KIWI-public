"""Sentinel rules on synthetic transcripts shaped like the real incidents in the field notes."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))

from agentbox.sentinel import SentinelThread, scan_attempt, scan_history, scan_project  # noqa: E402


def attempt(root: Path, name: str, records: list[dict], locked=("tests/test_x.py",)) -> Path:
    d = root / name
    (d / "task" / "out").mkdir(parents=True)
    (d / "task" / "task.json").write_text(json.dumps({"locked_tests": list(locked)}))
    with (d / "task" / "out" / "transcript.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return d


def step(t, n, content="", tools=()):
    return {"t": t, "kind": "assistant", "step": n, "content": content, "tools": list(tools)}


def tool(t, name, args="{}", result="ok"):
    return {"t": t, "kind": "tool", "name": name, "args": args, "result": result}


T0 = 1_790_000_000.0


class SentinelRuleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sentinel-"))

    def rules(self, d):
        return [f.rule for f in scan_attempt(d)]

    def test_locked_test_conflict_in_the_coders_own_words(self):
        d = attempt(self.root, "T2-implement-a1-20261015-223924", [
            {"t": T0, "kind": "start"},
            step(T0 + 60, 1, "Now let me read the locked test file to understand exactly what tests are expected:"),
            step(T0 + 120, 2, "The test expects that an unknown material returns exit code 2."),
            step(T0 + 180, 3, "The expected value `92.01631169711767` doesn't match `937.5*pi/32 = 92.039`."),
            {"t": T0 + 200, "kind": "end"}])
        found = [f for f in scan_attempt(d) if f.rule == "locked_test_conflict"]
        self.assertEqual(len(found), 1)                   # normal talk about the locked tests is not flagged
        self.assertIn("step 3", found[0].evidence[0])
        self.assertEqual(found[0].severity, "high")

    def test_only_in_implement_mode_with_locked_tests(self):
        d = attempt(self.root, "T2-tests-a1-20261015-222232",
                    [step(T0, 1, "The implementation is already complete and correct.")])
        self.assertNotIn("locked_test_conflict", self.rules(d))
        d = attempt(self.root, "T3-implement-a1-20261016-142935",
                    [step(T0, 1, "The implementation is already complete and correct.")], locked=())
        self.assertNotIn("locked_test_conflict", self.rules(d))

    def test_env_path_and_runner_config(self):
        d = attempt(self.root, "T3-implement-a1-20261016-142935", [
            step(T0, 1, tools=["run_command"]),
            tool(T0 + 5, "run_command", '{"command": "pip install -e ."}',
                 "WARNING: The script mechcalc is installed in '/home/agent/.local/bin' which is not on PATH."),
            step(T0 + 60, 2, tools=["write_file"]),
            tool(T0 + 61, "write_file", '{"path": "tests/conftest.py", "content": "import builtins"}', "created tests/conftest.py"),
            step(T0 + 90, 3, tools=["write_file"]),
            tool(T0 + 91, "write_file", '{"path": "conftest.py"}',
                 "error: 'conftest.py' changes how the locked tests run and cannot be changed while they are locked")])
        rules = self.rules(d)
        self.assertIn("env_path", rules)
        self.assertEqual(rules.count("runner_config"), 2)

    def test_repeated_call_only_without_an_edit_in_between(self):
        same = '{"command": "python -m pytest -q"}'
        d = attempt(self.root, "T5-implement-a1-20261018-204900", [
            step(T0, 1, tools=["run_command"]), tool(T0 + 1, "run_command", same),
            step(T0 + 10, 2, tools=["edit_file"]), tool(T0 + 11, "edit_file", '{"path": "src/a.py"}'),
            step(T0 + 20, 3, tools=["run_command"]), tool(T0 + 21, "run_command", same),
            step(T0 + 30, 4, tools=["write_file"]), tool(T0 + 31, "write_file", '{"path": "src/a.py"}'),
            step(T0 + 40, 5, tools=["run_command"]), tool(T0 + 41, "run_command", same)])
        self.assertNotIn("repeated_call", self.rules(d))      # re-running tests after each edit is normal
        d = attempt(self.root, "T5-implement-a2-20261018-215538",
                    [x for i in range(3) for x in (step(T0 + 10 * i, i + 1, tools=["run_command"]),
                                                    tool(T0 + 10 * i + 1, "run_command", same))])
        self.assertIn("repeated_call", self.rules(d))

    def test_slow_steps_compactions_and_long_attempts(self):
        d = attempt(self.root, "T5-tests-a1-20261018-175732", [
            {"t": T0, "kind": "start"}, step(T0 + 60, 1), step(T0 + 60 + 7 * 60, 2),
            {"t": T0 + 600, "kind": "compact", "est_tokens": 16713}, step(T0 + 50 * 60, 3), {"t": T0 + 50 * 60, "kind": "end"}])
        rules = self.rules(d)
        self.assertEqual(rules.count("slow_step"), 2)
        self.assertIn("compaction", rules)
        self.assertIn("long_attempt", rules)

    def test_empty_attempts_from_the_task_history(self):
        task = {"id": "T2", "history": [
            {"ts": "2026-10-16T11:19:26+00:00", "phase": "review", "attempt": 1, "outcome": "changes_requested", "note": "x"},
            {"ts": "2026-10-16T11:39:22+00:00", "phase": "implement", "attempt": 2, "outcome": "no_changes", "note": "a"},
            {"ts": "2026-10-16T11:40:00+00:00", "phase": "owner", "attempt": 2, "outcome": "owner_note_at_retry", "note": ""},
            {"ts": "2026-10-16T11:56:11+00:00", "phase": "implement", "attempt": 3, "outcome": "no_changes", "note": "b"}]}
        found = scan_history(task)
        self.assertEqual([f.rule for f in found], ["empty_attempts"])
        self.assertEqual(found[0].evidence, ["b"])


class SentinelThreadTests(unittest.TestCase):
    def test_reports_each_new_finding_once(self):
        root = Path(tempfile.mkdtemp(prefix="sentinel-live-"))
        work = root / "work"
        work.mkdir()
        old = attempt(work, "T1-implement-a1-20261015-205930", [
            step(T0, 1, tools=["run_command"]),
            tool(T0 + 1, "run_command", "{}", "installed in '/home/agent/.local/bin' which is not on PATH")])
        old_tr = old / "task" / "out" / "transcript.jsonl"
        os.utime(old_tr, (T0 + 10, T0 + 10))                        # written long ago: not a live attempt
        reported = []
        th = SentinelThread(work, root / "sentinel.jsonl", reported.append, lambda: [], interval=3600)
        self.assertEqual(th.scan_once(), [])                     # findings from earlier runs are not news
        now = time.time()
        attempt(work, "T2-implement-a1-20261019-100000", [
            step(now, 1, "The expected value doesn't match the formula in SPEC.md."),
            step(now + 1, 2, tools=["run_command"]),
            tool(now + 2, "run_command", "{}", "WARNING: installed in '/home/agent/.local/bin' which is not on PATH"),
            step(now + 3, 3, tools=["run_command"]),
            tool(now + 4, "run_command", '{"x": 1}', "is not on PATH")])
        new = th.scan_once()
        self.assertEqual(sorted(f.rule for f in new), ["env_path", "locked_test_conflict"])   # env_path once per run
        self.assertEqual(th.scan_once(), [])
        self.assertEqual(len(reported), 2)
        lines = (root / "sentinel.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("evidence", json.loads(lines[0]))
        self.assertTrue(old.exists())

    def test_scan_project_orders_by_time(self):
        root = Path(tempfile.mkdtemp(prefix="sentinel-order-"))
        attempt(root, "T2-implement-a1-20261015-223924", [step(T0 + 100, 1, "The test appears to be wrong.")])
        attempt(root, "T1-implement-a1-20261015-205930", [step(T0, 1, tools=["run_command"]),
                                                          tool(T0 + 1, "run_command", "{}", "is not on PATH")])
        found = scan_project(root)
        self.assertEqual([f.task for f in found], ["T1", "T2"])


if __name__ == "__main__":
    unittest.main()
