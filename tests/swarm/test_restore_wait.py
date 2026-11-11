import subprocess
from dataclasses import asdict, replace

import pytest

from scripts.swarm import resume, snapshot
from scripts.swarm.health import restore_wait
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from tests.swarm.test_resume import ResumingRuntime, restore
from tests.swarm.test_resume import store as store

pytestmark = pytest.mark.unit

MINUTE = 60_000


def waiting(store, at, name="sw-eng-1", **fields):
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    agent = AgentRecord(name, "eng", "t1", seat="eng-1@sw", state=resume.AWAITING, **fields)
    store.put_agent("sw", agent)
    row = resume.Outcome(name, "eng", "t1", resume.AWAITING, "worktree gone", "c1", at)
    store.put_restored("sw", [asdict(row)])
    return agent


def test_a_seat_waiting_past_one_tick_window_on_a_restore_decision_is_a_finding_for_the_master(store):
    waiting(store, at=0)
    (found,) = restore_wait.findings(store, "sw", 20 * MINUTE)
    assert (found.kind, found.subject, found.measure) == ("restore wait", "sw-eng-1", 20)
    assert found.id == "restore-wait/sw-eng-1"
    assert "eng-1@sw" in found.summary
    assert any("worktree gone" in line for line in found.evidence)


def test_a_seat_waiting_within_one_tick_window_raises_no_finding(store):
    waiting(store, at=0)
    assert restore_wait.findings(store, "sw", MINUTE) == []


def test_a_decided_restore_raises_no_finding(store):
    agent = waiting(store, at=0)
    store.put_agent("sw", replace(agent, state="working"))
    assert restore_wait.findings(store, "sw", 20 * MINUTE) == []


def test_the_wait_counts_from_when_a_retried_decision_first_awaited(store):
    waiting(store, at=0)
    row = {**store.restored("sw")[0], "at": 19 * MINUTE + 30_000, "awaited_at": 0}
    store.put_restored("sw", [row])
    (found,) = restore_wait.findings(store, "sw", 20 * MINUTE)
    assert found.measure == 20


def test_the_tick_sends_a_restore_wait_finding_to_the_master(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    waiting(store, at=0)
    store.put_agent("sw", AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat="master@sw"))
    (found,) = restore_wait.findings(store, "sw", 20 * MINUTE)
    shown = [{"id": found.id, "kind": found.kind, "subject": found.subject, "summary": found.summary}]
    inbox = InboxStore(store.redis)
    ledger_events.findings_pass(inbox, store, "sw", shown, {"phases": [], "tasks": [], "_meta": {"rev": 1}})
    (item,) = inbox.pending_items("master@sw")
    assert "restore wait" in item.text and "eng-1@sw" in item.text


def test_the_seats_current_occupant_is_judged_before_an_older_record_of_the_same_seat(store):
    older = AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw")
    current = AgentRecord("sw-master-2", MASTER, MASTER, seat="master@sw")
    seatless = AgentRecord("sw-eng-9", "eng", "t9")
    store.seats.occupy("master@sw", "sw-master-2", 1)
    ordered = resume.occupants_first(store, [older, seatless, current])
    assert [a.name for a in ordered] == ["sw-eng-9", "sw-master-2", "sw-master-1"]


def two_masters_saved(store, tmp_path):
    store.create(SwarmConfig("sw", str(tmp_path), 2, 1))
    for name in ("sw-master-1", "sw-master-2"):
        store.put_agent(
            "sw", AgentRecord(name, MASTER, MASTER, pane_id=f"w1:{name}", harness="claude", seat="master@sw")
        )
    snapshot.take(store, "sw", 99, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    store.redis.flushall()


def test_restore_refuses_a_second_waiting_agent_of_a_seat_that_already_waits(store, tmp_path):
    two_masters_saved(store, tmp_path)
    outcomes = restore(store, ResumingRuntime())
    assert sorted(o.outcome for o in outcomes) == [resume.AWAITING, resume.REFUSED]
    waited = next(o for o in outcomes if o.outcome == resume.AWAITING)
    refused = next(o for o in outcomes if o.outcome == resume.REFUSED)
    assert refused.reason == f"seat master@sw already waits on {waited.name}"
    states = {a.name: a.state for a in store.agents("sw")}
    assert states == {waited.name: resume.AWAITING, refused.name: "finished"}
    assert store.seats.occupant("master@sw").occupant == waited.name


def test_restore_lets_the_seats_current_occupant_wait_before_an_older_record(store, tmp_path):
    two_masters_saved(store, tmp_path)
    store.seats.occupy("master@sw", "sw-master-2", 1)
    outcomes = restore(store, ResumingRuntime())
    assert {o.name: o.outcome for o in outcomes} == {"sw-master-2": resume.AWAITING, "sw-master-1": resume.REFUSED}


class Notes:
    def __init__(self):
        self.lines = []

    def notify(self, slug, text):
        self.lines.append(text)


def test_a_reboot_refuses_a_second_waiting_agent_of_a_seat_that_already_waits(store):
    from scripts.swarm import reboot

    store.create(SwarmConfig("sw", "/repo", 2, 1))
    first = AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw", state=resume.AWAITING)
    second = AgentRecord("sw-master-2", MASTER, MASTER, seat="master@sw")
    store.put_agent("sw", first)
    store.put_agent("sw", second)
    notes = Notes()
    action = reboot._await_decision("sw", store, notes, second, 1_000)
    assert action == "sw-master-2 was not left waiting because seat master@sw already waits on sw-master-1"
    assert {a.name: a.state for a in store.agents("sw")} == {"sw-master-1": resume.AWAITING, "sw-master-2": "finished"}
    assert store.restored("sw") == []
    assert notes.lines == [f"{action}; one restore decision covers the seat."]
