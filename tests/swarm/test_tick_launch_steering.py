from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER
from scripts.swarm.tick import SpawnError
from tests.swarm.test_tick import _slow_launches
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


def test_resetting_claims_forgets_the_failed_harness(store):
    store.note_launch_failure("sw", "t1", "profile canary did not validate", "codex")
    store.note_launch_failure("sw", "t2", "placement refused")
    assert (store.failed_harness("sw", "t1"), store.failed_harness("sw", "t2")) == ("codex", "")
    store.reset_claims("sw", "t1")
    assert (store.failed_harness("sw", "t1"), store.launch_failure("sw", "t1")) == ("", "")


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
