from dataclasses import replace

import pytest

from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, master_wake
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

START = 1_000
WINDOW = 20 * 60 * 1_000
PROMPT = "Swarm backstop: run agentihooks msg inbox and work your Priorities. Work is waiting while your pane is idle."
SEAT = "master@sw"


@pytest.fixture
def setup():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    master = AgentRecord("master@a1b2c3-0001", MASTER, "", pane_id="w1:m1", harness="claude", started_at=START)
    store.put_agent("sw", master)
    runtime = FakeRuntime()
    runtime.live.add(master.name)
    runtime.statuses[master.name] = "idle"
    sent = []
    runtime.nudge = lambda agent, text: sent.append((agent.name, text))
    doc = {"tasks": [], "followups": [], "priorities": []}
    return store, master, runtime, doc, sent


def run(setup, at):
    store, _, runtime, doc, _ = setup
    return master_wake.run("sw", store, runtime, doc, at)


def backstops(store):
    return [(item.address, item.text) for item in InboxStore(store.redis).inbox(SEAT) if item.sender == "swarm"]


def read_backstops(store):
    inbox = InboxStore(store.redis)
    for item in inbox.open_items(SEAT):
        if item.sender == "swarm" and item.state == "pending":
            inbox.read(item.id, SEAT)


def open_backstops(store):
    return [item for item in InboxStore(store.redis).open_items(SEAT) if item.sender == "swarm"]


def woke(name):
    return [f"sent idle master {name} the backstop through its inbox to work waiting Priorities"]


@pytest.mark.parametrize("source", ["inbox", "seat", "read", "priority", "followup", "blocked"])
def test_idle_master_with_work_wakes_at_twenty_minutes_and_retries(setup, source):
    store, master, _, doc, sent = setup
    if source in {"inbox", "seat", "read"}:
        inbox = InboxStore(store.redis)
        item = inbox.send("worker", "master@sw" if source == "seat" else master.name, "Work waits")
        if source == "read":
            inbox.read(item.id, master.name)
    elif source == "priority":
        doc["priorities"] = [{"id": "p", "item": "questions/q", "text": "Decide"}]
    elif source == "followup":
        doc["followups"] = [{"id": "f", "done": False}]
    else:
        doc["tasks"] = [{"id": "t", "state": "blocked"}]
    assert run(setup, START) == []
    assert run(setup, START + WINDOW - 1) == []
    assert backstops(store) == []
    assert run(setup, START + WINDOW) == woke(master.name)
    assert backstops(store) == [(SEAT, PROMPT)]
    assert run(setup, START + 2 * WINDOW - 1) == []
    assert run(setup, START + 2 * WINDOW) == []
    read_backstops(store)
    assert run(setup, START + 2 * WINDOW + 1) == woke(master.name)
    assert backstops(store) == [(SEAT, PROMPT)] * 2
    assert [item.state for item in open_backstops(store)] == ["pending"]
    closed = [item.reason for item in InboxStore(store.redis).inbox(SEAT) if item.state == "done"]
    assert closed == ["done: a newer backstop replaced it"]
    assert sent == []


@pytest.mark.parametrize(
    "doc",
    [
        {"tasks": [], "followups": [], "priorities": []},
        {"tasks": [{"state": "blocked", "out_of_scope": True}], "followups": [{"done": True}], "priorities": []},
        {"tasks": [{"state": "done"}], "followups": [{"deleted": True}], "priorities": [{"cleared": True}]},
    ],
)
def test_idle_without_waiting_work_is_never_woken(setup, doc):
    setup[3].update(doc)
    run(setup, START)
    assert run(setup, START + 10 * WINDOW) == []
    assert setup[4] == []


def test_typed_input_prevents_wake(setup):
    _, master, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    runtime.typed[master.name] = "operator draft"
    assert run(setup, START + WINDOW) == []
    assert sent == []
    runtime.typed.clear()
    assert run(setup, START + WINDOW + 1)


def test_quiet_window_prevents_wake_until_its_boundary(setup, monkeypatch):
    store, master, _, doc, sent = setup
    monkeypatch.setenv(wake.QUIET_ENV, "60")
    doc["followups"] = [{"done": False}]
    run(setup, START)
    idle.prompted(store.redis, "sw", master.name, START + WINDOW - 1)
    assert run(setup, START + WINDOW + 59_998) == []
    assert sent == []
    assert run(setup, START + WINDOW + 59_999)


