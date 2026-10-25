#!/usr/bin/env python3
"""Claude Code PreToolUse hook: ask before anything is published to GitHub.

The `ask` rules in .claude/settings.json catch the usual spellings (`git push ...`,
`gh pr merge ...`). This hook catches the forms a text rule cannot see, such as
`git -C repo push`, `git -c x=y push`, `bash -c "git push"`, `cd x && git push` and
`gh api .../merge`. It never allows anything by itself: it either asks or stays silent,
so the normal permission rules still apply.

With Remote Control on and "Push when actions required" enabled, the question appears
on your phone in the Claude app.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

# git options that take a separate value: git -C <dir> push, git -c <k=v> push, ...
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "xargs", "timeout", "nice", "stdbuf"}
SHELLS = {"sh", "bash", "zsh", "dash"}
SEPARATORS = re.compile(r"\|\||&&|[;|&\n()]|\$\(|`")


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands (quotes respected; `;`, `&&`, `|`, `(`,
    `$(`, backticks and newlines separate)."""
    command = command.replace("`", " ; ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:  # unbalanced quotes: fall back to a rough split
        return [p.split() for p in SEPARATORS.split(command) if p.strip()]
    out: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t and all(c in "();<>|&" for c in t):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t[:-1] if t.endswith("$") and len(t) > 1 else t)  # "x$(" -> "x"
    if cur:
        out.append(cur)
    return [[t for t in seg if t not in ("$", "")] for seg in out]


def _strip_wrappers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        t = tokens[i]
        base = t.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):  # FOO=bar git push
            i += 1
        elif base in WRAPPERS:
            i += 1
            # skip the wrapper's own options/values (timeout 10, env -i, nice -n 5)
            while i < len(tokens) and (tokens[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", tokens[i])):
                i += 1
        else:
            break
    return tokens[i:]


def reason(command: str, depth: int = 0) -> str | None:
    """Return why this command needs your approval, or None."""
    for tokens in _segments(command):
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        args = tokens[1:]
        if prog in SHELLS and depth < 3:
            for j, a in enumerate(args):
                if a in ("-c", "-lc", "-ic") and j + 1 < len(args):
                    inner = reason(args[j + 1], depth + 1)
                    if inner:
                        return inner
        # `git` anywhere in the command (covers sudo -u x git ..., xargs git ..., etc.)
        for k, tok in enumerate(tokens):
            if tok.rsplit("/", 1)[-1] != "git":
                continue
            rest, j = tokens[k + 1:], 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in GIT_OPTS_WITH_VALUE else 1
            if j < len(rest) and rest[j] == "push":
                return "git push publishes commits to GitHub"
        if prog == "gh" and args:
            if args[:2] == ["pr", "merge"]:
                return "gh pr merge merges a pull request"
            if args[:2] in (["repo", "create"], ["repo", "delete"], ["repo", "archive"]):
                return f"gh {' '.join(args[:2])} changes a repository"
            if args[0] == "api":
                joined = " ".join(args)
                if re.search(r"/merges?\b", joined) and re.search(r"(-X|--method)\s*(PUT|POST)", joined, re.I):
                    return "gh api call that merges on GitHub"
                if re.search(r"/git/refs\b", joined) and re.search(r"(-X|--method)\s*(POST|PATCH|DELETE)", joined, re.I):
                    return "gh api call that moves a branch on GitHub"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # not our business: let the normal permission flow decide
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    why = reason(command)
    if not why:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Needs your OK: {why}.",
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
