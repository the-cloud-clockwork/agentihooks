import http.client
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-LDG-03"
PR = "https://github.com/the-cloud-clockwork/agentihooks/pull/1"


def TasksAPI(*args, **kwargs):
    from scripts.swarm_v2.api.tasks import TasksAPI as api

    return api(*args, **kwargs)


def spec_revision(task):
    from scripts.swarm_v2.api.tasks import spec_revision as compute

    return compute(task)


def ledger_revision_conflicts_total(store, slug):
    from scripts.swarm_v2.api.tasks import ledger_revision_conflicts_total as total

    return total(store, slug)


def revision(value):
    from scripts.swarm_ledger.api.resources import revision as compute

    return compute(value)


def refused_class():
    from scripts.swarm_v2.auth_context import GrantRefused

    return GrantRefused


def swarm_error(message):
    from scripts.swarm.store import SwarmError

    return SwarmError(message)


@pytest.fixture
def world(monkeypatch, tmp_path):
    from tests.sv2_ldg03_cases import World

    return World(monkeypatch, tmp_path)


@pytest.fixture
def worker(world):
    return world.worker()


def detail(error_class, message, operation_id="unknown", **extra):
    retry = "same_request" if error_class == "dependency_unavailable" else "new_request"
    return {"error_class": error_class, "operation_id": operation_id, "retry": retry, "message": message, **extra}


def test_spec_revision_covers_only_execution_relevant_fields():
    from scripts.swarm_v2.api.tasks import SPEC_FIELDS

    task = {name: f"{name} value" for name in SPEC_FIELDS}
    base = spec_revision(task)
    assert spec_revision({**task, "state": "pr", "pr_url": PR, "comments": [{"id": "c"}], "rank": "high"}) == base
    for name in SPEC_FIELDS:
        assert spec_revision({**task, name: "changed"}) != base
    assert len(base) == 64


def test_a_worker_reads_its_own_task_with_the_specification_revision(world, worker):
    agent, token = worker
    status, reply = world.read(token)
    task = world.task()
    assert status == 200
    assert reply == {
        "task_id": "task",
        "revision": spec_revision(task),
        "task": {
            "id": "task",
            "title": "Revision fixture",
            "description": "Build the first specification",
            "lane": "eng",
            "kind": "code",
            "phase": "p1",
            "state": "claimed",
            "claimed_by": agent.name,
        },
    }


def test_a_stale_specification_is_refused_and_a_refreshed_retry_lands(world, worker):
    _, token = worker
    old = world.read(token)[1]["revision"]
    world.operator({"description": "Build the second specification"})
    ledger = world.document()
    status, reply = world.update(token, "update-1", old, {"state": "pr", "pr_url": PR})
    current = spec_revision(world.task())
    assert (status, reply) == (
        409,
        detail(
            "revision_conflict",
            "the task specification changed; refresh and retry",
            "update-1",
            task_id="task",
            current_revision=current,
        ),
    )
    assert world.document() == ledger
    assert ledger_revision_conflicts_total(world.store, "fixture") == 1
    status, ack = world.update(token, "update-2", current, {"state": "pr", "pr_url": PR})
    assert status == 200
    assert ack == {
        "task_id": "task",
        "operation_id": "update-2",
        "revision": current,
        "ledger_revision": world.document()["_meta"]["rev"],
    }
    assert (world.task()["state"], world.task()["pr_url"]) == ("pr", PR)
    assert ledger_revision_conflicts_total(world.store, "fixture") == 1


def test_worker_updates_and_progress_leave_the_specification_revision_unchanged(world, worker):
    _, token = worker
    current = world.read(token)[1]["revision"]
    assert world.update(token, "update-1", current, {"issue_url": PR, "branch": "fixture"})[0] == 200
    assert world.progress(token, "progress-1", "Building the first slice")[0] == 200
    assert world.read(token)[1]["revision"] == current
    assert world.update(token, "update-2", current, {"state": "blocked"})[0] == 200
    assert world.task()["state"] == "blocked"


