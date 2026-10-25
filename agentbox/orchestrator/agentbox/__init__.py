"""agentbox: a local orchestrator that turns a project outline into a GitHub repo,
issues, pull requests and merged code, using open-weight models served locally."""

__version__ = "1.0.0"

# Marker placed in every comment the orchestrator writes, so human comments can be
# told apart even when the orchestrator acts with your own GitHub identity.
BOT_MARKER = "<!-- agentbox -->"
