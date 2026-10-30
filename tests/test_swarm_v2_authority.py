from dataclasses import asdict, replace

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


@pytest.mark.parametrize("owned", [True, False])
def test_first_admission_preserves_or_consumes_the_controller_preclaim(fixture, owned):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    holder = agent.name if owned else "another-holder"
    key = store.key("fixture", "claim", "task")
    store.redis.set(key, holder)
    if not owned:
        with pytest.raises(SwarmError) as caught:
            authority.admit(token, 100)
        assert str(caught.value) == "claim_held"
        assert store.redis.get(key) == holder
        assert authority.current("task") is None
        assert authority.journal("task") == []
        return
    claim = authority.admit(token, 100)
    assert claim.holder == agent.name
    assert claim.execution_id == agent.execution_id
    assert claim.execution_generation == agent.generation
    assert claim.generation == 1
    assert claim.lease_deadline_ms == 1100
    assert store.redis.get(key) == agent.name
    assert authority.current("task") == claim
    assert authority.admit(token, 100) == claim
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
    assert authority.journal("task")[1]["claim"] == {**asdict(first), "state": "fenced"}
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
    assert [row["event"] for row in authority.journal("task")] == [
        "admitted",
        "renewed",
        "released",
        "admitted",
        "completed",
    ]


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
    from scripts.swarm import fencing
    from scripts.swarm_v2.authority import TaskAuthority

    store, authority, controller, clock, start = fixture
    old, old_token = start()
    authority.admit(old_token, 100)
    clock[0] = 1100
    new, token = start("eng-2@fixture")
    second = authority.admit(token, 100)
    journal = authority.journal("task")
    fencing.write(store, "fixture", lambda doc: doc["claims"].pop("task", None))
    store.redis.delete(store.key("fixture", "claim", "task"))
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 100)
    assert str(error.value) == "dependency_unavailable"
    authority = TaskAuthority(store, controller, authority.authorize)
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


