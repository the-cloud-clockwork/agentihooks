from dataclasses import replace

import pytest

from scripts.swarm.store import AgentRecord, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def fixture(monkeypatch):
    from tests.sv2_ctl02_cases import build

    return build(monkeypatch)


def test_admission_persists_an_exclusive_task_owner(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    assert claim.holder == agent.name
    assert claim.generation == 1
    assert claim.execution_id == agent.execution_id
    assert claim.execution_generation == 1
    assert claim.lease_deadline_ms == 1100
    assert claim.state == "active"
    assert authority.current("task") == claim
    assert store.claimant("fixture", "task") == agent.name
    assert authority.admit(token, 100) == claim
    _, contender = start("eng-2@fixture")
    with pytest.raises(SwarmError) as error:
        authority.admit(contender, 100)
    assert str(error.value) == "claim_held"
    assert authority.current("task") == claim
    assert len(authority.journal("task")) == 1


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_partitioned_worker_cannot_mutate_its_successor(fixture, operation):
    store, authority, controller, clock, start = fixture
    old, old_token = start()
    first = authority.admit(old_token, 100)
    new, new_token = start("eng-2@fixture")
    clock[0] = 1100
    successor = authority.admit(new_token, 100)
    assert successor.generation == 2
    assert successor.execution_id == new.execution_id
    assert [row["event"] for row in authority.journal("task")] == ["admitted", "fenced", "admitted"]
    before = authority.journal("task")
    args = {"renew": (100,), "release": (), "complete": ({"outcome": "done"},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(old_token, first.generation, *args)
    assert str(error.value) == "stale_generation"
    assert authority.current("task") == successor
    assert authority.journal("task") == before
    assert store.claimant("fixture", "task") == new.name
    assert authority.stale_generation_rejections_total() == 1


def test_current_holder_renews_releases_and_completes_by_generation(fixture):
    store, authority, controller, clock, start = fixture
    old, token = start()
    first = authority.admit(token, 100)
    clock[0] = 1050
    renewed = authority.renew(token, first.generation, 200)
    assert renewed.lease_deadline_ms == 1250
    assert renewed.execution_id == old.execution_id
    released = authority.release(token, 1)
    assert released.state == "released"
    assert store.claimant("fixture", "task") is None
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, 100)
    assert str(error.value) == "stale_generation"
    new, token = start("eng-2@fixture")
    next_claim = authority.admit(token, 100)
    assert next_claim.generation == 2
    done = authority.complete(token, 2, {"outcome": "done"})
    assert done.state == "completed"
    assert done.result == {"outcome": "done"}
    assert authority.complete(token, 2, {"outcome": "done"}) == done
    assert store.claimant("fixture", "task") is None


def test_legacy_writers_cannot_change_distributed_claims(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    assert store.refresh("fixture", "task", agent.name, 500) is False
    assert store.release("fixture", "task", agent.name) is False
    assert authority.current("task") == claim
    assert store.claimant("fixture", "task") == agent.name
    authority.release(token, 1)
    assert store.claim("fixture", "task", agent.name, 500) is False
    assert store.claim("fixture", "local", agent.name, 500) is True
    assert store.refresh("fixture", "local", agent.name, 500) is True
    assert store.release("fixture", "local", agent.name) is True


def test_replay_recovers_highest_generation_without_reviving_old_worker(fixture):
    store, authority, controller, clock, start = fixture
    old, old_token = start()
    authority.admit(old_token, 100)
    clock[0] = 1100
    new, token = start("eng-2@fixture")
    second = authority.admit(token, 100)
    journal = authority.journal("task")
    store.redis.delete(store.key("fixture", "task-authority", "task"), store.key("fixture", "claim", "task"))
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 100)
    assert str(error.value) == "dependency_unavailable"
    assert authority.replay("task") == second
    assert authority.replay("task") == second
    assert authority.journal("task") == journal
    with pytest.raises(SwarmError) as error:
        authority.renew(old_token, 1, 100)
    assert str(error.value) == "stale_generation"
    assert store.claimant("fixture", "task") == new.name
    controller.admission_enabled = False
    _, third = start_after_rollback(controller, store, start)
    with pytest.raises(SwarmError) as error:
        authority.admit(third, 100)
    assert str(error.value) == "controller admission is disabled until reconciliation completes"
    assert authority.current("task") == second
    assert authority.journal("task") == journal
    assert store.claim("fixture", "local", "local-worker", 100)


def start_after_rollback(controller, store, start):
    controller.admission_enabled = True
    agent, token = start("eng-3@fixture")
    controller.admission_enabled = False
    return agent, token


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_expired_owner_cannot_revive_before_successor(fixture, operation):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    authority.admit(token, 100)
    clock[0] = 1100
    args = {"renew": (200,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "stale_generation"
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 100)
    assert str(error.value) == "stale_generation"
    assert authority.current("task").generation == 1


def test_worker_scope_and_retired_execution_are_checked(fixture):
    store, authority, controller, clock, start = fixture
    old, token = start()
    authority.admit(token, 100)
    valid = authority.authorize(token)
    for field in ("swarm_id", "task_id", "seat_id"):
        authority.authorize = lambda token, field=field: replace(valid, **{field: "other"})
        with pytest.raises(SwarmError) as error:
            authority.renew(token, 1, 100)
        assert str(error.value) == "forbidden_scope"
    authority.authorize = lambda token: valid
    controller.admit(AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat=old.seat), old.execution_id)
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, 100)
    assert str(error.value) == "stale_generation"


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_takeover_at_commit_rejects_former_holder(fixture, monkeypatch, operation):
    store, authority, controller, clock, start = fixture
    old, token = start()
    authority.admit(token, 100)
    new, new_token = start("eng-2@fixture")
    original = store.redis.pipeline
    interrupted = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if not interrupted and any(
                cmd[0][0] == "SET" and cmd[0][1] == store.key("fixture", "task-authority", "task")
                for cmd in pipe.command_stack
            ):
                interrupted.append(True)
                clock[0] = 1100
                authority.admit(new_token, 100)
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    args = {"renew": (200,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "stale_generation"
    assert interrupted == [True]
    assert authority.current("task").execution_id == new.execution_id
    assert authority.current("task").generation == 2
    assert [row["event"] for row in authority.journal("task")] == ["admitted", "fenced", "admitted"]


def test_replay_refuses_regressed_or_inconsistent_state(fixture):
    store, authority, controller, clock, start = fixture
    old, token = start()
    first = authority.admit(token, 100)
    clock[0] = 1100
    new, token = start("eng-2@fixture")
    authority.admit(token, 100)
    import json
    from dataclasses import asdict

    store.redis.set(store.key("fixture", "task-authority", "task"), json.dumps(asdict(first)))
    with pytest.raises(SwarmError) as error:
        authority.replay("task")
    assert str(error.value) == "journal_conflict"
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 2, 100)
    assert str(error.value) == "journal_conflict"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_acceptance_repeats_independently(case):
    from tests.sv2_ctl02_cases import run_case

    first, second = run_case(case), run_case(case)
    assert first["state"] == second["state"] == "passed"
    assert first["observed"]["generation"] == second["observed"]["generation"] == 2
    assert first["observed"]["execution_id"] != second["observed"]["execution_id"]
    assert first["input_sha256"] == second["input_sha256"]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_lease_and_generation_inputs_do_not_mutate(fixture, value):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    with pytest.raises(SwarmError) as error:
        authority.admit(token, value)
    assert str(error.value) == "invalid_request"
    claim = authority.admit(token, 100)
    for operation, args in (("renew", (100,)), ("release", ()), ("complete", ({},))):
        with pytest.raises(SwarmError) as error:
            getattr(authority, operation)(token, value, *args)
        assert str(error.value) == "invalid_request"
        assert authority.current("task") == claim
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, value)
    assert str(error.value) == "invalid_request"


def test_release_and_completion_require_exact_payload_and_identity(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    assert authority.current("task") is None
    assert authority.replay("task") is None
    assert authority.stale_generation_rejections_total() == 0
    assert store.claim("fixture", "task", "local-holder", 100)
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 100)
    assert str(error.value) == "claim_held"
    assert store.release("fixture", "task", "local-holder")
    authority.admit(token, 100)
    with pytest.raises(SwarmError) as error:
        authority.release(token, 2)
    assert str(error.value) == "stale_generation"
    done = authority.complete(token, 1, {"outcome": "done"})
    with pytest.raises(SwarmError) as error:
        authority.complete(token, 1, {"outcome": "different"})
    assert str(error.value) == "operation_conflict"
    assert authority.current("task") == done
    assert len(authority.journal("task")) == 2
    _, contender = start("eng-2@fixture")
    with pytest.raises(SwarmError) as error:
        authority.admit(contender, 100)
    assert str(error.value) == "claim_held"


def test_retired_execution_grant_is_counted_and_foreign_result_is_refused(fixture):
    store, authority, controller, clock, start = fixture
    old, token = start()
    authority.admit(token, 100)
    previous = authority.authorize
    authority.authorize = lambda token: None
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, 100)
    assert str(error.value) == "forbidden_scope"
    authority.authorize = previous
    new, new_token = start(old.seat, old.execution_id)
    clock[0] = 1100
    second = authority.admit(new_token, 100)
    assert second.generation == new.generation == 2
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, 100)
    assert str(error.value) == "stale_generation"
    assert authority.stale_generation_rejections_total() == 1
    assert authority.current("task") == second