def test_progress_needs_no_revision_so_it_survives_a_specification_edit(world, worker):
    agent, token = worker
    world.operator({"description": "Build the second specification"})
    status, ack = world.progress(token, "progress-1", "Building the first slice")
    assert status == 200
    assert ack["revision"] == spec_revision(world.task())
    assert [(entry["by"], entry["text"]) for entry in world.task()["comments"]] == [
        (agent.name, "Building the first slice")
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"rank": "high"}, "only the operator changes rank"),
        ({"claimed_by": "eng-2@fixture", "depends_on": []}, "only the operator changes claimed_by, depends_on"),
        ({"description": "Mine now", "state": "pr"}, "only the operator changes description"),
        ({"state": "done"}, "a worker sets state only to claimed, blocked or pr"),
        ({"state": "open"}, "a worker sets state only to claimed, blocked or pr"),
    ],
)
def test_operator_only_fields_are_forbidden_and_change_nothing(world, worker, fields, message):
    _, token = worker
    current = world.read(token)[1]["revision"]
    ledger, protected = world.document(), world.protected()
    assert world.update(token, "update-1", current, fields) == (403, detail("forbidden_scope", message, "update-1"))
    assert world.document() == ledger
    assert world.protected() == protected


@pytest.mark.parametrize("path", ["/v2/swarm", "/v2/swarm/config", "/v2/swarm/gates/talk"])
def test_swarm_controls_refuse_worker_credentials(world, worker, path):
    _, token = worker
    protected = world.protected()
    body = {"operation_id": "caps", "max_eng_agents": 99}
    assert world.call("PATCH", path, token, body) == (
        403,
        detail("forbidden_scope", "swarm controls need the operator", "caps"),
    )
    assert world.protected() == protected


def test_swarm_controls_check_the_credential_first(world):
    assert world.call("PATCH", "/v2/swarm/config", "forged", {}) == (
        401,
        detail("unauthenticated", "launch grant is malformed"),
    )


def test_another_task_is_refused_without_its_content(world, worker):
    _, token = worker
    world.worker("eng-2@fixture", "other")
    for reply in (
        world.read(token, "other"),
        world.update(token, "update-1", "0" * 64, {"state": "pr"}, task="other"),
    ):
        assert reply[0] == 403
        assert reply[1]["message"] == "the credential is scoped to another task"
        assert sorted(reply[1]) == ["error_class", "message", "operation_id", "retry"]


def test_a_progress_replay_is_acknowledged_once_and_never_rewinds_a_newer_line(world, worker):
    _, token = worker
    first = world.progress(token, "progress-1", "Building the first slice")
    world.progress(token, "progress-2", "Building the second slice")
    ledger = world.document()
    restarted = TasksAPI(world.grants, world.tasks, world.reopened())
    assert world.progress(token, "progress-1", "Building the first slice", api=restarted) == first
    assert world.document() == ledger
    assert [entry["text"] for entry in world.task()["comments"]] == ["Building the second slice"]


def test_an_update_replay_returns_its_first_acknowledgement(world, worker):
    _, token = worker
    current = world.read(token)[1]["revision"]
    first = world.update(token, "update-1", current, {"state": "pr", "pr_url": PR})
    world.operator({"description": "Build the second specification"})
    ledger = world.document()
    assert world.update(token, "update-1", current, {"state": "pr", "pr_url": PR}) == first
    assert world.document() == ledger


def test_an_operation_id_reused_for_other_content_conflicts(world, worker):
    _, token = worker
    world.progress(token, "progress-1", "Building the first slice")
    ledger = world.document()
    assert world.progress(token, "progress-1", "Building the second slice") == (
        409,
        detail("operation_conflict", "the operation ID was already used for different content", "progress-1"),
    )
    assert world.document() == ledger


def test_a_replaced_worker_cannot_write_after_its_claim(world, worker):
    agent, token = worker
    current = world.read(token)[1]["revision"]
    world.clock[0] += 101
    ledger = world.document()
    assert world.progress(token, "progress-1", "Building the first slice") == (
        409,
        detail("stale_generation", "the task generation is no longer current", "progress-1"),
    )
    assert world.update(token, "update-1", current, {"state": "pr"}) == (
        409,
        detail("stale_generation", "the task generation is no longer current", "update-1"),
    )
    assert world.document() == ledger


