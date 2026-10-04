import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "plan_ledger"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("LEDGER_DIR", tempfile.mkdtemp(prefix="ledger-hook-test-"))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "hook-2026-01-01"
SID = "sess-1"
JOIN = f"python3 {SCRIPTS}/ledger.py --slug {SLUG} --as boss join --role orchestrator"


def make_ledger(done=False):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    if done:
        pid = state["phases"][0]["id"]
        core.sync(
            SLUG,
            ops=[
                {"op": "join", "id": "j0", "by": "x"},
                {"op": "set", "id": "s0", "by": "x", "path": f"phases/{pid}/done", "value": True},
            ],
        )


def hook(event, **fields):
    payload = {"session_id": SID, "hook_event_name": event, **fields}
    env = {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": "1"}
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / "ledger_hook.py")],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout) if run.stdout.strip() else None


def bash(command, output=""):
    return hook("PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_response={"stdout": output})


def ask(text, n):
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": f"q-{n}", "text": text}])


class Gate(unittest.TestCase):
    def setUp(self):
        make_ledger()
        core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}])
        sessions = core.LEDGER_DIR / ".sessions"
        for stale in sessions.glob("*") if sessions.exists() else []:
            stale.unlink()
        bash(JOIN, '{"joined": "boss"}')
        core.watch_path(SLUG, "boss").parent.mkdir(parents=True, exist_ok=True)
        core.watch_path(SLUG, "boss").touch()

    def test_unbound_session_is_silent(self):
        self.assertIsNone(hook("Stop", session_id="other"))

    def test_join_command_binds_the_session(self):
        session = json.loads((core.LEDGER_DIR / ".sessions" / f"{SID}.json").read_text())
        self.assertEqual((session["slug"], session["name"], session["role"]), (SLUG, "boss", "orchestrator"))

    def test_only_the_agent_cli_counts_as_a_ledger_command(self):
        import ledger_hook

        self.assertTrue(ledger_hook.is_ledger_cli(["python3", "/x/plan_ledger/ledger.py", "say"]))
        self.assertTrue(ledger_hook.is_ledger_cli(["agentihooks", "ledger", "say"]))
        self.assertFalse(ledger_hook.is_ledger_cli(["pytest", "tests/test_ledger.py", "-k", "edit"]))
        self.assertFalse(ledger_hook.is_ledger_cli(["python3", "watch_ledger.py", "s"]))

    def test_agentihooks_join_command_binds_the_session(self):
        (core.LEDGER_DIR / ".sessions" / f"{SID}.json").unlink()
        bash(f"agentihooks ledger --slug {SLUG} --as boss join --role orchestrator", '{"joined": "boss"}')
        session = json.loads((core.LEDGER_DIR / ".sessions" / f"{SID}.json").read_text())
        self.assertEqual((session["slug"], session["name"]), (SLUG, "boss"))

    def test_operator_chat_reaches_the_prompt_and_blocks_stop(self):
        ask("where are we", 1)
        prompt = hook("UserPromptSubmit")
        self.assertIn("where are we", prompt["hookSpecificOutput"]["additionalContext"])
        stop = hook("Stop")
        self.assertEqual(stop["decision"], "block")
        self.assertIn("unhandled", stop["reason"])

    def test_ack_opens_the_gate(self):
        ask("ping", 2)
        rev = core.sync(SLUG)[0]["_meta"]["rev"]
        core.sync(SLUG, ops=[{"op": "ack", "id": "a1", "by": "boss", "rev": rev}])
        bash(f"python3 {SCRIPTS}/ledger.py --slug {SLUG} --as boss ack")
        self.assertIsNone(hook("Stop"))

    def test_block_budget_then_allow(self):
        ask("still there", 3)
        results = [hook("Stop") for _ in range(4)]
        self.assertEqual([bool(r) for r in results], [True, True, True, False])

    def test_work_without_recording_blocks_stop(self):
        for _ in range(10):
            bash("ls")
        self.assertIn("tool calls since you last recorded", hook("Stop")["reason"])
        bash(f"python3 {SCRIPTS}/ledger.py --slug {SLUG} --as boss say hi")
        self.assertIsNone(hook("Stop"))

    def test_orchestrator_needs_a_live_watcher(self):
        core.watch_path(SLUG, "boss").unlink()
        self.assertIn("watcher is not running", hook("Stop")["reason"])
        old = time.time() - 300
        core.watch_path(SLUG, "boss").touch()
        os.utime(core.watch_path(SLUG, "boss"), (old, old))
        self.assertIn("watcher is not running", hook("Stop")["reason"])

    def test_closed_ledger_and_kill_switch_allow(self):
        ask("pending", 4)
        os.environ["PLAN_LEDGER_HOOKS"] = "off"
        try:
            self.assertIsNone(hook("Stop"))
        finally:
            del os.environ["PLAN_LEDGER_HOOKS"]
        make_ledger(done=True)
        core.sync(SLUG, ops=[{"op": "join", "id": "j2", "by": "boss", "role": "orchestrator"}])
        self.assertIsNone(hook("Stop"))

    def test_unreadable_ledger_fails_open(self):
        ask("pending", 5)
        core.paths(SLUG)[1].write_text("{not json", encoding="utf-8")
        self.assertIsNone(hook("Stop"))


if __name__ == "__main__":
    unittest.main()
