import json

import pytest

from scripts.swarm.store import SwarmConfig, SwarmError
from scripts.swarm_v2 import admission as admission_module
from scripts.swarm_v2.admission import (
    ACTIVATED,
    ADMITTED,
    DEFERRED,
    EXPIRED,
    IMPOSSIBLE,
    RELEASED,
    REPLAYED,
    Decision,
    PendingAdmission,
    Policy,
    Resources,
    Template,
    requested,
)
from tests.sv2_ctl03_cases import fresh_store, outcomes, run_case

pytestmark = pytest.mark.unit

GENERAL = Template("general", Resources(16384, 4000))
COMPUTE = Template("compute", Resources(8192, 8000))


def make(global_cap=3, swarm_cap=3, ttl=100, slots=10, store=None):
    store = store or fresh_store()
    for slug in ("a", "b"):
        store.create(SwarmConfig(slug, "/repo", 10, 0))
    provider = {"slots": slots}
    gate = PendingAdmission(
        store,
        Policy(global_cap, swarm_cap, ttl, (GENERAL, COMPUTE), Resources(4096, 1000)),
        lambda s: provider["slots"],
    )
    return store, gate, provider


def ids(*names):
    return [{"id": n} for n in names]


def test_requested_reads_task_resources_over_the_default():
    default = Resources(4096, 1000)
    assert requested({"id": "t"}, default) == default
    assert requested({"id": "t", "resources": None}, default) == default
    assert requested({"id": "t", "resources": {"memory_mib": "9000"}}, default) == Resources(9000, 1000)
    assert requested({"id": "t", "resources": {"cpu_millis": 6000}}, default) == Resources(4096, 6000)
    for bad in ({"memory_mib": 0}, {"cpu_millis": -1}, {"cpu_millis": 0}):
        with pytest.raises(ValueError, match="^resources must be positive$"):
            requested({"id": "t", "resources": bad}, default)
    for bad in (["memory_mib"], "8192"):
        with pytest.raises(ValueError, match="^resources must be a mapping$"):
            requested({"id": "t", "resources": bad}, default)
    assert requested({"id": "t", "resources": {"memory_mib": 1, "cpu_millis": 1}}, default) == Resources(1, 1)


@pytest.mark.parametrize(
    "bad", [{"memory_mib": "lots"}, {"cpu_millis": None}, {"memory_mib": 0}, {"cpu_millis": -5}, ["memory_mib"], "8192"]
)
def test_unreadable_resources_are_impossible_and_never_abort_the_batch(bad):
    store, gate, _ = make()
    got = gate.admit("a", [{"id": "bad", "resources": bad}, *ids("t1")], 0)
    assert got[0] == Decision(
        "bad",
        IMPOSSIBLE,
        reason="task bad has unreadable resources; set memory_mib and cpu_millis to positive integers",
    )
    assert got[1].outcome == ADMITTED


def test_resources_fit_on_both_dimensions_inclusive():
    assert Resources(16384, 4000).fits(Resources(16384, 4000))
    assert not Resources(16385, 4000).fits(Resources(16384, 4000))
    assert not Resources(16384, 4001).fits(Resources(16384, 4000))


def test_admission_stops_at_the_swarm_cap_in_order():
    store, gate, _ = make(global_cap=10, swarm_cap=2)
    got = gate.admit("a", ids("t1", "t2", "t3", "t4"), 0)
    assert [(d.task, d.outcome) for d in got] == [
        ("t1", ADMITTED),
        ("t2", ADMITTED),
        ("t3", DEFERRED),
        ("t4", DEFERRED),
    ]
    assert got[2].reason == "pending cap reached: 2 of 2 pending attempts for this swarm, 2 of 10 across swarms"
    assert got[0].deadline_ms == 100
    assert got[0].reservation and got[0].reservation != got[1].reservation


