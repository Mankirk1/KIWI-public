# KIWI-public — notes for Claude Code sessions

- This repository is **agentbox**: see `README.md` and `agentbox/README.md`.
- If you are setting agentbox up or operating it, follow `agentbox/CLAUDE.md` (runbook, rules).
  Clone this repository to `~/KIWI` inside WSL: the scripts expect `~/KIWI/agentbox`.
- Never commit secrets: `.env` files, tokens, SSH keys, `config.yaml`, model and runtime data.
- Work on branches and open pull requests; CI (`.github/workflows/agentbox.yml`) must pass.
- agentbox tests: `cd agentbox && python -m unittest discover -s tests -v`
  (needs `pydantic pyyaml pathspec mcp`).
