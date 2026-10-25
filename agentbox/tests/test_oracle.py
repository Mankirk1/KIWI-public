"""Outline checks and the mutation check (agentbox/oracle.py and its sandbox scripts).
Run:  python3 -m unittest tests.test_oracle -v"""
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator")]

from agentbox.oracle import (MUTATION_MARK, ORACLE_MARK, Check, CheckDraft, CheckDraftSet, coder_text,  # noqa: E402
                             extract_checks, judge, load_checks, safe_eval, save_checks, stage, take_marked,
                             validate_checks)

OUTLINE = """# mechcalc
- `Rectangle(b, h)`: A = b·h, I = b·h³/12, c = h/2
| key | name | E (GPa) |
| stainless-304 | Stainless steel 304 | 193 |
- `mechcalc materials` lists the materials table. Every command accepts `--json` (one JSON object).
- Errors print one line to stderr and exit with status 2.
"""


def check(**kw) -> Check:
    base = {"id": "C1", "task": "T2", "kind": "value", "source": "outline", "imports": ["mechcalc.sections"]}
    return Check.model_validate({**base, **kw})


class SafeEvalTest(unittest.TestCase):
    def test_arithmetic_only(self):
        self.assertAlmostEqual(safe_eval("0.05*0.1**3/12"), 0.05 * 0.1 ** 3 / 12)
        self.assertAlmostEqual(safe_eval("pi*(0.05**4 - 0.03**4)/64"), 3.141592653589793 * (0.05 ** 4 - 0.03 ** 4) / 64)
        self.assertEqual(safe_eval("sqrt(16)"), 4.0)
        self.assertEqual(safe_eval("'Stainless steel 304'"), "Stainless steel 304")
        self.assertEqual(safe_eval("-1 + 1"), 0)
        for bad in ("__import__('os')", "a.b", "x", "[1, 2]", "open('f')", "(lambda: 1)()"):
            with self.assertRaises(Exception, msg=bad):
                safe_eval(bad)


class ValidateTest(unittest.TestCase):
    def test_numbers_must_come_from_the_outline_or_the_inputs(self):
        ok = [check(expr="mechcalc.sections.Rectangle(b=0.05, h=0.1).second_moment", expect="0.05*0.1**3/12"),
              check(id="C2", imports=["mechcalc.materials"], expr="mechcalc.materials.get_material('stainless-304').E",
                    expect="193e9")]                                             # 193 GPa in SI
        self.assertEqual(validate_checks(ok, OUTLINE, {"T2"}), [])
        self.assertAlmostEqual(ok[0].expect_value, 4.1666666666666675e-06)
        computed = [check(expr="mechcalc.sections.Rectangle(b=0.05, h=0.1).second_moment", expect="4.1667e-06")]
        self.assertIn("neither in the outline nor in expr", " ".join(validate_checks(computed, OUTLINE, {"T2"})))

    def test_strings_commands_tasks_and_expressions(self):
        problems = " | ".join(validate_checks([
            check(id="C1", imports=["mechcalc.materials"], expr="mechcalc.materials.get_material('x').name",
                  expect="'Stainless Steel 304'"),                                   # not verbatim
            check(id="C2", kind="command", run="calc materials", exit_code=0),        # program not in the outline
            check(id="C3", kind="command", run="mechcalc materials"),                 # nothing expected
            check(id="C4", task="T9", expr="mechcalc.sections.Circle(d=1).area", expect="pi/4"),
            check(id="C5", expr="os.system('x')", expect="1"),                        # not the imported module
            check(id="C6", kind="raises", expr="mechcalc.sections.Rectangle(b=-1, h=1)", error="not a name"),
            check(id="C6", kind="command", run="mechcalc materials --json", stdout="json_object"),  # duplicate id
            check(id="C7", kind="command", run='python -c "import mechcalc.cli; mechcalc.cli.main([])"', exit_code=2),
            check(id="C8", kind="command", run="mechcalc materials --json | head", exit_code=0),
            check(id="C9", kind="command", run="python -m mechcalc materials", exit_code=0),
        ], OUTLINE + "\nTest command: `python -m pytest -q`", {"T2"}))
        for needle in ("does not appear in the outline", "'calc' does not appear", "say what to expect", "'T9'",
                       "must start with an imported module", "exception class name", "duplicate id",
                       "not inline code (python -c", "no shell syntax",
                       "never runs `python -m mechcalc`"):
            self.assertIn(needle, problems)
        good = [check(kind="command", run="mechcalc materials --json", exit_code=0, stdout="json_object"),
                check(id="C2", kind="command", run="mechcalc section rect --b -1 --h 1", exit_code=2, stderr_lines=1,
                      stdout="empty"),
                check(id="C3", kind="raises", expr="mechcalc.sections.Rectangle(b=-1, h=1)", error="ValueError")]
        self.assertEqual(validate_checks(good, OUTLINE, {"T2"}), [])
        self.assertEqual(validate_checks([check(kind="command", run="python -m pytest -q", exit_code=0)],
                                         OUTLINE + "Test command: `python -m pytest -q`", {"T2"}), [])

    def test_extract_keeps_valid_checks_and_drops_the_rest(self):
        drafts = CheckDraftSet(checks=[
            CheckDraft(id="C1", task="T2", kind="value", source="A = b·h", imports=["mechcalc.sections"],
                       expr="mechcalc.sections.Rectangle(b=2, h=3).area", expect="2*3"),
            CheckDraft(id="C2", task="T2", kind="value", source="A = b·h", imports=["mechcalc.sections"],
                       expr="mechcalc.sections.Rectangle(b=2, h=3).area", expect="6.0001")])

        class LLM:
            calls = []

            def structured(self, role, system, user, cls, max_repairs=2, extra_check=None):
                self.calls.append(user)
                return drafts
        llm = LLM()
        tasks = [{"id": "T1", "title": "skeleton", "kind": "scaffold"}, {"id": "T2", "title": "x", "kind": "feature"},
                 {"id": "T3", "title": "y", "kind": "feature"}]
        checks, dropped = extract_checks(llm, OUTLINE, "spec", tasks, repairs=2)
        self.assertEqual([(c.id, c.task) for c in checks], [("C1", "T2"), ("C2", "T3")])   # renumbered, one per task
        self.assertEqual(len(dropped), 2)
        self.assertTrue(dropped[0].startswith("T2: "))
        self.assertEqual(len(llm.calls), 6)                     # per task (no scaffold): the first ask and two repairs
        self.assertIn("checks for task T2 only", llm.calls[0])
        self.assertIn("These problems were found", llm.calls[1])

    def test_save_load_and_owner_edits(self):
        d = Path(tempfile.mkdtemp())
        c = check(expr="mechcalc.sections.Rectangle(b=2, h=3).area", expect="2*3")
        validate_checks([c], OUTLINE, {"T2"})
        save_checks(d, [c])
        data = json.loads((d / "checks.json").read_text())
        data["checks"][0]["expect"] = "2*3*1"
        data["checks"].append({**data["checks"][0], "id": "C2", "expect": "import os"})
        (d / "checks.json").write_text(json.dumps(data))
        loaded = load_checks(d)
        self.assertEqual(loaded[0].expect_value, 6)
        self.assertFalse(loaded[1].enabled)                     # broken by hand: switched off, not crashing
        self.assertIn("C1: mechcalc.sections.Rectangle(b=2, h=3).area == 2*3*1", coder_text(loaded, "T2"))
        self.assertEqual(coder_text(loaded, "T3"), "")


