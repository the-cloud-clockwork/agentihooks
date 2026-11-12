import subprocess
from dataclasses import asdict, replace

import pytest

from scripts.handoff import transfers
from scripts.swarm import lease, reboot, resume, snapshot
from scripts.swarm.health import restore_wait
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from tests.swarm.test_resume import ResumingRuntime, restore
from tests.swarm.test_resume import store as store

pytestmark = pytest.mark.unit

MINUTE = 60_000


def swarm(store):
    store.create(SwarmConfig("sw", "/repo", 2, 1))


def waiting(store, at, name="sw-eng-1", task="t1", seat="eng-1@sw", awaited_at=0):
    agent = AgentRecord(name, "eng", task, seat=seat, state=resume.AWAITING)
    store.put_agent("sw", agent)
    row = resume.Outcome(name, "eng", task, resume.AWAITING, "worktree gone", "c1", at, awaited_at=awaited_at)
    store.put_restored("sw", [*store.restored("sw"), asdict(row)])
    return agent


@pytest.mark.feature("restore-wait-health-finding")
def test_a_first_restore_wait_counts_from_its_restore_and_is_a_finding_for_the_master(store):
    swarm(store)
    waiting(store, at=10 * MINUTE)
    found = restore_wait.findings(store, "sw", 30 * MINUTE + 30_000)
    assert found == [
        Finding(
            "restore wait",
            "sw-eng-1",
            "seat eng-1@sw has waited 20 minutes on a resume or fresh restore decision",
            ("restore outcome: worktree gone", "task t1"),
            "over one tick window",
            20,
        )
    ]
    assert found[0].id == "restore-wait/sw-eng-1"


@pytest.mark.feature("restore-wait-health-finding")
def test_a_seatless_taskless_wait_names_the_agent_and_no_task(store):
    swarm(store)
    waiting(store, at=10 * MINUTE, task="", seat="")
    (found,) = restore_wait.findings(store, "sw", 30 * MINUTE + 30_000)
    assert found.summary == "seat sw-eng-1 has waited 20 minutes on a resume or fresh restore decision"
    assert found.evidence == ("restore outcome: worktree gone", "task none")


@pytest.mark.feature("restore-wait-health-finding")
def test_a_seat_waiting_within_one_tick_window_raises_no_finding(store):
    swarm(store)
    waiting(store, at=10 * MINUTE)
    assert restore_wait.findings(store, "sw", 10 * MINUTE + 30_000) == []


@pytest.mark.feature("restore-wait-health-finding")
def test_a_wait_of_exactly_one_tick_window_raises_no_finding(store):
    swarm(store)
    waiting(store, at=10 * MINUTE)
    assert restore_wait.findings(store, "sw", 10 * MINUTE + lease.tick_ms()) == []
    assert [f.subject for f in restore_wait.findings(store, "sw", 10 * MINUTE + lease.tick_ms() + 1)] == ["sw-eng-1"]


@pytest.mark.feature("restore-wait-health-finding")
def test_a_wait_within_the_window_never_hides_a_later_seat_past_it(store):
    swarm(store)
    waiting(store, at=29 * MINUTE + 50_000, name="sw-eng-1")
    waiting(store, at=10 * MINUTE, name="sw-eng-2", seat="eng-2@sw")
    found = restore_wait.findings(store, "sw", 30 * MINUTE + 30_000)
    assert [(f.subject, f.measure) for f in found] == [("sw-eng-2", 20)]


@pytest.mark.feature("restore-wait-health-finding")
def test_a_decided_restore_raises_no_finding_and_never_hides_a_later_wait(store):
    swarm(store)
    agent = waiting(store, at=10 * MINUTE, name="sw-eng-1")
    store.put_agent("sw", replace(agent, state="working"))
    assert restore_wait.findings(store, "sw", 30 * MINUTE + 30_000) == []
    waiting(store, at=10 * MINUTE, name="sw-eng-2", seat="eng-2@sw")
    found = restore_wait.findings(store, "sw", 30 * MINUTE + 30_000)
    assert [f.subject for f in found] == ["sw-eng-2"]


@pytest.mark.feature("restore-wait-health-finding")
def test_the_wait_counts_from_when_a_retried_decision_first_awaited(store):
    swarm(store)
    waiting(store, at=29 * MINUTE + 30_000, awaited_at=10 * MINUTE)
    (found,) = restore_wait.findings(store, "sw", 30 * MINUTE + 30_000)
    assert found.measure == 20


@pytest.mark.feature("restore-wait-health-finding")
def test_swarm_status_reports_a_restore_wait_at_the_current_time(store, monkeypatch):
    from scripts.swarm import status

    swarm(store)
    waiting(store, at=10 * MINUTE)
    monkeypatch.setattr(status, "now_ms", lambda: 30 * MINUTE + 30_000)
    rows = status.findings(store, "sw", store.config("sw"), [], [])
    summary = "seat eng-1@sw has waited 20 minutes on a resume or fresh restore decision"
    assert [(r["subject"], r["summary"]) for r in rows if r["kind"] == "restore wait"] == [("sw-eng-1", summary)]


