import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-LDG-05"
SLUG = "fixture"


@pytest.fixture
def world(monkeypatch, tmp_path):
    from tests.sv2_ldg05_cases import World

    return World(monkeypatch, tmp_path)


@pytest.fixture
def worker(world):
    return world.worker()


def refused(action):
    from scripts.swarm_v2.auth_context import GrantRefused

    with pytest.raises(GrantRefused) as caught:
        action()
    return caught.value.error_class, str(caught.value)


def route(world, network, method, path, body=None):
    return world.commands_api.route(method, path, f"Bearer {network.token}", body)


def poll(world, agent, network):
    return route(world, network, "GET", f"/v2/executions/{agent.execution_id}/commands")


def ack(world, agent, network, command, payload_digest=None):
    body = {"payload_digest": payload_digest or command["payload_digest"]}
    return route(
        world, network, "POST", f"/v2/executions/{agent.execution_id}/commands/{command['command_id']}/ack", body
    )


def complete(world, agent, network, command, outcome):
    path = f"/v2/executions/{agent.execution_id}/commands/{command['command_id']}/complete"
    return route(world, network, "POST", path, {"outcome": outcome})


def detail(status, error_class, message, retry="new_request"):
    return status, {"error_class": error_class, "operation_id": "unknown", "retry": retry, "message": message}


def test_issue_assigns_an_immutable_id_and_payload_digest_bound_to_generation_and_expiry(world, worker):
    from uuid import NAMESPACE_URL, uuid5

    from scripts.swarm_v2.runtime.operations import digest

    agent, _, _ = worker
    command = world.issue(agent, "answer", "answer-1", {"text": "Use the second plan"})
    assert command == {
        "command_id": f"cmd-{uuid5(NAMESPACE_URL, json.dumps([SLUG, agent.execution_id, 'answer-1'])).hex}",
        "execution_id": agent.execution_id,
        "generation": agent.generation,
        "kind": "answer",
        "payload": {"text": "Use the second plan"},
        "payload_digest": digest({"kind": "answer", "payload": {"text": "Use the second plan"}}),
        "issued_at_ms": 1000,
        "expires_at_ms": 1050,
        "state": "issued",
        "accepted_at_ms": None,
        "completed_at_ms": None,
        "outcome": None,
    }
    world.later(5)
    assert world.issue(agent, "answer", "answer-1", {"text": "Use the second plan"}) == command
    assert world.queue.outcome(agent.execution_id, command["command_id"]) == command
    assert refused(lambda: world.issue(agent, "answer", "answer-1", {"text": "Use the first plan"})) == (
        "revision_conflict",
        "the command key was issued with another payload",
    )
    assert refused(lambda: world.issue(agent, "stop", "answer-1")) == (
        "revision_conflict",
        "the command key was issued with another payload",
    )
    assert (
        world.issue(agent, "answer", "answer-2", {"text": "Use the second plan"})["command_id"] != command["command_id"]
    )


