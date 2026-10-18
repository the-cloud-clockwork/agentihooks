from pathlib import Path

import pytest

from scripts.swarm.delivery import HerdrMessenger
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord

CAPTURE = Path("tests/fixtures/swarm/codex-content-refusal.txt").read_text()


from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, outage
from tests.swarm.test_tick import FakeLedger
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.xdist_group("fakeredis")


from dataclasses import replace

from hooks.context import swarm_heartbeat
from scripts.handoff import transfers
from scripts.swarm.tick import tick
from tests.swarm.test_outage_resume import three_owners
from tests.swarm.test_outage_seat_release import executing


@pytest.mark.parametrize("component", [HerdrRuntime, HerdrMessenger])
def test_completed_codex_refusal_is_idle_for_the_normal_inbox_wake(component, tmp_path):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "blocked"}
        return {"text": CAPTURE}

    runtime = component(herdr=herdr)
    status = runtime.status(agent) if component is HerdrRuntime else runtime.agent_status(agent)
    assert status == "idle"


def test_refusal_wakes_pending_mail_in_one_tick_without_an_outage(store):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")
    inbox = InboxStore(store.redis)
    item = inbox.send("master", agent.name, "Read the failed checks and continue")
    prompts = []

    def herdr(args):
        if args[:2] == ["agent", "get"]:
            return {"name": agent.name, "pane_id": "p1", "agent_status": "blocked"}
        if args[:2] == ["agent", "prompt"]:
            prompts.append(args)
            return {}
        return {"text": CAPTURE}

    wake.wake_pass(inbox, "sw", [agent], HerdrMessenger(herdr), FakeLedger([]), item.created_at + 1, 300_000)
    assert prompts == [["agent", "prompt", "p1", "[swarm delivery] " + wake.WAKE_TEXT]]
    assert not outage.open_records("sw")


@pytest.mark.parametrize(
    "state,typed,quiet", [("working", "", False), ("blocked", "stop", False), ("blocked", "", True)]
)
def test_refusal_wake_respects_busy_typed_and_operator_quiet_guards(store, state, typed, quiet):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")
    inbox = InboxStore(store.redis)
    item = inbox.send("master", agent.name, "Continue")
    prompts = []
    capture = CAPTURE.replace("› Ask Codex to do anything", "› " + typed) if typed else CAPTURE

    def herdr(args):
        if args[:2] == ["agent", "get"]:
            return {"name": agent.name, "pane_id": "p1", "agent_status": state}
        if args[:2] == ["agent", "prompt"]:
            prompts.append(args)
        return {"text": capture}

    if quiet:
        idle.prompted(store.redis, "sw", agent.name, item.created_at)
    wake.wake_pass(inbox, "sw", [agent], HerdrMessenger(herdr), FakeLedger([]), item.created_at + 1, 300_000)
    assert prompts == []


@pytest.mark.parametrize("expanded", [False, True])
def test_second_distinct_refusal_frees_the_task_with_a_handoff(store, monkeypatch, expanded):
    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], harness="codex"))
    capture = [CAPTURE]

    def herdr(args):
        if args[1] == "get":
            return {"name": owner.name, "pane_id": owner.pane_id, "agent_status": "blocked"}
        return {"text": capture[0]}

    observer = HerdrRuntime(herdr=herdr)
    previous = runtime.observe
    monkeypatch.setattr(
        runtime, "observe", lambda agent: observer.observe(agent) if agent.name == owner.name else previous(agent)
    )
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    env = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": owner.name}
    swarm_heartbeat.heard("[swarm delivery] Continue", env, store.redis, 1500)
    tick("sw", store, ledger, runtime, 2000)
    tick("sw", store, ledger, runtime, 3000)
    assert owner.name not in runtime.killed
    capture[0] = (
        CAPTURE.replace("• Ran gh api repos/", "• Ran expanded details gh api repos/")
        if expanded
        else "Earlier scrollback\n" + CAPTURE
    )
    tick("sw", store, ledger, runtime, 4000)
    assert owner.name not in runtime.killed
    swarm_heartbeat.heard("[swarm delivery] Continue", env, store.redis, 4500)
    capture[0] = CAPTURE
    tick("sw", store, ledger, runtime, 5000)
    assert owner.name in runtime.killed
    assert store.claimant("sw", owner.task) is None
    assert ledger.rows[owner.task]["state"] == "open"
    transfer = transfers.list_transfers(store, "sw")[-1]
    assert transfer["avoid_harness"] == "codex"
    assert "## Next" in transfer["handoff"]
    assert not outage.open_records("sw")
