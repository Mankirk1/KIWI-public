"""OpenAI-compatible chat client for llama-swap / llama-server, with role presets,
structured (JSON-schema) output and usage accounting."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from . import http
from .config import LLMConfig, RoleConfig
from .util import extract_json, log, strip_think

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    by_role: dict = field(default_factory=dict)

    def add(self, role: str, usage: dict, seconds: float) -> None:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += p
        self.completion_tokens += c
        self.calls += 1
        self.seconds += seconds
        r = self.by_role.setdefault(role, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "seconds": 0.0})
        r["prompt_tokens"] += p
        r["completion_tokens"] += c
        r["calls"] += 1
        r["seconds"] += seconds


def build_request_body(role: RoleConfig, messages: list[dict], **extra: Any) -> dict:
    body: dict[str, Any] = {
        "model": role.model,
        "messages": messages,
        "temperature": role.temperature,
        "top_p": role.top_p,
        "max_tokens": role.max_tokens,
        "stream": False,
        # Qwen3.x / Gemma 4 templates read this to switch reasoning on or off.
        "chat_template_kwargs": {"enable_thinking": bool(role.thinking)},
    }
    if role.top_k is not None:
        body["top_k"] = role.top_k
    body.update(role.extra_body or {})
    body.update({k: v for k, v in extra.items() if v is not None})
    return body


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.usage = Usage()

    def role(self, name: str) -> RoleConfig:
        if name not in self.cfg.roles:
            raise LLMError(f"unknown LLM role '{name}' (configure llm.roles.{name})")
        return self.cfg.roles[name]

    def _headers(self) -> dict[str, str]:
        key = self.cfg.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def chat(self, role_name: str, messages: list[dict], **extra: Any) -> dict:
        """Returns the raw `choices[0].message` dict plus `_usage` and `_timings`."""
        import time

        role = self.role(role_name)
        body = build_request_body(role, messages, **extra)
        t0 = time.time()
        try:
            resp = http.request(
                "POST",
                self.cfg.base_url.rstrip("/") + "/chat/completions",
                headers=self._headers(),
                json_body=body,
                timeout=self.cfg.request_timeout_s,
                retries=self.cfg.retries,
                backoff=3.0,
            )
        except http.HTTPError as e:
            raise LLMError(f"{role_name} ({role.model}) request failed: {e}") from None
        dt = time.time() - t0
        data = resp.json() or {}
        if "choices" not in data or not data["choices"]:
            raise LLMError(f"{role_name}: malformed response: {str(data)[:300]}")
        msg = dict(data["choices"][0].get("message") or {})
        msg["_finish_reason"] = data["choices"][0].get("finish_reason")
        msg["_usage"] = data.get("usage") or {}
        msg["_timings"] = data.get("timings") or {}
        self.usage.add(role_name, msg["_usage"], dt)
        log.debug("%s: %s prompt / %s completion tokens in %.1fs", role_name,
                  msg["_usage"].get("prompt_tokens"), msg["_usage"].get("completion_tokens"), dt)
        return msg

    def text(self, role_name: str, system: str, user: str, **extra: Any) -> str:
        msg = self.chat(role_name, [{"role": "system", "content": system}, {"role": "user", "content": user}], **extra)
        content = strip_think(msg.get("content"))
        if not content:
            raise LLMError(f"{role_name}: empty response (finish_reason={msg.get('_finish_reason')})")
        return content

    def structured(
        self,
        role_name: str,
        system: str,
        user: str,
        model_cls: Type[T],
        max_repairs: int = 2,
        use_schema: bool = True,
        extra_check=None,
    ) -> T:
        """Ask for JSON matching `model_cls`. Uses llama-server's grammar-constrained
        json_schema mode when available, then validates with pydantic and asks the model to
        repair its output on failure. `extra_check(obj) -> list[str]` adds semantic checks."""
        schema = model_cls.model_json_schema()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user + "\n\nRespond with a single JSON object only."},
        ]
        rf = {"type": "json_schema", "json_schema": {"name": model_cls.__name__, "schema": schema, "strict": True}}
        last_err = ""
        for attempt in range(max_repairs + 1):
            try:
                msg = self.chat(role_name, messages, response_format=rf if use_schema else None)
            except LLMError as e:
                # Some templates/backends reject response_format; fall back to plain JSON prompting.
                if use_schema and any(s in str(e) for s in ("HTTP 400", "response_format", "grammar", "schema")):
                    log.warning("json_schema mode rejected (%s); falling back to prompt-only JSON", str(e)[:200])
                    use_schema = False
                    continue
                raise
            raw = msg.get("content") or ""
            try:
                obj = model_cls.model_validate(extract_json(raw))
                problems = extra_check(obj) if extra_check else []
                if not problems:
                    return obj
                last_err = "; ".join(problems)
            except (ValueError, ValidationError) as e:
                last_err = str(e)[:2000]
            log.warning("%s produced invalid JSON (attempt %d): %s", role_name, attempt + 1, last_err[:300])
            messages = messages + [
                {"role": "assistant", "content": strip_think(raw)[:12000]},
                {"role": "user", "content": f"That output is invalid: {last_err}\nReturn the corrected, complete JSON object only."},
            ]
        raise LLMError(f"{role_name} could not produce valid {model_cls.__name__}: {last_err}")

    # ------------------------------------------------------------------ health
    def list_models(self) -> list[str]:
        resp = http.request("GET", self.cfg.base_url.rstrip("/") + "/models", headers=self._headers(), timeout=15, retries=1)
        return [m.get("id") for m in (resp.json() or {}).get("data", [])]
