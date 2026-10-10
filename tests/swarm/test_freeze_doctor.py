import json
from urllib.error import URLError

import pytest

from scripts.doctor import loop, priming
from scripts.swarm import capacity, freeze, metrics, metrics_outbox, metrics_swarm
from scripts.swarm.host_budget import HostSample
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_freeze import PHASES, PLANS, SLICES, FrozenLedger, record, spawned
from tests.swarm.test_tick import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

TASKS = [{"id": "fix", "phase": loop.FIX_PHASE}, {"id": "watch", "phase": "p1"}]


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


class PairedLedger(FrozenLedger):
    def __init__(self, tasks, watched, *freezes):
        super().__init__(tasks, *freezes)
        self.watched, self.read = watched, []

    def state(self, slug):
        self.read.append(slug)
        if slug == "sw":
            return super().state(slug)
        if self.watched is None:
            raise LedgerGone(f"ledger {slug}: gone")
        return {"plans": PLANS, "phases": PHASES, "slices": SLICES, "tasks": [], "freezes": self.watched}


def doctor(store, watched, tasks=TASKS, *freezes):
    store.update("sw", template=priming.TEMPLATE)
    store.set_peer("sw", "watched")
    return PairedLedger(tasks, watched, *freezes)


def ticked(store, ledger):
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    return sorted(spawned(runtime))


def test_a_focus_on_the_watched_ledger_lets_the_doctor_claim_a_fix_and_holds_a_non_fix_task(store):
    ledger = doctor(store, [record("plans/a", "focus")])
    assert ticked(store, ledger) == ["fix"]
    assert ledger.rows["watch"]["state"] == "open"
    assert "watched" in ledger.read


def test_an_urgent_doctor_task_passes_the_watched_focus(store):
    ledger = doctor(store, [record("plans/a", "focus")], [{"id": "watch", "phase": "p1", "rank": "urgent"}])
    assert ticked(store, ledger) == ["watch"]


@pytest.mark.parametrize("target", ["plans/a", "phases/p1", "tasks/watch", "lane:eng", "kind:code"])
def test_a_direct_freeze_on_the_watched_ledger_leaves_the_doctor_free(store, target):
    ledger = doctor(store, [record(target)])
    assert ticked(store, ledger) == ["fix", "watch"]


def test_a_swarm_that_is_not_a_doctor_never_reads_its_peer(store):
    store.set_peer("sw", "watched")
    ledger = PairedLedger(TASKS, [record("plans/a", "focus")])
    assert ticked(store, ledger) == ["fix", "watch"]
    assert "watched" not in ledger.read


def test_a_doctor_without_a_peer_reads_only_its_own_ledger(store):
    store.update("sw", template=priming.TEMPLATE)
    ledger = PairedLedger(TASKS, [record("plans/a", "focus")])
    assert ticked(store, ledger) == ["fix", "watch"]
    assert set(ledger.read) == {"sw"}


def test_a_doctor_whose_watched_ledger_is_gone_claims_as_before(store):
    ledger = doctor(store, None)
    assert ticked(store, ledger) == ["fix", "watch"]
    assert "watched" in ledger.read


def test_the_doctor_drain_notice_names_each_watched_focus(store):
    ledger = doctor(
        store,
        [record("plans/a", "focus"), record("plans/b"), record("lane:ci", "focus")],
        [{"id": "watch", "phase": "p1"}],
    )
    assert ticked(store, ledger) == []
    assert store.config("sw").state == "drained"
    assert ledger.notes == [
        "The swarm has no task it may start: 1 open task is held by the focus on plan Swarm v2 in the watched "
        "swarm and the focus on the ci lane in the watched swarm"
    ]


def test_the_doctor_drain_notice_names_its_own_freeze_before_the_watched_focus(store):
    ledger = doctor(store, [record("plans/a", "focus")], [{"id": "watch", "phase": "p1"}], record("phases/p1"))
    ticked(store, ledger)
    assert ledger.notes == [
        "The swarm has no task it may start: 1 open task is held by the freeze on phase Build "
        "and the focus on plan Swarm v2 in the watched swarm"
    ]


def test_a_watched_focus_holding_only_fixes_stays_out_of_the_drain_notice(store):
    ledger = doctor(store, [record("plans/a", "focus")], [{"id": "fix", "phase": loop.FIX_PHASE}], record("lane:eng"))
    ticked(store, ledger)
    assert ledger.notes == ["The swarm has no task it may start: 1 open task is held by the freeze on the eng lane"]


def test_watched_attaches_the_named_focuses_and_leaves_other_swarms_untouched(store):
    own = {"tasks": []}
    assert freeze.watched("sw", store, PairedLedger([], [record("plans/a", "focus")]), own) is own
    ledger = doctor(store, [record("plans/b"), record("kind:research", "focus")])
    assert freeze.watched("sw", store, ledger, own) == {
        "tasks": [],
        freeze.WATCHED: ["the focus on research tasks in the watched swarm"],
    }
    assert own == {"tasks": []}
    assert freeze.WATCHED == "watched_focus"


def test_autoscale_demand_for_the_doctor_counts_no_task_the_watched_focus_holds(store, monkeypatch):
    ledger = doctor(store, [record("plans/a", "focus")])
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    assert capacity.live_inputs("sw", store, ledger, {}, 5_000).demand == {"eng": 1, "ci": 0, "plan": 0}


class DemandRuntime:
    def __init__(self):
        self.demand = []

    def quota_capacity(self, config, agents, now, demand, requirements):
        self.demand.append(demand)
        caps = {"eng": 1, "ci": 0, "plan": 0}
        return {"configured": caps, "effective": caps, "reason": "accounts have quota", "placements": {}}


def test_the_capacity_pass_for_the_doctor_counts_no_task_the_watched_focus_holds(store):
    ledger, runtime = doctor(store, [record("plans/a", "focus")]), DemandRuntime()
    capacity.apply("sw", store.config("sw"), store, ledger, runtime, 1_000)
    assert runtime.demand == [{"eng": 1, "ci": 0, "plan": 0}]


def test_the_held_spawns_gauge_counts_no_doctor_task_the_watched_focus_holds(store, monkeypatch, tmp_path):
    ledger = doctor(store, [record("plans/a", "focus")])
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"configured": {"eng": 2}}))
    monkeypatch.setattr(metrics_swarm.host_budget, "read_host", lambda: HostSample(1.0, 1, 512, 0))
    monkeypatch.setattr(metrics_swarm.gate_log, "recent", lambda *args, **kwargs: [])
    monkeypatch.setattr(metrics_swarm, "read_classifier_calls", lambda box: metrics_swarm.LogBatch("", 0, []))
    monkeypatch.setattr(metrics_swarm, "read_review_events", lambda slug, box: metrics_swarm.LogBatch("", 0, []))
    monkeypatch.setattr(metrics.metrics_ledger, "record", lambda *args: None)
    spool = tmp_path / "outbox.db"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: spool)
    monkeypatch.setattr(
        metrics_outbox.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("sink down")),
    )
    configured = {"AGENTIHOOKS_METRICS_URL": "http://sink", "AGENTIHOOKS_METRICS_USER": "test"}
    snapshot = metrics_swarm.TickInput(store, ledger.state("sw"), [], lambda url: None, ledger)
    assert metrics.record_pass("sw", 1_000, 0, configured, snapshot) == []
    box = Outbox(spool, Settings("http://sink", "", ""))
    try:
        [row] = box.recent("host_samples", 1_000)
    finally:
        box.close()
    assert row["held_spawns"] == 1
    assert row["held_by"] == "quota"
    assert "watched" in ledger.read
