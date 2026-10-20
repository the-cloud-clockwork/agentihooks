from pathlib import Path

import pytest

from scripts.swarm.delivery import HerdrMessenger
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord

CAPTURE = Path("tests/fixtures/swarm/codex-content-refusal.txt").read_text()


from dataclasses import replace

from hooks.context import swarm_heartbeat
from scripts.handoff import transfers
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import content_refusal, idle, outage, pane
from scripts.swarm.store import SwarmConfig
from scripts.swarm.tick import _watch_idle, tick
from tests.swarm.test_outage_resume import three_owners
from tests.swarm.test_outage_seat_release import executing
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store


@pytest.mark.parametrize("component", [HerdrRuntime, HerdrMessenger])
@pytest.mark.parametrize("state", ["blocked", "idle"])
@pytest.mark.parametrize("styled", [False, True])
def test_completed_codex_refusal_is_idle_for_the_normal_inbox_wake(component, state, styled):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")
    capture = CAPTURE.replace("ⓘ", "\x1b[31mⓘ").replace("shown", "shown\x1b[0m") if styled else CAPTURE

    def herdr(args):
        if args == ["agent", "get", "p1"]:
            return {"name": "worker", "pane_id": "p1", "agent_status": state}
        assert args == ["pane", "read", "p1", "--source", "visible", "--format", "ansi"]
        return {"text": capture}

    runtime = component(herdr=herdr)
    if component is HerdrRuntime:
        observed = runtime.observe(agent)
        assert observed.state == "idle" and observed.provider_checked and observed.content_refusal
    else:
        assert runtime.agent_status(agent) == "idle"
        assert runtime.typed_input(agent) == ""


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
def test_second_distinct_refusal_frees_the_task_with_a_handoff(store, monkeypatch, expanded, scratch):
    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], harness="codex"))
    homes = scratch(owner.task)
    claims = store.claims("sw", owner.task)
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
    ledger.rows[owner.task]["claimed_by"] = "another-owner"
    tick("sw", store, ledger, runtime, 4200)
    assert owner.name not in runtime.killed
    ledger.rows[owner.task]["claimed_by"] = owner.name
    swarm_heartbeat.heard("[swarm delivery] Continue", env, store.redis, 4500)
    capture[0] = CAPTURE
    idle.prompted(store.redis, "sw", owner.name, 4500)
    tick("sw", store, ledger, runtime, 5000)
    assert owner.name not in runtime.killed
    end = 4500 + wake.quiet_ms({})
    runtime.stuck.add(owner.name)
    refused = tick("sw", store, ledger, runtime, end)
    assert any("could not retire" in action for action in refused)
    assert store.claimant("sw", owner.task) == owner.name
    assert not transfers.list_transfers(store, "sw")
    runtime.stuck.remove(owner.name)
    tick("sw", store, ledger, runtime, end + 1)
    assert owner.name in runtime.killed
    assert runtime.homes[owner.name] == homes
    assert store.claims("sw", owner.task) == claims - 1
    assert store.claimant("sw", owner.task) is None
    assert ledger.rows[owner.task]["state"] == "open"
    transfer = transfers.list_transfers(store, "sw")[-1]
    assert (transfer["avoid_harness"], transfer["reason"], transfer["at"]) == ("codex", "content-refusal", end + 1)
    assert "## Next" in transfer["handoff"]
    assert not outage.open_records("sw")


@pytest.mark.parametrize("reply,expected", [({"status": "blocked"}, "blocked"), ({}, "unknown")])
def test_messenger_keeps_native_fallback_states_for_an_unrelated_pane(reply, expected):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")

    def herdr(args):
        if args == ["agent", "get", "p1"]:
            return reply
        assert args == ["pane", "read", "p1", "--source", "visible", "--format", "ansi"]
        return {"text": "› "}

    assert HerdrMessenger(herdr).agent_status(agent) == expected


@pytest.mark.parametrize(
    "tail",
    [
        "",
        "\n• Current result\n› ",
        "\n› Continue\n• Current result\n› ",
        "\nWhat next?\n❯ 1. Continue\n  2. Stop\nEnter to confirm",
    ],
)
def test_an_old_refusal_without_its_idle_composer_is_not_a_completed_refusal(tail):
    capture = CAPTURE.rsplit("\n› ", 1)[0] + tail
    assert pane.content_refusal("codex", capture) is False


