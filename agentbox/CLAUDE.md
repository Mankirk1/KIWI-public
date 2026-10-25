# agentbox — runbook for Claude Code sessions

You are continuing the setup of agentbox on the owner's Dell XPS 15 9530 (i7-13620H,
32 GB RAM, Intel Arc A370M — no NVIDIA GPU, so models run on the CPU). The session should
be a Claude Code **WSL** session in Ubuntu-24.04 with the project folder `~/KIWI`.

## Rules

- Never print, `cat`, `grep` or echo the contents of `.env`, and never ask the owner to
  paste a token into the chat. To check tokens exist, use `agentctl doctor`, which reports
  validity without revealing them. The owner edits `.env` themselves with `nano`.
- You cannot answer `sudo` password prompts. If a command needs sudo, stop and give the
  owner the exact command to run in their own Ubuntu terminal.
- Never run `git clean -x`, `git reset --hard` or `rm -rf` inside `~/KIWI` or
  `~/agentbox-runtime`. Models (~31 GB) and project state live in `~/agentbox-runtime`.
- Do not run `wsl --shutdown` or change Windows settings; ask the owner (they are the only
  steps that would end this session or need admin rights).
- Stop a running agent with `agentctl stop`, not by killing processes.
- Work on KIWI through branches and pull requests; do not push to `main` directly.
- Prefer measuring over guessing: `agentctl doctor` prints model speed and isolation checks.
- Never run `agentctl pair` or read `~/agentbox-runtime/control/{sessions,pair}.json`: pairing
  codes are for the owner's phone. Ask the owner to run `agentctl pair` themselves.
- `git push` and `gh pr merge` ask for the owner's approval in this repo (`.claude/settings.json`).
  That is intended: say what you are about to push and wait; do not work around the prompt.
- Do not answer the agent's approval questions (`agentctl answer`) unless the owner tells you
  which answer to give.
- Keep the field notes: after every step (gate, phase, failure, fix, measurement) append an
  entry to `docs/field-notes/` (current file: `2026-10-phase-c.md`) with timings, attempts,
  speeds and evidence, and keep its "Improvement backlog" current. Never paste raw logs,
  transcripts or anything token-like into it. Run data: `scripts/export-run-data.py`.
- Windows scripts are run as `powershell -ExecutionPolicy Bypass -File .\<script>.ps1`
  (Windows blocks downloaded scripts by default).

## Status (update this list as you go)

Done in the cloud session on 2026-10-11 (merged to `main`, CI green):
- [x] PR #2: KIWI reorganized; agentbox committed with CPU profile `32gb-cpu`, Ollama guard,
      runtime folder outside git, first outline `outlines/mechcalc.md`; CI builds the
      worker image and checks the sandbox, proxy and model server on GitHub.
- [x] PR #3: phone control: approval gates (auto/manual), pause/stop, `agentctl serve`
      panel, `windows/Register-PanelTask.ps1`; Claude Code asks before `git push` in KIWI.

On the laptop, 2026-10-15 → 20 (Phase C steps 1–14 done; the owner registers the nightly task). Details, measurements
and the improvement backlog: `docs/field-notes/2026-10-phase-c.md`; per-run data: `docs/field-notes/runs/`.
- [x] Steps 1–9: preflight OK (14 CPUs, 25 GB RAM), models downloaded, doctor all OK.
- [x] Step 10, redone with `llama bench` on 2026-10-16 (single doctor runs were too noisy): profile
      `32gb-cpu` now uses `--threads 12 --threads-batch 12` and an f16 KV cache. Doctor: qwen-coder
      12.2 tok/s generation, 53 tok/s prompt (1,400-token prompt); gemma-reviewer 8.9 / 39.
      Windows power mode must be "Best performance" (doctor checks; `Start-AgentRun.ps1` sets it).
- [x] Step 11 / PR #5: planner omitted the optional task `kind` under grammar mode, so no plan
      had a scaffold; `kind` is now required. Outline pins names, `pytest.approx`, no linter.
