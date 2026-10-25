#!/usr/bin/env python3
"""Replay one captured coder session against the model server under different context policies,
and measure what each policy costs the server for the *same* conversation.

    .venv/bin/python scripts/bench-context.py <bench-run-dir>/task/out/history.jsonl

The capture (history.jsonl, written by the coder when task.json has "capture": true; see
scripts/bench-coder.py) holds every message in full plus each request point. For each policy the
replay rebuilds the message list the coder would have sent at every request (applying that
policy's compaction and tool-output clipping), sends it to llama-swap like the coder does, and
records llama-server's timings: how many prompt tokens it actually had to evaluate (after its
prompt cache) and how fast it generated at that context depth. Generation is capped at --gen
tokens per request and extrapolated to the recorded completion length at the measured rate.
The conversation itself is held fixed, so the numbers isolate the cost of context management.
The server must not be busy with anything else.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from agentbox_worker import agent as worker  # noqa: E402  (current policy: COMPACT_AT / COMPACT_TO)
from agentbox_worker import prompts  # noqa: E402
from agentbox_worker.tools import clip  # noqa: E402

estimate_tokens = worker.estimate_tokens


def compact_v1(messages: list[dict], target_tokens: int, keep_tail: int = 10) -> bool:
    """The compaction before 2026-10-19, verbatim: it appends the context note to the task message."""
    changed = False
    head = 2
    for m in messages[head:-keep_tail]:
        if m["role"] == "tool" and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:250] + "\n[... old output elided to save context ...]"
            changed = True
        elif m["role"] == "assistant" and len(m.get("content") or "") > 600:
            m["content"] = m["content"][:300] + " [...]"
            changed = True
    if estimate_tokens(messages) <= target_tokens:
        return changed
    if prompts.CONTEXT_NOTE not in (messages[1].get("content") or ""):
        messages[1]["content"] = (messages[1].get("content") or "") + "\n\n" + prompts.CONTEXT_NOTE
    while estimate_tokens(messages) > target_tokens and len(messages) > head + keep_tail:
        i = head
        while i < len(messages) and messages[i]["role"] != "assistant":
            i += 1
        if i >= len(messages) - keep_tail:
            break
        j = i + 1
        while j < len(messages) and messages[j]["role"] == "tool":
            j += 1
        del messages[i:j]
        changed = True
    return changed


POLICIES = {
    "old": {"compact": compact_v1, "at": 0.70, "to": 0.50, "clip": 12000},                    # before 2026-10-19
    "deep": {"compact": worker.compact, "at": 0.50, "to": 0.25, "clip": 8000},                 # first try (rejected)
    "new": {"compact": worker.compact, "at": worker.COMPACT_AT, "to": worker.COMPACT_TO, "clip": worker.TOOL_OUTPUT_CHARS},
}


def load(path: Path) -> tuple[list[dict], list[dict], list[tuple[int, int]]]:
    """(tools, messages, requests) where requests = [(number of messages so far, completion tokens)]."""
    tools, msgs, reqs = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["kind"] == "tools":
            tools = rec["tools"]
        elif rec["kind"] == "message":
            msgs.append(rec["message"])
        elif rec["kind"] == "response":
            reqs.append((len(msgs), int(rec.get("completion_tokens") or 0)))
    return tools, msgs, reqs


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read())


def replay(name: str, tools: list[dict], history: list[dict], reqs: list[tuple[int, int]], url: str, model: str,
           ctx: int, gen: int) -> dict:
    pol = POLICIES[name]
    post(url, {"model": model, "messages": [{"role": "user", "content": f"cache reset {time.time()}"}],
               "max_tokens": 1, "temperature": 0})            # don't let one policy reuse the other's cache
    msgs: list[dict] = []
    used = 0
    last_prompt = 0
    rows = []
    for n_msgs, completion in reqs:
        for m in history[used:n_msgs]:
            m = copy.deepcopy(m)
            if m["role"] == "tool":
                m["content"] = clip(m.get("content") or "", pol["clip"])
            msgs.append(m)
        used = n_msgs
        compacted = False
        budget = int(ctx * pol["at"])
        if last_prompt > budget or estimate_tokens(msgs) > budget:
            compacted = pol["compact"](msgs, int(ctx * pol["to"]))
        t0 = time.time()
        out = post(url, {"model": model, "messages": msgs, "tools": tools, "max_tokens": gen, "temperature": 0,
                         "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}})
        tm, usage = out.get("timings") or {}, out.get("usage") or {}
        last_prompt = int(usage.get("prompt_tokens") or 0)
        rate = (tm.get("predicted_n") or 0) / ((tm.get("predicted_ms") or 1) / 1000)
        rows.append({"context": last_prompt, "evaluated": tm.get("prompt_n", 0), "prompt_ms": tm.get("prompt_ms", 0),
                     "gen_rate": round(rate, 2), "gen_ms_est": (completion / rate * 1000) if rate else 0,
                     "compacted": compacted, "wall_s": round(time.time() - t0, 1)})
        print(f"  [{name}] request {len(rows):2d}/{len(reqs)}: context {last_prompt:6d}, evaluated {tm.get('prompt_n', 0):6d} "
              f"in {tm.get('prompt_ms', 0) / 1000:6.1f}s, gen {rate:5.2f} tok/s{'  (compacted)' if compacted else ''}", flush=True)
    pm = sum(r["prompt_ms"] for r in rows) / 60000
    gm = sum(r["gen_ms_est"] for r in rows) / 60000
    return {"policy": name, "compact_at": pol["at"], "compact_to": pol["to"], "tool_clip_chars": pol["clip"],
            "requests": len(rows), "compactions": sum(r["compacted"] for r in rows),
            "prompt_tokens_sent": sum(r["context"] for r in rows),
            "prompt_tokens_evaluated": sum(r["evaluated"] for r in rows),
            "prompt_minutes": round(pm, 1), "generation_minutes_est": round(gm, 1), "model_minutes_est": round(pm + gm, 1),
            "mean_context": round(sum(r["context"] for r in rows) / max(len(rows), 1)),
            "max_context": max((r["context"] for r in rows), default=0),
            "mean_gen_rate": round(sum(r["gen_rate"] for r in rows) / max(len(rows), 1), 2), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("history", type=Path)
    ap.add_argument("--url", default="http://localhost:8080/v1/chat/completions")
    ap.add_argument("--model", default="qwen-coder")
    ap.add_argument("--context-tokens", type=int, default=32768)
    ap.add_argument("--gen", type=int, default=48, help="tokens generated per request (rest extrapolated)")
    ap.add_argument("--policies", default="old,new")
    args = ap.parse_args()
    tools, history, reqs = load(args.history)
    print(f"replaying {len(reqs)} requests, {len(history)} messages, policies {args.policies}", flush=True)
    results = [replay(p, tools, history, reqs, args.url, args.model, args.context_tokens, args.gen)
               for p in args.policies.split(",")]
    out = args.history.parent / "context-replay.json"
    out.write_text(json.dumps(results, indent=2))
    keys = ["compactions", "prompt_tokens_sent", "prompt_tokens_evaluated", "prompt_minutes", "generation_minutes_est",
            "model_minutes_est", "mean_context", "max_context", "mean_gen_rate"]
    print("\n| metric | " + " | ".join(r["policy"] for r in results) + " |\n|---|" + "---|" * len(results))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(r[k]) for r in results) + " |")
    print(f"\nsaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
