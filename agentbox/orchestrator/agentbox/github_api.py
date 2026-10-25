"""Deterministic GitHub operations (REST API).

Design note: the *models* reach GitHub through the official GitHub MCP server, with a
read-only token, from inside the sandbox. Everything that changes the repository
(creating it, pushing, opening and merging PRs, labelling) is done here by plain code
with a separate token the models never see. That way a confused or prompt-injected
model cannot push to main, delete a branch or touch another repository.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Optional

from . import BOT_MARKER, http
from .util import log

LABELS = {
    "agent:todo": ("0e8a16", "Queued for the agent"),
    "agent:in-progress": ("fbca04", "The agent is working on this"),
    "agent:done": ("5319e7", "Completed and merged by the agent"),
    "agent:needs-human": ("d93f0b", "The agent gave up; needs a human decision"),
    "agent:blocked": ("bfdadc", "Waiting on a task that needs a human"),
    "agent:log": ("c5def5", "Run reports from the agent"),
}
AGENT_STATUS_LABELS = [k for k in LABELS if k != "agent:log"]


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not token:
            raise ValueError("GitHub token is empty - check your .env file")
        self.token = token
        self.api = api_url.rstrip("/")

    # ------------------------------------------------------------- plumbing
    def _h(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _req(self, method: str, path: str, body: Any = None, params: Optional[dict] = None, ok404: bool = False) -> Any:
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return http.request(method, url, headers=self._h(), json_body=body, timeout=60, retries=4).json()
        except http.HTTPError as e:
            if ok404 and e.status == 404:
                return None
            raise

    def _paged(self, path: str, params: Optional[dict] = None, limit: int = 1000) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            p = dict(params or {})
            p.update({"per_page": 100, "page": page})
            batch = self._req("GET", path, params=p) or []
            if isinstance(batch, dict):  # e.g. {"total_count":..., "workflow_runs": [...]}
                batch = next((v for v in batch.values() if isinstance(v, list)), [])
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    # ------------------------------------------------------------- identity
    def me(self) -> dict:
        return self._req("GET", "/user")

    def org(self, org: str) -> Optional[dict]:
        return self._req("GET", f"/orgs/{org}", ok404=True)

    # ------------------------------------------------------------- repos
    def get_repo(self, owner: str, name: str) -> Optional[dict]:
        return self._req("GET", f"/repos/{owner}/{name}", ok404=True)

    def create_repo(self, owner: str, owner_type: str, name: str, description: str, private: bool) -> dict:
        body = {
            "name": name,
            "description": description[:350],
            "private": private,
            "has_issues": True,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": True,
            "allow_rebase_merge": True,
        }
        path = f"/orgs/{owner}/repos" if owner_type == "org" else "/user/repos"
        repo = self._req("POST", path, body)
        log.info("created repository %s", repo.get("html_url"))
        return repo

    def ensure_labels(self, owner: str, repo: str) -> None:
        existing = {l["name"] for l in self._paged(f"/repos/{owner}/{repo}/labels")}
        for name, (color, desc) in LABELS.items():
            if name not in existing:
                self._req("POST", f"/repos/{owner}/{repo}/labels", {"name": name, "color": color, "description": desc})

    def protect_branch(self, owner: str, repo: str, branch: str = "main") -> bool:
        """Best effort: requires a public repo or a paid plan for private repos."""
        body = {
            "required_status_checks": None,
            "enforce_admins": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }
        try:
            self._req("PUT", f"/repos/{owner}/{repo}/branches/{branch}/protection", body)
            return True
        except http.HTTPError as e:
            log.info("branch protection not applied (%s) - fine on the free plan for private repos", e.status)
            return False

    # ------------------------------------------------------------- issues
    def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str]) -> int:
        issue = self._req("POST", f"/repos/{owner}/{repo}/issues", {"title": title[:250], "body": body, "labels": labels})
        return int(issue["number"])

    def get_issue(self, owner: str, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{owner}/{repo}/issues/{number}")

    def update_issue(self, owner: str, repo: str, number: int, **fields: Any) -> dict:
        return self._req("PATCH", f"/repos/{owner}/{repo}/issues/{number}", fields)

    def set_status_label(self, owner: str, repo: str, number: int, status_label: str) -> None:
        issue = self.get_issue(owner, repo, number)
        labels = [l["name"] for l in issue.get("labels", []) if l["name"] not in AGENT_STATUS_LABELS]
        labels.append(status_label)
        self.update_issue(owner, repo, number, labels=labels)

    def list_issues(self, owner: str, repo: str, state: str = "open", labels: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"state": state}
        if labels:
            params["labels"] = labels
        # The issues endpoint also returns PRs; drop them.
        return [i for i in self._paged(f"/repos/{owner}/{repo}/issues", params) if "pull_request" not in i]

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._req("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": f"{BOT_MARKER}\n{body}"[:65000]})

    def comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self._paged(f"/repos/{owner}/{repo}/issues/{number}/comments")

    def human_comments(self, owner: str, repo: str, number: int, since: Optional[str] = None) -> list[dict]:
        out = []
        for c in self.comments(owner, repo, number):
            if BOT_MARKER in (c.get("body") or ""):
                continue
            if (c.get("user") or {}).get("type") == "Bot":
                continue
            if since and c.get("created_at", "") <= since:
                continue
            out.append(c)
        return out

    # ------------------------------------------------------------- pull requests
    def find_open_pr(self, owner: str, repo: str, head_branch: str) -> Optional[dict]:
        prs = self._req("GET", f"/repos/{owner}/{repo}/pulls", params={"state": "open", "head": f"{owner}:{head_branch}"})
        return prs[0] if prs else None

    def create_pr(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> dict:
        return self._req("POST", f"/repos/{owner}/{repo}/pulls",
                         {"title": title[:250], "head": head, "base": base, "body": body, "maintainer_can_modify": True})

    def update_pr(self, owner: str, repo: str, number: int, **fields: Any) -> dict:
        return self._req("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", fields)

    def get_pr(self, owner: str, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def merge_pr(self, owner: str, repo: str, number: int, sha: str, method: str, title: str) -> dict:
        # Mergeability is computed asynchronously right after a push; poll briefly.
        for _ in range(10):
            pr = self.get_pr(owner, repo, number)
            if pr.get("mergeable") is not None:
                break
            time.sleep(3)
        return self._req("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                         {"sha": sha, "merge_method": method, "commit_title": title[:250]})

    # ------------------------------------------------------------- CI (Actions)
    def workflow_runs_for_sha(self, owner: str, repo: str, sha: str) -> list[dict]:
        data = self._req("GET", f"/repos/{owner}/{repo}/actions/runs", params={"head_sha": sha, "per_page": 50})
        return (data or {}).get("workflow_runs", [])

    def wait_for_ci(self, owner: str, repo: str, sha: str, timeout_s: int, poll_s: int = 20) -> tuple[str, str]:
        """Returns (status, detail) where status is success | failure | none | timeout."""
        deadline = time.time() + timeout_s
        seen_any = False
        while time.time() < deadline:
            runs = self.workflow_runs_for_sha(owner, repo, sha)
            if runs:
                seen_any = True
                pending = [r for r in runs if r.get("status") != "completed"]
                if not pending:
                    bad = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
                    if bad:
                        detail = "; ".join(f"{r.get('name')}: {r.get('conclusion')} ({r.get('html_url')})" for r in bad)
                        return "failure", detail
                    return "success", f"{len(runs)} workflow run(s) passed"
            elif time.time() > deadline - timeout_s + 120 and not seen_any:
                # No run has appeared two minutes after the push: no CI configured or no minutes left.
                return "none", "no workflow runs were triggered"
            time.sleep(poll_s)
        return "timeout", "CI did not finish in time"
