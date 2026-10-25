"""Tests the worker's MCP bridge against a stand-in `github-mcp-server` (a tiny stdio MCP
server written with the Python SDK). Checks the CLI arguments the real binary receives,
tool filtering to the read-only allowlist, and tool calls.
Run:  python3 -m unittest tests.test_mcp_bridge -v"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "worker")]

from agentbox_worker.mcp_bridge import McpBridge  # noqa: E402

FAKE_SERVER = r'''#!{python}
import json, os, sys
log = {args_log!r}
if log:
    open(log, "w").write(json.dumps(sys.argv[1:]))
try:
    from mcp.server.mcpserver import MCPServer as Server   # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server       # mcp 1.x
srv = Server("fake-github")

@srv.tool()
def issue_read(method: str, owner: str, repo: str, issue_number: int) -> str:
    """Get issue details"""
    return f"issue {{owner}}/{{repo}}#{{issue_number}} via {{method}}: token={{bool(os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN'))}}"

@srv.tool()
def issue_write(method: str, owner: str, repo: str) -> str:
    """Create or update issue (must be hidden by the bridge)"""
    return "should never be reachable"

srv.run("stdio")
'''


class McpBridgeTest(unittest.TestCase):
    def test_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "github-mcp-server"
            args_log = Path(d) / "args.json"
            exe.write_text(FAKE_SERVER.format(python=sys.executable, args_log=str(args_log)))
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
            env_backup = dict(os.environ)
            os.environ["PATH"] = f"{d}:{os.environ['PATH']}"
            os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = "read-only-token"
            try:
                async def scenario():
                    async with McpBridge(True, "repos,issues,pull_requests") as mcp:
                        self.assertEqual(mcp.error, "")
                        names = [t["function"]["name"] for t in mcp.tools]
                        self.assertEqual(names, ["github_issue_read"])
                        self.assertTrue(mcp.handles("github_issue_read"))
                        self.assertFalse(mcp.handles("github_issue_write"))
                        out = await mcp.call("github_issue_read", {"method": "get", "owner": "acme", "repo": "x", "issue_number": 7})
                        self.assertIn("acme/x#7 via get", out)
                        self.assertIn("token=True", out)
                asyncio.run(scenario())
                self.assertEqual(json.loads(args_log.read_text()), ["stdio", "--read-only", "--toolsets", "repos,issues,pull_requests"])
            finally:
                os.environ.clear()
                os.environ.update(env_backup)

    def test_disabled_without_token(self):
        os.environ.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)

        async def scenario():
            async with McpBridge(True, "issues") as mcp:
                self.assertEqual(mcp.tools, [])
                self.assertIn("disabled", mcp.error)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
