import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-LDG-02"


@pytest.fixture
def world(monkeypatch):
    from tests.sv2_ldg02_cases import World

    return World(monkeypatch)


@pytest.fixture
def worker(world):
    agent, token = world.start()
    assert world.register(agent, token)[0] == 200
    return agent, token


def stored(world, agent):
    raw = world.store.redis.hget(world.store.key("fixture", "heartbeats"), agent.execution_id)
    return json.loads(raw) if raw else None


def test_registration_validates_the_grant_then_admits_the_task_claim(world):
    agent, token = world.start()
    status, ack = world.register(agent, token)
    assert status == 200
    claim = world.tasks.current("task")
    assert (claim.holder, claim.execution_id, claim.generation) == (agent.name, agent.execution_id, 1)
    registration = world.grants.registration("fixture", agent.execution_id)
    assert ack == {
        "schema_version": "2.1",
        "execution_id": agent.execution_id,
        "task_id": "task",
        "task_generation": 1,
        "controller_epoch": 1,
        "owner_identity": agent.name,
        "lease_deadline_ms": 1100,
        "lease_deadline": "1970-01-01T00:00:01.100Z",
        "command_watermark": 0,
        "archive_watermark": 0,
        "grant_id": registration.grant_id,
        "registered_at": "1970-01-01T00:00:01Z",
    }
    world.clock[0] += 30
    assert world.register(agent, token) == (200, ack)
    assert world.tasks.current("task") == claim


def test_registration_with_a_forged_grant_admits_nothing(world):
    agent, token = world.start()
    status, refusal = world.register(agent, token[:-2] + ("AA" if token[-2:] != "AA" else "BB"))
    assert (status, refusal["error_class"], refusal["retry"]) == (401, "unauthenticated", "new_request")
    assert world.grants.registration("fixture", agent.execution_id) is None
    assert world.tasks.current("task") is None


def test_registration_while_another_execution_holds_the_task_is_stale(world, worker):
    claim = world.tasks.current("task")
    rival, rival_token = world.start("eng-2@fixture")
    status, refusal = world.register(rival, rival_token)
    assert (status, refusal["error_class"], refusal["message"]) == (
        409,
        "stale_generation",
        "another execution holds this task",
    )
    assert world.tasks.current("task") == claim


def test_a_worker_credential_expired_by_server_time_is_unauthenticated(world):
    from scripts.swarm_v2.api.executions import ExecutionsAPI

    api = ExecutionsAPI(world.grants, world.tasks, 600_000)
    agent, token = world.start()
    path = f"/v2/executions/{agent.execution_id}/heartbeat"
    assert (
        api.route(
            "POST", "/v2/executions/register", f"Bearer {token}", {"execution_id": agent.execution_id, "generation": 1}
        )[0]
        == 200
    )
    for now in (150_000, 301_000):
        world.clock[0] = now
        assert world.controller.renew()
    world.grants.clock = lambda: 300.0
    claim = world.tasks.current("task")
    status, refusal = api.route("PUT", path, f"Bearer {token}", world.beat(agent, 1))
    assert (status, refusal["error_class"], refusal["message"]) == (
        401,
        "unauthenticated",
        "the worker credential has expired",
    )
    assert world.tasks.current("task") == claim
    assert stored(world, agent) is None


def test_an_unavailable_store_is_reported_as_a_dependency_failure(world, worker, monkeypatch):
    from redis.exceptions import ConnectionError as StoreLost

    agent, token = worker

    def lost(*args):
        raise StoreLost("fixture store lost")

    monkeypatch.setattr(world.store.redis, "hget", lost)
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"], refusal["retry"]) == (503, "dependency_unavailable", "same_request")
    assert refusal["message"] == "the execution store is unavailable"


def test_a_heartbeat_before_registration_is_unauthenticated(world):
    from scripts.swarm_v2.api.executions import heartbeat_rejections

    agent, token = world.start()
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (401, "unauthenticated")
    assert refusal["operation_id"] == "heartbeat-1"
    assert heartbeat_rejections(world.store, "fixture") == {"unauthenticated": 1}
    assert world.tasks.current("task") is None


