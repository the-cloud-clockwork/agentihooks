import json
from pathlib import Path
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

from scripts.swarm_v2.api.tasks import SPEC_FIELDS, TasksAPI, revision_conflicts_total, spec_revision

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-LDG-03"
PR = "https://github.com/the-cloud-clockwork/agentihooks/pull/1"


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
    assert revision_conflicts_total(world.store, "fixture") == 1
    status, ack = world.update(token, "update-2", current, {"state": "pr", "pr_url": PR})
    assert status == 200
    assert ack == {
        "task_id": "task",
        "operation_id": "update-2",
        "revision": current,
        "ledger_revision": world.document()["_meta"]["rev"],
    }
    assert (world.task()["state"], world.task()["pr_url"]) == ("pr", PR)
    assert revision_conflicts_total(world.store, "fixture") == 1


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
    restarted = TasksAPI(world.grants, world.tasks, world.ledger)
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
    assert world.update(token, "update-1", current, {"state": "pr"}, generation=2)[1]["error_class"] == (
        "stale_generation"
    )
    assert world.document() == ledger


def test_a_wrong_generation_is_stale(world, worker):
    _, token = worker
    assert world.progress(token, "progress-1", "Building", generation=2)[:1] == (409,)
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
    )
    assert world.update(token, "update-1", current, {"state": "pr"}, api=paused) == (409, expected)
    assert world.progress(token, "update-1", "Building", api=paused) == (409, expected)
    assert world.document() == ledger
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
            "progress refused: file name or path 'tasks.py'",
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
    assert world.update(token, "update-1", current, {"pr_url": "not a link"}) == (
        400,
        detail("invalid_request", "the ledger refused this write", "update-1"),
    )


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
    with patch.object(world.tasks, "current", side_effect=RedisError("down")):
        assert world.progress(token, "progress-1", "Building") == (
            503,
            detail("dependency_unavailable", "the task store is unavailable"),
        )


def test_receipts_keep_only_the_newest_thousand(world, worker, monkeypatch):
    _, token = worker
    monkeypatch.setattr("scripts.swarm_v2.api.tasks.KEPT_RECEIPTS", 2)
    for index in range(3):
        assert world.progress(token, f"progress-{index}", f"Building slice {'abc'[index]}")[0] == 200
    assert list(world.document()["_meta"]["task_operations"]) == ["progress-1", "progress-2"]


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case, tmp_path):
    from tests.sv2_ldg03_cases import run_case

    first_folder, second_folder = tmp_path / "first", tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    first, second = run_case(case, first_folder), run_case(case, second_folder)
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text())
    assert committed == {"case": f"T-SV2-LDG-03-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )
