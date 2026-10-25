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
    from scripts.swarm import reaper, snapshot

    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(reaper, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names, *run: {name: str(tmp_path) for name in names})


def executing(store, agent, backend="local"):
    target = {"pod_namespace": "swarm", "pod_name": "agent"} if backend == "kubernetes" else {}
    fresh = replace(agent, execution_id="", generation=0, runtime_backend=backend, runtime_target=target)
    return store.start_execution("sw", fresh)


def stuck_owner(store, monkeypatch):
    from scripts.swarm import naming, pane, reaper

    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], harness="codex", account="alpha"))
    (reaper.SCRATCH / "home" / naming.scratch_folder("sw", owner.task)).mkdir(parents=True)
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    runtime.errors[owner.name] = pane.provider_error("codex", CAPACITY)

    def nudge_whose_turn_fails(agent, text):
        return FakeRuntime.nudge(runtime, agent, text)

    monkeypatch.setattr(runtime, "nudge", nudge_whose_turn_fails)
    return ledger, runtime, owner


def assert_seat_freed(store, ledger, runtime, owner, at, claims):
    from scripts.swarm import reaper
    from scripts.swarm_ledger import ledger_workspace

    assert owner.name in runtime.killed
    assert runtime.homes[owner.name] == reaper.scratch_homes("sw", owner.task) != []
    assert owner.name not in {agent.name for agent in store.agents("sw")}
    assert store.claimant("sw", owner.task) is None
    assert claims >= 1 and store.claims("sw", owner.task) == claims - 1
    assert store.redis.hget(store.key("sw", resume.RECOVERY), owner.name) is None
    assert (ledger.rows[owner.task]["state"], ledger.rows[owner.task]["claimed_by"]) == ("open", "")
    recovery = [
        row
        for row in transfers.list_transfers(store, "sw")
        if (row["task"], row["predecessor"], row["reason"]) == (owner.task, owner.name, "outage")
    ]
    assert len(recovery) == 1 and not recovery[0]["successor"], recovery
    assert (recovery[0]["avoid_harness"], recovery[0]["at"]) == (owner.harness, at)
    handoff = recovery[0]["handoff"]
    assert "## Next" in handoff and recovery[0]["next"]
    assert f"task {owner.task};" in handoff and f"retired {owner.name} " in handoff
    assert f"folder {ledger_workspace.folder('sw', owner.task)} " in handoff
    assert not store.redis.exists(
        store.key("sw", outage.PANE, owner.name), store.key("sw", outage.PANE_SINCE, owner.name)
    )


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
    from scripts.swarm import idle, provider_probe

    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    monkeypatch.setattr(provider_probe, "check", lambda target, *args: provider_probe.Result(True, "ok"))
    outage.providers(store, "sw", runtime, MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    outage.providers(store, "sw", runtime, 5 * MINUTE)
    assert not outage.open_records("sw")
    for at in (6, 7, 8):
        tick("sw", store, ledger, runtime, at * MINUTE)
        idle.beat(store.redis, "sw", owner.name, "working", at * MINUTE + 1)
    assert runtime.nudged.count(owner.name) == resume.RECOVERY_ATTEMPTS
    assert store.claimant("sw", owner.task) == owner.name
    claims = store.claims("sw", owner.task)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert_seat_freed(store, ledger, runtime, owner, 9 * MINUTE, claims)
    store.update("sw", state="running")
    tick("sw", store, ledger, runtime, 10 * MINUTE)
    successor = next(agent for agent in store.agents("sw") if agent.task == owner.task)
    transfer = runtime.tasks[-1]["transfer"]
    assert (transfer["successor"], transfer["avoid_harness"]) == (successor.name, "codex")
    assert store.seats.occupant(successor.seat).occupant == successor.name
    assert transfer["handoff"].startswith("## Intent") and runtime.errors[owner.name] in transfer["handoff"]


def test_a_provider_error_outlasting_the_hold_window_frees_the_seat_while_the_outage_stays_open(store, monkeypatch):
    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS - 1)
    assert store.claimant("sw", owner.task) == owner.name
    assert owner.name not in runtime.killed
    claims = store.claims("sw", owner.task)
    tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS)
    assert_seat_freed(store, ledger, runtime, owner, 2 * MINUTE + outage.HOLD_LIMIT_MS, claims)


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


def test_a_working_heartbeat_keeps_an_agent_whose_pane_still_shows_an_old_provider_error(store, monkeypatch):
    from scripts.swarm import idle

    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    at = 2 * MINUTE + outage.HOLD_LIMIT_MS
    idle.beat(store.redis, "sw", owner.name, "working", at - 1)
    tick("sw", store, ledger, runtime, at)
    assert store.claimant("sw", owner.task) == owner.name
    assert owner.name not in runtime.killed