def test_a_heartbeat_renews_its_own_lease_from_server_time_only(world, worker):
    agent, token = worker
    other, other_token = world.start("eng-2@fixture", "other")
    world.register(other, other_token)
    other_claim = world.tasks.current("other")
    world.clock[0] += 40
    resources = {"cpu_millicores": 64000, "memory_bytes": 549755813888}
    body = world.beat(agent, 5, archive_watermark=12, resources=resources, lease_deadline_ms=99999999)
    status, ack = world.put(agent.execution_id, token, body)
    assert status == 200
    assert ack == {
        "schema_version": "2.1",
        "execution_id": agent.execution_id,
        "task_id": "task",
        "task_generation": 1,
        "controller_epoch": 1,
        "owner_identity": agent.name,
        "lease_deadline_ms": 1140,
        "lease_deadline": "1970-01-01T00:00:01.140Z",
        "command_watermark": 0,
        "archive_watermark": 12,
        "renewal_sequence": 5,
    }
    assert world.tasks.current("task").lease_deadline_ms == 1140
    assert world.tasks.current("other") == other_claim
    record = stored(world, agent)
    assert record["resources"] == resources
    assert (record["renewal_sequence"], record["state"], record["observed_at"]) == (5, "working", body["observed_at"])
    assert (record["archive_watermark"], record["accepted_at_ms"], record["ack"]) == (12, 1040, ack)
    marker = world.store.key("fixture", "heartbeat-sequence", agent.execution_id)
    assert fence_of(world, marker) == {"sequence": 5, "digest": record["digest"]}


def test_a_forged_url_subject_cannot_touch_another_execution(world, worker):
    from scripts.swarm_v2.api.executions import heartbeat_rejections

    agent, token = worker
    other, other_token = world.start("eng-2@fixture", "other")
    world.register(other, other_token)
    assert world.put(other.execution_id, other_token, world.beat(other, 1))[0] == 200
    before = world.protected()
    for body in (world.beat(other, 2), world.beat(agent, 2)):
        status, refusal = world.put(other.execution_id, token, body)
        assert (status, refusal["error_class"], refusal["retry"]) == (403, "forbidden_scope", "new_request")
        assert other.execution_id not in refusal["message"]
    assert world.protected() == before
    assert heartbeat_rejections(world.store, "fixture") == {"forbidden_scope": 2}


@pytest.mark.parametrize("field", ["execution_id", "task_id", "owner_identity"])
def test_authority_outside_the_registration_is_forbidden(world, worker, field):
    agent, token = worker
    body = world.beat(agent, 1)
    body["authority"][field] = "someone-else"
    before = world.protected()
    status, refusal = world.put(agent.execution_id, token, body)
    assert (status, refusal["error_class"]) == (403, "forbidden_scope")
    assert world.protected() == before


def test_a_heartbeat_from_another_controller_epoch_is_stale(world, worker):
    agent, token = worker
    assert world.controller.release() and world.controller.acquire()
    assert world.controller.held.epoch == 2
    for epoch in (1, 3):
        body = world.beat(agent, 1)
        body["authority"]["controller_epoch"] = epoch
        status, refusal = world.put(agent.execution_id, token, body)
        assert (status, refusal["error_class"]) == (409, "stale_generation")
    assert stored(world, agent) is None
    assert world.put(agent.execution_id, token, world.beat(agent, 1))[0] == 200


def test_a_heartbeat_without_a_controller_lease_is_unavailable(world, worker):
    agent, token = worker
    body = world.beat(agent, 1)
    world.controller.held = None
    status, refusal = world.put(agent.execution_id, token, body)
    assert (status, refusal["error_class"], refusal["retry"]) == (503, "dependency_unavailable", "same_request")
    assert stored(world, agent) is None


def test_a_heartbeat_for_another_task_generation_is_stale(world, worker):
    agent, token = worker
    claim = world.tasks.current("task")
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1, generation=2))
    assert (status, refusal["error_class"]) == (409, "stale_generation")
    assert world.tasks.current("task") == claim
    assert stored(world, agent) is None


def test_a_superseded_execution_cannot_heartbeat(world, worker):
    agent, token = worker
    world.start(previous=agent.execution_id)
    before = world.protected()
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (409, "stale_generation")
    assert refusal["message"] == "launch grant is for a superseded execution"
    assert world.protected() == before


