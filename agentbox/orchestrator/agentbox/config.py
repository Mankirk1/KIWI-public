"""Configuration: config.yaml (behaviour) + .env (secrets).

Every field has a default that works for the Dell XPS 15 9530 profile described in
GUIDE.md, so a minimal config.yaml only needs `github.owner`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from .util import load_dotenv


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


class GitHubConfig(BaseModel):
    owner: str = Field(..., description="Org (recommended) or username that will own the repos")
    owner_type: Literal["org", "user"] = "org"
    private: bool = True
    admin_token_env: str = "GH_ADMIN_TOKEN"
    agent_token_env: str = "GH_AGENT_TOKEN"
    git_author_name: str = "agentbox-bot"
    git_author_email: str = "agentbox-bot@users.noreply.github.com"
    api_url: str = "https://api.github.com"
    git_url: str = "https://github.com/{owner}/{repo}.git"

    def remote(self, owner: str, repo: str) -> str:
        return self.git_url.format(owner=owner, repo=repo)

    @property
    def admin_token(self) -> str:
        return os.environ.get(self.admin_token_env, "")

    @property
    def agent_token(self) -> str:
        return os.environ.get(self.agent_token_env, "")


class RoleConfig(BaseModel):
    model: str
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: Optional[int] = 20
    max_tokens: int = 8192
    thinking: bool = False
    context_tokens: int = 49152
    extra_body: dict = Field(default_factory=dict)


def _checker(planner: RoleConfig) -> RoleConfig:
    # Writes the outline checks, one task at a time: transcription, not design, so the planner's
    # model without its (slow) reasoning, kept short and literal.
    return planner.model_copy(update={"thinking": False, "temperature": 0.2, "max_tokens": 6144})


def _default_roles() -> dict[str, RoleConfig]:
    planner = RoleConfig(model="qwen-coder", temperature=0.6, max_tokens=16384, thinking=True)
    return {
        "planner": planner,
        "checker": _checker(planner),
        "coder": RoleConfig(model="qwen-coder", temperature=0.6, max_tokens=8192, thinking=True),
        "reviewer": RoleConfig(model="gemma-reviewer", temperature=0.2, top_p=0.95, top_k=64,
                               max_tokens=4096, thinking=False),
    }


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:8080/v1"
    api_key_env: str = "LLAMA_SWAP_API_KEY"
    request_timeout_s: int = 1800
    retries: int = 4
    roles: dict[str, RoleConfig] = Field(default_factory=_default_roles)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @field_validator("roles")
    @classmethod
    def _need_roles(cls, v: dict[str, RoleConfig]) -> dict[str, RoleConfig]:
        merged = _default_roles()
        merged.update(v)
        if "checker" not in v:
            merged["checker"] = _checker(merged["planner"])
        return merged


class SandboxConfig(BaseModel):
    worker_image: str = "agentbox-worker:latest"
    network: str = "agentbox-internal"
    llm_url_in_sandbox: str = "http://llama-swap:8080/v1"
    proxy_url: str = "http://egress-proxy:3128"
    cpus: float = 4
    memory: str = "4g"
    pids_limit: int = 1024
    worker_timeout_min: int = 50
    test_timeout_min: int = 15
    max_agent_steps: int = 60
    mcp_toolsets: str = "repos,issues,pull_requests"
    # Read-only GitHub tools for the coder. Their definitions are ~4,600 prompt tokens (minutes
    # on a CPU) per cold start, and coders rarely need them: "auto" offers them only when the
    # issue, its feedback or an owner note mentions another issue or PR (#12, /pull/12).
    mcp: Literal["auto", "always", "off"] = "auto"


class CIConfig(BaseModel):
    mode: Literal["require", "advisory", "off"] = "advisory"
    wait_timeout_min: int = 20
    # When a merge would happen without you (auto mode, merge not in always_ask), CI that never
    # ran or did not finish counts as a failure even in advisory mode: no untested merges.
    require_for_auto_merge: bool = True


class ChecksConfig(BaseModel):
    # Outline checks (agentbox/oracle.py): written from the outline at plan time, run after the
    # tests pass; a failing check of the task (or a finished one) sends the attempt back.
    enabled: bool = True
    # Weak-test check: plant small errors in the task's Python code and rerun the tests; the
    # survivors are reported at the gates, on the PR and in the digest (never blocking).
    mutation: bool = True
    mutation_max: int = 20
    mutation_budget_s: int = 180


class WorkflowConfig(BaseModel):
    tdd: bool = True
    max_attempts_per_task: int = 3
    max_tasks_per_project: int = 25
    max_diff_lines: int = 1500
    plan_review: bool = True
    protected_paths: list[str] = Field(default_factory=lambda: [".github/**", "AGENTS.md", "SPEC.md", "OUTLINE.md"])
    ci: CIConfig = Field(default_factory=CIConfig)
    checks: ChecksConfig = Field(default_factory=ChecksConfig)
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    allowed_stacks: list[str] = Field(default_factory=lambda: ["python", "node"])


class BudgetConfig(BaseModel):
    max_hours: float = 8.0
    max_consecutive_failures: int = 4
    # Hard wall-clock limit for one command, waiting included (0 = none). Set it below the
    # nightly task's limit (Register-NightlyTask.ps1 -MaxHours, 9 h): no attempt starts unless it
    # can finish before it, and a wait for your answer ends there, so Windows never kills a run.
    max_wall_hours: float = 0
    stop_file: str = "~/agentbox-runtime/STOP"

    @property
    def stop_path(self) -> Path:
        return _expand(self.stop_file)


class NotifyConfig(BaseModel):
    ntfy_url: str = ""  # e.g. https://ntfy.sh/<long-random-topic>


class SentinelConfig(BaseModel):
    # Rule-based watchdog during runs (agentbox/sentinel.py): findings go to the activity feed;
    # those at or above `notify` also go to the phone (ntfy). Uses no model and changes nothing.
    enabled: bool = True
    interval_s: float = 30
    notify: Literal["high", "medium", "off"] = "high"


class GuardsConfig(BaseModel):
    # Other local LLM servers (Ollama) compete for the same RAM and CPU. Before a run and
    # before every task the orchestrator looks for Ollama on Windows, in WSL and in Docker.
    #   stop  - stop it (Windows app/process via taskkill.exe, Docker container), continue
    #   abort - refuse to run while it is up
    #   warn  - log a warning only
    #   off   - do not check
    ollama: Literal["stop", "abort", "warn", "off"] = "stop"
    restore_ollama: bool = True  # restart what was stopped when the run ends


Gate = Literal["plan", "task", "tests", "push", "merge"]
ALL_GATES: list[str] = ["plan", "task", "tests", "push", "merge"]
_HOURS = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")


class NightConfig(BaseModel):
    # Unattended hours, local time, e.g. "23:00-07:30" (empty = no night rules). During them:
    #  - a question nobody answers within `question_timeout_min` is left for you (the PR stays
    #    open, or the task is skipped) and the run goes on with work that doesn't depend on it;
    #  - notifications arrive silently (low priority), except Sentinel's high alerts when
    #    `wake_for_sentinel_high` is on.
    hours: str = ""
    question_timeout_min: float = 30
    quiet: bool = True
    wake_for_sentinel_high: bool = True

    @field_validator("hours")
    @classmethod
    def _hours(cls, v: str) -> str:
        v = v.replace(" ", "")
        if v and not _HOURS.match(v):
            raise ValueError('use "HH:MM-HH:MM", e.g. "23:00-07:30"')
        return v

    def active(self, hhmm: tuple[int, int]) -> bool:
        if not self.hours:
            return False
        a, b = (tuple(int(x) for x in part.split(":")) for part in self.hours.split("-"))
        return a <= hhmm < b if a < b else (hhmm >= a or hhmm < b)


class ControlConfig(BaseModel):
    # How much the agent asks you before acting. You can switch the mode from the phone
    # panel at any time (it is then remembered in <data_dir>/control/state.json).
    #   auto   - runs on its own; only the gates in `always_ask` wait for you
    #   manual - waits for your OK at every gate in `manual_gates`
    # Gates: plan  - before the repository and issues are created from the plan
    #        task  - before the coder starts on an issue (approve with a note to steer it)
    #        tests - before the tests written from the acceptance criteria are locked
    #        push  - before anything is pushed to GitHub / a pull request is opened
    #        merge - before a reviewed, tested pull request is merged
    mode: Literal["auto", "manual"] = "auto"
    manual_gates: list[Gate] = Field(default_factory=lambda: list(ALL_GATES))
    always_ask: list[Gate] = Field(default_factory=list)
    approval_timeout_min: float = 0          # 0 = wait as long as it takes (Stop still works)
    on_timeout: Literal["stop", "approve", "skip"] = "stop"
    night: NightConfig = Field(default_factory=NightConfig)
    # Phone panel (`agentctl serve`). Keep it on 127.0.0.1 and reach it through Tailscale.
    panel_host: str = "127.0.0.1"
    panel_port: int = 8765
    public_url: str = ""                     # e.g. https://my-laptop.tail1234.ts.net (links in notifications)
    session_days: int = 90                   # how long a paired phone stays signed in


class PathsConfig(BaseModel):
    # Runtime state lives OUTSIDE the git checkout so `git clean` can never delete it.
    data_dir: str = "~/agentbox-runtime"

    @property
    def data(self) -> Path:
        return _expand(self.data_dir)


class Config(BaseModel):
    github: GitHubConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Derived locations -------------------------------------------------
    @property
    def control_dir(self) -> Path:
        return self.paths.data / "control"

    @property
    def projects_dir(self) -> Path:
        return self.paths.data / "projects"

    @property
    def repos_dir(self) -> Path:
        return self.paths.data / "repos"

    @property
    def work_dir(self) -> Path:
        return self.paths.data / "work"

    @property
    def logs_dir(self) -> Path:
        return self.paths.data / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.paths.data / "reports"


def default_home() -> Path:
    if os.environ.get("AGENTBOX_HOME"):
        return _expand(os.environ["AGENTBOX_HOME"])
    # Default: the agentbox folder this package was installed from (KIWI/agentbox).
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> Config:
    home = default_home()
    load_dotenv(home / ".env")
    path = path or Path(os.environ.get("AGENTBOX_CONFIG", home / "config.yaml"))
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path}. Copy config.example.yaml to config.yaml and set github.owner."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    for d in (cfg.projects_dir, cfg.repos_dir, cfg.work_dir, cfg.logs_dir, cfg.reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
