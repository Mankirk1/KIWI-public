# Speed work and Sentinel: before/after results (2026-10-19)

The work was done overnight from 18 to 19 Oct, on branch `agentbox/speed-and-sentinel`. Raw data is in [`bench/2026-10-19-context-policy/`](bench/2026-10-19-context-policy/), and the commands to reproduce it are at the end.

## Summary

- **Speed:** a new context policy for the coder. Compaction never touches the system prompt or task message, so those stay in the server's cache, and it compacts at 60% of the window down to 40%, instead of 70% down to 50%.
  - **End to end on the identical task:** 56.1 min becomes **42.8 min (−24%)**, and the tests pass in both.
  - **On one identical conversation (controlled replay):** 56.9 becomes **50.0 model-minutes (−12%)**, and generation runs at 7.76 tok/s instead of 5.86.
- **A rejected variant.** A first, deeper variant looked better in the replay (−21%). End to end, though, it made the coder re-read files after every compaction: 52 steps instead of 19, and 16% *slower*. It was rejected. This is why both measurements matter: the replay holds the conversation fixed, so it can't show the coder's behavior changing.
- **Sentinel**, replayed over runs 1–4:
  - It flags all four process incidents. The sandbox PATH bug would have been flagged **2 days 20 hours** before it was found, and run 1's wrong locked test **26 minutes** before it was noticed.
  - After the rules were tuned on the real wording, none of its high-severity findings is a false positive.
  - It does not catch wrong *data* (T3's material values) or missing *behavior* (T5's text output). Those need the planned outline oracle.

## 1. Speed: the coder's context policy

### Why

In runs 3 and 4, 61–62% of model time went to reading prompts. Coder conversations grew past 16k tokens, and each compaction threw the server's prompt cache away. The old `compact()` appended a note to the task message (message 2), so everything after the system prompt had to be read again, at about 33 tok/s. Generation also slows as context grows: run 4 averaged 5.4 tok/s.

### Method

- **Scenario:** mechcalc T5 (the CLI), exactly as run 4's coder faced it. The repo is at the T4 merge plus T5's locked tests, with the same task file including the owner's notes. It's the longest task so far.
- **End-to-end trial** (`scripts/bench-coder.py`): the real sandboxed coder, with temperature 0 to reduce randomness, measured from llama-server's own per-request timings. Each policy was run once (n = 1), so the size of the effect is uncertain.
- **Controlled replay** (`scripts/bench-context.py`): the before-trial's full conversation was captured (new opt-in `history.jsonl`) and replayed request by request against the server, applying each policy's compaction. The conversation is identical, so only the cost of context management differs. Generation is measured over the first 48 tokens of each request and extrapolated to the recorded length.
- **Conditions:** Windows on Best performance, the model server otherwise idle. The before-trial overlapped with about 75 seconds of unit tests, so its numbers are slightly pessimistic.

### End-to-end trials (same task, same start, temperature 0)

| | Before (old policy) | Deep variant (rejected) | **After (new policy)** |
|---|---|---|---|
| Compaction | 70% → 50% | 50% → 25%, 8k clip | **60% → 40%, cache-preserving** |
| Wall time | 56.1 min | 64.8 min | **42.8 min (−24%)** |
| Coder steps | 19 | 52 | 22 |
| Compactions | 3 | 6 | 3 |
| Prompt tokens the server evaluated | 66,939 | 89,906 | **57,917 (−13%)** |
| Prompt time | 33.6 min | 42.0 min | **27.8 min (−17%)** |
| Generated tokens | 7,683 | 10,155 | 5,923 |
| Generation time | 21.9 min | 21.7 min | **14.4 min (−34%)** |
| Generation speed | 5.85 tok/s | 7.79 tok/s | 6.87 tok/s |
| Average / largest context | 16.7k / 23.5k | 12.4k / 17.3k | 15.1k / 22.7k |
| File reads | 12 | 30 (test file 7×) | 11 |
| Tests pass | yes | yes | yes |

### Controlled replay (one identical conversation: the before-trial's 19 requests)

| Policy | Model time | Prompt time | Generation time | Evaluated tokens | Average context | Generation speed |
|---|---|---|---|---|---|---|
| Old | 56.9 min | 34.7 | 22.2 | 64.4k | 16.7k | 5.86 tok/s |
| Deep variant | 44.7 min | 27.7 | 17.0 | 61.2k | 12.3k | 7.65 tok/s |
| **New** | **50.0 min (−12%)** | 33.0 | 17.0 | 69.1k | 13.8k | **7.76 tok/s (+32%)** |

### Reading the numbers

- **Where the controlled saving comes from:** almost entirely generation. A smaller context means faster tokens: about 7.8 tok/s instead of 5.9 at the same conversation.
  - Prompt time drops only a little.
  - Compacting earlier re-reads slightly more tokens in total, which offsets the roughly 2,200 tokens per compaction that the cache now keeps.
- **Why the end-to-end saving is larger** (−24%): the coder also took a different path. It generated 23% fewer tokens over 22 steps. With n = 1, part of that may be luck. The controlled −12% is the floor I'd rely on.
- **The trap:** making compaction more aggressive looks good while the coder's behavior is held fixed, but it backfires in practice, because the coder loses its working set and re-reads files.