def test_controller_admission_disabled_refuses_renewal_as_unavailable(world, worker):
    agent, token = worker
    world.controller.admission_enabled = False
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (503, "dependency_unavailable")
    assert refusal["message"] == "controller admission is disabled until reconciliation completes"
    assert stored(world, agent) is None


@pytest.mark.parametrize(
    "change",
    [
        {"renewal_sequence": -1},
        {"state": "dancing"},
        {"archive_watermark": -1},
        {"resources": "plenty"},
        {"resources": {"cpu_millicores": "lots"}},
        {"schema_version": "3.0"},
    ],
)
def test_a_malformed_heartbeat_is_invalid(world, worker, change):
    agent, token = worker
    status, refusal = world.put(agent.execution_id, token, {**world.beat(agent, 1), **change})
    assert (status, refusal["error_class"]) == (400, "invalid_request")
    assert stored(world, agent) is None


def test_a_heartbeat_body_that_is_not_an_object_is_invalid(world, worker):
    agent, token = worker
    status, refusal = world.put(agent.execution_id, token, ["heartbeat"])
    assert (status, refusal["error_class"], refusal["operation_id"]) == (400, "invalid_request", "unknown")


def test_a_replayed_heartbeat_returns_its_acknowledgement_and_writes_nothing(world, worker):
    from scripts.swarm_v2.api.executions import ExecutionsAPI

    agent, token = worker
    _, accepted = world.put(agent.execution_id, token, world.beat(agent, 5, archive_watermark=12))
    record, claim = stored(world, agent), world.tasks.current("task")
    world.clock[0] += 50
    restarted = ExecutionsAPI(world.grants, world.tasks, 100)
    path = f"/v2/executions/{agent.execution_id}/heartbeat"
    replay = restarted.route("PUT", path, f"Bearer {token}", world.beat(agent, 5, archive_watermark=12))
    assert replay == (200, accepted)
    assert (stored(world, agent), world.tasks.current("task")) == (record, claim)


@pytest.mark.parametrize("sequence, change", [(3, {}), (5, {"state": "waiting"})])
def test_an_out_of_order_heartbeat_moves_nothing_backward(world, worker, sequence, change):
    from scripts.swarm_v2.api.executions import heartbeat_rejections_total

    agent, token = worker
    world.put(agent.execution_id, token, world.beat(agent, 5, archive_watermark=12))
    record, claim = stored(world, agent), world.tasks.current("task")
    world.clock[0] += 50
    status, refusal = world.put(agent.execution_id, token, {**world.beat(agent, sequence), **change})
    assert (status, refusal["error_class"], refusal["retry"]) == (409, "revision_conflict", "new_request")
    assert (stored(world, agent), world.tasks.current("task")) == (record, claim)
    assert heartbeat_rejections_total(world.store, "fixture") == 1


def test_the_archive_watermark_never_moves_backward(world, worker):
    agent, token = worker
    world.put(agent.execution_id, token, world.beat(agent, 5, archive_watermark=12))
    world.clock[0] += 10
    _, lower = world.put(agent.execution_id, token, world.beat(agent, 6, archive_watermark=4))
    _, absent = world.put(agent.execution_id, token, world.beat(agent, 7))
    _, higher = world.put(agent.execution_id, token, world.beat(agent, 8, archive_watermark=20))
    assert [lower["archive_watermark"], absent["archive_watermark"], higher["archive_watermark"]] == [12, 12, 20]
    assert [lower["renewal_sequence"], higher["renewal_sequence"]] == [6, 8]
    assert higher["lease_deadline_ms"] == 1110


def during_renewal(world, monkeypatch, step):
    renew = world.tasks.renew
    seen = []

    def racing(*args):
        claim = renew(*args)
        if not seen:
            seen.append(None)
            seen[0] = step()
        return claim

    monkeypatch.setattr(world.tasks, "renew", racing)
    return seen