@pytest.mark.parametrize("state", ["working", "waiting", "unknown", "done"])
def test_non_idle_pane_resets_continuous_idle_time(setup, state):
    _, master, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    runtime.statuses[master.name] = state
    assert run(setup, START + WINDOW) == []
    runtime.statuses[master.name] = "idle"
    assert run(setup, START + WINDOW + 1) == []
    assert run(setup, START + 2 * WINDOW) == []
    assert sent == []
    assert run(setup, START + 2 * WINDOW + 1)


@pytest.mark.parametrize("change", ["gone", "finished", "starting", "worker", "no pane"])
def test_only_a_live_running_master_with_a_pane_is_woken(setup, change):
    store, master, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    if change == "gone":
        runtime.live.clear()
    else:
        fields = (
            {"state": change}
            if change in {"finished", "starting"}
            else {"lane": "eng"}
            if change == "worker"
            else {"pane_id": ""}
        )
        store.put_agent("sw", replace(master, **fields))
    run(setup, START)
    assert run(setup, START + WINDOW) == []
    assert sent == []


def test_retry_stops_when_waiting_work_is_resolved(setup):
    _, _, _, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    run(setup, START + WINDOW)
    doc["followups"][0]["done"] = True
    read_backstops(setup[0])
    assert run(setup, START + 2 * WINDOW) == []
    assert len(backstops(setup[0])) == 1
    assert sent == []


def test_tick_runs_the_master_wake_pass(setup):
    store, master, runtime, _, sent = setup
    ledger = FakeLedger([{"id": "t", "state": "blocked"}])
    tick("sw", store, ledger, runtime, START)
    actions = tick("sw", store, ledger, runtime, START + WINDOW)
    assert woke(master.name)[0] in actions
    assert backstops(store) == [(SEAT, PROMPT)]
    assert sent == []


def test_failed_master_pane_read_never_wakes(setup):
    from scripts.swarm.runtime import HerdrRuntime

    store, master, _, doc, sent = setup
    doc["followups"] = [{"done": False}]
    runtime = HerdrRuntime()
    runtime.live_names = lambda: {master.name}
    runtime._get = lambda target: {
        "pane_id": master.pane_id,
        "name": master.name.replace("@", "-"),
        "agent_status": "idle",
    }

    def failed_read(args):
        raise OSError("pane read unavailable")

    runtime.herdr = failed_read
    runtime.nudge = lambda agent, text: sent.append((agent.name, text))
    assert runtime.observe(master).state == "unknown"
    assert master_wake.run("sw", store, runtime, doc, START) == []
    assert master_wake.run("sw", store, runtime, doc, START + WINDOW) == []
    assert sent == []


@pytest.mark.parametrize("source", ["empty", "followup"])
def test_missing_or_excluded_work_sources_do_not_wake(setup, source):
    doc = setup[3]
    doc.clear()
    if source == "followup":
        doc["followups"] = [{"out_of_scope": True}]
    assert run(setup, START) == []
    assert run(setup, START + WINDOW) == []
    assert setup[4] == []


@pytest.mark.parametrize("reason", ["worker", "finished", "busy", "recent", "retry", "typed", "no work"])
def test_an_ineligible_record_does_not_suppress_the_waiting_master(setup, reason):
    store, first, runtime, doc, sent = setup
    second = replace(first, name="master@a1b2c3-0002", pane_id="w1:m2")
    store.put_agent("sw", second)
    runtime.live.add(second.name)
    runtime.statuses[second.name] = "idle"
    doc["followups"] = [{"done": False}]
    if reason == "worker":
        store.put_agent("sw", replace(first, lane="eng"))
    elif reason == "finished":
        store.put_agent("sw", replace(first, state="finished"))
    elif reason == "busy":
        runtime.statuses[first.name] = "working"
    elif reason == "recent":
        store.drop_agent("sw", first.name)
    elif reason == "typed":
        runtime.typed[first.name] = "draft"
    elif reason == "no work":
        doc.clear()
        InboxStore(store.redis).send("worker", second.name, "Work waits")
    run(setup, START)
    if reason == "recent":
        store.drop_agent("sw", second.name)
        store.put_agent("sw", first)
        store.put_agent("sw", second)
    elif reason == "retry":
        runtime.typed[second.name] = "draft"
        run(setup, START + WINDOW - 1)
        run(setup, START + WINDOW)
        runtime.typed.clear()
        sent.clear()
        read_backstops(store)
    assert run(setup, START + WINDOW + 1) == woke(second.name)
    assert backstops(store)[-1] == (SEAT, PROMPT)
    assert sent == []


