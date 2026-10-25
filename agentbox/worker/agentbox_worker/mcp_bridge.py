"""Bridge to the official GitHub MCP server (stdio), exposing a read-only subset of its
tools to the model. Works with the `mcp` Python SDK 1.x and 2.x."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from contextlib import AsyncExitStack
from typing import Any, Optional

PREFIX = "github_"
# Read-only tools that help with a coding task. Anything else the server offers is hidden
# to keep the tool list (and the model's context) small.
ALLOWED = {
    "issue_read", "list_issues", "search_issues",
    "get_file_contents", "search_code", "get_repository_tree",
    "list_commits", "get_commit",
    "pull_request_read", "list_pull_requests", "search_pull_requests",
}


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


class McpBridge:
    def __init__(self, enabled: bool, toolsets: str, command: str = "github-mcp-server"):
        self.enabled = enabled and bool(os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"))
        self.toolsets = toolsets
        self.command = command
        self.session = None
        self.tools: list[dict] = []
        self._stack: Optional[AsyncExitStack] = None
        self.error: str = ""

    async def __aenter__(self) -> "McpBridge":
        if not self.enabled:
            self.error = "disabled (no token or turned off)"
            return self
        if not shutil.which(self.command):
            self.error = f"{self.command} not found in PATH"
            return self
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            env = {k: v for k, v in os.environ.items() if k in (
                "PATH", "HOME", "GITHUB_PERSONAL_ACCESS_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE")}
            params = StdioServerParameters(command=self.command,
                                           args=["stdio", "--read-only", "--toolsets", self.toolsets], env=env)
            self._stack = AsyncExitStack()
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self.session.initialize(), timeout=45)
            listed = await asyncio.wait_for(self.session.list_tools(), timeout=30)
            for t in _field(listed, "tools", default=[]):
                name = _field(t, "name")
                if name not in ALLOWED:
                    continue
                schema = _field(t, "input_schema", "inputSchema", default=None) or {"type": "object", "properties": {}}
                desc = (_field(t, "description", default="") or "")[:500]
                self.tools.append({"type": "function", "function": {
                    "name": PREFIX + name, "description": "[GitHub, read-only] " + desc, "parameters": schema}})
        except Exception as e:  # MCP is a helper, never a reason to fail the task
            self.error = f"{type(e).__name__}: {e}"
            print(f"[worker] GitHub MCP unavailable: {self.error}", file=sys.stderr)
            await self._close()
            self.tools = []
        return self

    async def _close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self.session = None

    async def __aexit__(self, *exc: Any) -> None:
        await self._close()

    def handles(self, name: str) -> bool:
        return self.session is not None and name.startswith(PREFIX) and name[len(PREFIX):] in ALLOWED

    async def call(self, name: str, args: dict) -> str:
        if self.session is None:
            return "error: GitHub MCP server is not available"
        try:
            res = await asyncio.wait_for(self.session.call_tool(name[len(PREFIX):], args), timeout=90)
        except Exception as e:
            return f"error calling {name}: {type(e).__name__}: {e}"
        parts = []
        for c in _field(res, "content", default=[]) or []:
            text = _field(c, "text")
            parts.append(text if isinstance(text, str) else str(c))
        text = "\n".join(parts) or "(empty result)"
        if _field(res, "is_error", "isError", default=False):
            text = "error: " + text
        return text
