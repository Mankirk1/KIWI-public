"""Run reports (Markdown) and push notifications."""
from __future__ import annotations

import urllib.request
from pathlib import Path

from .state import ProjectState, RunRecord
from .util import atomic_write_text, log

ICON = {"done": "✅", "needs_human": "🙋", "blocked": "⛔", "todo": "⏳", "in_progress": "🔧", "skipped": "➖"}


def build_report(st: ProjectState, run: RunRecord) -> str:
    c = st.counts()
    lines = [
        f"## Agent run `{run.run_id}` — {st.slug}",
        "",
        f"- Started: {run.started}  ·  Finished: {run.finished or 'still running'}",
        f"- Command: `{run.command}`",
        f"- Stop reason: **{run.stop_reason or 'n/a'}**",
        f"- This run: {len(run.done)} merged, {len(run.failed)} handed to a human",
        f"- Overall: " + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())),
    ]
    u = run.usage or {}
    if u:
        lines.append(f"- Model usage: {u.get('calls', 0)} calls, {u.get('prompt_tokens', 0):,} prompt + "
                     f"{u.get('completion_tokens', 0):,} completion tokens, {u.get('seconds', 0) / 60:.0f} min of generation")
    lines += ["", "| | Task | Issue | PR | Attempts |", "|---|---|---|---|---|"]
    for t in st.tasks:
        issue = f"#{t.issue}" if t.issue else ""
        pr = f"#{t.pr}" if t.pr else ""
        lines.append(f"| {ICON.get(t.status, '')} {t.status} | {t.id}: {t.title} | {issue} | {pr} | {t.attempts_total} |")
    needs = [t for t in st.tasks if t.status == "needs_human"]
    if needs:
        lines += ["", "### Needs your attention"]
        for t in needs:
            from .loop import _brief
            last = _brief(t.history[-1].note, 240) if t.history else ""
            lines.append(f"- **#{t.issue} {t.title}** — last problem: {last}")
        lines += ["", "To hand a task back to the agent: comment on the issue with guidance (or fix it yourself),",
                  "then replace the `agent:needs-human` label with `agent:todo`. The next run picks it up."]
    return "\n".join(lines) + "\n"


def save_report(reports_dir: Path, st: ProjectState, run: RunRecord, text: str) -> Path:
    p = reports_dir / st.repo / f"{run.run_id}.md"
    atomic_write_text(p, text)
    return p


def _ascii(s: str, n: int = 500) -> str:
    return s.encode("ascii", "ignore").decode()[:n]


def notify(ntfy_url: str, title: str, message: str, click: str = "", priority: str = "",
           tags: str = "robot", actions: str = "") -> None:
    """Send a push notification through ntfy (https://ntfy.sh). `click` opens a URL when
    the notification is tapped; `actions` adds buttons (ntfy header syntax)."""
    if not ntfy_url:
        return
    headers = {"Title": _ascii(title, 200), "Tags": _ascii(tags, 100)}
    if click:
        headers["Click"] = _ascii(click)
    if priority:
        headers["Priority"] = _ascii(priority, 20)
    if actions:
        headers["Actions"] = _ascii(actions)
    try:
        req = urllib.request.Request(ntfy_url, data=message.encode("utf-8")[:3500], method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # notifications are best effort
        log.warning("notification failed: %s", e)
