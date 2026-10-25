"""Minimal OpenAI-compatible chat client (stdlib only) for use inside the sandbox."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class LLMError(Exception):
    pass


class ChatClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.temperature = cfg.get("temperature", 0.6)
        self.top_p = cfg.get("top_p", 0.95)
        self.top_k = cfg.get("top_k")
        self.max_tokens = cfg.get("max_tokens", 8192)
        self.thinking = bool(cfg.get("thinking", False))
        self.extra_body = cfg.get("extra_body") or {}
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.timeout = int(cfg.get("request_timeout_s", 1500))
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None, retries: int = 4) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.thinking},
        }
        if self.top_k is not None:
            body["top_k"] = self.top_k
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(self.extra_body)
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # The model server is on the internal network; never send this through the egress proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last = ""
        for attempt in range(1, retries + 2):
            req = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers, method="POST")
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}"
                if e.code in (400, 401, 404) or attempt > retries:
                    raise LLMError(last) from None
            except Exception as e:  # connection reset while a model swaps in, timeouts...
                last = f"{type(e).__name__}: {e}"
                if attempt > retries:
                    raise LLMError(last) from None
            time.sleep(min(60, 5 * attempt))
        choice = (payload.get("choices") or [{}])[0]
        msg = dict(choice.get("message") or {})
        msg["_finish_reason"] = choice.get("finish_reason")
        usage = payload.get("usage") or {}
        msg["_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
        self.prompt_tokens += msg["_prompt_tokens"]
        msg["_completion_tokens"] = int(usage.get("completion_tokens") or 0)
        msg["_timings"] = payload.get("timings") or {}   # llama-server: prompt_n/ms (uncached part), predicted_n/ms
        self.completion_tokens += msg["_completion_tokens"]
        self.calls += 1
        return msg
