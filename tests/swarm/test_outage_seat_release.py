from dataclasses import replace

import pytest

from scripts.handoff import transfers
from scripts.swarm import outage, resume
from scripts.swarm.tick import tick
from tests.swarm.test_outage_resume import MINUTE, rows_of, three_owners
from tests.swarm.test_tick import FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit

CAPACITY = "■ Selected model is at capacity. Please try a different model."


@pytest.fixture(autouse=True)
def outage_home(tmp_path, monkeypatch):
    from scripts.swarm import snapshot

    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})


def executing(store, agent, backend="local"):
    target = {"pod_namespace": "swarm", "pod_name": "agent"} if backend == "kubernetes" else {}
    fresh = replace(agent, execution_id="", generation=0, runtime_backend=backend, runtime_target=target)
    return store.start_execution("sw", fresh)


def stuck_owner(store, monkeypatch):
    from scripts.swarm import pane

    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], harness="codex", account="alpha"))
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    runtime.errors[owner.name] = pane.provider_error("codex", CAPACITY)
    monkeypatch.setattr(runtime, "nudge", lambda agent, text: FakeRuntime.nudge(runtime, agent, text))
    return ledger, runtime, owner


def assert_seat_freed(store, ledger, runtime, owner):
    assert owner.name in runtime.killed
    assert owner.name not in {agent.name for agent in store.agents("sw")}
    assert store.claimant("sw", owner.task) is None
    assert (ledger.rows[owner.task]["state"], ledger.rows[owner.task]["claimed_by"]) == ("open", "")
    recovery = [
        row
        for row in transfers.list_transfers(store, "sw")
        if (row["task"], row["predecessor"], row["reason"]) == (owner.task, owner.name, "outage")
    ]
    assert len(recovery) == 1 and not recovery[0]["successor"], recovery


@pytest.mark.parametrize(("backend", "recovered"), [("local", True), ("kubernetes", False)])
def test_an_execution_without_task_authority_owns_its_recovery_only_when_local(store, backend, recovered):
    ledger, runtime, agents = three_owners(store)
    owner = executing(store, agents[0], backend)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert (owner.name in runtime.nudged) is recovered


def test_an_owner_whose_turns_still_fail_after_the_probe_passes_frees_its_seat_one_tick_after_its_attempts(
    store, monkeypatch
):
    from scripts.swarm import provider_probe

    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    monkeypatch.setattr(provider_probe, "check", lambda target, *args: provider_probe.Result(True, "ok"))
    outage.providers(store, "sw", runtime, MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    outage.providers(store, "sw", runtime, 5 * MINUTE)
    assert not outage.open_records("sw")
    for at in (6, 7, 8):
        tick("sw", store, ledger, runtime, at * MINUTE)
    assert runtime.nudged.count(owner.name) == resume.RECOVERY_ATTEMPTS
    assert store.claimant("sw", owner.task) == owner.name
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert_seat_freed(store, ledger, runtime, owner)


def test_a_provider_error_outlasting_the_hold_window_frees_the_seat_while_the_outage_stays_open(store, monkeypatch):
    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS - 1)
    assert store.claimant("sw", owner.task) == owner.name
    assert owner.name not in runtime.killed
    tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS)
    assert_seat_freed(store, ledger, runtime, owner)


def test_a_cleared_provider_error_restarts_the_hold_window(store, monkeypatch):
    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    error = runtime.errors[owner.name]
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    runtime.errors.pop(owner.name)
    tick("sw", store, ledger, runtime, 3 * MINUTE)
    runtime.errors[owner.name] = error
    tick("sw", store, ledger, runtime, 4 * MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS)
    assert store.claimant("sw", owner.task) == owner.name
    assert owner.name not in runtime.killed