def test_global_cap_bounds_every_swarm_together():
    store, gate, _ = make(global_cap=3, swarm_cap=3)
    assert outcomes(gate.admit("a", ids("t1", "t2"), 0)) == {"t1": ADMITTED, "t2": ADMITTED}
    got = gate.admit("b", ids("u1", "u2"), 0)
    assert outcomes(got) == {"u1": ADMITTED, "u2": DEFERRED}
    assert got[1].reason == "pending cap reached: 1 of 3 pending attempts for this swarm, 3 of 3 across swarms"
    assert store.redis.zcard(gate.global_key) == 3


def test_provider_slots_bound_pending_without_touching_provider_sessions():
    store, gate, provider = make(global_cap=10, swarm_cap=10, slots=2)
    got = gate.admit("a", ids("t1", "t2", "t3"), 0)
    assert outcomes(got) == {"t1": ADMITTED, "t2": ADMITTED, "t3": DEFERRED}
    assert got[2].reason == "provider slots reached: 2 pending attempts already wait on 2 free provider sessions"
    assert provider == {"slots": 2}
    assert outcomes(gate.admit("a", ids("t1", "t2", "t3"), 1)) == {"t1": REPLAYED, "t2": REPLAYED, "t3": DEFERRED}


def test_impossible_requests_are_rejected_with_an_actionable_reason_and_hold_nothing():
    store, gate, _ = make()
    big = {"id": "big", "resources": {"memory_mib": 65536}}
    got = gate.admit("a", [big, *ids("t1")], 0)
    assert outcomes(got) == {"big": IMPOSSIBLE, "t1": ADMITTED}
    assert got[0] == Decision(
        "big",
        IMPOSSIBLE,
        reason=(
            "task big requests 65536 MiB memory and 1000 millicpu, more than every approved template "
            "(general 16384 MiB 4000 millicpu; compute 8192 MiB 8000 millicpu); "
            "lower the task resources or approve a larger template"
        ),
    )
    assert sorted(gate.pending("a", 0)) == ["t1"]


def test_a_request_fitting_one_template_on_both_dimensions_is_possible():
    store, gate, _ = make()
    fits_compute = {"id": "c", "resources": {"memory_mib": 8192, "cpu_millis": 8000}}
    neither = {"id": "n", "resources": {"memory_mib": 16384, "cpu_millis": 8000}}
    assert outcomes(gate.admit("a", [fits_compute, neither], 0)) == {"c": ADMITTED, "n": IMPOSSIBLE}


def test_no_templates_makes_every_request_impossible():
    store = fresh_store()
    gate = PendingAdmission(store, Policy(3, 3, 100, (), Resources(1, 1)), lambda s: 3)
    got = gate.admit("a", ids("t1"), 0)
    assert got[0].outcome == IMPOSSIBLE
    assert got[0].reason.endswith(
        "more than every approved template (none); lower the task resources or approve a larger template"
    )


def test_admission_set_to_zero_admits_nothing_and_says_so():
    store, gate, _ = make(global_cap=0, swarm_cap=0)
    got = gate.admit("a", ids("t1"), 0)
    assert got == [Decision("t1", DEFERRED, reason="distributed admission is set to zero")]
    assert gate.pending("a", 0) == {}


def test_a_cap_lowered_below_held_reservations_keeps_them_and_defers_new_work():
    store, gate, _ = make(global_cap=5, swarm_cap=5)
    gate.admit("a", ids("t1", "t2", "t3"), 0)
    lowered = PendingAdmission(store, Policy(1, 1, 100, (GENERAL,), Resources(1, 1)), lambda s: 10)
    got = lowered.admit("a", ids("t1", "t4"), 1)
    assert outcomes(got) == {"t1": REPLAYED, "t4": DEFERRED}
    assert got[1].reason == "pending cap reached: 3 of 1 pending attempts for this swarm, 3 of 1 across swarms"
    assert sorted(lowered.pending("a", 1)) == ["t1", "t2", "t3"]


