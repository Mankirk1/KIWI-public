"""A tiny JSON-over-HTTP client built on urllib, so the orchestrator has no network
library dependency. Handles retries with exponential backoff."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .util import log


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DEFAULT = urllib.request.build_opener()


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status, self.body, self.url = status, body, url

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception:
            return None


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    timeout: float = 60,
    retries: int = 3,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    backoff: float = 2.0,
) -> Response:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "agentbox/1.0"}
    if headers:
        hdrs.update(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
        host = urllib.parse.urlparse(url).hostname or ""
        # Never route local services (llama-swap) through a corporate HTTP proxy.
        opener = _DIRECT if host in ("localhost", "127.0.0.1", "::1") else _DEFAULT
        try:
            with opener.open(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            retry_after = e.headers.get("Retry-After") if e.headers else None
            # GitHub secondary rate limits come back as 403 with Retry-After.
            transient = e.code in retry_statuses or (e.code == 403 and retry_after)
            if transient and attempt <= retries:
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff ** attempt
                log.warning("HTTP %s from %s, retrying in %.0fs (%d/%d)", e.code, url, wait, attempt, retries)
                time.sleep(min(wait, 120))
                continue
            raise HTTPError(e.code, body, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt <= retries:
                wait = backoff ** attempt
                log.warning("network error to %s (%s), retrying in %.0fs (%d/%d)", url, e, wait, attempt, retries)
                time.sleep(wait)
                continue
            raise HTTPError(0, str(e), url) from None
