#!/usr/bin/env bash
# Build the sandbox image used for the coding worker and the test runner.
# Re-run after editing worker/ or to pick up a newer GitHub MCP server.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build \
  --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
  --build-arg GITHUB_MCP_IMAGE="${GITHUB_MCP_IMAGE:-ghcr.io/github/github-mcp-server:latest}" \
  -t agentbox-worker:latest worker/
echo
docker run --rm agentbox-worker:latest bash -lc 'python3 --version; node --version; github-mcp-server --version 2>&1 | head -1; whoami'