PKG = {
    "src/mechcalc/__init__.py": "",
    "src/mechcalc/sections.py": '''
        from __future__ import annotations
        from dataclasses import dataclass
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from mechcalc.materials import Section   # does not exist (run 3's nit)

        @dataclass(frozen=True)
        class Rectangle:
            b: float
            h: float

            def __post_init__(self):
                if self.b <= 0 or self.h <= 0:
                    raise ValueError("b and h must be positive")

            @property
            def area(self):
                return self.b * self.h

            @property
            def second_moment(self):
                return self.b * self.h ** 3 / 12
        ''',
    "src/mechcalc/materials.py": '''
        STAINLESS_E = 200e9   # run 2's wrong value; the outline says 193 GPa
        ''',
    "cli.py": '''
        import json, sys
        args = sys.argv[1:]
        if "--bad" in args:
            print("mechcalc: error: bad input", file=sys.stderr)
            sys.exit(2)
        if "--json" in args:
            print(json.dumps([{"key": "steel-a36"}]))   # an array, not one object (run 3)
        else:
            print("key        name")
        ''',
    "tests/test_sections.py": '''
        import unittest
        from mechcalc.sections import Rectangle

        class T(unittest.TestCase):
            def test_area(self):
                self.assertEqual(Rectangle(2, 3).area, 6)
            def test_invalid(self):
                with self.assertRaises(ValueError):
                    Rectangle(-1, 1)
        ''',
}


def make_pkg() -> Path:
    d = Path(tempfile.mkdtemp(prefix="oracle-pkg-"))
    for rel, text in PKG.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(textwrap.dedent(text).lstrip())
    return d


def run_after(tree: Path, after: str, rc: int = 0) -> str:
    p = subprocess.run(["bash", "-c", f"rc={rc}\n{after}"], cwd=tree, capture_output=True, text=True, timeout=300,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tree / "src")})
    return p.stdout + p.stderr


