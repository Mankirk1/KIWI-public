"""Reviewer: a second model (different family from the coder) reviews each PR diff."""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import prompts
from .llm import LLMClient
from .state import TaskState
from .util import log, truncate_middle

MAX_DIFF_CHARS = 60_000


class ReviewResult(BaseModel):
    analysis: str = ""
    approved: bool
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.approved and not self.blocking_issues

    def as_markdown(self) -> str:
        verdict = "✅ Approved" if self.passed else "❌ Changes requested"
        parts = [f"### Automated review: {verdict}", "", self.analysis.strip()]
        if self.blocking_issues:
            parts += ["", "**Blocking issues**"] + [f"- {i}" for i in self.blocking_issues]
        if self.suggestions:
            parts += ["", "**Suggestions (non-blocking)**"] + [f"- {s}" for s in self.suggestions]
        return "\n".join(parts)


MAX_OUTLINE_CHARS = 8_000


def review_change(llm: LLMClient, task: TaskState, diff: str, test_summary: str, agents_md: str,
                  owner_notes: str = "", outline: str = "") -> ReviewResult:
    """`owner_notes` (gate notes and issue comments) and `outline` (OUTLINE.md) override the
    issue's acceptance criteria where they conflict."""
    truncated = len(diff) > MAX_DIFF_CHARS
    user = prompts.REVIEW_USER.format(
        number=task.issue,
        title=task.title,
        body=task.description,
        criteria="\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none given)",
        owner_notes=(f"\nNotes from the project owner (they override the issue and its criteria):\n---\n"
                     f"{truncate_middle(owner_notes.strip(), 4000)}\n---\n" if owner_notes.strip() else ""),
        outline=(f"\nProject outline from the owner (authoritative for names, data and formulas):\n---\n"
                 f"{truncate_middle(outline.strip(), MAX_OUTLINE_CHARS)}\n---\n" if outline.strip() else ""),
        agents_md=truncate_middle(agents_md, 4000),
        tests=truncate_middle(test_summary, 3000),
        diff=truncate_middle(diff, MAX_DIFF_CHARS),
        truncated_note=" (TRUNCATED - focus on what is visible)" if truncated else "",
    )
    log.info("reviewer: reviewing %d chars of diff", len(diff))
    result = llm.structured("reviewer", prompts.REVIEW_SYSTEM, user, ReviewResult, max_repairs=2)
    log.info("reviewer verdict: %s (%d blocking)", "approve" if result.passed else "changes", len(result.blocking_issues))
    return result
