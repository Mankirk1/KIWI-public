"""Local tools the coding model can call. All paths are confined to the workspace."""
from __future__ import annotations

import fnmatch
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Callable

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "dist", "build", ".next", ".cache", "coverage"}
PROTECTED = [".github/*", ".github/**", "AGENTS.md", "SPEC.md", "OUTLINE.md", ".git/*", ".git/**"]
# Files that change how tests run; frozen while tests from the tests-first phase are locked.
RUNNER_CONFIG = ["conftest.py", "*/conftest.py", "pytest.ini", "*/pytest.ini", "tox.ini",
                 "sitecustomize.py", "*/sitecustomize.py", "usercustomize.py", "*/usercustomize.py", "*.pth", "*/*.pth"]
TEST_GLOBS = ["tests/*", "test/*", "*/tests/*", "*/test/*", "*/__tests__/*", "__tests__/*",
              "test_*.py", "*/test_*.py", "*_test.py", "*/*_test.py", "conftest.py", "*/conftest.py",
              "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx", "*.test.mjs",
              "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx", "*_test.go"]
MAX_OUTPUT = 8000


def is_test_path(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in TEST_GLOBS)


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-half:]


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMAS = [
    _schema("list_files", "List files in the repository (skips .git, node_modules, virtualenvs, caches).",
            {"path": {"type": "string", "description": "Directory relative to the repo root", "default": "."},
             "max_depth": {"type": "integer", "description": "How deep to recurse (default 3)", "default": 3}}, []),
    _schema("read_file", "Read a text file with line numbers. Use start_line/end_line for large files.",
            {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _schema("write_file", "Create a new file or completely replace an existing file with the given content.",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _schema("edit_file", "Replace an exact snippet in a file. old_text must match the file exactly (including "
            "indentation) and be unique unless replace_all is true. Read the file first.",
            {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
             "replace_all": {"type": "boolean", "default": False}}, ["path", "old_text", "new_text"]),
    _schema("run_command", "Run a bash command in the repo root (e.g. install dependencies, run tests, python -c). "
            "Non-interactive only. Returns exit code and output.",
            {"command": {"type": "string"}, "timeout_s": {"type": "integer", "description": "default 300, max 900"}},
            ["command"]),
    _schema("search", "Search file contents with a regular expression (ripgrep). Returns matching lines.",
            {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
             "glob": {"type": "string", "description": "optional file filter, e.g. *.py"}}, ["pattern"]),
    _schema("finish", "Call when the task is complete (or you cannot make progress). Give a concise summary of "
            "what you changed, decisions you made, and anything left undone.",
            {"summary": {"type": "string"}, "status": {"type": "string", "enum": ["done", "gave_up"], "default": "done"},
             "locked_test_problem": {"type": "string", "description": "only when a LOCKED test is wrong: file, test, "
                                     "what it expects and what SPEC.md/OUTLINE.md says instead"}},
            ["summary"]),
]


class ToolError(Exception):
    pass


class LocalTools:
    def __init__(self, root: Path, mode: str = "implement", locked: list[str] | None = None):
        self.root = root.resolve()
        self.mode = mode
        self.locked = set(locked or [])
        self.files_written: set[str] = set()
        self.commands: list[str] = []
        self.handlers: dict[str, Callable[[dict], str]] = {
            "list_files": self.list_files, "read_file": self.read_file, "write_file": self.write_file,
            "edit_file": self.edit_file, "run_command": self.run_command, "search": self.search,
        }

    # ---------------------------------------------------------------- helpers
    def _resolve(self, path: str) -> tuple[Path, str]:
        if not isinstance(path, str) or not path:
            raise ToolError("path is required")
        p = path.strip()
        if p.startswith("/workspace/"):
            p = p[len("/workspace/"):]
        elif p == "/workspace":
            p = "."
        full = (self.root / p).resolve()
        try:
            rel = full.relative_to(self.root)
        except ValueError:
            raise ToolError(f"path '{path}' is outside the repository")
        return full, str(rel).replace(os.sep, "/")

    def _check_writable(self, rel: str) -> None:
        if any(fnmatch.fnmatch(rel, g) for g in PROTECTED):
            raise ToolError(f"'{rel}' is maintained by the orchestrator and cannot be changed")
        if rel in self.locked:
            raise ToolError(f"'{rel}' is a locked test file written in the tests-first phase; make the "
                            "implementation satisfy it instead of changing it")
        if self.locked and self.mode != "tests" and any(fnmatch.fnmatch(rel, g) for g in RUNNER_CONFIG):
            raise ToolError(f"'{rel}' changes how the locked tests run and cannot be changed while they are locked; "
                            "if a locked test is wrong, call finish with status gave_up and locked_test_problem")
        if self.mode == "tests" and not is_test_path(rel):
            raise ToolError(f"in the tests-first phase you may only create or edit test files; '{rel}' is not "
                            "a test file (use tests/..., test_*.py, *.test.ts, ...)")

    # ---------------------------------------------------------------- tools
    def list_files(self, a: dict) -> str:
        full, rel = self._resolve(a.get("path") or ".")
        depth = max(1, min(int(a.get("max_depth") or 3), 8))
        if not full.is_dir():
            raise ToolError(f"'{rel}' is not a directory")
        out: list[str] = []
        base_depth = len(full.parts)
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            level = len(Path(dirpath).parts) - base_depth
            if level >= depth:
                dirnames[:] = []
            for f in sorted(filenames):
                out.append(str((Path(dirpath) / f).relative_to(self.root)).replace(os.sep, "/"))
            if len(out) > 500:
                out.append("... (truncated)")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, a: dict) -> str:
        full, rel = self._resolve(a.get("path", ""))
        if not full.is_file():
            raise ToolError(f"file '{rel}' does not exist")
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(a.get("start_line") or 1))
        end = int(a.get("end_line") or min(len(lines), start + 399))
        end = min(end, len(lines), start + 599)
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1)) if lines else "(empty file)"
        more = f"\n[showing lines {start}-{end} of {len(lines)}]" if (start > 1 or end < len(lines)) else ""
        return clip(body + more, 30000)

    def write_file(self, a: dict) -> str:
        full, rel = self._resolve(a.get("path", ""))
        self._check_writable(rel)
        content = a.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        full.parent.mkdir(parents=True, exist_ok=True)
        existed = full.exists()
        full.write_text(content, encoding="utf-8")
        self.files_written.add(rel)
        return f"{'overwrote' if existed else 'created'} {rel} ({len(content.splitlines())} lines)"

    def edit_file(self, a: dict) -> str:
        full, rel = self._resolve(a.get("path", ""))
        self._check_writable(rel)
        if not full.is_file():
            raise ToolError(f"file '{rel}' does not exist (use write_file to create it)")
        old, new = a.get("old_text"), a.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            raise ToolError("old_text and new_text must be strings and old_text must not be empty")
        text = full.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            stripped = old.strip()
            hint = " A whitespace-insensitive match exists - check indentation." if stripped and stripped in text else ""
            raise ToolError(f"old_text not found in {rel}.{hint} Re-read the file and copy the snippet exactly.")
        if n > 1 and not a.get("replace_all"):
            raise ToolError(f"old_text matches {n} places in {rel}; add surrounding lines to make it unique or set replace_all")
        text = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
        full.write_text(text, encoding="utf-8")
        self.files_written.add(rel)
        return f"edited {rel} ({n if a.get('replace_all') else 1} replacement(s))"

    def run_command(self, a: dict) -> str:
        cmd = a.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ToolError("command is required")
        timeout = max(5, min(int(a.get("timeout_s") or 300), 900))
        self.commands.append(cmd)
        proc = subprocess.Popen(["bash", "-lc", cmd], cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            code, timed_out = -9, True
        text = (out or b"").decode("utf-8", errors="replace")
        head = f"exit code: {code}" + (f" (killed after {timeout}s timeout)" if timed_out else "")
        return head + "\n" + (clip(text) if text.strip() else "(no output)")

    def search(self, a: dict) -> str:
        pattern = a.get("pattern")
        if not pattern:
            raise ToolError("pattern is required")
        full, _ = self._resolve(a.get("path") or ".")
        if shutil.which("rg"):
            args = ["rg", "-n", "--max-count", "30", "--max-columns", "300", "--no-heading"]
            for d in SKIP_DIRS:
                args += ["-g", f"!{d}"]
            if a.get("glob"):
                args += ["-g", a["glob"]]
            args += ["--", pattern, str(full)]
        else:
            args = ["grep", "-rnE", "--exclude-dir=.git", "--exclude-dir=node_modules", pattern, str(full)]
        p = subprocess.run(args, capture_output=True, text=True, timeout=60, errors="replace")
        out = (p.stdout or "").replace(str(self.root) + "/", "")
        if p.returncode == 1 and not out:
            return "no matches"
        if p.returncode not in (0, 1):
            return "search error: " + (p.stderr or "")[:500]
        return clip(out, 10000)

    def call(self, name: str, args: dict[str, Any]) -> str:
        fn = self.handlers.get(name)
        if fn is None:
            raise ToolError(f"unknown tool '{name}'")
        return fn(args)