def _contend(monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.swarm import fencing

    original = fencing.state.for_store
    attempts = []

    class Racing:
        def __init__(self, inner):
            self.inner = inner

        def get(self, key):
            return self.inner.get(key)

        def create(self, key, value):
            return self.inner.create(key, value)

        def update(self, key, value, revision):
            attempts.append(True)
            raise StateConflict("fixture contention")

        def watch(self, key, from_revision=0):
            return self.inner.watch(key, from_revision)

    def for_store(store, bucket, environ=None):
        return Racing(original(store, bucket, environ))

    monkeypatch.setattr(fencing.state, "for_store", for_store)
    return attempts


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
    interrupted = []
    original = authority._commit

    def commit(*args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            clock[0] = 1100
            authority.admit(new_token, 100)
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "_commit", commit)
    args = {"renew": (200,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "stale_generation"
    assert interrupted == [True]
    assert authority.current("task").execution_id == new.execution_id
    assert authority.current("task").generation == 2
    assert [row["event"] for row in authority.journal("task")] == ["admitted", "fenced", "admitted"]


def test_replay_refuses_regressed_or_inconsistent_state(fixture):
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    old, token = start()
    first = authority.admit(token, 100)
    clock[0] = 1100
    new, token = start("eng-2@fixture")
    authority.admit(token, 100)
    fencing.write(store, "fixture", lambda doc: doc["claims"].update({"task": asdict(first)}))
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
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    old, token = start()
    claim = authority.admit(token, 100)

    def mutate(doc):
        rows = doc["journals"]["task"]
        rows[0] = {**rows[0], "claim": {**rows[0]["claim"], "generation": 4}}

    fencing.write(store, "fixture", mutate)
    with pytest.raises(SwarmError) as error:
        authority.replay("task")
    assert str(error.value) == "journal_conflict"
    assert authority.current("task") == claim


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_controller_takeover_at_commit_denies_old_authority(fixture, monkeypatch, operation):
    from scripts.swarm import fencing
    from scripts.swarm_v2.controller import Controller

    store, authority, controller, clock, start = fixture
    old, token = start()
    first = authority.admit(token, 100)
    interrupted = []
    original = authority._commit

    def commit(*args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            fencing.write(store, "fixture", lambda doc: doc.update(lease=None))
            other = Controller(store, "fixture", [], lambda: True)
            assert other.acquire()
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "_commit", commit)
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
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    if operation == "replay":
        fencing.write(store, "fixture", lambda doc: doc["claims"].pop("task", None))
    attempts = _contend(monkeypatch)
    with pytest.raises(SwarmError) as error:
        if operation == "renew":
            authority.renew(token, 1, 200)
        else:
            authority.replay("task")
    assert str(error.value) == "dependency_unavailable"
    assert len(attempts) == fencing.ATTEMPTS
    assert authority.current("task") == (None if operation == "replay" else claim)
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


def test_delayed_admission_does_not_extend_the_claimant_deadline(fixture, monkeypatch):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    delayed = []
    original = authority._commit

    def commit(*args, **kwargs):
        if not delayed:
            delayed.append(True)
            clock[0] = 1101
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "_commit", commit)
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 100)
    assert str(error.value) == "stale_generation"
    assert delayed == [True]
    assert authority.current("task") is None
    assert authority.journal("task") == []
    assert store.claimant("fixture", "task") is None


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_delayed_renewal_cannot_cross_the_former_lease_deadline(fixture, monkeypatch, operation):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    first = authority.admit(token, 200)
    clock[0] = 1100
    delayed = []
    original = authority._commit

    def commit(*args, **kwargs):
        if not delayed:
            delayed.append(True)
            clock[0] = 1201
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "_commit", commit)
    args = {"renew": (300,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "stale_generation"
    assert delayed == [True]
    assert authority.current("task") == first
    assert [row["event"] for row in authority.journal("task")] == ["admitted"]
    assert store.claimant("fixture", "task") is None


def test_controller_expiry_at_commit_cannot_admit_a_task(fixture, monkeypatch):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    delayed = []
    original = controller.require

    def require():
        if not delayed:
            delayed.append(True)
            clock[0] = controller.held.expires_at + 1
        return original()

    monkeypatch.setattr(controller, "require", require)
    with pytest.raises(SwarmError) as error:
        authority.admit(token, 200)
    assert str(error.value) == "the controller lease is stale"
    assert delayed == [True]
    assert authority.current("task") is None
    assert authority.journal("task") == []
    assert store.claimant("fixture", "task") is None


@pytest.mark.parametrize("operation", ["admit", "renew", "release", "complete"])
def test_worker_credential_expiry_at_commit_preserves_task_state(fixture, monkeypatch, operation):
    from dataclasses import replace
    from datetime import datetime

    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    agent, token = start()
    scope = authority.authorize(token)
    audit = fencing.read(store, "fixture")["grants"][scope.grant_id]
    deadline = int(datetime.fromisoformat(audit["expires_at"]).timestamp() * 1000)
    controller.held = replace(controller.held, expires_at=deadline + 10000)
    fencing.write(store, "fixture", lambda doc: doc.update(lease={**doc["lease"], "expires_at": deadline + 10000}))
    if operation != "admit":
        authority.admit(token, deadline + 20000)
    before = authority.current("task")
    journal = authority.journal("task")
    delayed = []
    original = authority._commit

    def commit(*args, **kwargs):
        if not delayed:
            delayed.append(True)
            clock[0] = deadline
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "_commit", commit)
    clock[0] += 1
    with pytest.raises(SwarmError) as error:
        if operation == "admit":
            authority.admit(token, deadline + 20000)
        elif operation == "renew":
            authority.renew(token, 1, deadline + 20000)
        elif operation == "release":
            authority.release(token, 1)
        else:
            authority.complete(token, 1, {"outcome": "done"})
    assert str(error.value) == "worker credential has expired"
    assert delayed == [True]
    assert authority.current("task") == before
    assert authority.journal("task") == journal
    assert store.claimant("fixture", "task") == (agent.name if before else None)


@pytest.mark.parametrize("record", ["task-authority", "claim-journal"])
@pytest.mark.parametrize("operation", ["claim", "refresh", "release"])
def test_each_distributed_record_blocks_legacy_task_writes(fixture, record, operation):
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    agent, token = start()
    if operation != "claim":
        assert store.claim("fixture", "task", agent.name, 500)
    section = "claims" if record == "task-authority" else "journals"
    value = {"marker": True} if section == "claims" else [{"marker": True}]
    fencing.write(store, "fixture", lambda doc: doc[section].update({"task": value}))
    before = store.redis.get(store.key("fixture", "claim", "task"))
    if operation == "claim":
        result = store.claim("fixture", "task", agent.name, 500)
    elif operation == "refresh":
        result = store.refresh("fixture", "task", agent.name, 500)
    else:
        result = store.release("fixture", "task", agent.name)
    assert result is False
    assert store.redis.get(store.key("fixture", "claim", "task")) == before
    assert fencing.read(store, "fixture")[section]["task"] == value


@pytest.mark.parametrize("record", ["task-authority", "claim-journal", "claim"])
def test_legacy_claim_watches_authority_record_created_at_commit(fixture, monkeypatch, record):
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    raced = []
    original = fencing.authority_held

    def held(store_, slug_, task_):
        if not raced:
            raced.append(True)
            if record == "claim":
                store.redis.set(store.key("fixture", "claim", "task"), "retained-authority")
            elif record == "task-authority":
                fencing.write(store_, slug_, lambda doc: doc["claims"].update({task_: {"marker": True}}))
            else:
                fencing.write(store_, slug_, lambda doc: doc["journals"].update({task_: [{"marker": True}]}))
        return original(store_, slug_, task_)

    monkeypatch.setattr(fencing, "authority_held", held)
    assert store.claim("fixture", "task", "worker", 500) is False
    assert raced == [True]
    assert store.claimant("fixture", "task") == ("retained-authority" if record == "claim" else None)
    if record == "claim":
        assert store.redis.get(store.key("fixture", "claim", "task")) == "retained-authority"
    else:
        assert fencing.authority_held(store, "fixture", "task") is True


@pytest.mark.parametrize("operation", ["refresh", "release"])
def test_legacy_holder_write_watches_successor_replacement(fixture, monkeypatch, operation):
    store, authority, controller, clock, start = fixture
    assert store.claim("fixture", "task", "old", 500)
    original = store.redis.pipeline
    raced = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if not raced:
                raced.append(True)
                store.redis.set(store.key("fixture", "claim", "task"), "successor", px=500)
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    if operation == "refresh":
        result = store.refresh("fixture", "task", "old", 1000)
    else:
        result = store.release("fixture", "task", "old")
    assert result is False
    assert raced == [True]
    assert store.claimant("fixture", "task") == "successor"


def test_one_millisecond_task_lease_is_valid(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 1)
    assert claim.lease_deadline_ms == clock[0] + 1


def test_a_later_retired_attempt_cannot_be_admitted_again(fixture):
    store, authority, controller, clock, start = fixture
    old, old_token = start()
    authority.admit(old_token, 100)
    authority.release(old_token, 1)
    new, token = start("eng-2@fixture")
    authority.admit(token, 100)
    released = authority.release(token, 2)
    journal = authority.journal("task")
    for retired_token in (token, old_token):
        with pytest.raises(SwarmError) as error:
            authority.admit(retired_token, 100)
        assert str(error.value) == "stale_generation"
    with pytest.raises(SwarmError) as error:
        authority.release(token, 2)
    assert str(error.value) == "stale_generation"
    assert authority.current("task") == released
    assert authority.journal("task") == journal


@pytest.mark.parametrize("operation", ["renew", "release", "complete"])
def test_task_expiry_refuses_before_a_delayed_controller_expiry(fixture, monkeypatch, operation):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    clock[0] = claim.lease_deadline_ms
    original = store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def run(*args, **kwargs):
            if any(cmd[0][0] == "EVAL" for cmd in pipe.command_stack):
                clock[0] = controller.held.expires_at
            return execute(*args, **kwargs)

        pipe.execute = run
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    args = {"renew": (100,), "release": (), "complete": ({},)}[operation]
    with pytest.raises(SwarmError) as error:
        getattr(authority, operation)(token, 1, *args)
    assert str(error.value) == "stale_generation"
    assert authority.stale_generation_rejections_total() == 1
    assert authority.current("task") == claim


@pytest.mark.parametrize(
    "operation, record",
    [
        ("renew", "claim"),
        ("renew", "executions"),
        ("renew", "launch-grants"),
        ("renew", "launch-registrations"),
        ("renew", "launch-grants-disabled"),
        ("replay", "task-authority"),
        ("replay", "claim-journal"),
        ("replay", "claim"),
        ("replay", "control-owner"),
    ],
)
def test_concurrent_authority_changes_are_bounded(fixture, monkeypatch, operation, record):
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    journal = authority.journal("task")
    if operation == "replay":
        fencing.write(store, "fixture", lambda doc: doc["claims"].pop("task", None))
    attempts = _contend(monkeypatch)
    with pytest.raises(SwarmError) as error:
        if operation == "renew":
            authority.renew(token, 1, 200)
        else:
            authority.replay("task")
    assert str(error.value) == "dependency_unavailable"
    assert len(attempts) == fencing.ATTEMPTS
    assert authority.current("task") == (None if operation == "replay" else claim)
    assert authority.journal("task") == journal
    assert store.claimant("fixture", "task") == agent.name
    assert record


def test_admission_reads_the_clock_of_its_own_store(fixture, monkeypatch):
    from scripts.swarm import lease

    store, authority, controller, clock, start = fixture
    agent, token = start()
    monkeypatch.setattr(lease, "now_ms", lambda given: clock[0] if given is store else 10**15)

    claim = authority.admit(token, 100)

    assert authority.current("task") == claim
    assert store.claimant("fixture", "task") == agent.name


def test_an_admission_whose_lease_ends_before_its_commit_is_stale(fixture, monkeypatch):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    original = authority._execution

    def late(doc, execution_id):
        found = original(doc, execution_id)
        clock[0] += 100
        return found

    monkeypatch.setattr(authority, "_execution", late)
    with pytest.raises(SwarmError, match="^stale_generation$"):
        authority.admit(token, 100)
    assert authority.current("task") is None


def test_a_release_whose_lease_ends_before_its_commit_is_stale(fixture, monkeypatch):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    original = authority._holder

    def late(scope, generation, previous):
        original(scope, generation, previous)
        clock[0] = previous.lease_deadline_ms

    monkeypatch.setattr(authority, "_holder", late)
    with pytest.raises(SwarmError, match="^stale_generation$"):
        authority.release(token, claim.generation)
    assert authority.current("task") == claim


def test_a_claim_projects_no_holder_from_the_moment_its_lease_ends(fixture, monkeypatch):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    projected = []
    monkeypatch.setattr(store, "project_claim", lambda *args: projected.append(args))

    clock[0] = claim.lease_deadline_ms - 1
    authority._project(claim)
    clock[0] = claim.lease_deadline_ms
    authority._project(claim)

    assert projected == [
        ("fixture", "task", agent.name, claim.lease_deadline_ms),
        ("fixture", "task", None, claim.lease_deadline_ms),
    ]


def test_a_repeated_admission_writes_and_projects_nothing(fixture):
    store, authority, controller, clock, start = fixture
    agent, token = start()
    claim = authority.admit(token, 100)
    store.redis.delete(store.key("fixture", "claim", "task"))

    assert authority.admit(token, 100) == claim
    assert store.claimant("fixture", "task") is None
    assert len(authority.journal("task")) == 1


def _pinned(authority, registration):
    from scripts.swarm_v2.authority import TaskAuthority

    return TaskAuthority(authority.store, authority.controller, lambda token: registration)


def test_an_archived_attempt_is_stale_and_an_unknown_one_is_an_identity_conflict(fixture):
    from scripts.swarm import fencing

    store, authority, controller, clock, start = fixture
    agent, token = start()
    registration = authority.authorize(token)
    fencing.write(
        store,
        "fixture",
        lambda doc: doc["archive"].update({agent.execution_id: doc["executions"].pop(agent.execution_id)}),
    )
    conflicts = fencing.count(store, "fixture", fencing.IDENTITY_CONFLICTS)

    with pytest.raises(SwarmError, match="^stale_generation$"):
        _pinned(authority, registration).admit("pinned", 100)
    unknown = replace(registration, execution_id="exe-unknown")
    with pytest.raises(SwarmError, match="^unknown execution identity; display labels cannot identify attempts$"):
        _pinned(authority, unknown).admit("pinned", 100)
    assert fencing.count(store, "fixture", fencing.IDENTITY_CONFLICTS) == conflicts + 1


def test_task_authority_writes_are_fenced_by_the_controller_lease(fixture, monkeypatch):
    from scripts.swarm import fencing, lease

    store, authority, controller, clock, start = fixture
    agent, token = start()
    registration = authority.authorize(token)
    fencing.write(store, "fixture", lambda doc: doc.update(lease={**doc["lease"], "owner": "usurper"}))
    monkeypatch.setattr(controller, "require", lambda: None)

    with pytest.raises(SwarmError, match=f"^{lease.STALE}$"):
        _pinned(authority, registration).admit("pinned", 100)
    assert authority.current("task") is None