### Code changes

- `worker/agentbox_worker/agent.py`: `compact()` keeps messages 0–1 byte-identical. The context note goes into the first remaining tool result, and the trimming runs a second pass when the note pushes the size back over target. The trigger is `COMPACT_AT = 0.60` and the target is `COMPACT_TO = 0.40`; `TOOL_OUTPUT_CHARS = 12000` is unchanged.
- An opt-in conversation capture (`"capture": true` in task.json writes `history.jsonl`), plus per-request server timings in the worker's client.
- New scripts: `scripts/bench-coder.py` and `scripts/bench-context.py`. The live worker image is rebuilt, and `agentbox-worker:bench-before` is kept as the baseline.

### Next speed ideas, in order of expected value

1. **Keep the prompt cache across model swaps.** llama-server can save and restore its prompt cache; the first request after each review re-reads 3–7k tokens.
2. **Benchmark a Q4_K_M or Q4_0 build** of the coder model. IQ4_XS has no repacked CPU kernels.
3. **Repeat the trials** (n ≥ 3) to narrow the uncertainty.

## 2. Sentinel: the watchdog

### What it is

`orchestrator/agentbox/sentinel.py` is a set of rules over what a run records: the coder transcripts and the task history. It runs every 30 seconds during a run and once after each task. Medium and high findings go to the activity feed, high findings also go to the phone through ntfy, and everything goes to `control/sentinel.jsonl`. It uses no model, takes negligible CPU, and never changes anything. `agentctl sentinel [repo]` replays the rules over recorded runs.

The rules, and the incident each one comes from:

| Rule | Severity | Incident |
|---|---|---|
| `locked_test_conflict` | high | The coder doubts a locked test or the review in its own words ("the expected value … doesn't match", "already correct", "I cannot edit the locked test file"); run 1 T2, run 2 T2/T3 |
| `empty_attempts` | high | Two attempts in a row with no changes; run 2 T2 |
| `runner_config` | high | conftest/pytest config written while tests are locked, or the attempt refused; run 2 T3 |
| `env_path` | high | "is not on PATH" or FileNotFoundError for an installed command; all runs |
| `repeated_call` | medium | The same tool call 3 times with no edit in between |
| `long_attempt` | medium | An attempt running over 45 min |
| `slow_step`, `compaction` | info | A step over 6 min; context compacted, so the cache is lost |

### Replay over runs 1–4: when each incident would have been flagged

| Incident | Sentinel's first flag | Actually noticed | Earlier by | Cost of the delay |
|---|---|---|---|---|
| Sandbox PATH drops `~/.local/bin`, so installed commands can't run in tests | 15 Oct 22:36 (run 1) | 18 Oct about 18:55 (run 3) | **about 2 days 20 hours** | T5's 52-minute tests phase in run 3; T5 would have been impossible |
| Run 1 T2: the locked test has a wrong expected value | 15 Oct 22:56, 17 min into the attempt | 15 Oct 23:22 (the supervisor read the log) | **26 min** | The attempt ran 70 min until stopped |
| Run 2 T2: the coder disagrees with the review, then attempts with no changes | 16 Oct 13:35; `empty_attempts` 13:56 | 16 Oct about 13:39 | about 4 min | Attempt 3 (17 min) avoidable with a phone alert |
| Run 2 T3: `conftest.py` written to bypass a locked test | 16 Oct 14:43 (at the write) | 16 Oct 14:47 (at the Push gate) | 4 min | In auto mode there is no gate, so Sentinel would be the only signal |
| Run 2 T3: wrong material values | **not flagged** | Supervisor at the Review tests gate | – | Needs the outline/FACTS oracle (roadmap section 3.1) |
| Run 4 T5: outline gaps (traceback, no readable output) | **not flagged** | Supervisor's real-command check | – | Needs behavioral checks from the outline |

### Precision and noise

- **High severity:** after tightening the rules on the coder's real wording, all 21 high-severity findings over runs 1–4 point at a real problem.
  - The first version also matched phrases like "read the locked test file" or "is correct"; those were removed.
  - Tests and incident regressions are in `tests/test_sentinel.py` (8 tests) plus an end-to-end check in `tests/test_control.py`.
- **Live mode:** the PATH warning is reported once per run rather than once per attempt, and findings from earlier runs are not repeated.
- **Info:** 26 slow steps and 15 compactions over the four runs. These go only to `sentinel.jsonl`, not to the feed or the phone.

## Reproduce

```bash
cd ~/KIWI/agentbox
.venv/bin/python scripts/bench-coder.py --label before --image agentbox-worker:bench-before   # about 55 min
.venv/bin/python scripts/bench-coder.py --label after  --image agentbox-worker:latest         # about 45 min
.venv/bin/python scripts/bench-context.py ~/agentbox-runtime/bench/before-*/task/out/history.jsonl --policies old,deep,new
agentctl sentinel mechcalc            # add --all for info findings
```
