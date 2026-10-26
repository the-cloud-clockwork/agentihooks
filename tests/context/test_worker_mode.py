import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import hooks.context.inbox_delivery as delivery
import hooks.context.ledger_decision as decision
import hooks.context.swarm_refocus as refocus
from hooks import hook_manager
from hooks.context import broadcast, swarm_ledger, swarm_wait_nudge, worker_mode
from hooks.context.retry_breaker import _set_state
from hooks.hook_manager import BlockAction
from hooks.targets.emitter import flush
from scripts.headless import profile_run
from scripts.inbox.store import InboxStore
from scripts.swarm_ledger import ledger_hook
from scripts.swarm_v2 import broadcast_bridge
from scripts.swarm_v2 import broadcasts as fleet
from tests.swarm_ledger import legacy_page

ROOT = Path(__file__).resolve().parents[2]
PLAN_ACCEPT = json.loads((ROOT / "tests" / "fixtures" / "ledger_decision_payloads.json").read_text(encoding="utf-8"))[
    "claude_plan_accept"
]
LEDGER = {
    "title": "Rig grade swarm",
    "overview": "Borrow what OpenRig does that we lack.",
    "phases": [{"id": "p1", "title": "Worker", "description": "Headless runs."}],
    "tasks": [{"id": "w1", "phase": "p1", "title": "Quiet hooks", "description": "Hooks in worker mode."}],
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    ledgers = tmp_path / "ledgers"
    (ledgers / ".sessions").mkdir(parents=True)
    legacy_page.store(ledgers, "rig", LEDGER)
    monkeypatch.setenv("LEDGER_DIR", str(ledgers))
    monkeypatch.setattr(refocus, "STATE_DIR", tmp_path / "refocus-state")
    monkeypatch.setattr(decision, "STATE_DIR", tmp_path / "decision-state")
    for name in ("AGENTIHOOKS_TARGET", "AGENTIHOOKS_REFOCUS_EVERY", "AGENTIHOOKS_REFOCUS_MAX_CHARS", worker_mode.FLAG):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "w1")
    return ledgers


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv(worker_mode.FLAG, "1")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    monkeypatch.setattr(delivery, "connect", lambda environ=None: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    return store


@pytest.fixture
def broadcasts(tmp_path):
    with (
        patch.object(broadcast, "_broadcast_path", return_value=tmp_path / "broadcast.json"),
        patch.object(broadcast, "BROADCAST_ENABLED", True),
        patch.object(broadcast, "BROADCAST_CRITICAL_ON_PRETOOL", True),
    ):
        broadcast.create_broadcast("Fleet incident", severity="critical")
        yield


def _pre(capsys, session="s1"):
    hook_manager.on_pre_tool_use(
        {"session_id": session, "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"}
    )
    flush("PreToolUse")
    return capsys.readouterr().out


def _prompt(capsys, session="s1"):
    hook_manager.on_user_prompt_submit({"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    flush("UserPromptSubmit")
    return capsys.readouterr().out


def _plan_accept(capsys):
    hook_manager.on_post_tool_use(dict(PLAN_ACCEPT))
    flush("PostToolUse")
    return capsys.readouterr().out


def test_the_flag_is_the_one_a_headless_profile_run_sets():
    assert worker_mode.FLAG == profile_run.WORKER_RUN
    assert worker_mode.active({worker_mode.FLAG: "1"})
    assert not worker_mode.active({})
    assert not worker_mode.active({worker_mode.FLAG: "0"})
    assert not worker_mode.active({worker_mode.FLAG: "true"})


def test_a_worker_run_is_shown_no_inbox_item_and_leaves_it_pending(store, worker, capsys):
    item = store.send("alice", "bob", "review my branch")
    assert "review my branch" not in _pre(capsys)
    assert store.get(item.id).state == "pending"


def test_a_worker_run_confirms_no_inbox_item(store, worker):
    with patch.object(store, "confirm_shown") as confirm:
        delivery.confirm_shown("s1")
    confirm.assert_not_called()


def test_an_interactive_session_still_gets_its_inbox_item(store, capsys):
    item = store.send("alice", "bob", "review my branch")
    assert "review my branch" in _pre(capsys)
    assert store.get(item.id).state == "delivered"


def test_a_worker_run_gets_no_refocus_block(worker, capsys):
    assert refocus.refocus_context("s1", "prompt") == ""
    assert "SWARM REFOCUS" not in _prompt(capsys)


def test_an_interactive_bound_session_still_gets_its_refocus_block():
    assert "SWARM REFOCUS" in refocus.refocus_context("s1", "prompt")


def test_a_worker_run_gets_no_plan_mode_directive(isolated, worker, capsys):
    (isolated / ".sessions" / f"{PLAN_ACCEPT['session_id']}.json").write_text(
        json.dumps({"slug": "rig", "name": "master@323133-0001"}), encoding="utf-8"
    )
    assert decision.directive(dict(PLAN_ACCEPT)) == ""
    assert "LEDGER DECISION" not in _plan_accept(capsys)


def test_an_interactive_bound_session_still_gets_the_plan_mode_directive(isolated):
    (isolated / ".sessions" / f"{PLAN_ACCEPT['session_id']}.json").write_text(
        json.dumps({"slug": "rig", "name": "master@323133-0001"}), encoding="utf-8"
    )
    assert "LEDGER DECISION" in decision.directive(dict(PLAN_ACCEPT))


def test_a_worker_run_gets_no_broadcast(broadcasts, worker, capsys):
    broadcast.check_and_inject_broadcasts("s1")
    assert "Fleet incident" not in capsys.readouterr().out
    assert broadcast.get_pretool_context("s1") is None
    assert broadcast.get_posttool_context("s1") is None
    assert broadcast.get_broadcast_context("s1", [m["id"] for m in broadcast.list_broadcasts()]) is None


def test_an_interactive_session_still_gets_the_broadcast(broadcasts):
    assert "Fleet incident" in broadcast.get_pretool_context("s1")


def test_an_interactive_session_still_gets_the_brain_refresh_broadcast(broadcasts):
    assert "Fleet incident" in broadcast.get_broadcast_context("s1", [m["id"] for m in broadcast.list_broadcasts()])


def test_a_worker_run_keeps_the_credential_guard(worker):
    with pytest.raises(BlockAction):
        hook_manager.on_pre_tool_use({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "cat .env"}})


def test_a_worker_run_keeps_the_secret_scan(worker):
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    payload = {
        "session_id": "s1",
        "tool_name": "Write",
        "tool_input": {"file_path": "/x/a.py", "content": f"k = '{key}'"},
    }
    with pytest.raises(BlockAction):
        hook_manager.on_pre_tool_use(payload)


def test_a_worker_run_keeps_the_retry_breaker(worker):
    _set_state("s-breaker", "bash:npm", {"count": 10, "last_error_key": "enoent", "last_input": "npm install"})
    with pytest.raises(BlockAction, match="(?i)hard stop"):
        hook_manager.on_pre_tool_use(
            {"session_id": "s-breaker", "tool_name": "Bash", "tool_input": {"command": "npm install"}}
        )


def test_a_worker_run_skips_the_swarm_ledger_hook(worker, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(ledger_hook, "dispatch", dispatch)
    assert swarm_ledger.run({"session_id": "s1", "hook_event_name": "Stop"}) == ([], None)
    dispatch.assert_not_called()


def test_an_interactive_session_still_runs_the_swarm_ledger_hook(monkeypatch):
    def stop(payload):
        print(json.dumps({"decision": "block", "reason": "events unhandled"}))

    monkeypatch.setattr(ledger_hook, "dispatch", stop)
    assert swarm_ledger.run({"session_id": "s1", "hook_event_name": "Stop"}) == ([], "events unhandled")


WAIT_ENV = {
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_TASK": "w1",
    "AGENTIHOOKS_AGENT_NAME": "engineer@323133-0001",
}
SLEEP = {"tool_name": "Bash", "tool_input": {"command": "sleep 60"}}


def test_a_worker_run_gets_no_wait_nudge(monkeypatch):
    declared = Mock(return_value=None)
    monkeypatch.setattr(swarm_wait_nudge, "declared_wait", declared)
    assert swarm_wait_nudge.nudge(SLEEP, {**WAIT_ENV, worker_mode.FLAG: "1"}) == ""
    declared.assert_not_called()


def test_an_interactive_worker_lane_session_still_gets_the_wait_nudge(monkeypatch):
    monkeypatch.setattr(swarm_wait_nudge, "declared_wait", Mock(return_value=None))
    assert "SWARM WAIT" in swarm_wait_nudge.nudge(SLEEP, WAIT_ENV)


def test_a_worker_run_claims_no_fleet_broadcast(monkeypatch):
    sync = Mock(return_value=3)
    monkeypatch.setattr(fleet, "sync_local", sync)
    environ = {fleet.FLAG: "1", broadcast_bridge.GRANT_FILE: "/x/grant", broadcast_bridge.API_URL: "http://x"}
    assert broadcast_bridge.claim("s1", ["brain"], {**environ, worker_mode.FLAG: "1"}) == 0
    sync.assert_not_called()
