"""Git operations on the host-side clone of each project.

Safety rules applied to every git invocation here:
  * hooks are disabled (core.hooksPath=/dev/null) and fsmonitor is off, so nothing a
    model wrote can execute on the host through git;
  * the GitHub token is passed through GIT_CONFIG_* environment variables for one
    command at a time - it is never written to .git/config.
The sandbox never sees this .git directory at all (see workspace.py).
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from .util import CmdResult, run_cmd

SAFE_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


class Git:
    def __init__(self, repo_dir: Path, token: str = "", author_name: str = "agentbox-bot",
                 author_email: str = "agentbox-bot@users.noreply.github.com"):
        self.dir = Path(repo_dir)
        self.token = token
        self.author_name = author_name
        self.author_email = author_email

    def _env(self, auth: bool) -> dict[str, str]:
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
        }
        if auth and self.token:
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            })
        return env

    def run(self, *args: str, check: bool = True, auth: bool = False, timeout: float = 300, cwd: Optional[Path] = None) -> CmdResult:
        return run_cmd(["git", *SAFE_FLAGS, *args], cwd=cwd or self.dir, env=self._env(auth), timeout=timeout, check=check)

    # -------------------------------------------------------------- setup
    def init_new(self, remote_url: str, default_branch: str = "main") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run("init", "-q", "-b", default_branch)
        self.run("remote", "add", "origin", remote_url)

    def clone(self, remote_url: str) -> None:
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", *SAFE_FLAGS, "clone", "-q", remote_url, str(self.dir)], env=self._env(True), timeout=600, check=True)

    def exists(self) -> bool:
        return (self.dir / ".git").is_dir()

    # -------------------------------------------------------------- branches
    def fetch(self) -> None:
        self.run("fetch", "-q", "--prune", "origin", auth=True, timeout=600)

    def checkout(self, branch: str) -> None:
        self.run("checkout", "-q", branch)

    def checkout_new(self, branch: str, start_point: str) -> None:
        self.run("checkout", "-q", "-B", branch, start_point)

    def reset_hard(self, ref: str) -> None:
        self.run("reset", "-q", "--hard", ref)
        self.run("clean", "-qfdx")

    def sync_main(self, branch: str = "main") -> None:
        self.fetch()
        self.run("checkout", "-q", "-B", branch, f"origin/{branch}")
        self.reset_hard(f"origin/{branch}")

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").out.strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").out.strip()

    def show(self, ref: str, path: str) -> Optional[str]:
        """A file's content at `ref`, or None if it does not exist there."""
        r = self.run("show", f"{ref}:{path}", check=False)
        return r.out if r.ok else None

    def remote_head(self, branch: str) -> Optional[str]:
        """The commit the remote-tracking branch points at (as of the last fetch/push), or None."""
        r = self.run("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False)
        return r.out.strip() if r.ok else None

    def delete_local_branch(self, branch: str) -> None:
        self.run("branch", "-D", branch, check=False)

    # -------------------------------------------------------------- changes
    def add_all(self) -> None:
        self.run("add", "-A")

    def has_staged_changes(self) -> bool:
        return self.run("diff", "--cached", "--quiet", check=False).code == 1

    def commit(self, message: str) -> bool:
        """Stage everything and commit. Returns False when there was nothing to commit."""
        self.add_all()
        if not self.has_staged_changes():
            return False
        self.run("commit", "-q", "--no-verify", "-m", message)
        return True

    def push(self, branch: str, force: bool = False, set_upstream: bool = True) -> None:
        args = ["push", "-q"]
        if force:
            args.append("--force")
        if set_upstream:
            args.append("-u")
        args += ["origin", f"HEAD:refs/heads/{branch}"]
        self.run(*args, auth=True, timeout=600)

    def changed_files(self, base: str, head: str = "HEAD") -> list[tuple[str, str]]:
        """[(status, path)] between two commits (status: A, M, D, R...)."""
        out = self.run("diff", "--name-status", "--no-renames", base, head).out
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0][0], parts[-1]))
        return rows

    def worktree_changes(self) -> list[tuple[str, str]]:
        """Uncommitted changes vs HEAD, including untracked files: [(status, path)]."""
        self.add_all()
        out = self.run("diff", "--cached", "--name-status", "--no-renames").out
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0][0], parts[-1]))
        return rows

    def restore_path(self, path: str) -> None:
        """Undo uncommitted changes to one path (restore it from HEAD, or delete it if new)."""
        in_head = self.run("cat-file", "-e", f"HEAD:{path}", check=False).ok
        if in_head:
            self.run("checkout", "-q", "HEAD", "--", path)
        else:
            self.run("rm", "-q", "--cached", "--ignore-unmatch", "--", path, check=False)
            p = self.dir / path
            if p.is_file() or p.is_symlink():
                p.unlink()

    def diff(self, base: str, head: str = "HEAD") -> str:
        return self.run("diff", "--no-color", "--stat=200", "--patch", base, head, timeout=120).out

    def diff_line_count(self, base: str, head: str = "HEAD") -> int:
        total = 0
        for line in self.run("diff", "--numstat", base, head).out.splitlines():
            a, d, *_ = line.split("\t") + ["0", "0"]
            total += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        return total

    def ls_files(self) -> list[str]:
        """Tracked + untracked-but-not-ignored files."""
        out = self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard").out
        return sorted({p for p in out.split("\0") if p})
