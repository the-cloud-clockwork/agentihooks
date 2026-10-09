from dataclasses import replace

import fakeredis
import pytest

from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, master_wake
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

START = 1_000
WINDOW = 20 * 60 * 1_000
PROMPT = "Swarm backstop: run agentihooks msg inbox and work your Priorities. Work is waiting while your pane is idle."


@pytest.fixture
def setup():
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
    assert sent == []
    expected = [f"woke idle master {master.name} to work waiting Priorities"]
    assert run(setup, START + WINDOW) == expected
    assert sent == [(master.name, PROMPT)]
    assert run(setup, START + 2 * WINDOW - 1) == []
    assert run(setup, START + 2 * WINDOW) == expected
    assert sent == [(master.name, PROMPT)] * 2


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
    assert run(setup, START + 2 * WINDOW) == []
    assert len(sent) == 1


def test_tick_runs_the_master_wake_pass(setup):
    store, master, runtime, _, sent = setup
    ledger = FakeLedger([{"id": "t", "state": "blocked"}])
    tick("sw", store, ledger, runtime, START)
    actions = tick("sw", store, ledger, runtime, START + WINDOW)
    assert f"woke idle master {master.name} to work waiting Priorities" in actions
    assert sent == [(master.name, PROMPT)]
