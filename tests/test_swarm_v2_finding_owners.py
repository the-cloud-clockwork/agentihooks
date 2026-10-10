import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import masters

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
LEAD = f"master@{SLUG}"
SECOND = f"master-2@{SLUG}"
DOC = {
    "phases": [{"id": p, "title": f"phase {p}"} for p in ("p1", "p2", "p3", "p4")],
    "tasks": [
        {"id": "t1", "phase": "p1", "state": "claimed", "claimed_by": "sw-eng-1"},
        {"id": "t2", "phase": "p2", "state": "claimed", "claimed_by": "sw-eng-2"},
    ],
    "_meta": {"rev": 1, "events": []},
}
HELD_KINDS = ["idle with claim", "waiting on input", "stalled", "working on drain"]


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    for agent in (
        AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat=LEAD),
        AgentRecord("sw-master-2", MASTER, MASTER, pane_id="pm2", seat=SECOND),
        AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1"),
        AgentRecord("sw-eng-2", "eng", "t2", pane_id="p2"),
        AgentRecord("sw-eng-9", "eng", "", pane_id="p9"),
        AgentRecord("sw-eng-done", "eng", "t2", pane_id="pd", state="finished"),
    ):
        store.put_agent(SLUG, agent)
    return store, InboxStore(store.redis)


def texts(inbox, address):
    return [item.text for item in inbox.pending_items(address)]


def finding(kind, subject):
    return {"id": f"{kind.replace(' ', '-')}/{subject}", "kind": kind, "subject": subject, "summary": "look"}


@pytest.mark.parametrize("kind", HELD_KINDS)
def test_a_finding_on_an_agent_holding_a_task_reaches_the_master_owning_that_task_phase(swarm, kind):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding(kind, "sw-eng-2")], DOC)
    assert [kind in text for text in texts(inbox, SECOND)] == [True]
    assert texts(inbox, LEAD) == []


def test_a_finding_on_a_finished_agent_still_holding_a_task_reaches_the_master_owning_its_phase(swarm):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding("idle with claim", "sw-eng-done")], DOC)
    assert len(texts(inbox, SECOND)) == 1
    assert texts(inbox, LEAD) == []


@pytest.mark.parametrize("kind", HELD_KINDS)
def test_a_finding_on_an_agent_holding_a_lead_owned_task_reaches_the_lead(swarm, kind):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding(kind, "sw-eng-1")], DOC)
    assert [kind in text for text in texts(inbox, LEAD)] == [True]
    assert texts(inbox, SECOND) == []


@pytest.mark.parametrize("kind", ["proof loop", "failed launch", "stale claim"])
@pytest.mark.parametrize(("task", "owner", "other"), [("t2", SECOND, LEAD), ("t1", LEAD, SECOND)])
def test_a_finding_about_a_task_reaches_the_master_owning_its_phase(swarm, kind, task, owner, other):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding(kind, task)], DOC)
    assert [kind in text for text in texts(inbox, owner)] == [True]
    assert texts(inbox, other) == []


@pytest.mark.parametrize("subject", ["sw-eng-9", "sw-gone"])
def test_a_finding_on_an_agent_holding_no_task_reaches_the_lead(swarm, subject):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding("stalled", subject)], DOC)
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []


def test_an_agent_behaviour_finding_reaches_the_lead_even_while_the_agent_holds_a_task(swarm):
    store, inbox = swarm
    ledger_events.findings_pass(inbox, store, SLUG, [finding("over monitoring", "sw-eng-2")], DOC)
    assert len(texts(inbox, LEAD)) == 1 and texts(inbox, SECOND) == []
