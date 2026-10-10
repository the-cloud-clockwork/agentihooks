import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "hook-2026-01-01"
SID = "sess-1"
JOIN = f"python3 {SCRIPTS}/ledger.py --slug {SLUG} --as boss join --role orchestrator"


def setUpModule():
    global CLOSED_PORT, RUNNER, RUNNER_ERR
    CLOSED_PORT = socket.socket()
    CLOSED_PORT.bind(("127.0.0.1", 0))
    RUNNER_ERR = tempfile.TemporaryFile()
    RUNNER = subprocess.Popen(
        [sys.executable, "-c", RUN_HOOKS, str(SCRIPTS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=RUNNER_ERR,
        env=hook_env(),
        text=True,
    )


def tearDownModule():
    RUNNER.stdin.close()
    RUNNER.wait(timeout=20)
    RUNNER_ERR.close()
    CLOSED_PORT.close()


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
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
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


RUN_HOOKS = """
import io, json, os, sys
sys.path.insert(0, sys.argv[1])
import ledger_hook
import hooks._redis
requests, out = sys.stdin, sys.stdout
for line in requests:
    request = json.loads(line)
    os.environ.clear()
    os.environ.update(request["env"])
    hooks._redis._redis_client, hooks._redis._redis_checked = None, False
    sys.stdin, sys.stdout = io.StringIO(json.dumps(request["payload"])), io.StringIO()
    code = ledger_hook.main()
    text = sys.stdout.getvalue()
    out.write(json.dumps([code, json.loads(text) if text.strip() else None]) + "\\n")
    out.flush()
"""


def hook_env():
    return {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": str(CLOSED_PORT.getsockname()[1])}


def hook(event, **fields):
    RUNNER.stdin.write(
        json.dumps({"env": hook_env(), "payload": {"session_id": SID, "hook_event_name": event, **fields}}) + "\n"
    )
    RUNNER.stdin.flush()
    line = RUNNER.stdout.readline()
    RUNNER_ERR.seek(0)
    assert line, RUNNER_ERR.read().decode()
    code, result = json.loads(line)
    assert code == 0
    return result


def hooks(*payloads):
    return [hook(**payload) for payload in payloads]


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

    def test_session_start_brings_the_server_up(self):
        from tests import ledger_guard

        hold = ledger_guard.reserve_port()
        self.addCleanup(hold.close)
        port = hold.getsockname()[1]
        env = {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": str(port), "LEDGER_AUTOSTART": "1"}
        payload = json.dumps({"session_id": "fresh", "hook_event_name": "SessionStart"})
        subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger_hook.py")],
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("the ledger server did not come up")
        finally:
            subprocess.run(
                [sys.executable, str(SCRIPTS / "ledger_server.py"), "--stop"], env=env, capture_output=True, timeout=20
            )

    def test_owed_events_name_the_agentihooks_command(self):
        ask("where are we", 7)
        text = hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"agentihooks ledger --slug {SLUG} --as boss ack", text)
        self.assertNotIn("python3", text)

    def test_only_the_agent_cli_counts_as_a_ledger_command(self):
        import ledger_hook

        self.assertTrue(ledger_hook.is_ledger_cli(["python3", "/x/swarm_ledger/ledger.py", "say"]))
        self.assertTrue(ledger_hook.is_ledger_cli(["agentihooks", "ledger", "say"]))
        self.assertFalse(ledger_hook.is_ledger_cli(["pytest", "tests/test_ledger.py", "-k", "edit"]))
        self.assertFalse(ledger_hook.is_ledger_cli(["python3", "watch_ledger.py", "s"]))

    def test_agentihooks_join_command_binds_the_session(self):
        (core.LEDGER_DIR / ".sessions" / f"{SID}.json").unlink()
        bash(f"agentihooks ledger --slug {SLUG} --as boss join --role orchestrator", '{"joined": "boss"}')
        session = json.loads((core.LEDGER_DIR / ".sessions" / f"{SID}.json").read_text())
        self.assertEqual((session["slug"], session["name"]), (SLUG, "boss"))

    def test_creating_a_small_ledger_binds_the_creator_as_its_worker(self):
        (core.LEDGER_DIR / ".sessions" / f"{SID}.json").unlink()
        created = json.dumps({"slug": SLUG, "created": True, "size": "small", "joined": "worker"})
        bash("agentihooks ledger new --content c.json --plan p.md --as worker", f"{created}\nLedger page: x")
        session = json.loads((core.LEDGER_DIR / ".sessions" / f"{SID}.json").read_text())
        self.assertEqual((session["slug"], session["name"], session["role"]), (SLUG, "worker", "member"))

    def test_the_old_environment_names_do_not_bind_a_session(self):
        import ledger_hook

        old = {"PLAN_LEDGER": SLUG, "PLAN_LEDGER_AS": "boss", "PLAN_LEDGER_ROLE": "orchestrator"}
        with unittest.mock.patch.dict(os.environ, old):
            ledger_hook.bind({"hook_event_name": "SessionStart"}, "old-env")
        self.assertFalse((core.LEDGER_DIR / ".sessions" / "old-env.json").exists())
        self.assertIsNone(
            ledger_hook.join_from_command(f"PLAN_LEDGER={SLUG} PLAN_LEDGER_AS=boss agentihooks ledger join")
        )

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

    @pytest.mark.wall_clock
    def test_block_budget_then_allow(self):
        ask("still there", 3)
        start = time.monotonic()
        results = [hook("Stop") for _ in range(4)]
        self.assertEqual([bool(r) for r in results], [True, True, True, False])
        self.assertLess(time.monotonic() - start, 2)

    def test_work_without_recording_blocks_stop(self):
        ls = {"event": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        say = {**ls, "tool_input": {"command": f"python3 {SCRIPTS}/ledger.py --slug {SLUG} --as boss say hi"}}
        stop = {"event": "Stop"}
        results = hooks(*[ls] * 10, stop, say, stop)
        self.assertEqual(len(results), 13)
        self.assertIn("tool calls since you last recorded", results[10]["reason"])
        self.assertIsNone(results[12])

    def test_an_orchestrator_without_a_watcher_stops_freely(self):
        core.watch_path(SLUG, "boss").unlink()
        self.assertIsNone(hook("Stop"))
        old = time.time() - 300
        core.watch_path(SLUG, "boss").touch()
        os.utime(core.watch_path(SLUG, "boss"), (old, old))
        self.assertIsNone(hook("Stop"))

    def test_the_old_kill_switch_no_longer_disables_the_hook(self):
        ask("pending", 4)
        os.environ["PLAN_LEDGER_HOOKS"] = "off"
        try:
            self.assertEqual(hook("Stop")["decision"], "block")
        finally:
            del os.environ["PLAN_LEDGER_HOOKS"]

    def test_closed_ledger_allows(self):
        make_ledger(done=True)
        core.sync(SLUG, ops=[{"op": "join", "id": "j2", "by": "boss", "role": "orchestrator"}])
        ask("pending", 6)
        self.assertIsNone(hook("Stop"))

    def test_unreadable_ledger_fails_open(self):
        from scripts.swarm_ledger.repository.sqlite import DATABASE

        ask("pending", 5)
        db = core.LEDGER_DIR / DATABASE
        original = db.read_bytes()
        db.write_bytes(b"not a database")
        try:
            self.assertIsNone(hook("Stop"))
        finally:
            db.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
