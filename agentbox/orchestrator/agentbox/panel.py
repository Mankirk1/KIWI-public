"""Phone control panel: `agentctl serve`.

A small web app (standard library only) that shows what the agent is doing and lets you
answer its questions, switch auto/manual mode, pause, resume and stop it from your phone.

Security model
  * Listens on 127.0.0.1 by default. Reach it from the phone through Tailscale
    (`tailscale serve` on Windows gives it an HTTPS address only your devices can open).
  * Sign-in: `agentctl pair` prints a 6-digit code (10 minutes, one use, 5 tries). The
    phone exchanges it for a random session cookie (HttpOnly, SameSite=Strict). Only
    SHA-256 hashes of codes and sessions are stored. `agentctl pair --logout-all` revokes.
  * Changes need a JSON POST with an `X-Agentbox: 1` header from the same origin (CSRF).
    Requests whose Host is not localhost, an IP address, *.ts.net or `control.public_url`
    are refused (DNS rebinding).
  * No tokens or .env values are ever read or shown; logs are redacted before display.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import Config
from .control import Control, pid_alive
from .digest import latest_digest
from .state import StateStore
from .util import atomic_write_json, log, now_iso

WEB = Path(__file__).parent / "web"
COOKIE = "agentbox_session"
PAIR_TTL_S = 600
PAIR_TRIES = 5
REDACT = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
                    r"|(?i:(?:token|password|secret|api[_-]?key)\s*[=:]\s*)\S{8,})")


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redact(text: str) -> str:
    return REDACT.sub("[redacted]", text)


class PanelAuth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.control_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ctl = Control(cfg)

    def _load(self, name: str) -> dict:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, name: str, data: dict) -> None:
        p = self.dir / name
        atomic_write_json(p, data)
        p.chmod(0o600)

    # ------------------------------------------------------------------ pairing
    def new_pair_code(self) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self.ctl.lock():
            self._save("pair.json", {"h": _h(code), "expires": time.time() + PAIR_TTL_S, "tries": 0})
        return code

    def redeem(self, code: str, agent: str = "") -> Optional[str]:
        code = re.sub(r"\D", "", code or "")
        with self.ctl.lock():
            p = self._load("pair.json")
            if not p or time.time() > p.get("expires", 0) or p.get("tries", 0) >= PAIR_TRIES:
                return None
            if not hmac.compare_digest(p.get("h", ""), _h(code)):
                p["tries"] = p.get("tries", 0) + 1
                self._save("pair.json", p)
                return None
            (self.dir / "pair.json").unlink(missing_ok=True)  # one use
            token = secrets.token_urlsafe(32)
            s = self._load("sessions.json")
            sessions = [x for x in s.get("sessions", []) if not self._expired(x)]
            sessions.append({"h": _h(token), "created": now_iso(), "t": time.time(), "agent": agent[:80]})
            self._save("sessions.json", {"sessions": sessions[-20:]})
        self.ctl.event("A phone was paired with the control panel", kind="control")
        return token

    def _expired(self, s: dict) -> bool:
        return time.time() - s.get("t", 0) > self.cfg.control.session_days * 86400

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        th = _h(token)
        for s in self._load("sessions.json").get("sessions", []):
            if hmac.compare_digest(s.get("h", ""), th) and not self._expired(s):
                return True
        return False

    def logout(self, token: str) -> None:
        with self.ctl.lock():
            s = self._load("sessions.json")
            self._save("sessions.json", {"sessions": [x for x in s.get("sessions", []) if x.get("h") != _h(token)]})

    def logout_all(self) -> None:
        with self.ctl.lock():
            self._save("sessions.json", {"sessions": []})
            (self.dir / "pair.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------- state for the UI
def build_state(cfg: Config, ctl: Control) -> dict:
    # Questions before status: ask() writes the "waiting" status *before* saving a question, so
    # reading in the opposite order never shows a question next to a stale phase.
    asked = [a.public() for a in ctl.approvals(status="pending")]
    status = ctl.status()
    alive = pid_alive(status.get("pid"))
    phase = status.get("phase", "idle") if alive else "idle"
    running = alive and phase != "idle"
    st = ctl.state()
    agent = {
        "running": running,
        "phase": phase,
        "detail": status.get("detail", "") if (running or status.get("phase") == "idle") else "",
        "project": status.get("project", ""),
        "task": status.get("task", ""),
        "since": status.get("since"),
        "updated": status.get("updated"),
        "command": status.get("command", ""),
        "lost": bool(status.get("pid")) and not alive and status.get("phase") not in (None, "idle"),
    }
    pending = asked if running else []
    recent = [a.public() for a in ctl.approvals(limit=30) if a.status != "pending"][:8]
    projects = []
    store = StateStore(cfg.projects_dir)
    for repo in store.all():
        try:
            p = store.load(repo)
        except Exception:
            continue
        counts = p.counts()
        last = p.runs[-1] if p.runs else None
        projects.append({
            "slug": p.slug, "url": f"https://github.com/{p.slug}", "total": len(p.tasks), "counts": counts,
            "tasks": [{"id": t.id, "title": t.title, "status": t.status, "issue": t.issue, "pr": t.pr,
                       "attempts": t.attempts_total} for t in p.tasks],
            "last_run": {"id": last.run_id, "finished": last.finished, "stop": last.stop_reason,
                         "done": len(last.done), "failed": len(last.failed)} if last else None,
        })
    c = cfg.control
    return {
        "now": now_iso(),
        "mode": st["mode"],
        "paused": st["paused"],
        "stop": ctl.stop_requested(),
        "gates": {"manual": c.manual_gates, "always": c.always_ask},
        "agent": agent,
        "pending": pending,
        "recent": recent,
        "projects": projects,
        "events": list(reversed(ctl.events(60))),
        "notify": bool(cfg.notify.ntfy_url),
        "budget_hours": cfg.budget.max_hours,
        "night": {"hours": c.night.hours, "active": ctl.is_night(), "timeout_min": c.night.question_timeout_min,
                  "quiet": c.night.quiet},
    }


def read_log(cfg: Config, ctl: Control, lines: int = 120) -> dict:
    status = ctl.status()
    data = cfg.paths.data.resolve()
    candidates = []
    if status.get("worker_log") and status.get("phase") == "coding":
        candidates.append(("Coder", Path(status["worker_log"])))
    candidates.append(("agentctl", cfg.logs_dir / f"agentctl-{time.strftime('%Y%m%d')}.log"))
    for name, path in candidates:
        try:
            rp = path.resolve()
            if not rp.is_relative_to(data) or not rp.is_file():
                continue
            with open(rp, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64_000))
                text = f.read().decode("utf-8", "replace")
            return {"source": name, "text": redact("\n".join(text.splitlines()[-lines:]))}
        except OSError:
            continue
    return {"source": "", "text": "No log yet."}


# ---------------------------------------------------------------------- HTTP
class PanelHandler(BaseHTTPRequestHandler):
    server_version = "agentbox-panel"
    cfg: Config
    ctl: Control
    auth: PanelAuth

    def log_message(self, fmt, *args):  # quiet; errors go to the agentbox log
        log.debug("panel %s - %s", self.address_string(), fmt % args)

    # -------------------------------------------------------------- helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0].strip("[]") if not host.endswith("]") else host.strip("[]")
        if name in ("localhost", "") or name.endswith(".ts.net"):
            return True
        pub = urlparse(self.cfg.control.public_url).hostname if self.cfg.control.public_url else None
        if pub and name == pub.lower():
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False

    def _cookie(self) -> Optional[str]:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _secure(self) -> bool:
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            return True
        pub = urlparse(self.cfg.control.public_url)
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return pub.scheme == "https" and pub.hostname is not None and host == pub.hostname.lower()

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: Optional[dict] = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _authed(self) -> bool:
        return self.auth.valid(self._cookie())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _csrf_ok(self) -> bool:
        if self.headers.get("X-Agentbox") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return False
        return True

    # -------------------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._send(421, b"Unknown host. Set control.public_url in config.yaml to the address you use.",
                              "text/plain; charset=utf-8")
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(16)
            html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{NONCE}}", nonce)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
                   "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return self._send(200, html.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if path == "/manifest.webmanifest":
            return self._send(200, (WEB / "manifest.webmanifest").read_bytes(), "application/manifest+json")
        if path == "/icon.svg":
            return self._send(200, (WEB / "icon.svg").read_bytes(), "image/svg+xml")
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return self._json(401, {"error": "sign in with a code from `agentctl pair`"})
        if path == "/api/state":
            return self._json(200, build_state(self.cfg, self.ctl))
        if path == "/api/log":
            return self._json(200, read_log(self.cfg, self.ctl))
        if path == "/api/digest":
            p = latest_digest(self.cfg)
            return self._json(200, {"name": p.stem if p else "",
                                    "text": redact(p.read_text(encoding="utf-8")) if p else ""})
        m = re.fullmatch(r"/api/approvals/([0-9A-Za-z-]{6,40})", path)
        if m:
            a = self.ctl.get(m.group(1))
            if not a:
                return self._json(404, {"error": "no such request"})
            data = a.public(with_diff=True)
            data["diff"] = redact(data["diff"])
            return self._json(200, data)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(421, {"error": "unknown host"})
        if not self._csrf_ok():
            return self._json(403, {"error": "missing X-Agentbox header or cross-origin request"})
        path = urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        if path == "/api/pair":
            token = self.auth.redeem(str(body.get("code", "")), self.headers.get("User-Agent", ""))
            if not token:
                time.sleep(1.0)
                return self._json(403, {"error": "Wrong or expired code. Run `agentctl pair` for a new one."})
            age = self.cfg.control.session_days * 86400
            cookie = f"{COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict" + ("; Secure" if self._secure() else "")
            return self._json(200, {"ok": True}, {"Set-Cookie": cookie})
        if not self._authed():
            return self._json(401, {"error": "sign in with a code from `agentctl pair`"})
        who = "phone"
        try:
            if path == "/api/logout":
                self.auth.logout(self._cookie() or "")
                return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            if path == "/api/mode":
                self.ctl.set_mode(str(body.get("mode")), by=who)
            elif path == "/api/pause":
                self.ctl.set_paused(bool(body.get("paused")), by=who)
            elif path == "/api/stop":
                self.ctl.set_stop(bool(body.get("stop")), by=who)
            else:
                m = re.fullmatch(r"/api/approvals/([0-9A-Za-z-]{6,40})", path)
                if not m:
                    return self._json(404, {"error": "not found"})
                self.ctl.decide(m.group(1), str(body.get("choice", "")), str(body.get("note", "")), by=who)
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        return self._json(200, build_state(self.cfg, self.ctl))


def make_server(cfg: Config, host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    handler = type("Handler", (PanelHandler,), {"cfg": cfg, "ctl": Control(cfg), "auth": PanelAuth(cfg)})
    srv = ThreadingHTTPServer((host or cfg.control.panel_host, cfg.control.panel_port if port is None else port), handler)
    srv.daemon_threads = True
    return srv


def serve(cfg: Config, host: Optional[str] = None, port: Optional[int] = None) -> int:
    try:
        srv = make_server(cfg, host, port)
    except OSError as e:
        print(f"agentctl serve: cannot listen on {host or cfg.control.panel_host}:{port or cfg.control.panel_port}: {e}")
        print("Is the panel already running? Check with: curl -s localhost:8765/healthz")
        return 2
    h, p = srv.server_address[:2]
    print(f"agentbox panel on http://{h}:{p}  (Ctrl+C to stop)")
    if cfg.control.public_url:
        print(f"phone address: {cfg.control.public_url}")
    print("Sign a phone in with:  agentctl pair")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
