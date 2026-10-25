"""Remote control: approval gates, auto/manual mode, pause/stop, the phone panel's HTTP API
and a manual-mode end-to-end run where a simulated owner answers from the "phone".
Run:  python3 -m unittest tests.test_control -v"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "orchestrator"), str(ROOT / "worker"), str(ROOT / "tests")]

from agentbox.config import Config  # noqa: E402
from agentbox.control import PUSH_CHOICES, TASK_CHOICES, Control, StopRun  # noqa: E402
from agentbox.util import setup_logging  # noqa: E402


def make_cfg(tmp: Path, **control) -> Config:
    cfg = Config.model_validate({"github": {"owner": "acme"}, "budget": {"stop_file": str(tmp / "STOP")},
                                 "guards": {"ollama": "off"}, "control": control,
                                 "paths": {"data_dir": str(tmp / "data")}})
    for d in (cfg.projects_dir, cfg.repos_dir, cfg.work_dir, cfg.logs_dir, cfg.reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


class Owner(threading.Thread):
    """Answers pending questions like the owner tapping buttons on the phone."""

    def __init__(self, ctl: Control, answer, delay: float = 0.03):
        super().__init__(daemon=True)
        self.ctl, self.answer, self.delay = ctl, answer, delay
        self.seen: list[tuple[str, str, str]] = []
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            for a in self.ctl.approvals(status="pending"):
                time.sleep(self.delay)
                choice, note = self.answer(a)
                try:
                    self.ctl.decide(a.id, choice, note, by="test-owner")
                    self.seen.append((a.gate, a.title, choice))
                except ValueError:
                    pass
            time.sleep(0.01)


class ControlUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbox-ctl-"))
        self.sent = []

    def ctl(self, **control) -> Control:
        c = Control(make_cfg(self.tmp, **control), notifier=lambda *a, **k: self.sent.append((a, k)), poll_s=0.01)
        c.cfg.notify.ntfy_url = "https://ntfy.example/topic"
        return c

    def test_auto_mode_never_waits(self):
        c = self.ctl()
        d = c.ask("push", "T1", choices=PUSH_CHOICES)
        self.assertTrue(d.approved and d.auto)
        self.assertEqual(list(c.approvals_dir.iterdir()), [])
        self.assertEqual(self.sent, [])

    def test_always_ask_in_auto_mode(self):
        c = self.ctl(always_ask=["merge"])
        self.assertFalse(c.needs_you("push"))
        self.assertTrue(c.needs_you("merge"))

    def test_manual_waits_for_the_answer_and_records_it(self):
        c = self.ctl(mode="manual")
        owner = Owner(c, lambda a: ("revise", "rename the function"))
        owner.start()
        d = c.ask("push", "T1: add", choices=PUSH_CHOICES, project="acme/x", task="T1", diff="+x\n", summary=["2 files"])
        owner.halt.set()
        self.assertEqual((d.choice, d.note, d.auto), ("revise", "rename the function", False))
        self.assertGreater(c.waited_s, 0)
        a = c.approvals()[0]
        self.assertEqual((a.status, a.choice, a.by), ("answered", "revise", "test-owner"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["priority"], "high")
        texts = [e["text"] for e in c.events()]
        self.assertTrue(any("Waiting for you" in t for t in texts) and any("Send back with feedback" in t for t in texts))

    def test_decide_validation(self):
        c = self.ctl(mode="manual")
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("d", c.ask("push", "T", choices=PUSH_CHOICES,
                                                                      stop_check=self._stop_after(5.0))))
        t.start()
        while not c.approvals(status="pending"):
            time.sleep(0.01)
        aid = c.approvals(status="pending")[0].id
        with self.assertRaises(ValueError):
            c.decide(aid, "revise", "")             # needs a note
        with self.assertRaises(ValueError):
            c.decide(aid, "explode", "")            # not a choice
        with self.assertRaises(LookupError):
            c.decide("20990101-000000-abcdef", "approve")
        with self.assertRaises(ValueError):
            c.decide("../../etc/passwd", "approve")  # ids are checked
        c.decide(aid, "handoff")
        with self.assertRaises(ValueError):
            c.decide(aid, "approve")                # already answered
        t.join(5)
        self.assertEqual(out["d"].choice, "handoff")

    def _stop_after(self, seconds):
        t0 = time.time()

        def check():
            if time.time() - t0 > seconds:
                raise StopRun("test stop")
        return check

    def test_switching_to_auto_approves_what_is_waiting(self):
        c = self.ctl(mode="manual")
        threading.Timer(0.1, lambda: c.set_mode("auto", by="phone")).start()
        d = c.ask("merge", "PR #3", choices=PUSH_CHOICES)
        self.assertEqual((d.choice, d.by), ("approve", "auto mode"))

    def test_stop_while_waiting_expires_the_question(self):
        c = self.ctl(mode="manual")
        threading.Timer(0.1, lambda: c.set_stop(True, by="phone")).start()

        def check():
            if c.stop_requested():
                raise StopRun("stop")
        with self.assertRaises(StopRun):
            c.ask("task", "T1", choices=TASK_CHOICES, stop_check=check)
        self.assertEqual(c.approvals()[0].status, "expired")

    def test_timeouts(self):
        clock = [0.0]
        for action, expect in (("approve", "approve"), ("skip", "handoff"), ("stop", None)):
            c = Control(make_cfg(self.tmp, mode="manual", approval_timeout_min=1, on_timeout=action),
                        notifier=lambda *a, **k: None, poll_s=0.0,
                        sleep=lambda s: clock.__setitem__(0, clock[0] + 30), clock=lambda: clock[0])
            if expect is None:
                with self.assertRaises(StopRun):
                    c.ask("push", "T", choices=PUSH_CHOICES)
            else:
                self.assertEqual(c.ask("push", "T", choices=PUSH_CHOICES).choice, expect)

    def test_night_hours(self):
        from pydantic import ValidationError
        from agentbox.config import NightConfig
        n = NightConfig(hours="23:00-07:30")
        self.assertEqual([n.active(t) for t in ((22, 59), (23, 0), (2, 0), (7, 29), (7, 30), (12, 0))],
                         [False, True, True, True, False, False])
        self.assertTrue(NightConfig(hours="01:00-05:00").active((3, 0)))
        self.assertFalse(NightConfig(hours="").active((3, 0)))
        with self.assertRaises(ValidationError):
            NightConfig(hours="25:00-07:00")

    def _night_ctl(self, at_hour: int, **night):
        clock = [time.mktime((2026, 10, 20, at_hour, 30, 0, 0, 0, -1))]
        sent = []
        c = Control(make_cfg(self.tmp, mode="manual", night={"hours": "23:00-07:30", "question_timeout_min": 1, **night}),
                    notifier=lambda *a, **k: sent.append((a, k)), poll_s=0.0,
                    sleep=lambda s: clock.__setitem__(0, clock[0] + 30), clock=lambda: clock[0])
        c.cfg.notify.ntfy_url = "https://ntfy.example/topic"
        return c, sent

    def test_night_questions_are_left_for_the_owner(self):
        from agentbox.control import MERGE_CHOICES, PLAN_CHOICES
        c, sent = self._night_ctl(2)
        d = c.ask("merge", "T1 — PR #4", choices=MERGE_CHOICES)
        self.assertEqual((d.choice, d.by, d.note), ("handoff", "timeout", "no answer within 1 min"))
        self.assertEqual(c.ask("task", "T2", choices=TASK_CHOICES).choice, "skip")
        with self.assertRaises(StopRun):  # nothing to leave for you at the plan gate: the run stops
            c.ask("plan", "new project", choices=PLAN_CHOICES)
        self.assertEqual(sent[0][1]["priority"], "low")
        self.assertIn("Night: left for you after 1 min", sent[0][0][2])

        # in the day the same question waits for you (no approval_timeout_min set)
        c, sent = self._night_ctl(12)
        polls = [0]

        def give_up():
            polls[0] += 1
            if polls[0] > 200:   # 200 polls = 100 fake minutes
                raise StopRun("test stop")
        with self.assertRaises(StopRun):
            c.ask("merge", "T1 — PR #4", choices=MERGE_CHOICES, stop_check=give_up)
        self.assertEqual(sent[0][1]["priority"], "high")

    def test_night_priority(self):
        c, _ = self._night_ctl(2)
        self.assertEqual((c.priority("high"), c.priority("high", wake=True), c.priority()), ("low", "high", "low"))
        c, _ = self._night_ctl(2, wake_for_sentinel_high=False)
        self.assertEqual(c.priority("high", wake=True), "low")
        c, _ = self._night_ctl(2, quiet=False)
        self.assertEqual(c.priority("high"), "high")
        c, _ = self._night_ctl(12)
        self.assertEqual(c.priority("high"), "high")

    def test_pause_holds_until_resume(self):
        c = self.ctl()
        c.set_paused(True)
        threading.Timer(0.15, lambda: c.set_paused(False, by="phone")).start()
        t0 = time.time()
        c.wait_if_paused()
        self.assertGreater(time.time() - t0, 0.1)
        self.assertGreater(c.waited_s, 0.1)

    def test_mode_persists_and_expire(self):
        c = self.ctl()
        c.set_mode("manual")
        self.assertEqual(Control(c.cfg).mode(), "manual")
        with self.assertRaises(ValueError):
            c.set_mode("yolo")
        from agentbox.control import Approval
        c._save(Approval(id="20260101-000000-aaaaaa", gate="push", title="old"))
        self.assertEqual(c.expire_pending("restart"), 1)
        self.assertEqual(c.get("20260101-000000-aaaaaa").status, "expired")


class PanelServerTest(unittest.TestCase):
    def setUp(self):
        from agentbox.panel import PanelAuth, make_server
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbox-panel-"))
        self.cfg = make_cfg(self.tmp, public_url="https://laptop.tail1234.ts.net")
        self.srv = make_server(self.cfg, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.auth = PanelAuth(self.cfg)
        self.ctl = Control(self.cfg, notifier=lambda *a, **k: None, poll_s=0.01)
        self.cookie = None

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, host=None, headers=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
            if csrf:
                h["X-Agentbox"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode()
        return r.status, data, r

    def pair(self):
        code = self.auth.new_pair_code()
        status, data, r = self.req("POST", "/api/pair", {"code": code})
        self.assertEqual(status, 200, data)
        self.cookie = r.getheader("Set-Cookie").split(";")[0]
        return r.getheader("Set-Cookie")

    def test_page_and_auth(self):
        status, html, r = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("{{NONCE}}", html)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'nonce-", csp)
        self.assertIn(csp.split("'nonce-")[1].split("'")[0], html)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/", host="evil.example:8765")[0], 421)  # DNS rebinding
        self.assertEqual(self.req("GET", "/healthz", host="laptop.tail1234.ts.net")[0], 200)

    def test_digest_endpoint(self):
        self.assertEqual(self.req("GET", "/api/digest")[0], 401)
        self.pair()
        self.assertEqual(self.req("GET", "/api/digest")[1], {"name": "", "text": ""})
        from agentbox.digest import Digest, save_digest
        save_digest(self.cfg, Digest(text="# agentbox digest\n\n**1 merged, 0 left for you**\n"))
        status, data, _ = self.req("GET", "/api/digest")
        self.assertEqual(status, 200)
        self.assertIn("1 merged, 0 left for you", data["text"])
        status, state, _ = self.req("GET", "/api/state")
        self.assertEqual(state["night"]["hours"], "")

    def test_pairing_rules(self):
        code = self.auth.new_pair_code()
        self.assertEqual(self.req("POST", "/api/pair", {"code": code}, csrf=False)[0], 403)  # needs the header
        for _ in range(5):
            self.assertEqual(self.req("POST", "/api/pair", {"code": "000000" if code != "000000" else "111111"})[0], 403)
        self.assertEqual(self.req("POST", "/api/pair", {"code": code})[0], 403)  # locked after 5 wrong tries
        cookie = self.pair()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.req("POST", "/api/pair", {"code": "123456"})[0], 403)  # codes are single-use
        stored = (self.cfg.control_dir / "sessions.json").read_text()
        self.assertNotIn(self.cookie.split("=", 1)[1], stored)               # only hashes on disk
        self.assertEqual(oct((self.cfg.control_dir / "sessions.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.req("GET", "/api/state")[0], 200)
        self.assertEqual(self.req("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)

    def test_controls_and_answers(self):
        self.pair()
        status, s, _ = self.req("GET", "/api/state")
        self.assertEqual((s["mode"], s["paused"], s["stop"], s["agent"]["running"]), ("auto", False, False, False))
        self.assertEqual(self.req("POST", "/api/mode", {"mode": "manual"})[1]["mode"], "manual")
        self.assertEqual(self.req("POST", "/api/mode", {"mode": "nope"})[0], 409)
        self.assertEqual(self.req("POST", "/api/mode", {"mode": "auto"}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertTrue(self.req("POST", "/api/pause", {"paused": True})[1]["paused"])
        self.assertTrue(self.req("POST", "/api/stop", {"stop": True})[1]["stop"])
        self.assertTrue(self.cfg.budget.stop_path.exists())
        self.assertFalse(self.req("POST", "/api/stop", {"stop": False})[1]["stop"])
        self.req("POST", "/api/pause", {"paused": False})

        # The "agent" (this process) asks at the push gate; the phone answers through the API.
        result = {}
        self.ctl.set_status("coding", project="acme/calc", task="T2")
        secret = "ghp_" + "A" * 36
        t = threading.Thread(target=lambda: result.setdefault("d", self.ctl.ask(
            "push", "T2: Add function", choices=PUSH_CHOICES, project="acme/calc", task="T2",
            summary=["1 file"], diff=f"+token = '{secret}'\n+def add(a, b):\n")), daemon=True)
        t.start()
        for _ in range(200):
            s = self.req("GET", "/api/state")[1]
            if s["pending"]:
                break
            time.sleep(0.02)
        self.assertEqual(s["agent"]["phase"], "waiting")
        p = s["pending"][0]
        self.assertEqual((p["gate"], p["diff_lines"]), ("push", 3))
        self.assertNotIn("diff", p)
        full = self.req("GET", f"/api/approvals/{p['id']}")[1]
        self.assertIn("def add", full["diff"])
        self.assertNotIn(secret, full["diff"])                                  # redacted for display
        self.assertEqual(self.req("POST", f"/api/approvals/{p['id']}", {"choice": "revise"})[0], 409)  # needs note
        status, s, _ = self.req("POST", f"/api/approvals/{p['id']}", {"choice": "revise", "note": "no secrets in code"})
        self.assertEqual(status, 200)
        t.join(5)
        self.assertEqual((result["d"].choice, result["d"].note), ("revise", "no secrets in code"))
        self.assertEqual(self.req("POST", f"/api/approvals/{p['id']}", {"choice": "approve"})[0], 409)
        s = self.req("GET", "/api/state")[1]
        self.assertEqual(s["pending"], [])
        self.assertEqual(s["recent"][0]["choice"], "revise")
        self.assertTrue(any("Send back with feedback" in e["text"] for e in s["events"]))

    def test_log_is_redacted_and_confined(self):
        self.pair()
        log = self.cfg.logs_dir / f"agentctl-{time.strftime('%Y%m%d')}.log"
        log.write_text("starting\nusing token=supersecretvalue123 and ghp_" + "B" * 36 + "\nfinished\n")
        r = self.req("GET", "/api/log")[1]
        self.assertIn("finished", r["text"])
        self.assertNotIn("supersecretvalue123", r["text"])
        self.assertNotIn("B" * 36, r["text"])
        self.ctl.set_status("coding", worker_log="/etc/passwd")  # outside the runtime folder: ignored
        self.assertNotIn("root:", self.req("GET", "/api/log")[1]["text"])


class ManualModeEndToEnd(unittest.TestCase):
    """The whole orchestrator in manual mode, with a simulated owner answering each gate."""

    def setUp(self):
        from fakes import FakeGitHub, FakeLLM
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbox-manual-"))
        self.llm = FakeLLM()
        self.gh = FakeGitHub(self.tmp / "remotes")
        os.environ["GH_ADMIN_TOKEN"] = "admin-token"
        os.environ.pop("GH_AGENT_TOKEN", None)
        self.cfg = Config.model_validate({
            "github": {"owner": "acme", "api_url": self.gh.url,
                       "git_url": str(self.tmp / "remotes" / "{owner}" / "{repo}.git")},
            "llm": {"base_url": self.llm.url + "/v1", "retries": 0},
            "sandbox": {"llm_url_in_sandbox": self.llm.url + "/v1", "max_agent_steps": 10},
            "workflow": {"max_attempts_per_task": 4, "ci": {"mode": "advisory"}},
            "budget": {"stop_file": str(self.tmp / "STOP")},
            "guards": {"ollama": "off"},
            "control": {"mode": "manual"},
            "paths": {"data_dir": str(self.tmp / "data")},
        })
        for d in (self.cfg.projects_dir, self.cfg.repos_dir, self.cfg.work_dir, self.cfg.logs_dir, self.cfg.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        setup_logging(self.tmp / "manual.log", verbose=False)
        self.outline = self.tmp / "calc.md"
        self.outline.write_text("# calc\nA calculator library with add and mul.\n")

    def tearDown(self):
        self.llm.close()
        self.gh.close()

    def orch(self):
        from agentbox.loop import Orchestrator
        from fakes import make_fake_sandbox
        o = Orchestrator(self.cfg)
        o.sandbox = make_fake_sandbox(self.cfg)
        o.gh.wait_for_ci = lambda owner, repo, sha, timeout_s: ("success", "fake CI")
        o.ctl.poll_s = 0.01
        o.ctl.notifier = lambda *a, **k: None
        return o

    def worker_prompts(self, title):
        return [r["messages"][1]["content"] for r in self.llm.requests if r.get("tools") and title in r["messages"][1]["content"]]

    def test_owner_steers_every_step(self):
        state = {"plan": 0, "t2_push": 0}

        def answer(a):
            if a.gate == "plan":
                state["plan"] += 1
                return ("revise", "Keep the package name calc.") if state["plan"] == 1 else ("approve", "")
            if a.gate == "task" and "Project skeleton" in a.title:
                return ("approve", "Use only the standard library.")
            if a.gate == "push" and "Add function" in a.title:
                state["t2_push"] += 1
                if state["t2_push"] == 1:  # second attempt passes tests but has no docstring
                    return ("revise", "Code review requested changes: add() needs its docstring.")
            if a.gate == "push" and "unfinished work" in a.title:
                return ("skip", "")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        o.start("new calc.md")
        st = o.new_project(self.outline)
        o.finish()
        owner.halt.set()

        status = {t.id: t.status for t in st.tasks}
        self.assertEqual(status, {"T1": "done", "T2": "done", "T3": "needs_human"})
        gates = [g for g, _, _ in owner.seen]
        self.assertEqual(gates[:2], ["plan", "plan"])                   # revised once, then approved
        for g in ("task", "push", "merge"):
            self.assertIn(g, gates)
        # every merge was approved by the owner, never automatically
        self.assertEqual(sum(1 for g, _, c in owner.seen if g == "merge" and c == "approve"), 2)

        # notes reached the models
        self.assertTrue(any("Keep the package name calc." in json.dumps(r) for r in self.llm.requests))
        self.assertTrue(any("Use only the standard library." in p for p in self.worker_prompts("Project skeleton")))
        t2 = st.task("T2")
        self.assertIn("owner_changes_at_push", [h.outcome for h in t2.history])
        self.assertEqual([h.outcome for h in t2.history][-1], "merged")
        self.assertTrue(any("project owner reviewed the change" in p for p in self.worker_prompts("Add function")))

        # T3's unfinished work was NOT pushed because the owner said so
        bare = self.gh.bare("acme", "calc-lib")
        branches = subprocess.run(["git", "--git-dir", str(bare), "branch"], capture_output=True, text=True).stdout
        self.assertNotIn("multiply", branches)
        repo = self.gh.repos["acme/calc-lib"]
        t3_comments = [c["body"] for c in repo["issues"][st.task("T3").issue]["comments"]]
        self.assertTrue(any("was not pushed" in c for c in t3_comments))

        # the panel's view of it
        events = [e["text"] for e in o.ctl.events(200)]
        self.assertTrue(any("merged" in e for e in events))
        self.assertEqual(o.ctl.status()["phase"], "idle")
        self.assertEqual(o.ctl.approvals(status="pending"), [])

    def test_owner_rewrites_tests_before_they_are_locked(self):
        seen_tests = []

        def answer(a):
            if a.gate == "tests" and "Add function" in a.title:
                seen_tests.append(a.diff)
                if len(seen_tests) == 1:
                    return ("revise", "Expected values must come from the formula.")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        self.assertEqual(len(seen_tests), 2)                            # rewritten once, then locked
        self.assertIn("tests/test_add.py", seen_tests[0])               # the owner saw the test diff
        self.assertNotIn("calc/ops.py", seen_tests[0])                  # non-test changes are not in it
        t2 = st.task("T2")
        self.assertIn("owner_changes_at_tests", [h.outcome for h in t2.history])
        tests_prompts = [p for p in self.worker_prompts("Add function") if "PHASE: TESTS FIRST" in p]
        self.assertTrue(any("Expected values must come from the formula." in p for p in tests_prompts))
        self.assertEqual(t2.status, "done")

    def test_handoff_at_tests_review_skips_implementation(self):
        o = self.orch()
        owner = Owner(o.ctl, lambda a: ("handoff", "I'll fix the expected values myself")
                      if a.gate == "tests" and "Add function" in a.title else ("approve", ""), delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        self.assertEqual(st.task("T2").status, "needs_human")
        self.assertFalse(any("PHASE: IMPLEMENT" in p for p in self.worker_prompts("Add function")))
        self.assertFalse(any("unfinished work" in t for _, t, _ in owner.seen if "T2" in t))
        issue = self.gh.repos["acme/calc-lib"]["issues"][st.task("T2").issue]
        self.assertTrue(any("tests review" in c["body"] and "fix the expected values" in c["body"]
                            for c in issue["comments"]))

    def test_owner_overrules_the_reviewer_and_notes_reach_it(self):
        def answer(a):
            if a.gate == "task" and "Add function" in a.title:
                return ("approve", "Docstrings are optional in this project.")
            if a.gate == "merge" and "reviewer requested changes" in a.title:
                return ("override", "")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        t2 = st.task("T2")
        self.assertEqual(t2.status, "done")
        self.assertIn("owner_override_review", [h.outcome for h in t2.history])
        self.assertIn("owner_note_at_task", [h.outcome for h in t2.history])          # the note is kept with the task
        reviews = [json.dumps(r) for r in self.llm.requests if "senior code reviewer" in json.dumps(r)]
        self.assertTrue(any("Docstrings are optional in this project." in r for r in reviews))
        self.assertTrue(all("Project outline from the owner" in r for r in reviews))
        # the coder was not sent back after the overruled review: its last attempt is the one that merged
        self.assertFalse(any("Address every blocking review issue" in p for p in self.worker_prompts("Add function")))
        bare = self.gh.bare("acme", "calc-lib")
        outline = subprocess.run(["git", "--git-dir", str(bare), "show", "main:OUTLINE.md"], capture_output=True, text=True)
        self.assertIn("A calculator library with add and mul.", outline.stdout)
        agents = subprocess.run(["git", "--git-dir", str(bare), "show", "main:AGENTS.md"], capture_output=True, text=True)
        self.assertIn("OUTLINE.md", agents.stdout)

    def test_no_changes_after_review_asks_and_skips_redundant_push(self):
        def answer(a):
            if a.gate == "task" and a.title.startswith("T2: Add function"):
                return ("approve", "agentbox-test: no changes after review")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        t2 = st.task("T2")
        self.assertEqual(t2.status, "needs_human")
        titles = [t for _, t, _ in owner.seen]
        self.assertTrue(any("made no changes" in t for t in titles))         # the owner was asked each time
        self.assertFalse(any("unfinished work" in t for t in titles))       # the PR already has the work
        comments = [c["body"] for c in self.gh.repos["acme/calc-lib"]["issues"][t2.issue]["comments"]]
        self.assertTrue(any("made no changes after the review" in c and f"PR #{t2.pr}" in c for c in comments))

    def test_no_changes_handoff_stops_the_task(self):
        def answer(a):
            if a.gate == "task" and a.title.startswith("T2: Add function"):
                return ("approve", "agentbox-test: no changes after review")
            if "made no changes" in a.title:
                return ("handoff", "I'll look at it myself")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()
        t2 = st.task("T2")
        self.assertEqual(t2.status, "needs_human")
        self.assertEqual(sum(1 for _, t, _ in owner.seen if "made no changes" in t), 1)

    def test_coder_reports_a_wrong_locked_test_and_owner_has_it_rewritten(self):
        def answer(a):
            if a.gate == "task" and a.title.startswith("T2: Add function"):
                return ("approve", "agentbox-test: report locked test")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        t2 = st.task("T2")
        outcomes = [h.outcome for h in t2.history]
        self.assertIn("locked_test_problem", outcomes)
        self.assertEqual(sum(1 for o_ in outcomes if o_.startswith("tests_written")), 2)   # written, then rewritten
        self.assertTrue(any(g == "tests" and "locked test is wrong" in t for g, t, _ in owner.seen))
        tests_prompts = [p for p in self.worker_prompts("Add function") if "PHASE: TESTS FIRST" in p]
        self.assertTrue(any("expects add(2, 3) == 5 but the spec says 6" in p for p in tests_prompts))
        self.assertEqual(t2.status, "done")

    def test_conftest_cannot_change_how_locked_tests_run(self):
        def answer(a):
            if a.gate == "task" and a.title.startswith("T2: Add function"):
                return ("approve", "agentbox-test: add conftest")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        self.assertEqual(st.task("T2").status, "done")
        bare = self.gh.bare("acme", "calc-lib")
        files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"],
                               capture_output=True, text=True).stdout.split()
        self.assertIn("calc/ops.py", files)
        self.assertNotIn("tests/conftest.py", files)
        self.assertIn("reverted changes to test-runner config", (self.tmp / "manual.log").read_text())
        # Sentinel saw it live: a finding on disk and in the activity feed
        findings = [json.loads(l) for l in (o.cfg.control_dir / "sentinel.jsonl").read_text().splitlines()]
        self.assertIn("runner_config", [f["rule"] for f in findings])
        self.assertTrue(any(e.get("kind") == "sentinel" for e in o.ctl.events(200)))

    def test_continue_adds_outline_to_older_projects(self):
        o = self.orch()
        owner = Owner(o.ctl, lambda a: ("approve", ""), delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        # simulate a project created before OUTLINE.md existed
        clone = o.cfg.repos_dir / st.repo
        for args in (["checkout", "-q", "main"], ["reset", "-q", "--hard", "origin/main"], ["rm", "-q", "OUTLINE.md"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "old project"], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", *args], cwd=clone, check=True, capture_output=True)
        o2 = self.orch()
        owner.ctl = o2.ctl
        o2.continue_project(st.repo)
        owner.halt.set()
        bare = self.gh.bare("acme", "calc-lib")
        outline = subprocess.run(["git", "--git-dir", str(bare), "show", "main:OUTLINE.md"], capture_output=True, text=True)
        self.assertIn("A calculator library with add and mul.", outline.stdout)
        self.assertTrue(any(g == "push" and "OUTLINE.md" in t for g, t, _ in owner.seen))

    def test_cancel_plan_creates_nothing(self):
        o = self.orch()
        owner = Owner(o.ctl, lambda a: ("cancel", ""), delay=0.0)
        owner.start()
        self.assertIsNone(o.new_project(self.outline))
        owner.halt.set()
        self.assertEqual(self.gh.repos, {})

    def test_skip_task_is_not_a_failure(self):
        o = self.orch()
        owner = Owner(o.ctl, lambda a: ("skip", "later") if a.gate == "task" else ("approve", ""), delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()
        self.assertEqual(st.task("T1").status, "needs_human")
        self.assertEqual(o.consecutive_failures, 0)
        self.assertEqual(st.runs[-1].failed, [])
        self.assertIn("need a human", st.runs[-1].stop_reason)
        issue = self.gh.repos["acme/calc-lib"]["issues"][st.task("T1").issue]
        self.assertIn("agent:needs-human", issue["labels"])
        self.assertTrue(any("Skipped by the project owner" in c["body"] for c in issue["comments"]))

    def _tests_phase_prompts(self, title):
        return [p for p in self.worker_prompts(title) if "PHASE: TESTS FIRST" in p]

    def test_locked_tests_survive_a_run_that_stops_after_the_tests_phase(self):
        stop = self.cfg.budget.stop_path
        o = self.orch()
        tests_first = o._tests_first

        def tests_then_stop(*a, **k):   # like the budget ending right after the tests phase (backlog 27)
            locked = tests_first(*a, **k)
            stop.write_text("stop right after locking the tests\n")
            return locked
        o._tests_first = tests_then_stop
        owner = Owner(o.ctl, lambda a: ("approve", ""), delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        t2 = st.task("T2")
        self.assertIn("STOP requested", st.runs[-1].stop_reason)
        self.assertEqual(t2.status, "todo")
        self.assertEqual(sorted(t2.locked_tests), ["tests/test_add.py"])
        written = len(self._tests_phase_prompts("Add function"))
        self.assertGreater(written, 0)

        stop.unlink()
        o2 = self.orch()
        owner.ctl = o2.ctl
        st = o2.continue_project(st.repo)
        owner.halt.set()
        t2 = st.task("T2")
        self.assertEqual(t2.status, "done")
        self.assertEqual(len(self._tests_phase_prompts("Add function")), written)   # not written again
        self.assertIn("tests_reused", [h.outcome for h in t2.history])
        self.assertEqual(t2.locked_tests, {})                                      # cleared once merged
        starts = [t for g, t, _ in owner.seen if g == "task" and "Add function" in t]
        self.assertEqual(len(starts), 2)
        asked = [a for a in o2.ctl.approvals(limit=200) if a.gate == "task" and "Add function" in a.title]
        self.assertTrue(any("locked in an earlier run" in " ".join(a.summary) for a in asked))
        self.assertTrue(any(c["id"] == "fresh" for a in asked for c in a.choices))
        # the reused tests still bind the coder: the cheating first attempt was reverted as before
        bare = self.gh.bare("acme", "calc-lib")
        test_add = subprocess.run(["git", "--git-dir", str(bare), "show", "main:tests/test_add.py"],
                                  capture_output=True, text=True).stdout
        self.assertIn("assertEqual(add(2, 3), 5)", test_add)

    def test_issue_edits_reach_a_task_that_starts_later_in_the_run(self):
        def answer(a):
            if a.gate == "task" and "Project skeleton" in a.title:   # T3 waits behind T1 and T2
                repo = self.gh.repos["acme/calc-lib"]
                t3 = next(i for i in repo["issues"].values() if i["title"].startswith("T3:"))
                t3["body"] = t3["body"].replace("- [ ] mul(0, 5) == 0", "- [ ] mul(0, 5) == 0\n- [ ] mul(3, 3) == 9 (edited)")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()
        self.assertIn("mul(3, 3) == 9 (edited)", st.task("T3").acceptance_criteria)
        self.assertTrue(any("mul(3, 3) == 9 (edited)" in p for p in self.worker_prompts("Multiply function")))

        # a task that is blocked at run start also takes the edited issue (reconcile)
        t3 = st.task("T3")
        t3.status = "blocked"
        repo = self.gh.repos["acme/calc-lib"]
        repo["issues"][t3.issue]["state"] = "open"
        repo["issues"][t3.issue]["body"] = repo["issues"][t3.issue]["body"].replace("(edited)", "(edited twice)")
        o.reconcile(st)
        self.assertIn("mul(3, 3) == 9 (edited twice)", st.task("T3").acceptance_criteria)

    def test_outline_checks_catch_a_wrong_locked_value(self):
        """Runs 1-2: a wrong value is locked in the tests and the code is bent to match it. The tests
        pass; the outline check does not, the coder reports the locked test, the tests are rewritten."""
        def answer(a):
            if a.gate == "task" and a.title.startswith("T2: Add function"):
                return ("approve", "agentbox-test: wrong locked value")
            if a.gate == "push" and "unfinished work" in a.title:
                return ("skip", "")
            return ("approve", "")

        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        st = o.new_project(self.outline)
        owner.halt.set()

        # planning wrote the checks; the one with a computed number was dropped, and the owner saw them
        from agentbox.oracle import load_checks
        checks = load_checks(self.cfg.projects_dir / "calc-lib")
        self.assertEqual([c.id for c in checks], ["C1", "C2", "C3"])
        plan_q = next(a for a in o.ctl.approvals(limit=200) if a.gate == "plan")
        self.assertTrue(any(k.startswith("Outline checks (3)") for k in plan_q.details))
        self.assertIn("Checks dropped (invalid after repair)", plan_q.details)

        t2 = st.task("T2")
        self.assertEqual(t2.status, "done")
        outcomes = [h.outcome for h in t2.history]
        self.assertLess(outcomes.index("checks_failed"), outcomes.index("locked_test_problem"))
        self.assertIn("C1", next(h.note for h in t2.history if h.outcome == "checks_failed"))
        self.assertEqual(sum(1 for x in outcomes if x.startswith("tests_written")), 2)     # written, then rewritten
        # the coder saw its checks from the first attempt on
        self.assertTrue(all("Outline checks for this task" in p for p in self.worker_prompts("Add function")))
        bare = self.gh.bare("acme", "calc-lib")
        test_add = subprocess.run(["git", "--git-dir", str(bare), "show", "main:tests/test_add.py"],
                                  capture_output=True, text=True).stdout
        self.assertIn("assertEqual(add(2, 3), 5)", test_add)

        # reported at the push gate, on the PR and in the merge record
        push = [a for a in o.ctl.approvals(limit=200) if a.gate == "push" and a.title.startswith("T2")]
        self.assertTrue(any("Outline checks: 2 pass, 0 fail, 1 for later tasks" in " ".join(a.summary) for a in push))
        self.assertTrue(any("Mutation check: tests caught 1/1" in " ".join(a.summary) for a in push))
        pr_comments = [c["body"] for c in self.gh.repos["acme/calc-lib"]["issues"][t2.pr]["comments"]]
        self.assertTrue(any("### Outline checks and mutation check" in c for c in pr_comments))
        merged = next(h.note for h in t2.history if h.outcome == "merged")
        self.assertIn("checks 2/2 · mutants caught 1/1", merged)

    def _auto(self, **control):
        self.cfg.control.mode = "auto"
        for k, v in control.items():
            setattr(self.cfg.control, k, v)

    def test_night_merge_question_times_out_and_leaves_the_pr(self):
        from agentbox.config import NightConfig
        now = time.localtime()
        start, end = f"{(now.tm_hour - 1) % 24:02d}:00", f"{(now.tm_hour + 2) % 24:02d}:00"
        self._auto(always_ask=["merge"], night=NightConfig(hours=f"{start}-{end}", question_timeout_min=0.002))
        o = self.orch()
        st = o.new_project(self.outline)   # nobody answers: every merge question times out
        t1 = st.task("T1")
        self.assertEqual(t1.status, "needs_human")
        self.assertIn("timeout_handoff_at_merge", [h.outcome for h in t1.history])
        self.assertNotIn("owner", [h.phase for h in t1.history])          # not an owner note
        self.assertEqual(o.consecutive_failures, 0)                         # not a failure streak
        self.assertEqual(st.runs[-1].failed, ["T1"])
        repo = self.gh.repos["acme/calc-lib"]
        self.assertEqual(repo["prs"][t1.pr]["state"], "open")               # the PR waits for you
        comments = [c["body"] for c in repo["issues"][t1.issue]["comments"]]
        self.assertTrue(any("Nobody answered the Merge question" in c and f"PR #{t1.pr} stays open" in c for c in comments))
        self.assertIn("blocked behind", st.runs[-1].stop_reason)           # T2/T3 depend on T1

    def test_auto_merge_needs_ci_to_pass(self):
        self._auto(always_ask=[])
        o = self.orch()
        o._wait_ci = lambda st, sha: ("timeout", "CI did not finish in time")
        st = o.new_project(self.outline)
        t1 = st.task("T1")
        self.assertEqual(t1.status, "needs_human")
        self.assertIn("ci_timeout", [h.outcome for h in t1.history])
        self.assertIn("not merged without you", t1.history[-1].note)
        self.assertEqual(self.gh.repos["acme/calc-lib"]["prs"][t1.pr]["state"], "open")

        # the owner can still merge it (merge asked), and the old advisory behaviour is one switch away
        self.cfg.workflow.ci.require_for_auto_merge = False
        o2 = self.orch()
        o2._wait_ci = lambda st, sha: ("timeout", "CI did not finish in time")
        st2 = o2.new_project(self.outline, name="calc-two")
        self.assertEqual(st2.task("T1").status, "done")

    def test_digest_summarises_the_command(self):
        from unittest import mock
        from agentbox.digest import latest_digest

        def answer(a):
            return ("skip", "") if a.gate == "push" and "unfinished work" in a.title else ("approve", "")

        self.cfg.control.public_url = "https://laptop.tail1234.ts.net"
        self.cfg.notify.ntfy_url = "https://ntfy.example/topic"
        o = self.orch()
        owner = Owner(o.ctl, answer, delay=0.0)
        owner.start()
        o.start("new calc.md")
        st = o.new_project(self.outline)
        with mock.patch("agentbox.loop.notify") as sent:
            o.finish()
        owner.halt.set()
        self.assertEqual({t.id: t.status for t in st.tasks}, {"T1": "done", "T2": "done", "T3": "needs_human"})

        text = latest_digest(self.cfg).read_text()
        self.assertIn("**2 merged, 1 left for you**", text)
        merged = text.split("## Merged: check these")[1].split("##")[0]
        t2 = st.task("T2")
        self.assertIn(f"[PR #{t2.pr}](https://github.com/acme/calc-lib/pull/{t2.pr})", merged)
        self.assertIn("merged by you", merged)
        left = text.split("## Left for you")[1].split("##")[0]
        self.assertIn("calc-lib T3", left)
        self.assertIn("## Time per task", text)
        self.assertIn("To undo a merge", text)
        self.assertEqual(set(st.runs[-1].task_minutes), {"T1", "T2", "T3"})
        self.assertGreater(st.runs[-1].waited_minutes, -0.1)
        self.assertEqual(st.task("T3").history[-1].outcome, "needs_human")      # the reason is kept

        self.assertEqual(sent.call_count, 1)                                    # one notification per command
        args, kw = sent.call_args
        self.assertEqual(args[1], "agentbox digest")
        self.assertIn("2 merged, 1 left for you", args[2])
        self.assertIn("✅ calc-lib T2", args[2])
        self.assertEqual(kw["click"], "https://laptop.tail1234.ts.net/#digest")

    def test_wall_clock_limit(self):
        o = self.orch()
        o.check_budget()                                    # no limit set
        self.cfg.budget.max_wall_hours = 1                  # one attempt may take 50 + 15 min: none fits
        with self.assertRaises(StopRun) as e:
            o.check_budget()
        self.assertIn("not enough time left for another attempt", str(e.exception))
        self.cfg.budget.max_wall_hours = 2
        o.check_budget()
        o.t0 -= 2 * 3600                                    # also ends a wait for your answer
        with self.assertRaises(StopRun):
            o.stop_check()

    def test_new_reuses_the_plan_of_a_plan_only_run(self):
        from agentbox.loop import Orchestrator
        o = self.orch()
        o.new_project(self.outline, plan_only=True)
        plans = self.llm.plan_calls
        plan_dir = self.cfg.projects_dir / "calc-lib"
        self.assertTrue((plan_dir / "plan.json").is_file())

        other = self.tmp / "other.md"
        other.write_text("# other\nSomething else.\n")
        with self.assertRaises(ValueError):
            Orchestrator.load_plan(plan_dir, other.read_text())

        owner = Owner(o.ctl, lambda a: ("approve", ""), delay=0.0)
        owner.start()
        st = o.new_project(self.outline, use_plan=plan_dir)
        owner.halt.set()
        self.assertEqual(self.llm.plan_calls, plans)                       # no new planning
        self.assertEqual([t.id for t in st.tasks], ["T1", "T2", "T3"])
        self.assertTrue(any("Reusing the plan" in e["text"] for e in o.ctl.events(200)))


if __name__ == "__main__":
    unittest.main()
