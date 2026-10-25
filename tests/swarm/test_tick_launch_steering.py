from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, _slow_launches, tasks
from tests.swarm.test_tick import store as store


def _canary_failure(harness):
    error = SpawnError("profile canary did not validate")
    error.harness = harness
    return error


def _failing(monkeypatch, runtime, failing, seconds=0):
    clock = _slow_launches(monkeypatch, runtime, 0)
    spawn = runtime.spawn

    def launch(config, lane, name, task):
        if lane != MASTER and task["id"] in failing:
            clock[0] += seconds
            raise _canary_failure("codex")
        clock[0] += 5
        return spawn(config, lane, name, task)

    runtime.spawn = launch
    return clock


def test_a_slow_failed_launch_leaves_the_pass_budget_to_the_next_task(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    _failing(monkeypatch, runtime, {"t1"}, seconds=90)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t2"]
    assert ledger.rows["t2"]["state"] == "claimed"
    assert not any("launch budget" in action for action in actions)


def test_started_launches_still_spend_the_budget_after_a_failed_one(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    clock = _failing(monkeypatch, runtime, {"t1"}, seconds=90)
    spawn = runtime.spawn

    def slow(config, lane, name, task):
        if task["id"] == "t2":
            clock[0] += 15
        return spawn(config, lane, name, task)

    runtime.spawn = slow
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t2"]
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions


def test_the_forgiven_failure_leaves_the_mean_of_started_launches_alone(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    _failing(monkeypatch, runtime, {"t1"}, seconds=90)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t2", "t3"]
    assert not any("launch budget" in action for action in actions)


def test_only_the_first_failed_launch_of_a_pass_is_forgiven(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    clock = _slow_launches(monkeypatch, runtime, 0)
    spawn, cost = runtime.spawn, {"t1": 1, "t2": 90}

    def launch(config, lane, name, task):
        if lane != MASTER and task["id"] in cost:
            clock[0] += cost[task["id"]]
            raise _canary_failure("codex")
        return spawn(config, lane, name, task)

    runtime.spawn = launch
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions


def test_a_started_launch_clears_the_harness_its_task_avoided(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    failing = {"t1"}
    _failing(monkeypatch, runtime, failing)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.failed_harness("sw", "t1") == "codex"
    failing.clear()
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.failed_harness("sw", "t1") == ""


def test_resetting_claims_forgets_the_failed_harness(store):
    store.note_launch_failure("sw", "t1", "profile canary did not validate", "codex")
    store.note_launch_failure("sw", "t2", "placement refused")
    assert (store.failed_harness("sw", "t1"), store.failed_harness("sw", "t2")) == ("codex", "")
    store.reset_claims("sw", "t1")
    assert (store.failed_harness("sw", "t1"), store.launch_failure("sw", "t1")) == ("", "")


def test_launches_after_a_failed_launch_steer_off_its_harness(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    failing = {"t1"}
    _failing(monkeypatch, runtime, failing)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [(task["id"], task["avoid_harness"]) for task in runtime.tasks] == [("t2", "codex")]
    failing.clear()
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [(task["id"], task["avoid_harness"]) for task in runtime.tasks] == [("t2", "codex"), ("t1", "codex")]


def test_launches_without_a_failure_carry_no_harness_to_avoid(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    _failing(monkeypatch, runtime, set())
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task["id"] for task in runtime.tasks] == ["t1", "t2"]
    assert not any("avoid_harness" in task for task in runtime.tasks)


def test_a_capacity_wait_never_steers_the_retry(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    runtime.crash = SpawnError("no seat has room", "unavailable")
    runtime.crash.harness = "codex"
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.crash = None
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task["id"] for task in runtime.tasks] == ["t1"]
    assert "avoid_harness" not in runtime.tasks[0]


def test_a_task_avoiding_a_harness_launches_on_another_with_room(tmp_path, monkeypatch):
    from tests.swarm.test_capacity import _runtime_probe, account

    rows = [account("a", sessions=2), account("cx", harness="codex")]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, rows)
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan", "avoid_harness": "codex"})
    runtime.spawn(config, "plan", "planner@a1b2c3-0002", {"id": "q", "title": "Plan"})
    assert seen == [("p", "claude", "a"), ("q", "codex", "cx")]


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_failed_init_agent_names_the_harness_it_launched(tmp_path, harness):
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="profile canary did not validate\n")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: (requested, "requested"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": harness}},
        autonomy="delegate",
    )
    with pytest.raises(SpawnError, match="^profile canary did not validate$") as error:
        runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert error.value.harness == harness


@pytest.mark.parametrize("failure", ["timeout", "binding"])
def test_a_timed_out_or_unbound_init_agent_names_its_harness(tmp_path, failure):
    import subprocess

    def run(argv, **kwargs):
        if "init-agent" not in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: (requested, "requested"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": "codex"}},
        autonomy="delegate",
    )
    with pytest.raises(SpawnError) as error:
        runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert error.value.harness == "codex"
    assert error.value.status == ("ambiguous" if failure == "timeout" else "refused")