class RunnerTest(unittest.TestCase):
    TEST = "PYTHONPATH=src python3 -m unittest discover -s tests -q"

    def test_checks_and_import_check_in_a_project(self):
        tree = make_pkg()
        checks = [check(id="C1", expr="mechcalc.sections.Rectangle(b=2, h=3).area", expect="2*3"),
                  check(id="C2", expr="mechcalc.sections.Rectangle(b=0.05, h=0.1).second_moment", expect="0.05*0.1**3/12"),
                  check(id="C3", imports=["mechcalc.materials"], expr="mechcalc.materials.STAINLESS_E", expect="193e9"),
                  check(id="C4", kind="raises", expr="mechcalc.sections.Rectangle(b=-1, h=1)", error="ValueError"),
                  check(id="C5", kind="raises", expr="mechcalc.sections.Rectangle(b=1, h=1)", error="ValueError"),
                  check(id="C6", kind="command", run="python3 cli.py materials --json", exit_code=0, stdout="json_object"),
                  check(id="C7", kind="command", run="python3 cli.py materials", exit_code=0, stdout="text",
                        contains=["key"]),
                  check(id="C8", kind="command", run="python3 cli.py section --bad", exit_code=2, stderr_lines=1,
                        stdout="empty"),
                  check(id="C9", imports=["mechcalc.beams"], expr="mechcalc.beams.analyse_beam()", expect="1"),
                  check(id="C10", task="T5", kind="command", run="no-such-program --json", exit_code=0)]
        for c in checks:
            if c.kind == "value":
                c.expect_value = safe_eval(c.expect)
        after = stage(tree, checks, ["src/mechcalc/sections.py", "tests/test_sections.py"], [], self.TEST)
        res, rest = take_marked(run_after(tree, after), ORACLE_MARK)
        self.assertNotIn(ORACLE_MARK, rest)
        status = {r["id"]: r["status"] for r in res["results"]}
        self.assertEqual(status, {"C1": "pass", "C2": "pass", "C3": "fail", "C4": "pass", "C5": "fail", "C6": "fail",
                                  "C7": "pass", "C8": "pass", "C9": "missing", "C10": "missing"})
        detail = {r["id"]: r["detail"] for r in res["results"]}
        self.assertIn("got 200000000000.0, the outline gives 193e9", detail["C3"])
        self.assertIn("instead of raising ValueError", detail["C5"])
        self.assertIn("not one JSON object (it is a JSON list)", detail["C6"])
        self.assertEqual(len(res["dangling"]), 1)
        self.assertIn("imports Section from mechcalc.materials, which does not define it", res["dangling"][0])

        v = judge(res, None, checks, {"T2"})                      # T5's check is for a later task
        self.assertEqual((sorted(v.passed), v.later, v.ok), (["C1", "C2", "C4", "C7", "C8"], 1, False))
        self.assertEqual(sorted(f["id"] for f in v.failed), ["C3", "C5", "C6", "C9"])
        self.assertIn("disagrees with the owner's outline", v.feedback())
        self.assertIn("names that do not exist", v.feedback())
        self.assertIn("Outline checks: 5 pass, 4 fail, 1 for later tasks", v.line())

    def test_mutation_check_finds_what_the_tests_miss(self):
        tree = make_pkg()
        after = stage(tree, [], [], ["src/mechcalc/sections.py"], self.TEST, mutation_max=20, mutation_budget_s=120)
        before = (tree / "src/mechcalc/sections.py").read_text()
        res, _ = take_marked(run_after(tree, after), MUTATION_MARK)
        self.assertEqual((tree / "src/mechcalc/sections.py").read_text(), before)   # always restored
        self.assertEqual(res["baseline"], "survived")
        self.assertGreater(res["tested"], 3)
        self.assertGreater(res["killed"], 0)
        changes = [(s["change"], s["code"]) for s in res["survived"]]
        # second_moment has no test: changing its formula goes unnoticed; area's formula is pinned down
        self.assertTrue(any("self.h ** 3 / 12" in code for _, code in changes), changes)
        self.assertFalse(any(code == "return self.b * self.h" for _, code in changes), changes)
        self.assertFalse(any("Section" in code or '"""' in code for _, code in changes))   # skipped lines
        v = judge({"results": []}, res, [], {"T2"})
        self.assertIn(f"Mutation check: tests caught {res['killed']}/{res['tested']}", v.line())
        self.assertTrue(v.survivors())

    def test_mutation_needs_green_tests_and_never_changes_the_exit_code(self):
        tree = make_pkg()
        (tree / "tests/test_sections.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                                     "    def test_x(self):\n        self.fail('red')\n")
        after = stage(tree, [], [], ["src/mechcalc/sections.py"], self.TEST)
        self.assertNotIn(MUTATION_MARK, run_after(tree, after, rc=1))           # skipped when the tests failed
        p = subprocess.run(["bash", "-c", f"rc=1\n{after}\nexit $rc"], cwd=tree, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        res, _ = take_marked(run_after(tree, after.replace("if [ $rc -eq 0 ]; then ", "").replace("; fi", "")),
                             MUTATION_MARK)
        self.assertEqual(res["error"], "the tests do not pass before mutation")


if __name__ == "__main__":
    unittest.main()
