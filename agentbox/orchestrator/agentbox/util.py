"""Small helpers shared across the orchestrator. Standard library only."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("agentbox")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "task"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def truncate_middle(text: str, max_chars: int, marker: str = "\n... [{n} characters omitted] ...\n") -> str:
    """Keep the head and the tail of long text. Test output and diffs carry the useful
    information at both ends (the command and the final failures)."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 60)
    head = keep // 2
    tail = keep - head
    return text[:head] + marker.format(n=len(text) - head - tail) + text[-tail:]


def tail_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str | None) -> str:
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    # An unterminated <think> (model ran out of tokens while thinking) – drop it all.
    if "<think>" in text.lower():
        text = text[: text.lower().index("<think>")]
    return text.strip()


def extract_json(text: str) -> Any:
    """Return the first JSON object/array found in text (handles ```json fences and
    leading prose). Raises ValueError if nothing parses."""
    text = strip_think(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except Exception:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            while start != -1:
                depth, in_str, esc = 0, False, False
                for i in range(start, len(cand)):
                    ch = cand[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == opener:
                        depth += 1
                    elif ch == closer:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(cand[start : i + 1])
                            except Exception:
                                break
                start = cand.find(opener, start + 1)
    raise ValueError("no JSON object found in model output")


class CmdResult:
    def __init__(self, args: list[str], code: int, out: str, err: str, timed_out: bool = False):
        self.args, self.code, self.out, self.err, self.timed_out = args, code, out, err, timed_out

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CmdResult(code={self.code}, args={self.args[:3]}...)"


def run_cmd(
    args: list[str],
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    input_text: str | None = None,
) -> CmdResult:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        res = CmdResult(args, p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        res = CmdResult(args, -1, out, err, timed_out=True)
    except FileNotFoundError as e:
        res = CmdResult(args, 127, "", str(e))
    if check and not res.ok:
        raise RuntimeError(
            f"command failed ({res.code}): {' '.join(args[:6])}\n{truncate_middle(res.err or res.out, 3000)}"
        )
    return res


def setup_logging(log_file: Path | None, verbose: bool = False) -> None:
    root = logging.getLogger("agentbox")
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, optional quotes, # comments). Values already in
    the environment win, so you can override from the shell."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        loaded[key] = val
        os.environ.setdefault(key, val)
    return loaded


def chunked(items: Iterable[Any], n: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for it in items:
        buf.append(it)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf
