# agentbox worker / test-runner image.
# Build with scripts/build-worker.sh (passes your WSL user id so files stay yours).
ARG GITHUB_MCP_IMAGE=ghcr.io/github/github-mcp-server:latest

FROM node:22-bookworm-slim AS node
FROM ${GITHUB_MCP_IMAGE} AS ghmcp

FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ripgrep build-essential curl ca-certificates jq less procps tini \
 && rm -rf /var/lib/apt/lists/*

# Node.js 22 + npm/npx/corepack copied from the official image
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && node --version && npm --version

# Official GitHub MCP server binary (stdio mode is used by the worker)
COPY --from=ghmcp /server/github-mcp-server /usr/local/bin/github-mcp-server

# The worker itself
COPY pyproject.toml /opt/agentbox-worker/pyproject.toml
COPY agentbox_worker /opt/agentbox-worker/agentbox_worker
RUN pip install --no-cache-dir /opt/agentbox-worker

# Unprivileged user matching the host user, with a writable home for pip/npm caches.
RUN groupadd -g ${GID} agent 2>/dev/null || true \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash agent \
 && mkdir -p /workspace && chown ${UID}:${GID} /workspace

# Project installs go to the user's site-packages (pip) or the project dir (npm).
ENV PIP_USER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false \
    CI=1 \
    PATH=/home/agent/.local/bin:$PATH

# The test runner and the coder's run_command use login shells (bash -lc), and /etc/profile
# resets PATH there, dropping ~/.local/bin, where pip puts the project's console scripts
# (e.g. `mechcalc`). Keep it on PATH so a test that runs the installed command can pass.
RUN printf '%s\n' 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' \
      > /etc/profile.d/agentbox-local-bin.sh

USER agent
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "agentbox_worker", "/task/task.json"]
