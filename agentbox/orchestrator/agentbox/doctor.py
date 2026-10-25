"""`agentctl doctor`: checks every moving part and says how to fix what is broken."""
from __future__ import annotations

import json
import shutil
import time

from . import http
from .config import Config
from .github_api import GitHub
from .llm import LLMClient, LLMError
from .sandbox import Sandbox
from .util import run_cmd

OK, WARN, FAIL = "\033[32m OK \033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"

# The Settings > Power mode slider (plugged-in value). "Best power efficiency" cost ~10% of
# generation speed on the XPS 15 (measured with llama-bench, 2026-10-16).
POWER_MODES = {"961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency",
               "00000000-0000-0000-0000-000000000000": "Balanced",
               "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "Better performance",
               "ded574b5-45a0-4f42-8737-46345c09c238": "Best performance"}

# ~1,000 tokens of filler, so the prompt speed is measured on a realistic prompt instead of 20 tokens.
BENCH_CONTEXT = "Reference notes (ignore them):\n" + "\n".join(
    f"- note {i}: the orchestrator copies the work tree, runs the tests and records the outcome." for i in range(60))


def windows_power() -> tuple[bool, str] | None:
    """(on AC power, power mode) of the Windows host, read through WSL interop; None elsewhere."""
    if not shutil.which("powershell.exe"):
        return None
    r = run_cmd(["powershell.exe", "-NoProfile", "-Command",
                 "'battery=' + (Get-CimInstance Win32_Battery).BatteryStatus; 'overlay=' + (Get-ItemProperty "
                 "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Power\\User\\PowerSchemes').ActiveOverlayAcPowerScheme"],
                timeout=60)
    if not r.ok:
        return None
    vals = dict(line.strip().split("=", 1) for line in r.out.splitlines() if "=" in line)
    on_ac = vals.get("battery", "").strip() != "1"          # 1 = discharging; no battery = desktop
    overlay = vals.get("overlay", "").strip()
    return on_ac, POWER_MODES.get(overlay, overlay or "unknown")