def test_another_generation_replays_nothing_and_a_new_generation_starts_fresh_receipts(world, worker):
    agent, token = worker
    first = world.progress(token, "progress-1", "Building the first slice")
    world.clock[0] += 101
    _, successor = world.worker(previous=agent.execution_id)
    assert world.progress(token, "progress-1", "Building the first slice")[1]["error_class"] == "stale_generation"
    status, ack = world.progress(successor, "progress-1", "Building the first slice", generation=2)
    assert (status, ack["ledger_revision"] > first[1]["ledger_revision"]) == (200, True)
    assert list(world.document()["_meta"]["task_operations"]["task"]["operations"]) == ["progress-1"]
    assert world.document()["_meta"]["task_operations"]["task"]["generation"] == 2


def test_operation_ids_are_scoped_to_their_task(world, worker):
    _, token = worker
    _, other = world.worker("eng-2@fixture", "other")
    assert world.progress(token, "progress-1", "Building the first slice")[0] == 200
    body = {"operation_id": "progress-1", "task_generation": 1, "text": "Building the other slice"}
    assert world.call("POST", "/v2/tasks/other/progress", other, body)[0] == 200
    assert world.task("other")["comments"][0]["text"] == "Building the other slice"


def test_a_missing_or_malformed_expected_revision_is_refused(world, worker):
    _, token = worker
    world.operator({"description": "Build the second specification"})
    ledger = world.document()
    for expected in (None, "", "A" * 64, "0" * 63, 7):
        assert world.update(token, "update-1", expected, {"state": "pr"}) == (
            400,
            detail("invalid_request", "expected_revision must be a task revision from a read", "update-1"),
        )
    assert world.document() == ledger
    assert ledger_revision_conflicts_total(world.store, "fixture") == 0


def test_the_gate_refuses_a_write_the_ledger_rejects(world, worker):
    from types import SimpleNamespace

    from scripts.swarm_v2.api.tasks import WorkerGate

    agent, token = worker
    scope = world.grants.bound("fixture", token)
    request = {"operation_id": "update-1", "task_generation": 1}
    gate = WorkerGate(world.tasks_api, scope, request, {"op": "task_update", "item": "tasks/task"}, None)
    context = SimpleNamespace(meta={}, rev=9, dirty=False)
    doc = {"tasks": [world.task()]}
    with pytest.raises(refused_class()) as refused:
        gate.apply(doc, {**gate.op, "fields": {"state": "pr"}}, context, lambda *_: False)
    assert (refused.value.error_class, str(refused.value)) == ("invalid_request", "the ledger refused this write")
    assert (context.meta, context.dirty) == ({"task_operations": {}}, False)
    with pytest.raises(refused_class()) as missing:
        gate.apply({"tasks": []}, {**gate.op, "fields": {"state": "pr"}}, context, lambda *_: True)
    assert (missing.value.error_class, str(missing.value)) == ("invalid_request", "the task is not on the ledger")
    assert gate.op["id"] == "w-" + revision(["fixture", "task", "update-1"])[:16]
    assert agent.name == world.task()["claimed_by"]


def test_the_gate_accepts_and_records_a_write_then_accepts_its_replay(world, worker):
    from types import SimpleNamespace

    from scripts.swarm_v2.api.tasks import WorkerGate

    _, token = worker
    scope = world.grants.bound("fixture", token)
    request = {"operation_id": "update-1", "task_generation": 1}
    op = {"op": "task_update", "item": "tasks/task", "fields": {"state": "pr"}}
    task = world.task()
    context = SimpleNamespace(meta={}, rev=9, dirty=False)
    gate = WorkerGate(world.tasks_api, scope, request, op, spec_revision(task))
    assert gate.apply({"tasks": [task]}, gate.op, context, lambda *_: True) is True
    ack = {"task_id": "task", "operation_id": "update-1", "revision": spec_revision(task), "ledger_revision": 9}
    assert (gate.ack, context.dirty) == (ack, True)
    assert context.meta == {
        "task_operations": {"task": {"generation": 1, "operations": {"update-1": {"digest": gate.digest, "ack": ack}}}}
    }
    context.dirty, context.rev = False, 10
    replay = WorkerGate(world.tasks_api, scope, request, op, spec_revision(task))
    assert replay.apply({"tasks": []}, replay.op, context, lambda *_: False) is True
    assert (replay.ack, context.dirty) == (ack, False)
    other = WorkerGate(world.tasks_api, scope, request, op, "0" * 64)
    assert other.digest != gate.digest
    with pytest.raises(refused_class()) as reused:
        other.apply({"tasks": [task]}, other.op, context, lambda *_: True)
    assert str(reused.value) == "the operation ID was already used for different content"


