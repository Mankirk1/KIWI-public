"""Ollama guard: make sure no Ollama server competes with llama-swap for RAM and CPU.

agentbox runs its models on the CPU with ~18 GB of weights; an Ollama model loaded at
the same time can push WSL into swapping or get the model server killed. The guard looks
for Ollama in the three places it can live on a Windows + WSL machine:

  windows  the Ollama desktop app ("ollama app.exe") and its server/runner processes,
           seen from WSL through Windows interop (tasklist.exe / taskkill.exe)
  wsl      an `ollama` process inside the WSL distro itself (e.g. a systemd service)
  docker   a container whose image or name contains "ollama"
  port     anything answering on 127.0.0.1:11434 (Ollama's default port)

and then, depending on `guards.ollama` in config.yaml, stops it, aborts the run, or
only warns. What it stopped is restarted at the end of the run when
`guards.restore_ollama` is true.
"""
from __future__ import annotations

import csv
import io
import shutil
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .util import CmdResult, log, run_cmd

WINDOWS_IMAGES = ("ollama app.exe", "ollama.exe", "ollama_llama_server.exe")
OLLAMA_PORT = 11434
# Default per-user install location of the Ollama desktop app on Windows.
WINDOWS_APP = r"%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"


class GuardError(Exception):
    pass


@dataclass
class Finding:
    where: str          # windows | wsl | docker | port
    detail: str
    ref: str = ""       # image name, pid or container id used to stop/restore it

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


@dataclass
class OllamaGuard:
    mode: str = "stop"
    restore: bool = True
    runner: Callable[..., CmdResult] = run_cmd
    which: Callable[[str], Optional[str]] = shutil.which
    port_open: Optional[Callable[[], bool]] = None
    sleep: Callable[[float], None] = time.sleep
    stopped: list[Finding] = field(default_factory=list)

    # ------------------------------------------------------------------ detect
    def _port_open(self) -> bool:
        if self.port_open is not None:
            return self.port_open()
        try:
            with socket.create_connection(("127.0.0.1", OLLAMA_PORT), timeout=1.0):
                return True
        except OSError:
            return False

    def scan(self) -> list[Finding]:
        found: list[Finding] = []
        # Windows (through WSL interop)
        if self.which("tasklist.exe"):
            r = self.runner(["tasklist.exe", "/FO", "CSV", "/NH"], timeout=30)
            if r.ok:
                seen: set[str] = set()
                for row in csv.reader(io.StringIO(r.out)):
                    if row and row[0].strip().lower() in WINDOWS_IMAGES and row[0].lower() not in seen:
                        seen.add(row[0].lower())
                        pid = row[1] if len(row) > 1 else ""
                        found.append(Finding("windows", f"{row[0]} is running (pid {pid})", row[0]))
        # Inside WSL / Linux
        if self.which("pgrep"):
            r = self.runner(["pgrep", "-x", "ollama"], timeout=10)
            for pid in r.out.split():
                found.append(Finding("wsl", f"ollama process in WSL (pid {pid})", pid))
        # Docker containers
        if self.which("docker"):
            r = self.runner(["docker", "ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Ports}}"], timeout=30)
            if r.ok:
                for line in r.out.splitlines():
                    parts = (line.split("\t") + ["", "", "", ""])[:4]
                    cid, image, name, ports = parts
                    # By name/image, or by publishing Ollama's port (e.g. the labs/multi-llm
                    # containers, which run `ollama serve` inside a custom image).
                    if "ollama" in image.lower() or "ollama" in name.lower() or f":{OLLAMA_PORT}->" in ports:
                        found.append(Finding("docker", f"container {name} ({image})", cid))
        # Port 11434 (catches anything the checks above cannot see, e.g. mirrored networking)
        if not found and self._port_open():
            found.append(Finding("port", f"something is listening on 127.0.0.1:{OLLAMA_PORT}"))
        return found

    # ------------------------------------------------------------------ act
    def _stop(self, f: Finding) -> bool:
        if f.where == "windows":
            r = self.runner(["taskkill.exe", "/F", "/IM", f.ref], timeout=30)
            return r.ok
        if f.where == "docker":
            return self.runner(["docker", "stop", f.ref], timeout=120).ok
        if f.where == "wsl":
            return self.runner(["kill", f.ref], timeout=10).ok
        return False

    def enforce(self, when: str) -> list[Finding]:
        """Check and act according to the mode. Raises GuardError when the run must not
        continue. Returns what was found."""
        if self.mode == "off":
            return []
        found = self.scan()
        if not found:
            return []
        unknown = [f for f in found if f.where == "port"]
        actionable = [f for f in found if f.where != "port"]
        if unknown and not actionable:
            # Something answers on Ollama's port but we cannot tell what it is: never kill blindly.
            log.warning("Ollama guard (%s): %s - not an Ollama process I can identify; continuing", when, unknown[0])
            return found
        summary = "; ".join(str(f) for f in actionable)
        if self.mode == "warn":
            log.warning("Ollama guard (%s): %s - continuing because guards.ollama is 'warn'", when, summary)
            return found
        if self.mode == "abort":
            raise GuardError(f"Ollama is running ({summary}). Quit it, or set guards.ollama: stop in config.yaml")
        # mode == stop. Stop the tray app before the server, otherwise the app restarts the server.
        order = sorted(actionable, key=lambda f: (f.where != "windows", f.ref.lower() != "ollama app.exe"))
        for f in order:
            if self._stop(f):
                log.warning("Ollama guard (%s): stopped %s", when, f)
                if f.where in ("windows", "docker"):
                    self.stopped.append(f)
            else:
                log.warning("Ollama guard (%s): could not stop %s", when, f)
        self.sleep(2)
        still = [f for f in self.scan() if f.where != "port"]
        if still:
            raise GuardError("could not stop Ollama: " + "; ".join(str(f) for f in still)
                             + ". Quit Ollama yourself (tray icon -> Quit Ollama), or for a WSL service run "
                             "`sudo systemctl stop ollama`, then run again.")
        return found

    def restore_all(self) -> None:
        if not (self.restore and self.stopped):
            return
        for f in self.stopped:
            if f.where == "docker":
                r = self.runner(["docker", "start", f.ref], timeout=120)
            elif f.where == "windows" and f.ref.lower() == "ollama app.exe" and self.which("cmd.exe"):
                # The app starts its own server again. cwd on a Windows drive avoids cmd's UNC warning.
                r = self.runner(["cmd.exe", "/c", "start", "", WINDOWS_APP], cwd="/mnt/c", timeout=30)
            else:
                continue
            log.info("Ollama guard: restored %s (%s)", f, "ok" if r.ok else f"failed: {r.err.strip()[:200]}")
        self.stopped.clear()
