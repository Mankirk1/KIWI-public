# Outline checks and mutation check, live on mechcalc (2026-10-20/21)

Real model (`qwen-coder`, role `checker`: thinking off, temperature 0.2), real worker image, mechcalc at
`4b21461` (T1–T5 merged; T6 only changed the README).

| File | What |
|---|---|
| `extract.py`, `extract-metrics.json` | First extraction, whole outline per task, *first prompt*: 83 min, 13 calls, 45 valid, 15 dropped (T6 `grep` on the README, T5 multi-statement scripts) |
| `extract_t5.py`, `extract-t5-metrics.json` | T5 (CLI) again with the tightened prompt: 6.8 min, 1 call, 12 checks, 0 dropped (2 `python -m mechcalc` checks were then refused by the new validator rule) |
| `checks.json` | The 50 checks; 48 enabled (7 of the first T5 checks were replaced, 2 are disabled) |
| `sandbox_run.py`, `main-result.json` | Checks + mutation check on the real code |
| `planted-result.json` | Checks on the real code with 7 planted errors from runs 2–4 |

Results:
- Real code: **48/48 checks pass**; the import check finds run 3's real type-only `Section` import; the mutation check
  catches 33/40 planted errors, the 7 survivors are real test gaps (validation boundaries `<= 0` vs `< 0`, `2*tf >= h`,
  the zero-moment special case, exit status 2 of the no-subcommand path, the MPa column of the materials table).
- Planted: **7/7 caught**: stainless E, titanium E and yield (run 2), cantilever deflection `/48` (run 3 plant),
  materials `--json` as an array (run 3), section printing JSON instead of text and the 17-line traceback with exit 1
  (run 4 attempt 1).