def test_a_wrong_generation_is_stale(world, worker):
    _, token = worker
    assert world.progress(token, "progress-1", "Building", generation=2) == (
        409,
        detail("stale_generation", "the task generation is no longer current", "progress-1"),
    )
    assert world.progress(token, "progress-1", "Building", generation=0) == (
        400,
        detail("invalid_request", "task_generation must be a positive integer", "progress-1"),
    )


def test_disabled_writes_answer_read_only_conflicts_and_reads_still_work(world, worker):
    _, token = worker
    paused = TasksAPI(world.grants, world.tasks, world.ledger, writes_enabled=False)
    current = world.read(token)[1]["revision"]
    ledger = world.document()
    assert world.call("GET", "/v2/tasks/task", token, api=paused)[0] == 200
    expected = detail(
        "revision_conflict",
        "task writes are read only; refresh and retry later",
        "update-1",
        task_id="task",
        current_revision=current,
        read_only=True,
    )
    assert world.update(token, "update-1", current, {"state": "pr"}, api=paused) == (409, expected)
    assert world.update(token, "update-1", None, {"rank": "high"}, api=paused) == (409, expected)
    assert world.progress(token, "update-1", "Building", api=paused) == (409, expected)
    assert world.call("POST", "/v2/tasks/task/progress", token, {"operation_id": "update-1"}, api=paused) == (
        409,
        expected,
    )
    assert world.document() == ledger
    assert ledger_revision_conflicts_total(world.store, "fixture") == 0
    assert TasksAPI(world.grants, world.tasks, world.ledger).writes_enabled is True


@pytest.mark.parametrize(
    ("body", "message", "operation_id"),
    [
        ([], "the request carries exactly operation_id, task_generation, text", "unknown"),
        ({"operation_id": "p", "text": "x"}, "the request carries exactly operation_id, task_generation, text", "p"),
        (
            {"operation_id": "bad id", "task_generation": 1, "text": "x"},
            "operation_id must be an identifier",
            "unknown",
        ),
        ({"operation_id": 7, "task_generation": 1, "text": "x"}, "operation_id must be an identifier", "unknown"),
        ({"operation_id": "p", "task_generation": 1, "text": " "}, "text must be a non empty string", "p"),
        ({"operation_id": "p", "task_generation": 1, "text": 3}, "text must be a non empty string", "p"),
        (
            {"operation_id": "p", "task_generation": 1, "text": "Edited tasks.py"},
            "the ledger refused this write: comment refused, write plain words for the operator (what was done, "
            "or why it was skipped): file name or path 'tasks.py'",
            "p",
        ),
    ],
)
def test_malformed_progress_is_invalid(world, worker, body, message, operation_id):
    _, token = worker
    ledger = world.document()
    assert world.call("POST", "/v2/tasks/task/progress", token, body) == (
        400,
        detail("invalid_request", message, operation_id),
    )
    assert world.document() == ledger


@pytest.mark.parametrize("fields", [{}, [], "state"])
def test_an_update_names_at_least_one_worker_field(world, worker, fields):
    _, token = worker
    assert world.update(token, "update-1", "0" * 64, fields) == (
        400,
        detail("invalid_request", "fields must name at least one worker field", "update-1"),
    )


def test_a_ledger_refusal_is_invalid(world, worker):
    _, token = worker
    current = world.read(token)[1]["revision"]
    ledger = world.document()
    assert world.update(token, "update-1", current, {"pr_url": "not a link"}) == (
        400,
        detail("invalid_request", "the ledger refused this write: pr_url must be an http or https link", "update-1"),
    )
    assert world.document() == ledger


def test_a_task_missing_from_the_ledger_is_invalid(world, worker):
    _, token = worker
    with patch.object(world.ledger, "read", return_value={"tasks": []}):
        assert world.read(token) == (400, detail("invalid_request", "the task is not on the ledger"))