def test_a_concurrent_heartbeat_is_refused_before_any_lease_write(world, worker, monkeypatch):
    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)

    def concurrent():
        assert world.store.redis.pttl(lock) == 5_000
        world.clock[0] += 30
        claim = world.tasks.current("task")
        refused = world.put(agent.execution_id, token, world.beat(agent, 9))
        assert world.tasks.current("task") == claim
        return refused

    seen = during_renewal(world, monkeypatch, concurrent)
    status, ack = world.put(agent.execution_id, token, world.beat(agent, 8))
    assert (seen[0][0], seen[0][1]["error_class"], seen[0][1]["retry"]) == (
        503,
        "dependency_unavailable",
        "same_request",
    )
    assert seen[0][1]["message"] == "another heartbeat for this execution is in flight"
    assert (status, ack["lease_deadline_ms"], stored(world, agent)["renewal_sequence"]) == (200, 1100, 8)
    assert not world.store.redis.exists(lock)


def test_a_heartbeat_after_an_expired_lock_cannot_move_the_sequence_backward(world, worker, monkeypatch):
    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)

    def after_expiry():
        world.store.redis.delete(lock)
        return world.put(agent.execution_id, token, world.beat(agent, 9))

    seen = during_renewal(world, monkeypatch, after_expiry)
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 8))
    assert seen[0][0] == 200
    assert (status, refusal["error_class"]) == (409, "revision_conflict")
    assert stored(world, agent)["renewal_sequence"] == 9


def test_the_same_heartbeat_sent_twice_after_an_expired_lock_is_recorded_once(world, worker, monkeypatch):
    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)

    def after_expiry():
        world.store.redis.delete(lock)
        return world.put(agent.execution_id, token, world.beat(agent, 8))

    seen = during_renewal(world, monkeypatch, after_expiry)
    status, ack = world.put(agent.execution_id, token, world.beat(agent, 8))
    assert seen[0] == (status, ack) == (200, stored(world, agent)["ack"])


def test_a_heartbeat_never_releases_a_lock_another_heartbeat_holds(world, worker, monkeypatch):
    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)
    during_renewal(world, monkeypatch, lambda: world.store.redis.set(lock, "another-heartbeat"))
    assert world.put(agent.execution_id, token, world.beat(agent, 1))[0] == 200
    assert world.store.redis.get(lock) == "another-heartbeat"


def test_a_heartbeat_commit_that_keeps_conflicting_writes_nothing(world, worker, monkeypatch):
    from redis.exceptions import WatchError

    agent, token = worker
    heartbeats = world.store.key("fixture", "heartbeats")
    real = world.store.redis.pipeline
    attempts = []

    class Conflicting:
        def __init__(self, pipe):
            self.pipe, self.keys = pipe, ()

        def __enter__(self):
            self.pipe.__enter__()
            return self

        def __exit__(self, *exc):
            return self.pipe.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def watch(self, *keys):
            self.keys = keys
            return self.pipe.watch(*keys)

        def execute(self):
            if heartbeats in self.keys:
                attempts.append(1)
                raise WatchError("fixture conflict")
            return self.pipe.execute()

    monkeypatch.setattr(world.store.redis, "pipeline", lambda *a, **k: Conflicting(real(*a, **k)))
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"], refusal["retry"]) == (503, "dependency_unavailable", "same_request")
    assert refusal["message"] == "heartbeats kept changing; the heartbeat was not recorded"
    assert len(attempts) == 5
    assert stored(world, agent) is None
    monkeypatch.setattr(world.store.redis, "pipeline", real)
    world.clock[0] += 10
    status, ack = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, ack["lease_deadline_ms"], stored(world, agent)["ack"]) == (200, 1110, ack)


def test_the_command_watermark_counts_this_execution_commands(world, worker):
    agent, token = worker
    rows = {
        "op-1": {"execution_id": agent.execution_id, "action": "spawn"},
        "op-2": {"execution_id": agent.execution_id, "action": "command"},
        "op-3": {"execution_id": agent.execution_id, "action": "drain"},
        "op-4": {"execution_id": "exe-other", "action": "command"},
    }
    for operation_id, row in rows.items():
        operation = {
            "operation_id": operation_id,
            "generation": 1,
            "backend": "local",
            "payload_digest": "0" * 64,
            "target": {},
            "phase": "applied",
            **row,
        }
        world.store.redis.hset(world.store.key("fixture", "runtime-operations"), operation_id, json.dumps(operation))
    _, ack = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert ack["command_watermark"] == 2


