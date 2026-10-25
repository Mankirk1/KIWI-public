"""Integration test of the real Docker sandbox (orchestrator/agentbox/sandbox.py).
Requires Docker and a worker image; skipped otherwise.

  AGENTBOX_DOCKER_TESTS=1 AGENTBOX_TEST_IMAGE=agentbox-worker:latest python3 -m unittest tests.test_docker_sandbox -v

It starts the fake model server as a container named `llama-swap` on an *internal*
network, then runs the real worker container through Sandbox.run_worker and the test
runner through Sandbox.run_tests, and checks the network isolation."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator"), str(ROOT / "tests")]

from agentbox.config import Config  # noqa: E402
from agentbox.sandbox import Sandbox  # noqa: E402

IMAGE = os.environ.get("AGENTBOX_TEST_IMAGE", "agentbox-worker:latest")
NET = "agentbox-test-internal"
ENABLED = os.environ.get("AGENTBOX_DOCKER_TESTS") == "1" and shutil.which("docker") is not None


def sh(*args, check=True):
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


@unittest.skipUnless(ENABLED, "set AGENTBOX_DOCKER_TESTS=1 to run Docker integration tests")
class DockerSandboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sh("docker", "network", "rm", NET, check=False)
        sh("docker", "network", "create", "--internal", NET)
        sh("docker", "rm", "-f", "agentbox-test-llm", check=False)
        sh("docker", "run", "-d", "--name", "agentbox-test-llm", "--network", NET, "--network-alias", "llama-swap",
           "-v", f"{ROOT / 'tests'}:/tests:ro", "--entrypoint", "python3", IMAGE, "/tests/fakes.py", "llm", "8080")
        time.sleep(2)

    @classmethod
    def tearDownClass(cls):
        sh("docker", "rm", "-f", "agentbox-test-llm", check=False)
        sh("docker", "network", "rm", NET, check=False)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbox-docker-"))
        self.cfg = Config.model_validate({
            "github": {"owner": "acme"},
            "sandbox": {"worker_image": IMAGE, "network": NET, "llm_url_in_sandbox": "http://llama-swap:8080/v1",
                        "memory": "1g", "cpus": 1, "worker_timeout_min": 3, "test_timeout_min": 2},
            "paths": {"data_dir": str(self.tmp / "data")},
        })
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.sb = Sandbox(self.cfg)

    def test_worker_and_tests_in_containers(self):
        ws, task_dir = self.tmp / "ws", self.tmp / "task"
        ws.mkdir()
        (task_dir / "out").mkdir(parents=True)
        (ws / "AGENTS.md").write_text("rules\n")
        task = {
            "mode": "implement", "repo": "acme/calc-lib", "issue": 2, "task_id": "T1", "title": "Project skeleton",
            "kind": "scaffold", "description": "x", "acceptance_criteria": ["a", "b"], "files_hint": [], "feedback": "",
            "locked_tests": [], "stack": {"setup_command": "true", "test_command": "python3 -m unittest discover -s tests -t . -q"},
            "llm": {"base_url": "http://llama-swap:8080/v1", "model": "qwen-coder", "context_tokens": 32768},
            "max_steps": 10, "time_budget_s": 120, "mcp": {"enabled": False},
        }
        (task_dir / "task.json").write_text(json.dumps(task))
        code, timed_out = self.sb.run_worker(ws, task_dir, self.tmp / "worker.log", agent_token="")
        log = (self.tmp / "worker.log").read_text()
        self.assertFalse(timed_out, log)
        result = json.loads((task_dir / "out" / "result.json").read_text())
        self.assertEqual(result["status"], "done", log)
        self.assertTrue((ws / "calc" / "__init__.py").exists())
        self.assertEqual((ws / "tests" / "test_smoke.py").stat().st_uid, os.getuid())  # files stay yours

        tr = self.sb.run_tests(ws, "true", "python3 -m unittest discover -s tests -t . -q", self.tmp / "tests.log")
        self.assertTrue(tr.passed, tr.output)
        tr = self.sb.run_tests(ws, "true", "python3 -c 'import sys; sys.exit(3)'", self.tmp / "tests.log")
        self.assertFalse(tr.passed)
        self.assertEqual(tr.exit_code, 3)

    def test_console_scripts_on_path_in_login_shells(self):
        # pip installs a project's console scripts (e.g. `mechcalc`) to ~/.local/bin; the test
        # runner uses `bash -lc`, whose /etc/profile used to drop that directory from PATH.
        ws = self.tmp / "ws-path"
        ws.mkdir()
        check = 'case ":$PATH:" in *":$HOME/.local/bin:"*) exit 0 ;; *) echo "PATH=$PATH"; exit 1 ;; esac'
        tr = self.sb.run_tests(ws, "true", check, self.tmp / "path.log")
        self.assertTrue(tr.passed, tr.output)

    def test_isolation(self):
        code, out = self.sb.probe(
            "python3 -c \"import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('https://pypi.org', timeout=5)\" "
            "&& echo REACHED || echo BLOCKED")
        self.assertIn("BLOCKED", out)
        code, out = self.sb.probe("cat /proc/self/status | grep CapEff")
        self.assertIn("0000000000000000", out)  # all capabilities dropped
        code, out = self.sb.probe("python3 -c \"import urllib.request as u, json; "
                                  "print(json.load(u.build_opener(u.ProxyHandler({})).open('http://llama-swap:8080/v1/models'))['data'][0]['id'])\"")
        self.assertIn("qwen-coder", out)

    def test_timeout_kills_container(self):
        t0 = time.time()
        tr = Sandbox(self.cfg.model_copy(update={"sandbox": self.cfg.sandbox.model_copy(update={"test_timeout_min": 0.1})}))
        res = tr.run_tests(self.tmp, "true", "sleep 120", self.tmp / "t.log")
        self.assertTrue(res.timed_out)
        self.assertLess(time.time() - t0, 60)


if __name__ == "__main__":
    unittest.main()
