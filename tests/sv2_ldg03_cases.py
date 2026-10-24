import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from scripts.swarm_v2.api.tasks import TasksAPI, ledger_revision_conflicts_total
from tests import sv2_ldg02_cases

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/task-revision.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = sv2_ldg02_cases.SLUG
TASK = sv2_ldg02_cases.INPUTS["task"]
OTHER = sv2_ldg02_cases.INPUTS["other_task"]


class World(sv2_ldg02_cases.World):
    def __init__(self, monkeypatch, folder: Path):
        from tests.swarm_ledger.test_tasks import core, new_ledger

        super().__init__(monkeypatch)
        self.folder, self.core = folder, core
        self.ledger = self.reopened()
        content = {
            "title": "Task revision fixture",
            "overview": "Stale workers never overwrite a newer specification",
            "sources": [],
            "phases": [{"title": "Ledger", "description": "Revision aware task writes"}],
            "tasks": [],
        }
        document = new_ledger.build_doc(content)
        document["tasks"] = [
            {**INPUTS["spec"], "id": task_id, "state": "open", "done": False} for task_id in (TASK, OTHER)
        ]
        document, meta, _ = core.load_state(folder / "seed.json", document)
        self.ledger.create_document(SLUG, document, meta)
        self.tasks_api = TasksAPI(self.grants, self.tasks, self.ledger)
        self.edits = 0

    def reopened(self):
        return SQLiteLedgerRepository(self.folder / "ledger.sqlite", self.core)

    def worker(self, seat=sv2_ldg02_cases.INPUTS["seat"], task=TASK, previous=""):
        agent, token = self.start(seat, task, previous)
        assert self.register(agent, token)[0] == 200
        self.operator({"state": "claimed", "claimed_by": agent.name}, task)
        return agent, token

    def operator(self, fields, task=TASK):
        self.edits += 1
        op = {"id": f"operator-{self.edits}", "op": "task_update", "by": "operator", "item": f"tasks/{task}"}
        self.ledger.apply_ops(SLUG, ops=[{**op, "fields": fields}])

    def document(self):
        return self.ledger.get_document(SLUG)

    def task(self, task=TASK):
        return next(row for row in self.document()["tasks"] if row["id"] == task)

    def call(self, method, path, token, body=None, api=None):
        return (api or self.tasks_api).route(method, path, f"Bearer {token}", body)

    def read(self, token, task=TASK):
        return self.call("GET", f"/v2/tasks/{task}", token)

    def update(self, token, operation_id, expected, fields, generation=1, task=TASK, api=None):
        body = {
            "operation_id": operation_id,
            "task_generation": generation,
            "expected_revision": expected,
            "fields": fields,
        }
        return self.call("PATCH", f"/v2/tasks/{task}", token, body, api)

    def progress(self, token, operation_id, text, generation=1, api=None):
        body = {"operation_id": operation_id, "task_generation": generation, "text": text}
        return self.call("POST", f"/v2/tasks/{TASK}/progress", token, body, api)


def refusal(reply):
    return [reply[0], reply[1]["error_class"], reply[1]["retry"]]


def _positive(world):
    agent, token = world.worker()
    status, before = world.read(token)
    world.operator({"description": INPUTS["edited_description"]})
    ledger = copy.deepcopy(world.document())
    stale = world.update(token, "update-stale", before["revision"], INPUTS["worker_fields"])
    assert world.document() == ledger
    refreshed = world.read(token)
    retried = world.update(token, "update-fresh", refreshed[1]["revision"], INPUTS["worker_fields"])
    progressed = world.progress(token, "progress-1", INPUTS["progress"][0])
    task = world.task()
    return {
        "read": status,
        "stale_update": [*refusal(stale), stale[1]["current_revision"] == refreshed[1]["revision"]],
        "revision_changed_by_edit": before["revision"] != refreshed[1]["revision"],
        "retried": [retried[0], retried[1]["revision"] == refreshed[1]["revision"]],
        "progress_after_edit": progressed[0],
        "task": {name: task.get(name) for name in ("description", "state", "pr_url", "claimed_by")}
        | {"claimed_by": task["claimed_by"] == agent.name},
        "comments": [entry["text"] for entry in task["comments"]],
        "ledger_revision_conflicts_total": ledger_revision_conflicts_total(world.store, SLUG),
    }


def _rejection(world):
    agent, token = world.worker()
    world.worker(sv2_ldg02_cases.INPUTS["other_seat"], OTHER)
    current = world.read(token)[1]["revision"]
    protected, ledger = world.protected(), copy.deepcopy(world.document())
    fields = [
        refusal(world.update(token, f"operator-field-{index}", current, change))
        for index, change in enumerate(INPUTS["operator_fields"])
    ]
    caps = world.call("PATCH", "/v2/swarm/config", token, INPUTS["control"])
    other = world.read(token, OTHER)
    assert world.protected() == protected
    assert world.document() == ledger
    return {
        "operator_fields": fields,
        "global_caps": refusal(caps),
        "other_task_read": [*refusal(other), sorted(other[1])],
        "ledger_revision_conflicts_total": ledger_revision_conflicts_total(world.store, SLUG),
    }


def _recovery(world):
    agent, token = world.worker()
    first = world.progress(token, "progress-1", INPUTS["progress"][0])
    second = world.progress(token, "progress-2", INPUTS["progress"][1])
    ledger = copy.deepcopy(world.document())
    restarted = TasksAPI(world.grants, world.tasks, world.reopened())
    replayed = world.progress(token, "progress-1", INPUTS["progress"][0], api=restarted)
    reused = world.progress(token, "progress-1", INPUTS["progress"][1], api=restarted)
    assert replayed == first
    assert world.document() == ledger
    world.clock[0] += sv2_ldg02_cases.INPUTS["lease_ms"] + 1
    successor, successor_token = world.worker(previous=agent.execution_id)
    ledger = copy.deepcopy(world.document())
    late = world.progress(token, "progress-3", INPUTS["progress"][0])
    assert world.document() == ledger
    current = world.read(successor_token)[1]["revision"]
    paused = TasksAPI(world.grants, world.tasks, world.ledger, writes_enabled=False)
    read_only = world.call("GET", f"/v2/tasks/{TASK}", successor_token, api=paused)
    blocked = world.update(successor_token, "update-paused", current, INPUTS["worker_fields"], 2, api=paused)
    assert world.document() == ledger
    return {
        "replayed": [replayed[0], replayed[1] == first[1]],
        "second_kept": [second[0], [entry["text"] for entry in world.task()["comments"]]],
        "reused_operation_id": refusal(reused),
        "stale_attempt": refusal(late),
        "rollback": {
            "read": read_only[0],
            "write": [*refusal(blocked), blocked[1]["current_revision"] == current],
        },
        "ledger_revision_conflicts_total": ledger_revision_conflicts_total(world.store, SLUG),
    }


def run_case(case, folder: Path):
    with pytest.MonkeyPatch.context() as patch:
        world = World(patch, folder)
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](world)
        return {
            "case": f"T-SV2-LDG-03-{case.upper()}",
            "state": "passed",
            "evidence_class": "mocked Redis and server clock; SQLite fixture ledger; signed fixture grants",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": observed,
        }