def test_replay_keeps_the_same_reservation_and_writes_no_duplicate():
    store, gate, _ = make()
    first = gate.admit("a", ids("t1"), 0)[0]
    again = gate.admit("a", ids("t1"), 50)[0]
    assert again == Decision("t1", REPLAYED, first.reservation, first.deadline_ms)
    assert store.redis.hlen(gate.key("a")) == 1
    assert store.redis.zcard(gate.global_key) == 1
    assert gate.pending_execution_admission_total("a") == {ADMITTED: 1}


def test_a_task_named_twice_in_one_call_is_admitted_once():
    store, gate, _ = make()
    first, second = gate.admit("a", ids("t1", "t1"), 0)
    assert first.outcome == ADMITTED
    assert second == Decision("t1", REPLAYED, first.reservation, first.deadline_ms)
    assert gate.pending_execution_admission_total("a") == {ADMITTED: 1}


def test_held_names_the_stored_reservation_even_past_its_deadline():
    store, gate, _ = make()
    first = gate.admit("a", ids("t1"), 0)[0]
    assert gate.held("a", "t1") == first.reservation
    assert gate.pending("a", 500) == {}
    assert gate.held("a", "t1") == first.reservation
    assert gate.held("a", "missing") == ""


def test_reservations_expire_at_their_deadline_and_free_their_slot():
    store, gate, _ = make(swarm_cap=1)
    first = gate.admit("a", ids("t1"), 0)[0]
    assert gate.pending("a", 99) == {"t1": Decision("t1", REPLAYED, first.reservation, 100)}
    assert gate.pending("a", 100) == {}
    got = gate.admit("a", ids("t2", "t1"), 100)
    assert outcomes(got) == {"t2": ADMITTED, "t1": DEFERRED}
    assert store.redis.hexists(gate.key("a"), "t1") is False
    assert gate.pending_execution_admission_total("a") == {ADMITTED: 2, DEFERRED: 1, EXPIRED: 1}


def test_an_expired_reservation_of_another_swarm_leaves_the_global_count():
    store, gate, _ = make(global_cap=1, swarm_cap=1)
    gate.admit("b", ids("u1"), 0)
    assert outcomes(gate.admit("a", ids("t1"), 99)) == {"t1": DEFERRED}
    assert outcomes(gate.admit("a", ids("t1"), 100)) == {"t1": ADMITTED}
    assert store.redis.zrange(gate.global_key, 0, -1) == ["a\tt1"]


def test_activate_and_release_end_only_the_current_reservation():
    store, gate, _ = make()
    t1, t2 = gate.admit("a", ids("t1", "t2"), 0)
    assert gate.activate("a", "t1", "other") is False
    assert gate.release("a", "missing", t2.reservation) is False
    assert gate.activate("a", "t1", t1.reservation) is True
    assert gate.release("a", "t2", t2.reservation) is True
    assert gate.activate("a", "t1", t1.reservation) is False
    assert gate.pending("a", 0) == {}
    assert store.redis.zcard(gate.global_key) == 0
    assert gate.pending_execution_admission_total("a") == {ADMITTED: 2, ACTIVATED: 1, RELEASED: 1}


def test_counts_start_empty_and_are_per_swarm():
    store, gate, _ = make()
    assert gate.pending_execution_admission_total("a") == {}
    gate.admit("b", ids("u1"), 0)
    assert gate.pending_execution_admission_total("a") == {}
    assert gate.pending_execution_admission_total("b") == {ADMITTED: 1}


def test_stored_reservation_records_the_requested_resources():
    store, gate, _ = make()
    gate.admit("a", [{"id": "t1", "resources": {"memory_mib": 2048}}], 0)
    row = json.loads(store.redis.hget(gate.key("a"), "t1"))
    assert row["resources"] == {"memory_mib": 2048, "cpu_millis": 1000}
    assert row["deadline_ms"] == 100


