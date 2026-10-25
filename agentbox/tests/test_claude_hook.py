"""The Claude Code hook in KIWI/.claude/hooks asks before anything is published to GitHub.
Run:  python3 -m unittest tests.test_claude_hook -v"""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

KIWI = Path(__file__).resolve().parents[2]
HOOK = KIWI / ".claude" / "hooks" / "ask-before-publish.py"
SETTINGS = KIWI / ".claude" / "settings.json"


def _load():
    spec = importlib.util.spec_from_file_location("ask_before_publish", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ClaudeHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = _load()

    def test_asks_for_every_push_form(self):
        for c in ["git push", "git push origin main", "git -C ../x push -f", "git -c push.default=current push",
                  "cd x && git push", "bash -c 'cd x && git push'", "FOO=1 git push", "timeout 60 git push",
                  "echo $(git push)", "echo `git push`", "/usr/bin/git push", "git status; git push --tags",
                  "sudo -u bob git push", "nohup git push > log 2>&1 &", "gh pr merge 3 --squash", "gh repo create a/b",
                  "gh api repos/a/b/pulls/3/merge -X PUT", "gh api --method POST repos/a/b/merges -f base=main"]:
            self.assertTrue(self.hook.reason(c), c)

    def test_silent_for_everything_else(self):
        for c in ["git status", "git commit -m 'fix push logic'", "git log --grep push", "gh pr view 3",
                  "gh api repos/a/b/pulls", "git pull", "grep -r 'git push' .", "git -C x log", "pytest -q",
                  "echo 'unbalanced"]:
            self.assertIsNone(self.hook.reason(c), c)

    def test_hook_protocol(self):
        def run(event):
            return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True)
        r = run({"tool_name": "Bash", "tool_input": {"command": "git -C repo push"}})
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "ask"))
        r = run({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        self.assertEqual((r.returncode, r.stdout), (0, ""))  # no decision: normal rules apply
        r = run({"tool_name": "Read", "tool_input": {"file_path": "x"}})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_settings_rules(self):
        s = json.loads(SETTINGS.read_text())
        self.assertIn("Bash(git push *)", s["permissions"]["ask"])
        self.assertEqual(s["permissions"]["disableBypassPermissionsMode"], "disable")
        cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("ask-before-publish.py", cmd)


if __name__ == "__main__":
    unittest.main()