def test_issue_validates_kind_payload_key_and_expiry_before_writing(world, worker):
    agent, _, _ = worker
    before = world.protected()
    issue = world.queue.issue
    invalid = [
        lambda: issue(agent.execution_id, agent.generation, "reboot", {}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "answer", {}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "answer", {"text": ""}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "answer", {"text": 3}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "answer", {"text": "a", "extra": 1}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "drain", {"reason": "x"}, "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "drain", [], "k", 10),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, "", 10),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, 5, 10),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, "k", 0),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, "k", 900_001),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, "k", 10.0),
        lambda: issue(agent.execution_id, agent.generation, "drain", {}, "k", True),
    ]
    assert [refused(action)[0] for action in invalid] == ["invalid_request"] * len(invalid)
    assert refused(invalid[0])[1] == "unknown command kind"
    assert refused(invalid[1])[1] == "an answer needs non-empty text and nothing else"
    assert refused(invalid[5])[1] == "this command kind takes no payload"
    assert refused(invalid[7])[1] == "a command key is required"
    assert refused(invalid[9])[1] == "the expiry must be between 1 and 900000 milliseconds"
    assert refused(lambda: issue(agent.execution_id, agent.generation + 1, "drain", {}, "k", 10)) == (
        "stale_generation",
        "the execution is not the current occupant of its seat",
    )
    assert refused(lambda: issue("missing", 1, "drain", {}, "k", 10))[0] == "stale_generation"
    assert world.issue(agent, "cancel", "k")["expires_at_ms"] == 1050
    assert issue(agent.execution_id, agent.generation, "stop", {}, "max", 900_000)["expires_at_ms"] == 901_000
    assert issue(agent.execution_id, agent.generation, "drain", {}, "min", 1)["expires_at_ms"] == 1001
    world.store.redis.delete(world.queue.key(agent.execution_id))
    assert world.protected() == before


def test_rollback_stops_issuing_disabled_kinds_while_existing_commands_finish(world, worker):
    from scripts.swarm_v2.api.commands import CommandQueue

    agent, network, control = worker
    drain = world.issue(agent, "drain", "drain-1")
    world.queue = CommandQueue(world.store, SLUG, kinds=frozenset({"answer"}))
    world.commands_api = world.restarted()
    assert refused(lambda: world.issue(agent, "drain", "drain-2")) == (
        "forbidden_scope",
        "new commands of this kind are not issued",
    )
    assert world.issue(agent, "drain", "drain-1") == drain
    control.step()
    control.checkpointed("refs/checkpoints/rollback")
    assert world.queue.outcome(agent.execution_id, drain["command_id"])["state"] == "completed"


def test_an_unknown_command_has_no_outcome_and_an_unacknowledged_one_expires_on_read(world, worker):
    agent, _, _ = worker
    assert world.queue.outcome(agent.execution_id, "cmd-missing") is None
    command = world.issue(agent, "stop", "stop-1")
    world.later(49)
    assert world.state(agent, command) == "issued"
    world.later(1)
    assert world.state(agent, command) == "expired"
    stored = json.loads(world.store.redis.hget(world.queue.key(agent.execution_id), command["command_id"]))
    assert stored["state"] == "issued"


def test_poll_returns_the_oldest_live_commands_up_to_the_limit(world, worker):
    agent, network, _ = worker
    first = world.issue(agent, "cancel", "a")
    world.later(1)
    second = world.issue(agent, "answer", "b", {"text": "yes"})
    world.later(1)
    third = world.issue(agent, "stop", "c")
    status, reply = poll(world, agent, network)
    assert status == 200
    assert reply == {"execution_id": agent.execution_id, "commands": [first, second], "poll_interval_ms": 10}
    assert ack(world, agent, network, first)[0] == 200
    assert complete(world, agent, network, first, {"status": "succeeded"})[0] == 200
    world.later(10)
    assert poll(world, agent, network)[1]["commands"] == [second, third]
    assert ack(world, agent, network, second)[0] == 200
    world.later(40)
    assert [(c["command_id"], c["state"]) for c in poll(world, agent, network)[1]["commands"]] == [
        (second["command_id"], "accepted")
    ]


def test_polls_faster_than_the_interval_are_refused_for_a_retry(world, worker):
    agent, network, _ = worker
    assert poll(world, agent, network)[0] == 200
    world.later(9)
    assert poll(world, agent, network) == detail(429, "rate_limited", "poll again in 1 ms", "same_request")
    world.later(1)
    assert poll(world, agent, network)[0] == 200


def test_the_api_rejects_bad_settings(world):
    from scripts.swarm_v2.api.commands import CommandsAPI

    for interval, limit in ((0, 1), (1, 0), (1.0, 1), (1, True)):
        with pytest.raises(ValueError, match="poll interval and limit must be positive integers"):
            CommandsAPI(world.grants, world.queue, interval, limit)
    api = CommandsAPI(world.grants, world.queue)
    assert (api.poll_interval_ms, api.poll_limit) == (1000, 10)


def test_endpoints_refuse_missing_credentials_other_executions_and_unknown_paths(world, worker):
    agent, network, _ = worker
    other, other_network, _ = world.worker("eng-2@fixture", "other")
    command = world.issue(other, "stop", "stop-1")
    base = f"/v2/executions/{other.execution_id}/commands"
    assert world.commands_api.route("GET", base, "", None) == detail(
        401, "unauthenticated", "a bearer credential is required"
    )
    forbidden = detail(403, "forbidden_scope", "the path names another execution")
    assert route(world, network, "GET", base) == forbidden
    assert route(world, network, "POST", f"{base}/{command['command_id']}/ack", {}) == forbidden
    assert route(world, network, "POST", f"{base}/x/retry", {}) == detail(
        404, "invalid_request", "no such command endpoint"
    )
    assert route(world, network, "PUT", f"{base}/x/ack", {}) == detail(
        404, "invalid_request", "no such command endpoint"
    )
    assert route(world, network, "POST", base, {}) == detail(404, "invalid_request", "no such command endpoint")
    assert world.state(other, command) == "issued"


def test_ack_records_receipt_once_and_replays_the_stored_acceptance(world, worker):
    from scripts.swarm_v2.api.commands import worker_command_ack_lag_seconds

    agent, network, _ = worker
    command = world.issue(agent, "answer", "answer-1", {"text": "yes"})
    world.later(7)
    status, accepted = ack(world, agent, network, command)
    assert (status, accepted) == (200, {**command, "state": "accepted", "accepted_at_ms": 1007})
    world.later(3)
    assert ack(world, agent, network, command) == (200, accepted)
    assert worker_command_ack_lag_seconds(world.store, SLUG) == [0.007]
    assert ack(world, agent, network, command, "0" * 64) == detail(
        409, "revision_conflict", "the acknowledgement names another payload"
    )
    missing = {**command, "command_id": "cmd-missing"}
    assert ack(world, agent, network, missing) == detail(404, "not_found", "no such command for this execution")
    for body in (None, [], {}, {"payload_digest": 3}):
        assert route(
            world, network, "POST", f"/v2/executions/{agent.execution_id}/commands/{command['command_id']}/ack", body
        ) == detail(400, "invalid_request", "an acknowledgement needs the payload digest")


def test_an_expired_command_is_never_accepted(world, worker):
    agent, network, _ = worker
    command = world.issue(agent, "stop", "stop-1")
    world.later(49)
    early = world.issue(agent, "cancel", "cancel-1")
    world.later(1)
    before = world.protected()
    assert ack(world, agent, network, command) == detail(
        410, "expired", "the command expired before it was acknowledged"
    )
    assert world.protected() == before
    assert ack(world, agent, network, early)[0] == 200


def test_completion_is_separate_from_acceptance_and_validated_per_kind(world, worker):
    agent, network, _ = worker
    drain = world.issue(agent, "drain", "drain-1")
    stop = world.issue(agent, "stop", "stop-1")
    not_acked = detail(409, "revision_conflict", "the command was not acknowledged")
    assert complete(world, agent, network, drain, {"status": "checkpointed", "checkpoint": "c"}) == not_acked
    ack(world, agent, network, drain)
    ack(world, agent, network, stop)
    bad = detail(400, "invalid_request", "the outcome does not fit the command kind")
    for outcome in (
        None,
        {"status": "succeeded"},
        {"status": "checkpointed"},
        {"status": "checkpointed", "checkpoint": ""},
        {"status": "checkpointed", "checkpoint": 1},
    ):
        assert complete(world, agent, network, drain, outcome) == bad
    assert complete(world, agent, network, stop, {"status": "checkpointed", "checkpoint": "c"}) == bad
    world.later(60)
    status, done = complete(world, agent, network, drain, {"status": "checkpointed", "checkpoint": "c"})
    assert (status, done["state"], done["completed_at_ms"], done["accepted_at_ms"]) == (200, "completed", 1060, 1000)
    assert complete(world, agent, network, drain, {"status": "checkpointed", "checkpoint": "c"}) == (200, done)
    assert ack(world, agent, network, drain) == (200, done)
    assert complete(world, agent, network, drain, {"status": "failed"}) == detail(
        409, "revision_conflict", "the command already completed with another outcome"
    )
    assert complete(world, agent, network, stop, {"status": "failed", "detail": "x"})[1]["outcome"] == {
        "status": "failed",
        "detail": "x",
    }
    assert complete(world, agent, network, {"command_id": "cmd-missing"}, {"status": "failed"}) == detail(
        404, "not_found", "no such command for this execution"
    )


def test_store_loss_and_write_contention_are_dependency_failures(world, worker, monkeypatch):
    from redis.exceptions import ConnectionError as StoreLost
    from redis.exceptions import WatchError

    agent, network, _ = worker
    command = world.issue(agent, "stop", "stop-1")
    attempts = []
    real = world.store.redis.pipeline

    def contended(*args, **kwargs):
        pipe = real(*args, **kwargs)
        attempts.append(1)

        def watch(*keys):
            raise WatchError("fixture contention")

        pipe.watch = watch
        return pipe

    monkeypatch.setattr(world.store.redis, "pipeline", contended)
    assert ack(world, agent, network, command) == detail(
        503, "dependency_unavailable", "commands kept changing; nothing was recorded", "same_request"
    )
    assert len(attempts) == 5
    monkeypatch.setattr(world.store.redis, "pipeline", real)

    def lost(*args):
        raise StoreLost("fixture store lost")

    monkeypatch.setattr(world.store.redis, "hvals", lost)
    assert poll(world, agent, network) == detail(
        503, "dependency_unavailable", "the command store is unavailable", "same_request"
    )


def test_one_contended_write_is_retried(world, worker, monkeypatch):
    from redis.exceptions import WatchError

    agent, network, _ = worker
    command = world.issue(agent, "stop", "stop-1")
    real = world.store.redis.pipeline
    contention = [WatchError("fixture contention")]

    def once(*args, **kwargs):
        pipe = real(*args, **kwargs)
        original = pipe.watch

        def watch(*keys):
            if contention:
                raise contention.pop()
            return original(*keys)

        pipe.watch = watch
        return pipe

    monkeypatch.setattr(world.store.redis, "pipeline", once)
    assert ack(world, agent, network, command)[1]["state"] == "accepted"


def test_routes_send_command_paths_to_the_command_api(world, worker):
    from scripts.swarm_v2.api.server import Routes

    agent, network, _ = worker
    calls = []

    class Recorder:
        def __init__(self, name):
            self.name = name

        def route(self, *args):
            calls.append(self.name)
            return 200, {}

    routes = Routes(Recorder("executions"), Recorder("tasks"), Recorder("commands"))
    for path in (
        f"/v2/executions/{agent.execution_id}/commands",
        f"/v2/executions/{agent.execution_id}/commands/cmd-x/ack",
        f"/v2/executions/{agent.execution_id}/heartbeat",
        "/v2/tasks/task",
    ):
        routes.route("GET", path, "", None)
    assert calls == ["commands", "commands", "executions", "tasks"]
    calls.clear()
    Routes(Recorder("executions"), Recorder("tasks")).route(
        "GET", f"/v2/executions/{agent.execution_id}/commands", "", None
    )
    assert calls == ["executions"]


def test_a_drain_stops_mutations_before_its_acknowledgement_is_confirmed(world, worker):
    agent, network, control = worker
    assert control.may_mutate()
    drain = world.issue(agent, "drain", "drain-1")
    network.drop = {"ack"}
    control.step()
    assert not control.may_mutate()
    assert world.state(agent, drain) == "issued"
    assert not world.control(agent, network).may_mutate()
    network.drop = set()
    world.later(10)
    control.step()
    assert world.state(agent, drain) == "accepted"
    control.checkpointed("refs/checkpoints/one")
    assert world.queue.outcome(agent.execution_id, drain["command_id"])["outcome"] == {
        "status": "checkpointed",
        "checkpoint": "refs/checkpoints/one",
    }
    control.checkpointed("refs/checkpoints/two")
    assert (
        world.queue.outcome(agent.execution_id, drain["command_id"])["outcome"]["checkpoint"] == "refs/checkpoints/one"
    )
    assert not control.may_mutate()


def test_a_refused_drain_restores_mutations_unless_another_drain_holds(world, worker):
    agent, network, control = worker
    first = world.issue(agent, "drain", "drain-1")
    network.drop = {"ack"}
    control.step()
    world.later(10)
    second = world.issue(agent, "drain", "drain-2")
    world.later(45)
    network.drop = set()
    control.step()
    assert world.state(agent, first) == "expired"
    assert world.state(agent, second) == "accepted"
    assert not control.may_mutate()
    other, other_network, other_control = world.worker("eng-2@fixture", "other")
    world.issue(other, "drain", "drain-1")
    other_network.drop = {"ack"}
    other_control.step()
    assert not other_control.may_mutate()
    world.later(60)
    other_network.drop = set()
    other_control.step()
    assert other_control.may_mutate()


def test_commands_run_only_after_acceptance_and_never_twice(world, worker):
    agent, network, control = worker
    answer = world.issue(agent, "answer", "answer-1", {"text": "yes"})
    network.drop = {"ack"}
    control.step()
    assert world.ran == []
    network.drop = set()
    world.later(10)
    control.step()
    world.later(10)
    control.step()
    assert world.ran == [["answer", {"text": "yes"}]]
    assert world.queue.outcome(agent.execution_id, answer["command_id"])["outcome"] == {"status": "succeeded"}
    assert control.may_mutate()


def test_a_forged_payload_is_neither_acknowledged_nor_run(world, worker):
    agent, network, control = worker
    answer = world.issue(agent, "answer", "answer-1", {"text": "yes"})
    reply = poll(world, agent, network)
    reply[1]["commands"][0]["payload"] = {"text": "no"}
    network.repeat = reply
    control.step()
    assert world.ran == []
    assert world.state(agent, answer) == "issued"
    assert [call[1] for call in network.calls] == []


def test_an_already_accepted_command_without_local_state_is_reported_not_rerun(world, worker):
    agent, network, control = worker
    answer = world.issue(agent, "answer", "answer-1", {"text": "yes"})
    drain = world.issue(agent, "drain", "drain-1")
    ack(world, agent, network, answer)
    ack(world, agent, network, drain)
    world.later(10)
    control.step()
    assert world.ran == []
    assert world.queue.outcome(agent.execution_id, answer["command_id"])["outcome"] == {
        "status": "failed",
        "detail": "accepted before this worker state existed; not rerun",
    }
    assert world.state(agent, drain) == "accepted"
    assert not control.may_mutate()


def test_a_command_interrupted_while_running_is_reported_not_rerun(world, worker):
    agent, network, control = worker
    answer = world.issue(agent, "answer", "answer-1", {"text": "yes"})

    def crash(payload):
        raise KeyboardInterrupt

    control.handlers = {"answer": crash}
    with pytest.raises(KeyboardInterrupt):
        control.step()
    restarted = world.control(agent, network)
    world.later(10)
    restarted.step()
    assert world.ran == []
    assert world.queue.outcome(agent.execution_id, answer["command_id"])["outcome"] == {
        "status": "failed",
        "detail": "interrupted while running; not rerun",
    }


def test_a_command_without_a_handler_fails_and_a_refused_completion_is_kept_local(world, worker):
    agent, network, control = worker
    cancel = world.issue(agent, "cancel", "cancel-1")
    control.handlers = {}
    network.drop = {"complete"}
    control.step()
    complete(world, agent, network, cancel, {"status": "succeeded"})
    network.drop = set()
    world.later(10)
    control.step()
    assert world.queue.outcome(agent.execution_id, cancel["command_id"])["outcome"] == {"status": "succeeded"}
    state = json.loads(control.path.read_text(encoding="utf-8"))
    assert state[cancel["command_id"]]["state"] == "rejected"
    assert state[cancel["command_id"]]["outcome"] == {"status": "failed", "detail": "no handler for this command kind"}


def test_the_route_transport_separates_retryable_failures_from_refusals():
    from scripts.swarm_v2.worker.control import CommandRefused, RouteTransport

    replies = []
    sent = []

    def send(method, path, body):
        sent.append([method, path, body])
        return replies.pop(0)

    transport = RouteTransport("exec-1", send)
    replies[:] = [
        (200, {"commands": [{"command_id": "c"}]}),
        (200, {"state": "accepted"}),
        (200, {"state": "completed"}),
    ]
    assert transport.poll() == [{"command_id": "c"}]
    assert transport.ack("c", "d") == {"state": "accepted"}
    assert transport.complete("c", {"status": "succeeded"}) == {"state": "completed"}
    assert sent == [
        ["GET", "/v2/executions/exec-1/commands", None],
        ["POST", "/v2/executions/exec-1/commands/c/ack", {"payload_digest": "d"}],
        ["POST", "/v2/executions/exec-1/commands/c/complete", {"outcome": {"status": "succeeded"}}],
    ]
    replies[:] = [(503, {"error_class": "dependency_unavailable", "retry": "same_request", "message": "down"})]
    with pytest.raises(ConnectionError, match="down"):
        transport.poll()
    replies[:] = [(410, {"error_class": "expired", "retry": "new_request", "message": "late"})]
    with pytest.raises(CommandRefused, match="late") as caught:
        transport.ack("c", "d")
    assert caught.value.error_class == "expired"


def test_worker_state_survives_a_restart_and_is_written_whole(world, worker):
    agent, network, control = worker
    drain = world.issue(agent, "drain", "drain-1")
    control.step()
    saved = json.loads(control.path.read_text(encoding="utf-8"))
    assert saved == {
        drain["command_id"]: {
            "kind": "drain",
            "payload_digest": drain["payload_digest"],
            "payload": {},
            "state": "accepted",
            "outcome": None,
        }
    }
    assert sorted(path.name for path in control.path.parent.glob(f"{agent.execution_id}*")) == [control.path.name]


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case, tmp_path):
    from tests.sv2_ldg05_cases import run_case

    first_folder, second_folder = tmp_path / "first", tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    first, second = run_case(case, first_folder), run_case(case, second_folder)
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text(encoding="utf-8"))
    assert committed == {"case": f"T-SV2-LDG-05-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )


def test_the_manifest_names_the_hash_of_every_case_input():
    import hashlib

    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]
    assert manifest["inputs"] == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in ["tests/fixtures/swarm_v2/worker-commands.json"]
    }
    assert manifest["cases"] == ["T-SV2-LDG-05-A", "T-SV2-LDG-05-B", "T-SV2-LDG-05-C"]


def test_the_package_record_carries_its_completion_evidence():
    record = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    assert (record["package"], record["package_complete"]) == ("SV2-LDG-05", False)
    assert set(record["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for name in ("interfaces", "supported_versions", "authoritative_objects", "remaining_limitations", "validation"):
        assert record[name], name
    assert record["cases"] == {
        **{case: f"evidence/SV2-LDG-05/{case}-result.json" for case in "abc"},
        "manifest": "evidence/SV2-LDG-05/manifest.json",
    }
    assert set(record["measurements"]["worker_command_ack_lag_seconds"]) == {"a", "b", "c"}
    path, name = record["rollback_rehearsal"]["test"].split("::")
    assert path == "tests/test_swarm_v2_worker_commands.py"
    assert f"\ndef {name}(" in Path(__file__).read_text(encoding="utf-8")