def test_disabling_registration_lets_registered_leases_drain(world, worker):
    agent, token = worker
    late, late_token = world.start("eng-2@fixture", "other")
    assert world.grants.disable("fixture")
    status, refusal = world.register(late, late_token)
    assert (status, refusal["error_class"]) == (401, "unauthenticated")
    assert world.tasks.current("other") is None
    world.clock[0] += 20
    status, ack = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, ack["lease_deadline_ms"]) == (200, 1120)


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/v2/executions/register"),
        ("POST", "/v2/executions/exe-1/heartbeat"),
        ("PUT", "/v2/executions/exe-1/heartbeat/extra"),
        ("PUT", "/v2/executions//heartbeat"),
    ],
)
def test_an_unknown_endpoint_is_not_found(world, worker, method, path):
    agent, token = worker
    status, refusal = world.api.route(method, path, f"Bearer {token}", world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (404, "invalid_request")
    assert stored(world, agent) is None


@pytest.mark.parametrize("authorization", ["", "Basic abc", "bearer abc"])
def test_a_request_without_a_bearer_credential_is_unauthenticated(world, worker, authorization):
    agent, _ = worker
    path = f"/v2/executions/{agent.execution_id}/heartbeat"
    status, refusal = world.api.route("PUT", path, authorization, world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (401, "unauthenticated")


@pytest.mark.parametrize("lease_ms", [0, -5, 1.5, True])
def test_the_server_lease_duration_must_be_a_positive_integer(world, lease_ms):
    from scripts.swarm_v2.api.executions import ExecutionsAPI

    with pytest.raises(ValueError):
        ExecutionsAPI(world.grants, world.tasks, lease_ms)


def test_the_default_server_lease_is_one_minute(world):
    from scripts.swarm_v2.api.executions import ExecutionsAPI

    assert ExecutionsAPI(world.grants, world.tasks).lease_ms == 60_000


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests.sv2_ldg02_cases import run_case

    first, second = run_case(case), run_case(case)
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text())
    assert committed == {"case": f"T-SV2-LDG-02-{case.upper()}", "independent_runs": 2, "observed": first}


def test_the_smallest_server_lease_is_one_millisecond_and_refusals_name_it(world):
    from scripts.swarm_v2.api.executions import ExecutionsAPI

    assert ExecutionsAPI(world.grants, world.tasks, 1).lease_ms == 1
    with pytest.raises(ValueError, match="^the server lease duration must be a positive number of milliseconds$"):
        ExecutionsAPI(world.grants, world.tasks, 0)


def test_each_refusal_names_its_cause(world, worker):
    agent, token = worker
    other, other_token = world.start("eng-2@fixture", "other")
    world.register(other, other_token)
    stranger, stranger_token = world.start("eng-3@fixture", "third")

    def message(execution_id, credential, body):
        return world.put(execution_id, credential, body)[1]["message"]

    foreign = world.beat(agent, 1)
    foreign["authority"]["task_id"] = "other"
    epoch = world.beat(agent, 1)
    epoch["authority"]["controller_epoch"] = 2
    assert world.api.route("PUT", "/v2/executions/x/heartbeat", "Basic abc", {})[1]["message"] == (
        "a bearer credential is required"
    )
    assert world.api.route("GET", "/v2/executions", f"Bearer {token}", {})[1]["message"] == (
        "no such execution endpoint"
    )
    assert message(other.execution_id, token, world.beat(other, 1)) == "heartbeat names another execution"
    assert message(agent.execution_id, token, foreign) == "heartbeat authority is outside its registered execution"
    assert message(agent.execution_id, token, epoch) == "heartbeat names another controller epoch"
    assert message(agent.execution_id, token, world.beat(agent, 1, generation=2)) == (
        "the task generation is no longer current"
    )
    assert message(agent.execution_id, token, {**world.beat(agent, 1), "renewal_sequence": -1}) == (
        "renewal_sequence: fails minimum"
    )
    assert message(stranger.execution_id, stranger_token, world.beat(stranger, 1)) == ("launch grant is not registered")
    world.put(agent.execution_id, token, world.beat(agent, 5))
    assert message(agent.execution_id, token, world.beat(agent, 4)) == (
        "heartbeat renewal sequence is not newer than the accepted one"
    )
    leaderless = world.beat(agent, 6)
    world.controller.held = None
    assert message(agent.execution_id, token, leaderless) == "the controller lease is absent"


def test_a_repeated_registration_reports_the_accepted_archive_watermark(world, worker):
    agent, token = worker
    world.put(agent.execution_id, token, world.beat(agent, 1, archive_watermark=12))
    assert stored(world, agent)["resources"] == {}
    assert world.register(agent, token)[1]["archive_watermark"] == 12


def test_a_failed_lock_release_keeps_the_committed_heartbeat(world, worker, monkeypatch):
    from redis.exceptions import ConnectionError as StoreLost

    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)

    def lost(*args):
        raise StoreLost("fixture release lost")

    monkeypatch.setattr(world.store.redis, "eval", lost)
    status, ack = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, ack["renewal_sequence"], stored(world, agent)["renewal_sequence"]) == (200, 1, 1)
    assert world.store.redis.exists(lock)


