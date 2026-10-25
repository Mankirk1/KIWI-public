# Project: mechcalc

A small, well-tested Python library and command-line tool for first-pass mechanical
engineering calculations on beams: cross-section properties, maximum bending moment,
maximum bending stress, maximum deflection and safety factor for the standard textbook
load cases. Intended as a dependable building block for KIWI's engineering tools.

## What it should do

### Units
- Everything is SI internally: metres, newtons, pascals, kilograms. No unit library.
- Every public function documents its units in its docstring.

### Cross-sections (module `mechcalc.sections`)
Each section is a frozen dataclass with `area` (m²), `second_moment` I (m⁴, about the
horizontal bending axis) and `extreme_fibre` c (m, distance from the neutral axis to the
outermost fibre), all computed from these formulas:
- `Rectangle(b, h)`: A = b·h, I = b·h³/12, c = h/2
- `Circle(d)` (solid): A = π·d²/4, I = π·d⁴/64, c = d/2
- `Tube(outer_d, inner_d)` (hollow circle): A = π·(D² − d²)/4, I = π·(D⁴ − d⁴)/64, c = D/2
- `IBeam(b, h, tf, tw)` (symmetric; flange width b, total depth h, flange thickness tf,
  web thickness tw): A = 2·b·tf + (h − 2·tf)·tw, I = [b·h³ − (b − tw)·(h − 2·tf)³] / 12, c = h/2
- All dimensions must be positive; Tube needs inner_d < outer_d; IBeam needs 2·tf < h and
  tw < b. Invalid input raises `ValueError` with a clear message.

### Materials (module `mechcalc.materials`)
A built-in table (a Python dict or a JSON file shipped with the package) with exactly these
entries; `get_material(key)` returns a frozen dataclass and raises `KeyError` listing the
valid keys for an unknown key:

| key | name | E (GPa) | yield strength (MPa) | density (kg/m³) |
|---|---|---|---|---|
| steel-a36 | Structural steel ASTM A36 | 200 | 250 | 7850 |
| stainless-304 | Stainless steel 304 | 193 | 215 | 8000 |
| aluminium-6061-t6 | Aluminium 6061-T6 | 68.9 | 276 | 2700 |
| titanium-ti6al4v | Titanium Ti-6Al-4V (annealed) | 113.8 | 880 | 4430 |

Stored in SI (Pa, kg/m³).

### Beams (module `mechcalc.beams`)
`analyse_beam(support, load, length, magnitude, section, material)` returns a frozen
result dataclass with: max_moment (N·m), max_stress (Pa) = M·c/I, max_deflection (m),
safety_factor = yield_strength / max_stress, and self_weight_note (the beam's own mass in kg,
= density · A · L, reported but NOT added to the loads).

Load cases (P = point load in N, w = distributed load in N/m, L = length in m):
- simply supported, central point load: M = P·L/4, δ = P·L³ / (48·E·I)
- simply supported, uniform load: M = w·L²/8, δ = 5·w·L⁴ / (384·E·I)
- cantilever, point load at the free end: M = P·L, δ = P·L³ / (3·E·I)
- cantilever, uniform load: M = w·L²/2, δ = w·L⁴ / (8·E·I)

`support` is "simply-supported" or "cantilever"; `load` is "point" or "uniform". Unknown
values, non-positive length or negative magnitude raise `ValueError`.

### Command line (`mechcalc` console script, argparse, standard library only)
- `mechcalc section rect --b 0.05 --h 0.1` (also `circle --d`, `tube --outer-d --inner-d`,
  `ibeam --b --h --tf --tw`) prints A, I and c.
- `mechcalc beam --support cantilever --load point --length 2 --magnitude 1000 --material steel-a36 rect --b 0.05 --h 0.1`
  prints moment, stress (also in MPa), deflection (also in mm), safety factor and mass.
- `mechcalc materials` lists the materials table.
- Every command accepts `--json` for machine-readable output (one JSON object).
- Errors print one line to stderr and exit with status 2.

## Constraints
- Python 3.12, standard library only at runtime. Packaging with `pyproject.toml`
  (setuptools), a `dev` extra containing pytest. Source in `src/mechcalc/`.
- Tests with pytest. Every formula is checked against a value worked out by hand in the
  test (show the arithmetic in a comment), using `pytest.approx(rel=1e-9)`.
- The CLI is tested by calling its `main(argv)` function and by `subprocess` once.
- The full suite runs offline in under 30 seconds.
- README explains installation, the CLI with examples, and the formulas used.
- Keep the names given in this outline exactly (modules, functions, classes, result fields
  such as `self_weight_note`, CLI flags and keys); do not rename them.
- Floating-point results are compared with `pytest.approx(rel=1e-9)`, never with exact
  equality, in tests and in acceptance criteria alike.
- Test command: `python -m pytest -q`. No linter or formatter (no ruff, black, flake8): the
  `dev` extra contains pytest only, and no task depends on formatting checks.

## Out of scope
Units other than SI, shear stress, buckling, combined loading, statically indeterminate
beams, plotting, a GUI, a web API (planned as a follow-up), any network access.