@pytest.mark.parametrize("pinned", [False, True])
def test_a_spawn_after_a_freed_seat_takes_another_harness_when_one_has_room(tmp_path, monkeypatch, pinned):
    from tests.swarm.test_capacity import _runtime_probe, account

    rows = [account("a", sessions=2), account("cx", harness="codex")]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, rows)
    task = {"id": "p", "title": "Continue", "transfer": {"avoid_harness": "codex"}}
    if pinned:
        launch = {"profile": "planner", "harness": "codex", "model": "gpt-5.5", "effort": "high", "account": "cx"}
        task["handoff_envelope"] = {"launch": launch}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    runtime.spawn(config, "plan", "planner@a1b2c3-0002", {"id": "q", "title": "Plan"})
    assert seen == [("p", "claude", "a"), ("q", "codex", "cx")]


def test_a_spawn_after_a_freed_seat_keeps_the_same_harness_when_no_other_has_room(tmp_path, monkeypatch):
    from tests.swarm.test_capacity import _runtime_probe, account

    rows = [account("a", cap=0), account("cx", harness="codex")]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, rows)
    task = {"id": "p", "title": "Continue", "transfer": {"avoid_harness": "codex"}}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert seen == [("p", "codex", "cx")]


def stuck_verdict(store, monkeypatch, *, lane="eng", error="provider unavailable", exhausted=True):
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.tick import _stuck_on_provider

    _, _, owner = stuck_owner(store, monkeypatch)
    now = outage.HOLD_LIMIT_MS
    outage.mark_pane(store, "sw", owner.name, error, 0)
    observed = PaneObservation("idle", provider_error=error)
    return owner, lambda: _stuck_on_provider("sw", store, replace(owner, lane=lane), observed, exhausted, now)


def test_spent_attempts_with_the_error_on_the_pane_free_a_lane_seat(store, monkeypatch):
    _, stuck = stuck_verdict(store, monkeypatch)
    assert stuck() is True


@pytest.mark.parametrize(("lane", "error"), [("master", "provider unavailable"), ("eng", "")])
def test_the_master_seat_and_a_cleared_pane_are_never_freed(store, monkeypatch, lane, error):
    _, stuck = stuck_verdict(store, monkeypatch, lane=lane, error=error)
    assert stuck() is False


def test_an_operator_prompt_inside_the_quiet_window_keeps_the_seat(store, monkeypatch):
    from scripts.swarm import idle

    owner, stuck = stuck_verdict(store, monkeypatch)
    idle.prompted(store.redis, "sw", owner.name, outage.HOLD_LIMIT_MS - 1)
    assert stuck() is False


def test_a_declared_wait_keeps_the_seat_past_the_hold_window(store, monkeypatch):
    from scripts.swarm import idle

    owner, stuck = stuck_verdict(store, monkeypatch, exhausted=False)
    assert stuck() is True
    now = outage.HOLD_LIMIT_MS
    idle.declare_wait(store.redis, "sw", owner.name, now + MINUTE, "checks", now - 1)
    assert stuck() is False


def test_a_refused_retire_keeps_the_agent_and_its_claim_for_the_next_tick(store, monkeypatch):
    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 2 * MINUTE)
    runtime.stuck.add(owner.name)
    actions = tick("sw", store, ledger, runtime, 2 * MINUTE + outage.HOLD_LIMIT_MS)
    assert f"could not retire {owner.name}, stuck on a provider error, retrying next tick" in actions
    assert owner.name in {agent.name for agent in store.agents("sw")}
    assert store.claimant("sw", owner.task) == owner.name


def test_a_transfer_names_an_avoided_harness_only_when_given_one(store):
    _, _, agents = three_owners(store)
    assert "avoid_harness" not in transfers.record(store, "sw", agents[0], "exit", "", 1)
    assert transfers.record(store, "sw", agents[0], "outage", "", 1, "codex")["avoid_harness"] == "codex"


def test_a_freed_seat_carries_the_handoff_its_task_already_has(store, monkeypatch):
    ledger, runtime, owner = stuck_owner(store, monkeypatch)
    handoff = "## Next\nContinue from the saved proof.\n"
    store.put_handoff("sw", owner.task, handoff, seat=owner.seat)
    transfer = resume.free_seat(store, "sw", owner, MINUTE)
    assert (transfer["handoff"], transfer["avoid_harness"]) == (handoff, "codex")