def test_transport_errors_answer_with_their_class(world, worker):
    _, token = worker
    assert world.tasks_api.route("GET", "/v2/tasks/task", token, None) == (
        401,
        detail("unauthenticated", "a bearer credential is required"),
    )
    assert world.call("DELETE", "/v2/tasks/task", token) == (404, detail("invalid_request", "no such task endpoint"))
    assert world.call("GET", "/v2/tasks/task/progress", token) == (
        404,
        detail("invalid_request", "no such task endpoint"),
    )
    from redis.exceptions import RedisError

    with patch.object(world.tasks, "current", side_effect=RedisError("down")):
        assert world.progress(token, "progress-1", "Building") == (
            503,
            detail("dependency_unavailable", "the task store is unavailable"),
        )


def test_a_claim_lost_during_the_write_rolls_the_write_back(world, worker):
    _, token = worker
    ledger = world.document()
    checks = iter([None, swarm_error("stale_generation")])

    def holder(*_):
        if (outcome := next(checks)) is not None:
            raise outcome

    with patch.object(world.tasks, "_holder", side_effect=holder):
        assert world.progress(token, "progress-1", "Building the first slice") == (
            409,
            detail("stale_generation", "the task generation is no longer current", "progress-1"),
        )
    assert world.document() == ledger


def test_an_unknown_authority_refusal_keeps_its_cause_private(world, worker):
    _, token = worker
    with patch.object(world.tasks, "_holder", side_effect=swarm_error("journal_conflict")):
        assert world.progress(token, "progress-1", "Building") == (
            503,
            detail("dependency_unavailable", "the task authority could not confirm the claim", "progress-1"),
        )


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case, tmp_path):
    from tests.sv2_ldg03_cases import run_case

    first_folder, second_folder = tmp_path / "first", tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    first, second = run_case(case, first_folder), run_case(case, second_folder)
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text(encoding="utf-8"))
    assert committed == {"case": f"T-SV2-LDG-03-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )


def test_the_manifest_names_the_hash_of_every_case_input():
    import hashlib

    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]
    assert manifest["inputs"] == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in ["tests/fixtures/swarm_v2/task-revision.json"]
    }
    assert manifest["cases"] == ["T-SV2-LDG-03-A", "T-SV2-LDG-03-B", "T-SV2-LDG-03-C"]