def test_a_replacement_master_starts_its_own_idle_window(setup):
    store, first, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    store.drop_agent("sw", first.name)
    second = replace(first, name="master@a1b2c3-0002", pane_id="w1:m2")
    store.put_agent("sw", second)
    runtime.live = {second.name}
    runtime.statuses[second.name] = "idle"
    assert run(setup, START + WINDOW - 1) == []
    assert run(setup, START + WINDOW) == []
    assert sent == []
    assert run(setup, START + 2 * WINDOW - 1)


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_the_tick_does_not_wake_a_master_while_shutting_down(setup, state, monkeypatch):
    from scripts.swarm import tick as tick_module

    store, master, runtime, _, sent = setup
    ledger = FakeLedger([{"id": "t", "state": "blocked"}])
    store.update("sw", state=state)
    monkeypatch.setattr(tick_module, "_reap", lambda *args: [])
    monkeypatch.setattr(tick_module.lifetime, "retire_idle_master", lambda *args: [])
    monkeypatch.setattr(tick_module, "_close_space", lambda *args: [])
    runtime.retire = lambda *args, **kwargs: False
    master_wake.run("sw", store, runtime, ledger.state("sw"), START)
    tick("sw", store, ledger, runtime, START + WINDOW)
    assert sent == []


def test_removing_a_swarm_removes_its_idle_window(setup):
    store, master, _, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    config = store.config("sw")
    store.drop_agent("sw", master.name)
    store.remove("sw")
    store.create(config)
    store.put_agent("sw", master)
    assert run(setup, START + WINDOW) == []
    assert sent == []
    assert run(setup, START + 2 * WINDOW)


def test_an_open_backstop_from_an_earlier_master_is_not_waiting_work(setup):
    store, first, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    run(setup, START + WINDOW)
    read_backstops(store)
    doc.clear()
    store.drop_agent("sw", first.name)
    second = replace(first, name="master@a1b2c3-0002", pane_id="w1:m2")
    store.put_agent("sw", second)
    runtime.live = {second.name}
    runtime.statuses[second.name] = "idle"
    run(setup, START + WINDOW + 1)
    assert run(setup, START + 3 * WINDOW) == []
    assert len(backstops(store)) == 1
    assert sent == []


def test_a_replacement_master_gets_no_second_backstop_while_one_waits_unread(setup):
    store, first, runtime, doc, sent = setup
    doc["followups"] = [{"done": False}]
    run(setup, START)
    run(setup, START + WINDOW)
    store.drop_agent("sw", first.name)
    second = replace(first, name="master@a1b2c3-0002", pane_id="w1:m2")
    store.put_agent("sw", second)
    runtime.live = {second.name}
    runtime.statuses[second.name] = "idle"
    run(setup, START + WINDOW + 1)
    assert run(setup, START + 3 * WINDOW) == []
    read_backstops(store)
    assert run(setup, START + 3 * WINDOW + 1) == woke(second.name)
    assert len(backstops(store)) == 2
    assert len(open_backstops(store)) == 1
    assert sent == []


def test_a_codex_master_gets_the_backstop_through_its_inbox_and_an_unread_one_reaches_the_operator(setup):
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, master, runtime, doc, sent = setup
    codex = replace(master, harness="codex", seat=SEAT)
    store.put_agent("sw", codex)
    store.seats.occupy(SEAT, codex.name, START)
    doc["followups"] = [{"done": False}]
    run(setup, START)
    assert run(setup, START + WINDOW) == woke(codex.name)
    assert backstops(store) == [(SEAT, PROMPT)]
    inbox = InboxStore(store.redis)
    herdr, ledger = FakeHerdr({codex.pane_id: "idle"}), FakeLedger()
    first = inbox.open_items(SEAT)[0].created_at
    for at in range(first, first + 10 * wake.window_ms({}), wake.window_ms({})):
        wake.wake_now(inbox, "sw", [codex], herdr, at, wake.window_ms({}))
        wake.wake_pass(inbox, "sw", [codex], herdr, ledger, at, wake.window_ms({}))
    assert herdr.prompts == []
    assert ledger.flagged == [True]
    assert "still unread" in ledger.followups[0][1]
    assert sent == []