- [x] Step 12: panel via Tailscale (`control.public_url` set), phone paired, mode switch works.
- [x] Step 13: five supervised runs (manual, 2h budget) built mechcalc T1–T6.
      - Run 1: T1 merged; T2's tests phase locked a wrong value → PR #6 (tests review gate).
      - Run 2: reviewer vetoed a correct T2 (it never saw the owner's note), 2 empty attempts, T3 on
        wrong data → PR #7 (owner notes reach the reviewer and persist, OUTLINE.md in repos,
        locked-test report, conftest guard, GitHub tools only when needed).
      - Run 3: T3 and T4 merged in one attempt each; sandbox PATH fix → PR #8.
      - Run 4: T5 merged after one send-back (the outline asked for readable output and exit 2 that
        no test covered).
      - Run 5: T6 README verified (formulas and command output checked); mechcalc PR #14 left for the
        owner after a CI timeout.
      - Measured in runs 3–4: prompt ~33–35 tok/s, generation 5.4–6.4 tok/s, ~62% of model time
        reading prompts.
- [x] Speed work and Sentinel (PR #10, approved by the owner): a cache-preserving coder context
      policy (compact at 60% → 40%), −24% end to end on T5 (56.1 → 42.8 min) and −12% in a
      controlled replay; Sentinel watchdog live from run 5. Report:
      `docs/field-notes/2026-10-19-speed-and-sentinel.md`.
- [x] Step 14, hand-off (2026-10-20): `budget.max_hours: 8`, mode `auto` with
      `control.always_ask: [merge]` (agents work overnight, nothing merges without the owner),
      STOP file cleared.
- [ ] Owner: register the nightly task (`Register-NightlyTask.ps1`, optional
      `Set-OvernightPower.ps1`). mechcalc is finished once PR #14 is merged, so nightly runs
      have no work until a new outline is queued (`agentctl queue outlines/<name>.md`).
- Plans awaiting the owner's go-ahead (numeric/behavioural oracle, free-tier router, Hermes Agent,
  JARVIS UI, further speed ideas): `docs/roadmap/2026-10-improvement-plan.md`. Do not build them
  without a go-ahead.

Owner steps before this session (setup guide steps 1–4): WSL + `.wslconfig`, Docker Desktop
with WSL integration, `sudo apt-get install ... gh`, `gh auth login`, clone KIWI,
GitHub org `<owner>-agents` + two fine-grained tokens in `.env`.
Owner steps during Phase C: guide step 8 (Tailscale, panel task, pairing) at Phase C step
12; guide step 7 (nightly task) at the hand-off.

## Phase C — what to do in this session, in order

Check each step's result before moving on. Stop and tell the owner when a step needs them.

1. **Preflight**
   - `uname -a; nproc; free -g; df -h ~` → expect ~14 CPUs and ~25 GB RAM (from `.wslconfig`);
     if RAM shows ~15 GB, `.wslconfig` was not applied → owner fixes it (guide step 1).
   - `docker info --format '{{.ServerVersion}}'` works; `gh auth status` is logged in.
   - Need at least 45 GB free in `~` for models + images.
   - `docker ps --format '{{.Names}}  {{.Ports}}'`: nothing may already publish port 8080
     (the old `ollama_installation_guide` site container and `paired-orchestrator` from
     labs/ used it). Ask the owner before removing anything: `docker rm -f <name>`.
2. **Install**: `bash ~/KIWI/agentbox/scripts/setup-wsl.sh`. If it exits with code 2 and
   prints an `apt-get` command, hand that command to the owner, then re-run.
3. **Profile**: `cd ~/KIWI/agentbox && scripts/select-profile.sh 32gb-cpu`.
4. **Config**: set `github.owner` in `config.yaml` to the owner's agent organization (ask
   for the name if unknown). Ask the owner to fill `GH_ADMIN_TOKEN` and `GH_AGENT_TOKEN`
   in `.env` with `nano ~/KIWI/agentbox/.env` in their own terminal.
