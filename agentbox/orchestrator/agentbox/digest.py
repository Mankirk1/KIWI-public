"""The digest: one summary per command (typically one night of `continue-all`), so a morning
check of everything the agents did, merged automatically or left for you, takes minutes.

Built from what the runs record (project state, task history, Sentinel's findings), saved as
Markdown in <data_dir>/reports/digests/, sent to the phone as one notification and shown in
the panel's Digest tab. `agentctl digest` prints the latest one.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .state import ProjectState, RunRecord, StateStore, TaskState
from .util import atomic_write_text


@dataclass
class Digest:
    text: str
    merged: list[str] = field(default_factory=list)      # one line each, for the notification
    handed: list[str] = field(default_factory=list)
    sentinel_high: int = 0

    @property
    def headline(self) -> str:
        return f"{len(self.merged)} merged, {len(self.handed)} left for you"


def _local(iso: str, fmt: str = "%H:%M") -> str:
    try:
        return _dt.datetime.fromisoformat(iso).astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return "?"


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"


def _brief(text: str, n: int = 200) -> str:
    from .loop import _brief as brief  # late import: loop imports this module
    return brief(text, n)


def _in(ts: str, start: str, end: str) -> bool:
    return start <= ts <= end


def _sentinel(cfg: Config, start: str, end: str) -> list[dict]:
    path = cfg.control_dir / "sentinel.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    t0 = _dt.datetime.fromisoformat(start).timestamp()
    t1 = _dt.datetime.fromisoformat(end).timestamp()
    out = []
    for line in lines:
        try:
            f = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t0 <= float(f.get("ts", 0)) <= t1:
            out.append(f)
    return out


def _last(task: TaskState, start: str, end: str, outcome: str) -> Optional[str]:
    for h in reversed(task.history):
        if h.outcome == outcome and _in(h.ts, start, end):
            return h.note
    return None


def _why(task: TaskState, start: str, end: str) -> str:
    """Why a task was left for you: the recorded reason, else the last thing that happened."""
    why = _last(task, start, end, "needs_human")
    if why:
        return why
    for h in reversed(task.history):
        if h.note.strip():
            return h.note
        if h.outcome.startswith(("owner_handoff_at_", "timeout_handoff_at_")):
            gate = h.outcome.rsplit("_at_", 1)[1]
            return ("you kept it for yourself" if h.outcome.startswith("owner") else "nobody answered") + f" at the {gate} gate"
    return ""


def build_digest(cfg: Config, since: str, until: str = "") -> Optional[Digest]:
    """Everything the runs that started at or after `since` (ISO, UTC) did. None if no run did."""
    store = StateStore(cfg.projects_dir)
    runs: list[tuple[ProjectState, RunRecord]] = []
    for repo in store.all():
        try:
            st = store.load(repo)
        except Exception:
            continue
        runs += [(st, r) for r in st.runs if r.started >= since and (not until or r.started <= until)]
    if not runs:
        return None
    runs.sort(key=lambda x: x[1].started)
    start = runs[0][1].started
    end = max((r.finished or r.started) for _, r in runs)

    d = Digest(text="")
    merged, handed, rows = [], [], []
    wall = waited = 0.0
    for st, run in runs:
        r_end = run.finished or end
        wall += (_dt.datetime.fromisoformat(r_end) - _dt.datetime.fromisoformat(run.started)).total_seconds() / 60
        waited += run.waited_minutes
        for tid in run.done + run.failed:
            t = st.task(tid)
            if t is None:
                continue
            mins = run.task_minutes.get(tid)
            m_txt = f"{mins:.0f}" if mins is not None else "–"
            if tid in run.done:
                note = _last(t, run.started, r_end, "merged") or (f"PR #{t.pr}" if t.pr else "")
                link = f"https://github.com/{st.slug}/pull/{t.pr}" if t.pr else f"https://github.com/{st.slug}"
                merged.append(f"- **{st.repo} {t.id}** {t.title} — [PR #{t.pr}]({link}) · "
                              + " · ".join([p for p in note.split(" · ")[1:] if p] + ([_hm(mins)] if mins is not None else [])))
                d.merged.append(f"✅ {st.repo} {t.id}: {t.title} (PR #{t.pr})")
                rows.append(f"| {st.repo} | {t.id} | merged | {m_txt} |")
            else:
                why = _why(t, run.started, r_end)
                where = (f"[PR #{t.pr}](https://github.com/{st.slug}/pull/{t.pr})" if t.pr else
                         f"[issue #{t.issue}](https://github.com/{st.slug}/issues/{t.issue})" if t.issue else "")
                handed.append(f"- **{st.repo} {t.id}** {t.title} — {_brief(why)}" + (f" · {where}" if where else ""))
                d.handed.append(f"🙋 {st.repo} {t.id}: {_brief(why, 90)}")
                rows.append(f"| {st.repo} | {t.id} | left for you | {m_txt} |")

    findings = _sentinel(cfg, start, end)
    loud = [f for f in findings if f.get("severity") in ("medium", "high")]
    d.sentinel_high = sum(1 for f in loud if f.get("severity") == "high")
    stops = "; ".join(sorted({f"{st.repo}: {r.stop_reason}" for st, r in runs if r.stop_reason}))
    day = _local(start, "%a %d %b")

    lines = [f"# agentbox digest: {day}, {_local(start)} → {_local(end)}", "",
             f"**{d.headline}** · {_hm(wall - waited)} of work"
             + (f" + {_hm(waited)} waiting for you" if waited >= 1 else "")
             + f" · Sentinel: {d.sentinel_high} high, {len(loud) - d.sentinel_high} medium", "",
             f"Stopped: {stops or 'n/a'}", ""]
    if merged:
        lines += ["## Merged: check these", ""] + merged + [""]
    if handed:
        lines += ["## Left for you", ""] + handed + [
            "", "To give one back: comment on the issue with guidance (or fix it), then label it `agent:todo`.", ""]
    if loud:
        lines += ["## Sentinel", ""]
        lines += [f"- {_dt.datetime.fromtimestamp(float(f['ts'])).strftime('%H:%M')} **{f['severity']}** "
                  f"{f['rule']} ({f.get('task', '')}): {f.get('summary', '')}" for f in loud] + [""]
    elif findings:
        lines += [f"Sentinel: nothing above info ({len(findings)} info finding(s)).", ""]
    if rows:
        lines += ["## Time per task", "", "| project | task | result | minutes |", "|---|---|---|---|"] + rows + [""]
    todo = {st.repo: sum(1 for t in st.tasks if t.status in ("todo", "blocked")) for st, _ in runs}
    inbox = cfg.paths.data / "inbox"
    queued = len(list(inbox.glob("*.md"))) if inbox.is_dir() else 0
    lines += ["## Next", ""] + [f"- {repo}: {n} task(s) still to do" for repo, n in sorted(todo.items())]
    lines += [f"- {queued} outline(s) queued for the next `continue-all`"] if queued else []
    if not any(todo.values()) and not queued:
        lines += ["- Nothing queued: the next night has no work until you queue an outline (`agentctl queue`)."]
    if merged:
        lines += ["", "To undo a merge: open the PR on GitHub and press **Revert**, then relabel its issue `agent:todo` "
                      "with a comment saying what was wrong."]
    d.text = "\n".join(lines).rstrip() + "\n"
    return d


def digests_dir(cfg: Config) -> Path:
    return cfg.reports_dir / "digests"


def save_digest(cfg: Config, d: Digest) -> Path:
    p = digests_dir(cfg) / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    atomic_write_text(p, d.text)
    return p


def latest_digest(cfg: Config) -> Optional[Path]:
    files = sorted(digests_dir(cfg).glob("*.md")) if digests_dir(cfg).is_dir() else []
    return files[-1] if files else None
