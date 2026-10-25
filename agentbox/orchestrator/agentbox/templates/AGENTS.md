# AGENTS.md - rules for AI agents working in this repository

This repository is built and maintained by an automated agent (agentbox) working one
GitHub issue at a time. Humans may also contribute. Read SPEC.md for the design and
OUTLINE.md for the owner's original outline: it is authoritative for exact names, data
values, formulas and strings.

## Commands
- Install / setup: `{{SETUP_COMMAND}}`
- Run all tests: `{{TEST_COMMAND}}`
{{LINT_LINE}}
## Rules
1. Work only on the issue you were given. Do not start other features.
2. Every behaviour change comes with tests. Tests must be deterministic and must not
   need network access, real credentials, a GPU, or external services.
3. Never weaken, skip, or delete existing tests to make a change pass.
4. Do not edit `.github/`, `AGENTS.md`, `SPEC.md` or `OUTLINE.md` - they are maintained by the orchestrator.
5. Never commit secrets, `.env` files, build output, virtual environments or `node_modules`.
6. Keep changes small and focused. Prefer clear code over clever code.
7. Follow the repository layout and conventions in SPEC.md. If SPEC.md is ambiguous,
   choose the simplest reasonable option and mention it in your summary.
