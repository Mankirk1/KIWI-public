"""The tool-calling agent loop.

Guardrails in the loop itself (on top of the container sandbox):
  * step limit and wall-clock budget;
  * repeated-identical-call detection (nudge at 3, stop at 6);
  * tool outputs clipped, old outputs elided when the context fills up;
  * text-only replies are nudged back to tool use; textual tool calls are parsed as a fallback;
  * `finish` is refused once if files changed since the last test run.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import prompts
from .llm import ChatClient, LLMError
from .mcp_bridge import McpBridge
from .tools import TOOL_SCHEMAS, LocalTools, ToolError, clip

_TOOL_CALL_XML = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FUNC_XML = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.DOTALL)
_PARAM_XML = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_text_tool_calls(content: str) -> list[dict]:
    """Fallback for models/templates that emit tool calls as text instead of structured
    tool_calls. Supports the Hermes/Qwen JSON form and the Qwen3-Coder XML form."""
    calls: list[dict] = []
    if not content:
        return calls
    for m in _TOOL_CALL_XML.finditer(content):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name")
            args = obj.get("arguments", obj.get("parameters", {}))
            if name:
                calls.append({"id": "call_" + uuid.uuid4().hex[:10], "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}})
        except Exception:
            continue
    if calls:
        return calls
    for m in _FUNC_XML.finditer(content):
        args = {p.group(1): p.group(2) for p in _PARAM_XML.finditer(m.group(2))}
        calls.append({"id": "call_" + uuid.uuid4().hex[:10], "type": "function",
                      "function": {"name": m.group(1), "arguments": json.dumps(args)}})
    return calls


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total += len(tc.get("function", {}).get("arguments") or "") + 40
    return int(total / 3.2) + 8 * len(messages)


# Context policy. Compaction rewrites history, and llama-server's prompt cache is lost from the
# first changed message on, so the whole rest is read again (at ~35 tok/s on this CPU). So:
# compact rarely but deeply, and never touch the system prompt and task message (messages 0-1),
# whose ~3-4k tokens then stay cached. Measured with scripts/bench-context.py (field notes).
# A first try (0.50 -> 0.25, tool outputs clipped at 8,000 chars) was 21% cheaper per conversation
# in the replay, but end to end the coder then re-read files after each compaction (30 reads vs 12)
# and took 52 steps instead of 19. Compaction must not take away the working set.
COMPACT_AT = 0.60   # fraction of the context window that triggers a compaction (was 0.70)
COMPACT_TO = 0.40   # ... and the size it compacts down to (was 0.50)
TOOL_OUTPUT_CHARS = 12000   # a tool result kept in the conversation (8,000 made it read big files in pieces)


def compact(messages: list[dict], target_tokens: int, keep_tail: int = 10) -> bool:
    """Shrink the conversation in place. Returns True if anything changed.
    Stage 1 elides old tool outputs; stage 2 drops whole old assistant/tool turns.
    Messages 0-1 (system, task) are never modified, so their prompt cache survives."""
    changed = False
    head = 2  # system + task
    for m in messages[head:-keep_tail]:
        if m["role"] == "tool" and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:250] + "\n[... old output elided to save context ...]"
            changed = True
        elif m["role"] == "assistant" and len(m.get("content") or "") > 600:
            m["content"] = m["content"][:300] + " [...]"
            changed = True
    if estimate_tokens(messages) <= target_tokens:
        return changed
    # Stage 2: remove the oldest assistant turn and its tool results, repeatedly. Two passes:
    # the context note added after the first may push the size back over the target.
    for _ in range(2):
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
        if changed and not any(prompts.CONTEXT_NOTE in (m.get("content") or "") for m in messages[head:]):
            # Prefixed to the first remaining tool result, not the task message (which must stay
            # byte-identical for the cache) nor a new message (roles must keep alternating).
            for m in messages[head:]:
                if m["role"] == "tool":
                    m["content"] = prompts.CONTEXT_NOTE + "\n" + (m.get("content") or "")
                    break
        if estimate_tokens(messages) <= target_tokens:
            break
    return changed


class _History(list):
    """The message list, also written in full to history.jsonl when capture is on (benchmarks:
    scripts/bench-context.py replays a captured session under different context policies)."""

    def __init__(self, items: list[dict], sink=None):
        super().__init__(items)
        self.sink = sink
        for m in items:
            self._write(m)

    def _write(self, m: dict) -> None:
        if self.sink is not None:
            self.sink.write(json.dumps({"kind": "message", "message": m}, ensure_ascii=False) + "\n")
            self.sink.flush()

    def append(self, m: dict) -> None:  # type: ignore[override]
        super().append(m)
        self._write(m)


class Agent:
    def __init__(self, task: dict, workspace: Path, out_dir: Path):
        self.task = task
        self.ws = workspace
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.llm = ChatClient(task["llm"])
        self.ctx_tokens = int(task["llm"].get("context_tokens") or 32768)
        self.max_steps = int(task.get("max_steps") or 60)
        self.deadline = time.time() + int(task.get("time_budget_s") or 2700)
        self.mode = task.get("mode", "implement")
        self.tools = LocalTools(workspace, self.mode, task.get("locked_tests") or [])
        self.transcript = open(self.out / "transcript.jsonl", "a", encoding="utf-8")
        self.test_cmd = (task.get("stack") or {}).get("test_command", "")

    def log(self, kind: str, **data: Any) -> None:
        rec = {"t": round(time.time(), 1), "kind": kind, **data}
        self.transcript.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
        self.transcript.flush()
        short = {k: (str(v)[:200]) for k, v in data.items()}
        print(f"[worker] {kind}: {short}", flush=True)

    # ---------------------------------------------------------------- prompts
    def build_messages(self, mcp_on: bool) -> list[dict]:
        t = self.task
        stack = t.get("stack") or {}
        mcp_note = prompts.MCP_NOTE.format(repo=t.get("repo", "")) if mcp_on else ""
        if self.mode == "tests":
            mode_text = prompts.MODE_TESTS
        elif t.get("kind") == "scaffold":
            mode_text = prompts.MODE_SCAFFOLD
        else:
            mode_text = prompts.MODE_IMPLEMENT
        if t.get("locked_tests") and self.mode != "tests":
            mode_text += "\n" + prompts.MODE_LOCKED.format(files=", ".join(t["locked_tests"]))
        hints = t.get("files_hint") or []
        user = prompts.TASK_USER.format(
            repo=t.get("repo", ""), issue=t.get("issue", "?"), task_id=t.get("task_id", ""), title=t.get("title", ""),
            description=t.get("description", ""),
            criteria="\n".join(f"- {c}" for c in t.get("acceptance_criteria") or []),
            files_hint=("Likely files: " + ", ".join(hints) + "\n") if hints else "",
            setup=stack.get("setup_command") or "(none)", test=stack.get("test_command") or "(none)",
            lint=(f"- lint:  {stack['lint_command']}" if stack.get("lint_command") else ""),
            mode_text=mode_text,
            feedback=("\n" + t["feedback"].strip() + "\n") if t.get("feedback") else "",
        )
        return [{"role": "system", "content": prompts.CODER_SYSTEM.format(mcp_note=mcp_note)},
                {"role": "user", "content": user}]

    # ---------------------------------------------------------------- loop
    async def run(self) -> dict:
        status, summary, locked_problem = "error", "", ""
        steps = 0
        mcp_cfg = self.task.get("mcp") or {}
        async with McpBridge(bool(mcp_cfg.get("enabled")), mcp_cfg.get("toolsets", "repos,issues,pull_requests")) as mcp:
            self.log("start", mode=self.mode, mcp_tools=len(mcp.tools), mcp_error=mcp.error)
            tool_schemas = TOOL_SCHEMAS + mcp.tools
            sink = open(self.out / "history.jsonl", "w", encoding="utf-8") if self.task.get("capture") else None
            messages = _History(self.build_messages(bool(mcp.tools)), sink)
            if sink is not None:
                sink.write(json.dumps({"kind": "tools", "tools": tool_schemas}) + "\n")
            recent: list[str] = []
            no_tool_streak = 0
            edited_since_test = False
            finish_refused = False
            last_prompt_tokens = 0
            try:
                while steps < self.max_steps:
                    if time.time() > self.deadline:
                        status, summary = "timeout", "time budget exhausted"
                        break
                    steps += 1
                    budget = int(self.ctx_tokens * COMPACT_AT)
                    if last_prompt_tokens > budget or estimate_tokens(messages) > budget:
                        if compact(messages, int(self.ctx_tokens * COMPACT_TO)):
                            self.log("compact", est_tokens=estimate_tokens(messages))
                    try:
                        msg = await asyncio.to_thread(self.llm.chat, messages, tool_schemas)
                    except LLMError as e:
                        if "context" in str(e).lower() and compact(messages, int(self.ctx_tokens * 0.20), keep_tail=6):
                            self.log("compact_after_overflow", error=str(e)[:200])
                            continue
                        status, summary = "error", f"model request failed: {e}"
                        break
                    last_prompt_tokens = msg.get("_prompt_tokens", 0)
                    if sink is not None:
                        sink.write(json.dumps({"kind": "response", "step": steps, "sent_messages": len(messages),
                                               "prompt_tokens": last_prompt_tokens,
                                               "completion_tokens": msg.get("_completion_tokens", 0),
                                               "timings": msg.get("_timings", {})}) + "\n")
                    content = _THINK.sub("", msg.get("content") or "").strip()
                    tool_calls = msg.get("tool_calls") or parse_text_tool_calls(content)
                    assistant: dict[str, Any] = {"role": "assistant", "content": content}
                    if tool_calls:
                        for tc in tool_calls:
                            tc.setdefault("id", "call_" + uuid.uuid4().hex[:10])
                            tc.setdefault("type", "function")
                        assistant["tool_calls"] = tool_calls
                    messages.append(assistant)
                    self.log("assistant", step=steps, content=content[:2000],
                             tools=[tc["function"]["name"] for tc in tool_calls], finish=msg.get("_finish_reason"))

                    if not tool_calls:
                        no_tool_streak += 1
                        if no_tool_streak >= 3:
                            status, summary = "done" if content else "gave_up", content or "model stopped calling tools"
                            break
                        nudge = prompts.TRUNCATED_NUDGE if msg.get("_finish_reason") == "length" else prompts.NO_TOOL_NUDGE
                        messages.append({"role": "user", "content": nudge})
                        continue
                    no_tool_streak = 0

                    finished = False
                    for tc in tool_calls:
                        name = tc["function"].get("name", "")
                        raw_args = tc["function"].get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                            if not isinstance(args, dict):
                                raise ValueError("arguments must be a JSON object")
                        except Exception as e:
                            result = f"error: invalid JSON arguments for {name}: {e}. Send a valid JSON object."
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                            continue

                        sig = name + json.dumps(args, sort_keys=True)[:2000]
                        recent.append(sig)
                        recent = recent[-8:]
                        repeats = sum(1 for s in recent if s == sig)

                        if name == "finish":
                            if edited_since_test and not finish_refused and self.test_cmd and self.mode != "tests":
                                finish_refused = True
                                result = prompts.FINISH_NEEDS_TESTS.format(test=self.test_cmd)
                            else:
                                status = args.get("status") or "done"
                                summary = str(args.get("summary") or "")
                                locked_problem = str(args.get("locked_test_problem") or "").strip()
                                result = "ok"
                                finished = True
                        elif mcp.handles(name):
                            result = await mcp.call(name, args)
                        else:
                            try:
                                result = await asyncio.to_thread(self.tools.call, name, args)
                                if name in ("write_file", "edit_file"):
                                    edited_since_test = True
                                elif name == "run_command" and self._is_test_run(args.get("command", "")):
                                    edited_since_test = False
                            except ToolError as e:
                                result = f"error: {e}"
                            except Exception as e:
                                result = f"error: {type(e).__name__}: {e}"
                        if repeats >= 3 and not finished:
                            result += ("\n\nNOTE: you have made this exact call several times. It is not making progress - "
                                       "try a different approach, or call finish with status gave_up.")
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": clip(result, TOOL_OUTPUT_CHARS)})
                        self.log("tool", name=name, args=json.dumps(args)[:500], result=result[:1500])
                        if repeats >= 6:
                            status, summary = "gave_up", f"stuck repeating {name}"
                            finished = True
                        if finished:
                            break
                    if finished:
                        break
                else:
                    status, summary = "step_limit", f"reached the step limit of {self.max_steps}"
            finally:
                result = {
                    "status": status, "summary": summary, "steps": steps,
                    "locked_test_problem": locked_problem,
                    "files_written": sorted(self.tools.files_written),
                    "commands": self.tools.commands[-30:],
                    "usage": {"calls": self.llm.calls, "prompt_tokens": self.llm.prompt_tokens,
                              "completion_tokens": self.llm.completion_tokens},
                }
                (self.out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                self.log("end", **{k: v for k, v in result.items() if k in ("status", "summary", "steps")})
                self.transcript.close()
                if sink is not None:
                    sink.close()
        return result

    def _is_test_run(self, command: str) -> bool:
        c = command.strip()
        if self.test_cmd and self.test_cmd.strip() in c:
            return True
        return any(k in c for k in ("pytest", "npm test", "npm run test", "vitest", "jest", "go test", "unittest"))
