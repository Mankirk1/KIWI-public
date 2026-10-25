# agentbox improvement plan: research and ready-to-build plans (2026-10-16)

This document is research and plans only. **Nothing in sections 3–9 is built yet; each item waits for the owner's go-ahead.** Section 1 records what was already fixed today (branch `agentbox/run2-fixes`), because the plans build on it.

Every plan follows the same three rules:
- It costs **$0**. One optional exception is marked ⚠ $10.
- It is verified with data before it is trusted.
- It never takes away the owner's veto: pushes and merges stay behind the gates.

Facts about outside services were checked against their official pages on 2026-10-16 (see [Sources](#sources)). Where only third-party sources exist, the text says so.

---

## 0. One-screen summary

**Runs 1–2 in numbers:**
- Two supervised runs (about 4 hours of compute) produced **1 merged task (T1)** and **1 correct but unmerged task (T2, PR #9)**.
- About **35% of compute was wasted** by two logic gaps: the reviewer vetoed the owner's correction, and the spec dropped the outline's data table.
- **60% of model time went to reading prompts**, at about 19 tok/s. Most of that was the first prompt of each attempt, and two-thirds of that first prompt was tool definitions the coder never used.

Both logic gaps and most of the speed problems are fixed today (section 1).

**Ranked recommendations (all $0):**

| # | What | Why | Effort |
|---|---|---|---|
| 1 | Phone push notifications (ntfy, already built in; Remote Control for this Claude Code session) | You missed gates for an hour at a time; my pushes couldn't reach you | 10 min of setup by you |
| 2 | **Sentinel**: a rule-based debugger that runs alongside every run (section 5) | It would have flagged every failure in runs 1–2 within minutes, with evidence | 1–2 sessions |
| 3 | **Spec facts + numeric oracle + property tests** (section 3) | It makes "wrong number" bugs (3 of the 4 failures in runs 1–2) mechanically detectable | 2 sessions |
| 4 | **More speed**: quant benchmark, prompt-cache persistence, fewer model swaps, Arc GPU test (section 4) | Prompt reading is still the bottleneck; 1.5–3× is plausible, to be measured | 1–2 sessions, measurement first |
| 5 | **Free cloud tiers for planner, reviewer and Sentinel only**, via a router (section 7) | Stronger models where few tokens are needed; the coder stays local | 1 session; per-project privacy switch |
| 6 | **A long-running operator agent** (Claude Code now; Hermes Agent evaluated, section 6) | Unattended nights with a supervisor that triages, notifies and learns, while every code change still goes through PRs | 1 session for the MCP bridge; Hermes trial afterwards |
| 7 | **JARVIS command center**: voice, conversation, one agent controlling everything (section 8) | The panel becomes the cockpit | 3–5 sessions, in phases |

**Cost verdict:** everything that runs today costs **$0** beyond electricity (about 0.6 kWh per 8-hour night, an estimate) and your existing Claude subscription, which this supervising session uses.
- GitHub Actions for the private repos uses about 4 minutes per CI run, against 2,000 free minutes a month.
- Without a payment method, GitHub **blocks** usage beyond the free minutes instead of billing it.
- One thing only you can check: that neither your account nor the `mankirk1-agents` org has a payment method or a budget above $0 (section 2).

### Update 2026-10-20: status after runs 3–5, and a re-ranked list

**Done since this plan:**
- ntfy and Remote Control (items 1 and 9.1–9.2);
- Sentinel L1 (item 2, PR #10);
- the first speed step, a cache-preserving context policy: −24% end to end (PR #10);
- mechcalc finished (6/6 tasks).

Everything below is **not built** and waits for the owner's go-ahead. "Solo" means the supervising session can build and verify it alone; "owner" means it needs a decision, account or download from the owner.

| Rank | Item | Evidence | Effort | Who |
|---|---|---|---|---|
| 1 | **Night guard rails** (new): (a) `ci.mode: require` whenever merges are unattended; (b) a merge-question timeout that hands the PR to the owner and continues with tasks that don't depend on it; (c) quiet hours for ntfy (only Sentinel highs at night); (d) a graceful stop before the Windows task's 9-hour limit | Backlog 31; PR #14's CI timeout would have counted as a pass under `advisory` | Small, 1 session | Solo; the owner picks the settings |
| 2 | **Morning digest** (new; variant of 9.5): one ntfy message plus a panel page per night, covering merged PRs with size, Sentinel findings, minutes per task, and anything handed off | Makes auto merge reviewable in 5 minutes | Small | Solo |
| 3 | **Outline oracle** (3.1), extended to **behaviour** (new): FACTS.json for values, plus the outline's commands and documented examples run in the sandbox and compared | Runs 1–2 (wrong values) and run 4 (traceback/no text output) passed their tests; runs 4–5 were caught only by the supervisor running real commands | 2–3 sessions | Solo |
| 4 | **Weak-test check at the tests gate** (3.3): plant errors in a reference implementation and expect failures | The supervisor did this by hand in runs 3–4; it worked every time | 1 session | Solo |
| 5 | **Small loop fixes:** keep locked tests across runs (backlog 27, lost 52 min in run 3); re-read issue edits for blocked tasks (25); `new --use-plan` (6, saves ~35 min); the reviewer flags names that don't exist (26) | Field notes | 1 session total | Solo |
| 6 | **CI visibility** (backlog 30): CI run state and URL at the merge gate, plus a Sentinel rule for CI that never starts | PR #14 waited 20 min blind | Small | Solo |
| 7 | **Speed, phase D** (section 4): slot-cache persistence across model swaps, Q4_K_M/Q4_0 benchmark, static-first prompts, repeat trials n ≥ 3, then the Arc and speculative-decoding experiments | Runs 3–4: 62% of model time reading prompts; T5 cost ~186 min | 1–2 sessions | Solo; the owner approves the ~20 GB download |
| 8 | **Golden incident suite policy** (3.4) and property tests (3.2) | Keeps past lessons from regressing | Ongoing, small | Solo |
| 9 | **Outline templates** (9.7) and the **night budget planner** (9.8) | The next project needs an outline anyway | Small | Owner picks the next project |
| 10 | **Reporting:** coder usage in run reports (backlog 20), metrics in the panel (9.6), `doctor --fix` (9.4) | Convenience | Small each | Solo |
| 11 | **Scale and autonomy:** free-tier router for planner/reviewer (section 7), `agentbox-mcp` and a Hermes trial (section 6), JARVIS (section 8) | Bigger changes; privacy and account decisions | 1–5 sessions each | Owner |
| – | Read-only org access for the supervising session (backlog 8/24) | The owner does every mechcalc GitHub step today | 5 min | Owner (token) |

**Suggested order:** 1 → 2 → 3 → 4 → 5, then decide on fully automatic merging; after that, 6 → 7, then the owner's picks from 11.

---

## 1. Already fixed today (branch `agentbox/run2-fixes`, 67 tests green)

| Problem seen | Fix | How it was verified |
|---|---|---|
| The reviewer vetoed the owner's correction (2900 vs 3400) and blocked correct code | The reviewer gets owner notes, issue comments and OUTLINE.md, which override the criteria. Manual mode asks after a blocking review: send back / **overrule** / send back with notes / leave PR | End-to-end test with a scripted reviewer |
| Notes were lost after a stop | Every gate note is kept in the task history, sent with every later attempt and to the reviewer | End-to-end test |
| The spec dropped the outline's data table, so T3 used wrong material values | The spec copies tables and exact strings verbatim. OUTLINE.md is committed to each repo (backfilled into older ones behind a push question) and protected | End-to-end test (new and existing repos) |
| A wrong locked test made the task impossible | The coder can report it (`locked_test_problem`). Manual: rewrite / keep / hand off. Auto: one rewrite | End-to-end test |
| `conftest.py` loophole around locked tests | Test-runner config is frozen while tests are locked (tool guard, plus revert by the orchestrator) | Unit and end-to-end tests |
| Silent retries of attempts that change nothing | Manual mode asks "Try again / Hand off"; the hand-off message explains a reviewer disagreement | End-to-end test |
| Planner arithmetic in criteria | Formulas instead of computed numbers; the plan gate lists every expected value not found in the outline | Unit test, plus a live dry run (results in the field notes) |
| Prompt reading at about 19 tok/s | f16 KV cache at 12 threads (prompt +40%, generation +7% at 4k context). GitHub tools (about 4,600 tokens) only when a task mentions issues or PRs | `llama bench`, then doctor: 53 tok/s prompt, 12.2 tok/s generation |
| Windows "Best power efficiency" mode | `Start-AgentRun.ps1` sets Best performance for each run; doctor warns | Parser check, plus `powercfg` tested without admin |
| Stale single-file mount after a Docker Desktop restart | Folder mount; hot reload verified | Container recreated; `sed -i` reload test |
| The panel kept running after its task stopped | `Register-PanelTask.ps1 -Stop`, matching only the panel's own process | Tested end to end |

Measurements and evidence: `docs/field-notes/2026-10-phase-c.md`; per-attempt data: `docs/field-notes/runs/`.

---

## 2. Cost audit: does the current setup cost money?

| Component | Cost | Evidence | Note |
|---|---|---|---|
| Local models (llama.cpp via llama-swap) | $0 | Open weights from Hugging Face; runs on your CPU | Electricity: about 60–90 W under load, roughly 0.6 kWh per 8-hour night (estimate) |
| GitHub, private repos (KIWI, mechcalc) | $0 | Free plan: 2,000 Actions minutes a month for private repos. **"usage is blocked once you use up your quota"** without a payment method (GitHub docs) | Measured: KIWI used 58 billable minutes over 15 CI runs (about 3.9 each). mechcalc CI is about 1 minute per run, in the org's own 2,000 |
| GitHub API (issues, PRs, labels) | $0 | No charge for API use | — |
| Tailscale | $0 | Personal plan "Free forever", up to 6 users, for non-commercial use (pricing page) | — |
| Docker Desktop | $0 | Free for personal use and small businesses (<250 employees **and** <$10M revenue) | — |
| WSL2, Ubuntu, Windows scripts | $0 | — | — |
| Egress proxy, PyPI downloads | $0 | — | — |
| This supervising Claude Code session | Your existing Claude subscription | — | No extra cost; subject to the plan's usage limits. agentbox itself never calls Claude |

**Owner check (2 minutes, can't be done from here):**
1. Open github.com → Settings → Billing and licensing → Payment information: no payment method means no possible charges.
2. Do the same for the `mankirk1-agents` organization.

If either has a card, set the Actions budget to $0. My `gh` token doesn't have permission to read billing.

---

## 3. Correctness: make wrong results impossible to merge

All four failures in runs 1–2 were *plausible-looking wrong data*: a wrong expected value, a wrong criterion, wrong table values, and a reviewer judging against a wrong criterion. Today's fixes give the owner the information and the controls. The plans below make the system **detect** such errors mechanically.

### 3.1 Spec facts and a numeric oracle *(highest value)*
- **At planning time:** the planner extracts a machine-readable `FACTS.json` from the outline: tables (material → E, yield, density), formulas (as Python expressions over named inputs), constants, exact strings and CLI flags.
  - The plan gate shows the facts next to the outline, so the owner confirms the extraction once.
  - A deterministic check refuses facts whose values don't appear verbatim in the outline (with unit scaling, the same logic as today's `unverified_numbers`).
- **At the Review tests gate (and in auto mode):** a sandboxed evaluator does two things.
  1. It runs every formula from FACTS.json on the inputs found in the tests.
  2. It compares the results with the tests' expected literals: every `pytest.approx(x)` or `== x` near a call with known inputs.

  A mismatch gets flagged ("Tube(10, 5).second_moment: test expects 92.016, formula gives 460.194"). It would have caught run 1's T2 bug and run 2's T3 bug before they were locked.
- **At review:** the same evaluator checks code constants against the table (stainless E = 200e9 vs facts 193e9).
- **Effort:** about 2 sessions. **Risk:** formulas that don't map to simple expressions. Fallback: facts are advisory only.

### 3.2 Property-based and metamorphic tests
These tests need no hand-computed numbers at all, so arithmetic mistakes can't creep in:
- `Tube(D, d→0) ≈ Circle(D)`
- `IBeam(b, h, tf, tw=b) ≈ Rectangle(b, h)`
- deflection scales with L³ for a point load, and stress is linear in load.

The tests-phase prompt asks for 1–3 such properties per module, using Hypothesis (free, MIT). They complement the example tests and catch formula errors even when the example values are wrong.

### 3.3 Mutation testing of locked tests (weak-test detector)
Before locking, run `mutmut` (free) on the *reference stub*, or a quick self-check that flips operators in the implementation after it passes. If tests survive mutations of key formulas, they're too weak; one-letter `match="b"` patterns would also be flagged. Run it only at the Review tests gate, capped at about 2 minutes.

### 3.4 A golden incident suite (regression tests for the orchestrator itself)
Every incident from the field notes becomes a scripted scenario in `tests/fakes.py`: wrong locked value, reviewer veto, empty attempts, `conftest.py` hack, dropped table. Today's PR added five of these. **Plan:** turn this into a policy, so every new field-note incident gets a scenario before its fix. Then the "self-improving" loop (section 6) can never regress a past lesson.

### 3.5 A second-opinion reviewer *(optional, privacy switch)*
For tasks touching data or formulas, also send the diff plus the outline to a free cloud model (section 7). Block only if both reviewers agree, or if the numeric oracle disagrees. Off by default, enabled per project.

---

## 4. Efficiency: more work per night (measure first, then switch)

Measured baseline after today's changes: prompt reading is about 44 tok/s at 4k context (53 on a 1.4k prompt), and generation is about 11–12 tok/s.

| Idea | Expected gain | How to verify ($0) | Risk |
|---|---|---|---|
| **Quant choice:** test a Q4_K_M or Q4_0 GGUF of the same model. IQ4_XS has no repacked AVX2 kernels in llama.cpp; Q4_0/Q4_K do | Prompt speed 1.3–2× is plausible | Download about 18–20 GB, then `llama bench -d 4096`, then a quality check on the golden suite | Disk, plus about 1 hour of download |
| **Prompt-cache persistence:** llama-server `--slot-save-path` saves the KV of a long prompt; restore it after a model swap | Saves 3–7 minutes per attempt start after a review (the reviewer swaps the coder out) | Count `prompt eval` tokens per attempt in the llama-swap logs before and after | KV files of a few hundred MB on disk |
| **Static-first prompts:** put everything that doesn't change (system prompt, rules, tools) before task-specific text, so the prefix cache hits across tasks | 10–30% fewer prompt tokens | The same log metric | Low |
| **Fewer swaps:** run reviews in batches (e.g. review after the next task's tests phase), or use a smaller reviewer that fits next to the coder | Each avoided swap saves about 50 s of load plus a cold prompt | Swap count per run (Sentinel metric) | Review latency; the RAM limit (25 GB) |
| **Arc A370M offload:** native Windows llama.cpp (Vulkan) with attention and shared weights on the GPU and experts on the CPU (`-ot exps=CPU`) | Unknown. No public A370M MoE benchmark exists; Intel iGPU results suggest prompt speed could improve | 30-minute experiment with the Windows llama.cpp build against the same GGUF | WSL to Windows networking; 4 GB of VRAM |
| **Speculative decoding** with a small draft model | 1.2–1.8× on code generation (typical for dense models; less for MoE) | `llama bench` with the draft model | RAM for the draft model |
| **Plan reuse:** `agentctl new --use-plan projects/<name>` | Saves about 35 minutes per project start | — | Low |
| **Context hygiene:** clip tool outputs harder, use a repo map instead of full reads, compact earlier | Fewer prompt tokens per step | Prompt tokens per attempt (already exported in `runs/*/attempts.csv`) | Low |

---

## 5. Sentinel: a debugger that runs alongside every run

**Goal:** detect, explain and (with the owner's approval) stop problems while they happen, using only *verifiable* data (logs, files, test results, timings). It never relies on a model's opinion alone.

### 5.1 Architecture

```
            ┌──────────────── agentbox run ────────────────┐
 events.jsonl, llama-swap logs, worker transcripts, git, docker stats,
 test results, OUTLINE/FACTS, task history, panel status
            └──────────────┬───────────────────────────────┘
                           ▼
 L1  Watchdog rules (Python, no model, ~0% CPU) ──► finding {rule, evidence[], severity}
 L2  Verifiers (sandboxed Python: numeric oracle, runner-config diff, test-strength check)
 L3  Diagnosis (LLM, only when the CPU is idle or via a free cloud tier) ─► root-cause note citing the evidence
                           ▼
 panel "Health" tab · ntfy push · draft field-notes entry · (manual mode) "pause?" question
```

### 5.2 Detector catalog (L1). Each rule would have fired in runs 1–2.

| Rule | Signal (verifiable) | Would have caught |
|---|---|---|
| Locked-test conflict | Coder messages containing "suspicious", "test expects", "already correct", or `locked_test_problem` | Run 1, T2 (70 minutes of thrashing) |
| Review vs owner note | A blocking issue that contradicts an owner note or OUTLINE value (number or string match) | Run 2, T2 veto |
| Empty attempts | 2 or more `no_changes` in a row | Run 2, T2 (37 minutes) |
| Runner-config tampering | A diff touching `conftest.py` or pytest settings while tests are locked | Run 2, T3 |
| Data drift | Literals in code or tests differ from OUTLINE/FACTS values | Run 2, T3 materials |
| Prompt-reading spike | `prompt eval` above N tokens or above X minutes per request | Every cold start |
| Model swap churn | More than K swaps per hour | — |
| Throughput drop | Generation below 70% of the doctor baseline | The power-mode problem |
| Stuck worker | No transcript line for M minutes while the model server is idle | Hangs |
| Budget burn | Compute minutes per merged task above a threshold | Run 2 overall |
| Environment | Battery, power mode, disk < 20 GB, RAM pressure, stale mount, panel down, proxy denials | Runs 1–2 setup issues |

### 5.3 Where the "debugger model" fits (L3)
A model is only used to *explain* L1/L2 findings and propose a fix, citing evidence lines. Two placements, both $0:
- **Local:** the coder model itself, while it's idle (the owner is deciding at a gate, or between tasks). No extra RAM, and it never competes with a running attempt.
- **Cloud (opt-in):** Gemini Flash free tier or OpenRouter `:free` via the router (section 7). Sends log excerpts only, never code, unless allowed.

A second local model running *simultaneously* is not recommended: CPU inference is compute-bound, so it would slow the coder by roughly the same share it uses.

### 5.4 Safety and actions
- Sentinel is read-only, apart from writing findings.
- In manual mode it may raise a "Pause the run? (evidence…)" question. In auto mode it may pause only for rules the owner pre-approves (e.g. "3 empty attempts").
- It never pushes, merges or answers gates.

### 5.5 Build plan
1. **L1 rules** as a module `agentbox/sentinel.py`, run in-process at every checkpoint plus a 30-second background thread. Each rule gets a replay test against recorded run data (the runs-1–2 logs give real positives). About 1 session.
2. **Panel "Health" tab and ntfy routing.** About ½ session.
3. **L2 verifiers:** the numeric oracle (from section 3.1) and the runner-config diff (exists). About 1 session.
4. **L3 diagnosis** (idle-time local or opt-in cloud). About ½ session.

---

## 6. Long-running, self-improving operation (Hermes Agent and alternatives)

### 6.1 Hermes Agent: verified facts
- Open source (MIT), from Nous Research: "the self-improving AI agent".
- **Features:**
  - persistent agent-curated memory, plus full-text session search;
  - skills it creates and refines itself (agentskills.io format);
  - a built-in cron scheduler;
  - one gateway for Telegram, Discord, Slack, WhatsApp, Signal, email and CLI;
  - 7 terminal backends (local, Docker, SSH, …), MCP servers and subagents.
- **Platforms:** Linux, macOS, **WSL2**, and native Windows.
- **Models:** any OpenAI-compatible endpoint ("custom"), so it can use our llama-swap. **But it requires a context window of at least 64K tokens** and rejects smaller ones at startup.
- **Companion repo:** `hermes-agent-self-evolution` evolves skills and prompts with DSPy + GEPA.

**Fit for agentbox:**
- **Good:** messaging control, scheduling, memory of past incidents, skills like "triage a failed task" or "write a field-notes entry".
- **Constraint:** the 64K minimum context. Our models run at 32K (coder) and 24K (reviewer). A 64K local operator would need a separate llama-swap entry (more RAM, and it swaps the coder out) or a free cloud model.
- **Recommendation:** run Hermes with a **free cloud model** (Gemini Flash free tier, 1M context) as an *operator*, never as the coder. Locally, use it only while agentbox is idle.

### 6.2 Options compared

| Option | Cost | Strengths | Weaknesses |
|---|---|---|---|
| **Claude Code (this session)** with Remote Control, `/loop` and push notifications | Your existing subscription | The most capable supervisor; already knows the codebase and runbook; follows the approval prompts | Needs the desktop app or terminal running; subject to plan limits; Anthropic forbids using subscription logins in third-party tools |
| **Hermes Agent** (cloud free model) | $0 | 24/7 messaging gateway, cron, memory and skills; MIT | Another service to secure; 64K context; free-tier data use |
| OpenHands / Goose (open-source agent platforms) | $0 | Strong coding agents | They overlap with agentbox's own coder; heavier |
| **agentbox-native operator** (Sentinel L3 plus a chat endpoint in the panel) | $0 | No new service; fully under our tests | We build the conversation features ourselves |

### 6.3 Recommended architecture: one control surface, many front-ends
1. **`agentbox-mcp`:** a small MCP server that exposes agentctl as tools.
   - Read: status, ask, events, logs, field notes, run data.
   - Act, with confirmation: mode, pause, resume, stop, continue, answer.
   - Claude Code, Hermes and the JARVIS UI (section 8) all use the same tools, so there's one permission model.
2. **Operators:**
   - **Now:** Claude Code with Remote Control, so you get my pushes and can answer from the Claude app.
   - **Trial:** Hermes Agent on Telegram/Discord with a free cloud model, read-only at first. Then answers to gates come back to you as buttons.
3. **Self-improvement loop** (safe by construction):
   - The steps: finding (Sentinel, evidence) → lesson (field notes) → candidate change (prompt or code, written by the operator) → golden incident suite plus unit and end-to-end tests → PR on KIWI → **owner approval** → CI → merge.
   - Hermes skills can improve *how it operates*, but never merge into agentbox or change gates and budgets.
   - DSPy/GEPA prompt evolution is offline only, scored on the golden suite.
4. **Hard limits:**
   - no access to `.env` (agentctl holds the tokens);
   - no `git push` or merge outside the gates;
   - budgets stay in config.yaml;
   - every action is logged in `events.jsonl` with `by:`.

---

## 7. Free model routing (OmniRoute and alternatives)

### 7.1 OmniRoute: verified facts
- MIT-licensed gateway: one OpenAI-compatible endpoint (`localhost:20128/v1`) in front of 359–372 providers ("150+ free").
- **Features:** quota-aware fallback, 19 routing strategies, token compression (self-reported 15–95%).
- **Runtime:** Node.js 22+. The Docker default is a 1 GB heap; **coding agents need about 8 GB of heap and 10 GB+ of container memory**, which matters on a laptop where WSL already uses 25 of 32 GB.
- **How it gets free tokens:** a mix of official free API tiers, OAuth logins to consumer plans, and **web/cookie providers with TLS-fingerprint impersonation**.
  - It keeps a "ToS: avoid" catalog of 13 providers, excluded from auto-routing by default.
  - Its "~1.62B tokens/month" is described as an *estimate across separately eligible free tiers*, not a grant.

**Assessment:**
- Routing itself is fine.
- **Do not use** the OAuth-subscription or cookie providers:
  - Anthropic has prohibited Claude subscription logins in third-party tools since early 2026 (enforced with server-side blocks), and accounts risk suspension.
  - Cookie and fingerprint-impersonation access is against most providers' terms.
- Pooling several accounts to multiply quotas also breaks terms. One account per provider, official API keys only.

### 7.2 Free tiers you can legitimately use (checked 2026-10-16)

| Provider | Free allowance | Data use | Status |
|---|---|---|---|
| **Google Gemini API** (AI Studio key) | Free tier for Gemini 3.x/2.5 Flash, Flash-Lite, 2.5 Pro and Gemma 4; limits per project, shown in AI Studio | **Used to improve Google's products** on the free tier | Official pricing page, updated 2026-10-15 |
| **OpenRouter `:free` models** | 20 req/min; 50 req/day, or **1,000 req/day after a one-time ⚠ $10 credit purchase** | Free providers may log prompts | Official limits page |
| **Groq** | Free plan (limits shown in the console; Developer plan shows about 30 RPM, 1K RPD, 8K TPM per model) | Per org | Official docs (Developer table); free numbers from third parties |
| **Mistral La Plateforme** ("Experiment") | Free with phone verification; about 1 req/s (unofficial) | **Requests may be used to train models** | Mistral help center |
| **Cloudflare Workers AI** | About 10,000 "neurons" a day | — | Per OmniRoute's catalog; verify before use |
| **NVIDIA NIM** (build.nvidia.com) | About 40 RPM for developers | — | Per OmniRoute's catalog; verify before use |
| Cerebras | **No longer free:** a $5 trial credit with a card, expiring after 30 days (since July 2026) | — | Third-party trackers |
| GitHub Models | **Retired on July 30, 2026** | — | Official GitHub docs |

### 7.3 What free tiers can carry
- **The coder:** one coder attempt sends 100k–240k prompt tokens over 10–15 requests (measured), and a night has 15–30 attempts. That's far beyond free daily request caps, so **the coder stays local**.
- **Planner, plan reviewer, code reviewer and Sentinel diagnosis:** roughly 5–40 requests a night. That fits comfortably.
- **Gains:** a stronger model where judgement matters (catching wrong criteria, reviewing data), and less CPU taken from the coder. Each cloud review also saves a model swap.

### 7.4 Recommended design
- **Router:** **LiteLLM proxy** (open source, Python, light) with an explicit allow-list of the official free tiers above, each with its own rate limit, and the local llama-swap as the final fallback. OmniRoute is an alternative in "API-key providers only" mode, if its dashboard is worth the RAM.
- **agentbox:** the `llm.roles` are already OpenAI-compatible, so the router becomes a new `base_url` per role. Add `privacy.cloud_allowed: false` per project (default off), plus a per-role "what may be sent" setting (`diff`, `outline`, `logs`).
- **Budget guard:** no payment method anywhere means a $0 hard cap. The router enforces per-provider daily caps below the free limits, and Sentinel alerts on 429 storms.

---

## 8. JARVIS command center: voice, conversation, one agent

### 8.1 The experience
- Open `https://<laptop>.<tailnet>.ts.net` on the phone or laptop. A dark HUD shows:
  - a central **status reactor ring** (phase, current task, and progress across tasks);
  - live **tok/s gauges** (from llama-swap `/metrics`);
  - the **decision cards** (gates);
  - the Sentinel health strip;
  - the activity timeline and live log.
- **Hold the mic button** (or say the wake word on the laptop): "Jarvis, what's the agent doing?" → "Task 3, attempt 1: the coder is writing materials.py, 6 minutes in, tests not yet run. One finding: the test file is missing an import."
- "Show me the failing test." "Approve the push." → a **confirmation card** appears, and only a tap confirms it. Voice alone can never approve a push or merge, or stop a run.

### 8.2 Architecture ($0)
- **Front end:** the current panel, extended. Plain HTML/JS with no build step, as today. SSE or WebSocket for live updates, the Web Audio API for push-to-talk.
- **Conversation agent:** a "Jarvis" endpoint on `agentctl serve`, with the `agentbox-mcp` tools from section 6.3 and short-term chat memory plus the long-term notes.
  - When agentbox is idle, it uses the local coder model.
  - During a run, it uses a free cloud model (if allowed) or a small local model in "explain" mode, to avoid stealing CPU from the coder.

### 8.3 Voice stack options

| Part | Option | Cost / licence | Privacy | Latency on this laptop |
|---|---|---|---|---|
| Speech-to-text | Browser Web Speech API | Free | Chrome sends audio to Google's servers; Edge has an on-device mode (`processLocally`, en-US and others) | About 1 s |
| | **whisper.cpp** `base.en`/`small.en` on the laptop | MIT | Local | About 0.5–1.5 s per utterance (competes with a run for CPU) |
| Text-to-speech | Browser `speechSynthesis` | Free; system voices | Local on Windows, Android and iOS | Instant |
| | **Kokoro-82M** | Apache-2.0 | Local | About 1 s |
| | Piper | Original repo archived; successor GPL-3.0 | Local | About 0.4 s |
| Wake word | openWakeWord | Apache-2.0 code; **official models CC BY-NC-SA** (fine for personal use) | Local | Real-time |

**Recommended start:** push-to-talk, Edge on-device STT (or whisper.cpp when idle), and browser TTS. That's zero installs on the phone. Add Kokoro and the wake word later.

### 8.4 Safety
- The panel's existing auth (pairing code plus a 90-day cookie, Tailscale-only) is kept.
- Voice actions are logged with `by: voice`.
- Destructive or publishing actions always need an on-screen confirmation.
- Optional spoken PIN for remote sessions.

### 8.5 Phases
1. **HUD redesign of the panel:** visuals, gauges and the Sentinel strip. About 1–2 sessions.
2. **Text chat with Jarvis** (tools via `agentbox-mcp`). About 1 session.
3. **Push-to-talk:** STT plus TTS. About 1 session.
4. **Wake word, proactive voice announcements** ("Task 3 needs your decision"), and Kokoro voice. About 1 session.

---

## 9. Other ideas

1. **ntfy notifications** (already supported): set `notify.ntfy_url` to a long random topic on ntfy.sh, or self-host ntfy in Docker on the tailnet. Gates then reach the phone instantly. $0.
2. **Remote Control for this Claude Code session:** desktop app → Settings → Claude Code → connect sessions. My push notifications and approval prompts then reach your phone. $0.
3. **Auto mode with guard rails:** once Sentinel and the oracle are proven, run nights in auto mode with `always_ask: [merge]` first, then fully auto.
4. **`doctor --fix`:** auto-repairs known problems (stale mount → recreate; power mode → prompt; disk → `agentctl clean`).
5. **Owner decision log and weekly report:** generated from `events.jsonl` and run data (time per merged task, failure taxonomy, speed trend).
6. **Metrics in the panel:** SQLite fed by the exporter, with sparkline charts. No Grafana needed.
7. **Project templates for outlines** (CLI tool, library, web API), with built-in constraints and FACTS sections, so outlines are oracle-ready.
8. **Night budget planner:** before starting, estimate minutes per task from past data and pick tasks that fit the night.

---

## 10. Roadmap and decisions needed

| Phase | Items | Effort | Cost | Needs your go-ahead |
|---|---|---|---|---|
| A (now) | Merge the run-2 fixes; ntfy and Remote Control; run 3 (manual) | Small | $0 | ✅ merge; ntfy topic and app on your phone |
| B | Sentinel L1 + Health tab; golden-suite policy | 1–2 sessions | $0 | yes |
| C | FACTS + numeric oracle + property tests | 2 sessions | $0 | yes |
| D | Speed: quant benchmark, slot-cache persistence, static-first prompts, Arc experiment | 1–2 sessions | $0 (downloads) | yes (about 20 GB download) |
| E | Router (LiteLLM) for planner, reviewer and Sentinel; privacy switch | 1 session | $0 (⚠ optional $10 OpenRouter) | yes, plus a privacy choice per project |
| F | `agentbox-mcp`; Hermes Agent trial (read-only); self-improvement loop | 2 sessions | $0 | yes |
| G | JARVIS phases 1–4 | 4–5 sessions | $0 | yes |

**Suggested order:** A → B → C (correctness first), then D (speed), then E/F (scale and autonomy), then G (the cockpit). Every phase ends with tests, a field-notes entry and a KIWI PR you approve.

---

## Sources

Checked 2026-10-16.

- Hermes Agent: [GitHub NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), [official site](https://hermes-agent.nousresearch.com/), [quickstart (64K context minimum)](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart), [FAQ (custom endpoints)](https://hermes-agent.nousresearch.com/docs/reference/faq), [DEV overview](https://dev.to/wonderlab/one-open-source-project-a-day-no40-hermes-agent-nous-researchs-self-improving-ai-agent-4ale)
- OmniRoute: [GitHub diegosouzapw/OmniRoute](https://github.com/diegosouzapw/OmniRoute), [providers guide](https://github.com/diegosouzapw/OmniRoute/wiki/Providers-Guide), [nerdzap article](https://nerdzap.com/news/omniroute-open-source-ai-gateway-free-tokens/)
- Anthropic third-party OAuth restrictions: [zbuild.io](https://www.zbuild.io/resources/news/opencode-blocked-anthropic-2026), [paddo.dev](https://paddo.dev/blog/anthropic-walled-garden-crackdown/), [autonomee.ai](https://autonomee.ai/blog/claude-code-terms-of-service-explained/) (secondary sources; check Anthropic's terms for the exact wording)
- Free tiers:
  - [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) and [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
  - [OpenRouter limits](https://openrouter.ai/docs/api-reference/limits)
  - [Groq rate limits](https://console.groq.com/docs/rate-limits)
  - [GitHub Models (retired)](https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models)
  - [Mistral Experiment plan](https://help.mistral.ai/en/articles/450104-how-can-i-try-the-api-for-free-with-the-experiment-plan)
  - Cerebras changes: [BenchLM](https://benchlm.ai/md/free-tier/cerebras.md), [costbench](https://costbench.com/software/llm-api-providers/cerebras-inference/free-plan/)
  - Roundups: [OpenRouter blog](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/), [DEV, October 2026](https://dev.to/tariqnasser/free-llm-api-tiers-in-october-2026-whats-left-and-how-i-chain-them-227l)
- Costs: [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [Tailscale pricing](https://tailscale.com/pricing), [Docker Desktop license](https://docs.docker.com/subscription/desktop-license/)
- Speed: [llama.cpp MoE offload guide](https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide); our own `llama bench` runs (field notes)
- Voice:
  - [MDN processLocally](https://developer.mozilla.org/docs/Web/API/SpeechRecognition/processLocally), [Edge speech recognition](https://learn.microsoft.com/fi-fi/microsoft-edge/web-platform/speech-recognition-api)
  - [openWakeWord models (CC BY-NC-SA)](https://huggingface.co/davidscripka/openwakeword/raw/main/README.md)
  - [Kokoro vs Piper](https://www.promptquorum.com/power-local-llm/piper-vs-kokoro-tts)
  - Latency: [micdrop latency](https://micdrop.dev/docs/ai-integration/local-models/performance), [local voice stack](https://www.kunalganglani.com/blog/local-ai-voice-assistant-whisper-piper-ollama.md)