5. **Models**: `scripts/download-models.sh` (~31 GB; run in the background with output to
   `~/agentbox-runtime/download.log` and poll it; resumable if interrupted).
6. **Sandbox image**: `scripts/build-worker.sh` (can run while models download).
7. **Services**: `docker compose up -d && docker compose ps` → both containers running.
   `curl -s localhost:8080/v1/models` lists `qwen-coder` and `gemma-reviewer`.
8. **Ollama**: `agentctl doctor --quick` shows the Ollama line. With Ollama running on
   Windows it should say WARN with "stops it at the start of every run". Do not stop the
   owner's Ollama outside a run.
9. **Doctor**: `agentctl doctor` (loads each model; first load can take minutes). All lines
   OK. Record the generation and prompt speeds in this file's Status list.
10. **Tune threads** (optional, ~20 min): in `llama-swap/config.yaml` try `--threads 4`, `6`, `8`
    (file is hot-reloaded), run `agentctl doctor` each time, keep the fastest generation
    speed. Keep `--threads-batch` at 10 unless prompt speed improves clearly otherwise.
11. **Dry run**: `agentctl plan-only outlines/mechcalc.md`. Review
    `~/agentbox-runtime/projects/mechcalc/SPEC.md` and `plan.json` with the owner:
    scaffold first, 6–10 small tasks, `python -m pytest -q` style test command.
12. **Phone panel** (guide step 8; do it before the first run so the owner can approve
    from the phone). Run `agentctl serve` in the background with output to
    `~/agentbox-runtime/logs/panel.log` and check `curl -s localhost:8765/healthz`. Then
    ask the owner to: install Tailscale on Windows and the phone; copy the scripts
    (`cp ~/KIWI/agentbox/windows/*.ps1 /mnt/c/agentbox/`); stop your background panel
    first (only one process can use port 8765); run
    `powershell -ExecutionPolicy Bypass -File .\Register-PanelTask.ps1 -Tailscale`
    in Windows PowerShell; set `control.public_url` in `config.yaml` to the printed
    `https://…ts.net` address; run `agentctl pair` themselves and sign in on the phone.
    Check: the owner sees the panel; `agentctl mode` changes when they tap Manual/Auto.
    If the owner skips this step, use `agentctl ask` / `agentctl answer` in step 13.
13. **First supervised run**: with the owner's go-ahead, set `budget.max_hours: 2` in
    `config.yaml`, `agentctl mode manual` (the owner approves each step from the panel, or
    tells you the answer for `agentctl answer`), run `agentctl new outlines/mechcalc.md` in
    the background with output to a log, and watch `~/agentbox-runtime/logs/` and the
    GitHub repo. Fix infrastructure problems (setup command, proxy allow-list, timeouts);
    do not hand-write the project's code.
14. **Hand-off**: restore `budget.max_hours: 8`; set the mode the owner wants for nights
    (`agentctl mode auto` unless they ask otherwise: manual waits all night for answers);
    tell the owner to do the Windows nightly steps (guide step 7: `Register-NightlyTask.ps1`,
    optional `Set-OvernightPower.ps1`); record the measured speeds and tuning in the Status
    list above and open a PR on KIWI (the owner approves the `git push` prompt).

## Useful commands

```bash
agentctl status                                   # tasks per project
tail -f ~/agentbox-runtime/logs/agentctl-$(date +%Y%m%d).log
tail -f "$(ls -td ~/agentbox-runtime/work/*/*/ | head -1)worker.log"
docker compose logs -f llama-swap                 # model loading, speed, errors
scripts/proxy-log.sh denied                       # blocked downloads (TCP_DENIED)
agentctl mode; agentctl ask                       # current mode; questions waiting for the owner
curl -s localhost:8765/healthz                    # phone panel up?
agentctl sentinel mechcalc                        # watchdog findings (replay over recorded runs)
.venv/bin/python -m unittest discover -s tests    # agentbox's own tests
```