def test_the_package_record_carries_its_completion_evidence():
    record = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    assert (record["package"], record["package_complete"]) == ("SV2-LDG-03", False)
    assert set(record["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for name in ("interfaces", "supported_versions", "authoritative_objects", "remaining_limitations", "validation"):
        assert record[name], name
    assert record["cases"] == {
        **{case: f"evidence/SV2-LDG-03/{case}-result.json" for case in "abc"},
        "manifest": "evidence/SV2-LDG-03/manifest.json",
    }
    assert set(record["measurements"]["ledger_revision_conflicts_total"]) == {"a", "b", "c"}
    path, name = record["rollback_rehearsal"]["test"].split("::")
    assert path == "tests/test_swarm_v2_tasks_api.py"
    assert f"\ndef {name}(" in Path(__file__).read_text(encoding="utf-8")


def propose(world, token, operation_id, expected, outcome="done", proof="Pull request merged", generation=1):
    body = {
        "operation_id": operation_id,
        "task_generation": generation,
        "expected_revision": expected,
        "outcome": outcome,
        "proof": proof,
    }
    return world.call("POST", "/v2/tasks/task/outcomes", token, body)


def test_a_comment_replay_is_acknowledged_once(world, worker):
    agent, token = worker
    body = {"operation_id": "comment-1", "task_generation": 1, "text": "Asking about the second slice"}
    first = world.call("POST", "/v2/tasks/task/comments", token, body)
    assert first[0] == 200
    ledger = world.document()
    assert world.call("POST", "/v2/tasks/task/comments", token, body) == first
    assert world.call("POST", "/v2/tasks/task/progress", token, body) == first
    assert world.document() == ledger
    assert [(entry["by"], entry["text"]) for entry in world.task()["comments"]] == [
        (agent.name, "Asking about the second slice")
    ]


def test_an_outcome_proposal_needs_the_current_revision_and_lands_once(world, worker):
    agent, token = worker
    old = world.read(token)[1]["revision"]
    world.operator({"description": "Build the second specification"})
    ledger = world.document()
    status, reply = propose(world, token, "outcome-1", old)
    current = spec_revision(world.task())
    assert (status, reply["error_class"], reply["current_revision"]) == (409, "revision_conflict", current)
    assert world.document() == ledger
    assert ledger_revision_conflicts_total(world.store, "fixture") == 1
    first = propose(world, token, "outcome-2", current)
    assert first[0] == 200
    assert first[1]["revision"] == current
    proposed = world.document()
    assert propose(world, token, "outcome-2", current) == first
    assert world.document() == proposed
    status, reply = propose(world, token, "outcome-2", current, proof="Another proof")
    assert (status, reply["error_class"]) == (409, "operation_conflict")
    task = world.task()
    assert (task["done"], task["state"]) == (False, "claimed")
    assert [(entry["by"], entry["text"]) for entry in task["comments"]] == [
        (agent.name, "Outcome proposal: done. Pull request merged")
    ]


@pytest.mark.parametrize(
    ("outcome", "proof", "message"),
    [
        ("merged", "Pull request merged", "outcome must be done or blocked"),
        ("done", " ", "proof must be a non empty string"),
        ("done", 7, "proof must be a non empty string"),
    ],
)
def test_an_outcome_proposal_names_a_worker_outcome_and_its_proof(world, worker, outcome, proof, message):
    _, token = worker
    current = world.read(token)[1]["revision"]
    ledger = world.document()
    status, reply = propose(world, token, "outcome-1", current, outcome, proof)
    assert (status, reply) == (400, detail("invalid_request", message, "outcome-1"))
    assert world.document() == ledger


@pytest.fixture
def served(world):
    from scripts.swarm_v2.api.server import Routes, serve

    server = serve(Routes(world.api, world.tasks_api), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def send(port, method, path, token, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = b"" if body is None else json.dumps(body).encode()
    connection.request(method, path, body=data, headers={"Authorization": f"Bearer {token}"})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw)


def test_the_served_api_carries_task_reads_and_writes_beside_executions(world, served):
    agent, token = world.start()
    registration = {"execution_id": agent.execution_id, "generation": 1}
    assert send(served, "POST", "/v2/executions/register", token, registration)[0] == 200
    world.operator({"state": "claimed", "claimed_by": agent.name})
    status, read = send(served, "GET", "/v2/tasks/task", token)
    assert (status, read["revision"]) == (200, spec_revision(world.task()))
    world.operator({"description": "Build the second specification"})
    fields = {"state": "pr", "pr_url": PR}
    stale = {"operation_id": "update-1", "task_generation": 1, "expected_revision": read["revision"], "fields": fields}
    assert send(served, "PATCH", "/v2/tasks/task", token, stale)[1]["error_class"] == "revision_conflict"
    fresh = {**stale, "operation_id": "update-2", "expected_revision": spec_revision(world.task())}
    assert send(served, "PATCH", "/v2/tasks/task", token, fresh)[0] == 200
    progress = {"operation_id": "progress-1", "task_generation": 1, "text": "Building the first slice"}
    assert send(served, "POST", "/v2/tasks/task/progress", token, progress)[0] == 200
    comment = {**progress, "operation_id": "comment-1", "text": "Asking about the second slice"}
    assert send(served, "POST", "/v2/tasks/task/comments", token, comment)[0] == 200
    outcome = {
        "operation_id": "outcome-1",
        "task_generation": 1,
        "expected_revision": spec_revision(world.task()),
        "outcome": "done",
        "proof": "Pull request merged",
    }
    assert send(served, "POST", "/v2/tasks/task/outcomes", token, outcome)[0] == 200
    assert [entry["text"] for entry in world.task()["comments"]] == [
        "Building the first slice",
        "Asking about the second slice",
        "Outcome proposal: done. Pull request merged",
    ]
    assert send(served, "PATCH", "/v2/swarm/config", token, {"max_eng_agents": 99})[0] == 403
    assert (world.task()["state"], world.task()["pr_url"]) == ("pr", PR)