def test_server_deadlines_render_as_utc_with_milliseconds():
    from scripts.swarm_v2.api.executions import _timestamp

    assert _timestamp(1_791_630_892_123) == "2026-10-10T11:14:52.123Z"
    assert _timestamp(7) == "1970-01-01T00:00:00.007Z"


def test_server_deadlines_render_as_utc_on_a_host_in_another_timezone(monkeypatch):
    import time

    from scripts.swarm_v2.api.executions import _timestamp

    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        assert _timestamp(1_791_630_892_123) == "2026-10-10T11:14:52.123Z"
    finally:
        monkeypatch.undo()
        time.tzset()


def fence_of(world, fence):
    return json.loads(world.store.redis.get(fence))


def test_a_fenced_renewal_refuses_an_older_or_altered_sequence_and_admits_its_own_retry(world, worker):
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2.authority import RenewalFence

    _, token = worker
    fence = world.store.key("fixture", "fence-probe")
    world.clock[0] += 5
    claim = world.tasks.renew(token, 1, 100, RenewalFence(fence, 5, "a"))
    world.clock[0] += 10
    for sequence, digest in ((5, "b"), (4, "a")):
        with pytest.raises(SwarmError) as error:
            world.tasks.renew(token, 1, 100, RenewalFence(fence, sequence, digest))
        assert str(error.value) == "out_of_order"
    assert (world.tasks.current("task"), fence_of(world, fence)) == (claim, {"sequence": 5, "digest": "a"})
    retried = world.tasks.renew(token, 1, 100, RenewalFence(fence, 5, "a"))
    assert (retried.lease_deadline_ms, fence_of(world, fence)) == (1115, {"sequence": 5, "digest": "a"})
    world.clock[0] += 1
    renewed = world.tasks.renew(token, 1, 100, RenewalFence(fence, 6, "c"))
    assert (renewed.lease_deadline_ms, fence_of(world, fence)) == (1116, {"sequence": 6, "digest": "c"})
    world.clock[0] += 1
    assert world.tasks.renew(token, 1, 100).lease_deadline_ms == 1117
    assert fence_of(world, fence) == {"sequence": 6, "digest": "c"}


def test_a_fenced_renewal_at_an_unchanged_deadline_still_advances_its_fence(world, worker):
    from scripts.swarm_v2.authority import RenewalFence

    _, token = worker
    fence = world.store.key("fixture", "fence-probe")
    claim = world.tasks.current("task")
    journal = world.tasks.journal("task")
    assert world.tasks.renew(token, 1, 100) == claim
    assert world.tasks.journal("task") == journal
    assert world.tasks.renew(token, 1, 100, RenewalFence(fence, 0, "a")) == claim
    assert fence_of(world, fence) == {"sequence": 0, "digest": "a"}
    assert [row["event"] for row in world.tasks.journal("task")] == ["admitted", "renewed"]


