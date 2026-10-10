import json
from pathlib import Path

import pytest

from scripts.swarm_v2.api.executions import ExecutionsAPI, heartbeat_rejections, heartbeat_rejections_total
from scripts.swarm_v2.auth_context import GrantRefused

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
    assert (status, refusal["error_class"]) == (409, "stale_generation")
    assert world.tasks.current("task") == claim


def test_registration_refusal_echoes_an_identifier_operation_id(world):
    agent, token = world.start()
    with pytest.raises(GrantRefused) as error:
        world.api.register("", {"execution_id": agent.execution_id, "generation": 1}, "register-7")
    assert error.value.detail()["operation_id"] == "register-7"


def test_a_heartbeat_before_registration_is_unauthenticated(world):
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


def test_a_forged_url_subject_cannot_touch_another_execution(world, worker):
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
    assert world.protected() == before


def test_controller_admission_disabled_refuses_renewal_as_unavailable(world, worker):
    agent, token = worker
    world.controller.admission_enabled = False
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 1))
    assert (status, refusal["error_class"]) == (503, "dependency_unavailable")
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


def test_a_newer_heartbeat_committed_during_renewal_wins(world, worker, monkeypatch):
    agent, token = worker
    renew = world.tasks.renew
    raced = []

    def racing(*args):
        claim = renew(*args)
        if not raced:
            raced.append(world.put(agent.execution_id, token, world.beat(agent, 9)))
        return claim

    monkeypatch.setattr(world.tasks, "renew", racing)
    status, refusal = world.put(agent.execution_id, token, world.beat(agent, 8))
    assert raced[0][0] == 200
    assert (status, refusal["error_class"]) == (409, "revision_conflict")
    assert stored(world, agent)["renewal_sequence"] == 9


def test_the_same_heartbeat_committed_during_renewal_is_returned(world, worker, monkeypatch):
    agent, token = worker
    renew = world.tasks.renew
    raced = []

    def racing(*args):
        claim = renew(*args)
        if not raced:
            raced.append(world.put(agent.execution_id, token, world.beat(agent, 8)))
        return claim

    monkeypatch.setattr(world.tasks, "renew", racing)
    assert world.put(agent.execution_id, token, world.beat(agent, 8)) == raced[0]


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
    assert len(attempts) == 5
    assert stored(world, agent) is None


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
    with pytest.raises(ValueError):
        ExecutionsAPI(world.grants, world.tasks, lease_ms)


def test_the_default_server_lease_is_one_minute(world):
    assert ExecutionsAPI(world.grants, world.tasks).lease_ms == 60_000


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests.sv2_ldg02_cases import run_case

    first, second = run_case(case), run_case(case)
    assert first == second
    assert json.loads((EVIDENCE / f"{case}-result.json").read_text())["observed"] == first
