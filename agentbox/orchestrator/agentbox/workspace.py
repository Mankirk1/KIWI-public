"""Moving code between the host clone and the sandbox.

The sandbox gets a *copy* of the working tree without the real .git directory (a
throwaway git snapshot is created inside the copy so the model can still run
`git diff`). After the worker exits, changed files are copied back, skipping anything
ignored, oversized, or a symlink that escapes the tree. The real .git is never mounted
into a container, so nothing the model writes can later run on the host via git hooks
or config.
"""
from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable

import pathspec

from .gitops import SAFE_FLAGS
from .util import log, run_cmd

DEFAULT_IGNORES = [
    ".git/", "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".pytest_cache/",
    ".mypy_cache/", ".ruff_cache/", ".tox/", "dist/", "build/", "coverage/", ".coverage",
    ".next/", ".cache/", "*.egg-info/", ".DS_Store", ".env", ".env.*",
]
MAX_FILE_BYTES = 2_000_000

TEST_PATTERNS = pathspec.GitIgnoreSpec.from_lines([
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "**/test_*.py", "**/*_test.py", "**/conftest.py",
    "**/*.test.ts", "**/*.test.tsx", "**/*.test.js", "**/*.test.jsx", "**/*.test.mjs",
    "**/*.spec.ts", "**/*.spec.tsx", "**/*.spec.js", "**/*.spec.jsx",
    "**/*_test.go",
])


def is_test_path(path: str) -> bool:
    return TEST_PATTERNS.match_file(path)


def _ignore_spec(root: Path) -> pathspec.PathSpec:
    lines = list(DEFAULT_IGNORES)
    gi = root / ".gitignore"
    if gi.exists():
        lines += gi.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.GitIgnoreSpec.from_lines(lines)


def _walk_files(root: Path, spec: pathspec.PathSpec) -> set[str]:
    files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        # prune ignored directories early
        dirnames[:] = [d for d in dirnames if not spec.match_file(rel_dir + d + "/")]
        for fn in filenames:
            rel = rel_dir + fn
            if not spec.match_file(rel):
                files.add(rel)
        # symlinked directories show up in dirnames but are not followed; record them as files
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                files.add(rel_dir + d)
                dirnames.remove(d)
    return files


def export_tree(file_list: Iterable[str], repo_dir: Path, dest: Path) -> None:
    """Copy the listed files (relative to repo_dir) into a fresh dest directory."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel in file_list:
        src = repo_dir / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)


def init_snapshot_git(dest: Path) -> None:
    """Give the sandbox copy its own disposable git history (one commit) so the model can
    inspect its own changes with `git diff`/`git status`. Runs before the container
    starts, on a directory nothing untrusted has touched yet."""
    env = {"GIT_AUTHOR_NAME": "snapshot", "GIT_AUTHOR_EMAIL": "snapshot@local",
           "GIT_COMMITTER_NAME": "snapshot", "GIT_COMMITTER_EMAIL": "snapshot@local"}
    run_cmd(["git", *SAFE_FLAGS, "init", "-q", "-b", "main"], cwd=dest, env=env, check=True)
    run_cmd(["git", *SAFE_FLAGS, "add", "-A"], cwd=dest, env=env, check=True)
    run_cmd(["git", *SAFE_FLAGS, "commit", "-q", "--allow-empty", "--no-verify", "-m", "snapshot"], cwd=dest, env=env, check=True)


def _safe_symlink(link_path: Path, root: Path) -> bool:
    target = os.readlink(link_path)
    if os.path.isabs(target):
        return False
    resolved = (link_path.parent / target).resolve()
    try:
        resolved.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sync_back(workspace: Path, repo_dir: Path, repo_files: Iterable[str]) -> list[str]:
    """Make repo_dir's working tree match workspace (ignoring .git and ignored paths).
    Returns a list of human-readable notes about anything skipped."""
    notes: list[str] = []
    spec = _ignore_spec(workspace)
    ws_files = _walk_files(workspace, spec)
    repo_set = {f for f in repo_files if not spec.match_file(f)}

    for rel in sorted(repo_set - ws_files):
        p = repo_dir / rel
        if p.is_symlink() or p.is_file():
            p.unlink()
    for rel in sorted(ws_files):
        src = workspace / rel
        dst = repo_dir / rel
        if rel.startswith(".git/") or "/.git/" in rel:
            continue
        if src.is_symlink():
            if not _safe_symlink(src, workspace):
                notes.append(f"skipped symlink pointing outside the repo: {rel}")
                continue
            if dst.is_symlink() or dst.exists():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), dst)
            continue
        if not src.is_file():
            continue
        if src.stat().st_size > MAX_FILE_BYTES:
            notes.append(f"skipped file larger than {MAX_FILE_BYTES // 1000} KB: {rel}")
            continue
        if dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False):
            continue
        if dst.is_symlink():
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # remove directories left empty by deletions
    for dirpath, dirnames, filenames in os.walk(repo_dir, topdown=False):
        if ".git" in Path(dirpath).relative_to(repo_dir).parts:
            continue
        if Path(dirpath) != repo_dir and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    for n in notes:
        log.warning(n)
    return notes


def protected_violations(changes: list[tuple[str, str]], protected: list[str], locked: Iterable[str] = ()) -> list[str]:
    spec = pathspec.GitIgnoreSpec.from_lines(protected)
    locked_set = set(locked)
    return [path for _, path in changes if spec.match_file(path) or path in locked_set or path.startswith(".git/")]


# Files that change how tests run. While tests from the tests-first phase are locked, the
# implementation may not touch them: a conftest.py can monkeypatch, skip or inject names
# into locked tests (seen on the laptop: one put `pytest` into builtins).
RUNNER_CONFIG = pathspec.GitIgnoreSpec.from_lines([
    "**/conftest.py", "**/pytest.ini", "**/tox.ini", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth",
])


def _pytest_settings(path: str, text: str) -> object:
    """The pytest section of pyproject.toml / setup.cfg (None when absent or unparsable)."""
    if path.endswith("pyproject.toml"):
        # Compared as text (tomllib needs Python 3.11): the lines of [tool.pytest*] tables and
        # any top-level `tool.pytest...` dotted keys, without comments and blank lines.
        lines, inside = [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("["):
                inside = line.lstrip("[").startswith("tool.pytest")
            if line and (inside or line.startswith("tool.pytest")):
                lines.append(" ".join(line.split()))
        return lines or None
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        return "unparsable"
    return dict(cp["tool:pytest"]) if cp.has_section("tool:pytest") else None


def runner_config_violations(changes: list[tuple[str, str]], repo_dir: Path,
                             base_text: Callable[[str], str | None]) -> list[str]:
    """Changed paths that alter how tests run: conftest.py & co, or the pytest section of
    pyproject.toml / setup.cfg. `base_text(path)` returns a file's content before the change."""
    bad = []
    for _, path in changes:
        if RUNNER_CONFIG.match_file(path):
            bad.append(path)
        elif path.endswith(("pyproject.toml", "setup.cfg")):
            p = repo_dir / path
            new = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
            if _pytest_settings(path, base_text(path) or "") != _pytest_settings(path, new):
                bad.append(path)
    return bad


def non_test_changes(changes: list[tuple[str, str]]) -> list[str]:
    return [path for _, path in changes if not is_test_path(path)]
