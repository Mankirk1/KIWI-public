"""End-to-end test of the whole orchestrator with fakes for the model server, GitHub and
Docker. Exercises: spec -> plan (with a JSON repair) -> repo + issues -> scaffold ->
tests-first + locked tests -> failing attempt -> review rejection -> merge -> CI install
-> needs-human hand-off -> report -> continue with a human-filed issue -> extend.
Run:  python3 -m unittest tests.test_e2e -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator"), str(ROOT / "worker"), str(ROOT / "tests")]

from fakes import FakeGitHub, FakeLLM, make_fake_sandbox  # noqa: E402

from agentbox.config import Config  # noqa: E402
from agentbox.loop import Orchestrator  # noqa: E402
from agentbox.util import setup_logging  # noqa: E402


def show(bare: Path, path: str) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "show", f"main:{path}"],
                          capture_output=True, text=True).stdout


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbox-e2e-"))
        self.llm = FakeLLM()
        self.gh = FakeGitHub(self.tmp / "remotes")
        os.environ["GH_ADMIN_TOKEN"] = "admin-token"
        os.environ.pop("GH_AGENT_TOKEN", None)
        self.cfg = Config.model_validate({
            "github": {"owner": "acme", "owner_type": "org", "api_url": self.gh.url,
                       "git_url": str(self.tmp / "remotes" / "{owner}" / "{repo}.git")},
            "llm": {"base_url": self.llm.url + "/v1", "retries": 0},
            "sandbox": {"llm_url_in_sandbox": self.llm.url + "/v1", "max_agent_steps": 10},
            "workflow": {"max_attempts_per_task": 3, "ci": {"mode": "advisory"}},
            "budget": {"stop_file": str(self.tmp / "STOP")},
            "guards": {"ollama": "off"},
            "paths": {"data_dir": str(self.tmp / "data")},
        })
        for d in (self.cfg.projects_dir, self.cfg.repos_dir, self.cfg.work_dir, self.cfg.logs_dir, self.cfg.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        setup_logging(self.tmp / "e2e.log", verbose=False)

    def tearDown(self):
        self.llm.close()
        self.gh.close()

    def orch(self) -> Orchestrator:
        o = Orchestrator(self.cfg)
        o.sandbox = make_fake_sandbox(self.cfg)
        o.gh.wait_for_ci = lambda owner, repo, sha, timeout_s: ("success", "fake CI")
        return o

    def test_full_lifecycle(self):
        outline = self.tmp / "calc.md"
        outline.write_text("# calc\nA calculator library with add and mul.\n")
        st = self.orch().new_project(outline)
        bare = self.gh.bare("acme", "calc-lib")
        status = {t.id: t.status for t in st.tasks}
        self.assertEqual(status, {"T1": "done", "T2": "done", "T3": "needs_human"}, self._debug(st))

        # Code that landed on main
        self.assertIn('"""Add two numbers', show(bare, "calc/ops.py"))
        self.assertIn("self.assertEqual(add(2, 3), 5)", show(bare, "tests/test_add.py"))  # locked tests intact
        self.assertNotIn("hacked", show(bare, "SPEC.md"))                                # protected file reverted
        self.assertEqual(show(bare, ".github/evil.yml"), "")                             # sneaky file dropped
        self.assertIn("unittest discover", show(bare, ".github/workflows/ci.yml"))      # CI added after scaffold
        self.assertIn("Run all tests", show(bare, "AGENTS.md"))

        # T2 history: tests-first, failed tests, review rejection, merge
        t2 = st.task("T2")
        outcomes = [h.outcome for h in t2.history]
        self.assertEqual(outcomes, ["tests_written_failing", "tests_failed", "changes_requested", "merged"], outcomes)
        fb = self.llm_feedback_for("Add function", "Tests FAILED")
        self.assertIn("reverted", fb)
        self.assertIn("SPEC.md", fb)

        # GitHub side: labels, comments, PR closed, hand-off comment, run report
        repo = self.gh.repos["acme/calc-lib"]
        issue = lambda tid: repo["issues"][st.task(tid).issue]
        self.assertEqual(issue("T2")["state"], "closed")
        self.assertIn("agent:done", issue("T2")["labels"])
        self.assertIn("agent:needs-human", issue("T3")["labels"])
        self.assertTrue(any("could not complete" in c["body"] for c in issue("T3")["comments"]))
        log_issue = repo["issues"][st.log_issue]
        self.assertTrue(any("needs a human" in c["body"] or "Needs your attention" in c["body"] for c in log_issue["comments"]))
        branches = subprocess.run(["git", "--git-dir", str(bare), "branch"], capture_output=True, text=True).stdout
        self.assertIn("agent/", branches)  # T3's work-in-progress branch was pushed for a human
        self.assertTrue(any(p.name.endswith(".md") for p in (self.cfg.reports_dir / "calc-lib").iterdir()))
        self.assertIn("1 need a human", st.runs[-1].stop_reason)

        # A human files a new issue and re-queues nothing else: `continue` picks it up.
        n = self.gh.add_human_issue("acme", "calc-lib", "Add a NOTES.md file",
                                    "Please add NOTES.md.\n\n- [ ] NOTES.md exists\n- [ ] it has a heading")
        st2 = self.orch().continue_project("calc-lib")
        new = st2.task_by_issue(n)
        self.assertIsNotNone(new)
        self.assertEqual(new.status, "done", self._debug(st2))
        self.assertIn("# Notes", show(bare, "NOTES.md"))

        # The owner re-queues T3 after commenting; it fails again but the guidance reaches the model.
        repo["issues"][st2.task("T3").issue]["labels"] = ["agent:todo"]
        repo["issues"][st2.task("T3").issue]["comments"].append(
            {"body": "Hint: mul should multiply", "user": {"login": "owner", "type": "User"}, "created_at": "2999-01-01T00:00:00Z"})
        st3 = self.orch().continue_project("calc-lib")
        self.assertEqual(st3.task("T3").status, "needs_human")
        self.assertTrue(any("Hint: mul should multiply" in json.dumps(r) for r in self.llm.requests))

        # Extend with a change request -> new X1 task, done
        st4 = self.orch().extend_project("calc-lib", "Add a subtract function")
        self.assertEqual(st4.task("X1").status, "done", self._debug(st4))
        self.assertIn("return a - b", show(bare, "calc/sub.py"))

    def test_stop_file_and_plan_only(self):
        outline = self.tmp / "calc.md"
        outline.write_text("calc")
        o = self.orch()
        self.assertIsNone(o.new_project(outline, plan_only=True))
        self.assertTrue((self.cfg.projects_dir / "calc-lib" / "plan.json").exists())
        self.assertEqual(self.gh.repos, {})  # plan-only never touches GitHub
        (self.tmp / "STOP").write_text("stop")
        st = self.orch().new_project(outline)
        self.assertIn("STOP", st.runs[-1].stop_reason)
        self.assertTrue(all(t.status == "todo" for t in st.tasks))

    # ------------------------------------------------------------ helpers
    def llm_feedback_for(self, title: str, marker: str) -> str:
        for r in self.llm.requests:
            if r.get("tools"):
                user = r["messages"][1]["content"]
                if title in user and marker in user:
                    return user
        return ""

    def _debug(self, st) -> str:
        return "\n".join(f"{t.id} {t.status}: " + " | ".join(f"{h.phase}/{h.outcome}: {h.note[:300]}" for h in t.history)
                         for t in st.tasks)


if __name__ == "__main__":
    unittest.main()
