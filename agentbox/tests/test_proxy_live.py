"""Live test of the real egress proxy + internal network from docker-compose.yml.
Needs internet, Docker, the worker image and `docker compose up -d egress-proxy`.

  AGENTBOX_PROXY_TESTS=1 AGENTBOX_TEST_IMAGE=agentbox-worker:latest python3 -m unittest tests.test_proxy_live -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator")]

from agentbox.config import Config  # noqa: E402
from agentbox.sandbox import Sandbox  # noqa: E402

ENABLED = os.environ.get("AGENTBOX_PROXY_TESTS") == "1"
IMAGE = os.environ.get("AGENTBOX_TEST_IMAGE", "agentbox-worker:latest")


@unittest.skipUnless(ENABLED, "set AGENTBOX_PROXY_TESTS=1 (needs docker compose up -d egress-proxy)")
class EgressProxyTest(unittest.TestCase):
    def setUp(self):
        cfg = Config.model_validate({"github": {"owner": "acme"},
                                     "sandbox": {"worker_image": IMAGE, "cpus": 4, "memory": "1g"},
                                     "paths": {"data_dir": tempfile.mkdtemp()}})
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.sb = Sandbox(cfg)

    def code(self, url: str, extra: str = "") -> str:
        _, out = self.sb.probe(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 25 {extra} {url} || true", timeout=90)
        code = out.strip()[-3:]
        # Guard against a container that never ran (e.g. docker run rejected): must be an HTTP code.
        self.assertRegex(code, r"^\d{3}$", f"probe did not run: {out[-500:]}")
        return code

    def test_allowlisted_registries_reachable(self):
        self.assertEqual(self.code("https://pypi.org/simple/pip/"), "200")
        self.assertEqual(self.code("https://registry.npmjs.org/left-pad"), "200")
        self.assertIn(self.code("https://api.github.com/zen"), ("200", "403"))  # 403 = GitHub rate limit, still reached

    def test_other_sites_blocked(self):
        self.assertNotEqual(self.code("https://example.com/"), "200")
        self.assertNotEqual(self.code("https://huggingface.co/"), "200")

    def test_no_direct_route(self):
        self.assertNotEqual(self.code("https://pypi.org/", "--noproxy '*'"), "200")

    def test_pip_install_through_proxy(self):
        code, out = self.sb.probe("pip install --quiet --no-cache-dir six && python3 -c 'import six; print(\"six-ok\")'", timeout=240)
        self.assertIn("six-ok", out, out[-2000:])


if __name__ == "__main__":
    unittest.main()
