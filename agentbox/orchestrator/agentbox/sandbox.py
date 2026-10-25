"""Docker sandbox: runs the coding worker and the test runner in throwaway containers.

Container hardening (applied to both):
  * attached only to the `internal` Docker network: no direct internet, only the
    model server and the allow-listed egress proxy (package registries + GitHub API);
  * non-root user, all Linux capabilities dropped, no-new-privileges;
  * CPU, memory and process-count limits; hard wall-clock timeout (docker kill);
  * only the per-task workspace copy and task directory are mounted.
The test runner gets no GitHub token at all; the worker gets the read-only agent token
for the GitHub MCP server.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .util import log, run_cmd, tail_lines, truncate_middle


@dataclass
class TestResult:
    passed: bool
    exit_code: int
    output: str
    timed_out: bool
    seconds: float
    oracle: Optional[dict] = None      # outline checks' results, when they ran (agentbox/oracle.py)
    mutation: Optional[dict] = None    # mutation check's results, when it ran

    def summary(self, max_chars: int = 6000) -> str:
        status = "PASSED" if self.passed else ("TIMED OUT" if self.timed_out else f"FAILED (exit {self.exit_code})")
        return f"Tests {status} in {self.seconds:.0f}s\n\n" + truncate_middle(tail_lines(self.output, 250), max_chars)


class Sandbox:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sb = cfg.sandbox

    def _base_args(self, name: str, workspace: Path) -> list[str]:
        uid, gid = os.getuid(), os.getgid()
        proxy = self.sb.proxy_url
        no_proxy = "llama-swap,egress-proxy,localhost,127.0.0.1"
        return [
            "docker", "run", "--rm", "--name", name,
            "--network", self.sb.network,
            # Docker refuses --cpus above the host's CPU count; clamp so small machines still work.
            "--cpus", str(min(float(self.sb.cpus), float(os.cpu_count() or 1))),
            "--memory", self.sb.memory, "--memory-swap", self.sb.memory,
            "--pids-limit", str(self.sb.pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{uid}:{gid}",
            "--tmpfs", "/tmp:rw,exec,size=2g",
            "-v", f"{workspace}:/workspace",
            "-e", f"HTTP_PROXY={proxy}", "-e", f"HTTPS_PROXY={proxy}",
            "-e", f"http_proxy={proxy}", "-e", f"https_proxy={proxy}",
            "-e", f"NO_PROXY={no_proxy}", "-e", f"no_proxy={no_proxy}",
            "-e", f"npm_config_proxy={proxy}", "-e", f"npm_config_https_proxy={proxy}",
            "-e", "HOME=/home/agent",
        ]

    def _run_streaming(self, args: list[str], name: str, log_file: Path, timeout_s: int, env: Optional[dict] = None) -> tuple[int, bool]:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        with open(log_file, "ab") as lf:
            proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT, env=full_env)
            try:
                code = proc.wait(timeout=timeout_s)
                return code, False
            except subprocess.TimeoutExpired:
                log.warning("container %s exceeded %ds; killing", name, timeout_s)
                run_cmd(["docker", "kill", name], timeout=60)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return -1, True

    # ---------------------------------------------------------------- worker
    def run_worker(self, workspace: Path, task_dir: Path, log_file: Path, agent_token: str) -> tuple[int, bool]:
        name = f"agentbox-worker-{uuid.uuid4().hex[:8]}"
        args = self._base_args(name, workspace) + [
            "-v", f"{task_dir}:/task",
            # Passed by name so the token value never appears in the process list.
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "LLM_API_KEY",
            self.sb.worker_image,
            "python3", "-m", "agentbox_worker", "/task/task.json",
        ]
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": agent_token, "LLM_API_KEY": self.cfg.llm.api_key}
        log.info("worker container %s started (timeout %d min)", name, self.sb.worker_timeout_min)
        t0 = time.time()
        code, timed_out = self._run_streaming(args, name, log_file, self.sb.worker_timeout_min * 60, env)
        log.info("worker finished: exit=%s timed_out=%s after %.0fs", code, timed_out, time.time() - t0)
        return code, timed_out

    # ---------------------------------------------------------------- tests
    def run_tests(self, tree: Path, setup_command: str, test_command: str, log_file: Path,
                  after: str = "") -> TestResult:
        """`after`: shell lines run once the tests finish (the outline checks); `$rc` holds the
        tests' exit status, which stays the container's exit status."""
        name = f"agentbox-tests-{uuid.uuid4().hex[:8]}"
        script = "set -o pipefail\n"
        if setup_command.strip():
            script += f"echo '$ {setup_command.replace(chr(39), '')}'\n{setup_command}\nrc=$?\nif [ $rc -ne 0 ]; then echo 'SETUP FAILED'; exit $rc; fi\n"
        script += f"echo '$ {test_command.replace(chr(39), '')}'\n{test_command}\n"
        if after.strip():
            script += f"rc=$?\n{after}\nexit $rc\n"
        args = self._base_args(name, tree) + [
            "-w", "/workspace", self.sb.worker_image, "bash", "-lc", script,
        ]
        out_file = log_file
        t0 = time.time()
        start = out_file.stat().st_size if out_file.exists() else 0
        code, timed_out = self._run_streaming(args, name, out_file, self.sb.test_timeout_min * 60)
        with open(out_file, "rb") as f:
            f.seek(start)
            output = f.read().decode("utf-8", errors="replace")
        return TestResult(passed=(code == 0 and not timed_out), exit_code=code, output=output,
                          timed_out=timed_out, seconds=time.time() - t0)

    # ---------------------------------------------------------------- checks
    def image_exists(self) -> bool:
        return run_cmd(["docker", "image", "inspect", self.sb.worker_image], timeout=30).ok

    def network_exists(self) -> bool:
        return run_cmd(["docker", "network", "inspect", self.sb.network], timeout=30).ok

    def probe(self, command: str, timeout: int = 60) -> tuple[int, str]:
        """Run a one-off command in a sandbox container (used by `agentctl doctor`)."""
        name = f"agentbox-probe-{uuid.uuid4().hex[:8]}"
        tmp = self.cfg.work_dir / "_probe"
        tmp.mkdir(parents=True, exist_ok=True)
        args = self._base_args(name, tmp) + [self.sb.worker_image, "bash", "-lc", command]
        r = run_cmd(args, timeout=timeout)
        return r.code, (r.out + r.err)


def shell_quote(s: str) -> str:
    return shlex.quote(s)
