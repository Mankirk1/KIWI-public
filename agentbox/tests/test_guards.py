"""Tests for the Ollama guard with a simulated Windows/WSL/Docker environment.
Run:  python3 -m unittest tests.test_guards -v"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator")]

from agentbox.guards import GuardError, OllamaGuard  # noqa: E402
from agentbox.util import CmdResult  # noqa: E402


class FakeMachine:
    """Pretends to be WSL with Windows interop and Docker. Tracks what is running."""

    def __init__(self, windows=(), wsl=(), docker=(), killable=True):
        self.windows = list(windows)          # image names, e.g. "ollama app.exe"
        self.wsl = list(wsl)                  # pids
        self.docker = list(docker)            # (id, image, name)
        self.killable = killable
        self.calls: list[list[str]] = []

    def which(self, name):
        return f"/usr/bin/{name}"

    def run(self, args, cwd=None, env=None, timeout=None, check=False, input_text=None):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "tasklist.exe":
            rows = [f'"{img}","{1000 + i}","Console","1","50,000 K"' for i, img in enumerate(self.windows)]
            rows.append('"explorer.exe","42","Console","1","90,000 K"')
            return CmdResult(args, 0, "\n".join(rows) + "\n", "")
        if cmd == "taskkill.exe":
            img = args[-1]
            if self.killable and img in self.windows:
                # killing the tray app does not kill the server; each image is separate
                self.windows.remove(img)
                return CmdResult(args, 0, "SUCCESS", "")
            return CmdResult(args, 128, "", "ERROR: not found or access denied")
        if cmd == "pgrep":
            return CmdResult(args, 0 if self.wsl else 1, "\n".join(self.wsl), "")
        if cmd == "kill":
            if self.killable and args[1] in self.wsl:
                self.wsl.remove(args[1])
                return CmdResult(args, 0, "", "")
            return CmdResult(args, 1, "", "Operation not permitted")
        if cmd == "docker" and args[1] == "ps":
            return CmdResult(args, 0, "\n".join("\t".join(c) for c in self.docker), "")
        if cmd == "docker" and args[1] == "stop":
            self.docker = [c for c in self.docker if c[0] != args[2]]
            return CmdResult(args, 0, args[2], "")
        if cmd in ("docker", "cmd.exe"):
            return CmdResult(args, 0, "", "")
        return CmdResult(args, 127, "", "unknown")


def guard(machine, mode="stop", restore=True, port=False):
    return OllamaGuard(mode, restore, runner=machine.run, which=machine.which,
                       port_open=lambda: port, sleep=lambda s: None)


class GuardTests(unittest.TestCase):
    def test_nothing_running(self):
        m = FakeMachine()
        self.assertEqual(guard(m).enforce("start"), [])

    def test_stops_windows_app_then_server_and_restores_app(self):
        m = FakeMachine(windows=["ollama.exe", "ollama app.exe"])
        g = guard(m)
        found = g.enforce("start")
        self.assertEqual({f.ref for f in found}, {"ollama.exe", "ollama app.exe"})
        kills = [c[-1] for c in m.calls if c[0] == "taskkill.exe"]
        self.assertEqual(kills, ["ollama app.exe", "ollama.exe"])  # tray app first
        self.assertEqual(m.windows, [])
        g.restore_all()
        start = [c for c in m.calls if c[0] == "cmd.exe"]
        self.assertEqual(len(start), 1)
        self.assertIn("ollama app.exe", start[0][-1])

    def test_docker_container_stopped_and_restarted(self):
        m = FakeMachine(docker=[("abc123", "ollama/ollama:latest", "ollama"), ("def", "nginx", "web")])
        g = guard(m)
        g.enforce("start")
        self.assertEqual([c[0] for c in m.docker], ["def"])
        g.restore_all()
        self.assertIn(["docker", "start", "abc123"], m.calls)

    def test_container_publishing_ollama_port_detected(self):
        m = FakeMachine(docker=[("c1", "paired_llm_system-sherman_ai", "paired_llm_system-sherman_ai-1",
                                 "0.0.0.0:11434->11434/tcp"), ("c2", "agentbox-worker", "w", "")])
        found = guard(m).enforce("start")
        self.assertEqual([f.ref for f in found], ["c1"])

    def test_no_restore_when_disabled(self):
        m = FakeMachine(windows=["ollama app.exe"])
        g = guard(m, restore=False)
        g.enforce("start")
        g.restore_all()
        self.assertFalse(any(c[0] == "cmd.exe" for c in m.calls))

    def test_abort_mode(self):
        m = FakeMachine(windows=["ollama.exe"])
        with self.assertRaises(GuardError):
            guard(m, mode="abort").enforce("start")
        self.assertFalse(any(c[0] == "taskkill.exe" for c in m.calls))

    def test_warn_and_off_modes(self):
        m = FakeMachine(wsl=["777"])
        self.assertEqual(len(guard(m, mode="warn").enforce("start")), 1)
        self.assertEqual(m.wsl, ["777"])
        self.assertEqual(guard(m, mode="off").enforce("start"), [])

    def test_unstoppable_raises(self):
        m = FakeMachine(wsl=["777"], killable=False)
        with self.assertRaises(GuardError) as cm:
            guard(m).enforce("start")
        self.assertIn("systemctl stop ollama", str(cm.exception))

    def test_unknown_port_listener_is_never_killed(self):
        m = FakeMachine()
        found = guard(m, port=True).enforce("start")
        self.assertEqual([f.where for f in found], ["port"])
        self.assertFalse(any(c[0] in ("taskkill.exe", "kill") for c in m.calls))

    def test_orchestrator_turns_guard_error_into_stop(self):
        import tempfile
        from agentbox.config import Config
        from agentbox.loop import Orchestrator, StopRun
        import os
        os.environ.setdefault("GH_ADMIN_TOKEN", "x")
        cfg = Config.model_validate({"github": {"owner": "acme"}, "guards": {"ollama": "abort"},
                                     "paths": {"data_dir": tempfile.mkdtemp()}})
        o = Orchestrator(cfg)
        m = FakeMachine(windows=["ollama.exe"])
        o.guard = guard(m, mode="abort")
        with self.assertRaises(StopRun):
            o.check_guards("test")


if __name__ == "__main__":
    unittest.main()