def test_a_concurrent_write_is_retried_and_admission_stays_bounded(monkeypatch):
    store, gate, _ = make(global_cap=10, swarm_cap=1)
    real, raced = gate._decide, []

    def racing(*args):
        if not raced:
            raced.append(True)
            PendingAdmission(store, gate.policy, lambda s: 10).admit("a", ids("rival"), 0)
        return real(*args)

    monkeypatch.setattr(gate, "_decide", racing)
    assert outcomes(gate.admit("a", ids("t1"), 0)) == {"t1": DEFERRED}
    assert sorted(gate.pending("a", 0)) == ["rival"]
    assert gate.pending_execution_admission_total("a") == {ADMITTED: 1, DEFERRED: 1}


def test_a_conflict_that_never_settles_raises(monkeypatch):
    from redis.exceptions import WatchError

    store, gate, _ = make()
    calls = []

    def conflicted(*args):
        calls.append(args)
        raise WatchError("conflict")

    monkeypatch.setattr(gate, "_admit", conflicted)
    with pytest.raises(SwarmError, match="^pending admission kept conflicting; retry on the next tick$"):
        gate.admit("a", ids("t1"), 0)
    assert len(calls) == admission_module.ATTEMPTS == 8
    monkeypatch.setattr(gate, "_end_once", conflicted)
    with pytest.raises(SwarmError, match="^pending admission kept conflicting; retry on the next tick$"):
        gate.release("a", "t1", "r")


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_pass_from_independent_state(case):
    first, second = run_case(case), run_case(case)
    assert first["state"] == second["state"] == "passed"
    assert first["decisions"] == second["decisions"]
    assert first["input_sha256"] == second["input_sha256"]


def test_case_a_counts_one_outcome_per_task():
    result = run_case("a")
    assert result["pending"] == ["t01", "t02", "t03"]
    assert result["pending_execution_admission_total"] == {ADMITTED: 3, IMPOSSIBLE: 1, DEFERRED: 6}


def test_keys_are_named_per_swarm():
    store, gate, _ = make()
    assert gate.key("a").endswith(":a:pending-admission")
    assert gate.total_key("a").endswith(":a:pending-admission-total")


def test_the_provider_slot_source_is_asked_for_the_admitting_swarm():
    store, gate, _ = make()
    asked = []
    gate.provider_slots = lambda slug: asked.append(slug) or 10
    gate.admit("b", ids("u1"), 0)
    assert asked == ["b"]


@pytest.mark.parametrize("caps", [(0, 3), (3, 0)])
def test_either_cap_at_zero_turns_admission_off(caps):
    store, gate, _ = make(*caps)
    assert gate.admit("a", ids("t1"), 0) == [Decision("t1", DEFERRED, reason="distributed admission is set to zero")]


def _race(monkeypatch, gate, write):
    real, raced = gate._decide, []

    def racing(*args):
        if not raced:
            raced.append(True)
            write()
        return real(*args)

    monkeypatch.setattr(gate, "_decide", racing)


def test_a_write_by_another_swarm_alone_forces_a_retry(monkeypatch):
    store, gate, _ = make(global_cap=1, swarm_cap=3)
    _race(monkeypatch, gate, lambda: PendingAdmission(store, gate.policy, lambda s: 10).admit("b", ids("u1"), 0))
    assert outcomes(gate.admit("a", ids("t1"), 0)) == {"t1": DEFERRED}
    assert store.redis.zrange(gate.global_key, 0, -1) == ["b\tu1"]


def test_a_write_to_this_swarm_alone_forces_a_retry(monkeypatch):
    store, gate, _ = make(global_cap=10, swarm_cap=1)
    row = json.dumps({"reservation": "r", "deadline_ms": 100, "resources": {}})
    _race(monkeypatch, gate, lambda: store.redis.hset(gate.key("a"), "rival", row))
    assert outcomes(gate.admit("a", ids("t1"), 0)) == {"t1": DEFERRED}
    assert sorted(gate.pending("a", 0)) == ["rival"]