class Doctor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.failures = 0

    def _p(self, status: str, what: str, hint: str = "") -> None:
        print(f"[{status}] {what}")
        if hint and status != OK:
            print(f"        → {hint}")
        if status == FAIL:
            self.failures += 1

    def run(self, quick: bool = False) -> int:
        cfg = self.cfg
        print("== Host")
        for tool in ("git", "docker"):
            self._p(OK if shutil.which(tool) else FAIL, f"{tool} is installed",
                    "Install it (see GUIDE.md step 3/4). For docker: enable WSL integration in Docker Desktop.")
        free_gb = shutil.disk_usage(cfg.paths.data).free / 1e9
        self._p(OK if free_gb > 20 else WARN, f"{free_gb:.0f} GB free in {cfg.paths.data}",
                "Free disk space; each project run keeps work copies under data/work (agentctl clean).")
        power = windows_power()
        if power:
            on_ac, mode = power
            self._p(OK if on_ac else WARN, "Windows is on " + ("AC power" if on_ac else "battery"),
                    "Plug in the charger: the nightly task only starts on AC power, and the CPU is throttled on battery.")
            self._p(WARN if mode == "Best power efficiency" else OK, f"Windows power mode (plugged in): {mode}",
                    "Settings → System → Power & battery → Power mode → Best performance "
                    "(Start-AgentRun.ps1 switches to it for each run).")

        print("== Other local LLM servers")
        from .guards import OllamaGuard
        found = OllamaGuard(cfg.guards.ollama).scan() if cfg.guards.ollama != "off" else []
        if not found:
            self._p(OK, f"no Ollama running (guard mode: {cfg.guards.ollama})")
        else:
            sev = WARN if cfg.guards.ollama in ("stop", "warn") else FAIL
            self._p(sev, "Ollama is running: " + "; ".join(str(f) for f in found),
                    "With guards.ollama: stop, agentctl stops it at the start of every run and restarts it after."
                    if cfg.guards.ollama == "stop" else "Quit Ollama (tray icon -> Quit Ollama) before running agentctl.")

        print("== Docker")
        r = run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
        self._p(OK if r.ok else FAIL, f"Docker daemon reachable ({r.out.strip() or r.err.strip()[:80]})",
                "Start Docker Desktop and enable Settings → Resources → WSL integration for your Ubuntu distro.")
        for c in ("agentbox-llama-swap", "agentbox-egress-proxy"):
            r = run_cmd(["docker", "inspect", "-f", "{{.State.Status}}", c], timeout=30)
            st = r.out.strip()
            self._p(OK if st == "running" else FAIL, f"container {c}: {st or 'missing'}",
                    "cd ~/agentbox && docker compose up -d   (then: docker compose logs " + c.split('-', 1)[1] + ")")
        sb = Sandbox(cfg)
        self._p(OK if sb.network_exists() else FAIL, f"docker network {cfg.sandbox.network} exists",
                "It is created by docker compose up -d.")
        self._p(OK if sb.image_exists() else FAIL, f"worker image {cfg.sandbox.worker_image} built",
                "Run scripts/build-worker.sh")

        print("== Model server (llama-swap)")
        base = cfg.llm.base_url.rstrip("/").rsplit("/v1", 1)[0]
        try:
            http.request("GET", base + "/health", timeout=10, retries=0)
            self._p(OK, f"llama-swap answering at {base}")
        except http.HTTPError as e:
            self._p(FAIL, f"llama-swap not reachable at {base} ({e})",
                    "docker compose logs llama-swap ; check the published port and llm.base_url in config.yaml")
        llm = LLMClient(cfg.llm)
        try:
            models = set(llm.list_models())
        except Exception as e:
            models = set()
            self._p(FAIL, f"could not list models: {e}")
        for role, rc in cfg.llm.roles.items():
            self._p(OK if rc.model in models else FAIL, f"role {role} → model '{rc.model}' is configured in llama-swap",
                    f"Model ids available: {sorted(models)}. Fix llm.roles.{role}.model or llama-swap/config.yaml.")
        if not quick:
            self._bench_models(llm)

        print("== GitHub")
        g = cfg.github
        try:
            admin = GitHub(g.admin_token, g.api_url)
            me = admin.me()
            self._p(OK, f"admin token works (acting as {me.get('login')})")
            if g.owner_type == "org":
                org = admin.org(g.owner)
                self._p(OK if org else FAIL, f"organization '{g.owner}' visible",
                        "Create the org, and make the fine-grained token's resource owner that org.")
            elif me.get("login", "").lower() != g.owner.lower():
                self._p(FAIL, f"github.owner '{g.owner}' differs from token user '{me.get('login')}'",
                        "With owner_type: user, owner must be your own username.")
        except Exception as e:
            self._p(FAIL, f"admin token: {e}", f"Set {g.admin_token_env} in ~/agentbox/.env (see GUIDE.md step 6).")
        if g.agent_token:
            try:
                GitHub(g.agent_token, g.api_url).me()
                self._p(OK, "agent (read-only, MCP) token works")
            except Exception as e:
                self._p(FAIL, f"agent token: {e}", f"Check {g.agent_token_env} in ~/agentbox/.env")
        else:
            self._p(WARN, "no agent token: the coder will run without the GitHub MCP server",
                    f"Set {g.agent_token_env} in ~/agentbox/.env to enable it.")

        if sb.image_exists() and sb.network_exists():
            print("== Sandbox")
            self._sandbox_checks(sb, models)

        print()
        if self.failures:
            print(f"{self.failures} problem(s) found. Fix the FAIL lines above, then run `agentctl doctor` again.")
        else:
            print("All checks passed. Try: agentctl plan-only examples/todo-api.md")
        return 1 if self.failures else 0

    # ------------------------------------------------------------------
    def _bench_models(self, llm: LLMClient) -> None:
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "Get the weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        seen: set[str] = set()
        for role, rc in self.cfg.llm.roles.items():
            if rc.model in seen:
                continue
            seen.add(rc.model)
            print(f"   loading and testing '{rc.model}' (the first load can take a few minutes)...")
            t0 = time.time()
            try:
                msg = llm.chat(role, [{"role": "user", "content": BENCH_CONTEXT +
                                       "\n\nWrite a Python function that reverses a string. Code only."}],
                               max_tokens=200, chat_template_kwargs={"enable_thinking": False},
                               cache_prompt=False)  # a cached prompt would report only the uncached tail
                tm = msg.get("_timings") or {}
                gen = tm.get("predicted_per_second")
                pp = tm.get("prompt_per_second")
                self._p(OK, f"{rc.model}: answered in {time.time() - t0:.0f}s (incl. load); "
                            f"generation {gen or 0:.1f} tok/s, prompt {pp or 0:.0f} tok/s "
                            f"(on {tm.get('prompt_n', '?')} tokens)")
                if gen and gen < 5:
                    self._p(WARN, f"{rc.model} generates under 5 tok/s",
                            "Plug in the charger, close other apps, check `nvidia-smi` in Windows, or use a smaller quant/profile.")
            except LLMError as e:
                self._p(FAIL, f"{rc.model}: {e}", "docker compose logs llama-swap  (look for 'out of memory' or a missing .gguf file)")
                continue
            if role in ("coder", "planner"):
                try:
                    msg = llm.chat(role, [{"role": "user", "content": "What's the weather in Marseille? Use the tool."}],
                                   tools=tools, max_tokens=300, chat_template_kwargs={"enable_thinking": False})
                    calls = msg.get("tool_calls") or []
                    good = bool(calls) and calls[0].get("function", {}).get("name") == "get_weather"
                    self._p(OK if good else FAIL, f"{rc.model}: native tool calling works",
                            "llama-server needs --jinja; update the llama-swap image; see GUIDE.md troubleshooting.")
                except LLMError as e:
                    self._p(FAIL, f"{rc.model}: tool-call test errored: {e}")

    def _sandbox_checks(self, sb: Sandbox, models: set[str]) -> None:
        code, out = sb.probe("touch $HOME/.w && echo home-ok; python3 --version; node --version; github-mcp-server --version 2>&1 | head -1")
        self._p(OK if code == 0 and "home-ok" in out else FAIL, "worker image runs as your user with a writable HOME: "
                + " | ".join(l for l in out.strip().splitlines()[1:4]),
                "Rebuild with scripts/build-worker.sh (it passes your uid).")
        def http(cmd: str) -> str:
            _, out = sb.probe(cmd)
            code = out.strip()[-3:]
            return code if code.isdigit() else f"probe failed: {out.strip()[-160:]}"

        c = http("curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://pypi.org/simple/pip/")
        self._p(OK if c == "200" else FAIL, f"sandbox can reach PyPI through the egress proxy ({c})",
                "scripts/proxy-log.sh cache (startup errors) and scripts/proxy-log.sh denied; check proxy/allowlist.txt")
        c = http("curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://example.com/ || true")
        self._p(OK if c.isdigit() and c != "200" else FAIL, f"sandbox is blocked from non-allow-listed sites ({c})",
                "The egress allowlist is not being enforced - check proxy/squid.conf.")
        c = http("curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 10 https://pypi.org/ || true")
        self._p(OK if c.isdigit() and c != "200" else FAIL, f"sandbox has no direct internet ({c})",
                "The sandbox network must be `internal: true` in docker-compose.yml.")
        url = self.cfg.sandbox.llm_url_in_sandbox.rstrip("/") + "/models"
        code, out = sb.probe(f"curl -s --max-time 15 {url}")
        try:
            ok = bool(json.loads(out).get("data"))
        except Exception:
            ok = False
        self._p(OK if ok else FAIL, f"sandbox can reach the model server at {self.cfg.sandbox.llm_url_in_sandbox}",
                "llama-swap must be attached to the agentbox-internal network (docker-compose.yml).")