@pytest.mark.parametrize("change", [{"harness": "claude"}, {"lane": "master"}, {"harness": "codex"}])
def test_only_an_idle_codex_refusal_can_count(store, change):
    agent = replace(AgentRecord("worker", "eng", "t1", harness="codex"), **change)
    store.claim("sw", "t1", agent.name, 60_000)
    for _ in range(2):
        idle.begin_turn(store.redis, "sw", agent.name)
        assert (
            content_refusal.repeated(store, "sw", agent, pane.PaneObservation("working", content_refusal=True)) is False
        )
        assert content_refusal.repeated(store, "sw", agent, pane.PaneObservation("idle")) is False
    if agent.harness != "codex" or agent.lane == "master":
        for _ in range(2):
            idle.begin_turn(store.redis, "sw", agent.name)
            assert (
                content_refusal.repeated(store, "sw", agent, pane.PaneObservation("idle", content_refusal=True))
                is False
            )


def test_refusal_receipts_require_current_ownership_and_stay_with_their_task(store):
    agent = AgentRecord("worker", "eng", "t1", harness="codex")
    refused = pane.PaneObservation("idle", content_refusal=True)
    assert content_refusal.repeated(store, "sw", agent, refused) is False
    for slug, task in [("sw", "t1"), ("sw", "t2"), ("sw", "config"), ("other", "t1")]:
        if slug == "other":
            store.create(SwarmConfig(slug, "/repo", 1, 0))
        owner = agent if (slug, task) == ("sw", "t1") else replace(agent, name=f"worker-{slug}-{task}", task=task)
        store.claim(slug, task, owner.name, 60_000)
        assert content_refusal.repeated(store, slug, owner, refused) is False
    assert content_refusal.repeated(store, "sw", agent, refused) is False
    idle.begin_turn(store.redis, "sw", agent.name)
    assert content_refusal.repeated(store, "sw", agent, refused) is True


@pytest.mark.parametrize("source", ["stored", "transfer"])
def test_refusal_recovery_preserves_the_existing_handoff(store, source):
    agent = AgentRecord("worker", "eng", "t1", harness="codex")
    text = "## Next\nContinue the recorded change.\n"
    if source == "stored":
        store.put_handoff("sw", agent.task, text)
    else:
        transfers.record(store, "sw", agent, "recycle", text, 1)
    recovered = content_refusal.handoff(store, "sw", agent, 5000)
    assert recovered["handoff"] == text
    assert (recovered["reason"], recovered["at"], recovered["avoid_harness"]) == ("content-refusal", 5000, "codex")


def test_prompt_turn_receipts_follow_the_agent_to_its_joined_hive(store):
    from tests.hive.test_member_scope import join, place

    store.create(SwarmConfig("fixture", "/repo", 1, 0))
    join(store.redis, "m1")
    name = place(store, "m1")
    idle.begin_turn(store.redis, "fixture", name)
    assert idle.turn(store.redis, "fixture", name) == "1"
    idle.begin_turn(store.redis, "fixture", name)
    assert idle.turn(store.redis, "fixture", name) == "2"


@pytest.mark.parametrize("rows", [{}, {"t1": {"claimed_by": "another-owner", "state": "claimed"}}])
def test_a_refusal_never_retires_a_seat_without_its_ledger_owner(store, monkeypatch, rows):
    agent = AgentRecord("worker", "eng", "t1", harness="codex")
    store.claim("sw", agent.task, agent.name, 60_000)
    store.put_agent("sw", agent)
    runtime = FakeRuntime()
    runtime.live.add(agent.name)
    monkeypatch.setattr(runtime, "observe", lambda agent: pane.PaneObservation("idle", content_refusal=True))
    for at in (1000, 2000):
        idle.begin_turn(store.redis, "sw", agent.name)
        _watch_idle("sw", store, FakeLedger([]), runtime, rows, agent, at)
    assert runtime.killed == []
    assert store.claimant("sw", agent.task) == agent.name
