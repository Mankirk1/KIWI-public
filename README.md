# KIWI-public: agentbox

**agentbox** is an autonomous coding agent that runs on local open-weight models. You give it a
one-page project outline; it creates a private GitHub repository, plans the work as issues, and
merges tested, reviewed pull requests one issue at a time, overnight, on a laptop without a
cloud model API. You can approve each step from your phone (manual mode) or let it run on its
own (auto mode).

It was built for one Windows laptop (Dell XPS 15 9530, Intel i7-13620H, 32 GB RAM, no NVIDIA GPU)
through WSL2 and Docker, and the documentation describes that setup.

## What is in it

| Path | What it is |
| --- | --- |
| [`agentbox/`](agentbox/) | Orchestrator (`agentctl`), sandboxed coder, model server (llama-swap + llama.cpp), egress proxy, phone control panel, setup scripts, Windows scheduling |
| [`agentbox/docs/field-notes/`](agentbox/docs/field-notes/) | Notes and data from the real supervised runs: what failed, why, and how it was fixed |
| [`agentbox/docs/roadmap/`](agentbox/docs/roadmap/) | Research and plans for further improvements |
| [`.github/workflows/`](.github/workflows/) | CI: unit and end-to-end tests, and real-container sandbox checks |
| [`.claude/`](.claude/) | Claude Code settings for this repository: ask before `git push` and merges |

How it works, in short:

- **Planning:** the outline becomes a specification, a task plan (one GitHub issue per task) and
  executable *outline checks* (values, errors and command behaviour taken from the outline).
- **Each task:** the coder writes the tests first; they are locked; then it implements in a
  sandboxed container (no internet except package registries and GitHub through a proxy). The
  tests run in a fresh container, the outline checks and a mutation check run after them, a
  second model reviews the change, CI runs, and the pull request is merged.
- **Control:** gates (plan, start task, review tests, push, merge) that wait for you in manual
  mode; a phone panel reached through Tailscale; notifications through ntfy; a rule-based
  watchdog (Sentinel); a digest of every night's work.

## Getting started

Read [`agentbox/README.md`](agentbox/README.md). In short, inside WSL2 Ubuntu 24.04:

```bash
git clone https://github.com/Mankirk1/KIWI-public.git ~/KIWI
cd ~/KIWI/agentbox && bash scripts/setup-wsl.sh
```

Clone it to `~/KIWI`: the scripts expect `~/KIWI/agentbox`. You need your own GitHub
organization for the agent's repositories and two fine-grained tokens in `agentbox/.env`
(see `agentbox/.env.example`); `agentbox/CLAUDE.md` is the step-by-step runbook.

## Models

On a CPU-only laptop agentbox uses two mixture-of-experts models that stay usable there:
**Qwen3.6-35B-A3B** (planner and coder) and **Gemma 4 26B-A4B** (reviewer), served by
llama-swap. Model files are downloaded by `agentbox/scripts/download-models.sh` and are not
part of this repository.

## About this repository

This is a published snapshot of the agentbox part of a private working repository; it is
updated from there. Issues and pull requests are welcome.

## License

[MIT](LICENSE).