def test_a_heartbeat_stalled_past_its_lock_cannot_extend_the_lease(world, worker, monkeypatch):
    agent, token = worker
    lock = world.store.key("fixture", "heartbeat-lock", agent.execution_id)
    renew = world.tasks.renew
    newer = []

    def stalled(*args):
        if not newer:
            newer.append(None)
            world.store.redis.delete(lock)
            world.clock[0] += 30
            newer[0] = world.put(agent.execution_id, token, world.beat(agent, 9))
            world.clock[0] += 20
        return renew(*args)

    monkeypatch.setattr(world.tasks, "renew", stalled)
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 8))
    assert (newer[0][0], newer[0][1]["lease_deadline_ms"]) == (200, 1130)
    assert (status, refusal["error_class"], refusal["message"]) == (
        409,
        "revision_conflict",
        "a newer heartbeat already renewed this lease",
    )
    assert world.tasks.current("task").lease_deadline_ms == 1130
    assert stored(world, agent)["renewal_sequence"] == 9


@pytest.fixture
def served(world):
    import threading

    from scripts.swarm_v2.api.server import serve

    server = serve(world.api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def call(port, method, path, token="", body=b"", headers=None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = {"Authorization": f"Bearer {token}"} if token else {}
    connection.request(method, path, body=body, headers={**sent, **(headers or {})})
    response = connection.getresponse()
    reply = (response.status, response.getheader("Content-Type"), json.loads(response.read()))
    connection.close()
    return reply


def test_the_served_endpoints_register_and_renew_a_worker_over_http(world, served):
    agent, token = world.start()
    registration = json.dumps({"execution_id": agent.execution_id, "generation": 1}).encode()
    status, kind, ack = call(served, "POST", "/v2/executions/register", token, registration)
    assert (status, kind, ack["lease_deadline_ms"], ack["task_id"]) == (200, "application/json", 1100, "task")
    world.clock[0] += 25
    path = f"/v2/executions/{agent.execution_id}/heartbeat"
    status, _, ack = call(served, "PUT", path, token, json.dumps(world.beat(agent, 1)).encode())
    assert (status, ack["lease_deadline_ms"], ack["renewal_sequence"]) == (200, 1125, 1)
    assert world.tasks.current("task").lease_deadline_ms == 1125


def test_the_served_endpoints_refuse_forged_subjects_and_unreadable_bodies(world, served):
    agent, token = world.start()
    other, other_token = world.start("eng-2@fixture", "other")
    for worker, credential in ((agent, token), (other, other_token)):
        body = json.dumps({"execution_id": worker.execution_id, "generation": 1}).encode()
        call(served, "POST", "/v2/executions/register", credential, body)
    forged = json.dumps(world.beat(other, 1)).encode()
    assert call(served, "PUT", f"/v2/executions/{other.execution_id}/heartbeat", token, forged)[2]["error_class"] == (
        "forbidden_scope"
    )
    status, _, refusal = call(served, "PUT", f"/v2/executions/{agent.execution_id}/heartbeat", token, b"{not json")
    assert (status, refusal["error_class"]) == (400, "invalid_request")
    assert call(served, "GET", "/v2/executions", token)[0] == 404
    assert world.store.redis.hlen(world.store.key("fixture", "heartbeats")) == 0


def test_the_served_endpoints_refuse_a_body_over_the_limit_without_reading_it(world, served):
    import http.client

    agent, token = world.start()
    path = f"/v2/executions/{agent.execution_id}/heartbeat"
    connection = http.client.HTTPConnection("127.0.0.1", served, timeout=10)
    connection.putrequest("PUT", path)
    connection.putheader("Authorization", f"Bearer {token}")
    connection.putheader("Content-Length", "65537")
    connection.endheaders()
    response = connection.getresponse()
    refusal = json.loads(response.read())
    connection.close()
    assert (response.status, refusal["error_class"], refusal["message"]) == (
        413,
        "invalid_request",
        "the request body is too large",
    )
    padded = {**world.beat(agent, 1), "padding": ""}
    padded["padding"] = "x" * (65_536 - len(json.dumps(padded)))
    body = json.dumps(padded).encode()
    assert len(body) == 65_536
    assert call(served, "PUT", path, token, body)[0] == 401