@pytest.mark.feature("restore-wait-health-finding")
def test_the_tick_sends_a_restore_wait_finding_to_the_master(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    swarm(store)
    waiting(store, at=10 * MINUTE)
    store.put_agent("sw", AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat="master@sw"))
    (found,) = restore_wait.findings(store, "sw", 30 * MINUTE)
    shown = [{"id": found.id, "kind": found.kind, "subject": found.subject, "summary": found.summary}]
    inbox = InboxStore(store.redis)
    ledger_events.findings_pass(inbox, store, "sw", shown, {"phases": [], "tasks": [], "_meta": {"rev": 1}})
    (item,) = inbox.pending_items("master@sw")
    assert "restore wait" in item.text and "eng-1@sw" in item.text


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
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


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
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


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_restore_lets_the_seats_current_occupant_wait_before_an_older_record(store, tmp_path):
    two_masters_saved(store, tmp_path)
    store.seats.occupy("master@sw", "sw-master-2", 1)
    outcomes = restore(store, ResumingRuntime())
    assert {o.name: o.outcome for o in outcomes} == {"sw-master-2": resume.AWAITING, "sw-master-1": resume.REFUSED}


class Notes:
    def __init__(self):
        self.lines = []

    def notify(self, slug, text):
        self.lines.append((slug, text))


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_a_reboot_refuses_a_second_waiting_agent_of_a_seat_that_already_waits(store):
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    first = AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw", state=resume.AWAITING)
    second = AgentRecord("sw-master-2", MASTER, MASTER, seat="master@sw")
    store.put_agent("sw", first)
    store.put_agent("sw", second)
    store.seats.occupy("master@sw", "sw-master-1", 1)
    notes = Notes()
    action = reboot._await_decision("sw", store, notes, second, 1_000)
    assert action == "sw-master-2 was not left waiting because seat master@sw already waits on sw-master-1"
    assert {a.name: a.state for a in store.agents("sw")} == {"sw-master-1": resume.AWAITING, "sw-master-2": "finished"}
    assert store.restored("sw") == []
    assert notes.lines == [("sw", f"{action}; one restore decision covers the seat.")]


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_a_stale_waiting_record_that_no_longer_holds_the_seat_never_refuses_its_occupant(store):
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    stale = AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw", state=resume.AWAITING)
    current = AgentRecord("sw-master-2", MASTER, MASTER, seat="master@sw")
    store.put_agent("sw", stale)
    store.put_agent("sw", current)
    store.seats.occupy("master@sw", "sw-master-2", 1)
    assert resume.waiting_peer(store, "sw", current) == ""
    reboot._await_decision("sw", store, Notes(), current, 1_000)
    assert {a.name: a.state for a in store.agents("sw")}["sw-master-2"] == resume.AWAITING


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_restore_refuses_a_second_agent_of_a_waiting_seat_even_when_its_own_conversation_could_reopen(store, tmp_path):
    store.create(SwarmConfig("sw", str(tmp_path), 2, 1))
    store.put_agent(
        "sw", AgentRecord("sw-master-1", MASTER, MASTER, pane_id="w1:m1", harness="claude", seat="master@sw")
    )
    second = AgentRecord(
        "sw-master-2", MASTER, MASTER, pane_id="w1:m2", harness="claude", seat="master@sw", conversation_id="c2"
    )
    store.put_agent("sw", second)
    snapshot.take(store, "sw", 99, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    store.redis.flushall()
    store.seats.occupy("master@sw", "sw-master-1", 1)
    rt = ResumingRuntime()
    outcomes = restore(store, rt)
    assert {o.name: o.outcome for o in outcomes} == {"sw-master-1": resume.AWAITING, "sw-master-2": resume.REFUSED}
    assert rt.resumed == []
    refused = next(o for o in outcomes if o.outcome == resume.REFUSED)
    reason = "seat master@sw already waits on sw-master-1"
    assert replace(refused, transfer="") == resume.Outcome(
        "sw-master-2", MASTER, MASTER, resume.REFUSED, reason, "c2", 1_000
    )
    assert refused.transfer in {
        r["id"] for r in transfers.list_transfers(store, "sw") if r["predecessor"] == "sw-master-2"
    }


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_a_seatless_agent_has_no_waiting_peer(store):
    swarm(store)
    store.put_agent("sw", AgentRecord("sw-master-1", MASTER, MASTER, state=resume.AWAITING))
    assert resume.waiting_peer(store, "sw", AgentRecord("sw-eng-1", "eng", "t1")) == ""


@pytest.mark.feature("restore-refuses-second-waiting-seat-agent")
def test_another_seat_waiting_never_makes_a_working_occupant_a_waiting_peer(store):
    swarm(store)
    store.put_agent("sw", AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw"))
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw", state=resume.AWAITING))
    store.seats.occupy("master@sw", "sw-master-1", 1)
    second = AgentRecord("sw-master-2", MASTER, MASTER, seat="master@sw")
    assert resume.waiting_peer(store, "sw", second) == ""