def test_journal_replay_refuses_changed_generation(fixture):
    import json

    store, authority, controller, clock, start = fixture
    old, token = start()
    claim = authority.admit(token, 100)
    row = authority.journal("task")[0]
    row["claim"]["generation"] = 4
    store.redis.lset(store.key("fixture", "claim-journal", "task"), 0, json.dumps(row))
    with pytest.raises(SwarmError) as error:
        authority.replay("task")
    assert str(error.value) == "journal_conflict"
    assert authority.current("task") == claim


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_controller_takeover_at_commit_denies_old_authority(fixture, monkeypatch, operation):
    store, authority, controller, clock, start = fixture
    old, token = start()
    first = authority.admit(token, 100)
    original = store.redis.pipeline
    interrupted = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if not interrupted and any(
                cmd[0][0] == "SET" and cmd[0][1] == store.key("fixture", "task-authority", "task")
                for cmd in pipe.command_stack
            ):
                interrupted.append(True)
                store.redis.delete(store.key("fixture", "control-owner"))
                from scripts.swarm_v2.controller import Controller

                other = Controller(store, "fixture", [], lambda: True)
                assert other.acquire()
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    args = {"renew": (200,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "the controller lease is stale"
    assert authority.current("task") == first
    assert len(authority.journal("task")) == 1


def test_invalid_worker_grant_error_preserves_the_claim(fixture):
    from scripts.swarm_v2.auth_context import GrantRefused

    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)

    def revoked(token):
        raise GrantRefused("unauthenticated", "fixture credential revoked")

    authority.authorize = revoked
    with pytest.raises(GrantRefused) as error:
        authority.release(token, 1)
    assert str(error.value) == "fixture credential revoked"
    assert authority.current("task") == claim
    assert authority.stale_generation_rejections_total() == 0


@pytest.mark.parametrize("operation", ["renew", "replay"])
def test_continuous_claim_contention_is_bounded(fixture, monkeypatch, operation):
    from redis.exceptions import WatchError

    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    original = store.redis.pipeline
    attempts = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if any(
                cmd[0][0] == "SET" and cmd[0][1] == store.key("fixture", "task-authority", "task")
                for cmd in pipe.command_stack
            ):
                attempts.append(True)
                raise WatchError("fixture contention")
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with pytest.raises(SwarmError) as error:
        if operation == "renew":
            authority.renew(token, 1, 200)
        else:
            authority.replay("task")
    assert str(error.value) == "dependency_unavailable"
    assert len(attempts) == 5
    assert authority.current("task") == claim
    assert len(authority.journal("task")) == 1


def test_worker_scope_cannot_change_during_commit(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    scope = authority.authorize(token)
    calls = []

    def grant(token):
        calls.append(True)
        return scope if len(calls) == 1 else replace(scope, grant_id="other")

    authority.authorize = grant
    with pytest.raises(SwarmError) as error:
        authority.renew(token, 1, 200)
    assert str(error.value) == "forbidden_scope"
    assert authority.current("task") == claim
