from dataclasses import replace
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation", ["execution", "admission", "renewal"])
@pytest.mark.parametrize("writer", ["none", "ledger", "tick", "registry"])
def test_admission_commits_during_controller_writes(monkeypatch, operation, writer):
    from scripts.swarm.controller import FencedLedger, keep_tick, take_tick_lock
    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.sv2_ctl02_cases import build

    store, authority, controller, clock, start = build(monkeypatch)
    agent, token = start()
    authority.authorize(token)
    claim = authority.admit(token, 100) if operation == "renewal" else None
    lock = take_tick_lock(store, "fixture", controller.held, 1000)
    ledger = FencedLedger(store, "fixture", controller.held, SimpleNamespace(read=lambda: None))
    original = store.redis.pipeline
    attempts = []
    active = [False]

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def overlapping(*args, **kwargs):
            if pipe.watching and not active[0] and writer != "none":
                active[0] = True
                try:
                    attempts.append(writer)
                    clock[0] += 1
                    if writer == "ledger":
                        ledger.read()
                    elif writer == "tick":
                        keep_tick(store, "fixture", controller.held, lock, 1000)
                    else:
                        store.put_agent("fixture", store.execution("fixture", agent.execution_id))
                finally:
                    active[0] = False
            return execute(*args, **kwargs)

        pipe.execute = overlapping
        return pipe

    candidate = AgentRecord(store.next_name("fixture", "eng"), "eng", "other", seat="eng-2@fixture")
    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    clock[0] += 10
    refusal = ""
    try:
        if operation == "execution":
            admitted = controller.admit(candidate)
            assert admitted.generation == 1
        elif operation == "admission":
            admitted = authority.admit(token, 100)
            assert admitted.execution_id == agent.execution_id
        else:
            renewed = authority.renew(token, claim.generation, 100)
            assert renewed == replace(claim, lease_deadline_ms=renewed.lease_deadline_ms)
            assert renewed.lease_deadline_ms > claim.lease_deadline_ms
            assert authority.current("task") == renewed
    except SwarmError as error:
        refusal = str(error)
        assert len(attempts) == 5
        assert authority.current("task") == claim
        assert store.execution_occupants("fixture").get(candidate.seat) is None
    assert refusal == "", f"writer={writer}; operation={operation}; attempts={len(attempts)}; refusal={refusal}"


@pytest.mark.parametrize("change", ["owner", "epoch", "expiry", "missing"])
def test_execution_admission_refuses_changed_controller_at_commit(monkeypatch, change):
    import json

    from scripts.swarm.execution import ADMIT
    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.sv2_ctl02_cases import build

    store, authority, controller, clock, start = build(monkeypatch)
    agent, token = start()
    candidate = AgentRecord(store.next_name("fixture", "eng"), "eng", "other", seat="eng-2@fixture")
    key = store.key("fixture", "control-owner")
    before = store.executions("fixture", agent.seat)
    original = store.redis.pipeline
    changed = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if any(command[0][:2] == ("EVAL", ADMIT) for command in pipe.command_stack):
                changed.append(change)
                leader = json.loads(store.redis.get(key))
                if change == "missing":
                    store.redis.delete(key)
                elif change == "expiry":
                    clock[0] = leader["expires_at"]
                else:
                    leader[change] = "another" if change == "owner" else leader["epoch"] + 1
                    store.redis.set(key, json.dumps(leader))
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with pytest.raises(SwarmError, match="^the controller lease is stale$"):
        controller.admit(candidate)
    assert changed == [change]
    assert store.executions("fixture", agent.seat) == before
    assert candidate.seat not in store.execution_occupants("fixture")
    assert not store.redis.hexists(store.key("fixture", "agents"), candidate.name)
    assert not store.redis.hexists(store.key("fixture", "state-since"), candidate.name)
    assert len(store.redis.hgetall(store.key("fixture", "controller-intents"))) == 1


def test_fenced_admission_preserves_projections_timestamps_and_other_agents(monkeypatch):
    from scripts.swarm.store import AgentRecord
    from tests.sv2_ctl02_cases import build

    store, authority, controller, clock, start = build(monkeypatch)
    legacy = AgentRecord("XXXX", "eng", "legacy")
    store.put_agent("fixture", legacy)
    candidate = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-1@fixture")
    admitted_at = clock[0]
    admitted = controller.admit(candidate)
    assert {agent.name: agent for agent in store.agents("fixture")} == {
        legacy.name: legacy,
        admitted.name: admitted,
    }
    assert int(store.redis.hget(store.key("fixture", "state-since"), admitted.name)) == admitted_at
