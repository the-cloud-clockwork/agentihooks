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
    except SwarmError as error:
        refusal = str(error)
        assert len(attempts) == 5
        assert authority.current("task") == claim
        assert store.execution_occupants("fixture").get(candidate.seat) is None
    assert refusal == "", f"writer={writer}; operation={operation}; attempts={len(attempts)}; refusal={refusal}"
